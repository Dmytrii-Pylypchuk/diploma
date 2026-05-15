"""
Провайдер логінів студентів із JSON-файлу.

JSON-файл (напр. wluaivf4..json) містить список записів виду:
    [
      {
        "StudentId":        12345,
        "ADUserName":       "student@univ.edu",
        "AlternativeEmail": "personal@gmail.com",
        "UserFullName":     "Прізвище Ім'я По-батькові"
      },
      ...
    ]

ADUserName використовується для пошуку у Active Directory.
"""

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LoginRecord:
    """Запис про логін студента з JSON-файлу."""

    student_id: int
    ad_username: str        # userPrincipalName / sAMAccountName
    alternative_email: str  # особиста пошта (запасний варіант)
    full_name: str          # ПІБ у довільному форматі


class LoginsProvider:
    """
    Завантажує та індексує файл відповідності StudentId → AD-логін.

    Після завантаження підтримує швидкий O(1) пошук за student_id.

    Приклад:
        logins = LoginsProvider("wluaivf4..json")
        login = logins.get_login(12345)   # -> "student@univ.edu" або None
    """

    def __init__(self, json_path: str | Path) -> None:
        self._path = Path(json_path)
        self._by_id: dict[int, LoginRecord] = {}
        self._load()

    @classmethod
    def from_config(cls, config: dict[str, Any], base_dir: Path | None = None) -> "LoginsProvider":
        """
        Створює екземпляр із конфігурації.

        Шлях береться з config["logins_json"]. Якщо відносний — розраховується
        від base_dir (зазвичай директорія config.json).
        """
        raw_path = config.get("logins_json", "")
        if not raw_path:
            raise ValueError(
                "Відсутній ключ 'logins_json' у конфігурації. "
                "Вкажіть шлях до JSON-файлу з логінами."
            )
        path = Path(raw_path)
        if not path.is_absolute() and base_dir:
            path = base_dir / path
        return cls(path)

    # ------------------------------------------------------------------

    def _load(self) -> None:
        if not self._path.exists():
            raise FileNotFoundError(
                f"JSON-файл логінів не знайдено: {self._path}"
            )

        with self._path.open(encoding="utf-8-sig") as f:
            data = json.load(f)

        if not isinstance(data, list):
            raise ValueError(
                f"JSON-файл логінів має містити масив об'єктів, отримано: {type(data)}"
            )

        loaded = 0
        skipped = 0
        for entry in data:
            sid = entry.get("StudentId")
            if sid is None:
                skipped += 1
                continue
            self._by_id[int(sid)] = LoginRecord(
                student_id=int(sid),
                ad_username=str(entry.get("ADUserName") or "").strip(),
                alternative_email=str(entry.get("AlternativeEmail") or "").strip(),
                full_name=str(entry.get("UserFullName") or "").strip(),
            )
            loaded += 1

        logger.info(
            "LoginsProvider: завантажено %d записів із %s (пропущено: %d)",
            loaded, self._path.name, skipped,
        )

    # ------------------------------------------------------------------
    # Публічний API
    # ------------------------------------------------------------------

    def get_login(self, student_id: int) -> str | None:
        """
        Повертає AD-логін (ADUserName) для студента або None.

        Args:
            student_id: ID студента з Triton DB (Students.Student.Id).

        Returns:
            Рядок ADUserName або None якщо студент не знайдений у файлі
            або поле ADUserName порожнє.
        """
        rec = self._by_id.get(student_id)
        if rec is None:
            return None
        return rec.ad_username or None

    def get_record(self, student_id: int) -> LoginRecord | None:
        """Повертає повний LoginRecord або None."""
        return self._by_id.get(student_id)

    def __len__(self) -> int:
        return len(self._by_id)

    def __contains__(self, student_id: int) -> bool:
        return student_id in self._by_id
