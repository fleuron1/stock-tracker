"""Sign-in, accounts, and who gets recorded against what.

The distinction being protected here: `users` sign in and operate the app,
`people` are who kit is lent to. They are separate lists and neither affects
the other.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import auth, config, db, models

PASSWORD = "shelf-password"


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
        yield test_client


def make_admin(path, username="ali", display_name="Ali"):
    connection = db.connect(path)
    db.init_db(connection)
    user_id = auth.create_user(connection, username, PASSWORD,
                               display_name=display_name, is_admin=True)
    connection.close()
    return user_id


def sign_in(client, username="ali", password=PASSWORD):
    return client.post("/login", data={"username": username, "password": password},
                       follow_redirects=False)


# ------------------------------------------------------------ passwords ----

def test_a_password_survives_a_round_trip_and_a_wrong_one_does_not():
    stored = auth.hash_password("correct horse", rounds=1000)
    assert auth.verify_password("correct horse", stored)
    assert not auth.verify_password("Correct horse", stored)
    assert not auth.verify_password("", stored)


def test_the_password_is_not_stored_anywhere_in_the_clear(conn):
    auth.create_user(conn, "ali", "hunter2-and-more", display_name="Ali")
    row = auth.get_user_by_name(conn, "ali")

    assert "hunter2-and-more" not in row["password_hash"]
    assert row["password_hash"].startswith("pbkdf2_sha256$")


def test_the_same_password_hashes_differently_each_time():
    """Salted, so two people with the same password don't look identical."""
    assert auth.hash_password("same one", rounds=1000) \
        != auth.hash_password("same one", rounds=1000)


def test_a_damaged_hash_never_lets_anyone_in():
    for broken in ("", "rubbish", "pbkdf2_sha256$notanumber$aa$bb", "$$$"):
        assert not auth.verify_password("anything", broken)


def test_obviously_weak_passwords_are_refused(conn):
    for weak in ("short", "password", "12345678"):
        with pytest.raises(auth.AuthError):
            auth.create_user(conn, f"u{weak}", weak)


# ---------------------------------------------------------------- users ----

def test_usernames_are_unique_regardless_of_case(conn):
    auth.create_user(conn, "ali", PASSWORD)
    with pytest.raises(auth.AuthError, match="already a user"):
        auth.create_user(conn, "ALI", PASSWORD)


def test_signing_in_is_case_insensitive_on_the_username(conn):
    auth.create_user(conn, "ali", PASSWORD)
    assert auth.sign_in(conn, "ALI", PASSWORD)


def test_a_wrong_password_says_nothing_about_whether_the_user_exists(conn):
    auth.create_user(conn, "ali", PASSWORD)

    with pytest.raises(auth.AuthError) as wrong_password:
        auth.sign_in(conn, "ali", "not it")
    with pytest.raises(auth.AuthError) as no_such_user:
        auth.sign_in(conn, "nobody", "not it")

    assert str(wrong_password.value) == str(no_such_user.value)


def test_a_switched_off_account_cannot_sign_in(conn):
    user_id = auth.create_user(conn, "ali", PASSWORD, is_admin=True)
    auth.create_user(conn, "sam", PASSWORD, is_admin=True)  # so an admin remains
    auth.update_user(conn, user_id, "Ali", is_admin=True, active=False)

    with pytest.raises(auth.AuthError, match="switched off"):
        auth.sign_in(conn, "ali", PASSWORD)


def test_switching_someone_off_ends_their_session_immediately(conn):
    user_id = auth.create_user(conn, "sam", PASSWORD)
    auth.create_user(conn, "ali", PASSWORD, is_admin=True)
    token = auth.sign_in(conn, "sam", PASSWORD)
    assert auth.user_for_token(conn, token) is not None

    auth.update_user(conn, user_id, "Sam", is_admin=False, active=False)
    assert auth.user_for_token(conn, token) is None


def test_the_last_admin_cannot_lock_everyone_out(conn):
    user_id = auth.create_user(conn, "ali", PASSWORD, is_admin=True)
    auth.create_user(conn, "sam", PASSWORD)  # not an admin

    with pytest.raises(auth.AuthError, match="only admin"):
        auth.update_user(conn, user_id, "Ali", is_admin=False, active=True)
    with pytest.raises(auth.AuthError, match="only admin"):
        auth.update_user(conn, user_id, "Ali", is_admin=True, active=False)

    # With a second admin in place it's allowed.
    auth.create_user(conn, "jo", PASSWORD, is_admin=True)
    auth.update_user(conn, user_id, "Ali", is_admin=False, active=True)
    assert not auth.get_user(conn, user_id)["is_admin"]


def test_changing_a_password_signs_that_account_out_everywhere(conn):
    user_id = auth.create_user(conn, "ali", PASSWORD)
    token = auth.sign_in(conn, "ali", PASSWORD)

    auth.set_password(conn, user_id, "a-brand-new-one")

    assert auth.user_for_token(conn, token) is None
    assert auth.sign_in(conn, "ali", "a-brand-new-one")


# -------------------------------------------------------------- sessions ----

def test_a_session_token_is_not_stored_in_the_database(conn):
    auth.create_user(conn, "ali", PASSWORD)
    token = auth.sign_in(conn, "ali", PASSWORD)

    stored = conn.execute("SELECT token_hash FROM sessions").fetchone()[0]
    assert token not in stored          # only a hash is kept
    assert auth.user_for_token(conn, token) is not None


def test_rubbish_and_empty_tokens_are_rejected(conn):
    auth.create_user(conn, "ali", PASSWORD)
    for bad in ("", "not-a-token", "x" * 60):
        assert auth.user_for_token(conn, bad) is None


def test_signing_out_kills_only_that_session(conn):
    auth.create_user(conn, "ali", PASSWORD)
    laptop = auth.sign_in(conn, "ali", PASSWORD)
    desktop = auth.sign_in(conn, "ali", PASSWORD)

    auth.sign_out(conn, laptop)

    assert auth.user_for_token(conn, laptop) is None
    assert auth.user_for_token(conn, desktop) is not None


def test_an_expired_session_stops_working(conn):
    auth.create_user(conn, "ali", PASSWORD)
    token = auth.sign_in(conn, "ali", PASSWORD)

    conn.execute("UPDATE sessions SET expires_at = '2020-01-01 00:00:00'")
    conn.commit()

    assert auth.user_for_token(conn, token) is None


# ----------------------------------------------------- through the app ----

def test_every_page_needs_signing_in(client, tmp_path):
    make_admin(config.DB_PATH)

    for path in ("/", "/items", "/loans", "/people", "/history", "/data", "/users"):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 303, path
        assert response.headers["location"].startswith("/login"), path


def test_actions_cannot_be_performed_without_signing_in(client):
    make_admin(config.DB_PATH)

    response = client.post("/items/new",
                           data={"kind": "asset", "name": "Sneaky laptop"},
                           follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")

    # And nothing was created.
    sign_in(client)
    assert "Sneaky laptop" not in client.get("/items").text


def test_being_sent_to_login_remembers_where_you_were_going(client):
    make_admin(config.DB_PATH)

    response = client.get("/loans?state=overdue", follow_redirects=False)
    assert "next=" in response.headers["location"]

    signed = client.post("/login",
                         data={"username": "ali", "password": PASSWORD,
                               "next": "/loans?state=overdue"},
                         follow_redirects=False)
    assert signed.headers["location"] == "/loans?state=overdue"


def test_login_will_not_bounce_you_to_another_site(client):
    """An open redirect would make this a handy phishing stepping stone."""
    make_admin(config.DB_PATH)

    response = client.post("/login",
                           data={"username": "ali", "password": PASSWORD,
                                 "next": "https://evil.example.com/"},
                           follow_redirects=False)
    assert response.headers["location"] == "/"


def test_the_setup_page_explains_how_to_make_the_first_admin(client):
    """With no accounts at all, tell people what to run rather than 403."""
    response = client.get("/", follow_redirects=True)
    assert "No accounts yet" in response.text
    assert "python -m app.users add" in response.text


def test_signing_out_ends_access(client):
    make_admin(config.DB_PATH)
    sign_in(client)
    assert client.get("/", follow_redirects=False).status_code == 200

    client.post("/logout", follow_redirects=False)
    assert client.get("/", follow_redirects=False).status_code == 303


def test_only_an_admin_can_reach_the_users_page(client):
    make_admin(config.DB_PATH)
    conn = db.connect(config.DB_PATH)
    auth.create_user(conn, "sam", PASSWORD, display_name="Sam")   # ordinary user
    conn.close()

    sign_in(client, "sam")
    response = client.get("/users", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"].startswith("/?")

    created = client.post("/users", data={"username": "mole", "password": PASSWORD},
                          follow_redirects=False)
    assert created.headers["location"].startswith("/?")

    conn = db.connect(config.DB_PATH)
    assert auth.get_user_by_name(conn, "mole") is None
    conn.close()


def test_an_ordinary_user_can_still_do_everything_with_stock(client):
    """Admin controls users and nothing else -- stock is open to everyone."""
    make_admin(config.DB_PATH)
    conn = db.connect(config.DB_PATH)
    auth.create_user(conn, "sam", PASSWORD, display_name="Sam Okafor")
    conn.close()

    sign_in(client, "sam")
    response = client.post("/items/new",
                           data={"kind": "consumable", "name": "Cable ties",
                                 "quantity": 100},
                           follow_redirects=False)
    assert response.status_code == 303 and "err=" not in response.headers["location"]
    assert "Cable ties" in client.get("/items").text
    # And it's recorded against them.
    assert "Sam Okafor" in client.get("/history").text


def test_users_and_people_are_separate_lists(client):
    """Creating an account must not add anyone to the People list."""
    make_admin(config.DB_PATH)
    sign_in(client)

    client.post("/users", data={"username": "sam", "password": PASSWORD,
                                "display_name": "Sam Okafor"},
                follow_redirects=False)

    conn = db.connect(config.DB_PATH)
    assert [p["name"] for p in models.list_people(conn)] == []
    conn.close()
    assert "Nobody yet" in client.get("/people").text

    # And the reverse: adding a person creates no account.
    client.post("/people", data={"name": "Jo Reyes"}, follow_redirects=False)
    conn = db.connect(config.DB_PATH)
    assert auth.get_user_by_name(conn, "Jo Reyes") is None
    assert len(auth.list_users(conn)) == 2      # ali and sam only
    conn.close()


def test_a_user_can_change_their_own_password_through_the_app(client):
    make_admin(config.DB_PATH)
    sign_in(client)

    response = client.post("/account/password",
                           data={"current": PASSWORD, "new_password": "a-longer-one",
                                 "repeat": "a-longer-one"},
                           follow_redirects=False)
    assert response.status_code == 303

    # Signed out by the change, and the new password works.
    assert client.get("/", follow_redirects=False).status_code == 303
    assert sign_in(client, "ali", "a-longer-one").status_code == 303


def test_the_wrong_current_password_is_refused(client):
    make_admin(config.DB_PATH)
    sign_in(client)

    response = client.post("/account/password",
                           data={"current": "wrong", "new_password": "a-longer-one",
                                 "repeat": "a-longer-one"},
                           follow_redirects=False)
    assert "err=" in response.headers["location"]
    # Still signed in with the original password.
    assert client.get("/", follow_redirects=False).status_code == 200
