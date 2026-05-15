"""
Тестовий запуск повного ланцюга синхронізації без реальних зовнішніх сервісів.

Замінює:
  - Triton DB        -> статичний список StudentRecord (з новим форматом)
  - LoginsProvider   -> dict {StudentId -> ADUserName}
  - LDAPClient (AD)  -> dict {ADUserName -> email з msExchExtensionAttribute45}
  - Google Groups API -> MockGroupsAPI (in-memory)

Запуск:
  python -X utf8 test_run.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from data_providers.triton_db import StudentRecord
from google_api.google_groups_api import MockGroupsAPI
from logic.sync_logic import apply_diff, compute_diff
from utils.config_loader import is_dry_run, load_config
from utils.email_tools import is_valid_email, normalize_and_validate, normalize_email
from utils.logger import setup_logger

logger = setup_logger("test-run", level="DEBUG")

logger.info("=" * 60)
logger.info("TEST RUN -- mock-цикл синхронізації (новий конвеєр)")
logger.info("=" * 60)

# ---------------------------------------------------------------------------
# 1. Конфігурація
# ---------------------------------------------------------------------------
config_path = Path(__file__).parent / "config.json"
config = load_config(config_path)
dry_run = is_dry_run(config)
logger.info("dry_run = %s", dry_run)

# ---------------------------------------------------------------------------
# 2. Mock StudentRecord (нова схема: speciality_id, year, course_id, ...)
# ---------------------------------------------------------------------------
mock_students: list[StudentRecord] = [
    StudentRecord(1001, "Іваненко", "Олег",    "Петрович",  "ФІ-21", "Перший", 5, "2021", 3651),
    StudentRecord(1002, "Коваль",   "Марія",   "Іванівна",  "ФІ-21", "Перший", 5, "2021", 3651),
    StudentRecord(1003, "Мельник",  "Андрій",  "Сергійович","ФІ-21", "Перший", 5, "2021", 3651),
    StudentRecord(1004, "Шевченко", "Тетяна",  None,         "ФІ-21", "Перший", 5, "2021", 3651),
    StudentRecord(1005, "Бойко",    "Дмитро",  "Олегович",  "ФІ-22", "Перший", 5, "2021", 3651),
    # Студент 1006 — без запису в JSON-файлі (перевірка пропуску)
    StudentRecord(1006, "НеЄБезLogin", "Студент", None,     "ФІ-22", "Перший", 5, "2021", 3651),
    # Студент 1007 — є в JSON, але немає email в AD
    StudentRecord(1007, "БезEmail", "Другий",  None,         "ФІ-22", "Перший", 5, "2021", 3651),
]

logger.info("Triton DB (mock): %d студентів", len(mock_students))

# ---------------------------------------------------------------------------
# 3. Mock LoginsProvider: StudentId -> ADUserName
#    Студент 1006 — немає логіна
#    Студент 1007 — є логін, але в AD немає email
# ---------------------------------------------------------------------------
mock_logins_map: dict[int, str] = {
    1001: "o.ivanenko@univ.edu",
    1002: "m.koval@univ.edu",
    1003: "a.melnyk@univ.edu",
    1004: "t.shevchenko@univ.edu",
    1005: "d.boyko@univ.edu",
    # 1006 навмисно відсутній
    1007: "bez.email@univ.edu",
}


class MockLoginsProvider:
    def get_login(self, student_id: int) -> str | None:
        return mock_logins_map.get(student_id)

    def __len__(self) -> int:
        return len(mock_logins_map)


# ---------------------------------------------------------------------------
# 4. Mock LDAPClient (NTLM): ADUserName -> email з msExchExtensionAttribute45
#    Студент 1007 — є в AD, але msExchExtensionAttribute45 порожній
# ---------------------------------------------------------------------------
mock_ad_map: dict[str, str] = {
    "o.ivanenko@univ.edu": "o.ivanenko@domain.com",
    "m.koval@univ.edu":    "m.koval@domain.com",
    "a.melnyk@univ.edu":   "a.melnyk@domain.com",
    "t.shevchenko@univ.edu": "t.shevchenko@domain.com",
    "d.boyko@univ.edu":    "d.boyko@domain.com",
    # "bez.email@univ.edu" навмисно відсутній -> no email
}


class MockADClient:
    def get_email_by_login(self, login: str) -> str | None:
        return mock_ad_map.get(login)


# ---------------------------------------------------------------------------
# 5. Resolve emails (реальна функція з main.py, мок-об'єкти замість залежностей)
# ---------------------------------------------------------------------------
from main import resolve_emails  # noqa: E402

logins_provider = MockLoginsProvider()
ad_client = MockADClient()

desired_emails, no_login_count, name_map = resolve_emails(
    mock_students, logins_provider, ad_client
)

logger.info(
    "Desired: %d email-адрес, без логіна: %d",
    len(desired_emails), no_login_count,
)
logger.info("Desired set: %s", sorted(desired_emails))

# ---------------------------------------------------------------------------
# 6. MockGroupsAPI — поточний стан групи
# ---------------------------------------------------------------------------
GROUP_EMAIL = "spec-3651-2021@domain.com"

api = MockGroupsAPI(initial_members={
    GROUP_EMAIL: [
        "o.ivanenko@domain.com",   # актуальний — залишиться
        "m.koval@domain.com",      # актуальний — залишиться
        "old.student@domain.com",  # застарілий — буде видалено
    ]
})

actual_members = api.get_members(GROUP_EMAIL)
logger.info("Поточний склад: %d: %s", len(actual_members), sorted(actual_members))

# ---------------------------------------------------------------------------
# 7. Delta-sync
# ---------------------------------------------------------------------------
diff = compute_diff(desired_emails, set(actual_members))

logger.info("--- DIFF ---")
logger.info("  +%d to_add:    %s", len(diff.to_add),    sorted(diff.to_add))
logger.info("  -%d to_remove: %s", len(diff.to_remove), sorted(diff.to_remove))
logger.info("  =%d unchanged: %s", len(diff.unchanged), sorted(diff.unchanged))

# ---------------------------------------------------------------------------
# 8. Застосування diff
# ---------------------------------------------------------------------------
apply_diff(GROUP_EMAIL, diff, api, dry_run=dry_run, batch_size=100)

final_members = api.get_members(GROUP_EMAIL)
logger.info("--- ФІНАЛЬНИЙ СКЛАД (%s) ---", GROUP_EMAIL)
for email in sorted(final_members):
    logger.info("  %s", email)

# ---------------------------------------------------------------------------
# 9. Перевірки assert
# ---------------------------------------------------------------------------
logger.info("--- ПЕРЕВІРКИ ---")

# diff-перевірки (незалежні від dry_run)
assert "a.melnyk@domain.com"     in diff.to_add,    "melnyk має бути в to_add"
assert "t.shevchenko@domain.com" in diff.to_add,    "shevchenko має бути в to_add"
assert "d.boyko@domain.com"      in diff.to_add,    "boyko має бути в to_add"
assert "old.student@domain.com"  in diff.to_remove, "old.student має бути в to_remove"
assert "o.ivanenko@domain.com"   in diff.unchanged, "ivanenko має бути в unchanged"
assert "m.koval@domain.com"      in diff.unchanged, "koval має бути в unchanged"
assert len(diff.to_add) == 3,    "to_add: очікуємо 3 нові"
assert len(diff.to_remove) == 1, "to_remove: очікуємо 1 застарілу"
assert len(diff.unchanged) == 2, "unchanged: очікуємо 2 незмінні"

# Студент без логіна та без email не повинні потрапити
assert "bez.email@domain.com" not in desired_emails, "Студент 1007 (без AD-email) не має бути"

logger.info("  [OK] to_add = 3 нові адреси")
logger.info("  [OK] to_remove = 1 застаріла")
logger.info("  [OK] unchanged = 2 незмінні")
logger.info("  [OK] Студент без логіна (1006) та без email в AD (1007) пропущені")

if not dry_run:
    final_set = set(final_members)
    assert "old.student@domain.com" not in final_set, "Застарілий не видалено"
    assert desired_emails == final_set, "Фінальний склад != desired"
    logger.info("  [OK] Фінальний склад збігається з desired (dry_run=False)")
else:
    logger.info("  [DRY RUN] Зміни не застосовані")

# ---------------------------------------------------------------------------
# 10. Тести email_tools
# ---------------------------------------------------------------------------
logger.info("--- email_tools ---")

assert normalize_email("  User@Domain.COM  ") == "user@domain.com"
assert is_valid_email("valid@knu.ua") is True
assert is_valid_email("no-at-sign") is False
assert is_valid_email("missing@dot") is False
assert normalize_and_validate("  VALID@TEST.COM ") == "valid@test.com"
assert normalize_and_validate("") is None
assert normalize_and_validate("bad-email") is None
assert normalize_and_validate(None) is None

logger.info("  [OK] normalize_email, is_valid_email, normalize_and_validate")

# ---------------------------------------------------------------------------
# Підсумок
# ---------------------------------------------------------------------------
logger.info("=" * 60)
logger.info("ВСІ ТЕСТИ ПРОЙШЛИ УСПІШНО")
logger.info(
    "Diff: +%d, -%d, =%d",
    len(diff.to_add), len(diff.to_remove), len(diff.unchanged),
)
logger.info("=" * 60)
