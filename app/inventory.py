"""Every change to stock goes through this module.

The point of funnelling it here is that an item's numbers and its ledger row
are written in the same database transaction, so the history can never drift
out of step with the shelf. Routes call these functions; routes never write to
`items` themselves.
"""

from __future__ import annotations

import sqlite3

from . import models, notifications
from .db import now


class StockError(Exception):
    """A rejected operation, with a message meant for the person at the shelf."""


def _require_item(conn: sqlite3.Connection, item_id: int) -> sqlite3.Row:
    item = models.get_item(conn, item_id)
    if item is None:
        raise StockError("That item no longer exists.")
    return item


def _require_kind(item: sqlite3.Row, kind: str) -> None:
    if item["kind"] != kind:
        word = "an asset" if item["kind"] == "asset" else "a consumable"
        raise StockError(f"'{item['name']}' is {word}, so that action doesn't apply to it.")


# ------------------------------------------------------- create and edit ----

def create_item(conn: sqlite3.Connection, *, kind: str, name: str, category: str = "",
                asset_tag: str = "", serial_number: str = "", location: str = "",
                notes: str = "", status: str = "in_stock", quantity: int = 0,
                reorder_level: int = 0, actor: str = "") -> int:
    name = name.strip()
    if not name:
        raise StockError("An item needs a name.")
    if kind not in ("asset", "consumable"):
        raise StockError("An item must be either an asset or a consumable.")

    asset_tag = asset_tag.strip()
    if asset_tag and models.tag_exists(conn, asset_tag):
        raise StockError(f"Asset tag '{asset_tag}' is already used by another item.")

    if kind == "asset":
        if status not in ("in_stock", "assigned", "repair", "retired"):
            status = "in_stock"
        # An asset created straight into 'assigned' has nobody to assign to
        # yet, so it starts on the shelf and gets checked out separately.
        if status == "assigned":
            status = "in_stock"
        row_status, row_qty, row_reorder = status, None, None
    else:
        row_status = None
        row_qty = max(0, int(quantity))
        row_reorder = max(0, int(reorder_level))

    stamp = now()
    cur = conn.execute(
        "INSERT INTO items (kind, name, category, asset_tag, serial_number, location,"
        " notes, status, assigned_to, quantity, reorder_level, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)",
        (kind, name, category.strip(), asset_tag or None, serial_number.strip(),
         location.strip(), notes.strip(), row_status, row_qty, row_reorder, stamp, stamp),
    )
    item_id = int(cur.lastrowid)
    # Opening stock counts as the first movement in, so the ledger balances.
    models.log(conn, item_id, "created", qty_delta=(row_qty or 0), actor=actor,
               note=f"Added {'asset' if kind == 'asset' else f'{row_qty} unit(s)'}")
    conn.commit()

    if kind == "consumable":
        notifications.maybe_alert_low_stock(conn, item_id)
    return item_id


def update_item(conn: sqlite3.Connection, item_id: int, *, name: str, category: str = "",
                asset_tag: str = "", serial_number: str = "", location: str = "",
                notes: str = "", status: str = "", reorder_level: int = 0,
                actor: str = "") -> None:
    """Edit an item's details.

    Quantity is deliberately not editable here -- use set_quantity() so the
    change lands in the ledger as a stocktake adjustment.
    """
    item = _require_item(conn, item_id)
    name = name.strip()
    if not name:
        raise StockError("An item needs a name.")

    asset_tag = asset_tag.strip()
    if asset_tag and models.tag_exists(conn, asset_tag, exclude_id=item_id):
        raise StockError(f"Asset tag '{asset_tag}' is already used by another item.")

    if item["kind"] == "asset":
        new_status = status if status in ("in_stock", "repair", "retired") else item["status"]
        # Changing the status out from under a checked-out asset would strand
        # it, so send it home first.
        assigned_to = item["assigned_to"]
        if item["status"] == "assigned" and new_status != "assigned":
            assigned_to = None
        elif item["status"] == "assigned":
            new_status = "assigned"
        conn.execute(
            "UPDATE items SET name = ?, category = ?, asset_tag = ?, serial_number = ?,"
            " location = ?, notes = ?, status = ?, assigned_to = ?, updated_at = ?"
            " WHERE id = ?",
            (name, category.strip(), asset_tag or None, serial_number.strip(),
             location.strip(), notes.strip(), new_status, assigned_to, now(), item_id),
        )
    else:
        conn.execute(
            "UPDATE items SET name = ?, category = ?, serial_number = ?, location = ?,"
            " notes = ?, reorder_level = ?, updated_at = ? WHERE id = ?",
            (name, category.strip(), serial_number.strip(), location.strip(),
             notes.strip(), max(0, int(reorder_level)), now(), item_id),
        )

    models.log(conn, item_id, "updated", actor=actor, note="Details edited")
    conn.commit()

    if item["kind"] == "consumable":
        notifications.maybe_alert_low_stock(conn, item_id)


def retire_item(conn: sqlite3.Connection, item_id: int, actor: str = "",
                note: str = "") -> None:
    """Take an asset out of service. Kept in the database so its history survives."""
    item = _require_item(conn, item_id)
    _require_kind(item, "asset")
    if item["status"] == "assigned":
        raise StockError(
            f"'{item['name']}' is still checked out to {item['assigned_to_name']}."
            " Check it back in before retiring it."
        )
    conn.execute(
        "UPDATE items SET status = 'retired', assigned_to = NULL, updated_at = ?"
        " WHERE id = ?", (now(), item_id))
    models.log(conn, item_id, "retired", qty_delta=-1, actor=actor, note=note)
    conn.commit()


# ------------------------------------------------------ assets: in / out ----

def check_out(conn: sqlite3.Connection, item_id: int, person_id: int, actor: str = "",
              note: str = "") -> None:
    item = _require_item(conn, item_id)
    _require_kind(item, "asset")

    if item["status"] == "assigned":
        raise StockError(
            f"'{item['name']}' is already checked out to {item['assigned_to_name']}.")
    if item["status"] == "repair":
        raise StockError(f"'{item['name']}' is marked as in repair.")
    if item["status"] == "retired":
        raise StockError(f"'{item['name']}' has been retired.")

    person = models.get_person(conn, person_id)
    if person is None:
        raise StockError("Pick who the item is going to.")

    conn.execute(
        "UPDATE items SET status = 'assigned', assigned_to = ?, updated_at = ?"
        " WHERE id = ?", (person_id, now(), item_id))
    models.log(conn, item_id, "check_out", qty_delta=-1, person_id=person_id,
               actor=actor, note=note)
    conn.commit()


def check_in(conn: sqlite3.Connection, item_id: int, actor: str = "", note: str = "",
             to_repair: bool = False) -> None:
    item = _require_item(conn, item_id)
    _require_kind(item, "asset")
    if item["status"] not in ("assigned", "repair"):
        raise StockError(f"'{item['name']}' is not checked out.")

    # Remember who had it, so the ledger row says who brought it back.
    previous_holder = item["assigned_to"]
    new_status = "repair" if to_repair else "in_stock"
    conn.execute(
        "UPDATE items SET status = ?, assigned_to = NULL, updated_at = ? WHERE id = ?",
        (new_status, now(), item_id))
    models.log(conn, item_id, "check_in", qty_delta=1, person_id=previous_holder,
               actor=actor, note=note)
    conn.commit()


def set_status(conn: sqlite3.Connection, item_id: int, status: str, actor: str = "",
               note: str = "") -> None:
    """Move an asset between in_stock and repair without a person involved."""
    item = _require_item(conn, item_id)
    _require_kind(item, "asset")
    if status not in ("in_stock", "repair"):
        raise StockError("An asset can only be moved to 'in stock' or 'in repair' here.")
    if item["status"] == "assigned":
        raise StockError(
            f"'{item['name']}' is checked out. Check it in first.")
    conn.execute("UPDATE items SET status = ?, updated_at = ? WHERE id = ?",
                 (status, now(), item_id))
    models.log(conn, item_id, "updated", actor=actor,
               note=note or f"Status set to {status.replace('_', ' ')}")
    conn.commit()


# ------------------------------------------------ consumables: in / out ----

def stock_in(conn: sqlite3.Connection, item_id: int, qty: int, actor: str = "",
             note: str = "") -> None:
    item = _require_item(conn, item_id)
    _require_kind(item, "consumable")
    qty = int(qty)
    if qty <= 0:
        raise StockError("Enter how many are coming in (more than zero).")

    conn.execute(
        "UPDATE items SET quantity = quantity + ?, updated_at = ? WHERE id = ?",
        (qty, now(), item_id))
    models.log(conn, item_id, "stock_in", qty_delta=qty, actor=actor, note=note)
    conn.commit()
    notifications.maybe_alert_low_stock(conn, item_id)


def stock_out(conn: sqlite3.Connection, item_id: int, qty: int,
              person_id: int | None = None, actor: str = "", note: str = "") -> None:
    item = _require_item(conn, item_id)
    _require_kind(item, "consumable")
    qty = int(qty)
    if qty <= 0:
        raise StockError("Enter how many are going out (more than zero).")
    if qty > item["quantity"]:
        raise StockError(
            f"Only {item['quantity']} of '{item['name']}' left — can't take {qty}.")

    conn.execute(
        "UPDATE items SET quantity = quantity - ?, updated_at = ? WHERE id = ?",
        (qty, now(), item_id))
    models.log(conn, item_id, "stock_out", qty_delta=-qty, person_id=person_id,
               actor=actor, note=note)
    conn.commit()
    notifications.maybe_alert_low_stock(conn, item_id)


def set_quantity(conn: sqlite3.Connection, item_id: int, new_qty: int, actor: str = "",
                 note: str = "") -> None:
    """A stocktake correction: record the difference rather than the new total."""
    item = _require_item(conn, item_id)
    _require_kind(item, "consumable")
    new_qty = int(new_qty)
    if new_qty < 0:
        raise StockError("A count can't be negative.")

    delta = new_qty - item["quantity"]
    conn.execute("UPDATE items SET quantity = ?, updated_at = ? WHERE id = ?",
                 (new_qty, now(), item_id))
    models.log(conn, item_id, "adjust", qty_delta=delta, actor=actor,
               note=note or f"Counted {new_qty} (was {item['quantity']})")
    conn.commit()
    notifications.maybe_alert_low_stock(conn, item_id)


# ------------------------------------------------------------ low stock ----

def low_stock_items(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Consumables at or below their reorder level. A level of 0 means never flag."""
    return conn.execute(
        "SELECT * FROM items WHERE kind = 'consumable' AND reorder_level > 0"
        " AND quantity <= reorder_level ORDER BY (quantity * 1.0 / reorder_level),"
        " name COLLATE NOCASE"
    ).fetchall()
