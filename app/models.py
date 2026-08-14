"""Query helpers.

Thin wrappers over parameterised SQL. Nothing here changes stock levels --
that all lives in inventory.py so every movement is guaranteed a ledger row.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Iterable

from .db import now

# Selecting items nearly always wants the assignee's name alongside, so this
# join is factored out rather than repeated in five places.
ITEM_SELECT = """
SELECT i.*, p.name AS assigned_to_name
FROM items i
LEFT JOIN people p ON p.id = i.assigned_to
"""


# --------------------------------------------------------------- people ----

def list_people(conn: sqlite3.Connection, include_inactive: bool = False) -> list[sqlite3.Row]:
    sql = "SELECT * FROM people"
    if not include_inactive:
        sql += " WHERE active = 1"
    sql += " ORDER BY name COLLATE NOCASE"
    return conn.execute(sql).fetchall()


def get_person(conn: sqlite3.Connection, person_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM people WHERE id = ?", (person_id,)).fetchone()


def create_person(conn: sqlite3.Connection, name: str, email: str = "",
                  department: str = "") -> int:
    cur = conn.execute(
        "INSERT INTO people (name, email, department, active, created_at)"
        " VALUES (?, ?, ?, 1, ?)",
        (name.strip(), email.strip(), department.strip(), now()),
    )
    conn.commit()
    return int(cur.lastrowid)


def update_person(conn: sqlite3.Connection, person_id: int, name: str, email: str,
                  department: str, active: bool) -> None:
    conn.execute(
        "UPDATE people SET name = ?, email = ?, department = ?, active = ? WHERE id = ?",
        (name.strip(), email.strip(), department.strip(), 1 if active else 0, person_id),
    )
    conn.commit()


def person_holdings(conn: sqlite3.Connection, person_id: int) -> list[sqlite3.Row]:
    """Assets currently checked out to this person."""
    return conn.execute(
        ITEM_SELECT + " WHERE i.assigned_to = ? ORDER BY i.name COLLATE NOCASE",
        (person_id,),
    ).fetchall()


# ---------------------------------------------------------------- items ----

def get_item(conn: sqlite3.Connection, item_id: int) -> sqlite3.Row | None:
    return conn.execute(ITEM_SELECT + " WHERE i.id = ?", (item_id,)).fetchone()


def get_item_by_tag(conn: sqlite3.Connection, asset_tag: str) -> sqlite3.Row | None:
    tag = (asset_tag or "").strip()
    if not tag:
        return None
    # NOCASE so a scanner that reports uppercase still matches a tag typed in
    # lowercase.
    return conn.execute(
        ITEM_SELECT + " WHERE i.asset_tag = ? COLLATE NOCASE", (tag,)
    ).fetchone()


def list_items(conn: sqlite3.Connection, kind: str = "", category: str = "",
               status: str = "", q: str = "",
               include_retired: bool = True) -> list[sqlite3.Row]:
    where: list[str] = []
    params: list[Any] = []
    if kind:
        where.append("i.kind = ?")
        params.append(kind)
    if category:
        where.append("i.category = ?")
        params.append(category)
    if status:
        where.append("i.status = ?")
        params.append(status)
    elif not include_retired:
        where.append("(i.status IS NULL OR i.status != 'retired')")
    if q:
        where.append(
            "(i.name LIKE ? OR i.asset_tag LIKE ? OR i.serial_number LIKE ?"
            " OR i.category LIKE ? OR i.location LIKE ?)"
        )
        params.extend([f"%{q}%"] * 5)

    sql = ITEM_SELECT
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY i.kind, i.name COLLATE NOCASE"
    return conn.execute(sql, params).fetchall()


def categories(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT category FROM items WHERE category != ''"
        " ORDER BY category COLLATE NOCASE"
    ).fetchall()
    return [r["category"] for r in rows]


def tag_exists(conn: sqlite3.Connection, asset_tag: str, exclude_id: int | None = None) -> bool:
    tag = (asset_tag or "").strip()
    if not tag:
        return False
    sql = "SELECT id FROM items WHERE asset_tag = ? COLLATE NOCASE"
    params: list[Any] = [tag]
    if exclude_id is not None:
        sql += " AND id != ?"
        params.append(exclude_id)
    return conn.execute(sql, params).fetchone() is not None


# ---------------------------------------------------------- transactions ----

def log(conn: sqlite3.Connection, item_id: int, kind: str, qty_delta: int = 0,
        person_id: int | None = None, actor: str = "", note: str = "") -> None:
    """Append a ledger row. Callers commit -- see inventory.py."""
    conn.execute(
        "INSERT INTO transactions (ts, item_id, kind, qty_delta, person_id, actor, note)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (now(), item_id, kind, qty_delta, person_id, actor.strip(), note.strip()),
    )


TX_SELECT = """
SELECT t.*, i.name AS item_name, i.kind AS item_kind, i.asset_tag,
       p.name AS person_name
FROM transactions t
JOIN items i ON i.id = t.item_id
LEFT JOIN people p ON p.id = t.person_id
"""


def item_history(conn: sqlite3.Connection, item_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        TX_SELECT + " WHERE t.item_id = ? ORDER BY t.ts DESC, t.id DESC", (item_id,)
    ).fetchall()


def list_transactions(conn: sqlite3.Connection, item_id: int | None = None,
                      person_id: int | None = None, kind: str = "",
                      date_from: str = "", date_to: str = "",
                      limit: int | None = None) -> list[sqlite3.Row]:
    where: list[str] = []
    params: list[Any] = []
    if item_id:
        where.append("t.item_id = ?")
        params.append(item_id)
    if person_id:
        where.append("t.person_id = ?")
        params.append(person_id)
    if kind:
        where.append("t.kind = ?")
        params.append(kind)
    if date_from:
        where.append("t.ts >= ?")
        params.append(date_from)
    if date_to:
        # Timestamps carry a time, so compare against the end of that day.
        where.append("t.ts <= ?")
        params.append(f"{date_to} 23:59:59")

    sql = TX_SELECT
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY t.ts DESC, t.id DESC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return conn.execute(sql, params).fetchall()


# ------------------------------------------------------------ dashboard ----

def stats(conn: sqlite3.Connection) -> dict[str, int]:
    def scalar(sql: str, params: Iterable[Any] = ()) -> int:
        return int(conn.execute(sql, tuple(params)).fetchone()[0])

    return {
        "assets_total": scalar(
            "SELECT COUNT(*) FROM items WHERE kind = 'asset' AND status != 'retired'"),
        "assets_in_stock": scalar(
            "SELECT COUNT(*) FROM items WHERE kind = 'asset' AND status = 'in_stock'"),
        "assets_assigned": scalar(
            "SELECT COUNT(*) FROM items WHERE kind = 'asset' AND status = 'assigned'"),
        "assets_repair": scalar(
            "SELECT COUNT(*) FROM items WHERE kind = 'asset' AND status = 'repair'"),
        "consumable_lines": scalar(
            "SELECT COUNT(*) FROM items WHERE kind = 'consumable'"),
        "consumable_units": scalar(
            "SELECT COALESCE(SUM(quantity), 0) FROM items WHERE kind = 'consumable'"),
        "people_count": scalar("SELECT COUNT(*) FROM people WHERE active = 1"),
    }
