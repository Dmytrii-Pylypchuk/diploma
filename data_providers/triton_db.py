"""
Провайдер даних із бази Triton (MS SQL Server через pyodbc).

Схема БД (релевантні таблиці):
  Students.Student            — Id, FirstName, LastName, MiddleName,
                                ActiveGroupId, StudentStatusTypeId, IsOld
  Students.StudentsGroup      — Id, Name, StudyProgramId, IntervalOfStudyId
  Students.StudentStatusType  — Id, Name  (відрахован / academ / ...)
  Main.StudyProgram           — Id, SpecialityId, FacultyId, EducationFormId
  Main.IntervalOfStudy        — Id, StudySeasonId, CourseOfStudyNewId
  Main.CourseOfStudyNew       — Id, Name, LevelOfEducationId
  Main.StudySeason            — Id, Year  (рядок виду "2021", "2022", ...)

Фільтри за замовчуванням:
  - s.IsOld = 0
  - sst.Name NOT LIKE '%відрахован%' / '%academ%' / '%Помилково внесено%'

Основні параметри вибірки:
  speciality_id         — ідентифікатор спеціальності (Main.StudyProgram.SpecialityId)
  year                  — рік сезону навчання (LIKE 'YYYY%')
  course_id             — (опційно) Main.CourseOfStudyNew.Id
  group_name            — (опційно) точна назва групи
  faculty_id            — (опційно) Main.StudyProgram.FacultyId
  education_form_id     — (опційно) Main.StudyProgram.EducationFormId
  level_of_education_id — (опційно) Main.CourseOfStudyNew.LevelOfEducationId
"""

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StudentRecord:
    """Незмінний запис про студента, отриманий із бази Triton."""

    student_id: int
    last_name: str
    first_name: str
    middle_name: str | None
    group_name: str
    course_name: str   # назва курсу з CourseOfStudyNew.Name
    course_id: int     # CourseOfStudyNew.Id — використовується для фільтрації
    study_year: str    # StudySeason.Year, напр. "2021"
    speciality_id: int

    @property
    def full_name(self) -> str:
        """Повне ім'я у форматі 'Прізвище Ім'я По-батькові'."""
        parts = [self.last_name, self.first_name]
        if self.middle_name:
            parts.append(self.middle_name)
        return " ".join(parts)


# ---------------------------------------------------------------------------
# SQL-шаблон.
#
# Обов'язкові параметри: speciality_id (?), year (?).
# Опційні: course_id, group_name — підставляються через {course_filter}/{group_filter}.
#
# Статусний фільтр відсіває відрахованих, академвідпускників, помилково внесених.
# ---------------------------------------------------------------------------
_BASE_QUERY = """\
SELECT
    s.Id            AS student_id,
    s.LastName      AS last_name,
    s.FirstName     AS first_name,
    s.MiddleName    AS middle_name,
    sg.Name         AS group_name,
    cosn.Name       AS course_name,
    cosn.Id         AS course_id,
    ss.Year         AS study_year,
    sp.SpecialityId AS speciality_id
FROM        [Students].[Student]            AS s
INNER JOIN  [Students].[StudentsGroup]      AS sg   ON s.ActiveGroupId        = sg.Id
INNER JOIN  [Main].[StudyProgram]           AS sp   ON sg.StudyProgramId      = sp.Id
INNER JOIN  [Main].[IntervalOfStudy]        AS ios  ON sg.IntervalOfStudyId   = ios.Id
INNER JOIN  [Main].[CourseOfStudyNew]       AS cosn ON ios.CourseOfStudyNewId = cosn.Id
INNER JOIN  [Main].[StudySeason]            AS ss   ON ios.StudySeasonId      = ss.Id
INNER JOIN  [Students].[StudentStatusType]  AS sst  ON s.StudentStatusTypeId  = sst.Id
WHERE
        s.IsOld         = 0
    AND sp.SpecialityId = ?
    AND ss.Year LIKE ? + N'%'
    AND sst.Name NOT LIKE N'%відрахован%'
    AND sst.Name NOT LIKE N'%academ%'
    AND sst.Name NOT LIKE N'%Помилково внесено%'
    {course_filter}
    {group_filter}
    {faculty_filter}
    {education_form_filter}
    {level_filter}
ORDER BY sg.Name, s.LastName
"""


def _build_connection_string(db_cfg: dict[str, Any]) -> str:
    """
    Формує рядок підключення ODBC із словника конфігурації.

    Підтримує два формати:
      - Готовий рядок у db_cfg["connection_string"]
      - Окремі поля: server, database, user, password, driver
    """
    if cs := db_cfg.get("connection_string"):
        return cs

    server   = db_cfg["server"]
    database = db_cfg["database"]
    user     = db_cfg["user"]
    password = db_cfg["password"]
    driver   = db_cfg.get("driver", "ODBC Driver 17 for SQL Server")
    encrypt  = "yes" if db_cfg.get("encrypt", True) else "no"
    trust    = "yes" if db_cfg.get("trust_cert", True) else "no"

    return (
        f"DRIVER={{{driver}}};"
        f"SERVER={server};"
        f"DATABASE={database};"
        f"UID={user};"
        f"PWD={password};"
        f"Encrypt={encrypt};"
        f"TrustServerCertificate={trust};"
    )


class TritonDB:
    """
    Клієнт для отримання даних із бази Triton (MS SQL Server) через pyodbc.

    Підтримує контекстний менеджер (with-блок) для автоматичного закриття.

    Приклад:
        with TritonDB.from_config(db_cfg) as db:
            students = db.get_students(speciality_id=3651, year="2021")
    """

    def __init__(self, connection_string: str) -> None:
        self._connection_string = connection_string
        self._conn = None
        self._connect()

    @classmethod
    def from_config(cls, db_cfg: dict[str, Any]) -> "TritonDB":
        """Створює екземпляр із словника конфігурації (секція 'db' config.json)."""
        return cls(_build_connection_string(db_cfg))

    # ------------------------------------------------------------------

    def _connect(self) -> None:
        try:
            import pyodbc  # noqa: PLC0415
            self._conn = pyodbc.connect(self._connection_string, autocommit=True)
            logger.info("Підключено до Triton DB")
        except ImportError as exc:
            raise ImportError(
                "Бібліотека pyodbc не встановлена. Виконайте: pip install pyodbc"
            ) from exc
        except Exception as exc:
            logger.error("Не вдалося підключитися до Triton DB: %s", exc)
            raise

    def _execute_query(self, sql: str, params: list[Any]) -> list[StudentRecord]:
        try:
            cursor = self._conn.cursor()
            cursor.execute(sql, params)
            columns = [col[0] for col in cursor.description]
            records: list[StudentRecord] = []

            for row in cursor.fetchall():
                d: dict[str, Any] = dict(zip(columns, row))
                records.append(
                    StudentRecord(
                        student_id=int(d["student_id"]),
                        last_name=(d["last_name"] or "").strip(),
                        first_name=(d["first_name"] or "").strip(),
                        middle_name=(
                            d["middle_name"].strip() if d.get("middle_name") else None
                        ),
                        group_name=(d["group_name"] or "").strip(),
                        course_name=(d["course_name"] or "").strip(),
                        course_id=int(d["course_id"]),
                        study_year=(d["study_year"] or "").strip(),
                        speciality_id=int(d["speciality_id"]),
                    )
                )

            logger.debug("SQL повернув %d рядків", len(records))
            return records

        except Exception as exc:
            logger.error("Помилка виконання SQL-запиту: %s", exc)
            raise

    # ------------------------------------------------------------------
    # Публічний API
    # ------------------------------------------------------------------

    def get_students(
        self,
        speciality_id: int,
        year: str,
        course_id: int | None = None,
        group_name: str | None = None,
        faculty_id: int | None = None,
        education_form_id: int | None = None,
        level_of_education_id: int | None = None,
    ) -> list[StudentRecord]:
        """
        Повертає активних студентів за спеціальністю та роком навчального сезону.

        Args:
            speciality_id:         ID спеціальності (Main.StudyProgram.SpecialityId).
            year:                  Рік сезону у форматі 'YYYY' (фільтр LIKE 'YYYY%').
            course_id:             (опційно) Main.CourseOfStudyNew.Id.
            group_name:            (опційно) Точна назва групи.
            faculty_id:            (опційно) Main.StudyProgram.FacultyId.
            education_form_id:     (опційно) Main.StudyProgram.EducationFormId.
            level_of_education_id: (опційно) Main.CourseOfStudyNew.LevelOfEducationId.

        Returns:
            Список StudentRecord, відсортований за назвою групи та прізвищем.
        """
        params: list[Any] = [speciality_id, year]
        course_filter = ""
        group_filter = ""
        faculty_filter = ""
        education_form_filter = ""
        level_filter = ""

        if course_id is not None:
            course_filter = "AND cosn.Id = ?"
            params.append(course_id)

        if group_name is not None:
            group_filter = "AND sg.Name = ?"
            params.append(group_name)

        if faculty_id is not None:
            faculty_filter = "AND sp.FacultyId = ?"
            params.append(faculty_id)

        if education_form_id is not None:
            education_form_filter = "AND sp.EducationFormId = ?"
            params.append(education_form_id)

        if level_of_education_id is not None:
            level_filter = "AND cosn.LevelOfEducationId = ?"
            params.append(level_of_education_id)

        sql = _BASE_QUERY.format(
            course_filter=course_filter,
            group_filter=group_filter,
            faculty_filter=faculty_filter,
            education_form_filter=education_form_filter,
            level_filter=level_filter,
        )

        logger.info(
            "Запит студентів: speciality_id=%r, year=%r, course_id=%r, group=%r, "
            "faculty_id=%r, education_form_id=%r, level_of_education_id=%r",
            speciality_id, year, course_id, group_name,
            faculty_id, education_form_id, level_of_education_id,
        )

        records = self._execute_query(sql, params)
        logger.info("Отримано %d студентів із Triton DB", len(records))
        return records

    # ------------------------------------------------------------------
    # Контекстний менеджер
    # ------------------------------------------------------------------

    def close(self) -> None:
        if self._conn:
            try:
                self._conn.close()
                self._conn = None
                logger.info("Підключення до Triton DB закрито")
            except Exception as exc:
                logger.warning("Помилка при закритті Triton DB: %s", exc)

    def __enter__(self) -> "TritonDB":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()
