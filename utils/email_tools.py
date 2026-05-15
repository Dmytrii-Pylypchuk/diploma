"""
Утиліти для нормалізації та базової валідації email-адрес.

Нормалізація: strip() + lower().
Валідація: regex-перевірка формату local@domain.tld.
"""

import logging
import re

logger = logging.getLogger(__name__)

# RFC 5321-сумісний спрощений паттерн для перевірки email
# Дозволяє: літери, цифри, '.', '_', '+', '-' у локальній частині;
# домен із мінімум однією крапкою.
_EMAIL_PATTERN = re.compile(
    r"^[a-zA-Z0-9_.+\-]+@[a-zA-Z0-9\-]+(?:\.[a-zA-Z0-9\-]+)+$"
)


def normalize_email(raw: str) -> str:
    """
    Нормалізує email-адресу: видаляє крайні пробіли та переводить у нижній регістр.

    Args:
        raw: Вхідний рядок із email-адресою.

    Returns:
        Нормалізована email-адреса.
    """
    return raw.strip().lower()


def is_valid_email(email: str) -> bool:
    """
    Виконує базову regex-валідацію нормалізованої email-адреси.

    Не виконує DNS-перевірку — лише синтаксичну відповідність формату.

    Args:
        email: Нормалізована email-адреса (без пробілів, у нижньому регістрі).

    Returns:
        True якщо формат коректний, False — інакше.
    """
    return bool(_EMAIL_PATTERN.match(email))


def normalize_and_validate(raw: str | None) -> str | None:
    """
    Нормалізує email-адресу і повертає її якщо вона валідна, або None.

    Призначена для обробки даних із LDAP або інших зовнішніх джерел,
    де email може бути порожнім, з пробілами, або у неправильному форматі.

    Args:
        raw: Вхідна (можливо брудна) email-адреса. Може бути None.

    Returns:
        Нормалізована валідна email-адреса або None.
    """
    if not raw or not raw.strip():
        return None

    normalized = normalize_email(raw)

    if not is_valid_email(normalized):
        logger.warning("Невалідна email-адреса, пропущено: %r", raw)
        return None

    return normalized


def normalize_email_set(emails: list[str]) -> set[str]:
    """
    Нормалізує та валідує список email-адрес, повертаючи множину коректних.

    Некоректні та порожні адреси мовчки відфільтровуються з WARNING у лог.

    Args:
        emails: Список сирих email-адрес.

    Returns:
        Множина нормалізованих валідних email-адрес.
    """
    result: set[str] = set()
    for raw in emails:
        validated = normalize_and_validate(raw)
        if validated:
            result.add(validated)
    return result
