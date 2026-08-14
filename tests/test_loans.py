"""Borrowing: due dates, returns, overdue detection and reminders."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app import db, inventory, models, overdue


@pytest.fixture()
def conn(tmp_path):
    connection = db.connect(tmp_path / "test.db")
    db.init_db(connection)
    yield connection
    connection.close()


@pytest.fixture()
def sam(conn):
    return models.create_person(conn, "Sam Okafor", "sam@example.com", "Finance")


def days_from_now(days: int) -> str:
    return (date.today() + timedelta(days=days)).isoformat()


def make_asset(conn, name="Latitude 5540", tag="IT-001"):
    return inventory.create_item(conn, kind="asset", name=name, asset_tag=tag,
                                 actor="Ali")


def make_consumable(conn, name="Cat6 patch cable 2m", qty=40, reorder=10):
    return inventory.create_item(conn, kind="consumable", name=name, quantity=qty,
                                 reorder_level=reorder, actor="Ali")


# ---------------------------------------------------------------- assets ----

def test_an_asset_can_go_out_with_a_due_date(conn, sam):
    item_id = make_asset(conn)
    due = days_from_now(14)
    inventory.check_out(conn, item_id, sam, actor="Ali", due_on=due)

    loan = models.open_loan_for_item(conn, item_id)
    assert loan["due_on"] == due
    assert loan["qty"] == 1 and loan["outstanding"] == 1
    assert f"due back {due}" in models.item_history(conn, item_id)[0]["detail"]


def test_an_asset_can_go_out_open_ended(conn, sam):
    """A permanent laptop assignment has no date and can never be overdue."""
    item_id = make_asset(conn)
    inventory.check_out(conn, item_id, sam, actor="Ali")

    loan = models.open_loan_for_item(conn, item_id)
    assert loan["due_on"] is None
    assert models.days_overdue(loan) == 0
    assert models.list_loans(conn, "overdue") == []
    assert "no date agreed" in models.item_history(conn, item_id)[0]["detail"]


def test_checking_an_asset_in_closes_its_loan(conn, sam):
    item_id = make_asset(conn)
    inventory.check_out(conn, item_id, sam, actor="Ali", due_on=days_from_now(7))
    inventory.check_in(conn, item_id, actor="Ali")

    assert models.open_loan_for_item(conn, item_id) is None
    assert len(models.list_loans(conn, "returned")) == 1
    assert models.list_loans(conn, "open") == []


def test_a_late_return_says_how_late_it_was(conn, sam):
    item_id = make_asset(conn)
    inventory.check_out(conn, item_id, sam, actor="Ali", due_on=days_from_now(-3))
    inventory.check_in(conn, item_id, actor="Ali")

    assert "3 days late" in models.item_history(conn, item_id)[0]["detail"]


def test_an_on_time_return_says_nothing_about_lateness(conn, sam):
    item_id = make_asset(conn)
    inventory.check_out(conn, item_id, sam, actor="Ali", due_on=days_from_now(5))
    inventory.check_in(conn, item_id, actor="Ali")

    assert "late" not in models.item_history(conn, item_id)[0]["detail"]


def test_a_nonsense_due_date_is_refused(conn, sam):
    item_id = make_asset(conn)
    with pytest.raises(inventory.StockError, match="isn't a date"):
        inventory.check_out(conn, item_id, sam, actor="Ali", due_on="next tuesday")

    # The refusal left the asset alone -- it did not half-check-out.
    assert models.get_item(conn, item_id)["status"] == "in_stock"
    assert models.list_loans(conn, "open") == []


# ----------------------------------------------------------- consumables ----

def test_lending_takes_stock_down_and_returning_puts_it_back(conn, sam):
    item_id = make_consumable(conn, qty=40)
    inventory.lend(conn, item_id, 6, sam, actor="Ali", due_on=days_from_now(7))

    assert models.get_item(conn, item_id)["quantity"] == 34
    loan = models.list_loans(conn, "open")[0]
    assert loan["outstanding"] == 6

    inventory.return_loan(conn, loan["id"], actor="Ali")
    assert models.get_item(conn, item_id)["quantity"] == 40
    assert models.list_loans(conn, "open") == []


def test_a_loan_can_come_back_in_parts(conn, sam):
    item_id = make_consumable(conn, qty=40)
    loan_id = inventory.lend(conn, item_id, 10, sam, actor="Ali")

    inventory.return_loan(conn, loan_id, qty=6, actor="Ali")

    loan = models.get_loan(conn, loan_id)
    assert loan["returned_qty"] == 6
    assert loan["outstanding"] == 4
    assert loan["returned_at"] is None          # still open
    assert models.get_item(conn, item_id)["quantity"] == 36
    assert "6 of 10 returned" in models.item_history(conn, item_id)[0]["detail"]

    inventory.return_loan(conn, loan_id, qty=4, actor="Ali")
    assert models.get_loan(conn, loan_id)["returned_at"] is not None
    assert models.get_item(conn, item_id)["quantity"] == 40


def test_you_cannot_return_more_than_went_out(conn, sam):
    item_id = make_consumable(conn, qty=40)
    loan_id = inventory.lend(conn, item_id, 5, sam, actor="Ali")

    with pytest.raises(inventory.StockError, match="Only 5 still out"):
        inventory.return_loan(conn, loan_id, qty=9, actor="Ali")

    assert models.get_item(conn, item_id)["quantity"] == 35


def test_you_cannot_lend_more_than_there_is(conn, sam):
    item_id = make_consumable(conn, qty=3)
    with pytest.raises(inventory.StockError, match="can't lend 5"):
        inventory.lend(conn, item_id, 5, sam, actor="Ali")

    assert models.get_item(conn, item_id)["quantity"] == 3
    assert models.list_loans(conn, "open") == []


def test_lending_and_using_up_stay_separate(conn, sam):
    """Taking stock out is consumption; lending is expected back."""
    item_id = make_consumable(conn, qty=40)
    inventory.stock_out(conn, item_id, 10, actor="Ali")
    inventory.lend(conn, item_id, 5, sam, actor="Ali")

    assert models.get_item(conn, item_id)["quantity"] == 25
    # Only the lent units are tracked as coming back.
    assert len(models.list_loans(conn, "open")) == 1
    assert models.list_loans(conn, "open")[0]["outstanding"] == 5


def test_a_returned_loan_cannot_be_returned_twice(conn, sam):
    item_id = make_consumable(conn, qty=10)
    loan_id = inventory.lend(conn, item_id, 2, sam, actor="Ali")
    inventory.return_loan(conn, loan_id, actor="Ali")

    with pytest.raises(inventory.StockError, match="already fully back"):
        inventory.return_loan(conn, loan_id, actor="Ali")
    assert models.get_item(conn, item_id)["quantity"] == 10


# --------------------------------------------------------------- overdue ----

def test_only_dated_loans_past_their_date_are_overdue(conn, sam):
    late = make_consumable(conn, name="Late cables")
    soon = make_consumable(conn, name="Due next week")
    open_ended = make_consumable(conn, name="No date")

    inventory.lend(conn, late, 1, sam, actor="Ali", due_on=days_from_now(-2))
    inventory.lend(conn, soon, 1, sam, actor="Ali", due_on=days_from_now(3))
    inventory.lend(conn, open_ended, 1, sam, actor="Ali")

    overdue_names = [l["item_name"] for l in models.list_loans(conn, "overdue")]
    assert overdue_names == ["Late cables"]

    assert [l["item_name"] for l in models.list_loans(conn, "due_soon")] \
        == ["Due next week"]
    assert models.loan_counts(conn) == {"open": 3, "overdue": 1, "due_soon": 1}


def test_a_loan_due_today_is_not_yet_overdue(conn, sam):
    item_id = make_consumable(conn)
    inventory.lend(conn, item_id, 1, sam, actor="Ali", due_on=days_from_now(0))

    assert models.list_loans(conn, "overdue") == []


def test_extending_a_due_date_is_recorded(conn, sam):
    item_id = make_asset(conn)
    inventory.check_out(conn, item_id, sam, actor="Ali", due_on=days_from_now(-1))
    loan = models.open_loan_for_item(conn, item_id)
    assert len(models.list_loans(conn, "overdue")) == 1

    new_date = days_from_now(7)
    inventory.set_loan_due(conn, loan["id"], new_date, actor="Ali")

    assert models.list_loans(conn, "overdue") == []
    assert "due date" in models.item_history(conn, item_id)[0]["detail"]


# ------------------------------------------------------------- reminders ----

def _enable_mail(monkeypatch, sent):
    from app import config, notifications

    monkeypatch.setattr(config, "ALERTS_ENABLED", True)
    monkeypatch.setattr(config, "SMTP_HOST", "smtp.example.com")
    monkeypatch.setattr(config, "ALERT_FROM", "stock@example.com")
    monkeypatch.setattr(config, "ALERT_TO", ["manager@example.com"])
    monkeypatch.setattr(notifications, "_send",
                        lambda subject, body, to=None: sent.append(
                            {"subject": subject, "body": body, "to": to}))


def test_a_borrower_gets_one_email_listing_all_their_overdue_items(conn, sam,
                                                                   monkeypatch):
    sent: list[dict] = []
    _enable_mail(monkeypatch, sent)

    cables = make_consumable(conn, name="Cat6 patch cable 2m")
    laptop = make_asset(conn, name="Latitude 5540", tag="IT-001")
    inventory.lend(conn, cables, 4, sam, actor="Ali", due_on=days_from_now(-5))
    inventory.check_out(conn, laptop, sam, actor="Ali", due_on=days_from_now(-2))

    report = overdue.send_reminders(conn)

    assert report["overdue"] == 2
    assert report["emailed"] == 1          # one person, one email
    assert len(sent) == 1
    assert sent[0]["to"] == ["sam@example.com"]
    body = sent[0]["body"]
    assert "4 x Cat6 patch cable 2m" in body
    assert "Latitude 5540 (tag IT-001)" in body
    assert "5 days ago" in body and "2 days ago" in body


def test_each_borrower_is_emailed_separately(conn, sam, monkeypatch):
    sent: list[dict] = []
    _enable_mail(monkeypatch, sent)
    jo = models.create_person(conn, "Jo Reyes", "jo@example.com", "Support")

    for person in (sam, jo):
        item_id = make_consumable(conn, name=f"Cables for {person}")
        inventory.lend(conn, item_id, 1, person, actor="Ali",
                       due_on=days_from_now(-1))

    overdue.send_reminders(conn)

    assert sorted(m["to"][0] for m in sent) == ["jo@example.com", "sam@example.com"]


def test_a_borrower_is_not_nagged_twice_in_a_day(conn, sam, monkeypatch):
    sent: list[dict] = []
    _enable_mail(monkeypatch, sent)
    item_id = make_consumable(conn)
    inventory.lend(conn, item_id, 1, sam, actor="Ali", due_on=days_from_now(-1))

    overdue.send_reminders(conn)
    report = overdue.send_reminders(conn)

    assert len(sent) == 1
    assert report["emailed"] == 0
    assert report["skipped"][0]["person"] == "Sam Okafor"


def test_someone_with_no_email_is_reported_not_silently_skipped(conn, monkeypatch):
    sent: list[dict] = []
    _enable_mail(monkeypatch, sent)
    no_email = models.create_person(conn, "Pat Nolan")  # no address on file
    item_id = make_consumable(conn)
    inventory.lend(conn, item_id, 1, no_email, actor="Ali", due_on=days_from_now(-4))

    report = overdue.send_reminders(conn)

    assert sent == []
    assert report["no_email"] == [{"person": "Pat Nolan", "items": 1}]


def test_a_dry_run_sends_nothing(conn, sam, monkeypatch):
    sent: list[dict] = []
    _enable_mail(monkeypatch, sent)
    item_id = make_consumable(conn)
    inventory.lend(conn, item_id, 1, sam, actor="Ali", due_on=days_from_now(-1))

    report = overdue.send_reminders(conn, dry_run=True)

    assert sent == []
    assert report["people"][0]["person"] == "Sam Okafor"
    # Nothing was stamped, so a real run afterwards still sends.
    assert models.list_loans(conn, "overdue")[0]["last_remind_at"] is None


def test_a_broken_mail_server_is_reported_and_does_not_crash(conn, sam, monkeypatch):
    from app import config, notifications

    monkeypatch.setattr(config, "ALERTS_ENABLED", True)
    monkeypatch.setattr(config, "SMTP_HOST", "smtp.example.com")
    monkeypatch.setattr(config, "ALERT_FROM", "stock@example.com")
    monkeypatch.setattr(config, "ALERT_TO", ["manager@example.com"])

    def explode(*args, **kwargs):
        raise OSError("mail server is down")

    monkeypatch.setattr(notifications, "_send", explode)

    item_id = make_consumable(conn)
    inventory.lend(conn, item_id, 1, sam, actor="Ali", due_on=days_from_now(-1))

    report = overdue.send_reminders(conn)

    assert report["emailed"] == 0
    assert "mail server is down" in report["failed"][0]["error"]
    # Not stamped, so the next run tries again.
    assert models.list_loans(conn, "overdue")[0]["last_remind_at"] is None


def test_nothing_overdue_means_no_email(conn, sam, monkeypatch):
    sent: list[dict] = []
    _enable_mail(monkeypatch, sent)
    item_id = make_consumable(conn)
    inventory.lend(conn, item_id, 1, sam, actor="Ali", due_on=days_from_now(30))

    report = overdue.send_reminders(conn)

    assert sent == [] and report["overdue"] == 0
