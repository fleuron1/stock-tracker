"""Configuration, read once at import time from the environment and .env.

Deliberately dependency-free: .env parsing is about fifteen lines, and adding
python-dotenv for it would not earn its place in requirements.txt.
"""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Load KEY=value lines into os.environ, without clobbering real env vars."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        # A genuine environment variable always wins over the file.
        os.environ.setdefault(key, value)


_load_dotenv(PROJECT_ROOT / ".env")


def _str(key: str, default: str) -> str:
    return os.environ.get(key, default).strip()


def _int(key: str, default: int) -> int:
    try:
        return int(_str(key, str(default)))
    except ValueError:
        return default


def _bool(key: str, default: bool) -> bool:
    return _str(key, "true" if default else "false").lower() in ("1", "true", "yes", "on")


HOST = _str("HOST", "0.0.0.0")
PORT = _int("PORT", 8000)

_db_path = Path(_str("DB_PATH", "stock.db"))
DB_PATH = _db_path if _db_path.is_absolute() else PROJECT_ROOT / _db_path

ALERTS_ENABLED = _bool("ALERTS_ENABLED", False)
ALERT_TO = [addr.strip() for addr in _str("ALERT_TO", "").split(",") if addr.strip()]
ALERT_FROM = _str("ALERT_FROM", "")
ALERT_COOLDOWN_HOURS = _int("ALERT_COOLDOWN_HOURS", 24)

SMTP_HOST = _str("SMTP_HOST", "")
SMTP_PORT = _int("SMTP_PORT", 587)
SMTP_USE_TLS = _bool("SMTP_USE_TLS", True)
SMTP_USER = _str("SMTP_USER", "")
SMTP_PASSWORD = _str("SMTP_PASSWORD", "")

# The base URL used in alert emails so a link back to the item works. Falls
# back to the machine's hostname, which is usually right on a LAN.
import socket  # noqa: E402  (kept next to its only use)

PUBLIC_URL = _str("PUBLIC_URL", f"http://{socket.gethostname()}:{PORT}").rstrip("/")


def alert_config_problem() -> str | None:
    """Return a human-readable reason alerts can't send, or None if they can.

    Used by the Data page so a misconfiguration is visible in the UI rather
    than only in the server log.
    """
    if not ALERTS_ENABLED:
        return "Alerts are switched off (ALERTS_ENABLED=false in .env)."
    if not SMTP_HOST:
        return "No SMTP_HOST is set in .env."
    if not ALERT_FROM:
        return "No ALERT_FROM address is set in .env."
    if not ALERT_TO:
        return "No ALERT_TO recipient is set in .env."
    return None
