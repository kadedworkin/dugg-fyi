"""Dugg CLI — manage the server, users, and database."""

import argparse
import sys

from dugg.db import DuggDB, DEFAULT_DB_PATH


def cmd_serve(args):
    """Run the MCP server."""
    transport = getattr(args, "transport", "stdio")
    if transport == "http":
        from dugg.http import run_http
        from pathlib import Path
        db_path = Path(args.db) if args.db else None
        host = getattr(args, "host", "0.0.0.0")
        port = getattr(args, "port", 8411)
        run_http(host=host, port=port, db_path=db_path)
    else:
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


def cmd_invite_user(args):
    """Create an invite token for a new user."""
    from pathlib import Path
    db_path = Path(args.db) if args.db else DEFAULT_DB_PATH
    db = DuggDB(db_path)

    # Resolve the inviter — use api_key if given, else local user
    api_key = getattr(args, "key", None)
    if api_key:
        user = db.get_user_by_api_key(api_key)
        if not user:
            print("Invalid API key.")
            db.close()
            sys.exit(1)
    else:
        user = db.get_user_by_api_key("dugg_local_default")
        if not user:
            db.conn.execute(
                "INSERT OR IGNORE INTO users (id, name, api_key, created_at) VALUES (?, ?, ?, ?)",
                ("local", "Local User", "dugg_local_default", "2024-01-01T00:00:00Z"),
            )
            db.conn.commit()
            user = db.get_user_by_api_key("dugg_local_default")

    expires = getattr(args, "expires", 72)
    result = db.create_invite_token(user["id"], name_hint=args.name, expires_hours=expires)
    token = result["token"]

    # Check for endpoint URL
    instance = db.get_instance_for_owner(user["id"])
    endpoint = instance.get("endpoint_url", "") if instance else ""
    instance_name = instance.get("name", "a Dugg server") if instance else "a Dugg server"
    instance_topic = instance.get("topic", "") if instance else ""

    print(f"Invite created for {args.name}")
    print(f"Token: {token}")
    print(f"Expires: {result['expires_at']}")
    print()

    print("--- Send this to them ---")
    print()
    print(f"{user['name']} invited you to {instance_name}!")
    if instance_topic:
        print(instance_topic)
    if endpoint:
        print(f"\nGet set up here: {endpoint.rstrip('/')}/invite/{token}")
    else:
        print(f"\nRedeem via CLI: dugg redeem {token}")
    print(f"\nThis invite expires in {expires} hours.")
    print()
    print("--- End ---")
    db.close()


def cmd_redeem(args):
    """Redeem an invite token to create a user account."""
    from pathlib import Path
    db_path = Path(args.db) if args.db else DEFAULT_DB_PATH
    db = DuggDB(db_path)

    name = getattr(args, "name", None)
    if not name:
        # Fall back to the name hint from the invite
        invite = db.get_invite_token(args.token)
        name = invite["name_hint"] if invite and invite.get("name_hint") else "New User"
    result = db.redeem_invite_token(args.token, name)
    db.close()

    if not result:
        print("Invalid, expired, or already-redeemed invite token.")
        sys.exit(1)

    user = result["user"]
    agent = result["agent"]
    print(f"Welcome to Dugg, {user['name']}!")
    print(f"ID:        {user['id']}")
    print(f"Your key:  {user['api_key']}")
    print(f"Agent key: {agent['api_key']}")
    print()
    print("Save both keys — they won't be shown again.")
    print("If your account gets banned, your agent goes too.")
    print()
    print("What next?")
    print("  • Got an AI agent? Give it the agent key with X-Dugg-Key")
    print("  • Use the CLI?    dugg welcome --key <your-key>")


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


def cmd_welcome(args):
    """Show orientation info for the current Dugg installation."""
    from pathlib import Path
    db_path = Path(args.db) if args.db else DEFAULT_DB_PATH

    if not db_path.exists():
        print("Database not found. Run: dugg init")
        sys.exit(1)

    db = DuggDB(db_path)

    # Resolve user
    api_key = getattr(args, "key", None)
    if api_key:
        user = db.get_user_by_api_key(api_key)
        if not user:
            print("Invalid API key.")
            db.close()
            sys.exit(1)
    else:
        # Local user
        user = db.get_user_by_api_key("dugg_local_default")
        if not user:
            db.conn.execute(
                "INSERT OR IGNORE INTO users (id, name, api_key, created_at) VALUES (?, ?, ?, ?)",
                ("local", "Local User", "dugg_local_default", "2024-01-01T00:00:00Z"),
            )
            db.conn.commit()
            user = db.get_user_by_api_key("dugg_local_default")

    user_id = user["id"]
    print(f"Welcome to Dugg, {user['name']}!\n")

    # Instances
    instances = db.list_instances(user_id)
    if instances:
        print("Instances:")
        for inst in instances:
            mode = f" [{inst['access_mode']}]" if inst.get("access_mode") else ""
            topic = f" — {inst['topic']}" if inst.get("topic") else ""
            print(f"  - {inst['name']}{mode}{topic}")
        print()
    else:
        print("No instances. Running in local mode.\n")

    # Collections
    collections = db.list_collections(user_id)
    total = 0
    if collections:
        print(f"Collections: {len(collections)}")
        for c in collections:
            count = db.conn.execute(
                "SELECT COUNT(*) FROM resources WHERE collection_id = ?", (c["id"],)
            ).fetchone()[0]
            total += count
            print(f"  - {c['name']} ({count} resources)")
        print()
    else:
        print("No collections yet.\n")

    print(f"Total resources: {total}\n")

    # Recent feed
    feed = db.get_feed(user_id, limit=3)
    if feed:
        print("Recent activity:")
        for r in feed:
            title = r.get("title") or r["url"]
            print(f"  - {title}")
            print(f"    {r['url']}")
        print()
    else:
        print("No resources yet. Add one with: dugg_add(url=\"...\", note=\"...\")\n")

    # Rate limits
    rate_lines = []
    for c in collections:
        status = db.check_rate_limit(c["id"], user_id)
        if status["cap"] != -1:
            rate_lines.append(f"  - {c['name']}: {status['current']}/{status['cap']} posts today (member {status['days_member']}d)")
    if rate_lines:
        print("Rate limits:")
        for line in rate_lines:
            print(line)
        print()

    # Tip
    if total == 0:
        print("Get started: share a link with dugg_add(url=\"...\", note=\"why this matters\")")
    elif not instances:
        print("Tip: create an instance with dugg_instance_create to start publishing.")
    else:
        print("Tip: search with dugg_search(query=\"...\") or browse with dugg_feed().")

    db.close()


def cmd_admin(args):
    """Launch the admin TUI for ban & appeal management."""
    from pathlib import Path
    from dugg.tui import run_tui
    db_path = Path(args.db) if args.db else DEFAULT_DB_PATH
    api_key = getattr(args, "key", None)
    run_tui(db_path=db_path, api_key=api_key)


def cmd_doctor(args):
    """Run health checks on the Dugg installation."""
    from pathlib import Path
    issues = []
    ok = []

    db_path = Path(args.db) if args.db else DEFAULT_DB_PATH

    # Check 1: Database exists
    if db_path.exists():
        ok.append(f"Database exists at {db_path}")
    else:
        issues.append(f"Database not found at {db_path} — run: dugg init")
        print("\n".join(f"  ✗ {i}" for i in issues))
        sys.exit(1)

    # Check 2: Schema tables
    db = DuggDB(db_path)
    expected_tables = ["users", "collections", "resources", "tags", "share_rules",
                       "dugg_instances", "instance_subscribers", "collection_members",
                       "resource_edges", "publish_targets", "reactions",
                       "publish_queue", "event_log", "webhook_subscriptions"]
    existing = {r[0] for r in db.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    missing = [t for t in expected_tables if t not in existing]
    if missing:
        issues.append(f"Missing tables: {', '.join(missing)} — DB may be from an older version")
    else:
        ok.append(f"Schema OK ({len(expected_tables)} tables)")

    # Check 3: FTS5 index
    fts_tables = {r[0] for r in db.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%_fts%'").fetchall()}
    if fts_tables:
        ok.append(f"FTS5 index present ({', '.join(sorted(fts_tables))})")
    else:
        issues.append("No FTS5 index found — full-text search won't work")

    # Check 4: Users exist
    user_count = db.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    if user_count > 0:
        ok.append(f"{user_count} user(s) found")
    else:
        issues.append("No users — run: dugg add-user \"YourName\" (or use stdio mode for auto-created local user)")

    # Check 5: Resources (informational)
    res_count = db.conn.execute("SELECT COUNT(*) FROM resources").fetchone()[0]
    ok.append(f"{res_count} resource(s) stored")

    # Check 6: HTTP mode connectivity (if --port given)
    port = getattr(args, "port", None)
    if port:
        import urllib.request
        import urllib.error
        try:
            host = getattr(args, "host", "127.0.0.1")
            req = urllib.request.urlopen(f"http://{host}:{port}/health", timeout=3)
            ok.append(f"HTTP server reachable at {host}:{port}")
        except Exception as e:
            issues.append(f"HTTP server not reachable at {host}:{port} — {e}")

    db.close()

    # Report
    print("Dugg Doctor\n")
    for line in ok:
        print(f"  ✓ {line}")
    for line in issues:
        print(f"  ✗ {line}")
    print()
    if issues:
        print(f"{len(issues)} issue(s) found.")
        sys.exit(1)
    else:
        print("All checks passed.")


def main():
    parser = argparse.ArgumentParser(prog="dugg", description="Dugg — agentic-first shared knowledge base")
    parser.add_argument("--db", help=f"Database path (default: {DEFAULT_DB_PATH})", default=None)

    sub = parser.add_subparsers(dest="command")

    p_serve = sub.add_parser("serve", help="Run the MCP server (default)")
    p_serve.add_argument("--transport", choices=["stdio", "http"], default="stdio",
                         help="Transport mode: stdio (local agent) or http (remote HTTP/SSE)")
    p_serve.add_argument("--host", default="0.0.0.0", help="HTTP bind address (default: 0.0.0.0)")
    p_serve.add_argument("--port", type=int, default=8411, help="HTTP port (default: 8411)")
    sub.add_parser("init", help="Initialize the database")

    p_user = sub.add_parser("add-user", help="Create a new user")
    p_user.add_argument("name", help="User display name")

    p_invite = sub.add_parser("invite-user", help="Create an invite token for a new user")
    p_invite.add_argument("name", help="Name of the person being invited")
    p_invite.add_argument("--key", default=None, help="Your API key (uses local user if omitted)")
    p_invite.add_argument("--expires", type=int, default=72, help="Hours until invite expires (default: 72)")

    p_redeem = sub.add_parser("redeem", help="Redeem an invite token to join a Dugg server")
    p_redeem.add_argument("token", help="The invite token to redeem")
    p_redeem.add_argument("--name", default=None, help="Your display name (uses invite hint if omitted)")

    sub.add_parser("list-users", help="List all users")

    p_doctor = sub.add_parser("doctor", help="Check Dugg installation health")
    p_doctor.add_argument("--host", default="127.0.0.1", help="HTTP host to check (default: 127.0.0.1)")
    p_doctor.add_argument("--port", type=int, default=None, help="If set, also check HTTP server reachability")

    p_admin = sub.add_parser("admin", help="Launch admin TUI for ban & appeal management")
    p_admin.add_argument("--key", default=None, help="API key (uses local user if omitted)")

    p_welcome = sub.add_parser("welcome", help="Show orientation info for your Dugg installation")
    p_welcome.add_argument("--key", default=None, help="API key (uses local user if omitted)")

    args = parser.parse_args()

    if args.command is None or args.command == "serve":
        cmd_serve(args)
    elif args.command == "init":
        cmd_init(args)
    elif args.command == "add-user":
        cmd_add_user(args)
    elif args.command == "invite-user":
        cmd_invite_user(args)
    elif args.command == "redeem":
        cmd_redeem(args)
    elif args.command == "list-users":
        cmd_list_users(args)
    elif args.command == "admin":
        cmd_admin(args)
    elif args.command == "doctor":
        cmd_doctor(args)
    elif args.command == "welcome":
        cmd_welcome(args)


if __name__ == "__main__":
    main()
