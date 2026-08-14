"""Low-stock email.

Fully built and working, but switched off by default: with ALERTS_ENABLED
unset or false, every function here quietly does nothing. Turning it on later
is a .env edit, not a code change.

Rule of the module: a mail server problem must never break a stock movement.
Every failure is caught and logged, and the caller carries on.
"""

from __future__ import annotations

import logging
import smtplib
import sqlite3
from datetime import datetime, timedelta
from email.message import EmailMessage

from . import config
from .db import now

log = logging.getLogger("stock.notifications")


def _send(subject: str, body: str) -> None:
    """Send one plain-text mail. Raises on failure -- callers decide what that means."""
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = config.ALERT_FROM
    msg["To"] = ", ".join(config.ALERT_TO)
    msg.set_content(body)

    with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=20) as smtp:
        if config.SMTP_USE_TLS:
            smtp.starttls()
        if config.SMTP_USER:
            smtp.login(config.SMTP_USER, config.SMTP_PASSWORD)
        smtp.send_message(msg)


def _within_cooldown(last_alert_at: str | None) -> bool:
    if not last_alert_at:
        return False
    try:
        last = datetime.fromisoformat(last_alert_at)
    except ValueError:
        return False
    return datetime.now() - last < timedelta(hours=config.ALERT_COOLDOWN_HOURS)


def maybe_alert_low_stock(conn: sqlite3.Connection, item_id: int) -> bool:
    """Email the manager if this consumable has just dropped to its reorder level.

    Returns True only if a mail was actually sent. Skips silently when alerts
    are off, when the item is still above its level, or when one went out for
    this item recently -- so an afternoon of handing out cables sends one
    email, not thirty.
    """
    if config.alert_config_problem() is not None:
        return False

    item = conn.execute(
        "SELECT * FROM items WHERE id = ? AND kind = 'consumable'", (item_id,)
    ).fetchone()
    if item is None:
        return False
    if not item["reorder_level"] or item["quantity"] > item["reorder_level"]:
        # Back above the line -- clear the stamp so the next dip alerts again.
        if item["last_alert_at"]:
            conn.execute("UPDATE items SET last_alert_at = NULL WHERE id = ?", (item_id,))
            conn.commit()
        return False
    if _within_cooldown(item["last_alert_at"]):
        return False

    subject = f"[IT stock] Low: {item['name']} ({item['quantity']} left)"
    body = (
        f"{item['name']} is down to {item['quantity']}, at or below its reorder "
        f"level of {item['reorder_level']}.\n\n"
        f"Category: {item['category'] or '-'}\n"
        f"Location: {item['location'] or '-'}\n\n"
        f"Item page: {config.PUBLIC_URL}/items/{item['id']}\n\n"
        f"-- Sent automatically by the IT room stock app."
    )

    try:
        _send(subject, body)
    except Exception:
        # Logged, not raised: the stock movement that triggered this has
        # already been committed and must stand.
        log.exception("Could not send low-stock email for item %s", item_id)
        return False

    conn.execute("UPDATE items SET last_alert_at = ? WHERE id = ?", (now(), item_id))
    conn.commit()
    log.info("Low-stock email sent for '%s'", item["name"])
    return True


def send_test_email() -> tuple[bool, str]:
    """Prove the SMTP settings work. Returns (ok, message) for display in the UI."""
    problem = config.alert_config_problem()
    if problem:
        return False, problem
    try:
        _send(
            "[IT stock] Test email",
            "This is a test from the IT room stock app.\n\n"
            "If you can read this, low-stock alerts will reach you.\n\n"
            f"App address: {config.PUBLIC_URL}",
        )
    except Exception as exc:
        log.exception("Test email failed")
        return False, f"Sending failed: {exc}"
    return True, f"Test email sent to {', '.join(config.ALERT_TO)}."
