"""
Налаштування системи логування: одночасний вивід у консоль та файл.

Формат рядка: "2026-05-05 14:30:00 [INFO] triton-sync: повідомлення"
"""

import logging
import sys
from pathlib import Path


def setup_logger(
    name: str = "triton-sync",
    level: str = "INFO",
    logfile: str | None = None,
) -> logging.Logger:
    """
    Створює та налаштовує логер із виводом у консоль і (опційно) у файл.

    Якщо логер із таким ім'ям вже існує і має хендлери — вони очищуються,
    щоб уникнути дублювання рядків при повторній ініціалізації.

    Args:
        name:    Ім'я логера (відображається у кожному рядку лога).
        level:   Рівень логування: 'DEBUG', 'INFO', 'WARNING', 'ERROR'.
        logfile: Шлях до файлу лога. None — лише консоль.

    Returns:
        Налаштований екземпляр logging.Logger.
    """
    numeric_level = getattr(logging, level.upper(), logging.INFO)

    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    log = logging.getLogger(name)
    log.setLevel(numeric_level)

    # Очищаємо старі хендлери, щоб уникнути дублювання при повторному виклику
    if log.handlers:
        log.handlers.clear()

    # Консольний хендлер — завжди увімкнений.
    # На Windows (cp1251) форсуємо UTF-8, щоб коректно виводити кирилицю та
    # Unicode-символи. errors='replace' — запасний варіант якщо reconfigure недоступний.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except AttributeError:
        pass  # Python < 3.7 або не TextIOWrapper — ігноруємо
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    console_handler.setLevel(numeric_level)
    log.addHandler(console_handler)

    # Файловий хендлер — лише якщо задано logfile
    if logfile:
        file_path = Path(logfile)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(
            str(file_path), encoding="utf-8", mode="a"
        )
        file_handler.setFormatter(formatter)
        file_handler.setLevel(numeric_level)
        log.addHandler(file_handler)
        log.debug("Файловий лог: %s", file_path.resolve())

    return log
