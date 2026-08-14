"""Overdue loan reminders.

Run on a schedule rather than from the app, because overdue items are exactly
the thing nobody is sitting there looking at:

    python -m app.overdue            send reminders
    python -m app.overdue --dry-run  show who would be emailed, send nothing

One email per borrower listing everything of theirs that's late, not one per
item -- someone with four overdue cables should get one message, not four.
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
from collections import defaultdict

from . import config, db, models, notifications


def _summarise(loan: sqlite3.Row) -> str:
    late = models.days_overdue(loan)
    what = loan["item_name"]
    if loan["item_kind"] == "consumable":
        what = f"{loan['outstanding']} x {what}"
    elif loan["asset_tag"]:
        what = f"{what} (tag {loan['asset_tag']})"
    return f"  - {what}, due {loan['due_on']}, {late} day{'' if late == 1 else 's'} ago"


def _message(person_name: str, loans: list[sqlite3.Row]) -> tuple[str, str]:
    count = len(loans)
    subject = (f"[IT stock] {count} overdue item{'' if count == 1 else 's'}"
               f" — {person_name}")
    lines = [
        f"Hello {person_name},",
        "",
        f"Our records say {'this is' if count == 1 else 'these are'} still with"
        f" you and past the agreed date:",
        "",
        *[_summarise(loan) for loan in loans],
        "",
        "Could you drop it back to the IT room, or let us know if you still"
        " need it and we'll extend the date.",
        "",
        "If you've already returned it, reply and we'll correct our records.",
        "",
        "-- Sent automatically by the IT room stock app.",
    ]
    return subject, "\n".join(lines)


def send_reminders(conn: sqlite3.Connection, dry_run: bool = False) -> dict:
    """Email each borrower once about everything of theirs that's overdue.

    Returns a summary for the caller to print, including anyone who couldn't
    be emailed -- those still need chasing by a human, so they must not be
    silently dropped.
    """
    report: dict = {"overdue": 0, "emailed": 0, "people": [], "skipped": [],
                    "no_email": [], "failed": []}

    overdue = models.list_loans(conn, "overdue")
    report["overdue"] = len(overdue)
    if not overdue:
        return report

    by_person: dict[int | None, list[sqlite3.Row]] = defaultdict(list)
    for loan in overdue:
        by_person[loan["person_id"]].append(loan)

    for person_id, loans in by_person.items():
        name = loans[0]["person_name"] or "someone no longer on the People list"
        email = loans[0]["person_email"]

        if not email:
            # Nobody to write to. Surfaced rather than skipped quietly, so it
            # can be chased in person.
            report["no_email"].append({"person": name, "items": len(loans)})
            continue

        # Don't re-nag about loans that were emailed recently. A borrower with
        # one fresh overdue item and one already-nagged one still gets a note
        # about the fresh one.
        fresh = [l for l in loans
                 if not notifications.within_cooldown(
                     l["last_remind_at"], config.REMINDER_COOLDOWN_HOURS)]
        if not fresh:
            report["skipped"].append({"person": name, "items": len(loans)})
            continue

        subject, body = _message(name, fresh)
        if dry_run:
            report["people"].append({"person": name, "email": email,
                                     "items": len(fresh), "subject": subject})
            continue

        try:
            notifications.send_email(subject, body, to=[email])
        except Exception as exc:  # noqa: BLE001 -- reported, never fatal
            report["failed"].append({"person": name, "error": str(exc)})
            continue

        conn.executemany(
            "UPDATE loans SET last_remind_at = ? WHERE id = ?",
            [(db.now(), loan["id"]) for loan in fresh])
        conn.commit()
        report["emailed"] += 1
        report["people"].append({"person": name, "email": email,
                                 "items": len(fresh)})

    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Email borrowers about overdue items.")
    parser.add_argument("--dry-run", action="store_true",
                        help="show who would be emailed without sending anything")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    problem = config.alert_config_problem()
    if problem and not args.dry_run:
        print(f"Not sending: {problem}")
        print("Overdue items are still listed at /loans in the app.")
        return 1

    conn = db.connect()
    try:
        db.init_db(conn)
        report = send_reminders(conn, dry_run=args.dry_run)
    finally:
        conn.close()

    print(f"{report['overdue']} overdue loan(s).")
    for entry in report["people"]:
        verb = "would email" if args.dry_run else "emailed"
        print(f"  {verb} {entry['person']} <{entry['email']}>"
              f" about {entry['items']} item(s)")
    for entry in report["skipped"]:
        print(f"  skipped {entry['person']} — reminded recently"
              f" ({entry['items']} item(s))")
    for entry in report["no_email"]:
        print(f"  NO EMAIL ADDRESS for {entry['person']}"
              f" — {entry['items']} item(s) need chasing by hand")
    for entry in report["failed"]:
        print(f"  FAILED for {entry['person']}: {entry['error']}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
