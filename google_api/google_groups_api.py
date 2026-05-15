"""
Клієнт Google Workspace Directory API для управління членами груп.

Автентифікація: Service Account + Domain-Wide Delegation.
  Service Account — технічний акаунт Google Cloud із ключем (JSON-файл).
  Domain-Wide Delegation — дозволяє SA діяти від імені адміністратора домену.

Обробка помилок:
  HTTP 404 (Group Not Found) — WARNING у лог, повертається порожній список.
  HTTP 409 (Already Member)  — ігнорується тихо (вважається успіхом).
  HTTP 429 (Too Many Requests) — exponential backoff (1s, 2s, 4s, 8s, ...).

Оптимізація через batch requests:
  Google API дозволяє об'єднати до 1000 запитів в один HTTP-виклик.
  Це суттєво зменшує кількість мережевих з'єднань і час виконання.
  Реалізовано через self.service.new_batch_http_request().
"""

import logging
import time

logger = logging.getLogger(__name__)

# Максимальна кількість повторних спроб при HTTP 429
_MAX_RETRIES: int = 6
# Верхня межа затримки між спробами (секунд)
_MAX_BACKOFF_SECONDS: int = 64


def _with_backoff(func, *args, max_retries: int = _MAX_RETRIES, **kwargs):
    """
    Виконує функцію із повторними спробами при HTTP 429 (Too Many Requests).

    Алгоритм: затримка подвоюється після кожної невдалої спроби —
    1s → 2s → 4s → 8s → 16s → 32s → (не більше _MAX_BACKOFF_SECONDS).
    Усі інші HTTP-помилки пробрасуються негайно без повтору.

    Args:
        func:        Викликаний об'єкт (зазвичай request.execute).
        max_retries: Максимальна кількість спроб.

    Returns:
        Результат виклику func.

    Raises:
        HttpError: При непередбаченій HTTP-помилці або вичерпанні спроб.
    """
    from googleapiclient.errors import HttpError  # noqa: PLC0415

    delay = 1
    for attempt in range(max_retries):
        try:
            return func(*args, **kwargs)
        except HttpError as exc:
            if exc.resp.status == 429 and attempt < max_retries - 1:
                wait = min(delay, _MAX_BACKOFF_SECONDS)
                logger.warning(
                    "HTTP 429 Too Many Requests — чекаємо %ds "
                    "(спроба %d/%d)",
                    wait,
                    attempt + 1,
                    max_retries,
                )
                time.sleep(wait)
                delay *= 2
                continue
            # Будь-яка інша помилка або вичерпані спроби — пробрасуємо
            raise

    raise RuntimeError(
        f"Вичерпано {max_retries} спроб для запиту до Google API"
    )


class GoogleGroupsAPI:
    """
    Клієнт Google Workspace Directory API (admin.directory.v1).

    Управляє складом Google Groups: читання членів, додавання, видалення.
    Підтримує batch requests для мінімізації кількості HTTP-запитів.

    Приклад використання:
        api = GoogleGroupsAPI(
            service_account_file="service_account.json",
            admin_email="admin@knu.ua",
            batch_size=100,
        )
        current = api.get_members("students-frecs-1@knu.ua")
        api.batch_add("students-frecs-1@knu.ua", ["student@knu.ua"])
    """

    # Мінімальний набір OAuth 2.0 scopes для управління групами
    SCOPES: list[str] = [
        "https://www.googleapis.com/auth/admin.directory.group",
        "https://www.googleapis.com/auth/admin.directory.group.member",
        "https://www.googleapis.com/auth/admin.directory.user.readonly",
    ]

    def __init__(
        self,
        service_account_file: str,
        admin_email: str,
        batch_size: int = 100,
    ) -> None:
        """
        Ініціалізує Google Directory API клієнт.

        Args:
            service_account_file: Шлях до JSON-файлу ключа Service Account
                                  (завантажується з Google Cloud Console).
            admin_email:          Email адміністратора домену. Під його іменем
                                  буде виконуватись Domain-Wide Delegation.
            batch_size:           Максимальна кількість запитів в одному batch.
                                  Google API підтримує до 1000, рекомендовано 100.

        Note:
            Якщо ініціалізація не вдалася (невалідний ключ, немає файлу тощо),
            self.service буде None. Будь-який подальший виклик методу підніме
            RuntimeError з описом причини.
        """
        self.batch_size = batch_size
        self.admin_email = admin_email
        self.service = None
        self.init_error: str | None = None

        try:
            from google.oauth2 import service_account  # noqa: PLC0415
            from googleapiclient.discovery import build  # noqa: PLC0415

            credentials = service_account.Credentials.from_service_account_file(
                service_account_file,
                scopes=self.SCOPES,
            )
            # Domain-Wide Delegation: SA діє від імені адміністратора домену
            delegated = credentials.with_subject(admin_email)

            self.service = build(
                "admin",
                "directory_v1",
                credentials=delegated,
                cache_discovery=False,  # уникаємо кешування, яке ламається на CI
            )
            logger.info(
                "GoogleGroupsAPI ініціалізовано (admin=%s)", admin_email
            )

        except Exception as exc:
            logger.error(
                "Не вдалося ініціалізувати GoogleGroupsAPI: %s", exc
            )
            self.init_error = str(exc)

    # ------------------------------------------------------------------
    # Внутрішній захист
    # ------------------------------------------------------------------

    def _check_ready(self) -> None:
        """
        Перевіряє, чи готовий API до роботи.

        Raises:
            RuntimeError: Якщо service не ініціалізовано.
        """
        if self.service is None:
            raise RuntimeError(
                f"GoogleGroupsAPI не ініціалізовано. "
                f"Причина: {self.init_error}"
            )

    # ------------------------------------------------------------------
    # Читання членів групи
    # ------------------------------------------------------------------

    def get_members(self, group_email: str) -> list[str]:
        """
        Отримує список email-адрес усіх членів групи (з пагінацією).

        При HTTP 404 (група не існує) — повертає [] та записує WARNING.
        Це дозволяє безпечно обробляти ще не створені групи.

        Args:
            group_email: Email-адреса Google Group.

        Returns:
            Список нормалізованих email-адрес (lower-case, без дублів).

        Raises:
            HttpError: При будь-якій HTTP-помилці, крім 404 і 429.
        """
        self._check_ready()

        try:
            from googleapiclient.errors import HttpError  # noqa: PLC0415

            members: list[str] = []
            # Список членів може бути пагінованим — обходимо всі сторінки
            request = self.service.members().list(groupKey=group_email)

            while request is not None:
                response = _with_backoff(request.execute)
                for member in response.get("members", []):
                    email = member.get("email", "").strip().lower()
                    if email:
                        members.append(email)
                # Отримуємо наступну сторінку (або None якщо кінець)
                request = self.service.members().list_next(
                    previous_request=request,
                    previous_response=response,
                )

            logger.info(
                "Отримано %d членів групи %s", len(members), group_email
            )
            return members

        except Exception as exc:
            from googleapiclient.errors import HttpError  # noqa: PLC0415

            if isinstance(exc, HttpError) and exc.resp.status == 404:
                logger.warning(
                    "Група %s не знайдена (404) — вважаємо порожньою",
                    group_email,
                )
                return []
            logger.error(
                "Помилка отримання членів групи %s: %s", group_email, exc
            )
            raise

    # ------------------------------------------------------------------
    # Додавання членів (batch)
    # ------------------------------------------------------------------

    def batch_add(self, group_email: str, emails: list[str]) -> list[str]:
        """
        Додає список членів до групи через Google API batch requests.

        Batch requests — це HTTP Multipart-запит, що вміщує до 1000 підзапитів.
        Зменшує накладні витрати мережі у десятки разів порівняно з поодинокими
        викликами.

        HTTP 409 (Already a member) обробляється тихо як успіх — дозволяє
        безпечно повторно запускати синхронізацію.

        Args:
            group_email: Email-адреса цільової Google Group.
            emails:      Список email-адрес для додавання.

        Returns:
            Список фактично доданих (або вже існуючих) email-адрес.
        """
        self._check_ready()
        if not emails:
            return []

        added: list[str] = []
        errors: list[tuple[str, str]] = []

        for chunk_start in range(0, len(emails), self.batch_size):
            chunk = emails[chunk_start: chunk_start + self.batch_size]
            batch = self.service.new_batch_http_request()

            for email in chunk:
                # Замикання через допоміжну функцію, щоб правильно захопити
                # email у callback (інакше всі callback'и отримають одне значення)
                req = self.service.members().insert(
                    groupKey=group_email,
                    body={"email": email, "role": "MEMBER"},
                )
                batch.add(req, callback=self._make_add_callback(
                    email, group_email, added, errors
                ))

            _with_backoff(batch.execute)
            logger.debug(
                "Batch add [%s]: оброблено %d запитів",
                group_email,
                len(chunk),
            )

        if errors:
            logger.warning(
                "Не вдалося додати %d адрес до %s: перші 5: %s",
                len(errors),
                group_email,
                errors[:5],
            )

        logger.info(
            "Додано %d з %d членів до %s",
            len(added),
            len(emails),
            group_email,
        )
        return added

    @staticmethod
    def _make_add_callback(
        email: str,
        group_email: str,
        added: list[str],
        errors: list[tuple[str, str]],
    ):
        """
        Фабрика callback-функцій для batch add.

        Окрема функція (не лямбда в циклі) потрібна для правильного захоплення
        змінної email у замиканні Python.
        """
        def _callback(request_id, response, exception):
            if exception is None:
                logger.debug("Додано %s → %s", email, group_email)
                added.append(email)
                return

            from googleapiclient.errors import HttpError  # noqa: PLC0415

            if isinstance(exception, HttpError):
                if exception.resp.status == 409:
                    # 409 = вже є членом — вважаємо успіхом
                    logger.debug("%s вже є членом %s", email, group_email)
                    added.append(email)
                    return
                if exception.resp.status == 404:
                    # Група не існує — логуємо ERROR
                    logger.error(
                        "Група %s не знайдена при додаванні %s",
                        group_email,
                        email,
                    )
                    errors.append((email, str(exception)))
                    return

            logger.error(
                "Помилка додавання %s → %s: %s", email, group_email, exception
            )
            errors.append((email, str(exception)))

        return _callback

    # ------------------------------------------------------------------
    # Видалення членів (batch)
    # ------------------------------------------------------------------

    def batch_remove(self, group_email: str, emails: list[str]) -> list[str]:
        """
        Видаляє список членів з групи через Google API batch requests.

        HTTP 404 для окремого члена (вже не є членом) обробляється тихо —
        безпечно при повторному запуску синхронізації.

        Args:
            group_email: Email-адреса цільової Google Group.
            emails:      Список email-адрес для видалення.

        Returns:
            Список фактично видалених (або вже відсутніх) email-адрес.
        """
        self._check_ready()
        if not emails:
            return []

        removed: list[str] = []
        errors: list[tuple[str, str]] = []

        for chunk_start in range(0, len(emails), self.batch_size):
            chunk = emails[chunk_start: chunk_start + self.batch_size]
            batch = self.service.new_batch_http_request()

            for email in chunk:
                req = self.service.members().delete(
                    groupKey=group_email,
                    memberKey=email,
                )
                batch.add(req, callback=self._make_remove_callback(
                    email, group_email, removed, errors
                ))

            _with_backoff(batch.execute)
            logger.debug(
                "Batch remove [%s]: оброблено %d запитів",
                group_email,
                len(chunk),
            )

        if errors:
            logger.warning(
                "Не вдалося видалити %d адрес з %s: перші 5: %s",
                len(errors),
                group_email,
                errors[:5],
            )

        logger.info(
            "Видалено %d з %d членів з %s",
            len(removed),
            len(emails),
            group_email,
        )
        return removed

    @staticmethod
    def _make_remove_callback(
        email: str,
        group_email: str,
        removed: list[str],
        errors: list[tuple[str, str]],
    ):
        """Фабрика callback-функцій для batch remove."""
        def _callback(request_id, response, exception):
            if exception is None:
                logger.debug("Видалено %s ← %s", email, group_email)
                removed.append(email)
                return

            from googleapiclient.errors import HttpError  # noqa: PLC0415

            if isinstance(exception, HttpError):
                if exception.resp.status == 404:
                    # 404 = вже не є членом — вважаємо успіхом
                    logger.debug(
                        "%s вже відсутній у %s", email, group_email
                    )
                    removed.append(email)
                    return

            logger.error(
                "Помилка видалення %s ← %s: %s", email, group_email, exception
            )
            errors.append((email, str(exception)))

        return _callback


# ---------------------------------------------------------------------------
# Mock-реалізація для тестування без реального Google API
# ---------------------------------------------------------------------------

class MockGroupsAPI:
    """
    In-memory заглушка GoogleGroupsAPI для режиму backend='mock'.

    Зберігає стан груп у пам'яті. Ніяких мережевих запитів не виконує.
    Корисна для локального запуску та тестування бізнес-логіки.
    """

    def __init__(self, initial_members: dict[str, list[str]] | None = None) -> None:
        """
        Args:
            initial_members: Початковий стан груп.
                             Ключ — group_email, значення — список emails.
        """
        # Нормалізуємо початковий стан
        self._groups: dict[str, set[str]] = {}
        if initial_members:
            for group, members in initial_members.items():
                self._groups[group.lower()] = {
                    m.strip().lower() for m in members if m
                }

    def get_members(self, group_email: str) -> list[str]:
        """Повертає поточних членів групи (або [] якщо група не існує)."""
        key = group_email.strip().lower()
        members = sorted(self._groups.get(key, set()))
        logger.info(
            "[MOCK] get_members(%s) → %d членів", group_email, len(members)
        )
        return members

    def batch_add(self, group_email: str, emails: list[str]) -> list[str]:
        """Додає членів до in-memory групи."""
        key = group_email.strip().lower()
        if key not in self._groups:
            self._groups[key] = set()

        added = []
        for email in emails:
            norm = email.strip().lower()
            if norm:
                self._groups[key].add(norm)
                added.append(norm)

        logger.info(
            "[MOCK] batch_add(%s): додано %d", group_email, len(added)
        )
        return added

    def batch_remove(self, group_email: str, emails: list[str]) -> list[str]:
        """Видаляє членів з in-memory групи."""
        key = group_email.strip().lower()
        group_members = self._groups.get(key, set())
        removed = []
        for email in emails:
            norm = email.strip().lower()
            if norm in group_members:
                group_members.discard(norm)
                removed.append(norm)

        logger.info(
            "[MOCK] batch_remove(%s): видалено %d", group_email, len(removed)
        )
        return removed
