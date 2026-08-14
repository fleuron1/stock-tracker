"""Every change to stock goes through this module.

The point of funnelling it here is that an item's numbers and its ledger row
are written in the same database transaction, so the history can never drift
out of step with the shelf. Routes call these functions; routes never write to
`items` themselves.
"""

from __future__ import annotations

import sqlite3
from datetime import date

from . import models, notifications
from .db import STATUS_LABELS, now


class StockError(Exception):
    """A rejected operation, with a message meant for the person at the shelf."""


# ------------------------------------------------- describing what happened ----
# Every ledger row gets a `detail` written here, so the history reads as
# sentences even when nobody typed a note. These are the only strings in the
# app a reader of the history will see, so they say what changed and what the
# situation is now -- not just which button was pressed.

_FIELD_LABELS = {
    "name": "Name",
    "category": "Category",
    "asset_tag": "Asset tag",
    "serial_number": "Serial",
    "location": "Location",
    "notes": "Notes",
    "status": "Status",
    "reorder_level": "Reorder level",
}


def _who(person: sqlite3.Row | None) -> str:
    """A person's name, with their department when we know it."""
    if person is None:
        return "nobody"
    if person["department"]:
        return f"{person['name']} ({person['department']})"
    return person["name"]


def _due_phrase(due_on: str | None) -> str:
    return f", due back {due_on}" if due_on else ", no date agreed"


def _clean_due(due_on: str) -> str | None:
    """Accept a date, or nothing at all -- loans are allowed to be open-ended."""
    due_on = (due_on or "").strip()
    if not due_on:
        return None
    try:
        return date.fromisoformat(due_on).isoformat()
    except ValueError:
        raise StockError(f"'{due_on}' isn't a date I understand. Use YYYY-MM-DD.")


def _lateness(loan: sqlite3.Row | None) -> str:
    """How late a return was, for the ledger. Silent when it was on time."""
    if loan is None:
        return ""
    late = models.days_overdue(loan)
    if not late:
        return ""
    return f", {late} day{'' if late == 1 else 's'} late"


def _level_phrase(old_qty: int, new_qty: int, reorder: int | None) -> str:
    """Call out the moment stock crosses its reorder level, in either direction.

    Crossing the line is the thing worth noticing weeks later, and it's
    invisible from the numbers alone unless the level is written down too.
    """
    if not reorder:
        return ""
    was_low, is_low = old_qty <= reorder, new_qty <= reorder
    if is_low and not was_low:
        return f", at or below the reorder level of {reorder}"
    if was_low and not is_low:
        return f", back above the reorder level of {reorder}"
    if is_low:
        return f", still at or below the reorder level of {reorder}"
    return ""


def describe_change(field: str, old, new) -> str:
    """One field's before and after, phrased for the history log.

    Public because a CSV import edits the same fields and should describe
    them the same way.
    """
    label = _FIELD_LABELS[field]
    if field == "status":
        return (f"{label}: {STATUS_LABELS.get(old, old)}"
                f" → {STATUS_LABELS.get(new, new)}")
    if field == "notes":
        # Notes run long; that they changed is more useful than both versions.
        if not old:
            return "Notes added"
        return "Notes cleared" if not new else "Notes rewritten"
    old_text = str(old) if old not in (None, "") else "(blank)"
    new_text = str(new) if new not in (None, "") else "(blank)"
    return f"{label}: {old_text} → {new_text}"


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

    if kind == "asset":
        detail = "Added as an asset, on the shelf"
        if asset_tag:
            detail += f", tag {asset_tag}"
    else:
        detail = f"Added as a consumable with {row_qty} in stock"
        detail += (f", reorder at {row_reorder}" if row_reorder
                   else ", no reorder level set")
    if location.strip():
        detail += f", kept in {location.strip()}"

    # Opening stock counts as the first movement in, so the ledger balances.
    models.log(conn, item_id, "created", qty_delta=(row_qty or 0), actor=actor,
               detail=detail)
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
        proposed = {
            "name": name, "category": category.strip(), "asset_tag": asset_tag or None,
            "serial_number": serial_number.strip(), "location": location.strip(),
            "notes": notes.strip(), "status": new_status,
        }
    else:
        proposed = {
            "name": name, "category": category.strip(),
            "serial_number": serial_number.strip(), "location": location.strip(),
            "notes": notes.strip(), "reorder_level": max(0, int(reorder_level)),
        }

    changes = [describe_change(field, item[field], value)
               for field, value in proposed.items()
               if (item[field] or "") != (value or "")]
    if not changes:
        # Nothing actually changed, so don't write a ledger row saying it did.
        return

    if item["kind"] == "asset":
        conn.execute(
            "UPDATE items SET name = ?, category = ?, asset_tag = ?, serial_number = ?,"
            " location = ?, notes = ?, status = ?, assigned_to = ?, updated_at = ?"
            " WHERE id = ?",
            (proposed["name"], proposed["category"], proposed["asset_tag"],
             proposed["serial_number"], proposed["location"], proposed["notes"],
             proposed["status"], assigned_to, now(), item_id),
        )
    else:
        conn.execute(
            "UPDATE items SET name = ?, category = ?, serial_number = ?, location = ?,"
            " notes = ?, reorder_level = ?, updated_at = ? WHERE id = ?",
            (proposed["name"], proposed["category"], proposed["serial_number"],
             proposed["location"], proposed["notes"], proposed["reorder_level"],
             now(), item_id),
        )

    models.log(conn, item_id, "updated", actor=actor, detail="; ".join(changes))
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
    came_from = "repair" if item["status"] == "repair" else "the shelf"
    conn.execute(
        "UPDATE items SET status = 'retired', assigned_to = NULL, updated_at = ?"
        " WHERE id = ?", (now(), item_id))
    models.log(conn, item_id, "retired", qty_delta=-1, actor=actor,
               detail=f"Retired from {came_from}, no longer in service", note=note)
    conn.commit()


# ------------------------------------------------------ assets: in / out ----

def check_out(conn: sqlite3.Connection, item_id: int, person_id: int, actor: str = "",
              note: str = "", due_on: str = "") -> None:
    """Hand an asset to someone, optionally with a date it's due back."""
    item = _require_item(conn, item_id)
    _require_kind(item, "asset")
    due = _clean_due(due_on)

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
    conn.execute(
        "INSERT INTO loans (item_id, person_id, qty, out_at, due_on, note)"
        " VALUES (?, ?, 1, ?, ?, ?)",
        (item_id, person_id, now(), due, note.strip()))
    models.log(conn, item_id, "check_out", qty_delta=-1, person_id=person_id,
               actor=actor,
               detail=f"Off the shelf to {_who(person)}{_due_phrase(due)}", note=note)
    conn.commit()


def check_in(conn: sqlite3.Connection, item_id: int, actor: str = "", note: str = "",
             to_repair: bool = False) -> None:
    item = _require_item(conn, item_id)
    _require_kind(item, "asset")
    if item["status"] not in ("assigned", "repair"):
        raise StockError(f"'{item['name']}' is not checked out.")

    # Remember who had it, so the ledger row says who brought it back.
    previous_holder = item["assigned_to"]
    loan = models.open_loan_for_item(conn, item_id)
    new_status = "repair" if to_repair else "in_stock"
    conn.execute(
        "UPDATE items SET status = ?, assigned_to = NULL, updated_at = ? WHERE id = ?",
        (new_status, now(), item_id))
    if loan is not None:
        conn.execute(
            "UPDATE loans SET returned_qty = qty, returned_at = ? WHERE id = ?",
            (now(), loan["id"]))

    if previous_holder:
        came_from = f"Back from {_who(models.get_person(conn, previous_holder))}"
    else:
        came_from = "Back from repair"
    went_to = "sent straight to repair" if to_repair else "onto the shelf"

    models.log(conn, item_id, "check_in", qty_delta=1, person_id=previous_holder,
               actor=actor, detail=f"{came_from}{_lateness(loan)}, {went_to}",
               note=note)
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
    if status == item["status"]:
        return

    conn.execute("UPDATE items SET status = ?, updated_at = ? WHERE id = ?",
                 (status, now(), item_id))
    if status == "repair":
        detail = "Sent to repair from the shelf, out of service for now"
    else:
        detail = ("Repaired and back on the shelf, ready to hand out"
                  if item["status"] == "repair" else "Marked as on the shelf")
    # Its own kind, not "updated": moving to and from repair is a movement in
    # the life of the asset, not an edit to its details.
    models.log(conn, item_id, "status", actor=actor, detail=detail, note=note)
    conn.commit()


# ------------------------------------------------ consumables: in / out ----

def stock_in(conn: sqlite3.Connection, item_id: int, qty: int, actor: str = "",
             note: str = "") -> None:
    item = _require_item(conn, item_id)
    _require_kind(item, "consumable")
    qty = int(qty)
    if qty <= 0:
        raise StockError("Enter how many are coming in (more than zero).")

    was, new_qty = item["quantity"], item["quantity"] + qty
    conn.execute(
        "UPDATE items SET quantity = quantity + ?, updated_at = ? WHERE id = ?",
        (qty, now(), item_id))
    detail = (f"{qty} in — {new_qty} now in stock, was {was}"
              f"{_level_phrase(was, new_qty, item['reorder_level'])}")
    models.log(conn, item_id, "stock_in", qty_delta=qty, actor=actor, detail=detail,
               note=note)
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

    was, new_qty = item["quantity"], item["quantity"] - qty
    conn.execute(
        "UPDATE items SET quantity = quantity - ?, updated_at = ? WHERE id = ?",
        (qty, now(), item_id))

    person = models.get_person(conn, person_id) if person_id else None
    going_to = f" to {_who(person)}" if person is not None else ""
    if new_qty == 0:
        # "none left" says everything; the reorder level is moot at zero.
        detail = f"{qty} out{going_to} — none left, was {was}"
    else:
        detail = (f"{qty} out{going_to} — {new_qty} left, was {was}"
                  f"{_level_phrase(was, new_qty, item['reorder_level'])}")

    models.log(conn, item_id, "stock_out", qty_delta=-qty, person_id=person_id,
               actor=actor, detail=detail, note=note)
    conn.commit()
    notifications.maybe_alert_low_stock(conn, item_id)


def lend(conn: sqlite3.Connection, item_id: int, qty: int, person_id: int,
         actor: str = "", note: str = "", due_on: str = "") -> int:
    """Send consumable units out that are expected back.

    Distinct from stock_out, which is for things genuinely used up. The count
    drops either way, but a loan is tracked until it returns.
    """
    item = _require_item(conn, item_id)
    _require_kind(item, "consumable")
    qty = int(qty)
    if qty <= 0:
        raise StockError("Enter how many are going out (more than zero).")
    if qty > item["quantity"]:
        raise StockError(
            f"Only {item['quantity']} of '{item['name']}' left — can't lend {qty}.")

    person = models.get_person(conn, person_id)
    if person is None:
        raise StockError("Pick who is borrowing it.")
    due = _clean_due(due_on)

    was, new_qty = item["quantity"], item["quantity"] - qty
    conn.execute(
        "UPDATE items SET quantity = quantity - ?, updated_at = ? WHERE id = ?",
        (qty, now(), item_id))
    cur = conn.execute(
        "INSERT INTO loans (item_id, person_id, qty, out_at, due_on, note)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (item_id, person_id, qty, now(), due, note.strip()))

    detail = (f"{qty} lent to {_who(person)}{_due_phrase(due)} — {new_qty} left,"
              f" was {was}{_level_phrase(was, new_qty, item['reorder_level'])}")
    models.log(conn, item_id, "lent", qty_delta=-qty, person_id=person_id,
               actor=actor, detail=detail, note=note)
    conn.commit()
    notifications.maybe_alert_low_stock(conn, item_id)
    return int(cur.lastrowid)


def return_loan(conn: sqlite3.Connection, loan_id: int, qty: int | None = None,
                actor: str = "", note: str = "") -> None:
    """Take back some or all of a loan.

    Consumable loans can come back in parts -- six of the ten cables today,
    the rest next week -- so the quantity returned is tracked separately.
    """
    loan = models.get_loan(conn, loan_id)
    if loan is None:
        raise StockError("That loan no longer exists.")
    if loan["returned_at"] is not None:
        raise StockError("That loan is already fully back.")

    outstanding = loan["outstanding"]
    qty = outstanding if qty is None else int(qty)
    if qty <= 0:
        raise StockError("Enter how many are coming back (more than zero).")
    if qty > outstanding:
        raise StockError(
            f"Only {outstanding} still out on that loan — can't return {qty}.")

    if loan["item_kind"] == "asset":
        # An asset loan is one unit, and closing it is a check-in, so that
        # the item's status and holder are updated in one place.
        check_in(conn, loan["item_id"], actor=actor, note=note)
        return

    item = _require_item(conn, loan["item_id"])
    was, new_qty = item["quantity"], item["quantity"] + qty
    returned_total = loan["returned_qty"] + qty
    fully_back = returned_total >= loan["qty"]

    conn.execute(
        "UPDATE items SET quantity = quantity + ?, updated_at = ? WHERE id = ?",
        (qty, now(), loan["item_id"]))
    conn.execute(
        "UPDATE loans SET returned_qty = ?, returned_at = ? WHERE id = ?",
        (returned_total, now() if fully_back else None, loan_id))

    who = _who(models.get_person(conn, loan["person_id"])) if loan["person_id"] else "someone"
    if fully_back:
        detail = f"{qty} returned by {who}{_lateness(loan)} — {new_qty} now in stock"
    else:
        still_out = loan["qty"] - returned_total
        detail = (f"{qty} of {loan['qty']} returned by {who}{_lateness(loan)}"
                  f" — {still_out} still out, {new_qty} now in stock")

    models.log(conn, loan["item_id"], "returned", qty_delta=qty,
               person_id=loan["person_id"], actor=actor, detail=detail, note=note)
    conn.commit()


def set_loan_due(conn: sqlite3.Connection, loan_id: int, due_on: str,
                 actor: str = "") -> None:
    """Change the date on a loan that's already out, or clear it entirely.

    Extending a date is the normal answer to "I still need this", and it
    belongs in the ledger like any other decision about the item.
    """
    loan = models.get_loan(conn, loan_id)
    if loan is None:
        raise StockError("That loan no longer exists.")
    if loan["returned_at"] is not None:
        raise StockError("That loan is already back.")

    due = _clean_due(due_on)
    if due == loan["due_on"]:
        return

    conn.execute("UPDATE loans SET due_on = ?, last_remind_at = NULL WHERE id = ?",
                 (due, loan_id))

    was = loan["due_on"] or "open-ended"
    now_due = due or "open-ended"
    models.log(conn, loan["item_id"], "updated", person_id=loan["person_id"],
               actor=actor,
               detail=f"Loan to {loan['person_name'] or 'someone'}: due date"
                      f" {was} → {now_due}")
    conn.commit()


def set_quantity(conn: sqlite3.Connection, item_id: int, new_qty: int, actor: str = "",
                 note: str = "") -> None:
    """A stocktake correction: record the difference rather than the new total."""
    item = _require_item(conn, item_id)
    _require_kind(item, "consumable")
    new_qty = int(new_qty)
    if new_qty < 0:
        raise StockError("A count can't be negative.")

    was = item["quantity"]
    delta = new_qty - was
    conn.execute("UPDATE items SET quantity = ?, updated_at = ? WHERE id = ?",
                 (new_qty, now(), item_id))

    if delta == 0:
        detail = f"Counted {new_qty}, matching the recorded count"
    else:
        gap = (f"{abs(delta)} fewer than recorded" if delta < 0
               else f"{abs(delta)} more than recorded")
        detail = (f"Stocktake: counted {new_qty}, was {was} — {gap}"
                  f"{_level_phrase(was, new_qty, item['reorder_level'])}")

    models.log(conn, item_id, "adjust", qty_delta=delta, actor=actor, detail=detail,
               note=note)
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
