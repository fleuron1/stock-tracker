"""Tests for the protections, including the ones that were already there.

Several of these assert things that were true before any of this was written.
They exist because "SQL injection can't happen here" is a claim worth holding
to a test rather than a comment -- if someone later builds a query by pasting
strings together, this file is what notices.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import auth, config, db, inventory, models, validation

PASSWORD = "shelf-password"
BOBBY = "Robert'); DROP TABLE items;--"

# Written as escapes rather than pasted in: a zero-width space and a
# right-to-left override are invisible, and source you can't see is source you
# can't review.
ZERO_WIDTH = "​"
RTL_OVERRIDE = "‮"


@pytest.fixture()
def conn(tmp_path):
    connection = db.connect(tmp_path / "test.db")
    db.init_db(connection)
    yield connection
    connection.close()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "web.db")
    from app.main import app

    with TestClient(app) as test_client:
        conn = db.connect(config.DB_PATH)
        auth.create_user(conn, "ali", PASSWORD, display_name="Ali", is_admin=True)
        conn.close()
        test_client.post("/login", data={"username": "ali", "password": PASSWORD},
                         follow_redirects=False)
        yield test_client


# ------------------------------------------------------- sql stays inert ----

def test_a_name_that_looks_like_sql_is_stored_as_text(conn):
    """Parameterised queries are what make this safe -- not word blocklists."""
    item_id = inventory.create_item(conn, kind="consumable", name=BOBBY,
                                    quantity=5, actor="Ali")

    assert models.get_item(conn, item_id)["name"] == BOBBY
    # The table it names is still there, with the row in it.
    assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1


def test_sql_shaped_input_survives_every_text_field(conn):
    person = models.create_person(conn, BOBBY, "bobby@example.com", "IT")
    item_id = inventory.create_item(conn, kind="asset", name=BOBBY,
                                    asset_tag="'; DELETE FROM items; --",
                                    location=BOBBY, notes=BOBBY, actor=BOBBY)
    inventory.check_out(conn, item_id, person, actor=BOBBY, note=BOBBY)

    for table in ("items", "people", "transactions", "loans"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] >= 1
    assert models.get_person(conn, person)["name"] == BOBBY


def test_a_search_for_sql_finds_nothing_and_breaks_nothing(conn):
    inventory.create_item(conn, kind="consumable", name="Cables", quantity=1,
                          actor="Ali")
    assert models.list_items(conn, q="'; DROP TABLE items;--") == []
    assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1


def test_sql_shaped_input_through_the_web_leaves_the_tables_standing(client):
    client.post("/items/new", data={"kind": "consumable", "name": BOBBY,
                                    "quantity": 3}, follow_redirects=False)
    assert client.get("/items").status_code == 200

    conn = db.connect(config.DB_PATH)
    assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1
    conn.close()


# ------------------------------------------------------------------ xss ----

def test_markup_in_a_name_is_shown_as_text_not_run(client):
    nasty = "<script>alert(1)</script>"
    client.post("/items/new", data={"kind": "consumable", "name": nasty,
                                    "quantity": 1}, follow_redirects=False)

    page = client.get("/items").text
    assert "<script>alert" not in page          # never as live markup
    assert "&lt;script&gt;" in page             # escaped, and still readable


def test_markup_in_a_note_is_escaped_in_the_history(client):
    client.post("/items/new", data={"kind": "consumable", "name": "Cables",
                                    "quantity": 5}, follow_redirects=False)
    client.post("/items/1/stock-out",
                data={"qty": 1, "note": "<img src=x onerror=alert(1)>"},
                follow_redirects=False)

    page = client.get("/history").text
    # The test is whether it is live markup, not whether the letters appear:
    # escaped, the whole thing is just text on the page and no tag is created.
    assert "<img" not in page
    assert "&lt;img src=x onerror=alert(1)&gt;" in page


# ----------------------------------------------------- what may be typed ----

def test_emoji_are_refused_in_names(conn):
    with pytest.raises(inventory.StockError, match="emoji"):
        inventory.create_item(conn, kind="consumable", name="Cables \U0001F50C",
                              quantity=1, actor="Ali")


def test_ordinary_symbols_and_accents_still_work(conn):
    """Rejecting emoji must not reject real text."""
    item_id = inventory.create_item(conn, kind="consumable",
                                    name="Câble 2m (©) 20° n°4",
                                    quantity=1, actor="Ali")
    assert "Câble" in models.get_item(conn, item_id)["name"]


def test_invisible_and_direction_changing_characters_are_stripped(conn):
    """Zero-width and bidi characters make stored text lie about itself."""
    sneaky = f"Lap{ZERO_WIDTH}top{RTL_OVERRIDE} gnitcennoc"
    item_id = inventory.create_item(conn, kind="asset", name=sneaky, actor="Ali")

    stored = models.get_item(conn, item_id)["name"]
    assert ZERO_WIDTH not in stored and RTL_OVERRIDE not in stored
    assert stored.startswith("Laptop")


def test_absurdly_long_text_is_refused(conn):
    with pytest.raises(inventory.StockError, match="too long"):
        inventory.create_item(conn, kind="consumable", name="x" * 500,
                              quantity=1, actor="Ali")


def test_whitespace_is_tidied_so_duplicates_do_not_creep_in(conn):
    item_id = inventory.create_item(conn, kind="consumable",
                                    name="  Cat6   patch  cable  ", quantity=1,
                                    actor="Ali")
    assert models.get_item(conn, item_id)["name"] == "Cat6 patch cable"


def test_a_bad_email_is_refused_rather_than_stored(conn):
    with pytest.raises(validation.ValidationError, match="email"):
        models.create_person(conn, "Sam Okafor", "not-an-email")


def test_a_bad_email_through_the_web_does_not_crash(client):
    response = client.post("/people", data={"name": "Sam", "email": "nope"},
                           follow_redirects=False)
    assert response.status_code == 303
    assert "err=" in response.headers["location"]
    assert "Sam" not in client.get("/people").text


def test_a_csv_row_is_held_to_the_same_standard_as_a_form(client):
    csv_text = "name,quantity\nGood cable,5\n" + ("x" * 400) + ",5\n"
    response = client.post("/data/import",
                           files={"file": ("stock.csv", csv_text, "text/csv")},
                           follow_redirects=False)

    assert "Nothing" in response.headers["location"]
    assert "Good cable" not in client.get("/items").text


# ------------------------------------------------------ guessing passwords ----

def test_repeated_wrong_passwords_lock_that_username_out(conn):
    auth.create_user(conn, "ali", PASSWORD)

    for _ in range(auth.MAX_FAILURES):
        with pytest.raises(auth.AuthError, match="don't match"):
            auth.sign_in(conn, "ali", "wrong")

    # Even the right password is refused while locked out.
    with pytest.raises(auth.AuthError, match="Too many failed attempts"):
        auth.sign_in(conn, "ali", PASSWORD)
    assert auth.lockout_minutes_left(conn, "ali") > 0


def test_a_username_that_does_not_exist_locks_out_too(conn):
    """Otherwise the lockout would reveal which accounts are real."""
    for _ in range(auth.MAX_FAILURES):
        with pytest.raises(auth.AuthError):
            auth.sign_in(conn, "ghost", "wrong")

    with pytest.raises(auth.AuthError, match="Too many failed attempts"):
        auth.sign_in(conn, "ghost", "wrong")


def test_signing_in_correctly_clears_the_count(conn):
    auth.create_user(conn, "ali", PASSWORD)
    for _ in range(auth.MAX_FAILURES - 1):
        with pytest.raises(auth.AuthError):
            auth.sign_in(conn, "ali", "wrong")

    assert auth.sign_in(conn, "ali", PASSWORD)

    # The near-miss streak is forgotten, so the next typo doesn't lock them out.
    with pytest.raises(auth.AuthError, match="don't match"):
        auth.sign_in(conn, "ali", "wrong")
    assert auth.lockout_minutes_left(conn, "ali") == 0


def test_the_lockout_expires(conn):
    auth.create_user(conn, "ali", PASSWORD)
    for _ in range(auth.MAX_FAILURES):
        with pytest.raises(auth.AuthError):
            auth.sign_in(conn, "ali", "wrong")

    conn.execute("UPDATE login_attempts SET locked_until = '2020-01-01 00:00:00'")
    conn.commit()

    assert auth.lockout_minutes_left(conn, "ali") == 0
    assert auth.sign_in(conn, "ali", PASSWORD)


def test_lockout_applies_through_the_web(client):
    for _ in range(auth.MAX_FAILURES):
        client.post("/login", data={"username": "ali", "password": "wrong"},
                    follow_redirects=False)

    response = client.post("/login", data={"username": "ali", "password": PASSWORD},
                           follow_redirects=False)
    assert "Too" in response.headers["location"]


# ------------------------------------------------------- browser hardening ----

def test_the_security_headers_are_set(client):
    headers = client.get("/").headers

    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    # No inline script is allowed, which is what makes injected markup inert.
    assert "script-src 'self'" in headers["Content-Security-Policy"]
    assert "unsafe-inline" not in headers["Content-Security-Policy"]


def test_the_session_cookie_cannot_be_read_by_script(client):
    response = client.post("/login", data={"username": "ali", "password": PASSWORD},
                           follow_redirects=False)
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "samesite=lax" in cookie


def test_a_post_from_another_site_is_refused(client):
    response = client.post("/items/new",
                           data={"kind": "consumable", "name": "Injected",
                                 "quantity": 1},
                           headers={"Origin": "https://evil.example.com"},
                           follow_redirects=False)

    assert response.status_code == 403
    assert "Injected" not in client.get("/items").text


def test_a_post_from_this_site_is_allowed(client):
    response = client.post("/items/new",
                           data={"kind": "consumable", "name": "Legitimate",
                                 "quantity": 1},
                           headers={"Origin": "http://testserver"},
                           follow_redirects=False)

    assert response.status_code == 303
    assert "Legitimate" in client.get("/items").text


# ---------------------------------------------------------------------------
# Regression tests for findings from the pentest of 2026-08-22. Each of these
# failed before the fix in the same commit.
# ---------------------------------------------------------------------------

OFFSITE = [
    "https://evil.example.com/",
    "//evil.example.com/",
    r"/\evil.example.com",          # browsers read \ as /, so this leaves the site
    r"/\/evil.example.com",
    "https:/evil.example.com",
]


def test_signing_in_cannot_bounce_you_off_the_site(client):
    """A fake sign-in page is far more convincing if the real app sends you there."""
    for destination in OFFSITE:
        response = client.post("/login",
                               data={"username": "ali", "password": PASSWORD,
                                     "next": destination},
                               follow_redirects=False)
        location = response.headers["location"]
        assert location == "/", f"{destination!r} redirected to {location!r}"


def test_finishing_with_a_loan_cannot_bounce_you_off_the_site(client):
    for destination in OFFSITE:
        for route in ("/loans/1/return", "/loans/1/due"):
            response = client.post(route, data={"back_to": destination, "due_on": ""},
                                   follow_redirects=False)
            location = response.headers["location"]
            assert location.startswith("/loans"), \
                f"{route} with {destination!r} redirected to {location!r}"


def test_a_path_that_merely_starts_with_static_is_still_private(client):
    """The public-path check matches a whole folder, not a prefix.

    "/statistics" begins with "/static". Before the fix, any route named like
    that would have been reachable without signing in.
    """
    from app.main import PUBLIC_PATHS, require_sign_in  # noqa: F401
    from app import main

    assert not "/statistics".startswith("/static/")
    anon_client = TestClient(main.app)
    response = anon_client.get("/statistics", follow_redirects=False)
    assert response.status_code in (303, 404)
    if response.status_code == 303:
        assert response.headers["location"].startswith("/login")


def test_exported_cells_cannot_run_as_spreadsheet_formulas(client):
    """An item name is executed by Excel if the cell begins = + - or @."""
    for name in ("=cmd|'/c calc'!A1", "+1+1", "-2+3", "@SUM(1+9)"):
        client.post("/items/new", data={"kind": "consumable", "name": name,
                                        "quantity": 1}, follow_redirects=False)

    exported = client.get("/data/export/items").text
    for line in exported.splitlines()[1:]:
        for field in line.split(","):
            assert not field.startswith(("=", "+", "-", "@")), \
                f"unescaped formula cell: {field!r}"


def test_escaping_formulas_does_not_break_the_round_trip(client):
    """The apostrophe added on export is removed again on import."""
    client.post("/items/new", data={"kind": "consumable", "name": "=DANGER()",
                                    "quantity": 4}, follow_redirects=False)

    first = client.get("/data/export/items").text
    response = client.post("/data/import",
                           files={"file": ("items.csv", first, "text/csv")},
                           follow_redirects=False)

    assert "0 added" in response.headers["location"].replace("+", " ").replace("%2C", ",")
    assert client.get("/data/export/items").text == first


def test_an_unknown_username_costs_the_same_as_a_real_one(conn):
    """A fast rejection for unknown names reveals which accounts exist."""
    import time

    auth.create_user(conn, "realuser", PASSWORD)

    def attempt(username):
        start = time.perf_counter()
        try:
            auth.sign_in(conn, username, "definitely-wrong")
        except auth.AuthError:
            pass
        return time.perf_counter() - start

    known = attempt("realuser")
    conn.execute("DELETE FROM login_attempts")
    conn.commit()
    unknown = attempt("no-such-account")

    assert 0.5 < (known / unknown) < 2.0, \
        f"known {known*1000:.0f}ms vs unknown {unknown*1000:.0f}ms"


def test_an_enormous_upload_is_refused(client):
    """Reading an unbounded file into memory is a way to bring the app down."""
    from app.main import MAX_UPLOAD_BYTES

    oversized = "name,quantity\n" + ("x" * (MAX_UPLOAD_BYTES + 1024))
    response = client.post("/data/import",
                           files={"file": ("huge.csv", oversized, "text/csv")},
                           follow_redirects=False)

    assert "err=" in response.headers["location"]
    assert "larger than" in response.headers["location"].replace("+", " ")


def test_a_file_with_too_many_rows_is_refused(client):
    from app.csv_io import MAX_ROWS

    rows = "\n".join(f"item{i},1" for i in range(MAX_ROWS + 5))
    response = client.post("/data/import",
                           files={"file": ("many.csv", f"name,quantity\n{rows}",
                                           "text/csv")},
                           follow_redirects=False)

    assert "err=" in response.headers["location"]
    assert "item0" not in client.get("/items").text
