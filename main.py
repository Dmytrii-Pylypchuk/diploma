"""
Точка входу синхронізації Google Groups із Triton DB через AD.

Конвеєр:
  TritonDB (SQL)
    -> StudentRecord (speciality_id, year, course_id, group_name)
      -> LoginsProvider (JSON: StudentId -> ADUserName)
        -> LDAPClient (NTLM, userPrincipalName / sAMAccountName)
          -> email (msExchExtensionAttribute45)
            -> GoogleGroupsAPI (delta-sync: to_add / to_remove)

Запуск:
  python main.py                         # читає config.json поруч
  python main.py --config /path/to/cfg
"""

import argparse
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from data_providers.ldap_client import LDAPClient
from data_providers.logins_provider import LoginsProvider
from data_providers.triton_db import StudentRecord, TritonDB
from google_api.google_groups_api import GoogleGroupsAPI, MockGroupsAPI
from logic.sync_logic import SyncDiff, apply_diff, compute_diff
from utils.config_loader import (
    get_backend,
    get_db_config,
    get_google_config,
    get_groups_config,
    get_ldap_config,
    get_logging_config,
    is_dry_run,
    load_config,
)
from utils.email_tools import normalize_and_validate
from utils.logger import setup_logger

logger = logging.getLogger("triton-sync")


# ---------------------------------------------------------------------------
# Типи результатів
# ---------------------------------------------------------------------------


@dataclass
class GroupSyncResult:
    """Підсумок синхронізації однієї групи."""

    group_email: str
    students_from_db: int
    logins_resolved: int   # скільки студентів мають ADUserName
    emails_resolved: int   # скільки email знайдено в AD
    actual_emails: int
    diff: SyncDiff
    error: str | None = None

    @property
    def success(self) -> bool:
        return self.error is None

    def log_summary(self, log: logging.Logger) -> None:
        if self.error:
            log.error("  [FAIL] %s: %s", self.group_email, self.error)
            return
        log.info(
            "  [OK]   %s: студентів=%d, логінів=%d, email=%d, "
            "поточних=%d, %s",
            self.group_email,
            self.students_from_db,
            self.logins_resolved,
            self.emails_resolved,
            self.actual_emails,
            self.diff.summary(),
        )


# ---------------------------------------------------------------------------
# Крок 1: SQL -> StudentRecord
# ---------------------------------------------------------------------------


def fetch_students(db: TritonDB, rule: dict[str, Any]) -> list[StudentRecord]:
    """
    Завантажує студентів із Triton DB за правилом із конфігурації.

    rule може містити: speciality_id, year, course_id, group_name.
    """
    speciality_id = rule.get("speciality_id")
    year = rule.get("year")

    if speciality_id is None or year is None:
        raise ValueError(
            "Правило синхронізації має містити 'speciality_id' та 'year'. "
            f"Отримано: {rule}"
        )

    return db.get_students(
        speciality_id=int(speciality_id),
        year=str(year),
        course_id=rule.get("course_id"),
        group_name=rule.get("group_name"),
        faculty_id=rule.get("faculty_id"),
        education_form_id=rule.get("education_form_id"),
        level_of_education_id=rule.get("level_of_education_id"),
    )


# ---------------------------------------------------------------------------
# Крок 2: StudentRecord -> desired email set (JSON logins + AD LDAP)
# ---------------------------------------------------------------------------


def resolve_emails(
    students: list[StudentRecord],
    logins: LoginsProvider,
    ad: LDAPClient,
) -> tuple[set[str], int, dict[str, str]]:
    """
    Отримує множину корпоративних email через два кроки:
      1. JSON-файл: StudentId -> ADUserName
      2. AD (LDAP NTLM): ADUserName -> msExchExtensionAttribute45

    Returns:
        (emails, no_login_count, names) — множина валідних email, кількість
        студентів без логіна, та маппінг email -> ПІБ студента.
    """
    emails: set[str] = set()
    names: dict[str, str] = {}
    no_login = 0
    no_email = 0

    for student in students:
        login = logins.get_login(student.student_id)
        if not login:
            logger.debug(
                "JSON: логін відсутній для %s (id=%d)",
                student.full_name, student.student_id,
            )
            no_login += 1
            continue

        raw_email = ad.get_email_by_login(login)
        if not raw_email:
            logger.warning(
                "AD: email не знайдено для %s (login=%r)",
                student.full_name, login,
            )
            no_email += 1
            continue

        validated = normalize_and_validate(raw_email)
        if validated:
            emails.add(validated)
            names[validated] = student.full_name
        else:
            logger.warning(
                "Невалідний email з AD для %s (login=%r): %r",
                student.full_name, login, raw_email,
            )
            no_email += 1

    logger.info(
        "Resolve emails: %d студентів, без логіна=%d, без email=%d, знайдено=%d",
        len(students), no_login, no_email, len(emails),
    )
    return emails, no_login, names


# ---------------------------------------------------------------------------
# Основний цикл синхронізації однієї групи
# ---------------------------------------------------------------------------


def sync_one_group(
    group_cfg: dict[str, Any],
    db: TritonDB,
    logins: LoginsProvider,
    ad: LDAPClient,
    api: GoogleGroupsAPI | MockGroupsAPI,
    dry_run: bool,
    batch_size: int,
) -> GroupSyncResult:
    """
    Повний цикл синхронізації для однієї Google Group:
      1. SQL   -> StudentRecord
      2. JSON  -> ADUserName
      3. AD    -> email (msExchExtensionAttribute45)
      4. Google Groups API -> actual members
      5. Delta-sync (compute_diff + apply_diff)
    """
    group_email: str = group_cfg["group_email"]
    rule: dict[str, Any] = group_cfg.get("rule", {})

    logger.info("=== Синхронізація: %s ===", group_email)

    try:
        # Крок 1
        students = fetch_students(db, rule)
        logger.info("  SQL: %d студентів", len(students))

        # Кроки 2-3
        desired, no_login_count, _ = resolve_emails(students, logins, ad)
        logins_resolved = len(students) - no_login_count
        logger.info("  Бажаний склад: %d email-адрес", len(desired))

        # Крок 4
        actual_list = api.get_members(group_email)
        actual = set(actual_list)
        logger.info("  Поточний склад: %d email-адрес", len(actual))

        # Крок 5
        diff = compute_diff(desired, actual)
        apply_diff(group_email, diff, api, dry_run=dry_run, batch_size=batch_size)

        return GroupSyncResult(
            group_email=group_email,
            students_from_db=len(students),
            logins_resolved=logins_resolved,
            emails_resolved=len(desired),
            actual_emails=len(actual),
            diff=diff,
        )

    except Exception as exc:
        logger.exception("Помилка синхронізації %s: %s", group_email, exc)
        return GroupSyncResult(
            group_email=group_email,
            students_from_db=0,
            logins_resolved=0,
            emails_resolved=0,
            actual_emails=0,
            diff=SyncDiff(),
            error=str(exc),
        )


# ---------------------------------------------------------------------------
# Фабрики підключень
# ---------------------------------------------------------------------------


def build_api(config: dict[str, Any]) -> GoogleGroupsAPI | MockGroupsAPI:
    backend = get_backend(config)

    if backend == "mock":
        logger.info("Бекенд: MockGroupsAPI")
        return MockGroupsAPI()

    if backend == "google":
        logger.info("Бекенд: GoogleGroupsAPI")
        google_cfg = get_google_config(config)
        api = GoogleGroupsAPI(
            service_account_file=google_cfg["service_account_file"],
            admin_email=google_cfg["admin_email"],
            batch_size=int(google_cfg.get("batch_size", 100)),
        )
        if api.service is None:
            raise RuntimeError(
                f"Не вдалося підключитися до Google API: {api.init_error}"
            )
        return api

    raise ValueError(f"Невідомий backend: {backend!r}. Допустимі: 'mock', 'google'")


def build_db(config: dict[str, Any]) -> TritonDB:
    """Створює TritonDB із секції 'db' config.json."""
    db_cfg = get_db_config(config)
    if not db_cfg:
        raise ValueError("Відсутня секція 'db' у конфігурації")
    return TritonDB.from_config(db_cfg)


def build_ldap(config: dict[str, Any]) -> LDAPClient:
    """Створює LDAPClient (NTLM) із секції 'ldap' config.json."""
    ldap_cfg = get_ldap_config(config)
    required = ("server", "user", "password")
    missing = [k for k in required if not ldap_cfg.get(k)]
    if missing:
        raise ValueError(f"Відсутні поля LDAP-конфігурації: {missing}")
    return LDAPClient.from_config(ldap_cfg)


def build_logins(config: dict[str, Any], base_dir: Path) -> LoginsProvider:
    """Створює LoginsProvider із шляху 'logins_json' у config.json."""
    return LoginsProvider.from_config(config, base_dir=base_dir)


# ---------------------------------------------------------------------------
# Головна функція
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Синхронізація Google Groups <- Triton DB + AD"
    )
    parser.add_argument(
        "--config",
        default=str(Path(__file__).resolve().parent / "config.json"),
        help="Шлях до config.json (за замовч. поруч із main.py)",
    )
    args = parser.parse_args()

    config_path = Path(args.config)
    config = load_config(config_path)

    log_cfg = get_logging_config(config)
    setup_logger(
        name="triton-sync",
        level=log_cfg.get("level", "INFO"),
        logfile=log_cfg.get("file"),
    )

    dry_run = is_dry_run(config)
    groups_cfg = get_groups_config(config)

    logger.info("=" * 60)
    logger.info("Triton -> AD -> Google Groups Sync")
    logger.info("Конфігурація: %s", config_path)
    logger.info("Режим: %s | dry_run: %s", get_backend(config), dry_run)
    logger.info("Груп для синхронізації: %d", len(groups_cfg))
    logger.info("=" * 60)

    if dry_run:
        logger.warning("[DRY RUN] Реальних змін у Google API не буде")

    if not groups_cfg:
        logger.warning("Список груп у конфігурації порожній — нічого робити")
        return 0

    try:
        api = build_api(config)
    except Exception as exc:
        logger.error("Критична помилка ініціалізації API: %s", exc)
        return 1

    db: TritonDB | None = None
    ad: LDAPClient | None = None

    try:
        db = build_db(config)
        ad = build_ldap(config)
        logins = build_logins(config, base_dir=config_path.parent)
        logger.info("LoginsProvider: %d записів завантажено", len(logins))

        batch_size = int(get_google_config(config).get("batch_size", 100))

        results: list[GroupSyncResult] = []
        for group_cfg in groups_cfg:
            result = sync_one_group(
                group_cfg=group_cfg,
                db=db,
                logins=logins,
                ad=ad,
                api=api,
                dry_run=dry_run,
                batch_size=batch_size,
            )
            results.append(result)

    finally:
        if db:
            db.close()
        if ad:
            ad.close()

    logger.info("=" * 60)
    logger.info("ПІДСУМОК")
    logger.info("=" * 60)

    total_add = sum(len(r.diff.to_add) for r in results)
    total_remove = sum(len(r.diff.to_remove) for r in results)
    total_unchanged = sum(len(r.diff.unchanged) for r in results)
    failed = [r for r in results if not r.success]

    for result in results:
        result.log_summary(logger)

    logger.info("-" * 60)
    logger.info(
        "Груп: %d | +%d, -%d, =%d | Помилок: %d",
        len(results), total_add, total_remove, total_unchanged, len(failed),
    )

    if dry_run:
        logger.info("[DRY RUN] Жодних реальних змін не застосовано")

    if failed:
        logger.error("Групи з помилками: %s", [r.group_email for r in failed])
        return 1

    logger.info("Синхронізацію завершено успішно")
    return 0


if __name__ == "__main__":
    sys.exit(main())
