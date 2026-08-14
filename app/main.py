"""Routes.

Server-rendered HTML and plain form posts -- no JavaScript framework, no build
step. Every route that changes something redirects afterwards, so a refresh
never repeats an action.
"""

from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager
from typing import Iterator
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import config, csv_io, db, inventory, models, notifications
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

# Remembers who is standing at the shelf, so they aren't re-picking their own
# name off the "done by" list all afternoon.
ACTOR_COOKIE = "stock_actor"
COOKIE_MAX_AGE = 60 * 60 * 24 * 90


def get_conn() -> Iterator[sqlite3.Connection]:
    conn = db.connect()
    try:
        yield conn
    finally:
        conn.close()


def redirect(path: str, msg: str = "", err: str = "", actor: str = "") -> RedirectResponse:
    """Post/redirect/get, carrying a one-line result in the query string."""
    params = {k: v for k, v in (("msg", msg), ("err", err)) if v}
    if params:
        # The path may already carry a query -- returning a loan sends you back
        # to /loans?state=overdue -- so join with & rather than a second ?,
        # which would fold the message into the state value.
        url = f"{path}{'&' if '?' in path else '?'}{urlencode(params)}"
    else:
        url = path
    response = RedirectResponse(url, status_code=303)
    if actor:
        response.set_cookie(ACTOR_COOKIE, actor, max_age=COOKIE_MAX_AGE, samesite="lax")
    return response


def render(request: Request, template: str, **context) -> HTMLResponse:
    return templates.TemplateResponse(
        request, template,
        {
            "msg": request.query_params.get("msg", ""),
            "err": request.query_params.get("err", ""),
            "actor": request.cookies.get(ACTOR_COOKIE, ""),
            **context,
        },
    )


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
def item_new(kind: str = Form("asset"), name: str = Form(""), category: str = Form(""),
             asset_tag: str = Form(""), serial_number: str = Form(""),
             location: str = Form(""), notes: str = Form(""),
             quantity: int = Form(0), reorder_level: int = Form(0),
             actor: str = Form(""), conn: sqlite3.Connection = Depends(get_conn)):
    try:
        item_id = inventory.create_item(
            conn, kind=kind, name=name, category=category, asset_tag=asset_tag,
            serial_number=serial_number, location=location, notes=notes,
            quantity=quantity, reorder_level=reorder_level, actor=actor)
    except inventory.StockError as exc:
        return redirect("/items/new", err=str(exc), actor=actor)
    return redirect(f"/items/{item_id}", msg=f"Added '{name.strip()}'.", actor=actor)


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
def item_edit(item_id: int, name: str = Form(""), category: str = Form(""),
              asset_tag: str = Form(""), serial_number: str = Form(""),
              location: str = Form(""), notes: str = Form(""),
              status: str = Form(""), reorder_level: int = Form(0),
              actor: str = Form(""), conn: sqlite3.Connection = Depends(get_conn)):
    try:
        inventory.update_item(
            conn, item_id, name=name, category=category, asset_tag=asset_tag,
            serial_number=serial_number, location=location, notes=notes,
            status=status, reorder_level=reorder_level, actor=actor)
    except inventory.StockError as exc:
        return redirect(f"/items/{item_id}/edit", err=str(exc), actor=actor)
    return redirect(f"/items/{item_id}", msg="Saved.", actor=actor)


# --------------------------------------------------------- item actions ----

@app.post("/items/{item_id}/checkout")
def item_checkout(item_id: int, person_id: int = Form(0), actor: str = Form(""),
                  note: str = Form(""), due_on: str = Form(""),
                  conn: sqlite3.Connection = Depends(get_conn)):
    try:
        inventory.check_out(conn, item_id, person_id, actor=actor, note=note,
                            due_on=due_on)
    except inventory.StockError as exc:
        return redirect(f"/items/{item_id}", err=str(exc), actor=actor)
    person = models.get_person(conn, person_id)
    due = f" Due back {due_on}." if due_on.strip() else ""
    return redirect(f"/items/{item_id}", msg=f"Checked out to {person['name']}.{due}",
                    actor=actor)


@app.post("/items/{item_id}/lend")
def item_lend(item_id: int, qty: int = Form(0), person_id: int = Form(0),
              actor: str = Form(""), note: str = Form(""), due_on: str = Form(""),
              conn: sqlite3.Connection = Depends(get_conn)):
    try:
        inventory.lend(conn, item_id, qty, person_id, actor=actor, note=note,
                       due_on=due_on)
    except inventory.StockError as exc:
        return redirect(f"/items/{item_id}", err=str(exc), actor=actor)
    person = models.get_person(conn, person_id)
    due = f", due back {due_on}" if due_on.strip() else ""
    return redirect(f"/items/{item_id}", msg=f"Lent {qty} to {person['name']}{due}.",
                    actor=actor)


@app.post("/items/{item_id}/checkin")
def item_checkin(item_id: int, actor: str = Form(""), note: str = Form(""),
                 to_repair: str = Form(""), conn: sqlite3.Connection = Depends(get_conn)):
    try:
        inventory.check_in(conn, item_id, actor=actor, note=note,
                           to_repair=bool(to_repair))
    except inventory.StockError as exc:
        return redirect(f"/items/{item_id}", err=str(exc), actor=actor)
    where = "sent to repair" if to_repair else "back in stock"
    return redirect(f"/items/{item_id}", msg=f"Checked in — {where}.", actor=actor)


@app.post("/items/{item_id}/status")
def item_status(item_id: int, status: str = Form(""), actor: str = Form(""),
                conn: sqlite3.Connection = Depends(get_conn)):
    try:
        inventory.set_status(conn, item_id, status, actor=actor)
    except inventory.StockError as exc:
        return redirect(f"/items/{item_id}", err=str(exc), actor=actor)
    return redirect(f"/items/{item_id}",
                    msg=f"Marked {STATUS_LABELS.get(status, status).lower()}.",
                    actor=actor)


@app.post("/items/{item_id}/retire")
def item_retire(item_id: int, actor: str = Form(""), note: str = Form(""),
                conn: sqlite3.Connection = Depends(get_conn)):
    try:
        inventory.retire_item(conn, item_id, actor=actor, note=note)
    except inventory.StockError as exc:
        return redirect(f"/items/{item_id}", err=str(exc), actor=actor)
    return redirect(f"/items/{item_id}", msg="Retired. Its history is kept.", actor=actor)


@app.post("/items/{item_id}/stock-in")
def item_stock_in(item_id: int, qty: int = Form(0), actor: str = Form(""),
                  note: str = Form(""), conn: sqlite3.Connection = Depends(get_conn)):
    try:
        inventory.stock_in(conn, item_id, qty, actor=actor, note=note)
    except inventory.StockError as exc:
        return redirect(f"/items/{item_id}", err=str(exc), actor=actor)
    return redirect(f"/items/{item_id}", msg=f"Added {qty}.", actor=actor)


@app.post("/items/{item_id}/stock-out")
def item_stock_out(item_id: int, qty: int = Form(0), person_id: int = Form(0),
                   actor: str = Form(""), note: str = Form(""),
                   conn: sqlite3.Connection = Depends(get_conn)):
    try:
        inventory.stock_out(conn, item_id, qty, person_id=person_id or None,
                            actor=actor, note=note)
    except inventory.StockError as exc:
        return redirect(f"/items/{item_id}", err=str(exc), actor=actor)
    return redirect(f"/items/{item_id}", msg=f"Took out {qty}.", actor=actor)


@app.post("/items/{item_id}/adjust")
def item_adjust(item_id: int, new_qty: int = Form(0), actor: str = Form(""),
                note: str = Form(""), conn: sqlite3.Connection = Depends(get_conn)):
    try:
        inventory.set_quantity(conn, item_id, new_qty, actor=actor, note=note)
    except inventory.StockError as exc:
        return redirect(f"/items/{item_id}", err=str(exc), actor=actor)
    return redirect(f"/items/{item_id}", msg=f"Count set to {new_qty}.", actor=actor)


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
def loan_return(loan_id: int, qty: int = Form(0), actor: str = Form(""),
                note: str = Form(""), back_to: str = Form("/loans"),
                conn: sqlite3.Connection = Depends(get_conn)):
    try:
        inventory.return_loan(conn, loan_id, qty=qty or None, actor=actor, note=note)
    except inventory.StockError as exc:
        return redirect(back_to, err=str(exc), actor=actor)
    return redirect(back_to, msg="Returned, thanks.", actor=actor)


@app.post("/loans/{loan_id}/due")
def loan_due(loan_id: int, due_on: str = Form(""), actor: str = Form(""),
             back_to: str = Form("/loans"),
             conn: sqlite3.Connection = Depends(get_conn)):
    """Extend or set a date on a loan that's already out."""
    try:
        inventory.set_loan_due(conn, loan_id, due_on, actor=actor)
    except inventory.StockError as exc:
        return redirect(back_to, err=str(exc), actor=actor)
    return redirect(back_to,
                    msg=f"Due date set to {due_on}." if due_on.strip()
                        else "Due date removed — that loan is now open-ended.",
                    actor=actor)


# --------------------------------------------------------------- people ----

@app.get("/people", response_class=HTMLResponse)
def people_list(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    people = models.list_people(conn, include_inactive=True)
    holdings = {p["id"]: models.person_holdings(conn, p["id"]) for p in people}
    return render(request, "people.html", people=people, holdings=holdings)


@app.post("/people")
def person_add(name: str = Form(""), email: str = Form(""), department: str = Form(""),
               conn: sqlite3.Connection = Depends(get_conn)):
    if not name.strip():
        return redirect("/people", err="A name is needed.")
    models.create_person(conn, name, email, department)
    return redirect("/people", msg=f"Added {name.strip()}.")


@app.post("/people/{person_id}/edit")
def person_edit(person_id: int, name: str = Form(""), email: str = Form(""),
                department: str = Form(""), active: str = Form(""),
                conn: sqlite3.Connection = Depends(get_conn)):
    if not name.strip():
        return redirect("/people", err="A name is needed.")
    models.update_person(conn, person_id, name, email, department, bool(active))
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
def data_import(file: UploadFile, actor: str = Form(""),
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

    summary, errors = csv_io.import_items(conn, text, actor=actor or "csv-import")
    if errors:
        shown = "; ".join(errors[:5])
        if len(errors) > 5:
            shown += f" (and {len(errors) - 5} more)"
        return redirect("/data", err=f"Nothing was imported — {shown}", actor=actor)
    return redirect(
        "/data",
        msg=f"Imported: {summary['created']} added, {summary['updated']} updated,"
            f" {summary['unchanged']} already up to date.",
        actor=actor)


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
