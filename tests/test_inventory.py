"""Tests for the stock movement rules.

Each test gets its own throwaway database file, so nothing here touches the
real stock.db.
"""

from __future__ import annotations

import pytest

from app import db, inventory, models


@pytest.fixture()
def conn(tmp_path):
    connection = db.connect(tmp_path / "test.db")
    db.init_db(connection)
    yield connection
    connection.close()


@pytest.fixture()
def person(conn):
    return models.create_person(conn, "Sam Okafor", "sam@example.com", "IT")


def make_asset(conn, name="Latitude 5540", tag="IT-001"):
    return inventory.create_item(conn, kind="asset", name=name, asset_tag=tag,
                                 category="Laptop", actor="tester")


def make_consumable(conn, name="Cat6 patch cable 2m", qty=10, reorder=3):
    return inventory.create_item(conn, kind="consumable", name=name, quantity=qty,
                                 reorder_level=reorder, category="Cables", actor="tester")


# ------------------------------------------------------- assets in / out ----

def test_check_out_then_in_leaves_a_trail(conn, person):
    item_id = make_asset(conn)
    inventory.check_out(conn, item_id, person, actor="Ali", note="new starter")

    item = models.get_item(conn, item_id)
    assert item["status"] == "assigned"
    assert item["assigned_to"] == person
    assert item["assigned_to_name"] == "Sam Okafor"

    inventory.check_in(conn, item_id, actor="Ali")
    item = models.get_item(conn, item_id)
    assert item["status"] == "in_stock"
    assert item["assigned_to"] is None

    history = models.item_history(conn, item_id)
    kinds = [row["kind"] for row in history]
    assert kinds == ["check_in", "check_out", "created"]  # newest first

    check_out_row = next(r for r in history if r["kind"] == "check_out")
    assert check_out_row["actor"] == "Ali"
    assert check_out_row["person_id"] == person
    assert check_out_row["note"] == "new starter"
    # The person who had it is recorded on the way back in, too.
    assert next(r for r in history if r["kind"] == "check_in")["person_id"] == person


def test_cannot_check_out_something_already_out(conn, person):
    item_id = make_asset(conn)
    other = models.create_person(conn, "Jo Reyes")
    inventory.check_out(conn, item_id, person, actor="Ali")

    with pytest.raises(inventory.StockError, match="already checked out"):
        inventory.check_out(conn, item_id, other, actor="Ali")

    # The failed attempt changed nothing and wrote no ledger row.
    assert models.get_item(conn, item_id)["assigned_to"] == person
    assert len(models.item_history(conn, item_id)) == 2


def test_check_in_faulty_sends_it_to_repair(conn, person):
    item_id = make_asset(conn)
    inventory.check_out(conn, item_id, person, actor="Ali")
    inventory.check_in(conn, item_id, actor="Ali", to_repair=True)

    assert models.get_item(conn, item_id)["status"] == "repair"
    with pytest.raises(inventory.StockError, match="in repair"):
        inventory.check_out(conn, item_id, person, actor="Ali")


def test_cannot_retire_something_someone_is_holding(conn, person):
    item_id = make_asset(conn)
    inventory.check_out(conn, item_id, person, actor="Ali")

    with pytest.raises(inventory.StockError, match="still checked out"):
        inventory.retire_item(conn, item_id, actor="Ali")

    inventory.check_in(conn, item_id, actor="Ali")
    inventory.retire_item(conn, item_id, actor="Ali")
    assert models.get_item(conn, item_id)["status"] == "retired"


def test_duplicate_asset_tags_are_refused(conn):
    make_asset(conn, tag="IT-001")
    with pytest.raises(inventory.StockError, match="already used"):
        make_asset(conn, name="Another laptop", tag="IT-001")
    # Case shouldn't let one slip through either.
    with pytest.raises(inventory.StockError, match="already used"):
        make_asset(conn, name="Another laptop", tag="it-001")


def test_consumable_actions_are_refused_on_assets(conn):
    item_id = make_asset(conn)
    with pytest.raises(inventory.StockError, match="doesn't apply"):
        inventory.stock_out(conn, item_id, 1, actor="Ali")


# -------------------------------------------------- consumables in / out ----

def test_stock_out_and_in_move_the_count(conn, person):
    item_id = make_consumable(conn, qty=10)

    inventory.stock_out(conn, item_id, 4, person_id=person, actor="Ali")
    assert models.get_item(conn, item_id)["quantity"] == 6

    inventory.stock_in(conn, item_id, 20, actor="Ali", note="delivery")
    assert models.get_item(conn, item_id)["quantity"] == 26

    deltas = [r["qty_delta"] for r in models.item_history(conn, item_id)]
    assert deltas == [20, -4, 10]  # newest first, opening stock last


def test_cannot_take_out_more_than_there_is(conn):
    item_id = make_consumable(conn, qty=2)

    with pytest.raises(inventory.StockError, match="Only 2"):
        inventory.stock_out(conn, item_id, 5, actor="Ali")

    assert models.get_item(conn, item_id)["quantity"] == 2
    assert len(models.item_history(conn, item_id)) == 1  # just the opening row


def test_zero_and_negative_amounts_are_refused(conn):
    item_id = make_consumable(conn, qty=5)
    for qty in (0, -3):
        with pytest.raises(inventory.StockError):
            inventory.stock_out(conn, item_id, qty, actor="Ali")
        with pytest.raises(inventory.StockError):
            inventory.stock_in(conn, item_id, qty, actor="Ali")
    assert models.get_item(conn, item_id)["quantity"] == 5


def test_stocktake_records_the_difference(conn):
    item_id = make_consumable(conn, qty=10)
    inventory.set_quantity(conn, item_id, 7, actor="Ali")

    item = models.get_item(conn, item_id)
    assert item["quantity"] == 7

    latest = models.item_history(conn, item_id)[0]
    assert latest["kind"] == "adjust"
    assert latest["qty_delta"] == -3
    assert "was 10" in latest["note"]


# ------------------------------------------------------------ low stock ----

def test_low_stock_flags_only_what_is_at_or_below_its_level(conn):
    low = make_consumable(conn, name="Cables", qty=10, reorder=3)
    fine = make_consumable(conn, name="Mice", qty=10, reorder=3)
    unset = make_consumable(conn, name="Screws", qty=1, reorder=0)

    assert inventory.low_stock_items(conn) == []

    inventory.stock_out(conn, low, 7, actor="Ali")  # down to exactly 3
    flagged = [row["id"] for row in inventory.low_stock_items(conn)]
    assert flagged == [low]
    assert fine not in flagged and unset not in flagged

    inventory.stock_in(conn, low, 5, actor="Ali")
    assert inventory.low_stock_items(conn) == []


def test_alerts_stay_quiet_when_switched_off(conn, monkeypatch):
    """The default configuration must never try to reach a mail server."""
    from app import config, notifications

    monkeypatch.setattr(config, "ALERTS_ENABLED", False)

    def explode(*args, **kwargs):
        raise AssertionError("tried to send mail while alerts were off")

    monkeypatch.setattr(notifications, "_send", explode)

    item_id = make_consumable(conn, qty=5, reorder=4)
    inventory.stock_out(conn, item_id, 3, actor="Ali")  # drops to 2, well below
    assert models.get_item(conn, item_id)["quantity"] == 2


def test_a_broken_mail_server_does_not_break_a_stock_movement(conn, monkeypatch):
    from app import config, notifications

    monkeypatch.setattr(config, "ALERTS_ENABLED", True)
    monkeypatch.setattr(config, "SMTP_HOST", "smtp.example.com")
    monkeypatch.setattr(config, "ALERT_FROM", "stock@example.com")
    monkeypatch.setattr(config, "ALERT_TO", ["manager@example.com"])

    def explode(*args, **kwargs):
        raise OSError("mail server is down")

    monkeypatch.setattr(notifications, "_send", explode)

    item_id = make_consumable(conn, qty=5, reorder=4)
    inventory.stock_out(conn, item_id, 3, actor="Ali")

    # The movement stands even though the email failed.
    assert models.get_item(conn, item_id)["quantity"] == 2
    assert models.item_history(conn, item_id)[0]["kind"] == "stock_out"


def test_alert_is_sent_once_then_held_by_the_cooldown(conn, monkeypatch):
    from app import config, notifications

    sent: list[str] = []
    monkeypatch.setattr(config, "ALERTS_ENABLED", True)
    monkeypatch.setattr(config, "SMTP_HOST", "smtp.example.com")
    monkeypatch.setattr(config, "ALERT_FROM", "stock@example.com")
    monkeypatch.setattr(config, "ALERT_TO", ["manager@example.com"])
    monkeypatch.setattr(notifications, "_send",
                        lambda subject, body: sent.append(subject))

    item_id = make_consumable(conn, qty=10, reorder=4)
    inventory.stock_out(conn, item_id, 7, actor="Ali")   # 3 left -> alert
    assert len(sent) == 1

    inventory.stock_out(conn, item_id, 1, actor="Ali")   # 2 left -> still quiet
    assert len(sent) == 1

    # Back above the line and down again: that's a new problem, so it alerts.
    inventory.stock_in(conn, item_id, 20, actor="Ali")
    inventory.stock_out(conn, item_id, 20, actor="Ali")
    assert len(sent) == 2
