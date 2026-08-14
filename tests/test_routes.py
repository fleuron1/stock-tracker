"""Tests that go through the real HTTP routes.

The other test files call the Python functions directly. These drive the app
the way a browser does -- form posts, redirects, file uploads -- which is the
only way to catch wiring problems: a form field the route doesn't read, a
redirect to the wrong place, or a database connection used from the wrong
thread.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import config


@pytest.fixture()
def client(tmp_path, monkeypatch):
    # Point the app at a throwaway database before it starts up.
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    from app.main import app

    with TestClient(app) as test_client:
        yield test_client


def flash(response) -> tuple[str, str]:
    """The msg / err the app puts in the redirect it sends back."""
    location = response.headers.get("location", "")
    query = location.partition("?")[2]
    from urllib.parse import parse_qs

    params = parse_qs(query)
    return params.get("msg", [""])[0], params.get("err", [""])[0]


def add_person(client, name="Sam Okafor") -> int:
    client.post("/people", data={"name": name}, follow_redirects=False)
    page = client.get("/people").text
    assert name in page
    return 1  # first person added in a fresh database


def add_asset(client, name="Dell Latitude 5540", tag="IT-00123") -> int:
    response = client.post(
        "/items/new",
        data={"kind": "asset", "name": name, "asset_tag": tag,
              "category": "Laptop", "actor": "Ali"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return int(response.headers["location"].split("/items/")[1].split("?")[0])


def add_consumable(client, name="Cat6 patch cable 2m", qty=10, reorder=3) -> int:
    response = client.post(
        "/items/new",
        data={"kind": "consumable", "name": name, "quantity": qty,
              "reorder_level": reorder, "category": "Cables", "actor": "Ali"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return int(response.headers["location"].split("/items/")[1].split("?")[0])


# ----------------------------------------------------------- the basics ----

def test_every_page_loads_on_an_empty_database(client):
    for path in ("/", "/items", "/items/new", "/people", "/history", "/data",
                 "/loans", "/loans?state=overdue", "/loans?state=due_soon",
                 "/loans?state=returned", "/healthz"):
        assert client.get(path).status_code == 200, path


def test_an_asset_can_be_created_checked_out_and_checked_in(client):
    person = add_person(client)
    item_id = add_asset(client)

    page = client.get("/").text
    assert "Dell Latitude 5540" in page

    response = client.post(f"/items/{item_id}/checkout",
                           data={"person_id": person, "actor": "Ali"},
                           follow_redirects=False)
    assert flash(response)[0] == "Checked out to Sam Okafor."

    page = client.get(f"/items/{item_id}").text
    assert "Assigned" in page and "Sam Okafor" in page
    assert "Sam Okafor" in client.get("/").text  # shows in "out right now"

    response = client.post(f"/items/{item_id}/checkin", data={"actor": "Ali"},
                           follow_redirects=False)
    assert "back in stock" in flash(response)[0]
    assert "In stock" in client.get(f"/items/{item_id}").text


def test_checking_out_something_already_out_is_refused_by_the_route(client):
    person = add_person(client)
    item_id = add_asset(client)
    client.post(f"/items/{item_id}/checkout",
                data={"person_id": person, "actor": "Ali"}, follow_redirects=False)

    response = client.post(f"/items/{item_id}/checkout",
                           data={"person_id": person, "actor": "Ali"},
                           follow_redirects=False)
    msg, err = flash(response)
    assert msg == "" and "already checked out" in err


def test_taking_out_more_than_there_is_is_refused_by_the_route(client):
    item_id = add_consumable(client, qty=2)

    response = client.post(f"/items/{item_id}/stock-out",
                           data={"qty": 5, "actor": "Ali"}, follow_redirects=False)
    msg, err = flash(response)
    assert msg == "" and "Only 2" in err
    assert "2</b>" in client.get(f"/items/{item_id}").text.replace(" ", "")\
        or ">2<" in client.get(f"/items/{item_id}").text


def test_the_done_by_name_is_remembered_in_a_cookie(client):
    add_asset(client)
    assert client.cookies.get("stock_actor") == "Ali"


def test_low_stock_reaches_the_dashboard(client):
    item_id = add_consumable(client, qty=10, reorder=3)
    assert "Running low" not in client.get("/").text

    client.post(f"/items/{item_id}/stock-out", data={"qty": 8, "actor": "Ali"},
                follow_redirects=False)
    page = client.get("/").text
    assert "Running low" in page and "Cat6 patch cable 2m" in page


# ---------------------------------------------------------------- loans ----

def _in_days(days: int) -> str:
    from datetime import date, timedelta
    return (date.today() + timedelta(days=days)).isoformat()


def test_lending_a_consumable_and_taking_it_back_through_the_routes(client):
    person = add_person(client)
    item_id = add_consumable(client, qty=40)

    response = client.post(f"/items/{item_id}/lend",
                           data={"qty": 6, "person_id": person, "actor": "Ali",
                                 "due_on": _in_days(7)},
                           follow_redirects=False)
    msg, err = flash(response)
    assert err == "" and "Lent 6 to Sam Okafor" in msg

    page = client.get("/loans").text
    assert "Cat6 patch cable 2m" in page and "Sam Okafor" in page

    loans = client.get("/loans?state=open").text
    assert "open-ended" not in loans          # this one has a date

    # Give it back through the Loans page.
    from app import config, db, models
    conn = db.connect(config.DB_PATH)
    loan_id = models.list_loans(conn, "open")[0]["id"]
    conn.close()

    response = client.post(f"/loans/{loan_id}/return",
                           data={"qty": 6, "actor": "Ali"}, follow_redirects=False)
    assert flash(response)[1] == ""
    assert "Nothing is out on loan" in client.get("/loans").text


def test_returning_from_a_filtered_view_comes_back_to_that_same_view(client):
    """Regression test.

    The redirect helper used to always append '?msg=...', so returning a loan
    from /loans?state=overdue produced '/loans?state=overdue?msg=...'. The
    state read as nonsense, silently fell back to 'open', and the confirmation
    never appeared.
    """
    person = add_person(client)
    item_id = add_consumable(client, qty=10)
    client.post(f"/items/{item_id}/lend",
                data={"qty": 2, "person_id": person, "actor": "Ali",
                      "due_on": _in_days(-1)},
                follow_redirects=False)

    from app import config, db, models
    conn = db.connect(config.DB_PATH)
    loan_id = models.list_loans(conn, "open")[0]["id"]
    conn.close()

    response = client.post(f"/loans/{loan_id}/return",
                           data={"qty": 2, "actor": "Ali",
                                 "back_to": "/loans?state=overdue"},
                           follow_redirects=False)

    location = response.headers["location"]
    assert "state=overdue&" in location or location.endswith("state=overdue")
    assert location.count("?") == 1
    assert flash(response)[0] == "Returned, thanks."


def test_an_overdue_loan_shows_on_the_loans_page_and_the_dashboard(client):
    person = add_person(client)
    item_id = add_asset(client)

    client.post(f"/items/{item_id}/checkout",
                data={"person_id": person, "actor": "Ali", "due_on": _in_days(-3)},
                follow_redirects=False)

    overdue_page = client.get("/loans?state=overdue").text
    assert "Dell Latitude 5540" in overdue_page
    assert "3 days late" in overdue_page

    dashboard = client.get("/").text
    assert "Overdue" in dashboard and "Dell Latitude 5540" in dashboard


def test_an_open_ended_loan_is_never_overdue_through_the_routes(client):
    person = add_person(client)
    item_id = add_asset(client)
    client.post(f"/items/{item_id}/checkout",
                data={"person_id": person, "actor": "Ali", "due_on": ""},
                follow_redirects=False)

    assert "Nothing is overdue" in client.get("/loans?state=overdue").text
    assert "open-ended" in client.get("/loans").text


def test_a_bad_due_date_is_rejected_by_the_route(client):
    person = add_person(client)
    item_id = add_asset(client)

    response = client.post(f"/items/{item_id}/checkout",
                           data={"person_id": person, "actor": "Ali",
                                 "due_on": "whenever"},
                           follow_redirects=False)
    msg, err = flash(response)
    assert msg == "" and "isn't a date" in err
    assert "In stock" in client.get(f"/items/{item_id}").text


# --------------------------------------------------------------- search ----

def test_scanning_an_exact_tag_goes_straight_to_the_item(client):
    item_id = add_asset(client, tag="IT-00123")

    response = client.get("/search", params={"q": "IT-00123"}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == f"/items/{item_id}"

    # Lower case too -- scanners aren't always consistent about case.
    response = client.get("/search", params={"q": "it-00123"}, follow_redirects=False)
    assert response.headers["location"] == f"/items/{item_id}"


def test_a_partial_search_lists_what_matched(client):
    add_asset(client, name="Dell Latitude 5540", tag="IT-1")
    add_asset(client, name="Dell Latitude 7440", tag="IT-2")

    page = client.get("/search", params={"q": "Latitude"}).text
    assert "2 results" in page
    assert "5540" in page and "7440" in page


# ----------------------------------------------------------------- CSV -----

def test_csv_upload_goes_through_the_route(client):
    """Regression test.

    Import is the one route that handles an uploaded file. When it was an
    `async def`, FastAPI opened its database connection in a worker thread and
    used it on the event loop, and every upload died with a 500 that no
    unit test could see.
    """
    csv_text = ("name,kind,category,quantity,reorder_level\n"
                "Blank DVDs,consumable,Media,50,10\n"
                "USB-C dock,asset,Docks,,\n")

    response = client.post(
        "/data/import",
        files={"file": ("stock.csv", csv_text, "text/csv")},
        data={"actor": "Ali"},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    msg, err = flash(response)
    assert err == ""
    assert "2 added" in msg

    page = client.get("/items").text
    assert "Blank DVDs" in page and "USB-C dock" in page


def test_a_bad_upload_reports_the_line_and_imports_nothing(client):
    csv_text = ("name,kind,quantity\n"
                "Good cable,consumable,10\n"
                "Bad cable,consumable,lots\n")

    response = client.post(
        "/data/import",
        files={"file": ("stock.csv", csv_text, "text/csv")},
        data={"actor": "Ali"},
        follow_redirects=False,
    )
    msg, err = flash(response)
    assert msg == ""
    assert "Nothing was imported" in err and "Line 3" in err
    assert "Good cable" not in client.get("/items").text


def test_export_round_trips_through_the_routes(client):
    add_asset(client)
    add_consumable(client)

    exported = client.get("/data/export/items")
    assert exported.status_code == 200
    assert "text/csv" in exported.headers["content-type"]
    assert "attachment" in exported.headers["content-disposition"]

    response = client.post(
        "/data/import",
        files={"file": ("stock-items.csv", exported.text, "text/csv")},
        data={"actor": "Ali"},
        follow_redirects=False,
    )
    msg, err = flash(response)
    assert err == ""
    assert "0 added" in msg and "0 updated" in msg

    assert client.get("/data/export/items").text == exported.text


def test_history_export_follows_the_filters(client):
    person = add_person(client)
    item_id = add_asset(client)
    client.post(f"/items/{item_id}/checkout",
                data={"person_id": person, "actor": "Ali"}, follow_redirects=False)

    everything = client.get("/data/export/history").text
    assert "Checked out" in everything and "Created" in everything

    filtered = client.get("/data/export/history", params={"kind": "check_out"}).text
    assert "Checked out" in filtered and "Created" not in filtered
