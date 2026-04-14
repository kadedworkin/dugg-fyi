"""Dugg CLI — manage the server, users, and database."""

import argparse
import sys

from dugg.db import DuggDB, DEFAULT_DB_PATH, DEFAULT_API_KEY


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
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = DuggDB(db_path)

    server_url = getattr(args, "server", None)
    if server_url:
        db.set_config("server_url", server_url.rstrip("/"))
        print(f"Server URL: {server_url.rstrip('/')}")
        print()
        print("DNS setup — point your domain to this server's IP:")
        print(f"  A Record:  your-subdomain → <server IP>")
        print(f"  Or CNAME:  your-subdomain → your-root-domain")
        print()

    # Write .dugg-env so the CLI finds this DB from any user
    env_file = Path.cwd() / ".dugg-env"
    env_file.write_text(f"DUGG_DB_PATH={db_path.resolve()}\n")
    print(f"Config written to {env_file}")

    db.close()
    print(f"Database initialized at {db_path}")


def cmd_set_url(args):
    """Set or update the server's public URL."""
    from pathlib import Path
    db_path = Path(args.db) if args.db else DEFAULT_DB_PATH
    db = DuggDB(db_path)
    db.set_config("server_url", args.url.rstrip("/"))
    db.close()
    print(f"Server URL set to: {args.url.rstrip('/')}")


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

    # Resolve server URL: explicit --server flag > db config > instance endpoint
    server_url = getattr(args, "server", None) or ""
    if not server_url:
        server_url = db.get_config("server_url", "")
    if not server_url:
        instance = db.get_instance_for_owner(user["id"])
        server_url = (instance.get("endpoint_url", "") if instance else "")
    instance = db.get_instance_for_owner(user["id"])
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
    if server_url:
        invite_url = f"{server_url.rstrip('/')}/invite/{token}"
        print(f"\nJoin in your browser: {invite_url}")
        print(f"\nOr via CLI:")
        print(f"  git clone https://github.com/kadedworkin/dugg-fyi.git && cd dugg-fyi && uv sync")
        print(f"  .venv/bin/dugg redeem {token} --server {server_url.rstrip('/')}")
    else:
        print(f"\nVia CLI:")
        print(f"  git clone https://github.com/kadedworkin/dugg-fyi.git && cd dugg-fyi && uv sync")
        print(f"  .venv/bin/dugg redeem {token}")
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


def _resolve_user(db, args):
    """Get user from --key flag > .dugg-env > local user."""
    api_key = getattr(args, "key", None) or DEFAULT_API_KEY
    if api_key:
        user = db.get_user_by_api_key(api_key)
        if not user:
            print("Invalid API key.")
            db.close()
            sys.exit(1)
        return user
    user = db.get_user_by_api_key("dugg_local_default")
    if not user:
        db.conn.execute(
            "INSERT OR IGNORE INTO users (id, name, api_key, created_at) VALUES (?, ?, ?, ?)",
            ("local", "Local User", "dugg_local_default", "2024-01-01T00:00:00Z"),
        )
        db.conn.commit()
        user = db.get_user_by_api_key("dugg_local_default")
    return user


def _ensure_default_collection(db, user_id):
    """Ensure user has a default collection, return its ID."""
    collections = db.list_collections(user_id)
    for c in collections:
        if c["name"] == "Default":
            return c["id"]
    result = db.create_collection("Default", user_id, description="Default collection", visibility="private")
    return result["id"]


def cmd_login(args):
    """Save your API key to .dugg-env so you don't need --key every time."""
    from pathlib import Path
    env_file = Path.cwd() / ".dugg-env"
    existing = {}
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                existing[k.strip()] = v.strip()
    existing["DUGG_API_KEY"] = args.key
    env_file.write_text("\n".join(f"{k}={v}" for k, v in existing.items()) + "\n")

    # Verify the key
    db_path = Path(existing.get("DUGG_DB_PATH", args.db)) if (existing.get("DUGG_DB_PATH") or args.db) else DEFAULT_DB_PATH
    db = DuggDB(db_path)
    user = db.get_user_by_api_key(args.key)
    db.close()
    if user:
        print(f"Logged in as {user['name']}. Key saved to {env_file}")
    else:
        print(f"Key saved to {env_file} (warning: key not found in local DB — may be valid on a remote server)")


def cmd_add(args):
    """Add a URL to Dugg."""
    import asyncio
    from pathlib import Path
    db_path = Path(args.db) if args.db else DEFAULT_DB_PATH
    db = DuggDB(db_path)
    user = _resolve_user(db, args)

    coll_id = _ensure_default_collection(db, user["id"])
    note = getattr(args, "note", "") or ""
    tags = [t.strip() for t in (getattr(args, "tags", "") or "").split(",") if t.strip()]

    print(f"Adding {args.url} ...")

    # Enrich
    try:
        from dugg.enrichment import enrich_url
        enriched = asyncio.run(enrich_url(args.url))
    except Exception:
        enriched = {}

    resource = db.add_resource(
        url=args.url,
        collection_id=coll_id,
        submitted_by=user["id"],
        note=note,
        title=enriched.get("title", ""),
        description=enriched.get("description", ""),
        thumbnail=enriched.get("thumbnail", ""),
        source_type=enriched.get("source_type", "unknown"),
        author=enriched.get("raw_metadata", {}).get("author", ""),
        transcript=enriched.get("transcript", ""),
        raw_metadata=enriched.get("raw_metadata"),
        tags=tags,
        tag_source="human" if tags else "agent",
    )

    title = resource.get("title") or args.url
    print(f"  Added: {title}")
    print(f"  ID: {resource['id']}")
    if enriched.get("transcript"):
        print(f"  Transcript: {len(enriched['transcript'])} chars")
    if note:
        print(f"  Note: {note}")
    db.close()


def cmd_search(args):
    """Search Dugg for resources."""
    from pathlib import Path
    db_path = Path(args.db) if args.db else DEFAULT_DB_PATH
    db = DuggDB(db_path)
    user = _resolve_user(db, args)

    results = db.search(args.query, user["id"], limit=getattr(args, "limit", 20))
    db.close()

    if not results:
        print(f"No results for \"{args.query}\"")
        return

    print(f"{len(results)} result(s) for \"{args.query}\":\n")
    for r in results:
        title = r.get("title") or r["url"]
        print(f"  {title}")
        print(f"    {r['url']}")
        if r.get("note"):
            print(f"    Note: {r['note']}")
        print()


def cmd_feed(args):
    """Show recent resources."""
    from pathlib import Path
    db_path = Path(args.db) if args.db else DEFAULT_DB_PATH
    db = DuggDB(db_path)
    user = _resolve_user(db, args)

    limit = getattr(args, "limit", 20)
    results = db.get_feed(user["id"], limit=limit)
    db.close()

    if not results:
        print("Feed is empty. Add something with: dugg add <url>")
        return

    print(f"Latest {len(results)} resource(s):\n")
    for r in results:
        title = r.get("title") or r["url"]
        date = r.get("created_at", "")[:10]
        print(f"  {title}")
        print(f"    {r['url']}")
        if r.get("note"):
            print(f"    Note: {r['note']}")
        if date:
            print(f"    Added: {date}")
        print()


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
    p_init = sub.add_parser("init", help="Initialize the database")
    p_init.add_argument("--server", default=None, help="Public URL of this server (e.g. https://my-dugg.example.com)")

    p_seturl = sub.add_parser("set-url", help="Set or update the server's public URL")
    p_seturl.add_argument("url", help="Public URL (e.g. https://my-dugg.example.com)")

    p_user = sub.add_parser("add-user", help="Create a new user")
    p_user.add_argument("name", help="User display name")

    p_invite = sub.add_parser("invite-user", help="Create an invite token for a new user")
    p_invite.add_argument("name", help="Name of the person being invited")
    p_invite.add_argument("--key", default=None, help="Your API key (uses local user if omitted)")
    p_invite.add_argument("--server", default=None, help="Server URL (e.g. https://chino-bandido.kadedworkin.com) — included in invite message")
    p_invite.add_argument("--expires", type=int, default=72, help="Hours until invite expires (default: 72)")

    p_redeem = sub.add_parser("redeem", help="Redeem an invite token to join a Dugg server")
    p_redeem.add_argument("token", help="The invite token to redeem")
    p_redeem.add_argument("--name", default=None, help="Your display name (uses invite hint if omitted)")

    sub.add_parser("list-users", help="List all users")

    p_login = sub.add_parser("login", help="Save your API key so you don't need --key every time")
    p_login.add_argument("key", help="Your API key")

    p_add = sub.add_parser("add", help="Add a URL to Dugg")
    p_add.add_argument("url", help="URL to add")
    p_add.add_argument("--note", default="", help="Why this resource matters")
    p_add.add_argument("--tags", default="", help="Comma-separated tags")
    p_add.add_argument("--key", default=None, help="Your API key (uses local user if omitted)")

    p_search = sub.add_parser("search", help="Search Dugg for resources")
    p_search.add_argument("query", help="Search query")
    p_search.add_argument("--limit", type=int, default=20, help="Max results (default: 20)")
    p_search.add_argument("--key", default=None, help="Your API key (uses local user if omitted)")

    p_feed = sub.add_parser("feed", help="Show recent resources")
    p_feed.add_argument("--limit", type=int, default=20, help="Max results (default: 20)")
    p_feed.add_argument("--key", default=None, help="Your API key (uses local user if omitted)")

    p_doctor = sub.add_parser("doctor", help="Check Dugg installation health")
    p_doctor.add_argument("--host", default="127.0.0.1", help="HTTP host to check (default: 127.0.0.1)")
    p_doctor.add_argument("--port", type=int, default=None, help="If set, also check HTTP server reachability")

    p_admin = sub.add_parser("admin", help="Launch admin TUI for ban & appeal management")
    p_admin.add_argument("--key", default=None, help="API key (uses local user if omitted)")

    p_welcome = sub.add_parser("welcome", help="Show orientation info for your Dugg installation")
    p_welcome.add_argument("--key", default=None, help="API key (uses local user if omitted)")

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(0)
    elif args.command == "serve":
        cmd_serve(args)
    elif args.command == "init":
        cmd_init(args)
    elif args.command == "set-url":
        cmd_set_url(args)
    elif args.command == "add-user":
        cmd_add_user(args)
    elif args.command == "invite-user":
        cmd_invite_user(args)
    elif args.command == "redeem":
        cmd_redeem(args)
    elif args.command == "list-users":
        cmd_list_users(args)
    elif args.command == "login":
        cmd_login(args)
    elif args.command == "add":
        cmd_add(args)
    elif args.command == "search":
        cmd_search(args)
    elif args.command == "feed":
        cmd_feed(args)
    elif args.command == "admin":
        cmd_admin(args)
    elif args.command == "doctor":
        cmd_doctor(args)
    elif args.command == "welcome":
        cmd_welcome(args)


if __name__ == "__main__":
    main()
