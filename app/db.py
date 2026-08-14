"""SQLite connection handling and the schema.

The whole database is one file (see config.DB_PATH). Copy that file and you
have a complete backup.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from . import config

SCHEMA_VERSION = 2

# Applied in order to databases created by an older version. A brand-new
# database gets the current schema straight from SCHEMA_SQL and skips these.
MIGRATIONS: dict[int, list[str]] = {
    2: ["ALTER TABLE transactions ADD COLUMN detail TEXT NOT NULL DEFAULT ''"],
}

SCHEMA_SQL = """
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
