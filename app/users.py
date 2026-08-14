"""Account management from the command line.

Mainly for creating the very first admin, before anyone can sign in at all,
and for rescuing yourself if the admin password is lost. Day to day, accounts
are managed on the Users page in the app.

    python -m app.users add alice --admin
    python -m app.users list
    python -m app.users passwd alice

Passwords are asked for at the prompt so they don't end up in your shell
history. --password is there for scripting and is best avoided otherwise.
"""

from __future__ import annotations

import argparse
import getpass
import sqlite3
import sys

from . import auth, db


def _ask_password(prompt: str = "Password: ") -> str:
    first = getpass.getpass(prompt)
    if first != getpass.getpass("Repeat it: "):
        raise auth.AuthError("Those didn't match.")
    return first


def cmd_add(conn: sqlite3.Connection, args) -> int:
    password = args.password or _ask_password()
    user_id = auth.create_user(conn, args.username, password,
                               display_name=args.name or "", is_admin=args.admin)
    role = "admin" if args.admin else "user"
    print(f"Created {role} '{args.username}' (id {user_id}).")
    return 0


def cmd_list(conn: sqlite3.Connection, args) -> int:
    users = auth.list_users(conn)
    if not users:
        print("No accounts yet. Create the first admin with:")
        print("  python -m app.users add <username> --admin")
        return 0
    print(f"{'username':<20} {'name':<24} {'role':<7} {'state':<9} last signed in")
    for user in users:
        print(f"{user['username']:<20} {user['display_name']:<24}"
              f" {'admin' if user['is_admin'] else 'user':<7}"
              f" {'active' if user['active'] else 'disabled':<9}"
              f" {user['last_login_at'] or 'never'}")
    return 0


def cmd_passwd(conn: sqlite3.Connection, args) -> int:
    user = auth.get_user_by_name(conn, args.username)
    if user is None:
        print(f"No user called '{args.username}'.")
        return 1
    password = args.password or _ask_password("New password: ")
    auth.set_password(conn, user["id"], password)
    print(f"Password changed for '{user['username']}'."
          " Any signed-in sessions for that account have been ended.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.users", description="Manage sign-in accounts.")
    sub = parser.add_subparsers(dest="command", required=True)

    add = sub.add_parser("add", help="create an account")
    add.add_argument("username")
    add.add_argument("--name", default="", help="display name shown in history")
    add.add_argument("--admin", action="store_true", help="can manage other users")
    add.add_argument("--password", default="", help="avoid; prompts if omitted")
    add.set_defaults(func=cmd_add)

    listing = sub.add_parser("list", help="show all accounts")
    listing.set_defaults(func=cmd_list)

    passwd = sub.add_parser("passwd", help="set someone's password")
    passwd.add_argument("username")
    passwd.add_argument("--password", default="", help="avoid; prompts if omitted")
    passwd.set_defaults(func=cmd_passwd)

    args = parser.parse_args(argv)

    conn = db.connect()
    try:
        db.init_db(conn)
        return args.func(conn, args)
    except auth.AuthError as exc:
        print(f"{exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
