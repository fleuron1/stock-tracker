"""Sign-in: passwords, sessions and user accounts.

Two things are deliberately kept apart in this app:

  users   -- staff who sign in and operate it. They have passwords, and the
             signed-in user's name is what lands in the "done by" column of
             every history entry.
  people  -- who equipment is lent to. No passwords, no sign-in; managed on
             the People page and untouched by any of this.

Someone can be both, but they don't have to be, and the two lists are never
merged.

Password hashing uses PBKDF2-HMAC-SHA256 from the standard library. It is
well understood, needs no extra dependency, and at 600,000 iterations is a
reasonable setting for an internal tool.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
from datetime import datetime, timedelta

from .db import now

PBKDF2_ROUNDS = 600_000
SESSION_COOKIE = "stock_session"
SESSION_DAYS = 30


class AuthError(Exception):
    """A rejected sign-in or account change, with a message for the screen."""


# ------------------------------------------------------------ passwords ----

def hash_password(password: str, *, rounds: int = PBKDF2_ROUNDS) -> str:
    """Hash a password for storage. Format: pbkdf2_sha256$rounds$salt$hash."""
    if not password:
        raise AuthError("A password is needed.")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds)
    return f"pbkdf2_sha256${rounds}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Check a password against a stored hash, in constant time."""
    try:
        algorithm, rounds, salt_hex, digest_hex = stored.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        expected = bytes.fromhex(digest_hex)
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(rounds))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(expected, actual)


def check_password_rules(password: str) -> None:
    """Refuse the passwords that are genuinely not worth having.

    Deliberately mild: this is an internal tool behind an office network, and
    rules that force a symbol and a digit mostly produce Password1! on a
    sticky note.
    """
    if len(password) < 8:
        raise AuthError("Use at least 8 characters.")
    if password.lower() in ("password", "12345678", "password1", "qwertyui",
                            "changeme", "letmein1"):
        raise AuthError("That password is too easy to guess.")


# ---------------------------------------------------------------- users ----

def user_count(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])


def get_user(conn: sqlite3.Connection, user_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def get_user_by_name(conn: sqlite3.Connection, username: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM users WHERE username = ? COLLATE NOCASE",
                        ((username or "").strip(),)).fetchone()


def list_users(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM users ORDER BY active DESC, username COLLATE NOCASE"
    ).fetchall()


def create_user(conn: sqlite3.Connection, username: str, password: str,
                display_name: str = "", is_admin: bool = False) -> int:
    username = (username or "").strip()
    if not username:
        raise AuthError("A username is needed.")
    if " " in username:
        raise AuthError("Usernames can't contain spaces.")
    if get_user_by_name(conn, username) is not None:
        raise AuthError(f"There is already a user called '{username}'.")
    check_password_rules(password)

    cur = conn.execute(
        "INSERT INTO users (username, display_name, password_hash, is_admin,"
        " active, created_at) VALUES (?, ?, ?, ?, 1, ?)",
        (username, (display_name or username).strip(), hash_password(password),
         1 if is_admin else 0, now()),
    )
    conn.commit()
    return int(cur.lastrowid)


def set_password(conn: sqlite3.Connection, user_id: int, password: str) -> None:
    check_password_rules(password)
    conn.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                 (hash_password(password), user_id))
    # Signing out everywhere is the point of a password reset.
    conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
    conn.commit()


def update_user(conn: sqlite3.Connection, user_id: int, display_name: str,
                is_admin: bool, active: bool) -> None:
    user = get_user(conn, user_id)
    if user is None:
        raise AuthError("That user no longer exists.")

    # Never let the last admin be demoted or switched off; someone has to be
    # able to let people back in.
    if user["is_admin"] and (not is_admin or not active):
        others = conn.execute(
            "SELECT COUNT(*) FROM users WHERE is_admin = 1 AND active = 1"
            " AND id != ?", (user_id,)).fetchone()[0]
        if not others:
            raise AuthError(
                "This is the only admin left. Make someone else an admin first.")

    conn.execute(
        "UPDATE users SET display_name = ?, is_admin = ?, active = ? WHERE id = ?",
        ((display_name or user["username"]).strip(), 1 if is_admin else 0,
         1 if active else 0, user_id))
    if not active:
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
    conn.commit()


# -------------------------------------------------------------- sessions ----

def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def sign_in(conn: sqlite3.Connection, username: str, password: str) -> str:
    """Check credentials and return a new session token.

    The same message comes back whether the username or the password was
    wrong, so the form can't be used to find out who has an account.
    """
    user = get_user_by_name(conn, username)
    if user is None or not verify_password(password, user["password_hash"]):
        raise AuthError("That username and password don't match.")
    if not user["active"]:
        raise AuthError("That account has been switched off.")

    token = secrets.token_urlsafe(32)
    expires = (datetime.now() + timedelta(days=SESSION_DAYS)).isoformat(
        sep=" ", timespec="seconds")
    conn.execute(
        "INSERT INTO sessions (token_hash, user_id, created_at, expires_at)"
        " VALUES (?, ?, ?, ?)", (_token_hash(token), user["id"], now(), expires))
    conn.execute("UPDATE users SET last_login_at = ? WHERE id = ?",
                 (now(), user["id"]))
    conn.commit()
    return token


def user_for_token(conn: sqlite3.Connection, token: str) -> sqlite3.Row | None:
    """The signed-in user for a cookie value, or None if it isn't valid."""
    if not token:
        return None
    row = conn.execute(
        "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id"
        " WHERE s.token_hash = ? AND s.expires_at > ? AND u.active = 1",
        (_token_hash(token), now())).fetchone()
    return row


def sign_out(conn: sqlite3.Connection, token: str) -> None:
    if token:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?",
                     (_token_hash(token),))
        conn.commit()


def clear_expired_sessions(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (now(),))
    conn.commit()
