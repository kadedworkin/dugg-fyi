"""HTTP/SSE transport for the Dugg MCP server.

Provides three things:
1. MCP SSE transport — standard MCP protocol over HTTP (GET /sse, POST /messages)
2. REST ingest endpoint — POST /ingest for receiving published resources from remote instances
3. Health check — GET /health

Uses Starlette (already a transitive dep of mcp[sse]) and uvicorn.
"""

import asyncio
from contextlib import asynccontextmanager
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
from typing import Optional

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

from mcp.server.sse import SseServerTransport

from dugg.db import DuggDB
from dugg.sync import start_sync_daemon

logger = logging.getLogger("dugg.http")


def _xml_escape(s: str) -> str:
    """Escape XML special characters."""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def create_app(db_path: Optional[Path] = None) -> Starlette:
    """Create the Starlette ASGI app with MCP SSE transport and REST endpoints."""

    # --- Shared state ---
    db: Optional[DuggDB] = None

    def get_db() -> DuggDB:
        nonlocal db
        if db is None:
            path = db_path or (Path(os.environ["DUGG_DB_PATH"]) if os.environ.get("DUGG_DB_PATH") else None)
            db = DuggDB(path)
        return db

    def _ensure_default_collection(d: DuggDB, user_id: str) -> str:
        """Ensure user has a default collection, return its ID."""
        collections = d.list_collections(user_id)
        for c in collections:
            if c["name"] == "Default":
                return c["id"]
        result = d.create_collection("Default", user_id, description="Default collection", visibility="private")
        return result["id"]

    def resolve_user_from_request(request: Request) -> dict:
        """Resolve user from X-Dugg-Key header."""
        api_key = request.headers.get("x-dugg-key", "")
        d = get_db()
        if api_key:
            user = d.get_user_by_api_key(api_key)
            if user:
                return user
            raise ValueError("Invalid API key")
        raise ValueError("Missing X-Dugg-Key header — API key required for HTTP transport")

    def verify_hmac_signature(request: Request, body: bytes, secret: str) -> bool:
        """Verify HMAC-SHA256 signature from X-Dugg-Signature header."""
        sig_header = request.headers.get("x-dugg-signature", "")
        if not sig_header.startswith("sha256="):
            return False
        expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig_header[7:], expected)

    # --- MCP SSE Transport ---
    # The SseServerTransport endpoint is where clients POST messages back
    sse_transport = SseServerTransport("/messages")

    async def handle_sse(request: Request):
        """SSE connection endpoint — clients connect here to receive server events."""
        async with sse_transport.connect_sse(
            request.scope, request.receive, request._send
        ) as streams:
            # Import server here to avoid circular imports
            from dugg.server import server
            await server.run(
                streams[0], streams[1],
                server.create_initialization_options(),
            )

    async def handle_messages(request: Request):
        """Message endpoint — clients POST MCP messages here."""
        await sse_transport.handle_post_message(
            request.scope, request.receive, request._send
        )

    # --- REST Endpoints ---

    async def handle_ingest(request: Request):
        """POST /ingest — receive published resources from remote Dugg instances.

        Expected payload:
        {
            "resource": {
                "url": "...",
                "title": "...",
                "description": "...",
                ...
            },
            "target": "instance-name",
            "source_instance_id": "..."
        }

        Auth: X-Dugg-Key header required.
        Optional HMAC: X-Dugg-Signature header with sha256=<hex>.
        """
        try:
            user = resolve_user_from_request(request)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=401)

        try:
            body = await request.body()
            payload = json.loads(body)
        except (json.JSONDecodeError, Exception):
            return JSONResponse({"error": "Invalid JSON payload"}, status_code=400)

        resource_data = payload.get("resource", {})
        source_instance_id = payload.get("source_instance_id", "")
        target = payload.get("target", "")

        if not resource_data.get("url"):
            return JSONResponse({"error": "Missing resource.url"}, status_code=400)
        if not source_instance_id:
            return JSONResponse({"error": "Missing source_instance_id"}, status_code=400)

        d = get_db()

        # Find a collection to ingest into — use Default
        coll_id = _ensure_default_collection(d, user["id"])

        result = d.ingest_remote_publish(resource_data, source_instance_id, coll_id)
        if not result:
            return JSONResponse({"error": "Ingest failed"}, status_code=500)

        if result["status"] == "duplicate":
            return JSONResponse({
                "status": "duplicate",
                "id": result["id"],
                "url": resource_data["url"],
            }, status_code=200)

        return JSONResponse({
            "status": "ingested",
            "id": result["id"],
            "url": resource_data["url"],
            "source_instance_id": source_instance_id,
        }, status_code=201)

    async def handle_health(request: Request):
        """GET /health — liveness check."""
        d = get_db()
        try:
            d.conn.execute("SELECT 1").fetchone()
            db_ok = True
        except Exception:
            db_ok = False

        return JSONResponse({
            "status": "ok" if db_ok else "degraded",
            "db": "connected" if db_ok else "error",
            "transport": "http+sse",
        })

    async def handle_tools(request: Request):
        """POST /tools/{tool_name} — HTTP dispatch for any MCP tool.

        Body: JSON with tool arguments.
        Auth: X-Dugg-Key header.
        Response: JSON with tool result text.

        Accepts an optional X-Dugg-Format header:
        - "rich" (default): full output with descriptions and context
        - "compact": condensed output for terminal/CLI environments
        """
        tool_name = request.path_params["tool_name"]

        try:
            user = resolve_user_from_request(request)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=401)

        try:
            body = await request.body()
            args = json.loads(body) if body else {}
        except json.JSONDecodeError:
            return JSONResponse({"error": "Invalid JSON"}, status_code=400)

        # Inject the API key so the tool handler resolves the same user
        args["api_key"] = request.headers.get("x-dugg-key", "")

        from dugg.server import server as mcp_server
        # Call the tool handler directly
        from dugg.server import call_tool
        try:
            results = await call_tool(tool_name, args)
            texts = [r.text for r in results if hasattr(r, "text")]
            full_result = "\n".join(texts)

            # Compact mode: strip blank lines, truncate long fields
            format_mode = request.headers.get("x-dugg-format", "rich").lower()
            if format_mode == "compact":
                lines = [ln for ln in full_result.split("\n") if ln.strip()]
                full_result = "\n".join(lines)

            return JSONResponse({
                "tool": tool_name,
                "result": full_result,
                "format": format_mode,
            })
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    # --- Invite & Feed (unauthenticated) ---

    def _html_page(title: str, body: str) -> str:
        """Minimal HTML page wrapper."""
        return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
         background: #0a0a0a; color: #e0e0e0; min-height: 100vh;
         display: flex; justify-content: center; padding: 2rem 1rem; }}
  .card {{ max-width: 480px; width: 100%; background: #1a1a1a; border: 1px solid #333;
           border-radius: 12px; padding: 2rem; }}
  h1 {{ font-size: 1.5rem; margin-bottom: 0.5rem; color: #fff; }}
  .topic {{ color: #888; font-size: 0.9rem; margin-bottom: 1.5rem; }}
  label {{ display: block; font-size: 0.85rem; color: #aaa; margin-bottom: 0.3rem; }}
  input[type=text] {{ width: 100%; padding: 0.6rem; background: #111; border: 1px solid #444;
                      border-radius: 6px; color: #fff; font-size: 1rem; margin-bottom: 1rem; }}
  input[type=text]:focus {{ outline: none; border-color: #6366f1; }}
  button {{ width: 100%; padding: 0.7rem; background: #6366f1; color: #fff; border: none;
            border-radius: 6px; font-size: 1rem; cursor: pointer; font-weight: 600; }}
  button:hover {{ background: #5558e6; }}
  .key-box {{ background: #111; border: 1px solid #444; border-radius: 6px; padding: 1rem;
              font-family: monospace; font-size: 0.95rem; word-break: break-all; margin: 1rem 0;
              color: #4ade80; }}
  .next-steps {{ margin-top: 1.5rem; }}
  .next-steps h3 {{ font-size: 0.95rem; margin-bottom: 0.5rem; color: #fff; }}
  .next-steps li {{ font-size: 0.85rem; color: #aaa; margin-bottom: 0.5rem; list-style: none; }}
  .next-steps li strong {{ color: #e0e0e0; }}
  .error {{ color: #f87171; margin-bottom: 1rem; }}
  .feed-item {{ border-bottom: 1px solid #222; padding: 1rem 0; }}
  .feed-item:last-child {{ border-bottom: none; }}
  .feed-item h3 {{ font-size: 1rem; margin-bottom: 0.25rem; }}
  .feed-item h3 a {{ color: #93c5fd; text-decoration: none; }}
  .feed-item h3 a:hover {{ text-decoration: underline; }}
  .feed-item .meta {{ font-size: 0.8rem; color: #666; }}
  .feed-item .note {{ font-size: 0.85rem; color: #aaa; margin-top: 0.3rem; }}
  .empty {{ color: #666; text-align: center; padding: 2rem 0; }}
</style>
</head>
<body><div class="card">{body}</div></body>
</html>"""

    async def handle_invite_page(request: Request):
        """GET /invite/{token} — show the invite redemption page."""
        token = request.path_params["token"]
        d = get_db()
        invite = d.get_invite_token(token)

        if not invite:
            return HTMLResponse(_html_page("Invalid Invite", "<h1>Invalid invite</h1><p>This invite link is not valid.</p>"), status_code=404)

        if invite.get("redeemed_by"):
            return HTMLResponse(_html_page("Already Redeemed", "<h1>Already redeemed</h1><p>This invite has already been used.</p>"), status_code=410)

        from datetime import datetime, timezone
        if datetime.fromisoformat(invite["expires_at"]) < datetime.now(timezone.utc):
            return HTMLResponse(_html_page("Expired Invite", "<h1>Invite expired</h1><p>This invite link has expired. Ask for a new one.</p>"), status_code=410)

        inviter = d.get_user(invite["created_by"])
        inviter_name = inviter["name"] if inviter else "Someone"
        instance = d.get_instance_for_owner(invite["created_by"])
        instance_name = instance["name"] if instance else "a Dugg server"
        topic_html = f'<p class="topic">{instance["topic"]}</p>' if instance and instance.get("topic") else ""
        name_hint = invite.get("name_hint", "")

        body = f"""
<h1>{inviter_name} invited you to {instance_name}</h1>
{topic_html}
<form method="POST" action="/invite/{token}/redeem">
  <label for="name">Your name</label>
  <input type="text" id="name" name="name" value="{name_hint}" placeholder="Your name" required>
  <button type="submit">Join</button>
</form>"""
        return HTMLResponse(_html_page(f"Join {instance_name}", body))

    async def handle_invite_redeem(request: Request):
        """POST /invite/{token}/redeem — process the invite redemption."""
        token = request.path_params["token"]
        d = get_db()

        # Accept both form-encoded and JSON
        content_type = request.headers.get("content-type", "")
        if "application/json" in content_type:
            body = await request.body()
            data = json.loads(body)
            name = data.get("name", "")
        else:
            form = await request.form()
            name = form.get("name", "")

        if not name:
            invite = d.get_invite_token(token)
            name = invite["name_hint"] if invite and invite.get("name_hint") else "New User"

        result = d.redeem_invite_token(token, name)

        if not result:
            if "application/json" in content_type:
                return JSONResponse({"error": "Invalid, expired, or already-redeemed invite token"}, status_code=400)
            return HTMLResponse(_html_page("Error", '<h1>Could not redeem</h1><p class="error">This invite is invalid, expired, or already used.</p>'), status_code=400)

        user = result["user"]
        agent = result["agent"]
        invite_info = result["invite"]
        inviter = d.get_user(invite_info["created_by"])
        instance = d.get_instance_for_owner(invite_info["created_by"])
        endpoint = instance.get("endpoint_url", "").rstrip("/") if instance else ""

        if "application/json" in content_type:
            return JSONResponse({
                "status": "redeemed",
                "user": {"id": user["id"], "name": user["name"], "api_key": user["api_key"]},
                "agent": {"id": agent["id"], "name": agent["name"], "api_key": agent["api_key"]},
                "feed_url": f"{endpoint}/feed/{user['api_key']}" if endpoint else None,
                "instructions": (
                    f"Two keys were issued. The user key ({user['api_key']}) belongs to your human — "
                    f"present it to them and tell them to store it somewhere safe (it won't be shown again). "
                    f"The agent key ({agent['api_key']}) is yours — use it as your X-Dugg-Key header for all "
                    f"future requests. If your human's account gets banned, your key stops working too. "
                    f"Next step: call dugg_welcome with your agent key to get oriented."
                ),
            }, status_code=201)

        feed_url = f"{endpoint}/feed/{user['api_key']}" if endpoint else f"/feed/{user['api_key']}"

        body = f"""
<h1>You're in, {user['name']}!</h1>
<p>Here are your keys — save them somewhere safe, they won't be shown again.</p>
<h3>Your key</h3>
<div class="key-box">{user['api_key']}</div>
<h3>Your agent's key</h3>
<div class="key-box">{agent['api_key']}</div>
<p style="font-size: 0.85em; color: #666;">Give this key to your AI agent. If your account gets banned, your agent goes too.</p>
<ul class="next-steps">
  <h3>What now?</h3>
  <li><strong>Got an AI agent?</strong> Set <code>X-Dugg-Key</code> to the agent key above{f' and point it at <code>{endpoint}</code>' if endpoint else ''}</li>
  <li><strong>Use the CLI?</strong> <code>dugg welcome --key {user['api_key']}</code></li>
  <li><strong>Just want to read?</strong> <a href="{feed_url}">Bookmark your personal feed</a></li>
</ul>"""
        return HTMLResponse(_html_page("Welcome to Dugg", body))

    async def handle_feed(request: Request):
        """GET /feed/{key} — read-only browser-friendly feed view."""
        api_key = request.path_params["key"]
        d = get_db()
        user = d.get_user_by_api_key(api_key)

        if not user:
            return HTMLResponse(_html_page("Not Found", "<h1>Invalid key</h1><p>This feed URL is not valid.</p>"), status_code=404)

        # Check Accept header for Atom/RSS preference
        accept = request.headers.get("accept", "")
        want_atom = "application/atom+xml" in accept or "application/rss+xml" in accept

        feed = d.get_feed(user["id"], limit=50)

        # Get instance context for the page title
        instances = d.list_instances(user["id"])
        page_title = instances[0]["name"] if instances else "Dugg"
        page_topic = instances[0].get("topic", "") if instances else ""

        if want_atom:
            # Simple Atom feed
            entries = ""
            for r in feed:
                title = r.get("title") or r["url"]
                desc = r.get("description", "")
                note = r.get("note", "")
                content = f"{desc}\n\n{note}".strip() if (desc or note) else ""
                entries += f"""<entry>
  <title>{_xml_escape(title)}</title>
  <link href="{_xml_escape(r['url'])}"/>
  <id>{_xml_escape(r['id'])}</id>
  <updated>{r['created_at']}</updated>
  <summary>{_xml_escape(content)}</summary>
</entry>\n"""
            atom = f"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>{_xml_escape(page_title)}</title>
  <updated>{feed[0]['created_at'] if feed else ''}</updated>
{entries}</feed>"""
            return HTMLResponse(atom, media_type="application/atom+xml")

        # HTML feed view
        if not feed:
            items_html = '<p class="empty">Nothing here yet. Check back later.</p>'
        else:
            items_html = ""
            for r in feed:
                title = r.get("title") or r["url"]
                note_html = f'<p class="note">{r["note"]}</p>' if r.get("note") else ""
                author_html = f' · {r["author"]}' if r.get("author") else ""
                items_html += f"""<div class="feed-item">
  <h3><a href="{r['url']}" target="_blank" rel="noopener">{title}</a></h3>
  <p class="meta">{r['created_at'][:10]}{author_html}</p>
  {note_html}
</div>\n"""

        topic_html = f'<p class="topic">{page_topic}</p>' if page_topic else ""
        body = f"""<h1>{page_title}</h1>
{topic_html}
{items_html}"""
        return HTMLResponse(_html_page(page_title, body))

    # --- Events SSE stream ---

    async def handle_events_stream(request: Request):
        """GET /events/stream — SSE stream of Dugg events (not MCP protocol).

        Auth: X-Dugg-Key header.
        Query params: types (comma-separated event types), since (ISO timestamp).
        """
        try:
            user = resolve_user_from_request(request)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=401)

        from starlette.responses import StreamingResponse

        event_types = request.query_params.get("types", "").split(",") if request.query_params.get("types") else []
        since = request.query_params.get("since", "")

        async def event_generator():
            last_since = since
            d = get_db()
            while True:
                events = d.get_events(
                    user["id"],
                    event_types=event_types or None,
                    since=last_since or None,
                    limit=50,
                )
                for event in events:
                    data = json.dumps({
                        "id": event["id"],
                        "event_type": event["event_type"],
                        "payload": json.loads(event["payload"]) if isinstance(event["payload"], str) else event["payload"],
                        "created_at": event["created_at"],
                    })
                    yield f"data: {data}\n\n"
                    last_since = event["created_at"]

                await asyncio.sleep(5)

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # --- Lifecycle ---

    @asynccontextmanager
    async def lifespan(app):
        d = get_db()
        sync_task = start_sync_daemon(d, interval=30)
        logger.info("Dugg HTTP server started — sync daemon running")
        try:
            yield
        finally:
            sync_task.cancel()
            if db:
                db.close()
            logger.info("Dugg HTTP server shut down")

    # --- App assembly ---

    routes = [
        Route("/sse", endpoint=handle_sse),
        Route("/messages", endpoint=handle_messages, methods=["POST"]),
        Route("/ingest", endpoint=handle_ingest, methods=["POST"]),
        Route("/health", endpoint=handle_health),
        Route("/invite/{token}", endpoint=handle_invite_page),
        Route("/invite/{token}/redeem", endpoint=handle_invite_redeem, methods=["POST"]),
        Route("/feed/{key}", endpoint=handle_feed),
        Route("/events/stream", endpoint=handle_events_stream),
        Route("/tools/{tool_name}", endpoint=handle_tools, methods=["POST"]),
    ]

    app = Starlette(
        routes=routes,
        lifespan=lifespan,
    )

    return app


def run_http(host: str = "0.0.0.0", port: int = 8411, db_path: Optional[Path] = None):
    """Run the Dugg HTTP server with uvicorn."""
    import uvicorn

    app = create_app(db_path=db_path)
    uvicorn.run(app, host=host, port=port, log_level="info")
