# IT Room Stock

A small web app for keeping track of what's in the IT room: what's on the
shelf, who has it, and what's running low. Runs on one PC; everyone else
opens it in a browser.

Two kinds of thing are tracked:

- **Assets** — one unit with its own tag, held by at most one person at a time.
  Laptops, monitors, docks. You *check them out* and *check them in*.
- **Consumables** — a pile of identical things you count. Cables, keyboards,
  toner. You *put them in* and *take them out*, and the app flags them when
  they get low.

Every movement is written to a history log that is never edited or deleted.

## Try it in your browser

[![Open in GitHub Codespaces](https://github.com/codespaces/badge.svg)](https://codespaces.new/fleuron1/stock-tracker)

That builds the app in a container and starts it, with an empty database — no
installing anything locally. It takes about half a minute the first time, then
the app opens in a preview tab. Each person who does this gets their own
throwaway copy, so nothing you do in it touches anyone else.

To run it properly on a machine in your own IT room, carry on below.

## Getting started

From this folder, in PowerShell:

```powershell
.\run.ps1
```

The first run builds a virtual environment and installs the dependencies (a
minute or so); after that it just starts. When it's up it prints the addresses
to use — `http://localhost:8000` on this machine, and an
`http://192.168.x.x:8000` address for everyone else.

Stop it with Ctrl+C.

Options:

```powershell
.\run.ps1 -Port 8080     # different port
.\run.ps1 -LocalOnly     # only this machine can reach it
.\run.ps1 -Reload        # restart automatically when the code changes
```

On macOS or Linux, use `./run.sh` instead — same behaviour, with `PORT=8080`
and `LOCAL_ONLY=1` as environment variables.

### Letting colleagues reach it

If the app loads on the host machine but not on anyone else's, Windows
Firewall is blocking the port. Open PowerShell **as administrator** on the
host, once:

```powershell
New-NetFirewallRule -DisplayName "IT Stock app" -Direction Inbound -Protocol TCP -LocalPort 8000 -Action Allow -Profile Private
```

`-Profile Private` keeps it to networks Windows considers private (an office
LAN), not public Wi-Fi. To find the host's address by hand, run `ipconfig` and
use the IPv4 address.

**There is no login.** Anyone who can reach the page can change anything, and
the "done by" box is a signature, not a verified identity. That's fine for a
room where everyone is trusted; it is *not* fine on the open internet, so don't
forward a port to this or put it behind a public DNS name as it stands.

### Starting it automatically when the PC boots

Task Scheduler → Create Task:

- **General:** "Run whether user is logged on or not", "Run with highest privileges".
- **Triggers:** At startup.
- **Actions:** Start a program —
  Program: `powershell.exe`
  Arguments: `-NoProfile -ExecutionPolicy Bypass -File "C:\path\to\stock\run.ps1"`
  Start in: `C:\path\to\stock`

## Day-to-day use

- **The dashboard** is the front page: counts, what's low, what's out, and
  recent activity. Its search box is focused the moment the page loads, so a
  USB barcode scanner works straight away — scan a tag and you land on that
  item's page. From any other page, press `/` to jump to the search box.
- **Inventory** lists everything, with filters.
- **An item's page** is where you move it: check out / check in for an asset,
  put in / take out / stocktake for a consumable, each with a "done by" box.
  Your name is remembered in a cookie so you're not retyping it all day.
- **People** is the staff list — who can be handed an asset. Mark someone
  inactive when they leave; their history stays.
- **History** is the full ledger, filterable, and exportable as it's filtered.

Every entry writes its own description, so the log is readable months later
even when nobody typed anything:

| What | What happened |
|---|---|
| Checked out | `Off the shelf to Sam Okafor (Finance)` |
| Checked in | `Back from Sam Okafor (Finance), sent straight to repair` |
| Status change | `Repaired and back on the shelf, ready to hand out` |
| Retired | `Retired from repair, no longer in service` |
| Stock out | `26 out — 8 left, was 34, at or below the reorder level of 10` |
| Stock in | `50 in — 58 now in stock, was 8, back above the reorder level of 10` |
| Adjusted | `Stocktake: counted 55, was 58 — 3 fewer than recorded` |
| Edited | `Serial: (blank) → SN-77120; Location: Shelf A → Shelf B` |

Anything a person types in a "note" box is kept *alongside* that description
and shown in quotes, never instead of it — so a note can add the reason
("screen flickering") without hiding the facts. An edit that changes nothing
writes no entry at all.

### Low stock

Give a consumable a "tell me when it drops to" level and it turns up on the
dashboard when it hits that number. A level of 0 means never flag it.

### Import / export

The **Import / export** page exports current stock and the full history as
CSV, and loads an existing spreadsheet (saved as CSV).

Only a `name` column is required and column order doesn't matter — common
spellings like `qty`, `tag`, `serial` and `min level` are understood. Rows are
matched to what's already there by asset tag, or by name and category when
there's no tag, so re-importing updates rather than duplicates. If anything in
the file is wrong, **nothing** is imported and you get the line numbers.

The easiest way to see the expected format is to export what you have and edit
that file.

## Emailing a manager when something is low

Built and ready, switched off by default. To turn it on:

1. Copy `.env.example` to `.env` in this folder.
2. Fill in your mail server details and the manager's address.
3. Set `ALERTS_ENABLED=true`.
4. Restart the app and use **Send a test email** on the Import / export page.

An item alerts at most once every `ALERT_COOLDOWN_HOURS` (24 by default), so a
busy afternoon of handing out cables sends one email rather than thirty. If it
climbs back above its level and drops again, that's treated as new and alerts
again. A mail server problem never blocks a stock movement — the movement is
saved and the failure is logged.

## Backups

The entire database is one file: `stock.db`. Copy it somewhere safe on a
schedule and you have everything — items, people, and the whole history. To
restore, put the file back and restart.

A simple scheduled copy:

```powershell
Copy-Item C:\path\to\stock\stock.db "\\backup-server\it\stock-$(Get-Date -Format yyyy-MM-dd).db"
```

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest
```

Covers the stock rules (you can't take out more than there is, you can't check
out something already out, a stocktake records the difference), the low-stock
threshold and its cooldown, and CSV import — including that a file with a bad
row imports nothing at all.

## How it's put together

Python 3.12, FastAPI + Jinja2 templates, SQLite. No JavaScript framework, no
build step — plain HTML forms.

```
app/
  main.py           routes
  inventory.py      the stock rules; the only module that changes stock
  models.py         database queries
  db.py             connection and schema
  csv_io.py         import / export
  notifications.py  low-stock email
  templates/        pages
  static/style.css  the one stylesheet
tests/              pytest
```

The rule worth keeping if you extend this: everything that moves stock goes
through `inventory.py`, which writes the item change and its history row in
one database transaction. That's what stops the log ever drifting out of step
with the shelf.
