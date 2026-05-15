"""
Модуль для завантаження та валідації конфігурації з config.json.

Підтримує два режими роботи:
  - 'mock'   — локальне тестування без реальних Google API викликів.
  - 'google' — реальна взаємодія з Google Workspace Directory API.

Підтримує dry_run — попередній перегляд змін без їх фактичного застосування.
"""

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def load_config(path: str | Path = "config.json") -> dict[str, Any]:
    """
    Зчитує та повертає конфігурацію з JSON-файлу.

    Args:
        path: Шлях до файлу конфігурації (абсолютний або відносний).

    Returns:
        Словник з налаштуваннями.

    Raises:
        FileNotFoundError: Якщо файл конфігурації відсутній.
        json.JSONDecodeError: Якщо файл містить невалідний JSON.
    """
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(
            f"Файл конфігурації не знайдено: {config_path.resolve()}"
        )

    try:
        config: dict[str, Any] = json.loads(
            config_path.read_text(encoding="utf-8")
        )
        logger.info("Конфігурацію завантажено з %s", config_path.resolve())
        return config
    except json.JSONDecodeError as exc:
        logger.error(
            "Помилка розбору JSON у файлі %s: %s", config_path, exc
        )
        raise


def is_dry_run(config: dict[str, Any]) -> bool:
    """
    Повертає True, якщо увімкнено режим сухого запуску.

    У dry_run=True програма розраховує diff та логує зміни,
    але НЕ виконує жодних реальних операцій у Google API.
    """
    return bool(config.get("dry_run", True))


def get_backend(config: dict[str, Any]) -> str:
    """
    Повертає назву активного бекенду: 'mock' або 'google'.

    'mock'   — використовує MockGroupsAPI (in-memory, без мережевих запитів).
    'google' — використовує GoogleGroupsAPI (реальний Google Workspace).
    """
    return config.get("backend", "mock")


def get_google_config(config: dict[str, Any]) -> dict[str, Any]:
    """Повертає підсекцію конфігурації для Google API."""
    return config.get("google", {})


def get_db_config(config: dict[str, Any]) -> dict[str, Any]:
    """Повертає підсекцію конфігурації для підключення до Triton DB."""
    return config.get("db", {})


def get_ldap_config(config: dict[str, Any]) -> dict[str, Any]:
    """Повертає підсекцію конфігурації для LDAP-сервера."""
    return config.get("ldap", {})


def get_groups_config(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Повертає список налаштувань груп для синхронізації."""
    return config.get("groups", [])


def get_logging_config(config: dict[str, Any]) -> dict[str, Any]:
    """Повертає підсекцію конфігурації логування."""
    return config.get("logging", {})


def get_logins_json(config: dict[str, Any]) -> str:
    """Повертає шлях до JSON-файлу з логінами студентів (logins_json)."""
    return config.get("logins_json", "")
