"""
Active Directory клієнт для отримання корпоративних email-адрес студентів.

Відмінності від OpenLDAP-підходу:
  - Автентифікація: NTLM (DOMAIN\\Login), а не Simple Bind.
  - Пошук: за логіном (userPrincipalName або sAMAccountName), а не uid/cn.
  - Email-атрибут: msExchExtensionAttribute45 (Exchange розширення).

Стратегія пошуку:
  1. primary_filter  — зазвичай (userPrincipalName={login}).
  2. fallback_filter — (sAMAccountName={login}), якщо primary не дав результату.
"""

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_PRIMARY = (
    "(&(objectCategory=user)(objectClass=user)(userPrincipalName={login}))"
)
_DEFAULT_FALLBACK = (
    "(&(objectCategory=user)(objectClass=user)(sAMAccountName={login}))"
)
_DEFAULT_MAIL_ATTR = "msExchExtensionAttribute45"

# Додаткові атрибути для діагностики
_DIAG_ATTRS = [
    "distinguishedName",
    "sAMAccountName",
    "userPrincipalName",
    "displayName",
    "mail",
]


class LDAPClient:
    """
    Клієнт Active Directory (NTLM) для отримання email за логіном студента.

    Використовує ldap3 із NTLM-автентифікацією.
    Підтримує контекстний менеджер.

    Приклад:
        with LDAPClient.from_config(ldap_cfg) as ad:
            email = ad.get_email_by_login("student@univ.edu")
    """

    def __init__(
        self,
        server: str,
        port: int,
        user: str,
        password: str,
        base_dn: str = "",
        mail_attr: str = _DEFAULT_MAIL_ATTR,
        primary_filter: str = _DEFAULT_PRIMARY,
        fallback_filter: str = _DEFAULT_FALLBACK,
    ) -> None:
        """
        Args:
            server:          Хост або IP AD-сервера (напр. "10.10.16.2").
            port:            Порт LDAP (389 або 636 для LDAPS).
            user:            Логін у форматі DOMAIN\\Login для NTLM.
            password:        Пароль.
            base_dn:         Базовий DN; якщо порожній — береться з RootDSE.
            mail_attr:       Атрибут із email (за замовч. msExchExtensionAttribute45).
            primary_filter:  Шаблон основного фільтра. {login} підставляється.
            fallback_filter: Шаблон запасного фільтра (порожній → не використовується).
        """
        self._host = server
        self._port = port
        self._user = user
        self._password = password
        self._base_dn = base_dn
        self._mail_attr = mail_attr
        self._primary_filter = primary_filter
        self._fallback_filter = fallback_filter or ""
        self._conn = None
        self._resolved_base_dn: str | None = None
        self._connect()

    @classmethod
    def from_config(cls, ldap_cfg: dict[str, Any]) -> "LDAPClient":
        """Створює екземпляр із секції 'ldap' config.json."""
        return cls(
            server=ldap_cfg["server"],
            port=int(ldap_cfg.get("port", 389)),
            user=ldap_cfg["user"],
            password=ldap_cfg["password"],
            base_dn=ldap_cfg.get("base_dn", ""),
            mail_attr=ldap_cfg.get("mail_attr", _DEFAULT_MAIL_ATTR),
            primary_filter=ldap_cfg.get("primary_filter", _DEFAULT_PRIMARY),
            fallback_filter=ldap_cfg.get("fallback_filter", _DEFAULT_FALLBACK),
        )

    # ------------------------------------------------------------------
    # Підключення
    # ------------------------------------------------------------------

    def _connect(self) -> None:
        try:
            from ldap3 import ALL, NTLM, Connection, Server  # noqa: PLC0415

            server = Server(self._host, port=self._port, get_info=ALL)
            self._conn = Connection(
                server,
                user=self._user,
                password=self._password,
                authentication=NTLM,
                auto_bind=True,
                raise_exceptions=True,
            )
            logger.info("Підключено до AD: %s:%d як %s", self._host, self._port, self._user)

        except ImportError as exc:
            raise ImportError(
                "Бібліотека ldap3 не встановлена. Виконайте: pip install ldap3"
            ) from exc
        except Exception as exc:
            logger.error(
                "Не вдалося підключитися до AD (%s:%d): %s",
                self._host, self._port, exc,
            )
            raise

    def _get_base_dn(self) -> str:
        """Повертає Base DN; якщо не задано — виводить із RootDSE."""
        if self._resolved_base_dn is not None:
            return self._resolved_base_dn

        if self._base_dn:
            self._resolved_base_dn = self._base_dn
            return self._resolved_base_dn

        info = self._conn.server.info
        if info and info.other.get("defaultNamingContext"):
            self._resolved_base_dn = info.other["defaultNamingContext"][0]
            logger.info("Base DN з RootDSE: %s", self._resolved_base_dn)
            return self._resolved_base_dn

        raise RuntimeError(
            "Не вдалося визначити Base DN з RootDSE. "
            "Задайте base_dn у конфігурації."
        )

    # ------------------------------------------------------------------
    # Публічний API
    # ------------------------------------------------------------------

    def get_email_by_login(self, login: str) -> str | None:
        """
        Шукає email у AD за логіном студента (ADUserName).

        Послідовно пробує primary_filter, потім fallback_filter (якщо задано).

        Args:
            login: AD-логін у форматі 'student@univ.edu' або просто 'login'.

        Returns:
            Email із mail_attr або None якщо не знайдено.
        """
        if not login:
            return None

        try:
            from ldap3.utils.conv import escape_filter_chars  # noqa: PLC0415
            safe = escape_filter_chars(login)
        except ImportError:
            safe = login

        attrs = list({self._mail_attr} | set(_DIAG_ATTRS))
        base_dn = self._get_base_dn()

        filters_to_try = [self._primary_filter]
        if self._fallback_filter:
            filters_to_try.append(self._fallback_filter)

        for tpl in filters_to_try:
            ldap_filter = tpl.format(login=safe)
            email = self._search_email(ldap_filter, attrs, base_dn, login, tpl)
            if email is not None:
                return email

        logger.warning("AD: email не знайдено для логіна %r", login)
        return None

    # ------------------------------------------------------------------
    # Внутрішні методи
    # ------------------------------------------------------------------

    def _search_email(
        self,
        ldap_filter: str,
        attrs: list[str],
        base_dn: str,
        login: str,
        tpl: str,
    ) -> str | None:
        """Виконує один LDAP-пошук і повертає email або None."""
        try:
            self._conn.search(
                search_base=base_dn,
                search_filter=ldap_filter,
                attributes=attrs,
            )
        except Exception as exc:
            logger.error("LDAP пошук (%r) для %r: %s", tpl, login, exc)
            return None

        if not self._conn.entries:
            logger.debug("AD: нічого за фільтром %r (login=%r)", tpl, login)
            return None

        entry = self._conn.entries[0]
        if len(self._conn.entries) > 1:
            logger.warning(
                "AD: %d записів для %r (фільтр %r) — беру перший",
                len(self._conn.entries), login, tpl,
            )

        raw = self._attr_value(entry, self._mail_attr)
        if not raw:
            logger.debug(
                "AD: запис знайдено для %r, але %s порожній (UPN=%s)",
                login, self._mail_attr,
                self._attr_value(entry, "userPrincipalName") or "?",
            )
            return None

        email = raw.strip().lower()
        logger.debug("AD: %r -> %r (фільтр %r)", login, email, tpl)
        return email

    @staticmethod
    def _attr_value(entry: Any, attr: str) -> str | None:
        """Витягає перше значення атрибута з ldap3 entry."""
        try:
            val = entry[attr].value
            if val is None:
                return None
            if isinstance(val, (list, tuple)):
                val = val[0] if val else None
            return str(val).strip() if val else None
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Контекстний менеджер
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Від'єднується від AD-сервера (unbind)."""
        if self._conn:
            try:
                self._conn.unbind()
            except Exception as exc:
                logger.warning("Помилка при закритті AD: %s", exc)
            self._conn = None
            logger.info("AD-з'єднання закрито")

    def __enter__(self) -> "LDAPClient":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()
