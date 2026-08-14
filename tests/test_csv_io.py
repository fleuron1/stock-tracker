"""Tests for CSV import and export."""

from __future__ import annotations

import pytest

from app import csv_io, db, inventory, models


@pytest.fixture()
def conn(tmp_path):
    connection = db.connect(tmp_path / "test.db")
    db.init_db(connection)
    yield connection
    connection.close()


SAMPLE = """name,kind,category,asset_tag,location,quantity,reorder_level
Latitude 5540,asset,Laptop,IT-001,Shelf A,,
Cat6 patch cable 2m,consumable,Cables,,Bin 3,40,10
"""


def test_import_creates_items_of_both_kinds(conn):
    summary, errors = csv_io.import_items(conn, SAMPLE)

    assert errors == []
    assert summary["created"] == 2

    laptop = models.get_item_by_tag(conn, "IT-001")
    assert laptop["kind"] == "asset"
    assert laptop["status"] == "in_stock"
    assert laptop["location"] == "Shelf A"

    cable = models.list_items(conn, kind="consumable")[0]
    assert cable["quantity"] == 40
    assert cable["reorder_level"] == 10
    # Opening stock is in the ledger, so the history balances from day one.
    assert models.item_history(conn, cable["id"])[0]["qty_delta"] == 40


def test_export_then_import_changes_nothing(conn):
    csv_io.import_items(conn, SAMPLE)
    before = len(models.list_items(conn))

    exported = csv_io.export_items(conn)
    summary, errors = csv_io.import_items(conn, exported)

    assert errors == []
    assert summary["created"] == 0
    assert summary["updated"] == 0
    assert summary["unchanged"] == 2
    assert len(models.list_items(conn)) == before


def test_column_order_and_spelling_are_forgiving(conn):
    text = ("Qty,Item,Type,Min Level,Shelf\n"
            "25,USB-C dock,consumable,5,Cupboard 2\n")
    summary, errors = csv_io.import_items(conn, text)

    assert errors == []
    assert summary["created"] == 1
    item = models.list_items(conn)[0]
    assert item["name"] == "USB-C dock"
    assert item["quantity"] == 25
    assert item["reorder_level"] == 5
    assert item["location"] == "Cupboard 2"


def test_kind_is_inferred_from_a_quantity_when_not_given(conn):
    text = "name,quantity\nHDMI cable,12\n"
    csv_io.import_items(conn, text)
    assert models.list_items(conn)[0]["kind"] == "consumable"

    text = "name,asset_tag\nProjector,IT-900\n"
    csv_io.import_items(conn, text)
    assert models.get_item_by_tag(conn, "IT-900")["kind"] == "asset"


def test_a_bad_row_stops_the_whole_file(conn):
    text = ("name,kind,quantity\n"
            "Good cable,consumable,10\n"
            "Bad cable,consumable,lots\n")
    summary, errors = csv_io.import_items(conn, text)

    assert summary["created"] == 0
    assert any("Line 3" in e for e in errors)
    # Nothing at all was written -- not even the valid first row.
    assert models.list_items(conn) == []


def test_duplicate_tags_within_one_file_are_caught(conn):
    text = ("name,asset_tag\n"
            "Laptop one,IT-500\n"
            "Laptop two,IT-500\n")
    summary, errors = csv_io.import_items(conn, text)

    assert summary["created"] == 0
    assert any("also appears on line 2" in e for e in errors)


def test_a_missing_name_column_is_explained(conn):
    summary, errors = csv_io.import_items(conn, "tag,qty\nIT-1,4\n")
    assert summary["created"] == 0
    assert "name" in errors[0]


def test_import_updates_counts_as_a_stocktake(conn):
    csv_io.import_items(conn, SAMPLE)
    cable = models.list_items(conn, kind="consumable")[0]

    text = "name,kind,category,quantity,reorder_level\nCat6 patch cable 2m,consumable,Cables,25,10\n"
    summary, errors = csv_io.import_items(conn, text)

    assert errors == []
    assert summary["updated"] == 1
    assert models.get_item(conn, cable["id"])["quantity"] == 25

    latest = models.item_history(conn, cable["id"])[0]
    assert latest["kind"] == "adjust"
    assert latest["qty_delta"] == -15


def test_import_never_takes_an_asset_off_the_person_holding_it(conn):
    csv_io.import_items(conn, SAMPLE)
    laptop = models.get_item_by_tag(conn, "IT-001")
    person = models.create_person(conn, "Sam Okafor")
    inventory.check_out(conn, laptop["id"], person, actor="Ali")

    # A re-import that says the laptop is on the shelf must not override
    # reality -- someone is holding it.
    text = "name,kind,asset_tag,status,location\nLatitude 5540,asset,IT-001,in_stock,Shelf B\n"
    summary, errors = csv_io.import_items(conn, text)

    assert errors == []
    after = models.get_item_by_tag(conn, "IT-001")
    assert after["status"] == "assigned"
    assert after["assigned_to"] == person
    assert after["location"] == "Shelf B"  # descriptive fields still update


def test_history_export_has_a_row_per_movement(conn):
    csv_io.import_items(conn, SAMPLE)
    cable = models.list_items(conn, kind="consumable")[0]
    inventory.stock_out(conn, cable["id"], 5, actor="Ali", note="for the new desks")

    text = csv_io.export_history(conn)
    lines = [line for line in text.splitlines() if line.strip()]

    assert lines[0].startswith("timestamp,action,item")
    assert len(lines) == 4  # header + two 'created' + one 'stock out'
    assert "for the new desks" in text
    assert "Ali" in text
