"""Routes.

Server-rendered HTML and plain form posts -- no JavaScript framework, no build
step. Every route that changes something redirects afterwards, so a refresh
never repeats an action.
"""

from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager
from typing import Iterator
from urllib.parse import urlencode, urlparse

from fastapi import Depends, FastAPI, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import auth, config, csv_io, db, inventory, models, notifications, validation
from .db import STATUS_LABELS, TX_LABELS

@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Create the database on first start, before the first request lands."""
    conn = db.connect()
    try:
        db.init_db(conn)
    finally:
        conn.close()
    yield


app = FastAPI(title="IT Room Stock", docs_url=None, redoc_url=None, lifespan=lifespan)

BASE_DIR = config.PROJECT_ROOT / "app"
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.globals.update(
    status_labels=STATUS_LABELS,
    tx_labels=TX_LABELS,
    app_version=config.PROJECT_ROOT.name,
)

# Pages reachable without signing in. Everything else is closed by default,
# so a route added later is protected whether or not anyone remembers to.
PUBLIC_PATHS = {"/login", "/healthz"}


# Everything the pages need comes from this app: no CDNs, no external images,
# no fonts from elsewhere. Saying so explicitly means that even if hostile
# markup somehow reached a page, the browser would refuse to run it.
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self'; "          # no inline script -- app.js is a file
        "style-src 'self'; "
        "img-src 'self' data:; "       # data: covers the emoji favicon
        "form-action 'self'; "         # forms can only post back here
        "frame-ancestors 'none'; "     # nobody can frame this page
        "base-uri 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
    "Cross-Origin-Opener-Policy": "same-origin",
}


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """Set the headers on the way out, and refuse cross-site posts on the way in.

    The session cookie is already SameSite=Lax, which stops a browser sending
    it with a form posted from another site. This is the belt to that pair of
    braces: a state-changing request that announces it came from somewhere
    else is refused outright.

    A request with no Origin and no Referer is allowed through -- that is a
    script or a curl command, not a browser being tricked, and an attacker
    cannot make someone else's curl carry their cookies.
    """
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        origin = request.headers.get("origin") or request.headers.get("referer")
        if origin:
            host = request.headers.get("host", "")
            if urlparse(origin).netloc != host:
                return PlainTextResponse(
                    "That request looked like it came from another site, so it"
                    " was refused.", status_code=403)

    response = await call_next(request)
    for header, value in SECURITY_HEADERS.items():
        response.headers.setdefault(header, value)
    return response


@app.middleware("http")
async def require_sign_in(request: Request, call_next):
    """Turn away anyone not signed in, and hand routes the signed-in user."""
    path = request.url.path
    if path in PUBLIC_PATHS or path.startswith("/static"):
        return await call_next(request)

    conn = db.connect()
    try:
        user = auth.user_for_token(conn, request.cookies.get(auth.SESSION_COOKIE, ""))
        # Before any account exists there is nobody who could sign in, so send
        # people to a page that explains how to create the first admin.
        no_users = user is None and auth.user_count(conn) == 0
    finally:
        conn.close()

    if user is None:
        if no_users:
            return RedirectResponse("/login?setup=1", status_code=303)
        wanted = request.url.path
        if request.url.query:
            wanted = f"{wanted}?{request.url.query}"
        return RedirectResponse(f"/login?{urlencode({'next': wanted})}",
                                status_code=303)

    request.state.user = dict(user)
    return await call_next(request)


def signed_in(request: Request) -> dict:
    """The signed-in user. Always present -- the middleware guarantees it."""
    return request.state.user


def actor_of(request: Request) -> str:
    """The name recorded in the "done by" column: the signed-in user's."""
    return request.state.user["display_name"]


def require_admin(request: Request) -> None:
    if not request.state.user["is_admin"]:
        raise PermissionError


def get_conn() -> Iterator[sqlite3.Connection]:
    conn = db.connect()
    try:
        yield conn
    finally:
        conn.close()


def redirect(path: str, msg: str = "", err: str = "") -> RedirectResponse:
    """Post/redirect/get, carrying a one-line result in the query string."""
    params = {k: v for k, v in (("msg", msg), ("err", err)) if v}
    if params:
        # The path may already carry a query -- returning a loan sends you back
        # to /loans?state=overdue -- so join with & rather than a second ?,
        # which would fold the message into the state value.
        url = f"{path}{'&' if '?' in path else '?'}{urlencode(params)}"
    else:
        url = path
    return RedirectResponse(url, status_code=303)


def render(request: Request, template: str, **context) -> HTMLResponse:
    return templates.TemplateResponse(
        request, template,
        {
            "msg": request.query_params.get("msg", ""),
            "err": request.query_params.get("err", ""),
            "user": getattr(request.state, "user", None),
            **context,
        },
    )


# --------------------------------------------------------------- signing ----

@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "/", setup: int = 0,
               conn: sqlite3.Connection = Depends(get_conn)):
    if auth.user_for_token(conn, request.cookies.get(auth.SESSION_COOKIE, "")):
        return redirect("/")
    return render(request, "login.html", next=next,
                  needs_setup=bool(setup) or auth.user_count(conn) == 0)


@app.post("/login")
def login(request: Request, username: str = Form(""), password: str = Form(""),
          next: str = Form("/"), conn: sqlite3.Connection = Depends(get_conn)):
    try:
        token = auth.sign_in(conn, username, password)
    except auth.AuthError as exc:
        return redirect(f"/login?{urlencode({'next': next})}", err=str(exc))

    # Only ever bounce back to somewhere inside this app.
    destination = next if next.startswith("/") and not next.startswith("//") else "/"
    response = RedirectResponse(destination, status_code=303)
    response.set_cookie(auth.SESSION_COOKIE, token, httponly=True, samesite="lax",
                        max_age=60 * 60 * 24 * auth.SESSION_DAYS)
    return response


@app.post("/logout")
def logout(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    auth.sign_out(conn, request.cookies.get(auth.SESSION_COOKIE, ""))
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(auth.SESSION_COOKIE)
    return response


@app.get("/account", response_class=HTMLResponse)
def account(request: Request):
    """Where anyone signed in can change their own password."""
    return render(request, "account.html")


@app.post("/account/password")
def account_password(request: Request, current: str = Form(""),
                     new_password: str = Form(""), repeat: str = Form(""),
                     conn: sqlite3.Connection = Depends(get_conn)):
    user = auth.get_user(conn, signed_in(request)["id"])
    if not auth.verify_password(current, user["password_hash"]):
        return redirect("/account", err="Your current password isn't right.")
    if new_password != repeat:
        return redirect("/account", err="The two new passwords don't match.")
    try:
        auth.set_password(conn, user["id"], new_password)
    except auth.AuthError as exc:
        return redirect("/account", err=str(exc))
    # Changing a password ends every session, this one included.
    response = RedirectResponse("/login?msg=Password+changed.+Sign+in+again.",
                                status_code=303)
    response.delete_cookie(auth.SESSION_COOKIE)
    return response


# ----------------------------------------------------------------- users ----
# Admin accounts manage who can sign in, and nothing else: an admin has no
# extra power over stock, and this page has no bearing on the People list.

@app.get("/users", response_class=HTMLResponse)
def users_page(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    if not signed_in(request)["is_admin"]:
        return redirect("/", err="Only an admin can manage sign-in accounts.")
    return render(request, "users.html", users=auth.list_users(conn))


@app.post("/users")
def user_add(request: Request, username: str = Form(""), password: str = Form(""),
             display_name: str = Form(""), is_admin: str = Form(""),
             conn: sqlite3.Connection = Depends(get_conn)):
    if not signed_in(request)["is_admin"]:
        return redirect("/", err="Only an admin can manage sign-in accounts.")
    try:
        auth.create_user(conn, username, password, display_name=display_name,
                         is_admin=bool(is_admin))
    except auth.AuthError as exc:
        return redirect("/users", err=str(exc))
    return redirect("/users", msg=f"Account created for {username.strip()}.")


@app.post("/users/{user_id}/edit")
def user_edit(request: Request, user_id: int, display_name: str = Form(""),
              is_admin: str = Form(""), active: str = Form(""),
              conn: sqlite3.Connection = Depends(get_conn)):
    if not signed_in(request)["is_admin"]:
        return redirect("/", err="Only an admin can manage sign-in accounts.")
    try:
        auth.update_user(conn, user_id, display_name, bool(is_admin), bool(active))
    except auth.AuthError as exc:
        return redirect("/users", err=str(exc))
    return redirect("/users", msg="Saved.")


@app.post("/users/{user_id}/password")
def user_password(request: Request, user_id: int, password: str = Form(""),
                  conn: sqlite3.Connection = Depends(get_conn)):
    if not signed_in(request)["is_admin"]:
        return redirect("/", err="Only an admin can manage sign-in accounts.")
    user = auth.get_user(conn, user_id)
    if user is None:
        return redirect("/users", err="That user no longer exists.")
    try:
        auth.set_password(conn, user_id, password)
    except auth.AuthError as exc:
        return redirect("/users", err=str(exc))
    return redirect("/users",
                    msg=f"New password set for {user['username']}."
                        " They've been signed out everywhere.")


# ------------------------------------------------------------ dashboard ----

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    return render(
        request, "dashboard.html",
        stats=models.stats(conn),
        low_stock=inventory.low_stock_items(conn),
        recent=models.list_transactions(conn, limit=20),
        on_loan=models.list_loans(conn, "open"),
        overdue=models.list_loans(conn, "overdue"),
        loan_counts=models.loan_counts(conn),
        days_overdue=models.days_overdue,
    )


@app.get("/search", response_class=HTMLResponse)
def search(request: Request, q: str = "", conn: sqlite3.Connection = Depends(get_conn)):
    """One box for both typing and scanning.

    A barcode scanner types the tag and presses Enter, so an exact tag match
    jumps straight to the item -- one scan, one item page.
    """
    q = q.strip()
    if not q:
        return redirect("/")

    exact = models.get_item_by_tag(conn, q)
    if exact is not None:
        return redirect(f"/items/{exact['id']}")

    results = models.list_items(conn, q=q)
    if len(results) == 1:
        return redirect(f"/items/{results[0]['id']}")
    return render(request, "search_results.html", q=q, results=results)


# ---------------------------------------------------------------- items ----

@app.get("/items", response_class=HTMLResponse)
def items_list(request: Request, kind: str = "", category: str = "", status: str = "",
               q: str = "", conn: sqlite3.Connection = Depends(get_conn)):
    return render(
        request, "items.html",
        items=models.list_items(conn, kind=kind, category=category, status=status, q=q),
        categories=models.categories(conn),
        f_kind=kind, f_category=category, f_status=status, f_q=q,
    )


@app.get("/items/new", response_class=HTMLResponse)
def item_new_form(request: Request, kind: str = "asset",
                  conn: sqlite3.Connection = Depends(get_conn)):
    return render(request, "item_form.html", item=None,
                  kind=kind if kind in ("asset", "consumable") else "asset",
                  categories=models.categories(conn), people=models.list_people(conn))


@app.post("/items/new")
def item_new(request: Request, kind: str = Form("asset"), name: str = Form(""),
             category: str = Form(""), asset_tag: str = Form(""),
             serial_number: str = Form(""), location: str = Form(""),
             notes: str = Form(""), quantity: int = Form(0),
             reorder_level: int = Form(0),
             conn: sqlite3.Connection = Depends(get_conn)):
    try:
        item_id = inventory.create_item(
            conn, kind=kind, name=name, category=category, asset_tag=asset_tag,
            serial_number=serial_number, location=location, notes=notes,
            quantity=quantity, reorder_level=reorder_level, actor=actor_of(request))
    except inventory.StockError as exc:
        return redirect("/items/new", err=str(exc))
    return redirect(f"/items/{item_id}", msg=f"Added '{name.strip()}'.")


@app.get("/items/{item_id}", response_class=HTMLResponse)
def item_detail(request: Request, item_id: int,
                conn: sqlite3.Connection = Depends(get_conn)):
    item = models.get_item(conn, item_id)
    if item is None:
        return redirect("/items", err="That item doesn't exist.")
    return render(request, "item_detail.html", item=item,
                  history=models.item_history(conn, item_id),
                  people=models.list_people(conn),
                  loans=models.list_loans(conn, "open", item_id=item_id),
                  days_overdue=models.days_overdue)


@app.get("/items/{item_id}/edit", response_class=HTMLResponse)
def item_edit_form(request: Request, item_id: int,
                   conn: sqlite3.Connection = Depends(get_conn)):
    item = models.get_item(conn, item_id)
    if item is None:
        return redirect("/items", err="That item doesn't exist.")
    return render(request, "item_form.html", item=item, kind=item["kind"],
                  categories=models.categories(conn), people=models.list_people(conn))


@app.post("/items/{item_id}/edit")
def item_edit(request: Request, item_id: int, name: str = Form(""),
              category: str = Form(""), asset_tag: str = Form(""),
              serial_number: str = Form(""), location: str = Form(""),
              notes: str = Form(""), status: str = Form(""),
              reorder_level: int = Form(0),
              conn: sqlite3.Connection = Depends(get_conn)):
    try:
        inventory.update_item(
            conn, item_id, name=name, category=category, asset_tag=asset_tag,
            serial_number=serial_number, location=location, notes=notes,
            status=status, reorder_level=reorder_level, actor=actor_of(request))
    except inventory.StockError as exc:
        return redirect(f"/items/{item_id}/edit", err=str(exc))
    return redirect(f"/items/{item_id}", msg="Saved.")


# --------------------------------------------------------- item actions ----

@app.post("/items/{item_id}/checkout")
def item_checkout(request: Request, item_id: int, person_id: int = Form(0),
                  note: str = Form(""), due_on: str = Form(""),
                  conn: sqlite3.Connection = Depends(get_conn)):
    try:
        inventory.check_out(conn, item_id, person_id, actor=actor_of(request),
                            note=note, due_on=due_on)
    except inventory.StockError as exc:
        return redirect(f"/items/{item_id}", err=str(exc))
    person = models.get_person(conn, person_id)
    due = f" Due back {due_on}." if due_on.strip() else ""
    return redirect(f"/items/{item_id}", msg=f"Checked out to {person['name']}.{due}")


@app.post("/items/{item_id}/lend")
def item_lend(request: Request, item_id: int, qty: int = Form(0),
              person_id: int = Form(0), note: str = Form(""),
              due_on: str = Form(""),
              conn: sqlite3.Connection = Depends(get_conn)):
    try:
        inventory.lend(conn, item_id, qty, person_id, actor=actor_of(request),
                       note=note, due_on=due_on)
    except inventory.StockError as exc:
        return redirect(f"/items/{item_id}", err=str(exc))
    person = models.get_person(conn, person_id)
    due = f", due back {due_on}" if due_on.strip() else ""
    return redirect(f"/items/{item_id}", msg=f"Lent {qty} to {person['name']}{due}.")


@app.post("/items/{item_id}/checkin")
def item_checkin(request: Request, item_id: int, note: str = Form(""),
                 to_repair: str = Form(""), conn: sqlite3.Connection = Depends(get_conn)):
    try:
        inventory.check_in(conn, item_id, actor=actor_of(request), note=note,
                           to_repair=bool(to_repair))
    except inventory.StockError as exc:
        return redirect(f"/items/{item_id}", err=str(exc))
    where = "sent to repair" if to_repair else "back in stock"
    return redirect(f"/items/{item_id}", msg=f"Checked in — {where}.")


@app.post("/items/{item_id}/status")
def item_status(request: Request, item_id: int, status: str = Form(""),
                conn: sqlite3.Connection = Depends(get_conn)):
    try:
        inventory.set_status(conn, item_id, status, actor=actor_of(request))
    except inventory.StockError as exc:
        return redirect(f"/items/{item_id}", err=str(exc))
    return redirect(f"/items/{item_id}",
                    msg=f"Marked {STATUS_LABELS.get(status, status).lower()}.")


@app.post("/items/{item_id}/retire")
def item_retire(request: Request, item_id: int, note: str = Form(""),
                conn: sqlite3.Connection = Depends(get_conn)):
    try:
        inventory.retire_item(conn, item_id, actor=actor_of(request), note=note)
    except inventory.StockError as exc:
        return redirect(f"/items/{item_id}", err=str(exc))
    return redirect(f"/items/{item_id}", msg="Retired. Its history is kept.")


@app.post("/items/{item_id}/stock-in")
def item_stock_in(request: Request, item_id: int, qty: int = Form(0),
                  note: str = Form(""), conn: sqlite3.Connection = Depends(get_conn)):
    try:
        inventory.stock_in(conn, item_id, qty, actor=actor_of(request), note=note)
    except inventory.StockError as exc:
        return redirect(f"/items/{item_id}", err=str(exc))
    return redirect(f"/items/{item_id}", msg=f"Added {qty}.")


@app.post("/items/{item_id}/stock-out")
def item_stock_out(request: Request, item_id: int, qty: int = Form(0),
                   person_id: int = Form(0), note: str = Form(""),
                   conn: sqlite3.Connection = Depends(get_conn)):
    try:
        inventory.stock_out(conn, item_id, qty, person_id=person_id or None,
                            actor=actor_of(request), note=note)
    except inventory.StockError as exc:
        return redirect(f"/items/{item_id}", err=str(exc))
    return redirect(f"/items/{item_id}", msg=f"Took out {qty}.")


@app.post("/items/{item_id}/adjust")
def item_adjust(request: Request, item_id: int, new_qty: int = Form(0),
                note: str = Form(""), conn: sqlite3.Connection = Depends(get_conn)):
    try:
        inventory.set_quantity(conn, item_id, new_qty, actor=actor_of(request),
                               note=note)
    except inventory.StockError as exc:
        return redirect(f"/items/{item_id}", err=str(exc))
    return redirect(f"/items/{item_id}", msg=f"Count set to {new_qty}.")


# ---------------------------------------------------------------- loans ----

@app.get("/loans", response_class=HTMLResponse)
def loans(request: Request, state: str = "open", person_id: int = 0,
          conn: sqlite3.Connection = Depends(get_conn)):
    """What's out, with anything late at the top."""
    if state not in ("open", "overdue", "due_soon", "returned", "all"):
        state = "open"
    rows = models.list_loans(conn, state, person_id=person_id or None)
    return render(request, "loans.html", loans=rows, state=state,
                  counts=models.loan_counts(conn), people=models.list_people(conn),
                  f_person=person_id, days_overdue=models.days_overdue)


@app.post("/loans/{loan_id}/return")
def loan_return(request: Request, loan_id: int, qty: int = Form(0),
                note: str = Form(""), back_to: str = Form("/loans"),
                conn: sqlite3.Connection = Depends(get_conn)):
    try:
        inventory.return_loan(conn, loan_id, qty=qty or None,
                              actor=actor_of(request), note=note)
    except inventory.StockError as exc:
        return redirect(back_to, err=str(exc))
    return redirect(back_to, msg="Returned, thanks.")


@app.post("/loans/{loan_id}/due")
def loan_due(request: Request, loan_id: int, due_on: str = Form(""),
             back_to: str = Form("/loans"),
             conn: sqlite3.Connection = Depends(get_conn)):
    """Extend or set a date on a loan that's already out."""
    try:
        inventory.set_loan_due(conn, loan_id, due_on, actor=actor_of(request))
    except inventory.StockError as exc:
        return redirect(back_to, err=str(exc))
    return redirect(back_to,
                    msg=f"Due date set to {due_on}." if due_on.strip()
                        else "Due date removed — that loan is now open-ended.")


# --------------------------------------------------------------- people ----

@app.get("/people", response_class=HTMLResponse)
def people_list(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    people = models.list_people(conn, include_inactive=True)
    holdings = {p["id"]: models.person_holdings(conn, p["id"]) for p in people}
    return render(request, "people.html", people=people, holdings=holdings)


@app.post("/people")
def person_add(name: str = Form(""), email: str = Form(""), department: str = Form(""),
               conn: sqlite3.Connection = Depends(get_conn)):
    try:
        models.create_person(conn, name, email, department)
    except validation.ValidationError as exc:
        return redirect("/people", err=str(exc))
    return redirect("/people", msg=f"Added {name.strip()}.")


@app.post("/people/{person_id}/edit")
def person_edit(person_id: int, name: str = Form(""), email: str = Form(""),
                department: str = Form(""), active: str = Form(""),
                conn: sqlite3.Connection = Depends(get_conn)):
    try:
        models.update_person(conn, person_id, name, email, department, bool(active))
    except validation.ValidationError as exc:
        return redirect("/people", err=str(exc))
    return redirect("/people", msg="Saved.")


# -------------------------------------------------------------- history ----

@app.get("/history", response_class=HTMLResponse)
def history(request: Request, item_id: int = 0, person_id: int = 0, kind: str = "",
            date_from: str = "", date_to: str = "",
            conn: sqlite3.Connection = Depends(get_conn)):
    rows = models.list_transactions(
        conn, item_id=item_id or None, person_id=person_id or None, kind=kind,
        date_from=date_from, date_to=date_to)
    return render(request, "history.html", rows=rows, people=models.list_people(conn),
                  items=models.list_items(conn), f_item=item_id, f_person=person_id,
                  f_kind=kind, f_from=date_from, f_to=date_to,
                  query=urlencode({k: v for k, v in (
                      ("item_id", item_id or ""), ("person_id", person_id or ""),
                      ("kind", kind), ("date_from", date_from), ("date_to", date_to),
                  ) if v}))


# ----------------------------------------------------- import / export -----

@app.get("/data", response_class=HTMLResponse)
def data_page(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    return render(request, "data.html",
                  alert_problem=config.alert_config_problem(),
                  alerts_enabled=config.ALERTS_ENABLED,
                  alert_to=", ".join(config.ALERT_TO),
                  cooldown=config.ALERT_COOLDOWN_HOURS,
                  db_path=str(config.DB_PATH),
                  item_columns=csv_io.ITEM_COLUMNS)


@app.post("/data/import")
def data_import(request: Request, file: UploadFile,
                conn: sqlite3.Connection = Depends(get_conn)):
    # Sync, like every other route here: FastAPI then runs the handler and its
    # database dependency in the same worker thread, which is what SQLite
    # requires. An `async def` here would open the connection in one thread and
    # use it in another.
    raw = file.file.read()
    if not raw:
        return redirect("/data", err="That file was empty.")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        # Spreadsheets saved on Windows are often in the legacy codepage.
        text = raw.decode("cp1252", errors="replace")

    summary, errors = csv_io.import_items(conn, text, actor=actor_of(request))
    if errors:
        shown = "; ".join(errors[:5])
        if len(errors) > 5:
            shown += f" (and {len(errors) - 5} more)"
        return redirect("/data", err=f"Nothing was imported — {shown}")
    return redirect(
        "/data",
        msg=f"Imported: {summary['created']} added, {summary['updated']} updated,"
            f" {summary['unchanged']} already up to date.")


@app.get("/data/export/items")
def export_items(conn: sqlite3.Connection = Depends(get_conn)):
    return Response(
        csv_io.export_items(conn), media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="stock-items.csv"'})


@app.get("/data/export/history")
def export_history(item_id: int = 0, person_id: int = 0, kind: str = "",
                   date_from: str = "", date_to: str = "",
                   conn: sqlite3.Connection = Depends(get_conn)):
    """Exports exactly what the history page is showing, filters and all."""
    rows = models.list_transactions(
        conn, item_id=item_id or None, person_id=person_id or None, kind=kind,
        date_from=date_from, date_to=date_to)
    return Response(
        csv_io.export_history(conn, rows), media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="stock-history.csv"'})


@app.post("/data/test-email")
def data_test_email():
    ok, message = notifications.send_test_email()
    return redirect("/data", msg=message if ok else "", err="" if ok else message)


@app.get("/healthz", response_class=PlainTextResponse)
def healthz():
    return "ok"
