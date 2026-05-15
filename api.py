"""
Flask API-сервер для модуля triton_sync.

Endpoints:
  GET  /                  -> index.html (SPA)
  GET  /api/health        -> { ok, backend, dry_run, groups_count }
  GET  /api/config        -> поточний config.json
  POST /api/config        -> зберегти config.json
  GET  /api/groups        -> список груп із config
  POST /api/preview       -> { group_email } -> diff (dry_run=True)
  POST /api/sync          -> { group_email, dry_run } -> diff + stats
  GET  /api/logs          -> останні рядки sync.log

Конвеєр у режимі backend='google':
  TritonDB -> LoginsProvider (JSON) -> LDAPClient (NTLM) -> GoogleGroupsAPI

Запуск:
  python api.py               # -> http://localhost:5001
  python api.py --port 8080
"""

import argparse
import logging
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

from google_api.google_groups_api import GoogleGroupsAPI, MockGroupsAPI
from logic.sync_logic import apply_diff, compute_diff
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

# ---------------------------------------------------------------------------
# Ініціалізація
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
STATIC_DIR = BASE_DIR / "static"
LOG_PATH = BASE_DIR / "logs" / "sync.log"
LOG_TAIL_LINES = 200

app = Flask(__name__, static_folder=str(STATIC_DIR))
CORS(app)

_logger: logging.Logger | None = None


def get_logger() -> logging.Logger:
    global _logger
    if _logger is None:
        try:
            cfg = load_config(CONFIG_PATH)
            log_cfg = get_logging_config(cfg)
            _logger = setup_logger(
                "triton-api",
                level=log_cfg.get("level", "INFO"),
                logfile=log_cfg.get("file"),
            )
        except Exception:
            _logger = setup_logger("triton-api")
    return _logger


# ---------------------------------------------------------------------------
# Допоміжні функції
# ---------------------------------------------------------------------------

def _read_config() -> dict[str, Any]:
    return load_config(CONFIG_PATH)


def _mock_desired_emails(rule: dict[str, Any]) -> tuple[set[str], dict[str, Any]]:
    """
    Симулює конвеєр Triton->JSON->AD для mock-режиму.

    Генерує різні набори email залежно від speciality_id, year, course_id, group_name.
    """
    speciality_id = rule.get("speciality_id", 3651)
    year = str(rule.get("year", "2021"))
    course_id = rule.get("course_id")
    group = (rule.get("group_name") or "").lower()

    # Пул студентів (mock) за спеціальністю + курсом
    pool: dict[tuple, set[str]] = {
        (3651, "2021", None): {
            "o.ivanenko@domain.com", "m.koval@domain.com",
            "a.melnyk@domain.com",  "t.shevchenko@domain.com",
            "d.boyko@domain.com",   "p.petrenko@domain.com",
            "n.lysenko@domain.com", "v.kravchenko@domain.com",
        },
        (3651, "2021", 4): {
            "o.ivanenko@domain.com", "m.koval@domain.com",
            "a.melnyk@domain.com",
        },
        (3651, "2021", 3): {
            "p.petrenko@domain.com", "n.lysenko@domain.com",
        },
    }
    group_pool: dict[str, set[str]] = {
        "фі-22": {"o.ivanenko@domain.com", "m.koval@domain.com"},
        "фі-23": {"a.melnyk@domain.com",   "t.shevchenko@domain.com"},
    }

    if group:
        emails = group_pool.get(group, {"mock.student@domain.com"})
    else:
        key = (int(speciality_id), year, int(course_id) if course_id else None)
        emails = pool.get(key, {f"student{i}@domain.com" for i in range(1, 4)})

    names = {e: e.split("@")[0].replace(".", " ").title() for e in emails}
    return emails, {
        "students_from_db": len(emails),
        "logins_resolved": len(emails),
        "emails_resolved": len(emails),
        "ldap_missing": 0,
        "pipeline": "mock",
    }, names


def _real_desired_emails(
    config: dict[str, Any],
    rule: dict[str, Any],
) -> tuple[set[str], dict[str, Any]]:
    """
    Реальний конвеєр: TritonDB -> LoginsProvider -> LDAPClient (NTLM).

    Lazy-import — не завантажуємо pyodbc/ldap3 у mock-режимі.
    """
    from data_providers.ldap_client import LDAPClient          # noqa: PLC0415
    from data_providers.logins_provider import LoginsProvider  # noqa: PLC0415
    from data_providers.triton_db import TritonDB              # noqa: PLC0415
    from main import fetch_students, resolve_emails            # noqa: PLC0415

    db_cfg = get_db_config(config)
    ldap_cfg = get_ldap_config(config)
    log = get_logger()

    logins = LoginsProvider.from_config(config, base_dir=BASE_DIR)
    log.info("LoginsProvider: %d записів", len(logins))

    with TritonDB.from_config(db_cfg) as db:
        students = fetch_students(db, rule)
    log.info("Triton DB: %d студентів", len(students))

    with LDAPClient.from_config(ldap_cfg) as ad:
        emails, no_login, names = resolve_emails(students, logins, ad)

    logins_resolved = len(students) - no_login
    return emails, {
        "students_from_db": len(students),
        "logins_resolved": logins_resolved,
        "emails_resolved": len(emails),
        "ldap_missing": len(students) - len(emails),
        "pipeline": "triton+json+ldap",
    }, names


def _build_google_api(config: dict[str, Any]) -> GoogleGroupsAPI | MockGroupsAPI:
    backend = get_backend(config)

    if backend == "mock":
        return MockGroupsAPI(
            initial_members={
                grp["group_email"]: [
                    "old.student.1@domain.com",
                    "old.student.2@domain.com",
                ]
                for grp in get_groups_config(config)
            }
        )

    google_cfg = get_google_config(config)
    service_file = BASE_DIR / google_cfg.get("service_account_file", "service_account.json")
    api = GoogleGroupsAPI(
        service_account_file=str(service_file),
        admin_email=google_cfg["admin_email"],
        batch_size=int(google_cfg.get("batch_size", 100)),
    )
    if api.service is None:
        raise RuntimeError(
            f"Не вдалося ініціалізувати Google API: {api.init_error}"
        )
    return api


def _run_sync(
    config: dict[str, Any],
    group_email: str,
    dry_run: bool,
) -> dict[str, Any]:
    """
    Повний цикл синхронізації для однієї групи.

    1. Знаходимо rule за group_email у config.groups
    2. desired = mock або реальний конвеєр
    3. actual  = Google Groups API
    4. compute_diff -> apply_diff (якщо не dry_run)
    """
    log = get_logger()
    backend = get_backend(config)

    group_cfg = next(
        (g for g in get_groups_config(config)
         if g["group_email"].strip().lower() == group_email.strip().lower()),
        None,
    )
    if group_cfg is None:
        raise ValueError(f"Група {group_email!r} не знайдена у config.groups")

    rule: dict[str, Any] = group_cfg.get("rule", {})

    if backend == "mock":
        desired, pipeline_stats, names = _mock_desired_emails(rule)
    else:
        desired, pipeline_stats, names = _real_desired_emails(config, rule)

    log.info(
        "[%s] %s: desired=%d, backend=%s, dry_run=%s",
        "PREVIEW" if dry_run else "SYNC",
        group_email, len(desired), backend, dry_run,
    )

    api = _build_google_api(config)
    actual = set(api.get_members(group_email))

    diff = compute_diff(desired, actual)
    apply_diff(group_email, diff, api, dry_run=dry_run)

    log.info(
        "Результат %s: +%d -%d =%d",
        group_email, len(diff.to_add), len(diff.to_remove), len(diff.unchanged),
    )

    return {
        "group_email": group_email,
        "to_add":      sorted(diff.to_add),
        "to_remove":   sorted(diff.to_remove),
        "unchanged":   sorted(diff.unchanged),
        "invalid_emails": [],
        "names":       names,
        "stats": {
            **pipeline_stats,
            "actual_members":  len(actual),
            "diff_add":        len(diff.to_add),
            "diff_remove":     len(diff.to_remove),
            "diff_unchanged":  len(diff.unchanged),
            "dry_run":         dry_run,
            "backend":         backend,
        },
    }


# ---------------------------------------------------------------------------
# Статичні файли
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return send_from_directory(str(STATIC_DIR), "index.html")


@app.route("/<path:filename>")
def static_files(filename: str):
    return send_from_directory(str(STATIC_DIR), filename)


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------

@app.route("/api/health", methods=["GET"])
def api_health():
    try:
        cfg = _read_config()
        return jsonify({
            "ok": True,
            "backend":      get_backend(cfg),
            "dry_run":      is_dry_run(cfg),
            "groups_count": len(get_groups_config(cfg)),
        })
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/config", methods=["GET"])
def api_get_config():
    try:
        return jsonify(_read_config())
    except FileNotFoundError:
        return jsonify({"error": "config.json не знайдено"}), 404
    except Exception as exc:
        get_logger().error("GET /api/config: %s", exc)
        return jsonify({"error": str(exc)}), 500


@app.route("/api/config", methods=["POST"])
def api_post_config():
    data = request.get_json(silent=True)
    if not data or not isinstance(data, dict):
        return jsonify({"error": "Тіло запиту має бути JSON-об'єктом"}), 400

    missing = [k for k in ("backend",) if k not in data]
    if missing:
        return jsonify({"error": f"Відсутні обов'язкові поля: {missing}"}), 400

    try:
        import json
        CONFIG_PATH.write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        get_logger().info("config.json збережено")
        return jsonify({"ok": True})
    except Exception as exc:
        get_logger().error("POST /api/config: %s", exc)
        return jsonify({"error": str(exc)}), 500


@app.route("/api/groups", methods=["GET", "PUT"])
def api_groups():
    if request.method == "GET":
        try:
            cfg = _read_config()
            groups = [
                {"group_email": g["group_email"], "rule": g.get("rule", {})}
                for g in get_groups_config(cfg)
            ]
            return jsonify({"groups": groups})
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    # PUT — зберігає лише масив groups, не торкаючись інших полів
    data = request.get_json(silent=True)
    if not isinstance(data, list):
        return jsonify({"error": "Тіло запиту має бути масивом груп"}), 400

    for g in data:
        if not g.get("group_email"):
            return jsonify({"error": "Кожна група має мати 'group_email'"}), 400
        rule = g.get("rule", {})
        if not rule.get("speciality_id") or not rule.get("year"):
            return jsonify({"error": "Кожне правило має мати 'speciality_id' та 'year'"}), 400

    try:
        import json
        cfg = _read_config()
        cfg["groups"] = data
        CONFIG_PATH.write_text(
            json.dumps(cfg, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        get_logger().info("groups оновлено: %d груп", len(data))
        return jsonify({"ok": True, "count": len(data)})
    except Exception as exc:
        get_logger().error("PUT /api/groups: %s", exc)
        return jsonify({"error": str(exc)}), 500


@app.route("/api/preview", methods=["POST"])
def api_preview():
    payload = request.get_json(silent=True) or {}
    group_email = (payload.get("group_email") or "").strip()

    if not group_email:
        return jsonify({"error": "Поле group_email є обов'язковим"}), 400

    try:
        cfg = _read_config()
        result = _run_sync(cfg, group_email, dry_run=True)
        return jsonify(result)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 404
    except Exception as exc:
        get_logger().exception("POST /api/preview для %s: %s", group_email, exc)
        return jsonify({"error": str(exc)}), 500


@app.route("/api/sync", methods=["POST"])
def api_sync():
    payload = request.get_json(silent=True) or {}
    group_email = (payload.get("group_email") or "").strip()
    dry_run = bool(payload.get("dry_run", True))

    if not group_email:
        return jsonify({"error": "Поле group_email є обов'язковим"}), 400

    log = get_logger()
    log.info("POST /api/sync: group=%s dry_run=%s", group_email, dry_run)

    try:
        cfg = _read_config()
        result = _run_sync(cfg, group_email, dry_run=dry_run)
        return jsonify(result)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 404
    except Exception as exc:
        log.exception("POST /api/sync для %s: %s", group_email, exc)
        return jsonify({"error": str(exc)}), 500


@app.route("/api/logs", methods=["GET"])
def api_logs():
    lines_count = int(request.args.get("lines", LOG_TAIL_LINES))

    if not LOG_PATH.exists():
        return jsonify({"lines": [], "total": 0, "file": str(LOG_PATH)})

    try:
        all_lines = LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
        tail = all_lines[-lines_count:] if len(all_lines) > lines_count else all_lines
        return jsonify({"lines": tail, "total": len(all_lines), "file": str(LOG_PATH)})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# Точка входу
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="triton_sync Web API Server")
    parser.add_argument("--host",  default="0.0.0.0")
    parser.add_argument("--port",  type=int, default=5001)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    if not (STATIC_DIR / "index.html").exists():
        print(f"[WARN] static/index.html не знайдено у {STATIC_DIR}")

    print(f"Triton Sync API: http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=args.debug, use_reloader=False)


if __name__ == "__main__":
    main()
