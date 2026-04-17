"""Dugg CLI — manage the server, users, and database."""

import argparse
import json
import sqlite3
import sys
from typing import Optional

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


def cmd_set_config(args):
    """Set a server config value."""
    from pathlib import Path
    db_path = Path(args.db) if args.db else DEFAULT_DB_PATH
    db = DuggDB(db_path)
    db.set_config(args.key, args.value)
    db.close()
    print(f"Config set: {args.key} = {args.value}")


def cmd_enable_shared_default(args):
    """Mark a collection as the server's shared default. All existing users are
    auto-added as members, and new invite-redeemers will be added automatically."""
    from pathlib import Path
    db_path = Path(args.db) if args.db else DEFAULT_DB_PATH
    db = DuggDB(db_path)
    try:
        db.enable_shared_default(args.collection_id)
    except ValueError as e:
        print(f"Error: {e}")
        db.close()
        sys.exit(1)
    added = 0
    for user in db.conn.execute("SELECT id FROM users").fetchall():
        before = db.conn.execute(
            "SELECT 1 FROM collection_members WHERE collection_id = ? AND user_id = ?",
            (args.collection_id, user[0]),
        ).fetchone()
        if not before:
            db.add_collection_member(args.collection_id, user[0], role="member")
            added += 1
    db.close()
    print(f"Shared-default collection set: {args.collection_id}")
    print(f"Auto-added {added} existing user(s) as members.")


def cmd_add_user(args):
    """Create a user and print their API key."""
    server = getattr(args, "server", None)
    if server:
        import urllib.request
        api_key = getattr(args, "key", None)
        if not api_key:
            env_file = _find_env_file()
            try:
                if env_file.exists():
                    for line in env_file.read_text().splitlines():
                        if line.startswith("DUGG_API_KEY="):
                            api_key = line.split("=", 1)[1].strip()
                            break
            except (OSError, PermissionError):
                pass
        if not api_key:
            print("No API key found. Either run 'dugg login <key>' first or pass --key.")
            sys.exit(1)
        url = f"{server.rstrip('/')}/tools/dugg_create_user"
        data = json.dumps({"name": args.name}).encode()
        req = urllib.request.Request(
            url, data=data,
            headers={"X-Dugg-Key": api_key, "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req) as resp:
                result = json.loads(resp.read())
                print(result.get("result", result))
        except urllib.error.HTTPError as e:
            body = e.read().decode()
            print(f"Server error ({e.code}): {body}")
            sys.exit(1)
        except urllib.error.URLError as e:
            print(f"Connection failed: {e.reason}")
            sys.exit(1)
        return

    from pathlib import Path
    db_path = Path(args.db) if args.db else DEFAULT_DB_PATH
    db = DuggDB(db_path)
    user = db.create_user(args.name)
    server_url = db.get_config("server_url", "")
    db.close()
    print(f"User: {user['name']}")
    print(f"ID:   {user['id']}")
    print(f"Key:  {user['api_key']}")
    from dugg.db import dugg_email_address
    email_addr = dugg_email_address(user["api_key"], server_url)
    if email_addr:
        print(f"Email: {email_addr}")
    print("\nSave this API key — it won't be shown again.")


def cmd_invite_user(args):
    """Create an invite token for a new user."""
    from pathlib import Path
    db_path = Path(args.db) if args.db else DEFAULT_DB_PATH
    db = DuggDB(db_path)

    user = _resolve_user(db, args)

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

    if not server_url:
        print("⚠  No server URL configured. Invites require HTTP mode for browser redemption.")
        print("   Set one with: dugg set-config server_url https://your-host:8411")
        print("   Or pass --server https://your-host:8411 to this command.")
        print()

    print(f"Invite created for {args.name}")
    print(f"Token: {token}")
    print(f"Expires: {result['expires_at']}")
    print()

    print("--- Send this to them ---")
    print()
    print(f"{user['name']} invited you to {instance_name}!")
    if instance_topic:
        print(instance_topic)
    print(f"\nThis invite expires in {expires} hours.")
    if server_url:
        invite_url = f"{server_url.rstrip('/')}/invite/{token}"
        print()
        print("--- For humans ---")
        print()
        print(invite_url)
        print()
        print("Click the link, enter your name, and you're in.")
        print()
        print("--- For agents ---")
        print()
        print("Hand your agent this entire message. It covers the full setup.")
        print()
        print(f"1. Set up your local Dugg")
        print()
        print("   git clone https://github.com/kadedworkin/dugg-fyi.git")
        print("   cd dugg-fyi && uv sync")
        print("   dugg init")
        print("   dugg serve")
        print()
        print("   This is your personal knowledge base. It starts empty —")
        print("   that's fine. The shared server is where value comes from first.")
        print()
        print(f"2. Redeem this invite to subscribe to {instance_name}")
        print()
        print(f"   GET {invite_url}")
        print(f"     Header: Accept: application/json")
        print(f"     → Returns redemption instructions")
        print()
        print(f"   POST {invite_url}/redeem")
        print(f"     Header: Content-Type: application/json")
        print(f'     Body: {{"name": "{args.name}"}}')
        print(f"     → Returns human key + agent key + connection details")
        print()
        print(f"3. Connect to {instance_name} via MCP")
        print()
        print(f"   SSE endpoint: {server_url.rstrip('/')}/sse")
        print(f"   Auth header: X-Dugg-Key: <agent_api_key from step 2>")
        print()
        print("4. Explore what's already here")
        print()
        print("   dugg_welcome  — orient yourself")
        print("   dugg_feed     — see what others have shared")
        print("   dugg_search   — find specific topics")
        print("   dugg_react    — signal value to publishers")
        print()
        print("   This is where day-one value lives. Browse, search, react.")
        print("   Use dugg_catchup later for incremental updates, or set up")
        print("   dugg_webhook_subscribe for push notifications.")
        print()
        print("   The more servers you subscribe to, the more signal flows")
        print("   to you. Each subscription is another curated source.")
    else:
        print()
        print("--- For humans ---")
        print()
        print("Redeem via CLI:")
        print(f"  dugg redeem {token}")
        print()
        print("--- For agents ---")
        print()
        print("1. Set up your local Dugg")
        print()
        print("   git clone https://github.com/kadedworkin/dugg-fyi.git")
        print("   cd dugg-fyi && uv sync")
        print("   dugg init")
        print("   dugg serve")
        print()
        print("   Starts empty — the shared server is where value comes from first.")
        print()
        print("2. Redeem the invite to subscribe")
        print()
        print(f"   POST /invite/{token}/redeem")
        print(f"   Header: Content-Type: application/json")
        print(f'   Body: {{"name": "{args.name}"}}')
        print(f"   → Returns human key + agent key")
        print()
        print("3. Explore what's already here")
        print()
        print("   dugg_welcome, dugg_feed, dugg_search, dugg_react.")
        print("   Browse, search, react — that's the day-one experience.")
    print()
    print("Partner guide (read before first submission):")
    print("  https://github.com/kadedworkin/dugg-fyi/blob/main/PARTNER_AGENT.md")
    print()
    print("--- End ---")
    db.close()


def cmd_invites(args):
    """List invite tokens with their status."""
    from datetime import datetime, timezone
    from pathlib import Path
    db_path = Path(args.db) if args.db else DEFAULT_DB_PATH
    db = DuggDB(db_path)

    api_key = getattr(args, "key", None)
    if api_key:
        user = db.get_user_by_api_key(api_key)
        if not user:
            print("Invalid API key")
            sys.exit(1)
        tokens = db.list_invite_tokens(created_by=user["id"])
    else:
        tokens = db.list_invite_tokens()

    if not tokens:
        print("No invite tokens found.")
        db.close()
        return

    now = datetime.now(timezone.utc)
    pending, redeemed, expired = 0, 0, 0
    for t in tokens:
        name = t.get("name_hint") or "(no name)"
        if t.get("redeemed_by"):
            redeemer = db.get_user(t["redeemed_by"])
            redeemer_name = redeemer["name"] if redeemer else t["redeemed_by"]
            onboard_status = " (onboarded)" if t.get("onboarded_at") else " (awaiting first connect)"
            print(f"  {name} — redeemed by {redeemer_name} at {t['redeemed_at']}{onboard_status}")
            redeemed += 1
        elif datetime.fromisoformat(t["expires_at"]) < now:
            print(f"  {name} — expired ({t['expires_at']})")
            expired += 1
        else:
            print(f"  {name} — pending (token: {t['token']}, expires: {t['expires_at']})")
            pending += 1

    print(f"\n{pending} pending, {redeemed} redeemed, {expired} expired")
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
    server_url = db.get_config("server_url", "")
    db.close()

    if not result:
        print("Invalid, expired, or already-redeemed invite token.")
        sys.exit(1)

    user = result["user"]
    agent = result["agent"]
    from dugg.db import dugg_email_address
    email_addr = dugg_email_address(user["api_key"], server_url)
    print(f"Welcome to Dugg, {user['name']}!")
    print(f"ID:        {user['id']}")
    print(f"Your key:  {user['api_key']}")
    print(f"Agent key: {agent['api_key']}")
    if email_addr:
        print(f"Email:     {email_addr}")
    print()

    env_file = _find_env_file()
    existing = {}
    try:
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    existing[k.strip()] = v.strip()
    except (OSError, PermissionError):
        pass
    existing["DUGG_API_KEY"] = user["api_key"]
    existing["DUGG_AGENT_KEY"] = agent["api_key"]
    env_file.write_text("\n".join(f"{k}={v}" for k, v in existing.items()) + "\n")
    print(f"Keys saved to {env_file}")

    print("If your account gets banned, your agent goes too.")
    print()
    print("What next?")
    print("  • Got an AI agent? Give it the agent key with X-Dugg-Key")
    print("  • Use the CLI?    dugg welcome")


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
    return db.ensure_default_collection(user_id)


def _find_env_file():
    """Find the nearest writable .dugg-env, or create one next to this script."""
    from pathlib import Path
    try:
        check = Path.cwd()
    except (OSError, PermissionError):
        check = None
    if check:
        for _ in range(10):
            candidate = check / ".dugg-env"
            try:
                if candidate.exists():
                    return candidate
            except (OSError, PermissionError):
                pass
            parent = check.parent
            if parent == check:
                break
            check = parent
    # Fall back to the dugg-fyi install directory
    return Path(__file__).resolve().parent.parent.parent / ".dugg-env"


def cmd_rotate_key(args):
    """Rotate the API key you're authenticating with, invalidating the old one.

    Works against a remote server (via --server) or the local DB. Updates
    .dugg-env if the old key is stored there. Memberships, webhooks, and
    invites survive rotation (all keyed by user_id)."""
    from pathlib import Path
    import urllib.request
    import urllib.error

    server = getattr(args, "server", None)
    env_file = _find_env_file()
    env_vars = {}
    if env_file.exists():
        try:
            for line in env_file.read_text().splitlines():
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    env_vars[k.strip()] = v.strip()
        except (OSError, PermissionError):
            pass

    old_key = getattr(args, "key", None) or env_vars.get("DUGG_API_KEY") or DEFAULT_API_KEY
    if not old_key:
        print("No key found. Pass --key or run `dugg login <key>` first.")
        sys.exit(1)

    target_is_remote = bool(server) or env_vars.get("DUGG_SERVER_URL")
    server_url = server or env_vars.get("DUGG_SERVER_URL")

    if target_is_remote and server_url:
        req = urllib.request.Request(
            f"{server_url.rstrip('/')}/rotate-key",
            method="POST",
            headers={"X-Dugg-Key": old_key, "Content-Type": "application/json"},
            data=b"{}",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            print(f"Rotation failed: HTTP {e.code} — {e.read().decode()[:200]}")
            sys.exit(1)
        except urllib.error.URLError as e:
            print(f"Rotation failed: {e}")
            sys.exit(1)
        new_key = data["api_key"]
    else:
        db_path = Path(args.db) if args.db else DEFAULT_DB_PATH
        db = DuggDB(db_path)
        user = db.get_user_by_api_key(old_key)
        if not user:
            print(f"Key not recognized in {db_path}.")
            db.close()
            sys.exit(1)
        new_key = db.rotate_api_key(user["id"])
        db.close()

    if env_vars.get("DUGG_API_KEY") == old_key:
        env_vars["DUGG_API_KEY"] = new_key
        env_file.write_text("\n".join(f"{k}={v}" for k, v in env_vars.items()) + "\n")
        print(f"Rotated. New key saved to {env_file}:")
    else:
        print("Rotated. New key:")
    print(f"  {new_key}")
    print("\nThe old key is now invalid. Update any other places it's stored.")


def cmd_link(args):
    """Print a shareable URL for a resource that doesn't include any API key."""
    from pathlib import Path
    env_file = _find_env_file()
    env_vars = {}
    if env_file.exists():
        try:
            for line in env_file.read_text().splitlines():
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    env_vars[k.strip()] = v.strip()
        except (OSError, PermissionError):
            pass
    server_url = getattr(args, "server", None) or env_vars.get("DUGG_SERVER_URL")
    if not server_url:
        db_path = Path(args.db) if args.db else DEFAULT_DB_PATH
        db = DuggDB(db_path)
        server_url = db.get_config("server_url", "")
        db.close()
    if not server_url:
        print("No server URL found. Pass --server or run `dugg set-url <url>`.")
        sys.exit(1)
    print(f"{server_url.rstrip('/')}/r/{args.resource_id}")


def cmd_login(args):
    """Save your API key to .dugg-env so you don't need --key every time."""
    from pathlib import Path
    env_file = _find_env_file()
    existing = {}
    try:
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    existing[k.strip()] = v.strip()
    except (OSError, PermissionError):
        pass
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
    db.wait_for_webhooks()
    db.close()


def cmd_paste(args):
    """Add raw content (no URL) to Dugg."""
    import os
    import tempfile
    from pathlib import Path
    db_path = Path(args.db) if args.db else DEFAULT_DB_PATH
    db = DuggDB(db_path)
    user = _resolve_user(db, args)

    coll_id = _ensure_default_collection(db, user["id"])

    body = getattr(args, "body", None)
    file_path = getattr(args, "file", None)

    if file_path:
        body = Path(file_path).read_text()
    elif body is None:
        editor = os.environ.get("EDITOR", os.environ.get("VISUAL", "vi"))
        with tempfile.NamedTemporaryFile(suffix=".txt", mode="w+", delete=False) as f:
            tmp_path = f.name
        try:
            os.system(f'{editor} "{tmp_path}"')
            body = Path(tmp_path).read_text()
        finally:
            os.unlink(tmp_path)

    if not body or not body.strip():
        print("No content provided. Aborting.")
        db.close()
        sys.exit(1)

    note = getattr(args, "note", "") or ""
    tags = [t.strip() for t in (getattr(args, "tags", "") or "").split(",") if t.strip()]
    source_type = getattr(args, "source_type", "email")
    source_label = getattr(args, "source_label", "")
    published_at = getattr(args, "published_at", "") or ""

    from dugg.db import _uuid
    res_id = _uuid()
    synthetic_url = f"dugg://content/{res_id}"
    metadata = {}
    if source_label:
        metadata["source_label"] = source_label
    if published_at:
        metadata["published_at"] = published_at

    resource = db.add_resource(
        url=synthetic_url,
        collection_id=coll_id,
        submitted_by=user["id"],
        note=note,
        title=args.title,
        description=source_label,
        source_type=source_type,
        transcript=body,
        raw_metadata=metadata,
        tags=tags,
        tag_source="human" if tags else "agent",
    )

    word_count = len(body.split())
    print(f"  Pasted: {args.title}")
    print(f"  ID: {resource['id']}")
    print(f"  Type: {source_type}")
    if source_label:
        print(f"  Source: {source_label}")
    print(f"  Content: {word_count} words")
    db.wait_for_webhooks()
    db.close()


def _user_name_cache(db):
    """Build a user ID → name lookup."""
    rows = db.conn.execute("SELECT id, name FROM users").fetchall()
    return {r["id"]: r["name"] for r in rows}


def _short_date(value) -> str:
    """Coerce an ISO-ish date/datetime string to YYYY-MM-DD, or '' if it can't."""
    if not value:
        return ""
    s = str(value).strip()
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        return s[:10]
    return ""


def _publication_date(raw_metadata) -> str:
    """Extract a publication date from a resource's raw_metadata blob."""
    if not raw_metadata:
        return ""
    if isinstance(raw_metadata, str):
        import json as _json
        try:
            raw_metadata = _json.loads(raw_metadata)
        except (ValueError, TypeError):
            return ""
    if not isinstance(raw_metadata, dict):
        return ""
    return _short_date(raw_metadata.get("published_at") or raw_metadata.get("updated_at"))


def _format_attribution(added_by: str, added_date: str, pub_date: str, source: str = "") -> str:
    """Render the 'by X on YYYY-MM-DD (published YYYY-MM-DD)' line."""
    parts = []
    who_when = ""
    if added_by and added_date:
        who_when = f"by {added_by} on {added_date}"
    elif added_by:
        who_when = f"by {added_by}"
    elif added_date:
        who_when = f"on {added_date}"
    if who_when:
        if pub_date and pub_date != added_date:
            who_when += f" (published {pub_date})"
        parts.append(who_when)
    elif pub_date:
        parts.append(f"published {pub_date}")
    if source:
        parts.append(f"from {source}")
    return " · ".join(parts)


def cmd_search(args):
    """Search Dugg for resources."""
    from pathlib import Path
    db_path = Path(args.db) if args.db else DEFAULT_DB_PATH
    db = DuggDB(db_path)
    user = _resolve_user(db, args)
    names = _user_name_cache(db)

    results = db.search(args.query, user["id"], limit=getattr(args, "limit", 20))
    db.close()

    if not results:
        print(f"No results for \"{args.query}\"")
        return

    print(f"{len(results)} result(s) for \"{args.query}\":\n")
    for r in results:
        title = r.get("title") or r["url"]
        added_by = names.get(r.get("submitted_by", ""), "")
        added_date = _short_date(r.get("created_at", ""))
        pub_date = _publication_date(r.get("raw_metadata"))
        source = r.get("source_server", "")
        print(f"  {title}")
        print(f"    {r['url']}")
        meta = _format_attribution(added_by, added_date, pub_date, source)
        if meta:
            print(f"    {meta}")
        if r.get("note"):
            print(f"    Note: {r['note']}")
        print()


def _check_server_health(url):
    """Ping a server's /health endpoint. Returns (ok, detail) tuple."""
    import urllib.request
    import urllib.error
    try:
        req = urllib.request.urlopen(f"{url.rstrip('/')}/health", timeout=5)
        import json as _json
        data = _json.loads(req.read())
        return True, data
    except Exception as e:
        return False, str(e)


def cmd_health(args):
    """Check server health."""
    from pathlib import Path
    from datetime import datetime, timezone
    db_path = Path(args.db) if args.db else DEFAULT_DB_PATH
    db = DuggDB(db_path)
    server_url = db.get_config("server_url", "")
    db.close()

    if not server_url:
        print("No server URL configured. Set one with: dugg set-url <url>")
        sys.exit(1)

    ok, detail = _check_server_health(server_url)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    if ok:
        transport = detail.get("transport", "unknown")
        db_status = detail.get("db", "unknown")
        print(f"  {server_url}")
        print(f"  Status: ok · DB: {db_status} · Transport: {transport}")
        print(f"  Checked: {now}")
    else:
        print(f"  {server_url}")
        print(f"  Status: unreachable — {detail}")
        print(f"  Checked: {now}")
        sys.exit(1)


def cmd_feed(args):
    """Show recent resources."""
    from pathlib import Path
    from datetime import datetime, timezone
    db_path = Path(args.db) if args.db else DEFAULT_DB_PATH
    db = DuggDB(db_path)
    user = _resolve_user(db, args)
    names = _user_name_cache(db)
    server_url = db.get_config("server_url", "")

    limit = getattr(args, "limit", 20)
    results = db.get_feed(user["id"], limit=limit)
    db.close()

    if not results:
        print("Feed is empty. Add something with: dugg add <url>")
        return

    print(f"Latest {len(results)} resource(s):\n")
    for r in results:
        title = r.get("title") or r["url"]
        added_date = _short_date(r.get("created_at", ""))
        added_by = names.get(r.get("submitted_by", ""), "")
        pub_date = _publication_date(r.get("raw_metadata"))
        source = r.get("source_server", "")
        print(f"  {title}")
        print(f"    {r['url']}")
        meta = _format_attribution(added_by, added_date, pub_date, source)
        if meta:
            print(f"    {meta}")
        if r.get("note"):
            print(f"    Note: {r['note']}")
        print()

    if server_url:
        ok, detail = _check_server_health(server_url)
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        if ok:
            print(f"Server: {server_url} · ok · {now}")
        else:
            print(f"Server: {server_url} · unreachable · {now}")


def cmd_status(args):
    """Show your Dugg identity, connections, and resource count."""
    from pathlib import Path
    from datetime import datetime, timezone
    db_path = Path(args.db) if args.db else DEFAULT_DB_PATH
    if not db_path.exists():
        print("No Dugg database found. Run: dugg init")
        sys.exit(1)
    db = DuggDB(db_path)
    user = _resolve_user(db, args)
    server_url = db.get_config("server_url", "")

    print(f"  User: {user['name']} ({user['id'][:12]})")
    print(f"  DB:   {db_path}")
    if server_url:
        print(f"  Server: {server_url}")
    from dugg.db import dugg_email_address
    email_addr = dugg_email_address(user["api_key"], server_url)
    if email_addr:
        print(f"  Email: {email_addr}")

    collections = db.list_collections(user["id"])
    total_resources = 0
    for c in collections:
        count = db.conn.execute(
            "SELECT COUNT(*) FROM resources WHERE collection_id = ?", (c["id"],)
        ).fetchone()[0]
        total_resources += count
    print(f"  Collections: {len(collections)}")
    print(f"  Resources: {total_resources}")

    instances = db.list_instances(user["id"])
    if instances:
        print(f"  Instances: {len(instances)}")
        for inst in instances:
            print(f"    - {inst['name']} ({inst.get('endpoint_url', 'local')})")

    hooks = db.list_webhooks(user["id"])
    if hooks:
        active = sum(1 for h in hooks if h["status"] == "active")
        print(f"  Webhooks: {active} active")

    if server_url:
        from dugg.cli import _check_server_health
        ok, detail = _check_server_health(server_url)
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        status = "ok" if ok else "unreachable"
        print(f"  Health: {status} · {now}")

    db.close()


def cmd_servers(args):
    """List publish targets and connected servers."""
    from pathlib import Path
    db_path = Path(args.db) if args.db else DEFAULT_DB_PATH
    db = DuggDB(db_path)
    user = _resolve_user(db, args)
    server_url = db.get_config("server_url", "")

    print("Servers:\n")

    if server_url:
        print(f"  This server: {server_url}")
        ok, _ = _check_server_health(server_url)
        print(f"    Status: {'ok' if ok else 'unreachable'}")
        print()

    instances = db.list_instances(user["id"])
    if instances:
        for inst in instances:
            endpoint = inst.get("endpoint_url", "")
            print(f"  {inst['name']}")
            if endpoint:
                print(f"    Endpoint: {endpoint}")
            print(f"    Access: {inst.get('access_mode', 'invite')}")
            print()

    # Show publish targets from recent resources
    targets = db.conn.execute(
        "SELECT DISTINCT target FROM publish_targets ORDER BY published_at DESC LIMIT 20"
    ).fetchall()
    if targets:
        print("Publish targets:")
        for t in targets:
            print(f"    - {t['target']}")
        print()

    if not server_url and not instances and not targets:
        print("  No servers configured. Running in local-only mode.")
        print("  Set a server URL with: dugg set-url <url>")

    db.close()


def cmd_remove(args):
    """Remove a resource by ID or URL."""
    from pathlib import Path
    db_path = Path(args.db) if args.db else DEFAULT_DB_PATH
    db = DuggDB(db_path)
    user = _resolve_user(db, args)

    target = args.target
    # Find by URL or ID
    if target.startswith("http://") or target.startswith("https://"):
        row = db.conn.execute(
            "SELECT id, url, title, collection_id, submitted_by FROM resources WHERE url = ?", (target,)
        ).fetchone()
    else:
        row = db.conn.execute(
            "SELECT id, url, title, collection_id, submitted_by FROM resources WHERE id = ? OR id LIKE ?",
            (target, target + "%"),
        ).fetchone()

    if not row:
        print(f"Resource not found: {target}")
        db.close()
        sys.exit(1)

    resource = dict(row)
    title = resource.get("title") or resource["url"]

    # Allow deletion if user is the submitter or collection owner
    member = db.get_member_status(resource["collection_id"], user["id"])
    if resource["submitted_by"] != user["id"] and (not member or member["role"] != "owner"):
        print(f"Permission denied — you didn't submit this and aren't the collection owner.")
        db.close()
        sys.exit(1)

    # Direct delete (bypass owner check since we verified permissions above)
    db.conn.execute("DELETE FROM publish_queue WHERE resource_id = ?", (resource["id"],))
    db.conn.execute("DELETE FROM resources WHERE id = ?", (resource["id"],))
    db.conn.commit()
    db.emit_event("resource_deleted", actor_id=user["id"],
                   collection_id=resource["collection_id"],
                   payload={"resource_id": resource["id"], "url": resource["url"], "title": title})

    print(f"Removed: {title}")
    print(f"  {resource['url']}")
    db.close()


def cmd_edit(args):
    """Edit a resource's title or note."""
    from pathlib import Path
    db_path = Path(args.db) if args.db else DEFAULT_DB_PATH
    db = DuggDB(db_path)
    user = _resolve_user(db, args)

    target = args.target
    if target.startswith("http://") or target.startswith("https://"):
        row = db.conn.execute("SELECT id, submitted_by FROM resources WHERE url = ?", (target,)).fetchone()
    else:
        row = db.conn.execute(
            "SELECT id, submitted_by FROM resources WHERE id = ? OR id LIKE ?",
            (target, target + "%"),
        ).fetchone()

    if not row:
        print(f"Resource not found: {target}")
        db.close()
        sys.exit(1)

    resource_id = row["id"]

    # Only the submitter can edit
    if row["submitted_by"] != user["id"]:
        print("Permission denied — you didn't submit this resource.")
        db.close()
        sys.exit(1)

    updates = {}
    if getattr(args, "title", None):
        updates["title"] = args.title
    if getattr(args, "note", None):
        updates["note"] = args.note

    if not updates:
        print("Nothing to change. Use --title or --note.")
        db.close()
        sys.exit(1)

    result = db.update_resource(resource_id, **updates)
    db.close()

    if result:
        print(f"Updated: {result.get('title') or result['url']}")
        if "title" in updates:
            print(f"  Title: {updates['title']}")
        if "note" in updates:
            print(f"  Note: {updates['note']}")
    else:
        print("Update failed.")


def cmd_webhook(args):
    """Manage webhook subscriptions."""
    from pathlib import Path
    db_path = Path(args.db) if args.db else DEFAULT_DB_PATH
    db = DuggDB(db_path)
    user = _resolve_user(db, args)
    action = getattr(args, "webhook_action", None)

    if action == "add":
        url = args.url
        events = [e.strip() for e in (getattr(args, "events", "") or "").split(",") if e.strip()] or None
        secret = getattr(args, "secret", "") or ""
        result = db.subscribe_webhook(user["id"], url, event_types=events, secret=secret)
        db.close()
        print(f"Webhook registered: {result['id'][:12]}")
        print(f"  URL: {url}")
        print(f"  Events: {', '.join(events) if events else 'all'}")
        if "hooks.slack.com" in url:
            print("  Format: Slack (auto-detected)")

    elif action == "list":
        hooks = db.list_webhooks(user["id"])
        db.close()
        if not hooks:
            print("No webhooks. Add one with: dugg webhook add <url>")
            return
        for h in hooks:
            events = ", ".join(h["event_types"]) if h["event_types"] else "all"
            print(f"  {h['id'][:12]}  {h['callback_url']}")
            print(f"    Events: {events} · Status: {h['status']} · Failures: {h['failure_count']}")

    elif action == "remove":
        removed = db.unsubscribe_webhook(args.webhook_id, user["id"])
        db.close()
        if removed:
            print("Webhook removed.")
        else:
            print("Webhook not found (wrong ID or not yours).")

    elif action == "test":
        hooks = db.list_webhooks(user["id"])
        if not hooks:
            db.close()
            print("No webhooks to test. Add one with: dugg webhook add <url>")
            return
        server_name = db.get_config("server_url", "Dugg")
        db.emit_event("resource_added", actor_id=user["id"],
                       payload={"url": "https://dugg.fyi", "title": f"{server_name} Webhook Test", "note": "This is a test notification from Dugg"})
        db.wait_for_webhooks()
        db.close()
        print(f"Test event fired to {len(hooks)} webhook(s). Check your channel.")

    else:
        print("Usage: dugg webhook {add,list,remove,test}")
        db.close()


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

    p_setconf = sub.add_parser("set-config", help="Set a server config value")
    p_setconf.add_argument("key", help="Config key (e.g. slack_signing_secret)")
    p_setconf.add_argument("value", help="Config value")

    p_shared = sub.add_parser(
        "enable-shared-default",
        help="Mark a collection as the server's shared Default (hosted-instance mode)",
    )
    p_shared.add_argument("collection_id", help="Collection ID to make the shared default")

    p_user = sub.add_parser("add-user", help="Create a new user")
    p_user.add_argument("name", help="User display name")
    p_user.add_argument("--server", default=None, help="Remote server URL — create user via HTTP instead of local DB")
    p_user.add_argument("--key", default=None, help="Owner API key for remote auth (required with --server)")

    p_invite = sub.add_parser("invite-user", help="Create an invite token for a new user")
    p_invite.add_argument("name", help="Name of the person being invited")
    p_invite.add_argument("--key", default=None, help="Your API key (uses local user if omitted)")
    p_invite.add_argument("--server", default=None, help="Server URL (e.g. https://chino-bandido.kadedworkin.com) — included in invite message")
    p_invite.add_argument("--expires", type=int, default=72, help="Hours until invite expires (default: 72)")

    p_invites = sub.add_parser("invites", help="List invite tokens (pending, redeemed, expired)")
    p_invites.add_argument("--key", default=None, help="Your API key (shows only your invites)")

    p_redeem = sub.add_parser("redeem", help="Redeem an invite token to join a Dugg server")
    p_redeem.add_argument("token", help="The invite token to redeem")
    p_redeem.add_argument("--name", default=None, help="Your display name (uses invite hint if omitted)")

    sub.add_parser("list-users", help="List all users")

    p_login = sub.add_parser("login", help="Save your API key so you don't need --key every time")
    p_login.add_argument("key", help="Your API key")

    p_rotate = sub.add_parser("rotate-key", help="Rotate your API key, invalidating the old one")
    p_rotate.add_argument("--key", default=None, help="The current key to rotate (defaults to .dugg-env)")
    p_rotate.add_argument("--server", default=None, help="Remote server URL — rotates via HTTP instead of local DB")

    p_link = sub.add_parser("link", help="Print a shareable URL for a resource (no key in URL)")
    p_link.add_argument("resource_id", help="Resource ID to link to")
    p_link.add_argument("--server", default=None, help="Override server URL")

    p_add = sub.add_parser("add", help="Add a URL to Dugg")
    p_add.add_argument("url", help="URL to add")
    p_add.add_argument("--note", default="", help="Why this resource matters")
    p_add.add_argument("--tags", default="", help="Comma-separated tags")
    p_add.add_argument("--key", default=None, help="Your API key (uses local user if omitted)")

    p_paste = sub.add_parser("paste", help="Add raw content (no URL) — for emails, newsletters, notes")
    p_paste.add_argument("title", help="Title for this content")
    p_paste.add_argument("--body", default=None, help="Content body (opens $EDITOR if omitted)")
    p_paste.add_argument("--file", default=None, help="Read body from a file")
    p_paste.add_argument("--source-type", default="email", choices=["note", "email", "document"], help="Content type (default: email)")
    p_paste.add_argument("--source-label", default="", help="Origin label (e.g. 'Substack', 'meeting notes')")
    p_paste.add_argument("--published-at", default="", help="Original publication/send date (ISO 8601)")
    p_paste.add_argument("--note", default="", help="Why this content matters")
    p_paste.add_argument("--tags", default="", help="Comma-separated tags")
    p_paste.add_argument("--key", default=None, help="Your API key (uses local user if omitted)")

    p_search = sub.add_parser("search", help="Search Dugg for resources")
    p_search.add_argument("query", help="Search query")
    p_search.add_argument("--limit", type=int, default=20, help="Max results (default: 20)")
    p_search.add_argument("--key", default=None, help="Your API key (uses local user if omitted)")

    p_feed = sub.add_parser("feed", help="Show recent resources")
    p_feed.add_argument("--limit", type=int, default=20, help="Max results (default: 20)")
    p_feed.add_argument("--key", default=None, help="Your API key (uses local user if omitted)")

    p_status = sub.add_parser("status", help="Show your identity, connections, and resource count")
    p_status.add_argument("--key", default=None, help="Your API key")

    p_servers = sub.add_parser("servers", help="List connected servers and publish targets")
    p_servers.add_argument("--key", default=None, help="Your API key")

    p_remove = sub.add_parser("remove", help="Remove a resource by ID or URL")
    p_remove.add_argument("target", help="Resource ID (or prefix) or URL")
    p_remove.add_argument("--key", default=None, help="Your API key")

    p_edit = sub.add_parser("edit", help="Edit a resource's title or note")
    p_edit.add_argument("target", help="Resource ID (or prefix) or URL")
    p_edit.add_argument("--title", default=None, help="New title")
    p_edit.add_argument("--note", default=None, help="New note")
    p_edit.add_argument("--key", default=None, help="Your API key")

    sub.add_parser("health", help="Check server health")

    p_webhook = sub.add_parser("webhook", help="Manage webhook notifications (e.g. Slack)")
    webhook_sub = p_webhook.add_subparsers(dest="webhook_action")
    pw_add = webhook_sub.add_parser("add", help="Subscribe a webhook URL")
    pw_add.add_argument("url", help="Webhook callback URL (e.g. Slack incoming webhook URL)")
    pw_add.add_argument("--events", default="", help="Comma-separated event types to subscribe to (default: all)")
    pw_add.add_argument("--secret", default="", help="HMAC secret for signature verification")
    pw_add.add_argument("--key", default=None, help="Your API key")
    pw_list = webhook_sub.add_parser("list", help="List your webhook subscriptions")
    pw_list.add_argument("--key", default=None, help="Your API key")
    pw_rm = webhook_sub.add_parser("remove", help="Remove a webhook subscription")
    pw_rm.add_argument("webhook_id", help="Webhook ID (first 12 chars is enough)")
    pw_rm.add_argument("--key", default=None, help="Your API key")
    pw_test = webhook_sub.add_parser("test", help="Fire a test event to your webhooks")
    pw_test.add_argument("--key", default=None, help="Your API key")

    p_doctor = sub.add_parser("doctor", help="Full installation diagnostics (schema, FTS, users)")
    p_doctor.add_argument("--host", default="127.0.0.1", help="HTTP host to check (default: 127.0.0.1)")
    p_doctor.add_argument("--port", type=int, default=None, help="If set, also check HTTP server reachability")

    p_admin = sub.add_parser("admin", help="Launch admin TUI for ban & appeal management")
    p_admin.add_argument("--key", default=None, help="API key (uses local user if omitted)")

    p_welcome = sub.add_parser("welcome", help="Show orientation info for your Dugg installation")
    p_welcome.add_argument("--key", default=None, help="API key (uses local user if omitted)")

    p_email = sub.add_parser("email", help="Show your email forwarding address for each connected instance")
    p_email.add_argument("--key", default=None, help="API key (uses local user if omitted)")

    p_rss = sub.add_parser("rss", help="Subscribe to RSS / Atom feeds and ingest entries as resources")
    rss_sub = p_rss.add_subparsers(dest="rss_action")

    pr_sub = rss_sub.add_parser("subscribe", help="Register a feed URL to poll")
    pr_sub.add_argument("url", help="Feed URL (RSS or Atom; parameterized/auth URLs are preserved as-is)")
    pr_sub.add_argument("--collection", default="", help="Collection name or id (defaults to Default)")
    pr_sub.add_argument("--interval", default="1h", help="Poll interval: 30m, 1h, 6h, 1d, or bare seconds (default: 1h, min 60s)")
    pr_sub.add_argument("--tag", default="rss", help="Tag applied to each ingested entry (default: rss)")
    pr_sub.add_argument("--now", action="store_true", help="Poll the feed immediately after subscribing")
    pr_sub.add_argument("--key", default=None, help="Your API key")

    pr_list = rss_sub.add_parser("list", help="List your RSS subscriptions")
    pr_list.add_argument("--key", default=None, help="Your API key")

    pr_rm = rss_sub.add_parser("remove", help="Remove an RSS subscription")
    pr_rm.add_argument("sub_id", help="Subscription id")
    pr_rm.add_argument("--key", default=None, help="Your API key")

    pr_pause = rss_sub.add_parser("pause", help="Pause polling for a subscription")
    pr_pause.add_argument("sub_id", help="Subscription id")
    pr_pause.add_argument("--key", default=None, help="Your API key")

    pr_resume = rss_sub.add_parser("resume", help="Resume polling for a subscription")
    pr_resume.add_argument("sub_id", help="Subscription id")
    pr_resume.add_argument("--key", default=None, help="Your API key")

    pr_poll = rss_sub.add_parser("poll", help="Poll a subscription right now (or all if no id given)")
    pr_poll.add_argument("sub_id", nargs="?", default=None, help="Optional subscription id")
    pr_poll.add_argument("--key", default=None, help="Your API key")

    p_export = sub.add_parser("export", help="Export resources to a portable .dugg.json file")
    p_export.add_argument("output", help="Output file path (use - for stdout)")
    p_export.add_argument("--collection", default=None, help="Export only this collection (name or id)")
    p_export.add_argument("--tag", action="append", help="Filter to resources with this tag (repeatable)")
    p_export.add_argument("--since", default=None, help="Only resources created after this date (ISO 8601)")
    p_export.add_argument("--pretty", action="store_true", help="Indent JSON for readability")
    p_export.add_argument("--key", default=None, help="Your API key")

    p_import = sub.add_parser("import", help="Import resources from a .dugg.json file")
    p_import.add_argument("input", help="Input file path (use - for stdin)")
    p_import.add_argument("--collection", default=None, help="Import into this collection (name or id)")
    p_import.add_argument("--tag", action="append", help="Apply this tag to all imported resources (repeatable)")
    p_import.add_argument("--on-conflict", choices=["skip", "update"], default="skip",
                          help="URL collision behavior (default: skip)")
    p_import.add_argument("--dry-run", action="store_true", help="Show what would be imported without writing")
    p_import.add_argument("--key", default=None, help="Your API key")

    # If the first arg looks like a URL, treat it as `dugg add <url> ...`
    if len(sys.argv) > 1 and sys.argv[1].startswith(("http://", "https://")):
        sys.argv.insert(1, "add")

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
    elif args.command == "set-config":
        cmd_set_config(args)
    elif args.command == "enable-shared-default":
        cmd_enable_shared_default(args)
    elif args.command == "add-user":
        cmd_add_user(args)
    elif args.command == "invite-user":
        cmd_invite_user(args)
    elif args.command == "invites":
        cmd_invites(args)
    elif args.command == "redeem":
        cmd_redeem(args)
    elif args.command == "list-users":
        cmd_list_users(args)
    elif args.command == "login":
        cmd_login(args)
    elif args.command == "rotate-key":
        cmd_rotate_key(args)
    elif args.command == "link":
        cmd_link(args)
    elif args.command == "add":
        cmd_add(args)
    elif args.command == "paste":
        cmd_paste(args)
    elif args.command == "search":
        cmd_search(args)
    elif args.command == "feed":
        cmd_feed(args)
    elif args.command == "status":
        cmd_status(args)
    elif args.command == "servers":
        cmd_servers(args)
    elif args.command == "remove":
        cmd_remove(args)
    elif args.command == "edit":
        cmd_edit(args)
    elif args.command == "health":
        cmd_health(args)
    elif args.command == "webhook":
        cmd_webhook(args)
    elif args.command == "admin":
        cmd_admin(args)
    elif args.command == "doctor":
        cmd_doctor(args)
    elif args.command == "welcome":
        cmd_welcome(args)
    elif args.command == "email":
        cmd_email(args)
    elif args.command == "rss":
        cmd_rss(args)
    elif args.command == "export":
        cmd_export(args)
    elif args.command == "import":
        cmd_import(args)


def _resolve_collection(db, user_id: str, name_or_id: str) -> Optional[str]:
    """Return a collection_id for either an id or a case-insensitive name."""
    if not name_or_id:
        return None
    row = db.conn.execute(
        "SELECT id FROM collections WHERE id = ?", (name_or_id,)
    ).fetchone()
    if row:
        return row["id"]
    for c in db.list_collections(user_id):
        if c["name"].lower() == name_or_id.lower():
            return c["id"]
    return None


def cmd_rss(args):
    """Dispatch for `dugg rss <action>`."""
    action = getattr(args, "rss_action", None)
    if action == "subscribe":
        cmd_rss_subscribe(args)
    elif action == "list":
        cmd_rss_list(args)
    elif action == "remove":
        cmd_rss_remove(args)
    elif action == "pause":
        cmd_rss_pause(args)
    elif action == "resume":
        cmd_rss_resume(args)
    elif action == "poll":
        cmd_rss_poll(args)
    else:
        print("Usage: dugg rss {subscribe,list,remove,pause,resume,poll}")
        sys.exit(1)


def cmd_rss_subscribe(args):
    """Register a feed URL for polling into a collection."""
    from pathlib import Path
    db_path = Path(args.db) if args.db else DEFAULT_DB_PATH
    db = DuggDB(db_path)
    user = _resolve_user(db, args)

    interval_raw = (getattr(args, "interval", "1h") or "1h").strip().lower()
    interval_seconds = _parse_interval(interval_raw)

    collection_arg = getattr(args, "collection", "") or ""
    if collection_arg:
        coll_id = _resolve_collection(db, user["id"], collection_arg)
        if not coll_id:
            print(f"Collection not found: {collection_arg}")
            db.close()
            sys.exit(1)
    else:
        coll_id = _ensure_default_collection(db, user["id"])

    tag_label = getattr(args, "tag", "") or "rss"

    try:
        sub = db.add_rss_subscription(
            user_id=user["id"],
            collection_id=coll_id,
            feed_url=args.url,
            tag_label=tag_label,
            poll_interval_seconds=interval_seconds,
        )
    except sqlite3.IntegrityError:
        print("Already subscribed to that feed for this collection.")
        db.close()
        sys.exit(1)

    print(f"  Subscribed: {args.url}")
    print(f"  ID: {sub['id']}")
    print(f"  Collection: {collection_arg or 'Default'} ({coll_id[:12]})")
    print(f"  Poll every: {interval_raw} ({interval_seconds}s)")
    print(f"  Tag: {tag_label}")
    if getattr(args, "now", False):
        print("\nPolling now…")
        _poll_one_subscription(db, sub["id"])
    db.close()


def cmd_rss_list(args):
    from pathlib import Path
    db_path = Path(args.db) if args.db else DEFAULT_DB_PATH
    db = DuggDB(db_path)
    user = _resolve_user(db, args)
    subs = db.list_rss_subscriptions(user_id=user["id"])
    db.close()

    if not subs:
        print("No RSS subscriptions. Add one with: dugg rss subscribe <url>")
        return
    print(f"{len(subs)} subscription(s):\n")
    for s in subs:
        state = "paused" if not s["enabled"] else "active"
        last = (s["last_polled_at"] or "").split("T")[0] or "never"
        print(f"  [{state}] {s['feed_url']}")
        print(f"    id: {s['id'][:12]}  every: {s['poll_interval_seconds']}s  last: {last}")
        if s["feed_title"]:
            print(f"    title: {s['feed_title']}")


def cmd_rss_remove(args):
    from pathlib import Path
    db_path = Path(args.db) if args.db else DEFAULT_DB_PATH
    db = DuggDB(db_path)
    _resolve_user(db, args)
    ok = db.remove_rss_subscription(args.sub_id)
    db.close()
    print("  Removed." if ok else "  Not found.")
    if not ok:
        sys.exit(1)


def cmd_rss_pause(args):
    _set_rss_enabled(args, False)


def cmd_rss_resume(args):
    _set_rss_enabled(args, True)


def _set_rss_enabled(args, enabled: bool):
    from pathlib import Path
    db_path = Path(args.db) if args.db else DEFAULT_DB_PATH
    db = DuggDB(db_path)
    _resolve_user(db, args)
    ok = db.set_rss_subscription_enabled(args.sub_id, enabled)
    db.close()
    print(f"  {'Resumed' if enabled else 'Paused'}." if ok else "  Not found.")
    if not ok:
        sys.exit(1)


def cmd_rss_poll(args):
    """Manually poll a single subscription (or all) right now."""
    from pathlib import Path
    db_path = Path(args.db) if args.db else DEFAULT_DB_PATH
    db = DuggDB(db_path)
    user = _resolve_user(db, args)

    if getattr(args, "sub_id", None):
        _poll_one_subscription(db, args.sub_id)
    else:
        for s in db.list_rss_subscriptions(user_id=user["id"]):
            if s["enabled"]:
                _poll_one_subscription(db, s["id"])
    db.close()


def _poll_one_subscription(db, sub_id: str):
    import asyncio
    from dugg.rss import sync_feed
    sub = db.get_rss_subscription(sub_id)
    if not sub:
        print(f"  Subscription not found: {sub_id}")
        return
    result = asyncio.run(sync_feed(db, sub))
    db.update_rss_subscription_state(
        sub["id"],
        etag=result["etag"],
        last_modified=result["last_modified"],
        seen_entry_ids=result["seen_entry_ids"],
        feed_title=result["feed_title"] or sub.get("feed_title") or "",
    )
    db.wait_for_webhooks()
    print(f"  {sub['feed_url']}: +{result['new']} new, {result['skipped']} skipped (HTTP {result['status']})")


def _parse_interval(raw: str) -> int:
    """Parse a human interval like '1h', '30m', '15s', or a bare integer (seconds)."""
    raw = (raw or "").strip().lower()
    if not raw:
        return 3600
    if raw.isdigit():
        return max(60, int(raw))
    try:
        num = int(raw[:-1])
    except ValueError:
        return 3600
    suffix = raw[-1]
    if suffix == "s":
        return max(60, num)
    if suffix == "m":
        return max(60, num * 60)
    if suffix == "h":
        return max(60, num * 3600)
    if suffix == "d":
        return max(60, num * 86400)
    return 3600


def cmd_email(args):
    """Show email forwarding addresses for all connected instances."""
    from pathlib import Path
    from dugg.db import dugg_email_address
    db_path = Path(args.db) if args.db else DEFAULT_DB_PATH
    if not db_path.exists():
        print("No Dugg database found. Run: dugg init")
        sys.exit(1)
    db = DuggDB(db_path)
    user = _resolve_user(db, args)
    server_url = db.get_config("server_url", "")

    print(f"Email forwarding addresses for {user['name']}:\n")

    if server_url:
        email_addr = dugg_email_address(user["api_key"], server_url)
        if email_addr:
            print(f"  This server: {email_addr}")

    instances = db.list_instances(user["id"])
    for inst in instances:
        endpoint = inst.get("endpoint_url", "")
        if endpoint:
            email_addr = dugg_email_address(user["api_key"], endpoint)
            if email_addr:
                print(f"  {inst['name']}: {email_addr}")

    if not server_url and not instances:
        print("  No server URL configured. Run: dugg set-config server_url https://your-server")

    db.close()
    print()
    print("Forward emails to any of these addresses to add them as resources.")


def cmd_export(args):
    """Export resources to a portable .dugg.json file."""
    from pathlib import Path
    db_path = Path(args.db) if args.db else DEFAULT_DB_PATH
    db = DuggDB(db_path)
    user = _resolve_user(db, args)

    collection_id = None
    if getattr(args, "collection", None):
        collection_id = _resolve_collection(db, user["id"], args.collection)
        if not collection_id:
            print(f"Collection not found: {args.collection}")
            db.close()
            sys.exit(1)

    tags = [t.strip() for t in (getattr(args, "tag", None) or [])]
    since = getattr(args, "since", None) or None

    resources = db.export_resources(
        user_id=user["id"],
        collection_id=collection_id,
        tags=tags if tags else None,
        since=since,
    )

    server_url = db.get_config("server_url", "")
    db.close()

    payload = {
        "dugg_version": "1.0",
        "exported_at": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        ).isoformat(),
        "source_server": server_url,
        "resource_count": len(resources),
        "resources": resources,
    }

    pretty = getattr(args, "pretty", False)
    indent = 2 if pretty else None
    output_json = json.dumps(payload, indent=indent, ensure_ascii=False)

    output_file = args.output
    if output_file == "-":
        sys.stdout.write(output_json + "\n")
    else:
        Path(output_file).write_text(output_json, encoding="utf-8")
        print(f"Exported {len(resources)} resources to {output_file}")


def cmd_import(args):
    """Import resources from a .dugg.json file."""
    from pathlib import Path
    db_path = Path(args.db) if args.db else DEFAULT_DB_PATH
    db = DuggDB(db_path)
    user = _resolve_user(db, args)

    input_file = args.input
    if input_file == "-":
        raw = sys.stdin.read()
    else:
        raw = Path(input_file).read_text(encoding="utf-8")

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"Invalid JSON: {e}")
        db.close()
        sys.exit(1)

    version = payload.get("dugg_version", "")
    if not version:
        print("Missing dugg_version — is this a .dugg.json file?")
        db.close()
        sys.exit(1)

    resources = payload.get("resources", [])
    if not resources:
        print("No resources in file.")
        db.close()
        sys.exit(0)

    # Resolve target collection
    coll_name = getattr(args, "collection", None)
    if coll_name:
        coll_id = _resolve_collection(db, user["id"], coll_name)
        if not coll_id:
            print(f"Collection not found: {coll_name}")
            db.close()
            sys.exit(1)
    else:
        coll_id = _ensure_default_collection(db, user["id"])

    extra_tags = [t.strip() for t in (getattr(args, "tag", None) or [])]
    on_conflict = getattr(args, "on_conflict", "skip")
    dry_run = getattr(args, "dry_run", False)

    imported = 0
    skipped = 0
    updated = 0

    for res_data in resources:
        if dry_run:
            url = res_data.get("url", "?")
            title = res_data.get("title", "") or url
            existing = db.conn.execute(
                "SELECT id FROM resources WHERE url = ? AND collection_id = ?",
                (url, coll_id),
            ).fetchone()
            if existing:
                action = on_conflict
                if on_conflict == "skip":
                    skipped += 1
                else:
                    updated += 1
            else:
                action = "import"
                imported += 1
            print(f"  [{action}] {title[:60]}")
            continue

        result = db.import_resource(
            resource_data=res_data,
            collection_id=coll_id,
            submitted_by=user["id"],
            on_conflict=on_conflict,
            extra_tags=extra_tags if extra_tags else None,
        )
        status = result.get("status", "imported")
        if status == "imported":
            imported += 1
        elif status == "skipped":
            skipped += 1
        elif status == "updated":
            updated += 1

    db.close()

    prefix = "[dry run] " if dry_run else ""
    print(f"\n{prefix}Import complete: {imported} imported, {skipped} skipped, {updated} updated")


if __name__ == "__main__":
    main()
