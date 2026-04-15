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
        api_key = request.headers.get("x-dugg-key", "")
        if not api_key:
            return JSONResponse({"error": "Missing X-Dugg-Key header"}, status_code=401)
        d = get_db()
        user = d.get_user_by_api_key(api_key)
        if not user:
            return JSONResponse({"error": "Invalid API key"}, status_code=401)
        d.mark_invite_onboarded(user["id"])
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

        source_server = payload.get("source_server", "")
        result = d.ingest_remote_publish(resource_data, source_instance_id, coll_id, source_server=source_server)
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

    async def handle_bootstrap(request: Request):
        """POST /bootstrap — create the first admin user when DB has zero users."""
        d = get_db()
        count = d.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        if count > 0:
            return JSONResponse({"error": "Database already has users — bootstrap is disabled"}, status_code=400)
        try:
            body = await request.body()
            data = json.loads(body) if body else {}
        except json.JSONDecodeError:
            return JSONResponse({"error": "Invalid JSON"}, status_code=400)
        name = data.get("name", "Admin")
        user = d.create_user(name)
        return JSONResponse({
            "status": "bootstrapped",
            "user": {"id": user["id"], "name": user["name"], "api_key": user["api_key"]},
            "message": "First user created. Save this API key — it won't be shown again.",
        }, status_code=201)

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

        get_db().mark_invite_onboarded(user["id"])

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
  .step {{ display: flex; gap: 1rem; margin-bottom: 1.25rem; }}
  .step-num {{ flex-shrink: 0; width: 28px; height: 28px; background: #6366f1; color: #fff;
               border-radius: 50%; display: flex; align-items: center; justify-content: center;
               font-size: 0.85rem; font-weight: 700; margin-top: 0.1rem; }}
  .step-body {{ flex: 1; }}
  .step-body p {{ font-size: 0.85rem; color: #aaa; margin: 0.25rem 0 0.5rem; }}
  .step-example {{ background: #111; border: 1px solid #333; border-radius: 6px; padding: 0.75rem;
                   margin-top: 0.5rem; }}
  .step-example code {{ font-size: 0.8rem; color: #4ade80; display: block; word-break: break-all; }}
  .step-label {{ font-size: 0.75rem; color: #666; margin-bottom: 0.25rem; text-transform: uppercase;
                 letter-spacing: 0.03em; }}
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
            accept = request.headers.get("accept", "")
            if "application/json" in accept:
                return JSONResponse({"error": "Invalid invite token"}, status_code=404)
            return HTMLResponse(_html_page("Invalid Invite", "<h1>Invalid invite</h1><p>This invite link is not valid.</p>"), status_code=404)

        if invite.get("redeemed_by"):
            if invite.get("onboarded_at"):
                accept = request.headers.get("accept", "")
                if "application/json" in accept:
                    return JSONResponse({"error": "This invite has already been redeemed"}, status_code=410)
                return HTMLResponse(_html_page("Already Redeemed", "<h1>Already redeemed</h1><p>This invite has already been used.</p>"), status_code=410)
            # Not yet onboarded — show welcome page with keys so they can retrieve them
            user = d.get_user(invite["redeemed_by"])
            agents = d.get_agents_for_user(invite["redeemed_by"])
            agent = agents[0] if agents else None
            if not user or not agent:
                accept = request.headers.get("accept", "")
                if "application/json" in accept:
                    return JSONResponse({"error": "This invite has already been redeemed"}, status_code=410)
                return HTMLResponse(_html_page("Already Redeemed", "<h1>Already redeemed</h1><p>This invite has already been used.</p>"), status_code=410)
            inviter = d.get_user(invite["created_by"])
            instance = d.get_instance_for_owner(invite["created_by"])
            endpoint = instance.get("endpoint_url", "").rstrip("/") if instance else ""
            if not endpoint:
                endpoint = d.get_config("server_url", "")
            accept = request.headers.get("accept", "")
            if "application/json" in accept:
                server_url_json = endpoint or ""
                return JSONResponse({
                    "status": "redeemed_pending_onboarding",
                    "user": {"id": user["id"], "name": user["name"], "api_key": user["api_key"]},
                    "agent": {"id": agent["id"], "name": agent["name"], "api_key": agent["api_key"]},
                    "endpoints": {
                        "sse": f"{server_url_json}/sse" if server_url_json else None,
                        "feed": f"{server_url_json}/feed/{user['api_key']}" if server_url_json else None,
                        "health": f"{server_url_json}/health" if server_url_json else None,
                    },
                    "message": "Invite already redeemed. Keys shown again because onboarding is not yet complete. Connect to the server via SSE or make an authenticated tool call to finalize.",
                })
            feed_url = f"{endpoint}/feed/{user['api_key']}" if endpoint else f"/feed/{user['api_key']}"
            body = f"""
<h1>Welcome back, {_xml_escape(user['name'])}!</h1>
<p>You've already redeemed this invite. Here are your keys again — once your agent connects to the server (via SSE or a tool call), this page will lock.</p>
<h3>Your key</h3>
<div class="key-box">{user['api_key']}</div>
<h3>Your agent's key</h3>
<div class="key-box">{agent['api_key']}</div>
<p style="font-size: 0.85em; color: #666;">Give the agent key to your AI agent. If your account gets banned, your agent goes too.</p>
<div class="next-steps">
  <p style="color: #93c5fd; font-size: 1.1em;">Connect your agent to the server to complete onboarding.</p>
  <p style="font-size: 0.85em; color: #666; margin-top: 0.5rem;">Once your agent makes its first SSE connection or tool call, this page will lock and keys won't be shown again.</p>
</div>"""
            return HTMLResponse(_html_page("Welcome Back", body))

        from datetime import datetime, timezone
        if datetime.fromisoformat(invite["expires_at"]) < datetime.now(timezone.utc):
            accept = request.headers.get("accept", "")
            if "application/json" in accept:
                return JSONResponse({"error": "This invite has expired"}, status_code=410)
            return HTMLResponse(_html_page("Expired Invite", "<h1>Invite expired</h1><p>This invite link has expired. Ask for a new one.</p>"), status_code=410)

        inviter = d.get_user(invite["created_by"])
        inviter_name = inviter["name"] if inviter else "Someone"
        instance = d.get_instance_for_owner(invite["created_by"])
        instance_name = instance["name"] if instance else "a Dugg server"

        accept = request.headers.get("accept", "")
        if "application/json" in accept:
            endpoint = instance.get("endpoint_url", "").rstrip("/") if instance else ""
            if not endpoint:
                endpoint = d.get_config("server_url", "")
            return JSONResponse({
                "status": "pending",
                "invite": {
                    "token": token,
                    "invited_by": inviter_name,
                    "instance": instance_name,
                    "topic": instance.get("topic") if instance else None,
                    "name_hint": invite.get("name_hint", ""),
                    "expires_at": invite["expires_at"],
                },
                "redeem": {
                    "method": "POST",
                    "url": f"{endpoint}/invite/{token}/redeem",
                    "content_type": "application/json",
                    "body": {"name": "Your Name"},
                    "description": "POST this to redeem the invite. The response contains your user key (for your human) and agent key (for API/MCP access).",
                },
                "after_redeem": {
                    "mcp_connect": {
                        "transport": "sse",
                        "url": f"{endpoint}/sse" if endpoint else None,
                        "auth_header": "X-Dugg-Key: <agent_api_key from redeem response>",
                    },
                    "first_call": "dugg_welcome — returns instance orientation, recent activity, and rate limits in one call.",
                    "partner_guide": "https://github.com/kadedworkin/dugg-fyi/blob/main/PARTNER_AGENT.md",
                    "partner_guide_note": "Read this before your first submission. Covers norms, rate limits, catchup patterns, and how to stay active.",
                    "feed_url": f"{endpoint}/feed/<user_api_key from redeem response>" if endpoint else None,
                    "health": f"{endpoint}/health" if endpoint else None,
                },
            })

        topic_html = f'<p class="topic">{instance["topic"]}</p>' if instance and instance.get("topic") else ""
        name_hint = invite.get("name_hint", "")

        body = f"""
<h1>{inviter_name} invited you to {instance_name}</h1>
{topic_html}
<form method="POST" action="/invite/{token}/redeem">
  <label for="name">Your name</label>
  <input type="text" id="name" name="name" value="{name_hint}" placeholder="Your name" required maxlength="100">
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
        if not endpoint:
            endpoint = d.get_config("server_url", "")

        if "application/json" in content_type:
            server_url_json = endpoint or ""
            return JSONResponse({
                "status": "redeemed",
                "user": {"id": user["id"], "name": user["name"], "api_key": user["api_key"]},
                "agent": {"id": agent["id"], "name": agent["name"], "api_key": agent["api_key"]},
                "endpoints": {
                    "sse": f"{server_url_json}/sse" if server_url_json else None,
                    "tools": f"{server_url_json}/tools/{{tool_name}}" if server_url_json else None,
                    "health": f"{server_url_json}/health" if server_url_json else None,
                    "feed": f"{server_url_json}/feed/{user['api_key']}" if server_url_json else None,
                },
                "instructions": (
                    f"Two keys were issued. The user key ({user['api_key']}) belongs to your human — "
                    f"present it to them and tell them to store it somewhere safe (it won't be shown again). "
                    f"The agent key ({agent['api_key']}) is yours — use it as your X-Dugg-Key header for all "
                    f"future requests. If your human's account gets banned, your key stops working too. "
                    f"IMPORTANT: Read PARTNER_AGENT.md in the Dugg repo before your first submission — "
                    f"it covers behavioral norms, rate limits, catchup patterns, and how to be a good citizen."
                ),
                "quickstart": [
                    {
                        "step": 1,
                        "action": "Set up your local Dugg",
                        "commands": ["git clone https://github.com/kadedworkin/dugg-fyi.git", "cd dugg-fyi && uv sync", "dugg init", "dugg serve"],
                        "what_happens": "Creates your personal knowledge base. Starts empty — the shared server is where value comes from first.",
                    },
                    {
                        "step": 2,
                        "action": "Explore what's already here",
                        "tools": ["dugg_welcome", "dugg_feed", "dugg_search", "dugg_react"],
                        "what_happens": "Day-one value: browse what others have shared, search for topics, react to signal value. Use dugg_catchup later for incremental updates.",
                    },
                ],
            }, status_code=201)

        feed_url = f"{endpoint}/feed/{user['api_key']}" if endpoint else f"/feed/{user['api_key']}"
        server_url = endpoint or ""

        body = f"""
<h1>You're in, {_xml_escape(user['name'])}!</h1>
<p>Here are your keys — save them somewhere safe, they won't be shown again.</p>
<h3>Your key</h3>
<div class="key-box">{user['api_key']}</div>
<h3>Your agent's key</h3>
<div class="key-box">{agent['api_key']}</div>
<p style="font-size: 0.85em; color: #666;">Give this key to your AI agent. If your account gets banned, your agent goes too.</p>

<div class="next-steps">
  <h3>Get started in 3 steps</h3>

  <div class="step">
    <div class="step-num">1</div>
    <div class="step-body">
      <strong>Add your first link</strong>
      <p>Share something useful — a doc, article, video, whatever. Dugg grabs the title, description, and transcript automatically.</p>
      <div class="step-example">
        <div class="step-label">If you have an AI agent:</div>
        <code>"Dugg this: https://example.com/cool-article — worth reading for the pricing breakdown"</code>
        <div class="step-label" style="margin-top: 0.5rem;">Via the API:</div>
        <code>POST {server_url}/tools/dugg_add<br>X-Dugg-Key: {agent['api_key']}<br>{{"url": "https://example.com", "note": "why this matters"}}</code>
      </div>
    </div>
  </div>

  <div class="step">
    <div class="step-num">2</div>
    <div class="step-body">
      <strong>Search for it</strong>
      <p>Dugg indexes everything — titles, descriptions, transcripts, your notes. Full-text search across all of it.</p>
      <div class="step-example">
        <div class="step-label">Ask your agent:</div>
        <code>"Search Dugg for pricing"</code>
        <div class="step-label" style="margin-top: 0.5rem;">Via the API:</div>
        <code>POST {server_url}/tools/dugg_search<br>X-Dugg-Key: {agent['api_key']}<br>{{"query": "pricing"}}</code>
      </div>
    </div>
  </div>

  <div class="step">
    <div class="step-num">3</div>
    <div class="step-body">
      <strong>Browse your feed</strong>
      <p>Everything you and others have shared, in one place. No agent needed — works in any browser.</p>
      <div class="step-example">
        <a href="{feed_url}" style="color: #93c5fd;">Open your personal feed &rarr;</a>
      </div>
    </div>
  </div>

  <h3 style="margin-top: 1.5rem;">Staying updated</h3>
  <p style="font-size: 0.85rem; color: #aaa; margin-bottom: 0.75rem;">New content shows up as others share. Here's how to keep up:</p>
  <div class="step-example">
    <div class="step-label">Bookmark your feed (works in RSS readers too):</div>
    <code><a href="{feed_url}" style="color: #4ade80;">{feed_url}</a></code>
    <div class="step-label" style="margin-top: 0.5rem;">Your agent can poll for updates:</div>
    <code>dugg_catchup — shows everything new since last check</code>
    <div class="step-label" style="margin-top: 0.5rem;">Or get push notifications:</div>
    <code>dugg_webhook_subscribe — sends events to Slack, HTTP endpoints, etc.</code>
  </div>

  <h3 style="margin-top: 1.5rem;">Connecting your agent</h3>
  <p style="font-size: 0.85rem; color: #aaa; margin-bottom: 0.75rem;">Your agent connects via SSE (Server-Sent Events) for real-time communication, or plain HTTP for one-off calls.</p>
  <div class="step-example">
    <div class="step-label">SSE endpoint (real-time):</div>
    <code>{server_url}/sse</code>
    <div class="step-label" style="margin-top: 0.5rem;">Auth header for all requests:</div>
    <code>X-Dugg-Key: {agent['api_key']}</code>
    <div class="step-label" style="margin-top: 0.5rem;">Health check (no auth needed):</div>
    <code>{server_url}/health</code>
  </div>

  <h3 style="margin-top: 1.5rem;">Want the CLI too?</h3>
  <p style="font-size: 0.85rem; color: #aaa; margin-bottom: 0.75rem;">Install Dugg locally to manage resources from your terminal:</p>
  <div class="step-example">
    <code>git clone https://github.com/kadedworkin/dugg-fyi.git</code>
    <code>cd dugg-fyi && uv sync</code>
    <code style="margin-top: 0.5rem;">.venv/bin/dugg welcome --key {user['api_key']}</code>
  </div>
  <p style="font-size: 0.8rem; color: #666; margin-top: 0.5rem;">Don't have uv? <code style="font-size: 0.8rem; color: #888;">curl -LsSf https://astral.sh/uv/install.sh | sh</code></p>
</div>"""
        return HTMLResponse(_html_page("Welcome to Dugg", body))

    async def handle_feed(request: Request):
        """GET /feed/{key} — read-only browser-friendly feed view."""
        api_key = request.path_params["key"]
        d = get_db()
        user = d.get_user_by_api_key(api_key)

        if not user:
            return HTMLResponse(_html_page("Not Found", "<h1>Invalid key</h1><p>This feed URL is not valid.</p>"), status_code=404)

        d.touch_user(user["id"])

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

    # --- Ban Appeal Pages ---

    async def handle_appeal_page(request: Request):
        """GET /appeal/{key} — show the ban appeal submission page.

        The key is the user's API key. If they're banned, they can submit an appeal.
        """
        api_key = request.path_params["key"]
        d = get_db()
        user = d.get_user_by_api_key(api_key)

        if not user:
            return HTMLResponse(_html_page("Not Found", "<h1>Invalid key</h1><p>This appeal URL is not valid.</p>"), status_code=404)

        # Find collections where this user is banned or appealing
        rows = d.conn.execute(
            """SELECT cm.collection_id, cm.status, c.name
               FROM collection_members cm
               JOIN collections c ON cm.collection_id = c.id
               WHERE cm.user_id = ? AND cm.status IN ('banned', 'appealing')""",
            (user["id"],),
        ).fetchall()

        if not rows:
            body = f"""<h1>No active bans</h1>
<p>You're not currently banned from any collections, {_xml_escape(user['name'])}.</p>"""
            return HTMLResponse(_html_page("No Bans", body))

        items_html = ""
        for row in rows:
            row = dict(row)
            coll_name = row["name"]
            coll_id = row["collection_id"]
            status = row["status"]

            if status == "appealing":
                items_html += f"""<div class="feed-item">
  <h3>{coll_name}</h3>
  <p class="meta" style="color: #fbbf24;">Appeal pending — waiting for owner review</p>
</div>\n"""
            else:
                score = d.get_member_credit_score(coll_id, user["id"])
                items_html += f"""<div class="feed-item">
  <h3>{coll_name}</h3>
  <p class="meta">Your credit score: {score['total']} ({score['submissions']} submissions, {score['reactions_received']} reactions)</p>
  <form method="POST" action="/appeal/{api_key}/submit" style="margin-top: 0.5rem;">
    <input type="hidden" name="collection_id" value="{coll_id}">
    <button type="submit">Submit Appeal</button>
  </form>
</div>\n"""

        body = f"""<h1>Ban Appeals</h1>
<p>Hi {_xml_escape(user['name'])}. You can appeal bans below. The collection owner will see your credit score and decide.</p>
{items_html}"""
        return HTMLResponse(_html_page("Ban Appeals", body))

    async def handle_appeal_submit(request: Request):
        """POST /appeal/{key}/submit — submit an appeal for a specific collection."""
        api_key = request.path_params["key"]
        d = get_db()
        user = d.get_user_by_api_key(api_key)

        if not user:
            return HTMLResponse(_html_page("Error", "<h1>Invalid key</h1>"), status_code=404)

        content_type = request.headers.get("content-type", "")
        if "application/json" in content_type:
            body = await request.body()
            data = json.loads(body)
            collection_id = data.get("collection_id", "")
        else:
            form = await request.form()
            collection_id = form.get("collection_id", "")

        if not collection_id:
            return HTMLResponse(_html_page("Error", '<h1>Missing collection</h1><p class="error">No collection specified.</p>'), status_code=400)

        result = d.appeal_ban(collection_id, user["id"])

        if not result:
            if "application/json" in content_type:
                return JSONResponse({"error": "Cannot appeal — you may not be banned in this collection"}, status_code=400)
            return HTMLResponse(_html_page("Cannot Appeal", '<h1>Cannot appeal</h1><p class="error">You can only appeal if you are currently banned.</p>'), status_code=400)

        if "application/json" in content_type:
            return JSONResponse({"status": "appealing", **result}, status_code=201)

        body = f"""<h1>Appeal submitted</h1>
<p>Your appeal has been submitted. The collection owner will review it along with your credit score.</p>
<p style="margin-top: 1rem;"><a href="/appeal/{api_key}" style="color: #93c5fd;">Back to appeals</a></p>"""
        return HTMLResponse(_html_page("Appeal Submitted", body))

    async def handle_appeal_status(request: Request):
        """GET /appeal/{key}/status — JSON endpoint for checking appeal status."""
        api_key = request.path_params["key"]
        d = get_db()
        user = d.get_user_by_api_key(api_key)

        if not user:
            return JSONResponse({"error": "Invalid key"}, status_code=404)

        rows = d.conn.execute(
            """SELECT cm.collection_id, cm.status, c.name
               FROM collection_members cm
               JOIN collections c ON cm.collection_id = c.id
               WHERE cm.user_id = ? AND cm.status IN ('banned', 'appealing')""",
            (user["id"],),
        ).fetchall()

        return JSONResponse({
            "user_id": user["id"],
            "bans": [{"collection_id": r["collection_id"], "name": r["name"], "status": r["status"]} for r in rows],
        })

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

    # --- Slack slash command ---

    async def handle_slack_command(request: Request):
        """Handle Slack slash command: /dugg or /dugg <url> [note]"""
        d = get_db()
        form = await request.form()
        text = (form.get("text") or "").strip()
        slack_user = form.get("user_name", "someone")

        # Verify signing secret if configured
        signing_secret = d.get_config("slack_signing_secret", "")
        if signing_secret:
            import time
            timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
            slack_sig = request.headers.get("X-Slack-Signature", "")
            if abs(time.time() - int(timestamp or 0)) > 300:
                return JSONResponse({"text": "Request too old."}, status_code=403)
            sig_basestring = f"v0:{timestamp}:{(await request.body()).decode()}"
            my_sig = "v0=" + hmac.new(signing_secret.encode(), sig_basestring.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(my_sig, slack_sig):
                return JSONResponse({"text": "Invalid signature."}, status_code=403)

        # Find or create a user for this Slack user
        # Look up by name match first; if not found, use the first admin user
        rows = d.conn.execute("SELECT id, name, api_key FROM users").fetchall()
        user = None
        for r in rows:
            if r["name"].lower() == slack_user.lower():
                user = dict(r)
                break
        if not user and rows:
            user = dict(rows[0])

        if not user:
            return JSONResponse({"response_type": "ephemeral", "text": "No users on this Dugg server yet."})

        # /dugg with no args → show feed
        if not text:
            feed = d.get_feed(user["id"], limit=5)
            if not feed:
                return JSONResponse({"response_type": "ephemeral", "text": "Feed is empty. Add something with `/dugg https://...`"})
            names = {r["id"]: r["name"] for r in d.conn.execute("SELECT id, name FROM users").fetchall()}
            lines = [f"*Latest {len(feed)} resource(s):*\n"]
            for r in feed:
                title = r.get("title") or r["url"]
                added_by = names.get(r.get("submitted_by", ""), "")
                source = r.get("source_server", "")
                date = r.get("created_at", "")[:10]
                lines.append(f"*{_xml_escape(title)}*")
                lines.append(f"<{r['url']}>")
                meta = []
                if added_by:
                    meta.append(f"by {added_by}")
                if source:
                    meta.append(f"from {source}")
                if date:
                    meta.append(date)
                if meta:
                    lines.append(" · ".join(meta))
                if r.get("note"):
                    lines.append(f"_{_xml_escape(r['note'])}_")
                lines.append("")
            return JSONResponse({"response_type": "in_channel", "text": "\n".join(lines)})

        # /dugg <url> [--note ...] → add resource
        url = text.split()[0].strip("<>")
        if url.startswith("http://") or url.startswith("https://"):
            note = ""
            rest = text[len(text.split()[0]):].strip()
            if rest.startswith("--note "):
                note = rest[7:].strip().strip('"\'')
            elif rest:
                note = rest

            # Ensure user has a default collection
            collections = d.list_collections(user["id"])
            coll_id = None
            for c in collections:
                if c["name"] == "Default":
                    coll_id = c["id"]
                    break
            if not coll_id:
                result = d.create_collection("Default", user["id"], description="Default collection", visibility="private")
                coll_id = result["id"]

            try:
                from dugg.enrichment import enrich_url
                enriched = await enrich_url(url)
            except Exception:
                enriched = {}

            resource = d.add_resource(
                url=url,
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
            )
            d.wait_for_webhooks()

            title = resource.get("title") or url
            resp_lines = [f"Added *{_xml_escape(title)}*", f"<{url}>"]
            if note:
                resp_lines.append(f"_{_xml_escape(note)}_")
            return JSONResponse({"response_type": "in_channel", "text": "\n".join(resp_lines)})

        # /dugg <search query> → search
        results = d.search(text, user["id"], limit=5)
        if not results:
            return JSONResponse({"response_type": "ephemeral", "text": f'No results for "{_xml_escape(text)}"'})
        names = {r["id"]: r["name"] for r in d.conn.execute("SELECT id, name FROM users").fetchall()}
        lines = [f'*{len(results)} result(s) for "{_xml_escape(text)}":*\n']
        for r in results:
            title = r.get("title") or r["url"]
            added_by = names.get(r.get("submitted_by", ""), "")
            lines.append(f"*{_xml_escape(title)}*")
            lines.append(f"<{r['url']}>")
            if added_by:
                lines.append(f"by {added_by}")
            if r.get("note"):
                lines.append(f"_{_xml_escape(r['note'])}_")
            lines.append("")
        return JSONResponse({"response_type": "in_channel", "text": "\n".join(lines)})

    # --- Browser admin panel ---

    def _admin_resolve_user(request: Request):
        """Resolve user from URL path key param."""
        key = request.path_params.get("key", "")
        d = get_db()
        user = d.get_user_by_api_key(key)
        return d, user

    # --- Paste Pages ---

    async def handle_paste_page(request: Request):
        """GET /paste/{key} — browser form to paste raw content."""
        api_key = request.path_params["key"]
        d = get_db()
        user = d.get_user_by_api_key(api_key)

        if not user:
            return HTMLResponse(_html_page("Not Found", "<h1>Invalid key</h1><p>This paste URL is not valid.</p>"), status_code=404)

        instances = d.list_instances(user["id"])
        page_title = instances[0]["name"] if instances else "Dugg"

        body = f"""<h1>Paste Content</h1>
<p class="topic">Add raw content to {_xml_escape(page_title)} — no URL needed.</p>
<form method="POST" action="/paste/{_xml_escape(api_key)}/submit" enctype="multipart/form-data">
  <label for="title">Title</label>
  <input type="text" id="title" name="title" placeholder="e.g. Substack newsletter from Rocco" required>
  <label for="body">Content</label>
  <textarea id="body" name="body" rows="12" placeholder="Paste the content here..." style="width:100%;padding:0.6rem;background:#111;border:1px solid #444;border-radius:6px;color:#fff;font-size:0.9rem;margin-bottom:1rem;resize:vertical;font-family:inherit;"></textarea>
  <label for="file">Or upload a file (.txt, .html, .md)</label>
  <input type="file" id="file" name="file" accept=".txt,.html,.htm,.md,.eml" style="margin-bottom:1rem;color:#aaa;font-size:0.85rem;">
  <label for="source_type">Content type</label>
  <select id="source_type" name="source_type" style="width:100%;padding:0.6rem;background:#111;border:1px solid #444;border-radius:6px;color:#fff;font-size:1rem;margin-bottom:1rem;">
    <option value="paste">Paste</option>
    <option value="email">Email / Newsletter</option>
    <option value="document">Document</option>
  </select>
  <label for="source_label">Source (optional)</label>
  <input type="text" id="source_label" name="source_label" placeholder="e.g. Substack, meeting notes">
  <label for="tags">Tags (comma-separated, optional)</label>
  <input type="text" id="tags" name="tags" placeholder="e.g. newsletter, ai, weekly">
  <label for="note">Note (optional)</label>
  <input type="text" id="note" name="note" placeholder="Why this matters...">
  <button type="submit">Save to Dugg</button>
</form>"""
        return HTMLResponse(_html_page(f"Paste — {page_title}", body))

    async def handle_paste_submit(request: Request):
        """POST /paste/{key}/submit — process paste form submission."""
        api_key = request.path_params["key"]
        d = get_db()
        user = d.get_user_by_api_key(api_key)

        if not user:
            return HTMLResponse(_html_page("Error", "<h1>Invalid key</h1>"), status_code=404)

        d.touch_user(user["id"])

        form = await request.form()
        title = (form.get("title") or "").strip()
        body = (form.get("body") or "").strip()
        source_type = form.get("source_type", "paste")
        source_label = (form.get("source_label") or "").strip()
        tags_raw = (form.get("tags") or "").strip()
        note = (form.get("note") or "").strip()

        uploaded = form.get("file")
        if uploaded and hasattr(uploaded, "read"):
            file_content = (await uploaded.read()).decode("utf-8", errors="replace").strip()
            if file_content and not body:
                body = file_content

        if not title:
            return HTMLResponse(_html_page("Error", '<h1>Missing title</h1><p><a href="javascript:history.back()">Go back</a></p>'), status_code=400)
        if not body:
            return HTMLResponse(_html_page("Error", '<h1>Missing content</h1><p>Paste some text or upload a file.</p><p><a href="javascript:history.back()">Go back</a></p>'), status_code=400)

        coll_id = _ensure_default_collection(d, user["id"])
        tags = [t.strip() for t in tags_raw.split(",") if t.strip()]

        from dugg.db import _uuid
        res_id = _uuid()
        synthetic_url = f"dugg://paste/{res_id}"
        metadata = {"source_label": source_label} if source_label else {}

        resource = d.add_resource(
            url=synthetic_url,
            collection_id=coll_id,
            submitted_by=user["id"],
            note=note,
            title=title,
            description=source_label,
            source_type=source_type,
            transcript=body,
            raw_metadata=metadata,
            tags=tags,
            tag_source="human" if tags else "agent",
        )

        word_count = len(body.split())
        success_body = f"""<h1>Saved</h1>
<div class="key-box">
  <strong>{_xml_escape(title)}</strong><br>
  ID: {resource['id']}<br>
  Type: {source_type}<br>
  Content: {word_count} words
</div>
<p style="margin-top:1rem;"><a href="/paste/{_xml_escape(api_key)}" style="color:#93c5fd;">Paste another</a></p>"""
        return HTMLResponse(_html_page("Saved", success_body))

    async def handle_admin_page(request: Request):
        """GET /admin/{key} — browser-based admin dashboard."""
        d, user = _admin_resolve_user(request)
        if not user:
            return HTMLResponse(_html_page("Unauthorized", "<h1>Invalid API key</h1><p>Check your admin URL.</p>"), status_code=401)

        key = request.path_params["key"]
        collections = d.list_collections(user["id"])
        server_url = d.get_config("server_url", "")

        # Gather members and resources per collection
        sections = []
        for c in collections:
            member = d.get_member_status(c["id"], user["id"])
            is_owner = member and member["role"] == "owner"

            # Members
            members = d.conn.execute(
                "SELECT cm.user_id, cm.role, cm.status, u.name FROM collection_members cm JOIN users u ON cm.user_id = u.id WHERE cm.collection_id = ? ORDER BY cm.joined_at",
                (c["id"],)
            ).fetchall()

            member_html = ""
            for m in members:
                status_badge = ""
                if m["status"] == "banned":
                    status_badge = ' <span style="color:#f87171;">banned</span>'
                elif m["status"] == "appealing":
                    status_badge = ' <span style="color:#fbbf24;">appealing</span>'
                actions = ""
                if is_owner and m["user_id"] != user["id"]:
                    if m["status"] == "active":
                        actions = f' <form method="POST" action="/admin/{key}/ban" style="display:inline;"><input type="hidden" name="collection_id" value="{c["id"]}"><input type="hidden" name="user_id" value="{m["user_id"]}"><button type="submit" style="background:#dc2626;padding:0.2rem 0.5rem;font-size:0.75rem;border-radius:4px;border:none;color:#fff;cursor:pointer;">Ban</button></form>'
                    elif m["status"] in ("banned", "appealing"):
                        actions = f' <form method="POST" action="/admin/{key}/unban" style="display:inline;"><input type="hidden" name="collection_id" value="{c["id"]}"><input type="hidden" name="user_id" value="{m["user_id"]}"><button type="submit" style="background:#16a34a;padding:0.2rem 0.5rem;font-size:0.75rem;border-radius:4px;border:none;color:#fff;cursor:pointer;">Unban</button></form>'
                member_html += f'<div style="padding:0.4rem 0;border-bottom:1px solid #222;display:flex;justify-content:space-between;align-items:center;"><span>{_xml_escape(m["name"])} <span style="color:#666;">({m["role"]})</span>{status_badge}</span>{actions}</div>'

            # Resources
            resources = d.conn.execute(
                "SELECT r.id, r.url, r.title, r.submitted_by, r.created_at, u.name as submitter_name FROM resources r JOIN users u ON r.submitted_by = u.id WHERE r.collection_id = ? ORDER BY r.created_at DESC LIMIT 50",
                (c["id"],)
            ).fetchall()

            resource_html = ""
            for r in resources:
                title = r["title"] or r["url"]
                date = r["created_at"][:10]
                remove_btn = ""
                if is_owner or r["submitted_by"] == user["id"]:
                    remove_btn = f' <form method="POST" action="/admin/{key}/remove" style="display:inline;"><input type="hidden" name="resource_id" value="{r["id"]}"><input type="hidden" name="collection_id" value="{c["id"]}"><button type="submit" style="background:#dc2626;padding:0.15rem 0.4rem;font-size:0.7rem;border-radius:4px;border:none;color:#fff;cursor:pointer;">Remove</button></form>'
                resource_html += f'<div class="feed-item"><h3><a href="{_xml_escape(r["url"])}" target="_blank">{_xml_escape(title)}</a>{remove_btn}</h3><div class="meta">by {_xml_escape(r["submitter_name"])} · {date}</div></div>'

            if not resource_html:
                resource_html = '<div class="empty">No resources yet.</div>'

            owner_tag = " (owner)" if is_owner else ""
            sections.append(f"""
<div style="margin-bottom:2rem;">
  <h2 style="font-size:1.1rem;color:#fff;margin-bottom:0.75rem;">{_xml_escape(c['name'])}{owner_tag}</h2>
  <details style="margin-bottom:1rem;"><summary style="cursor:pointer;color:#aaa;font-size:0.85rem;">Members ({len(members)})</summary><div style="margin-top:0.5rem;">{member_html}</div></details>
  <div>{resource_html}</div>
</div>""")

        health_line = ""
        if server_url:
            health_line = f'<div style="margin-top:1rem;padding-top:1rem;border-top:1px solid #222;font-size:0.8rem;color:#666;">Server: {_xml_escape(server_url)}</div>'

        body = f"""
<h1>Dugg Admin</h1>
<p style="color:#888;margin-bottom:1.5rem;">Logged in as {_xml_escape(user['name'])}</p>
{''.join(sections)}
{health_line}
"""
        return HTMLResponse(_html_page("Dugg Admin", body))

    async def handle_admin_ban(request: Request):
        """POST /admin/{key}/ban — ban a user from a collection."""
        d, user = _admin_resolve_user(request)
        if not user:
            return HTMLResponse(_html_page("Unauthorized", "<h1>Invalid API key</h1>"), status_code=401)
        form = await request.form()
        collection_id = form.get("collection_id", "")
        target_user_id = form.get("user_id", "")
        member = d.get_member_status(collection_id, user["id"])
        if not member or member["role"] != "owner":
            return HTMLResponse(_html_page("Forbidden", "<h1>Not the collection owner</h1>"), status_code=403)
        d.conn.execute("UPDATE collection_members SET status = 'banned' WHERE collection_id = ? AND user_id = ?", (collection_id, target_user_id))
        d.conn.commit()
        key = request.path_params["key"]
        from starlette.responses import RedirectResponse
        return RedirectResponse(f"/admin/{key}", status_code=303)

    async def handle_admin_unban(request: Request):
        """POST /admin/{key}/unban — unban a user."""
        d, user = _admin_resolve_user(request)
        if not user:
            return HTMLResponse(_html_page("Unauthorized", "<h1>Invalid API key</h1>"), status_code=401)
        form = await request.form()
        collection_id = form.get("collection_id", "")
        target_user_id = form.get("user_id", "")
        member = d.get_member_status(collection_id, user["id"])
        if not member or member["role"] != "owner":
            return HTMLResponse(_html_page("Forbidden", "<h1>Not the collection owner</h1>"), status_code=403)
        d.conn.execute("UPDATE collection_members SET status = 'active' WHERE collection_id = ? AND user_id = ?", (collection_id, target_user_id))
        d.conn.commit()
        key = request.path_params["key"]
        from starlette.responses import RedirectResponse
        return RedirectResponse(f"/admin/{key}", status_code=303)

    async def handle_admin_remove(request: Request):
        """POST /admin/{key}/remove — remove a resource."""
        d, user = _admin_resolve_user(request)
        if not user:
            return HTMLResponse(_html_page("Unauthorized", "<h1>Invalid API key</h1>"), status_code=401)
        form = await request.form()
        resource_id = form.get("resource_id", "")
        collection_id = form.get("collection_id", "")
        resource = d.get_resource(resource_id)
        if not resource:
            return HTMLResponse(_html_page("Not Found", "<h1>Resource not found</h1>"), status_code=404)
        member = d.get_member_status(collection_id, user["id"])
        is_owner = member and member["role"] == "owner"
        is_submitter = resource.get("submitted_by") == user["id"]
        if not is_owner and not is_submitter:
            return HTMLResponse(_html_page("Forbidden", "<h1>Permission denied</h1>"), status_code=403)
        d.conn.execute("DELETE FROM publish_queue WHERE resource_id = ?", (resource_id,))
        d.conn.execute("DELETE FROM resources WHERE id = ?", (resource_id,))
        d.conn.commit()
        key = request.path_params["key"]
        from starlette.responses import RedirectResponse
        return RedirectResponse(f"/admin/{key}", status_code=303)

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
        Route("/bootstrap", endpoint=handle_bootstrap, methods=["POST"]),
        Route("/invite/{token}", endpoint=handle_invite_page),
        Route("/invite/{token}/redeem", endpoint=handle_invite_redeem, methods=["POST"]),
        Route("/feed/{key}", endpoint=handle_feed),
        Route("/paste/{key}", endpoint=handle_paste_page),
        Route("/paste/{key}/submit", endpoint=handle_paste_submit, methods=["POST"]),
        Route("/appeal/{key}", endpoint=handle_appeal_page),
        Route("/appeal/{key}/submit", endpoint=handle_appeal_submit, methods=["POST"]),
        Route("/appeal/{key}/status", endpoint=handle_appeal_status),
        Route("/events/stream", endpoint=handle_events_stream),
        Route("/tools/{tool_name}", endpoint=handle_tools, methods=["POST"]),
        Route("/slack/command", endpoint=handle_slack_command, methods=["POST"]),
        Route("/admin/{key}", endpoint=handle_admin_page),
        Route("/admin/{key}/ban", endpoint=handle_admin_ban, methods=["POST"]),
        Route("/admin/{key}/unban", endpoint=handle_admin_unban, methods=["POST"]),
        Route("/admin/{key}/remove", endpoint=handle_admin_remove, methods=["POST"]),
    ]

    app = Starlette(
        routes=routes,
        lifespan=lifespan,
    )

    return app


def run_http(host: str = "0.0.0.0", port: int = 8411, db_path: Optional[Path] = None):
    """Run the Dugg HTTP server with uvicorn."""
    import uvicorn

    # Auto-detect server_url if not already configured
    _path = db_path or (Path(os.environ["DUGG_DB_PATH"]) if os.environ.get("DUGG_DB_PATH") else None)
    if _path:
        _db = DuggDB(_path)
        if not _db.get_config("server_url"):
            display_host = "localhost" if host in ("0.0.0.0", "::") else host
            inferred = f"http://{display_host}:{port}"
            _db.set_config("server_url", inferred)
            logger.info("Auto-set server_url to %s (override with 'dugg set-url')", inferred)
        _db.close()

    app = create_app(db_path=db_path)
    uvicorn.run(app, host=host, port=port, log_level="info")
