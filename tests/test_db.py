"""Schema creation and upgrades."""

from __future__ import annotations

import sqlite3

from app import db, inventory, models

# The transactions table exactly as version 1 shipped it -- no `detail`.
V1_SCHEMA = """
CREATE TABLE people (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
    email TEXT NOT NULL DEFAULT '', department TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);
CREATE TABLE items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL CHECK (kind IN ('asset', 'consumable')),
    name TEXT NOT NULL, category TEXT NOT NULL DEFAULT '', asset_tag TEXT UNIQUE,
    serial_number TEXT NOT NULL DEFAULT '', location TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '', status TEXT,
    assigned_to INTEGER REFERENCES people(id) ON DELETE SET NULL,
    quantity INTEGER, reorder_level INTEGER, last_alert_at TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    kind TEXT NOT NULL, qty_delta INTEGER NOT NULL DEFAULT 0,
    person_id INTEGER REFERENCES people(id) ON DELETE SET NULL,
    actor TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '');
PRAGMA user_version = 1;
"""


def columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def test_a_new_database_is_created_at_the_current_version(tmp_path):
    conn = db.connect(tmp_path / "new.db")
    db.init_db(conn)

    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    assert "detail" in columns(conn, "transactions")
    conn.close()


def test_running_init_twice_is_harmless(tmp_path):
    conn = db.connect(tmp_path / "twice.db")
    db.init_db(conn)
    item_id = inventory.create_item(conn, kind="consumable", name="Cables",
                                    quantity=5, actor="Ali")

    db.init_db(conn)  # e.g. the server restarting

    assert models.get_item(conn, item_id)["quantity"] == 5
    conn.close()


def test_a_version_1_database_gains_the_detail_column_without_losing_data(tmp_path):
    """An IT room already running this must upgrade in place, not start over."""
    path = tmp_path / "old.db"
    old = db.connect(path)
    old.executescript(V1_SCHEMA)
    old.execute("INSERT INTO items (kind, name, status, created_at, updated_at)"
                " VALUES ('asset', 'Old laptop', 'in_stock', '2020-01-01', '2020-01-01')")
    old.execute("INSERT INTO transactions (ts, item_id, kind, qty_delta, actor, note)"
                " VALUES ('2020-01-01', 1, 'created', 0, 'Ali', 'from the old system')")
    old.commit()
    old.close()

    conn = db.connect(path)
    db.init_db(conn)

    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    assert "detail" in columns(conn, "transactions")

    # The old row survived, its note intact and its new column simply empty.
    row = conn.execute("SELECT * FROM transactions").fetchone()
    assert row["note"] == "from the old system"
    assert row["detail"] == ""

    # And the upgraded database still works.
    inventory.check_out(conn, 1, models.create_person(conn, "Sam"), actor="Ali")
    assert models.item_history(conn, 1)[0]["detail"] == "Off the shelf to Sam"
    conn.close()
