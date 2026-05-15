# Triton → LDAP → Google Groups Sync

Модуль автоматичної синхронізації складу Google Groups (Google Workspace)
із бази даних Triton (MS SQL Server) через LDAP.

## Архітектура

```
triton_sync/
├── main.py                        # Точка входу, оркестрація
├── config.json                    # Конфігурація (не комітити з реальними паролями!)
├── requirements.txt
│
├── utils/
│   ├── config_loader.py           # Зчитування та валідація config.json
│   ├── logger.py                  # Налаштування логера (консоль + файл)
│   └── email_tools.py             # Нормалізація та валідація email-адрес
│
├── data_providers/
│   ├── triton_db.py               # MS SQL клієнт (pyodbc), SQL-запити
│   └── ldap_client.py             # LDAP клієнт (ldap3), пошук email
│
├── logic/
│   └── sync_logic.py              # Delta-sync через Set operations
│
├── google_api/
│   └── google_groups_api.py       # Google Directory API + MockGroupsAPI
│
└── logs/
    └── sync.log                   # Файл логів (створюється автоматично)
```

## Ланцюг обробки даних

```
Triton DB (SQL)
    │  SELECT студентів за факультетом / курсом / групою
    ▼
StudentRecord (Id, ПІБ, група, факультет, курс)
    │  LDAP-запит: uid=<Id>  або  cn=<ПІБ>
    ▼
desired_set = { email1, email2, ... }   ← бажаний склад
    │
    │  actual_set ← api.get_members(group_email)
    ▼
Delta-sync (Set operations):
    to_add    = desired - actual   → batch_add()
    to_remove = actual - desired   → batch_remove()
    unchanged = desired ∩ actual   → нічого не робимо
```

## Встановлення

### Системні вимоги

- Python 3.10+
- ODBC Driver 17 (або 18) for SQL Server
  - Windows: встановити з [Microsoft Download Center](https://learn.microsoft.com/uk-ua/sql/connect/odbc/download-odbc-driver-for-sql-server)
  - Linux: `apt-get install msodbcsql17` або аналог

### Кроки

```powershell
# 1. Перейти до директорії модуля
cd triton_sync

# 2. Створити та активувати віртуальне середовище
python -m venv .venv
.\.venv\Scripts\Activate.ps1       # Windows PowerShell
# source .venv/bin/activate         # Linux / macOS

# 3. Встановити залежності
pip install -r requirements.txt

# 4. Налаштувати конфігурацію
copy config.json config.local.json
# Відредагуйте config.local.json — вкажіть реальні облікові дані
```

## Налаштування (config.json)

```json
{
  "backend": "google",          // "mock" для локального тестування, "google" для реального
  "google": {
    "service_account_file": "service_account.json",  // ключ Service Account
    "admin_email": "admin@domain.com",               // email адміністратора домену
    "batch_size": 100                                // розмір пакету (1–1000)
  },
  "db": {
    "connection_string": "DRIVER={ODBC Driver 17 for SQL Server};SERVER=...;DATABASE=TritonDatabase;UID=...;PWD=..."
  },
  "ldap": {
    "server": "ldap://ldap.domain.com",
    "bind_dn": "cn=sync-service,ou=svc,dc=domain,dc=com",
    "password": "secret",
    "search_base": "ou=students,dc=domain,dc=com",
    "email_attribute": "mail",    // атрибут із email (за замовч. "mail")
    "uid_attribute": "uid",       // атрибут з ID студента (за замовч. "uid")
    "cn_attribute": "cn"          // атрибут з повним ім'ям (за замовч. "cn")
  },
  "dry_run": true,               // true = лише перегляд, false = реальні зміни
  "logging": {
    "level": "INFO",             // DEBUG | INFO | WARNING | ERROR
    "file": "logs/sync.log"      // шлях до файлу логів (null = лише консоль)
  },
  "groups": [
    {
      "group_email": "1course-frecs@domain.com",
      "rule": {
        "faculty_name": "ФРЕКС",   // точна назва факультету (Main.Faculty.Name)
        "course": 1                 // номер курсу (1–6)
      }
    },
    {
      "group_email": "group-fi-22@domain.com",
      "rule": {
        "group_name": "ФІ-22"      // точна назва групи (StudentsGroup.Name)
      }
    }
  ]
}
```

### Поля правила (rule)

| Поле          | Тип    | Опис                                                   |
|---------------|--------|--------------------------------------------------------|
| `faculty_name`| string | Точна назва факультету (порівнюється з `Main.Faculty.Name`) |
| `course`      | int    | Номер курсу 1–6 (порівнюється з `Main.CourseOfStudy.Name`) |
| `group_name`  | string | Точна назва групи (`Students.StudentsGroup.Name`)       |

Усі поля є опційними і комбінуються через AND.

## Запуск

```powershell
# Режим dry_run (перегляд без змін) — встановлено за замовч. у config.json
python main.py

# З альтернативним файлом конфігурації
python main.py --config config.local.json

# Увімкнути реальні зміни: встановити "dry_run": false у config.json
# і запустити знову:
python main.py --config config.local.json
```

## Налаштування Google Service Account

1. У Google Cloud Console створити Service Account.
2. Завантажити JSON-ключ → зберегти як `service_account.json` поруч із `main.py`.
3. У Google Admin Console → Безпека → API → Domain-Wide Delegation:
   - Додати Client ID Service Account.
   - Scopes:
     ```
     https://www.googleapis.com/auth/admin.directory.group
     https://www.googleapis.com/auth/admin.directory.group.member
     https://www.googleapis.com/auth/admin.directory.user.readonly
     ```

## Обробка помилок

| Ситуація                          | Поведінка                                           |
|-----------------------------------|-----------------------------------------------------|
| Група не існує в Google (HTTP 404)| WARNING у лог, повертається порожній список          |
| Студент не знайдений у LDAP       | WARNING у лог, студент пропускається                 |
| Rate limit Google API (HTTP 429)  | Exponential backoff: 1s → 2s → 4s → 8s → 16s → 32s|
| Помилка SQL-запиту                | ERROR у лог, виняток пробрасується                  |
| Помилка LDAP                      | ERROR у лог, функція повертає None (студент пропускається) |
| Помилка синхронізації групи       | ERROR у лог, решта груп продовжують оброблятися      |

## Схема БД Triton (використовувані таблиці)

```sql
-- Ланцюг для визначення курсу:
-- Student.ActiveGroupId
--   → StudentsGroup.IntervalOfStudyId
--     → IntervalOfStudy.CourseOfStudyNewId
--       → CourseOfStudyNew.CourseOfStudyId
--         → CourseOfStudy.Name  ('1', '2', '3', ...)

SELECT
    s.Id, s.LastName, s.FirstName, s.MiddleName,
    sg.Name AS group_name,
    f.Name  AS faculty_name,
    cos.Name AS course_name
FROM Students.Student s
JOIN Students.StudentsGroup sg ON s.ActiveGroupId = sg.Id
JOIN Main.StudyProgram sp       ON sg.StudyProgramId = sp.Id
JOIN Main.Faculty f              ON sp.FacultyId = f.Id
JOIN Main.IntervalOfStudy ios    ON sg.IntervalOfStudyId = ios.Id
JOIN Main.CourseOfStudyNew cosn  ON ios.CourseOfStudyNewId = cosn.Id
JOIN Main.CourseOfStudy cos      ON cosn.CourseOfStudyId = cos.Id
WHERE s.IsOld = 0 AND sg.IsOld = 0 AND sg.IsLatest = 1 AND ios.IsActive = 1
  AND f.Name = 'ФРЕКС'
  AND cos.Name = '1'
```

## Режим mock (без Google API та реальної БД)

Встановіть у `config.json`:
```json
{
  "backend": "mock",
  "dry_run": true
}
```

У цьому режимі використовується `MockGroupsAPI` — in-memory заглушка.
Підключення до Google, Triton DB та LDAP не потрібні.
Корисно для демонстрації та відлагодження бізнес-логіки.
