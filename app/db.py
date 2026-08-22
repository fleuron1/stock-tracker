"""SQLite connection handling and the schema.

The whole database is one file (see config.DB_PATH). Copy that file and you
have a complete backup.
"""

from __future__ import annotations

import secrets
import sqlite3
from datetime import datetime
from pathlib import Path

from . import config

SCHEMA_VERSION = 5

# Applied in order to databases created by an older version. A brand-new
# database gets the current schema straight from SCHEMA_SQL and skips these.
MIGRATIONS: dict[int, list[str]] = {
    2: ["ALTER TABLE transactions ADD COLUMN detail TEXT NOT NULL DEFAULT ''"],
    3: [
        # The loans table itself comes from SCHEMA_SQL, which has already run
        # by the time migrations are applied. What's needed here is to give
        # assets that are already checked out an open loan, so nothing that is
        # currently out disappears from the Loans page. They get no due date,
        # because nobody ever agreed one.
        """
        INSERT INTO loans (item_id, person_id, qty, out_at, due_on, returned_qty)
        SELECT id, assigned_to, 1, updated_at, NULL, 0
        FROM items
        WHERE kind = 'asset' AND status = 'assigned' AND assigned_to IS NOT NULL
        """,
    ],
    # Version 4 added sign-in. Both new tables come from SCHEMA_SQL, which has
    # already run by this point, so there is nothing to alter -- existing rows
    # are untouched and the history keeps whatever names were typed before.
    4: [],
    # Version 5 added rate limiting on sign-in. Its tables come from
    # SCHEMA_SQL, so there is nothing to alter here.
    5: [],
}

SCHEMA_SQL = """
-- A few values the app generates for itself and must keep between restarts,
-- such as the key used to sign things. Not user settings.
CREATE TABLE IF NOT EXISTS settings (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);

-- Failed sign-in attempts, so a password can't be guessed at speed. Keyed by
-- the username that was tried, including ones that don't exist -- otherwise
-- the lockout itself would reveal which accounts are real.
CREATE TABLE IF NOT EXISTS login_attempts (
    username         TEXT PRIMARY KEY,
    failures         INTEGER NOT NULL DEFAULT 0,
    last_failure_at  TEXT,
    locked_until     TEXT
);

-- Staff who sign in and operate the app. Deliberately NOT the same thing as
-- `people`: a user is someone who runs the IT room, a person is someone kit
-- gets lent to. Most people never need an account, and a user need not appear
-- on the People list at all.
CREATE TABLE IF NOT EXISTS users (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    username       TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    display_name   TEXT    NOT NULL,   -- what shows in the "done by" column
    password_hash  TEXT    NOT NULL,
    is_admin       INTEGER NOT NULL DEFAULT 0,
    active         INTEGER NOT NULL DEFAULT 1,
    created_at     TEXT    NOT NULL,
    last_login_at  TEXT
);

-- Sign-in sessions, kept server-side so an account can be cut off at once by
-- deleting its rows. Only a hash of each cookie value is stored, so a copy of
-- the database is not a set of usable session cookies.
CREATE TABLE IF NOT EXISTS sessions (
    token_hash  TEXT    PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at  TEXT    NOT NULL,
    expires_at  TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

CREATE TABLE IF NOT EXISTS people (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL,
    email       TEXT    NOT NULL DEFAULT '',
    department  TEXT    NOT NULL DEFAULT '',
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT    NOT NULL
);

-- One table for both kinds of stock, split by `kind`:
--   asset      -- a single unit with its own tag, held by at most one person
--   consumable -- a pile of identical things tracked as a quantity
-- Columns that don't apply to a kind stay NULL; inventory.py is what enforces
-- which is which, not the schema.
CREATE TABLE IF NOT EXISTS items (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    kind           TEXT    NOT NULL CHECK (kind IN ('asset', 'consumable')),
    name           TEXT    NOT NULL,
    category       TEXT    NOT NULL DEFAULT '',
    asset_tag      TEXT    UNIQUE,          -- the barcode field; assets only
    serial_number  TEXT    NOT NULL DEFAULT '',
    location       TEXT    NOT NULL DEFAULT '',
    notes          TEXT    NOT NULL DEFAULT '',
    status         TEXT,                    -- assets: in_stock/assigned/repair/retired
    assigned_to    INTEGER REFERENCES people(id) ON DELETE SET NULL,
    quantity       INTEGER,                 -- consumables only
    reorder_level  INTEGER,                 -- consumables only; 0 = never flag
    last_alert_at  TEXT,                    -- low-stock email cooldown stamp
    created_at     TEXT    NOT NULL,
    updated_at     TEXT    NOT NULL
);

-- The in/out ledger. Append-only: rows are never edited or deleted, so the
-- history of a shelf is always reconstructable.
CREATE TABLE IF NOT EXISTS transactions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT    NOT NULL,
    item_id    INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    kind       TEXT    NOT NULL,
    qty_delta  INTEGER NOT NULL DEFAULT 0,
    person_id  INTEGER REFERENCES people(id) ON DELETE SET NULL,
    actor      TEXT    NOT NULL DEFAULT '',
    -- `detail` is written by the app and says what actually happened;
    -- `note` is whatever the person chose to type, and is often empty.
    -- Keeping them apart means a typed note never hides the facts.
    detail     TEXT    NOT NULL DEFAULT '',
    note       TEXT    NOT NULL DEFAULT ''
);

-- Things that have gone out and are expected back. An asset loan is always
-- one unit; a consumable loan can be several, and can come back in parts,
-- which is why the quantity out and the quantity returned are both recorded.
-- `due_on` is deliberately nullable: plenty of kit goes out open-ended, and
-- only a loan with a date can ever be overdue.
CREATE TABLE IF NOT EXISTS loans (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id        INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    person_id      INTEGER REFERENCES people(id) ON DELETE SET NULL,
    qty            INTEGER NOT NULL DEFAULT 1,
    returned_qty   INTEGER NOT NULL DEFAULT 0,
    out_at         TEXT    NOT NULL,
    due_on         TEXT,                    -- 'YYYY-MM-DD', or NULL for open-ended
    returned_at    TEXT,                    -- set once everything is back
    last_remind_at TEXT,                    -- so a borrower isn't emailed daily
    note           TEXT    NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_loans_open        ON loans(returned_at);
CREATE INDEX IF NOT EXISTS idx_loans_due         ON loans(due_on);
CREATE INDEX IF NOT EXISTS idx_loans_item        ON loans(item_id);
CREATE INDEX IF NOT EXISTS idx_loans_person      ON loans(person_id);
CREATE INDEX IF NOT EXISTS idx_items_asset_tag   ON items(asset_tag);
CREATE INDEX IF NOT EXISTS idx_items_name        ON items(name);
CREATE INDEX IF NOT EXISTS idx_items_kind        ON items(kind);
CREATE INDEX IF NOT EXISTS idx_tx_item           ON transactions(item_id);
CREATE INDEX IF NOT EXISTS idx_tx_ts             ON transactions(ts);
"""

# Transaction kinds, and how each reads in the history table.
TX_LABELS = {
    "check_out": "Checked out",
    "check_in": "Checked in",
    "lent": "Lent",
    "returned": "Returned",
    "stock_in": "Stock in",
    "stock_out": "Stock out",
    "adjust": "Adjusted",
    "created": "Created",
    "updated": "Edited",
    "status": "Status change",
    "retired": "Retired",
}

ASSET_STATUSES = ["in_stock", "assigned", "repair", "retired"]
STATUS_LABELS = {
    "in_stock": "In stock",
    "assigned": "Assigned",
    "repair": "In repair",
    "retired": "Retired",
}


def today() -> str:
    """Today as 'YYYY-MM-DD', matching how due dates are stored."""
    return datetime.now().date().isoformat()


def now() -> str:
    """Local-time ISO 8601 timestamp, to the second.

    Local rather than UTC because every reader of this app is in the same
    building as the shelf, and ISO strings still sort correctly.
    """
    return datetime.now().isoformat(sep=" ", timespec="seconds")


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Open a connection with the conventions this app relies on."""
    path = Path(db_path) if db_path is not None else config.DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread is left at its default (on) deliberately. Every route
    # is a sync `def`, so FastAPI runs the handler and its connection in one
    # worker thread; if someone later makes a route `async def`, SQLite will
    # say so loudly instead of quietly misbehaving.
    conn = sqlite3.connect(path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL keeps a reader on the dashboard from blocking someone at the shelf.
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Create the schema if it isn't there, upgrade it if it's old.

    Safe to run on every start: a new database is built from SCHEMA_SQL and
    stamped as current, and an existing one only runs the migrations it has
    not seen yet.
    """
    existing = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'items'"
    ).fetchone()[0]

    conn.executescript(SCHEMA_SQL)

    if existing:
        # An older database: bring it up to date one version at a time.
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        for step in range(version + 1, SCHEMA_VERSION + 1):
            for statement in MIGRATIONS.get(step, []):
                conn.execute(statement)

    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()
    _ensure_secret(conn)


def _ensure_secret(conn: sqlite3.Connection) -> None:
    """Generate this installation's signing key once, and keep it.

    Stored rather than configured so there is nothing for anyone to forget to
    set, and so it differs between installations.
    """
    row = conn.execute("SELECT value FROM settings WHERE key = 'secret'").fetchone()
    if row is None:
        conn.execute("INSERT INTO settings (key, value) VALUES ('secret', ?)",
                     (secrets.token_hex(32),))
        conn.commit()


def get_secret(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT value FROM settings WHERE key = 'secret'").fetchone()
    return row["value"] if row else ""
