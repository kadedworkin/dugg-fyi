"""HTTP/SSE transport for the Dugg MCP server.

Provides three things:
1. MCP SSE transport — standard MCP protocol over HTTP (GET /sse, POST /messages)
2. REST ingest endpoint — POST /ingest for receiving published resources from remote instances
3. Health check — GET /health

Uses Starlette (already a transitive dep of mcp[sse]) and uvicorn.
"""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
from typing import Optional

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

from mcp.server.sse import SseServerTransport

from dugg.db import DuggDB
from dugg.sync import start_sync_daemon
from dugg.rss import start_rss_daemon

logger = logging.getLogger("dugg.http")


def _problem_response(status: int, detail: str, headers: dict | None = None, **extra) -> JSONResponse:
    """Return an RFC 7807 Problem Details JSON response.

    See https://www.rfc-editor.org/rfc/rfc7807
    """
    status_titles = {
        400: "Bad Request",
        401: "Unauthorized",
        403: "Forbidden",
        404: "Not Found",
        410: "Gone",
        429: "Too Many Requests",
        500: "Internal Server Error",
    }
    body = {
        "type": "about:blank",
        "title": status_titles.get(status, "Error"),
        "status": status,
        "detail": detail,
        **extra,
    }
    return JSONResponse(body, status_code=status, headers=headers,
                        media_type="application/problem+json")


def _seconds_until_utc_midnight() -> int:
    """Seconds remaining until the next UTC midnight (rate limit reset)."""
    now = datetime.now(timezone.utc)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    midnight += timedelta(days=1)
    return max(1, int((midnight - now).total_seconds()))


def _xml_escape(s: str) -> str:
    """Escape XML special characters."""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _short_date(value) -> str:
    if not value:
        return ""
    s = str(value).strip()
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        return s[:10]
    return ""


def _resolve_display_url(url: str, server_url: str = "") -> str:
    """Resolve dugg:// internal URLs to web-accessible /r/ URLs."""
    if url.startswith("dugg://content/"):
        resource_id = url.removeprefix("dugg://content/")
        if server_url:
            return f"{server_url.rstrip('/')}/r/{resource_id}"
        return f"/r/{resource_id}"
    return url


def _resource_pub_date(resource: dict) -> str:
    """Pull a publication date (YYYY-MM-DD) out of the resource's raw_metadata, if any."""
    raw = resource.get("raw_metadata")
    if not raw:
        return ""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return ""
    if not isinstance(raw, dict):
        return ""
    return _short_date(raw.get("published_at") or raw.get("updated_at"))


def create_app(db_path: Optional[Path] = None, mode: str = "local") -> Starlette:
    """Create the Starlette ASGI app with MCP SSE transport and REST endpoints.

    mode: "local" (LAN/dev — /setup available) or "public" (internet-facing — invite-only).
    """

    # --- Shared state ---
    db: Optional[DuggDB] = None
    server_mode: str = mode

    def get_db() -> DuggDB:
        nonlocal db
        if db is None:
            path = db_path or (Path(os.environ["DUGG_DB_PATH"]) if os.environ.get("DUGG_DB_PATH") else None)
            db = DuggDB(path)
        return db

    def _ensure_default_collection(d: DuggDB, user_id: str) -> str:
        """Ensure user has a default collection, return its ID."""
        return d.ensure_default_collection(user_id)

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
            return _problem_response(401, "Missing X-Dugg-Key header")
        d = get_db()
        user = d.get_user_by_api_key(api_key)
        if not user:
            return _problem_response(401, "Invalid API key")
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
            return _problem_response(401, str(e))

        try:
            body = await request.body()
            payload = json.loads(body)
        except (json.JSONDecodeError, Exception):
            return _problem_response(400, "Invalid JSON payload")

        resource_data = payload.get("resource", {})
        source_instance_id = payload.get("source_instance_id", "")
        target = payload.get("target", "")

        if not resource_data.get("url"):
            return _problem_response(400, "Missing resource.url")
        if not source_instance_id:
            return _problem_response(400, "Missing source_instance_id")

        d = get_db()

        # Find a collection to ingest into — use Default
        coll_id = _ensure_default_collection(d, user["id"])

        source_server = payload.get("source_server", "")

        # Resolve submitter: prefer matching user by name from source, fall back to authed user
        submitter_id = user["id"]
        submitter_name = resource_data.get("submitter_name", "")
        if submitter_name:
            match = d.conn.execute("SELECT id FROM users WHERE name = ?", (submitter_name,)).fetchone()
            if match:
                submitter_id = match["id"]

        result = d.ingest_remote_publish(resource_data, source_instance_id, coll_id, source_server=source_server, submitted_by=submitter_id)
        if not result:
            return _problem_response(500, "Ingest failed")

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

    async def handle_delete(request: Request):
        """POST /delete — remove a published resource by URL.

        Mirrors /ingest for CRUD symmetry. Accepts:
        {
            "url": "https://...",
            "source_instance_id": "..."
        }

        Looks up the resource by URL, verifies the requesting user submitted it
        (or is a collection owner), and deletes it. Records a tombstone for
        Atom feed propagation.
        """
        try:
            user = resolve_user_from_request(request)
        except ValueError as e:
            return _problem_response(401, str(e))

        try:
            body = await request.body()
            payload = json.loads(body)
        except (json.JSONDecodeError, Exception):
            return _problem_response(400, "Invalid JSON payload")

        url = (payload.get("url") or "").strip()
        if not url:
            return _problem_response(400, "Missing url")

        d = get_db()

        # Find the resource by URL across collections the user has access to
        accessible = d._accessible_collection_ids(user["id"])
        if not accessible:
            return _problem_response(403, "No accessible collections")

        placeholders = ",".join("?" for _ in accessible)
        row = d.conn.execute(
            f"SELECT id, collection_id, submitted_by, title FROM resources WHERE url = ? AND collection_id IN ({placeholders})",
            [url] + accessible,
        ).fetchone()

        if not row:
            return _problem_response(404, "Resource not found")

        resource = dict(row)

        # Authorization: submitter can delete their own, collection owner can delete any
        member = d.get_member_status(resource["collection_id"], user["id"])
        is_owner = member and member["role"] == "owner"
        is_submitter = resource["submitted_by"] == user["id"]
        if not is_owner and not is_submitter:
            return _problem_response(403, "Only the submitter or collection owner can delete")

        result = d.delete_resource(resource["id"], resource["collection_id"], user["id"])
        if result.get("error"):
            return _problem_response(500, result["error"])

        return JSONResponse({
            "status": "deleted",
            "id": resource["id"],
            "url": url,
            "title": resource.get("title", ""),
        }, status_code=200)

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
            "mode": server_mode,
        })

    async def handle_bootstrap(request: Request):
        """POST /bootstrap — create the first admin user when DB has zero users."""
        d = get_db()
        count = d.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        if count > 0:
            return _problem_response(400, "Database already has users — bootstrap is disabled")
        try:
            body = await request.body()
            data = json.loads(body) if body else {}
        except json.JSONDecodeError:
            return _problem_response(400, "Invalid JSON")
        name = data.get("name", "Admin")
        user = d.create_user(name)
        return JSONResponse({
            "status": "bootstrapped",
            "user": {"id": user["id"], "name": user["name"], "api_key": user["api_key"]},
            "message": "First user created. Save this API key — it won't be shown again.",
        }, status_code=201)

    async def handle_whoami(request: Request):
        """GET /whoami — verify API key and return user info."""
        try:
            user = resolve_user_from_request(request)
        except ValueError as e:
            return _problem_response(401, str(e))
        return JSONResponse({
            "status": "ok",
            "user": {"id": user["id"], "name": user["name"]},
        })

    async def handle_setup_page(request: Request):
        """GET /setup — self-service key generation (local mode only)."""
        if server_mode != "local":
            return _problem_response(404, "Setup is disabled in public mode")
        d = get_db()
        server_url = d.get_config("server_url") or ""
        return HTMLResponse(f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif; background:#0a0a0a; color:#e0e0e0; display:flex; justify-content:center; padding:40px; }}
  .card {{ max-width:420px; width:100%; }}
  h1 {{ font-size:20px; margin-bottom:8px; color:#fff; }}
  p {{ font-size:13px; color:#aaa; margin-bottom:20px; }}
  label {{ display:block; font-size:12px; color:#aaa; margin-bottom:4px; }}
  input {{ width:100%; padding:8px; background:#111; border:1px solid #333; border-radius:6px; color:#fff; font-size:13px; margin-bottom:12px; font-family:monospace; }}
  input:focus {{ outline:none; border-color:#6366f1; }}
  button {{ width:100%; padding:10px; background:#6366f1; color:#fff; border:none; border-radius:6px; font-size:13px; font-weight:600; cursor:pointer; }}
  button:hover {{ background:#5558e6; }}
  .result {{ margin-top:16px; padding:12px; background:#052e16; border:1px solid #166534; border-radius:6px; display:none; }}
  .result h3 {{ font-size:13px; color:#4ade80; margin-bottom:8px; }}
  .key-box {{ font-family:monospace; font-size:13px; color:#fff; word-break:break-all; user-select:all; }}
  .hint {{ font-size:11px; color:#888; margin-top:8px; }}
</style>
</head><body>
<div class="card">
  <h1>Dugg &mdash; Quick Setup</h1>
  <p>Create a user and get an API key for this local server.</p>
  <label for="name">Your name</label>
  <input type="text" id="name" placeholder="Kade" value="">
  <button id="goBtn" onclick="doSetup()">Create &amp; Get Key</button>
  <div class="result" id="result">
    <h3>Your API key:</h3>
    <div class="key-box" id="keyDisplay"></div>
    <p class="hint">Copy this key into the Chrome extension settings. It won't be shown again.</p>
  </div>
</div>
<script>
async function doSetup() {{
  const name = document.getElementById('name').value.trim() || 'User';
  const btn = document.getElementById('goBtn');
  btn.disabled = true; btn.textContent = 'Creating...';
  try {{
    const res = await fetch('/setup', {{ method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify({{name}}) }});
    const data = await res.json();
    if (data.error) {{ alert(data.error); btn.disabled=false; btn.textContent='Create & Get Key'; return; }}
    document.getElementById('keyDisplay').textContent = data.user.api_key;
    document.getElementById('result').style.display = 'block';
    btn.textContent = 'Done';
  }} catch(e) {{ alert('Error: ' + e.message); btn.disabled=false; btn.textContent='Create & Get Key'; }}
}}
</script>
</body></html>""")

    async def handle_setup_submit(request: Request):
        """POST /setup — create a user (local mode only)."""
        if server_mode != "local":
            return _problem_response(404, "Setup is disabled in public mode")
        d = get_db()
        try:
            body = await request.body()
            data = json.loads(body) if body else {}
        except json.JSONDecodeError:
            return _problem_response(400, "Invalid JSON")
        name = data.get("name", "User")
        user = d.create_user(name)
        return JSONResponse({
            "status": "created",
            "user": {"id": user["id"], "name": user["name"], "api_key": user["api_key"]},
        }, status_code=201)

    async def handle_instances(request: Request):
        """GET /instances — list instances with endpoint_url for distribution UI."""
        try:
            user = resolve_user_from_request(request)
        except ValueError as e:
            return _problem_response(401, str(e))
        d = get_db()
        instances = d.list_instances(user["id"])
        targets = [
            {"id": inst["id"], "name": inst["name"], "topic": inst.get("topic", "")}
            for inst in instances
            if inst.get("endpoint_url")
        ]
        return JSONResponse({"instances": targets})

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
            return _problem_response(401, str(e))

        get_db().mark_invite_onboarded(user["id"])

        try:
            body = await request.body()
            args = json.loads(body) if body else {}
        except json.JSONDecodeError:
            return _problem_response(400, "Invalid JSON")

        # Inject the API key so the tool handler resolves the same user
        args["api_key"] = request.headers.get("x-dugg-key", "")

        from dugg.server import server as mcp_server
        # Call the tool handler directly
        from dugg.server import call_tool
        try:
            results = await call_tool(tool_name, args)
            texts = [r.text for r in results if hasattr(r, "text")]
            full_result = "\n".join(texts)

            # RFC 6585: return 429 with Retry-After when rate-limited
            if full_result.startswith("Rate limit exceeded"):
                retry_after = _seconds_until_utc_midnight()
                return _problem_response(
                    429, full_result,
                    headers={"Retry-After": str(retry_after)},
                )

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
            return _problem_response(500, str(e))

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
  .item-actions {{ margin-top: 0.4rem; display: flex; gap: 0.5rem; }}
  .action-btn {{ width: auto; padding: 0.2rem 0.5rem; font-size: 0.75rem; background: transparent;
                 color: #666; border: 1px solid #333; border-radius: 4px; cursor: pointer; font-weight: 400; }}
  .action-btn:hover {{ color: #ccc; border-color: #555; background: #1a1a1a; }}
  .delete-btn:hover {{ color: #f87171; border-color: #7f1d1d; }}
  .save-btn {{ color: #4ade80; border-color: #166534; }}
  .save-btn:hover {{ background: #052e16; }}
  .edit-form {{ margin-top: 0.4rem; }}
  .edit-input {{ width: 100%; padding: 0.5rem; background: #111; border: 1px solid #444; border-radius: 6px;
                 color: #fff; font-size: 0.85rem; font-family: inherit; min-height: 60px; resize: vertical; }}
  .edit-input:focus {{ outline: none; border-color: #6366f1; }}
  .edit-buttons {{ display: flex; gap: 0.5rem; margin-top: 0.3rem; }}
  .sync-status {{ margin-bottom: 1rem; font-size: 0.85rem; color: #888; }}
  .sync-status summary {{ cursor: pointer; display: flex; align-items: center; gap: 0.5rem; }}
  .sync-status ul {{ margin: 0.5rem 0 0 1rem; padding: 0; list-style: none; }}
  .sync-status li {{ color: #666; margin-bottom: 0.25rem; }}
  .sync-btn {{ font-size: 0.7rem; padding: 0.15rem 0.4rem; }}
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
                return _problem_response(404, "Invalid invite token")
            return HTMLResponse(_html_page("Invalid Invite", "<h1>Invalid invite</h1><p>This invite link is not valid.</p>"), status_code=404)

        if invite.get("redeemed_by"):
            if invite.get("onboarded_at"):
                accept = request.headers.get("accept", "")
                if "application/json" in accept:
                    return _problem_response(410, "This invite has already been redeemed")
                return HTMLResponse(_html_page("Already Redeemed", "<h1>Already redeemed</h1><p>This invite has already been used.</p>"), status_code=410)
            # Not yet onboarded — show welcome page with keys so they can retrieve them
            user = d.get_user(invite["redeemed_by"])
            agents = d.get_agents_for_user(invite["redeemed_by"])
            agent = agents[0] if agents else None
            if not user or not agent:
                accept = request.headers.get("accept", "")
                if "application/json" in accept:
                    return _problem_response(410, "This invite has already been redeemed")
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
                return _problem_response(410, "This invite has expired")
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
                return _problem_response(400, "Invalid, expired, or already-redeemed invite token")
            return HTMLResponse(_html_page("Error", '<h1>Could not redeem</h1><p class="error">This invite is invalid, expired, or already used.</p>'), status_code=400)

        user = result["user"]
        agent = result["agent"]  # None for subscriber invites
        invite_role = result.get("role", "contributor")
        invite_info = result["invite"]
        inviter = d.get_user(invite_info["created_by"])
        instance = d.get_instance_for_owner(invite_info["created_by"])
        endpoint = instance.get("endpoint_url", "").rstrip("/") if instance else ""
        if not endpoint:
            endpoint = d.get_config("server_url", "")

        if "application/json" in content_type:
            server_url_json = endpoint or ""
            from dugg.db import dugg_email_address
            email_addr = dugg_email_address(user["api_key"], server_url_json) if invite_role == "contributor" else None

            agent_info = {"id": agent["id"], "name": agent["name"], "api_key": agent["api_key"]} if agent else None

            if invite_role == "subscriber":
                instructions = (
                    f"One key was issued. The user key ({user['api_key']}) belongs to your human — "
                    f"present it to them and tell them to store it somewhere safe (it won't be shown again). "
                    f"This is a subscriber account — read-only access to the feed. No agent key was created "
                    f"because subscribers cannot post. Your human can browse the feed at the URL below."
                )
            else:
                instructions = (
                    f"Two keys were issued. The user key ({user['api_key']}) belongs to your human — "
                    f"present it to them and tell them to store it somewhere safe (it won't be shown again). "
                    f"The agent key ({agent['api_key']}) is yours — use it as your X-Dugg-Key header for all "
                    f"future requests. If your human's account gets banned, your key stops working too. "
                    f"IMPORTANT: Read PARTNER_AGENT.md in the Dugg repo before your first submission — "
                    f"it covers behavioral norms, rate limits, catchup patterns, and how to be a good citizen."
                    + (f"\n\nEmail forwarding: forward emails to {email_addr} and they'll appear as resources in Dugg." if email_addr else "")
                )

            response_data = {
                "status": "redeemed",
                "role": invite_role,
                "user": {"id": user["id"], "name": user["name"], "api_key": user["api_key"]},
                "agent": agent_info,
                "email": email_addr,
                "endpoints": {
                    "sse": f"{server_url_json}/sse" if server_url_json and agent else None,
                    "tools": f"{server_url_json}/tools/{{tool_name}}" if server_url_json and agent else None,
                    "health": f"{server_url_json}/health" if server_url_json else None,
                    "feed": f"{server_url_json}/feed/{user['api_key']}" if server_url_json else None,
                },
                "instructions": instructions,
            }

            if invite_role == "contributor":
                feed_url = f"{server_url_json}/feed/{user['api_key']}" if server_url_json else None
                response_data["quickstart"] = [
                    {
                        "step": 1,
                        "action": "Set up your local Dugg",
                        "commands": ["git clone https://github.com/kadedworkin/dugg-fyi.git", "cd dugg-fyi && uv sync", "dugg init"],
                        "what_happens": "Creates your personal knowledge base.",
                    },
                    {
                        "step": 2,
                        "action": "Sync shared server content into local",
                        "commands": [f"dugg rss subscribe {feed_url} --tag {instance['name'] if instance else 'shared'}", "dugg rss poll"] if feed_url else [],
                        "feed_url": feed_url,
                        "what_happens": "Backfills all existing content from the shared server into your local Dugg. New items sync automatically every hour. No empty starting point.",
                    },
                    {
                        "step": 3,
                        "action": "Connect and explore",
                        "tools": ["dugg_welcome", "dugg_feed", "dugg_search", "dugg_react"],
                        "what_happens": "Browse what others have shared, search for topics, react to signal value. Content is already in your local feed from step 2.",
                    },
                ]
            else:
                response_data["quickstart"] = [
                    {
                        "step": 1,
                        "action": "Bookmark your feed",
                        "url": f"{server_url_json}/feed/{user['api_key']}" if server_url_json else None,
                        "what_happens": "Browse curated content from contributors in your browser. This feed updates as new resources are added.",
                    },
                ]

            return JSONResponse(response_data, status_code=201)

        feed_url = f"{endpoint}/feed/{user['api_key']}" if endpoint else f"/feed/{user['api_key']}"
        server_url = endpoint or ""

        if invite_role == "subscriber":
            body = f"""
<h1>You're in, {_xml_escape(user['name'])}!</h1>
<p>You have <strong>subscriber</strong> access — browse and search everything contributors share.</p>
<h3>Your key</h3>
<div class="key-box">{user['api_key']}</div>
<p style="font-size: 0.85em; color: #666;">This key gives you access to your personal feed. Bookmark the link below.</p>

<div class="next-steps">
  <h3>Your feed</h3>
  <p>Everything contributors share, in one place. Works in any browser or RSS reader.</p>
  <div class="step-example">
    <a href="{feed_url}" style="color: #93c5fd;">Open your feed &rarr;</a>
  </div>
  <div class="step-example" style="margin-top: 0.75rem;">
    <div class="step-label">Atom feed URL (for RSS readers):</div>
    <code><a href="{feed_url}" style="color: #4ade80;">{feed_url}</a></code>
  </div>
</div>"""
            return HTMLResponse(_html_page("Welcome to Dugg", body))

        from dugg.db import dugg_email_address
        email_addr = dugg_email_address(user["api_key"], server_url)

        email_section = ""
        if email_addr:
            email_section = f"""
<h3>Your email forwarding address</h3>
<div class="key-box">{_xml_escape(email_addr)}</div>
<p style="font-size: 0.85em; color: #666;">Forward emails to this address and they'll appear as resources in Dugg. Use it for newsletters, forwarded articles, or anything you want indexed.</p>"""

        body = f"""
<h1>You're in, {_xml_escape(user['name'])}!</h1>
<p>Here are your keys — save them somewhere safe, they won't be shown again.</p>
<h3>Your key</h3>
<div class="key-box">{user['api_key']}</div>
<h3>Your agent's key</h3>
<div class="key-box">{agent['api_key']}</div>
<p style="font-size: 0.85em; color: #666;">Give this key to your AI agent. If your account gets banned, your agent goes too.</p>
{email_section}

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

        # Page title: user's name feed
        page_title = f"{user['name']}'s Dugg"
        page_topic = ""

        if want_atom:
            # Atom feed with full metadata + tombstones (RFC 6721)
            srv_url = d.get_config("server_url", "")
            # Collect tombstones from all accessible collections
            accessible = d._accessible_collection_ids(user["id"])
            tombstones_xml = ""
            for coll_id in accessible:
                for tomb in d.list_recent_deletions(coll_id):
                    tombstones_xml += f"""<at:deleted-entry ref="{_xml_escape(tomb['resource_id'])}" when="{tomb['deleted_at']}">
  <at:comment>Removed: {_xml_escape(tomb.get('title') or tomb['url'])}</at:comment>
  <link href="{_xml_escape(tomb['url'])}"/>
</at:deleted-entry>\n"""
            entries = ""
            for r in feed:
                title = r.get("title") or r["url"]
                desc = r.get("description", "")
                note = r.get("note", "")
                sibling_notes = d.list_resource_notes(r["id"])
                sibling_text = ""
                if sibling_notes:
                    parts = []
                    for sn in sibling_notes:
                        who = sn.get("submitter_name") or "someone"
                        origin = sn.get("source_server") or ""
                        label = f"{who}" + (f" (via {origin})" if origin else "")
                        parts.append(f"— {label}: {sn['note']}")
                    sibling_text = "\n\n".join(parts)
                content = "\n\n".join(p for p in [desc, note, sibling_text] if p)
                display_url = _resolve_display_url(r["url"], srv_url)
                # Author element
                author_xml = ""
                if r.get("author"):
                    author_xml = f"\n  <author><name>{_xml_escape(r['author'])}</name></author>"
                # Published date from raw_metadata
                pub_date = _resource_pub_date(r)
                published_xml = ""
                if pub_date:
                    published_xml = f"\n  <published>{pub_date}T00:00:00Z</published>"
                # Category elements for tags
                tags = r.get("tags", [])
                categories_xml = ""
                for t in tags:
                    categories_xml += f'\n  <category term="{_xml_escape(t["label"])}"/>'
                entries += f"""<entry>
  <title>{_xml_escape(title)}</title>
  <link href="{_xml_escape(display_url)}"/>
  <id>{_xml_escape(r['id'])}</id>
  <updated>{r['created_at']}</updated>{published_xml}{author_xml}{categories_xml}
  <summary>{_xml_escape(content)}</summary>
</entry>\n"""
            # RFC 8288: self-referencing Link header + Atom <link rel="self">
            feed_path = f"/feed/{api_key}"
            self_url = f"{srv_url.rstrip('/')}{feed_path}" if srv_url else feed_path
            atom = f"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:at="http://purl.org/atompub/tombstones/1.0">
  <title>{_xml_escape(page_title)}</title>
  <link rel="self" type="application/atom+xml" href="{_xml_escape(self_url)}"/>
  <updated>{feed[0]['created_at'] if feed else ''}</updated>
{tombstones_xml}{entries}</feed>"""
            link_header = f'<{self_url}>; rel="self"; type="application/atom+xml"'
            return HTMLResponse(
                atom,
                media_type="application/atom+xml",
                headers={"Link": link_header},
            )

        # HTML feed view
        if not feed:
            items_html = '<p class="empty">Nothing here yet. Check back later.</p>'
        else:
            items_html = ""
            for r in feed:
                title = r.get("title") or r["url"]
                note_html = f'<p class="note">{_xml_escape(r["note"])}</p>' if r.get("note") else ""
                sibling_notes = d.list_resource_notes(r["id"])
                siblings_html = ""
                if sibling_notes:
                    sib_parts = []
                    for sn in sibling_notes:
                        who = _xml_escape(sn.get("submitter_name") or "someone")
                        origin = sn.get("source_server") or ""
                        origin_label = f' <span class="sib-origin">via {_xml_escape(origin)}</span>' if origin else ""
                        sib_parts.append(
                            f'<p class="note sibling"><span class="sib-who">{who}{origin_label}:</span> {_xml_escape(sn["note"])}</p>'
                        )
                    siblings_html = "".join(sib_parts)
                author_html = f' · {r["author"]}' if r.get("author") else ""
                added_date = _short_date(r.get("created_at"))
                pub_date = _resource_pub_date(r)
                pub_html = f" (published {pub_date})" if pub_date and pub_date != added_date else ""
                url = r["url"]
                if url.startswith("dugg://content/"):
                    url = "/r/" + url.removeprefix("dugg://content/")
                note_escaped = _xml_escape(r.get("note") or "")
                note_display = f'<p class="note" id="note-{r["id"]}">{note_escaped}</p>' if r.get("note") else f'<p class="note" id="note-{r["id"]}" style="display:none;"></p>'
                coll_id = r.get("collection_id", "")
                items_html += f"""<div class="feed-item" id="item-{r["id"]}" data-collection="{coll_id}">
  <h3><a href="{url}" target="_blank" rel="noopener">{title}</a></h3>
  <p class="meta">{added_date}{pub_html}{author_html}</p>
  {note_display}
  {siblings_html}
  <div class="item-actions">
    <button class="action-btn edit-btn" onclick="editNote('{r["id"]}')">edit</button>
    <button class="action-btn delete-btn" onclick="deleteItem('{r["id"]}')">delete</button>
  </div>
</div>\n"""

        # RSS subscription status
        subs = d.list_rss_subscriptions(user["id"])
        sync_html = ""
        if subs:
            sync_items = ""
            for s in subs:
                feed_title = s.get("feed_title") or s["feed_url"]
                last_polled = s.get("last_polled_at") or "never"
                if last_polled != "never":
                    last_polled = _short_date(last_polled)
                enabled = "active" if s.get("enabled") else "paused"
                sync_items += f'<li>{_xml_escape(feed_title)} · last synced: {last_polled} · {enabled}</li>'
            sync_html = f"""<div class="sync-status">
  <details>
    <summary>Subscriptions ({len(subs)}) <button class="action-btn sync-btn" onclick="syncNow(event)">Sync now</button></summary>
    <ul>{sync_items}</ul>
  </details>
</div>"""

        topic_html = f'<p class="topic">{page_topic}</p>' if page_topic else ""
        feed_js = """
<script>
const API_KEY = window.location.pathname.split('/feed/')[1];
const BASE = window.location.origin;

function editNote(id) {
  const noteEl = document.getElementById('note-' + id);
  const current = noteEl.textContent || '';
  const actionsEl = noteEl.parentElement.querySelector('.item-actions');
  // Replace note with textarea
  const editor = document.createElement('div');
  editor.className = 'edit-form';
  editor.innerHTML = `<textarea class="edit-input" id="edit-${id}">${current}</textarea>
    <div class="edit-buttons">
      <button class="action-btn save-btn" onclick="saveNote('${id}')">save</button>
      <button class="action-btn" onclick="cancelEdit('${id}', '${current.replace(/'/g, "\\\\'")}')">cancel</button>
    </div>`;
  noteEl.style.display = 'none';
  actionsEl.style.display = 'none';
  noteEl.parentElement.insertBefore(editor, actionsEl);
}

function cancelEdit(id, original) {
  const form = document.querySelector('#item-' + id + ' .edit-form');
  if (form) form.remove();
  const noteEl = document.getElementById('note-' + id);
  const actionsEl = document.querySelector('#item-' + id + ' .item-actions');
  if (original) noteEl.style.display = '';
  actionsEl.style.display = '';
}

async function saveNote(id) {
  const textarea = document.getElementById('edit-' + id);
  const newNote = textarea.value.trim();
  try {
    const res = await fetch(BASE + '/tools/dugg_edit', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Dugg-Key': API_KEY },
      body: JSON.stringify({ resource_id: id, note: newNote }),
    });
    if (!res.ok) { alert('Failed to save'); return; }
    const noteEl = document.getElementById('note-' + id);
    noteEl.textContent = newNote;
    noteEl.style.display = newNote ? '' : 'none';
    const form = document.querySelector('#item-' + id + ' .edit-form');
    if (form) form.remove();
    document.querySelector('#item-' + id + ' .item-actions').style.display = '';
  } catch (e) { alert('Error: ' + e.message); }
}

async function deleteItem(id) {
  if (!confirm('Delete this item?')) return;
  try {
    const collectionId = document.getElementById('item-' + id).dataset.collection;
    const res = await fetch(BASE + '/tools/dugg_delete_resource', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Dugg-Key': API_KEY },
      body: JSON.stringify({ resource_id: id, collection_id: collectionId }),
    });
    if (!res.ok) { alert('Failed to delete'); return; }
    const item = document.getElementById('item-' + id);
    item.style.opacity = '0.3';
    item.style.pointerEvents = 'none';
    setTimeout(() => item.remove(), 300);
  } catch (e) { alert('Error: ' + e.message); }
}

async function syncNow(e) {
  e.preventDefault();
  e.stopPropagation();
  const btn = e.target;
  btn.textContent = 'syncing...';
  btn.disabled = true;
  try {
    const res = await fetch(BASE + '/tools/dugg_rss_poll', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Dugg-Key': API_KEY },
      body: JSON.stringify({}),
    });
    if (res.ok) {
      btn.textContent = 'done!';
      setTimeout(() => window.location.reload(), 1000);
    } else {
      btn.textContent = 'failed';
      setTimeout(() => { btn.textContent = 'Sync now'; btn.disabled = false; }, 2000);
    }
  } catch (err) {
    btn.textContent = 'error';
    setTimeout(() => { btn.textContent = 'Sync now'; btn.disabled = false; }, 2000);
  }
}
</script>"""
        body = f"""<h1>{page_title}</h1>
{sync_html}
{topic_html}
{items_html}
{feed_js}"""
        # RFC 8288: Link header pointing to Atom alternate
        srv_url = d.get_config("server_url", "")
        feed_path = f"/feed/{api_key}"
        atom_url = f"{srv_url.rstrip('/')}{feed_path}" if srv_url else feed_path
        return HTMLResponse(
            _html_page(page_title, body),
            headers={"Link": f'<{atom_url}>; rel="alternate"; type="application/atom+xml"'},
        )

    # --- Key Rotation ---

    async def handle_rotate_key(request: Request):
        """POST /rotate-key — issue a new API key for the caller, invalidating the old one.

        Authenticates via the current X-Dugg-Key header. Returns {"api_key": "..."}.
        Memberships, webhooks, and invites survive rotation (all keyed by user_id)."""
        try:
            user = resolve_user_from_request(request)
        except ValueError as e:
            return _problem_response(401, str(e))
        d = get_db()
        new_key = d.rotate_api_key(user["id"])
        return JSONResponse({"api_key": new_key, "user_id": user["id"]})

    # --- Shareable Resource Viewer (/r/{resource_id}) ---
    # Form-gated: unauthenticated visitors get a "paste your key" form; submitting
    # sets a cookie so subsequent visits go straight to content. Membership is
    # checked on every render, so leaking the URL does not grant access.

    COOKIE_NAME = "dugg_key"

    def _resolve_user_for_viewer(request: Request) -> Optional[dict]:
        """Cookie > X-Dugg-Key header. Returns None if neither resolves to a user."""
        d = get_db()
        cookie_key = request.cookies.get(COOKIE_NAME, "")
        if cookie_key:
            user = d.get_user_by_api_key(cookie_key)
            if user:
                return user
        header_key = request.headers.get("x-dugg-key", "")
        if header_key:
            user = d.get_user_by_api_key(header_key)
            if user:
                return user
        return None

    def _unlock_form_html(resource_id: str, error: str = "") -> str:
        err_html = f'<p style="color:#f87171;margin-top:0.5rem;font-size:0.9rem;">{_xml_escape(error)}</p>' if error else ""
        return _html_page(
            "Unlock",
            f"""<h1>Unlock</h1>
<p style="margin-top:0.5rem;color:#aaa;">This content is only visible to Dugg members of this server. Paste your Dugg key to view.</p>
<form method="POST" action="/r/{_xml_escape(resource_id)}/unlock" style="margin-top:1rem;">
  <input type="password" name="key" placeholder="dugg_..." autofocus required
         style="width:100%;padding:0.6rem;background:#1a1a1a;border:1px solid #333;color:#eee;border-radius:4px;font-family:monospace;">
  <button type="submit" style="margin-top:0.5rem;padding:0.6rem 1rem;background:#2563eb;color:white;border:0;border-radius:4px;cursor:pointer;">Unlock</button>
  {err_html}
</form>""",
        )

    def _render_resource(resource: dict, sibling_notes: Optional[list] = None) -> str:
        title = resource.get("title") or "Untitled"
        transcript = resource.get("transcript") or ""
        author = resource.get("author") or ""
        created = (resource.get("created_at") or "")[:10]
        pub_date = _resource_pub_date(resource)
        note = resource.get("note") or ""
        tags = resource.get("tags") or []
        meta_parts = [created]
        if pub_date and pub_date != created:
            meta_parts.append(f"published {pub_date}")
        if author:
            meta_parts.append(author)
        meta_html = " · ".join(meta_parts)
        note_html = f'<p class="note" style="margin-top:1rem;font-style:italic;">{_xml_escape(note)}</p>' if note else ""
        siblings_html = ""
        if sibling_notes:
            parts = []
            for sn in sibling_notes:
                who = _xml_escape(sn.get("submitter_name") or "someone")
                origin = sn.get("source_server") or ""
                origin_html = f' <span style="color:#888;">via {_xml_escape(origin)}</span>' if origin else ""
                parts.append(
                    f'<p class="note sibling" style="margin-top:0.5rem;padding-left:0.75rem;border-left:2px solid #333;font-style:italic;color:#ccc;">'
                    f'<span style="color:#aaa;font-style:normal;">{who}{origin_html}:</span> {_xml_escape(sn["note"])}</p>'
                )
            siblings_html = "".join(parts)
        tags_html = f'<p style="margin-top:0.5rem;font-size:0.8rem;color:#666;">{", ".join(_xml_escape(t) for t in tags)}</p>' if tags else ""
        content_html = _xml_escape(transcript).replace("\n", "<br>")
        body = f"""<h1>{_xml_escape(title)}</h1>
<p class="meta" style="margin-bottom:1rem;">{meta_html}</p>
{note_html}
{siblings_html}
{tags_html}
<div style="margin-top:1.5rem;line-height:1.6;font-size:0.9rem;color:#ccc;white-space:pre-wrap;word-break:break-word;">{content_html}</div>"""
        return _html_page(_xml_escape(title), body)

    async def handle_resource_page(request: Request):
        """GET /r/{resource_id} — render a resource if viewer has access.

        Unauthenticated: show a form to paste a key (no key in URL ever).
        Authenticated via cookie or header: render if viewer is a member of a
        collection that contains the resource."""
        resource_id = request.path_params["resource_id"]
        d = get_db()
        user = _resolve_user_for_viewer(request)
        if not user:
            return HTMLResponse(_unlock_form_html(resource_id), status_code=401)

        d.touch_user(user["id"])
        resource = d.get_resource(resource_id)
        if not resource:
            row = d.conn.execute(
                "SELECT id FROM resources WHERE url = ?",
                (f"dugg://content/{resource_id}",),
            ).fetchone()
            if row:
                resource = d.get_resource(row["id"])
        accessible = d._accessible_collection_ids(user["id"])
        if not resource or resource.get("collection_id") not in accessible:
            return HTMLResponse(_html_page("Not Found", "<h1>Not found</h1>"), status_code=404)

        siblings = d.list_resource_notes(resource["id"])
        return HTMLResponse(_render_resource(resource, sibling_notes=siblings))

    async def handle_resource_unlock(request: Request):
        """POST /r/{resource_id}/unlock — validate pasted key, set cookie, redirect back."""
        resource_id = request.path_params["resource_id"]
        form = await request.form()
        key = (form.get("key") or "").strip()
        d = get_db()
        user = d.get_user_by_api_key(key) if key else None
        if not user:
            return HTMLResponse(_unlock_form_html(resource_id, error="Invalid key."), status_code=401)
        from starlette.responses import RedirectResponse
        resp = RedirectResponse(url=f"/r/{resource_id}", status_code=303)
        is_https = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
        resp.set_cookie(
            COOKIE_NAME, key,
            httponly=True, secure=is_https, samesite="lax",
            max_age=60 * 60 * 24 * 365,
            path="/",
        )
        return resp

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
  <p class="meta">Your credit score: {score['total']} ({score['submissions']} submissions, {score['distinct_human_reactors']} reactions)</p>
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
                return _problem_response(400, "Cannot appeal — you may not be banned in this collection")
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
            return _problem_response(404, "Invalid key")

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
            return _problem_response(401, str(e))

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

        # /dugg with no args or /dugg feed [--limit N] → show feed
        feed_limit = 5
        show_feed = not text
        if text.startswith("feed"):
            show_feed = True
            # Parse --limit N from the rest
            rest = text[4:].strip()
            if rest:
                import re
                m = re.search(r'--limit\s+(\d+)', rest)
                if m:
                    feed_limit = min(int(m.group(1)), 25)
        if show_feed:
            feed = d.get_feed(user["id"], limit=feed_limit)
            if not feed:
                return JSONResponse({"response_type": "ephemeral", "text": "Feed is empty. Add something with `/dugg https://...`"})
            names = {r["id"]: r["name"] for r in d.conn.execute("SELECT id, name FROM users").fetchall()}
            sibling_notes = d.batch_resource_notes([r["id"] for r in feed])
            srv_url = d.get_config("server_url", "")
            blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": f"*Latest {len(feed)} resource(s):*"}}]
            text_lines = [f"*Latest {len(feed)} resource(s):*\n"]
            for r in feed:
                title = r.get("title") or r["url"]
                added_by = names.get(r.get("submitted_by", ""), "")
                source = r.get("source_server", "")
                added_date = _short_date(r.get("created_at"))
                pub_date = _resource_pub_date(r)
                display_url = _resolve_display_url(r["url"], srv_url)
                res_lines = [f"*{_xml_escape(title)}*"]
                res_lines.append(f"<{display_url}>")
                attrib = ""
                if added_by and added_date:
                    attrib = f"by {added_by} on {added_date}"
                elif added_by:
                    attrib = f"by {added_by}"
                elif added_date:
                    attrib = f"on {added_date}"
                if attrib and pub_date:
                    attrib += f" (published {pub_date})"
                meta = []
                if attrib:
                    meta.append(attrib)
                if source:
                    meta.append(f"from {source}")
                if meta:
                    res_lines.append(" · ".join(meta))
                if r.get("note"):
                    res_lines.append(f"_{_xml_escape(r['note'])}_")
                for sn in sibling_notes.get(r.get("id", ""), []):
                    label = f"{sn['submitter_name']}: " if sn.get("submitter_name") else ""
                    res_lines.append(f"_{_xml_escape(label + sn['note'][:200])}_")
                if r.get("description"):
                    res_lines.append(f">{_xml_escape(r['description'][:200])}")
                res_text = "\n".join(res_lines)
                text_lines.extend(res_lines + [""])
                blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": res_text}})
                resource_id = r.get("id", "")
                if resource_id:
                    blocks.append({
                        "type": "actions",
                        "elements": [
                            {"type": "button", "text": {"type": "plain_text", "text": ":point_right: Tap", "emoji": True},
                             "action_id": "dugg_react_tap", "value": resource_id},
                            {"type": "button", "text": {"type": "plain_text", "text": ":star: Star", "emoji": True},
                             "action_id": "dugg_react_star", "value": resource_id},
                            {"type": "button", "text": {"type": "plain_text", "text": ":thumbsup: Nice", "emoji": True},
                             "action_id": "dugg_react_thumbsup", "value": resource_id},
                        ],
                    })
                blocks.append({"type": "divider"})
            return JSONResponse({"response_type": "in_channel", "text": "\n".join(text_lines), "blocks": blocks, "unfurl_links": False, "unfurl_media": False})

        # /dugg <url> [--note ...] → add resource
        url = text.split()[0].strip("<>")
        if url.startswith("http://") or url.startswith("https://"):
            note = ""
            rest = text[len(text.split()[0]):].strip()
            if rest.startswith("--note "):
                note = rest[7:].strip().strip('"\'')
            elif rest:
                note = rest

            coll_id = _ensure_default_collection(d, user["id"])

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
            text_fallback = "\n".join(resp_lines)
            resource_id = resource.get("id", "")
            blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": text_fallback}}]
            if resource_id:
                blocks.append({
                    "type": "actions",
                    "elements": [
                        {"type": "button", "text": {"type": "plain_text", "text": ":point_right: Tap", "emoji": True},
                         "action_id": "dugg_react_tap", "value": resource_id},
                        {"type": "button", "text": {"type": "plain_text", "text": ":star: Star", "emoji": True},
                         "action_id": "dugg_react_star", "value": resource_id},
                        {"type": "button", "text": {"type": "plain_text", "text": ":thumbsup: Nice", "emoji": True},
                         "action_id": "dugg_react_thumbsup", "value": resource_id},
                    ],
                })
            return JSONResponse({"response_type": "in_channel", "text": text_fallback, "blocks": blocks})

        # /dugg <search query> or /dugg search <query> → search
        search_text = text
        if text.startswith("search "):
            search_text = text[7:].strip()
        if not search_text:
            return JSONResponse({"response_type": "ephemeral", "text": "Usage: `/dugg <search terms>` or `/dugg search <terms>`"})
        try:
            results = d.search(search_text, user["id"], limit=5)
        except Exception:
            return JSONResponse({"response_type": "ephemeral", "text": f'Search error — try simpler terms.'})
        if not results:
            return JSONResponse({"response_type": "ephemeral", "text": f'No results for "{_xml_escape(search_text)}"'})
        names = {r["id"]: r["name"] for r in d.conn.execute("SELECT id, name FROM users").fetchall()}
        sibling_notes = d.batch_resource_notes([r["id"] for r in results])
        srv_url = d.get_config("server_url", "")
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": f'*{len(results)} result(s) for "{_xml_escape(search_text)}":*'}}]
        text_lines = [f'*{len(results)} result(s) for "{_xml_escape(search_text)}":*\n']
        for r in results:
            title = r.get("title") or r["url"]
            added_by = names.get(r.get("submitted_by", ""), "")
            added_date = _short_date(r.get("created_at"))
            pub_date = _resource_pub_date(r)
            display_url = _resolve_display_url(r["url"], srv_url)
            res_lines = [f"*{_xml_escape(title)}*"]
            res_lines.append(f"<{display_url}>")
            attrib = ""
            if added_by and added_date:
                attrib = f"by {added_by} on {added_date}"
            elif added_by:
                attrib = f"by {added_by}"
            elif added_date:
                attrib = f"on {added_date}"
            if attrib and pub_date:
                attrib += f" (published {pub_date})"
            if attrib:
                res_lines.append(attrib)
            if r.get("note"):
                res_lines.append(f"_{_xml_escape(r['note'])}_")
            for sn in sibling_notes.get(r.get("id", ""), []):
                label = f"{sn['submitter_name']}: " if sn.get("submitter_name") else ""
                res_lines.append(f"_{_xml_escape(label + sn['note'][:200])}_")
            if r.get("description"):
                res_lines.append(f">{_xml_escape(r['description'][:200])}")
            res_text = "\n".join(res_lines)
            text_lines.extend(res_lines + [""])
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": res_text}})
            resource_id = r.get("id", "")
            if resource_id:
                blocks.append({
                    "type": "actions",
                    "elements": [
                        {"type": "button", "text": {"type": "plain_text", "text": ":point_right: Tap", "emoji": True},
                         "action_id": "dugg_react_tap", "value": resource_id},
                        {"type": "button", "text": {"type": "plain_text", "text": ":star: Star", "emoji": True},
                         "action_id": "dugg_react_star", "value": resource_id},
                        {"type": "button", "text": {"type": "plain_text", "text": ":thumbsup: Nice", "emoji": True},
                         "action_id": "dugg_react_thumbsup", "value": resource_id},
                    ],
                })
            blocks.append({"type": "divider"})
        return JSONResponse({"response_type": "in_channel", "text": "\n".join(text_lines), "blocks": blocks, "unfurl_links": False, "unfurl_media": False})

    # --- Slack interactive actions (Block Kit buttons) ---

    async def handle_slack_actions(request: Request):
        """Handle Slack Block Kit interactive payloads (button clicks)."""
        d = get_db()
        form = await request.form()
        raw_payload = form.get("payload", "")
        if not raw_payload:
            return JSONResponse({"text": "Missing payload."}, status_code=400)

        try:
            data = json.loads(raw_payload)
        except json.JSONDecodeError:
            return JSONResponse({"text": "Invalid payload."}, status_code=400)

        # Verify signing secret if configured
        signing_secret = d.get_config("slack_signing_secret", "")
        if signing_secret:
            import time
            timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
            slack_sig = request.headers.get("X-Slack-Signature", "")
            if abs(time.time() - int(timestamp or 0)) > 300:
                return JSONResponse({"text": "Request too old."}, status_code=403)
            body_bytes = await request.body()
            sig_basestring = f"v0:{timestamp}:{body_bytes.decode()}"
            my_sig = "v0=" + hmac.new(signing_secret.encode(), sig_basestring.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(my_sig, slack_sig):
                return JSONResponse({"text": "Invalid signature."}, status_code=403)

        actions = data.get("actions", [])
        if not actions:
            return JSONResponse({"text": ""})

        action = actions[0]
        action_id = action.get("action_id", "")
        resource_id = action.get("value", "")

        # Map action_id to reaction type
        reaction_map = {
            "dugg_react_tap": "tap",
            "dugg_react_star": "star",
            "dugg_react_thumbsup": "thumbsup",
        }
        reaction_type = reaction_map.get(action_id)
        if not reaction_type or not resource_id:
            return JSONResponse({"text": ""})

        # Resolve Slack user to Dugg user
        slack_user = data.get("user", {}).get("username", "")
        rows = d.conn.execute("SELECT id, name FROM users").fetchall()
        user = None
        for r in rows:
            if r["name"].lower() == slack_user.lower():
                user = dict(r)
                break
        if not user and rows:
            user = dict(rows[0])
        if not user:
            return JSONResponse({"text": "No Dugg user found."})

        # Verify resource exists
        resource = d.get_resource(resource_id)
        if not resource:
            return JSONResponse({"text": "Resource not found."})

        emoji = {"tap": ":point_right:", "star": ":star:", "thumbsup": ":thumbsup:"}.get(reaction_type, "")
        d.react_to_resource(resource_id, user["id"], reaction_type)
        d.wait_for_webhooks()

        title = resource.get("title") or resource.get("url", "")
        return JSONResponse({
            "response_type": "ephemeral",
            "replace_original": False,
            "text": f"{emoji} You reacted {reaction_type} to *{title}*",
        })

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
  <input type="text" id="title" name="title" placeholder="e.g. Weekly AI newsletter, Apr 15" required>
  <label for="body">Content</label>
  <textarea id="body" name="body" rows="12" placeholder="Paste the content here..." style="width:100%;padding:0.6rem;background:#111;border:1px solid #444;border-radius:6px;color:#fff;font-size:0.9rem;margin-bottom:1rem;resize:vertical;font-family:inherit;"></textarea>
  <label for="file">Or upload a file (.txt, .html, .md)</label>
  <input type="file" id="file" name="file" accept=".txt,.html,.htm,.md,.eml" style="margin-bottom:1rem;color:#aaa;font-size:0.85rem;">
  <label for="source_type">Content type</label>
  <select id="source_type" name="source_type" style="width:100%;padding:0.6rem;background:#111;border:1px solid #444;border-radius:6px;color:#fff;font-size:1rem;margin-bottom:1rem;">
    <option value="email" selected>Email / Newsletter</option>
    <option value="note">Note</option>
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
        synthetic_url = f"dugg://content/{res_id}"
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
        rss_task = start_rss_daemon(d, interval=300)
        logger.info("Dugg HTTP server started — sync + RSS daemons running")
        try:
            yield
        finally:
            sync_task.cancel()
            rss_task.cancel()
            if db:
                db.close()
            logger.info("Dugg HTTP server shut down")

    # --- App assembly ---

    routes = [
        Route("/sse", endpoint=handle_sse),
        Route("/messages", endpoint=handle_messages, methods=["POST"]),
        Route("/ingest", endpoint=handle_ingest, methods=["POST"]),
        Route("/delete", endpoint=handle_delete, methods=["POST"]),
        Route("/health", endpoint=handle_health),
        Route("/whoami", endpoint=handle_whoami),
        Route("/instances", endpoint=handle_instances),
        Route("/bootstrap", endpoint=handle_bootstrap, methods=["POST"]),
        Route("/setup", endpoint=handle_setup_page),
        Route("/setup", endpoint=handle_setup_submit, methods=["POST"]),
        Route("/invite/{token}", endpoint=handle_invite_page),
        Route("/invite/{token}/redeem", endpoint=handle_invite_redeem, methods=["POST"]),
        Route("/feed/{key}", endpoint=handle_feed),
        Route("/paste/{key}", endpoint=handle_paste_page),
        Route("/paste/{key}/submit", endpoint=handle_paste_submit, methods=["POST"]),
        Route("/appeal/{key}", endpoint=handle_appeal_page),
        Route("/appeal/{key}/submit", endpoint=handle_appeal_submit, methods=["POST"]),
        Route("/appeal/{key}/status", endpoint=handle_appeal_status),
        Route("/r/{resource_id}", endpoint=handle_resource_page),
        Route("/r/{resource_id}/unlock", endpoint=handle_resource_unlock, methods=["POST"]),
        Route("/rotate-key", endpoint=handle_rotate_key, methods=["POST"]),
        Route("/events/stream", endpoint=handle_events_stream),
        Route("/tools/{tool_name}", endpoint=handle_tools, methods=["POST"]),
        Route("/slack/command", endpoint=handle_slack_command, methods=["POST"]),
        Route("/slack/actions", endpoint=handle_slack_actions, methods=["POST"]),
        Route("/admin/{key}", endpoint=handle_admin_page),
        Route("/admin/{key}/ban", endpoint=handle_admin_ban, methods=["POST"]),
        Route("/admin/{key}/unban", endpoint=handle_admin_unban, methods=["POST"]),
        Route("/admin/{key}/remove", endpoint=handle_admin_remove, methods=["POST"]),
    ]

    app = Starlette(
        routes=routes,
        lifespan=lifespan,
        middleware=[
            Middleware(
                CORSMiddleware,
                allow_origins=["*"],
                allow_methods=["GET", "POST", "OPTIONS"],
                allow_headers=["Content-Type", "X-Dugg-Key", "X-Dugg-Format", "X-Dugg-Signature"],
            ),
        ],
    )

    return app


def run_http(host: str = "0.0.0.0", port: int = 8411, db_path: Optional[Path] = None, mode: Optional[str] = None):
    """Run the Dugg HTTP server with uvicorn."""
    import uvicorn

    # Resolve mode: explicit flag > env var > default "local"
    if mode is None:
        mode = os.environ.get("DUGG_MODE", "local")

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

    logger.info("Starting Dugg HTTP server in %s mode", mode)
    app = create_app(db_path=db_path, mode=mode)
    uvicorn.run(app, host=host, port=port, log_level="info")
