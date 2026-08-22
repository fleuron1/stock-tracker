"""CSV import and export.

Import is all-or-nothing on purpose: the whole file is validated before a
single row is written, so a typo on line 40 leaves you with the inventory you
had rather than half a new one.
"""

from __future__ import annotations

import csv
import io
import sqlite3
from typing import Any

from . import inventory, models, notifications, validation
from .db import now

ITEM_COLUMNS = [
    "kind", "name", "category", "asset_tag", "serial_number", "location",
    "status", "assigned_to", "quantity", "reorder_level", "notes",
]

HISTORY_COLUMNS = [
    "timestamp", "action", "item", "asset_tag", "item_kind", "qty_delta",
    "person", "done_by", "what_happened", "note",
]

# Header spellings people actually use in spreadsheets, mapped to our names.
HEADER_ALIASES = {
    "type": "kind",
    "item": "name",
    "item_name": "name",
    "description": "name",
    "tag": "asset_tag",
    "assettag": "asset_tag",
    "asset_id": "asset_tag",
    "barcode": "asset_tag",
    "serial": "serial_number",
    "serialno": "serial_number",
    "serial_no": "serial_number",
    "qty": "quantity",
    "count": "quantity",
    "in_stock": "quantity",
    "reorder": "reorder_level",
    "min": "reorder_level",
    "min_level": "reorder_level",
    "minimum": "reorder_level",
    "assigned": "assigned_to",
    "assignee": "assigned_to",
    "holder": "assigned_to",
    "room": "location",
    "shelf": "location",
    "comment": "notes",
    "comments": "notes",
}


def _normalise_header(header: str) -> str:
    key = (header or "").strip().lower().replace(" ", "_").replace("-", "_")
    return HEADER_ALIASES.get(key, key)


# ---------------------------------------------------------------- export ----

def export_items(conn: sqlite3.Connection) -> str:
    """Every item as CSV. Re-importing this file changes nothing -- it round-trips."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(ITEM_COLUMNS)
    for item in models.list_items(conn):
        writer.writerow([
            item["kind"],
            item["name"],
            item["category"],
            item["asset_tag"] or "",
            item["serial_number"],
            item["location"],
            item["status"] or "",
            item["assigned_to_name"] or "",
            "" if item["quantity"] is None else item["quantity"],
            "" if item["reorder_level"] is None else item["reorder_level"],
            item["notes"],
        ])
    return buf.getvalue()


def export_history(conn: sqlite3.Connection, rows: list[sqlite3.Row] | None = None) -> str:
    """The ledger as CSV. Pass `rows` to export exactly what a filtered view shows."""
    from .db import TX_LABELS

    if rows is None:
        rows = models.list_transactions(conn)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(HISTORY_COLUMNS)
    for tx in rows:
        writer.writerow([
            tx["ts"],
            TX_LABELS.get(tx["kind"], tx["kind"]),
            tx["item_name"],
            tx["asset_tag"] or "",
            tx["item_kind"],
            tx["qty_delta"],
            tx["person_name"] or "",
            tx["actor"],
            tx["detail"],
            tx["note"],
        ])
    return buf.getvalue()


# ---------------------------------------------------------------- import ----

def _parse_int(value: str, field: str, line: int, errors: list[str]) -> int:
    text = (value or "").strip()
    if not text:
        return 0
    try:
        number = int(float(text))
    except ValueError:
        errors.append(f"Line {line}: '{text}' is not a whole number for {field}.")
        return 0
    if number < 0:
        errors.append(f"Line {line}: {field} can't be negative.")
        return 0
    return number


def import_items(conn: sqlite3.Connection, file_text: str,
                 actor: str = "csv-import") -> tuple[dict[str, int], list[str]]:
    """Load items from CSV text.

    Returns (summary, errors). If `errors` is non-empty nothing at all was
    written. Rows are matched to existing items by asset tag where there is
    one, otherwise by name and category -- so re-importing a file you exported
    updates the same rows instead of duplicating them.

    Assignments are never changed by an import: who holds an asset is decided
    by checking it in and out, not by a spreadsheet.
    """
    summary = {"created": 0, "updated": 0, "unchanged": 0}
    errors: list[str] = []

    text = file_text.lstrip("﻿")
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        return summary, ["The file is empty."]

    # Column order doesn't matter; only the header spellings do.
    fields = {_normalise_header(h): h for h in reader.fieldnames if h}
    if "name" not in fields:
        return summary, [
            "No 'name' column found. The header row needs at least a 'name' column; "
            f"found: {', '.join(reader.fieldnames)}."
        ]

    def cell(row: dict[str, Any], key: str) -> str:
        original = fields.get(key)
        if original is None:
            return ""
        return (row.get(original) or "").strip()

    planned: list[dict[str, Any]] = []
    seen_tags: dict[str, int] = {}

    for offset, row in enumerate(reader):
        line = offset + 2  # +1 for the header, +1 because humans count from 1
        name = cell(row, "name")
        if not name:
            # A trailing blank line is normal in spreadsheets; skip it quietly.
            if not any((v or "").strip() for v in row.values()):
                continue
            errors.append(f"Line {line}: no name given.")
            continue

        tag = cell(row, "asset_tag")
        quantity_given = cell(row, "quantity") != ""
        kind = cell(row, "kind").lower()
        if kind in ("assets", "asset"):
            kind = "asset"
        elif kind in ("consumables", "consumable", "stock"):
            kind = "consumable"
        elif not kind:
            # No kind column: a row with a count is a consumable, anything
            # else is a single asset.
            kind = "consumable" if quantity_given else "asset"
        else:
            errors.append(
                f"Line {line}: kind '{kind}' should be either 'asset' or 'consumable'.")
            continue

        status = cell(row, "status").lower().replace(" ", "_")
        if kind == "asset":
            if status in ("", "available", "in_stock"):
                status = "in_stock"
            elif status not in ("in_stock", "assigned", "repair", "retired"):
                errors.append(
                    f"Line {line}: status '{status}' should be one of in_stock,"
                    " assigned, repair, retired.")
                continue

        if tag:
            if tag.lower() in seen_tags:
                errors.append(
                    f"Line {line}: asset tag '{tag}' also appears on line"
                    f" {seen_tags[tag.lower()]}.")
                continue
            seen_tags[tag.lower()] = line

        # A spreadsheet is just as capable of carrying a 5,000-character name
        # or an invisible character as a form is, so uploaded rows go through
        # the same checks -- reported by line, like every other CSV problem.
        try:
            name = validation.clean_text(name, "name", required=True)
            tag = validation.clean_text(tag, "asset_tag")
            clean = {
                "category": validation.clean_text(cell(row, "category"), "category"),
                "serial_number": validation.clean_text(
                    cell(row, "serial_number"), "serial_number"),
                "location": validation.clean_text(cell(row, "location"), "location"),
                "notes": validation.clean_text(cell(row, "notes"), "notes",
                                               multiline=True),
            }
        except validation.ValidationError as exc:
            errors.append(f"Line {line}: {exc}")
            continue

        entry = {
            "line": line,
            "kind": kind,
            "name": name,
            "category": clean["category"],
            "asset_tag": tag,
            "serial_number": clean["serial_number"],
            "location": clean["location"],
            "notes": clean["notes"],
            "status": status if kind == "asset" else None,
            "quantity": _parse_int(cell(row, "quantity"), "quantity", line, errors)
            if kind == "consumable" else None,
            "reorder_level": _parse_int(
                cell(row, "reorder_level"), "reorder level", line, errors)
            if kind == "consumable" else None,
        }

        existing = None
        if tag:
            existing = models.get_item_by_tag(conn, tag)
        else:
            existing = conn.execute(
                "SELECT * FROM items WHERE name = ? COLLATE NOCASE"
                " AND category = ? COLLATE NOCASE AND kind = ?"
                " AND asset_tag IS NULL LIMIT 1",
                (name, entry["category"], kind),
            ).fetchone()

        if existing is not None and existing["kind"] != kind:
            errors.append(
                f"Line {line}: '{name}' already exists as a"
                f" {existing['kind']}, so it can't be imported as a {kind}.")
            continue

        entry["existing"] = existing
        planned.append(entry)

    if errors:
        # Nothing is written when anything is wrong -- a half-imported
        # inventory is worse than none.
        return summary, errors
    if not planned:
        return summary, ["No rows to import."]

    stamp = now()
    touched_consumables: list[int] = []
    for entry in planned:
        existing = entry["existing"]
        if existing is None:
            cur = conn.execute(
                "INSERT INTO items (kind, name, category, asset_tag, serial_number,"
                " location, notes, status, assigned_to, quantity, reorder_level,"
                " created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)",
                (entry["kind"], entry["name"], entry["category"],
                 entry["asset_tag"] or None, entry["serial_number"], entry["location"],
                 entry["notes"], entry["status"], entry["quantity"],
                 entry["reorder_level"], stamp, stamp),
            )
            item_id = int(cur.lastrowid)
            if entry["kind"] == "asset":
                opening = "as an asset, on the shelf"
            else:
                opening = f"as a consumable with {entry['quantity']} in stock"
            models.log(conn, item_id, "created", qty_delta=entry["quantity"] or 0,
                       actor=actor,
                       detail=f"Imported from CSV line {entry['line']}, added {opening}")
            summary["created"] += 1
            if entry["kind"] == "consumable":
                touched_consumables.append(item_id)
        else:
            item_id = int(existing["id"])
            # Re-importing a file you exported should be a no-op, so a row
            # that matches what's already stored is skipped entirely -- no
            # UPDATE, no ledger noise.
            same_details = all(
                (existing[field] or "") == entry[field]
                for field in ("name", "category", "serial_number", "location", "notes")
            )
            delta = 0
            if entry["kind"] == "consumable":
                delta = (entry["quantity"] or 0) - (existing["quantity"] or 0)
                same_details = same_details and delta == 0 and (
                    (existing["reorder_level"] or 0) == (entry["reorder_level"] or 0))

            if same_details:
                summary["unchanged"] += 1
                continue

            from_csv = f"From CSV line {entry['line']}"
            # Describe the edit the same way a hand edit would be described.
            edited = [
                inventory.describe_change(field, existing[field], entry[field])
                for field in ("name", "category", "serial_number", "location", "notes")
                if (existing[field] or "") != (entry[field] or "")
            ]

            if entry["kind"] == "asset":
                # Status and holder are left alone: an import must not silently
                # hand someone's laptop back to the shelf.
                conn.execute(
                    "UPDATE items SET name = ?, category = ?, serial_number = ?,"
                    " location = ?, notes = ?, updated_at = ? WHERE id = ?",
                    (entry["name"], entry["category"], entry["serial_number"],
                     entry["location"], entry["notes"], stamp, item_id),
                )
                models.log(conn, item_id, "updated", actor=actor,
                           detail=f"{from_csv}: {'; '.join(edited)}")
            else:
                if (existing["reorder_level"] or 0) != (entry["reorder_level"] or 0):
                    edited.append(inventory.describe_change(
                        "reorder_level", existing["reorder_level"],
                        entry["reorder_level"]))
                conn.execute(
                    "UPDATE items SET name = ?, category = ?, serial_number = ?,"
                    " location = ?, notes = ?, quantity = ?, reorder_level = ?,"
                    " updated_at = ? WHERE id = ?",
                    (entry["name"], entry["category"], entry["serial_number"],
                     entry["location"], entry["notes"], entry["quantity"],
                     entry["reorder_level"], stamp, item_id),
                )
                if delta:
                    # A count that differs from the file is a stocktake, so it
                    # lands in the ledger as one.
                    gap = (f"{abs(delta)} fewer than recorded" if delta < 0
                           else f"{abs(delta)} more than recorded")
                    detail = (f"{from_csv}: counted {entry['quantity']},"
                              f" was {existing['quantity']} — {gap}")
                    if edited:
                        detail += f". {'; '.join(edited)}"
                    models.log(conn, item_id, "adjust", qty_delta=delta, actor=actor,
                               detail=detail)
                else:
                    models.log(conn, item_id, "updated", actor=actor,
                               detail=f"{from_csv}: {'; '.join(edited)}")
                touched_consumables.append(item_id)
            summary["updated"] += 1

    conn.commit()

    # A bulk import can push things below their reorder level too.
    for item_id in touched_consumables:
        notifications.maybe_alert_low_stock(conn, item_id)
    return summary, []
