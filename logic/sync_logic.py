"""
Ядро синхронізації Google Groups — «delta-sync» через операції над множинами.

Алгоритм:
  desired_set — множина email-адрес, які МАЮ бути в групі (з Triton DB + LDAP).
  actual_set  — множина email-адрес, які ЗАРАЗ є в групі (з Google API).

  to_add    = desired_set - actual_set   (нові члени, яких ще немає)
  to_remove = actual_set  - desired_set  (застарілі члени, яких вже не має бути)
  unchanged = desired_set ∩ actual_set   (члени, яких чіпати не потрібно)

Перевага множин: O(1) для перевірки належності, O(n) для різниці/перетину.
"""

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from google_api.google_groups_api import GoogleGroupsAPI

logger = logging.getLogger(__name__)


@dataclass
class SyncDiff:
    """
    Результат порівняння бажаного та поточного складу групи.

    Всі множини містять нормалізовані (lower-case, без пробілів) email-адреси.
    """

    to_add: set[str] = field(default_factory=set)
    to_remove: set[str] = field(default_factory=set)
    unchanged: set[str] = field(default_factory=set)

    @property
    def is_empty(self) -> bool:
        """True якщо жодних змін не потрібно."""
        return not self.to_add and not self.to_remove

    def summary(self) -> str:
        """Коротке текстове зведення для логів."""
        return (
            f"+{len(self.to_add)} додати, "
            f"-{len(self.to_remove)} видалити, "
            f"={len(self.unchanged)} без змін"
        )


def compute_diff(
    desired_emails: set[str] | list[str],
    actual_emails: set[str] | list[str],
) -> SyncDiff:
    """
    Обчислює різницю між бажаним та поточним складом групи.

    Нормалізує всі адреси перед порівнянням (lower-case, strip),
    щоб уникнути помилок через різний регістр або пробіли.

    Args:
        desired_emails: Множина/список email-адрес, які мають бути в групі.
        actual_emails:  Множина/список email-адрес, які зараз є в групі.

    Returns:
        SyncDiff із трьома множинами: to_add, to_remove, unchanged.
    """
    # Нормалізація та очищення від порожніх рядків
    desired: set[str] = {
        e.strip().lower() for e in desired_emails if e and e.strip()
    }
    actual: set[str] = {
        e.strip().lower() for e in actual_emails if e and e.strip()
    }

    diff = SyncDiff(
        to_add=desired - actual,
        to_remove=actual - desired,
        unchanged=desired & actual,
    )

    logger.info("Delta-sync: %s", diff.summary())

    if diff.to_add:
        logger.debug("До додавання: %s", sorted(diff.to_add)[:10])
    if diff.to_remove:
        logger.debug("До видалення: %s", sorted(diff.to_remove)[:10])

    return diff


def apply_diff(
    group_email: str,
    diff: SyncDiff,
    api: "GoogleGroupsAPI",
    dry_run: bool = True,
    batch_size: int = 100,
) -> SyncDiff:
    """
    Застосовує розрахований SyncDiff до Google Group через API.

    При dry_run=True лише логує заплановані зміни, не виконуючи реальних
    HTTP-запитів до Google API. Це безпечний режим для перевірки.

    Args:
        group_email: Email-адреса цільової Google Group.
        diff:        Розрахований delta-diff (результат compute_diff).
        api:         Екземпляр GoogleGroupsAPI або MockGroupsAPI.
        dry_run:     True — тільки лог, False — реальні зміни.
        batch_size:  Розмір пакету для batch-запитів (передається у api).

    Returns:
        Той самий об'єкт SyncDiff (для зручності ланцюжка викликів).
    """
    if diff.is_empty:
        logger.info("Група %s: зміни не потрібні", group_email)
        return diff

    if dry_run:
        logger.info(
            "[DRY RUN] Група %s: було б: %s (без реальних змін)",
            group_email,
            diff.summary(),
        )
        return diff

    # --- Додавання нових членів ---
    if diff.to_add:
        add_list = sorted(diff.to_add)
        logger.info(
            "Додаю %d членів до %s ...", len(add_list), group_email
        )
        # Розбиваємо на пакети на рівні логіки (API також може мати свій батчинг)
        for offset in range(0, len(add_list), batch_size):
            chunk = add_list[offset: offset + batch_size]
            api.batch_add(group_email, chunk)
            logger.debug(
                "Додано пакет %d–%d для %s",
                offset + 1,
                offset + len(chunk),
                group_email,
            )

    # --- Видалення застарілих членів ---
    if diff.to_remove:
        remove_list = sorted(diff.to_remove)
        logger.info(
            "Видаляю %d членів з %s ...", len(remove_list), group_email
        )
        for offset in range(0, len(remove_list), batch_size):
            chunk = remove_list[offset: offset + batch_size]
            api.batch_remove(group_email, chunk)
            logger.debug(
                "Видалено пакет %d–%d з %s",
                offset + 1,
                offset + len(chunk),
                group_email,
            )

    logger.info("Синхронізацію групи %s завершено", group_email)
    return diff
