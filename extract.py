"""
Витяг списку студентів із Triton DB + логіни з JSON + email з Active Directory.

Аналог кореневого get_students.py, але:
  - використовує config.json замість хардкодних змінних
  - використовує компоненти модуля triton_sync (TritonDB, LoginsProvider, LDAPClient)

Приклади запуску:
  # Всі студенти спеціальності 3651, сезон 2021
  python extract.py --speciality 3651 --year 2021

  # Тільки 4-й курс + зберегти у CSV
  python extract.py --speciality 3651 --year 2021 --course-id 4 --csv students.csv

  # Конкретна група
  python extract.py --speciality 3651 --year 2021 --group "ФІ-22"

  # Без походу в AD (швидше — лише дані з БД + JSON)
  python extract.py --speciality 3651 --year 2021 --no-ldap

  # Інший config.json
  python extract.py --speciality 3651 --year 2021 --config /path/to/config.json
"""

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from data_providers.logins_provider import LoginsProvider, LoginRecord
from data_providers.triton_db import StudentRecord, TritonDB
from utils.config_loader import get_db_config, get_ldap_config, load_config

# ---------------------------------------------------------------------------
# Формування рядків результату
# ---------------------------------------------------------------------------

def _build_rows(
    students: list[StudentRecord],
    logins: LoginsProvider,
    ad_emails: dict[str, str],   # {login -> email_from_AD}
    ad_found: dict[str, bool],   # {login -> True якщо об'єкт знайдено в AD}
) -> list[dict]:
    rows = []
    for st in students:
        rec: LoginRecord | None = logins.get_record(st.student_id)
        login        = rec.ad_username        if rec else ""
        alt_email    = rec.alternative_email  if rec else ""
        full_name    = rec.full_name          if rec else ""

        if login:
            ldap_found = "yes" if ad_found.get(login) else "no"
            ldap_email = ad_emails.get(login, "")
        else:
            ldap_found = ""
            ldap_email = ""

        rows.append({
            "StudentId":   st.student_id,
            "LastName":    st.last_name,
            "FirstName":   st.first_name,
            "MiddleName":  st.middle_name or "",
            "GroupName":   st.group_name,
            "CourseName":  st.course_name,
            "CourseId":    st.course_id,
            "StudyYear":   st.study_year,
            "SpecialityId": st.speciality_id,
            "Login":       login,
            "AltEmail":    alt_email,
            "UserFullName": full_name,
            "LdapFound":   ldap_found,
            "LdapEmail":   ldap_email,
        })
    return rows


# ---------------------------------------------------------------------------
# Вивід таблиці в консоль
# ---------------------------------------------------------------------------

def _print_table(rows: list[dict]) -> None:
    if not rows:
        print("За заданими параметрами студентів не знайдено.")
        return

    headers = [
        "StudentId", "GroupName", "LastName", "FirstName",
        "Login", "LdapFound", "LdapEmail", "AltEmail",
    ]
    widths = {
        h: max(len(h), max(len(str(r.get(h) or "")) for r in rows))
        for h in headers
    }

    header_line = " | ".join(h.ljust(widths[h]) for h in headers)
    print(header_line)
    print("-" * len(header_line))
    for r in rows:
        print(" | ".join(str(r.get(h) or "").ljust(widths[h]) for h in headers))

    total          = len(rows)
    with_login     = sum(1 for r in rows if r["Login"])
    found_in_ldap  = sum(1 for r in rows if r["LdapFound"] == "yes")
    with_ldap_email = sum(1 for r in rows if r["LdapEmail"])

    print(f"\nВсього записів:                          {total}")
    print(f"З логіном у JSON:                         {with_login}")
    print(f"Знайдено в AD (об'єкт існує):             {found_in_ldap} з {with_login}")
    print(f"  з email у msExchExtensionAttribute45:   {with_ldap_email}")

    not_found = with_login - found_in_ldap
    if not_found:
        print(f"\nНЕ знайдено в AD: {not_found}")
        examples = [r["Login"] for r in rows
                    if r["Login"] and r["LdapFound"] == "no"][:5]
        if examples:
            print(f"  Приклади логінів без матчу: {', '.join(examples)}")

    # кілька прикладів знайдених — щоб переконатися, що фільтр правильний
    samples = [r for r in rows if r["LdapFound"] == "yes"][:3]
    if samples:
        print("\nПриклади знайдених у AD (для перевірки):")
        for r in samples:
            print(f"  {r['Login']:<25} -> email={r['LdapEmail'] or '—'}")


# ---------------------------------------------------------------------------
# Збереження CSV
# ---------------------------------------------------------------------------

def _write_csv(rows: list[dict], path: Path) -> None:
    if not rows:
        return
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    print(f"\nЗбережено CSV: {path}")


# ---------------------------------------------------------------------------
# AD-пошук (тільки якщо --no-ldap не задано)
# ---------------------------------------------------------------------------

def _fetch_ad_emails(
    logins: LoginsProvider,
    student_ids: list[int],
    ldap_cfg: dict,
) -> tuple[dict[str, str], dict[str, bool]]:
    """
    Для кожного логіна студентів шукає email в AD.

    Returns:
        (ad_emails, ad_found) — email та прапорець знайденого об'єкта.
    """
    from data_providers.ldap_client import LDAPClient  # noqa: PLC0415

    # збираємо унікальні логіни тих студентів, що є в JSON
    login_set: set[str] = set()
    for sid in student_ids:
        login = logins.get_login(sid)
        if login:
            login_set.add(login)

    if not login_set:
        print("[AD] Жодного логіна для пошуку — пропускаємо.")
        return {}, {}

    print(f"[AD] Шукаємо {len(login_set)} логінів...")

    ad_emails: dict[str, str] = {}
    ad_found:  dict[str, bool] = {}

    with LDAPClient.from_config(ldap_cfg) as ad:
        for login in sorted(login_set):
            email = ad.get_email_by_login(login)
            if email is not None:
                ad_found[login]  = True
                ad_emails[login] = email
            else:
                # Перевіряємо чи об'єкт взагалі є (email міг бути порожнім)
                ad_found[login] = False

    found_count = sum(1 for v in ad_found.values() if v)
    email_count = len(ad_emails)
    print(f"[AD] Знайдено об'єктів: {found_count} з {len(login_set)}")
    print(f"[AD] З email у mail_attr: {email_count}\n")

    return ad_emails, ad_found


# ---------------------------------------------------------------------------
# Точка входу
# ---------------------------------------------------------------------------

def main() -> None:
    default_config = str(Path(__file__).resolve().parent / "config.json")

    parser = argparse.ArgumentParser(
        description="Витяг студентів із Triton DB + JSON-логіни + email з AD.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # Фільтри вибірки
    parser.add_argument("--speciality", type=int, required=True,
                        help="ID спеціальності (Main.StudyProgram.SpecialityId)")
    parser.add_argument("--year", required=True,
                        help="Рік навчального сезону (LIKE 'YYYY%%'), напр. 2021")
    parser.add_argument("--course-id", type=int, default=None, dest="course_id",
                        help="ID курсу (Main.CourseOfStudyNew.Id), опційно")
    parser.add_argument("--group", default=None,
                        help="Точна назва групи, напр. 'ФІ-22'")

    # Вихід
    parser.add_argument("--csv", type=Path, default=None,
                        help="Зберегти результат у CSV-файл")
    parser.add_argument("--no-ldap", action="store_true",
                        help="Не звертатися до AD (швидше, без email-адрес)")

    # Конфіг
    parser.add_argument("--config", default=default_config,
                        help=f"Шлях до config.json (за замовч. {default_config})")

    args = parser.parse_args()

    # --- Завантаження конфігурації ---
    config_path = Path(args.config)
    config = load_config(config_path)
    db_cfg   = get_db_config(config)
    ldap_cfg = get_ldap_config(config)

    course_desc = args.course_id if args.course_id is not None else "всі"
    print(f"[SQL] Спеціальність={args.speciality}, рік={args.year}, "
          f"курс={course_desc}, група={args.group or 'всі'}")

    # --- Triton DB -> StudentRecord ---
    with TritonDB.from_config(db_cfg) as db:
        students = db.get_students(
            speciality_id=args.speciality,
            year=args.year,
            course_id=args.course_id,
            group_name=args.group,
        )
    print(f"[SQL] Отримано {len(students)} студентів.\n")

    # --- JSON-логіни ---
    logins = LoginsProvider.from_config(config, base_dir=config_path.parent)
    print(f"[JSON] Завантажено {len(logins)} записів логінів.\n")

    # --- AD-пошук (опційно) ---
    if args.no_ldap:
        print("[AD] Пропущено за --no-ldap.\n")
        ad_emails: dict[str, str] = {}
        ad_found:  dict[str, bool] = {}
    else:
        student_ids = [s.student_id for s in students]
        ad_emails, ad_found = _fetch_ad_emails(logins, student_ids, ldap_cfg)

    # --- Формування та вивід ---
    rows = _build_rows(students, logins, ad_emails, ad_found)
    _print_table(rows)

    if args.csv:
        _write_csv(rows, args.csv)


if __name__ == "__main__":
    main()
