"""Dugg CLI — manage the server, users, and database."""

import argparse
import sys

from dugg.db import DuggDB, DEFAULT_DB_PATH


def cmd_serve(args):
    """Run the MCP server."""
    from dugg.server import main
    main()


def cmd_init(args):
    """Initialize the database."""
    from pathlib import Path
    db_path = Path(args.db) if args.db else DEFAULT_DB_PATH
    db = DuggDB(db_path)
    db.close()
    print(f"Database initialized at {db_path}")


def cmd_add_user(args):
    """Create a user and print their API key."""
    from pathlib import Path
    db_path = Path(args.db) if args.db else DEFAULT_DB_PATH
    db = DuggDB(db_path)
    user = db.create_user(args.name)
    db.close()
    print(f"User: {user['name']}")
    print(f"ID:   {user['id']}")
    print(f"Key:  {user['api_key']}")
    print("\nSave this API key — it won't be shown again.")


def cmd_list_users(args):
    """List all users."""
    from pathlib import Path
    db_path = Path(args.db) if args.db else DEFAULT_DB_PATH
    db = DuggDB(db_path)
    rows = db.conn.execute("SELECT id, name, created_at FROM users ORDER BY created_at").fetchall()
    db.close()
    if not rows:
        print("No users.")
        return
    for r in rows:
        print(f"  {r['id']}  {r['name']}  (created {r['created_at']})")


def main():
    parser = argparse.ArgumentParser(prog="dugg", description="Dugg — agentic-first shared knowledge base")
    parser.add_argument("--db", help=f"Database path (default: {DEFAULT_DB_PATH})", default=None)

    sub = parser.add_subparsers(dest="command")

    sub.add_parser("serve", help="Run the MCP server (default)")
    sub.add_parser("init", help="Initialize the database")

    p_user = sub.add_parser("add-user", help="Create a new user")
    p_user.add_argument("name", help="User display name")

    sub.add_parser("list-users", help="List all users")

    args = parser.parse_args()

    if args.command is None or args.command == "serve":
        cmd_serve(args)
    elif args.command == "init":
        cmd_init(args)
    elif args.command == "add-user":
        cmd_add_user(args)
    elif args.command == "list-users":
        cmd_list_users(args)


if __name__ == "__main__":
    main()
