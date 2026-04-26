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
from urllib.parse import urlparse

import httpx

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

from mcp.server.sse import SseServerTransport

from dugg.db import DuggDB, READ_STATE_SOURCES
from dugg.source_registry import hints_for
from dugg.sync import start_sync_daemon
from dugg.rss import start_rss_daemon
from dugg.skills import parse_skill_markdown, render_skill_markdown, validate_skill_name

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

    # --- Session cookie auth (shared across all browser-served endpoints) ---
    # The `dugg_key` cookie holds the API key directly — HttpOnly+Secure+SameSite=Lax,
    # 30-day Max-Age. Cookie first, X-Dugg-Key header as fallback. Key rotation
    # invalidates the cookie instantly (the cookie value == the key).
    COOKIE_NAME = "dugg_key"
    COOKIE_MAX_AGE = 60 * 60 * 24 * 30  # 30 days

    def _resolve_user_from_cookie_or_header(request: Request) -> Optional[dict]:
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

    def _cookie_key_from_request(request: Request) -> str:
        """Return the cookie-or-header key string (empty if neither set)."""
        return request.cookies.get(COOKIE_NAME, "") or request.headers.get("x-dugg-key", "")

    def _set_session_cookie(resp, key: str, request: Request) -> None:
        """Attach a 30-day dugg_key cookie to a response. HttpOnly+Secure(when TLS)+SameSite=Lax."""
        is_https = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
        resp.set_cookie(
            COOKIE_NAME, key,
            httponly=True, secure=is_https, samesite="lax",
            max_age=COOKIE_MAX_AGE,
            path="/",
        )

    def _safe_return_to(raw: Optional[str], default: str = "/feed") -> str:
        """Validate a return_to path: must be same-origin (starts with `/`, not `//`)."""
        if not raw or not isinstance(raw, str):
            return default
        if not raw.startswith("/") or raw.startswith("//"):
            return default
        return raw

    def resolve_user_from_request(request: Request) -> dict:
        """Resolve user from dugg_key cookie or X-Dugg-Key header."""
        user = _resolve_user_from_cookie_or_header(request)
        if user:
            return user
        if request.cookies.get(COOKIE_NAME) or request.headers.get("x-dugg-key"):
            raise ValueError("Invalid API key")
        raise ValueError("Missing credentials — dugg_key cookie or X-Dugg-Key header required")

    def _query_flag(request: Request, name: str) -> bool:
        return (request.query_params.get(name) or "").strip().lower() == "true"

    def _reaction_implicit_source(request: Request) -> str:
        raw_surface = (request.headers.get("X-Dugg-Surface") or "").strip().lower()
        if raw_surface in READ_STATE_SOURCES and raw_surface.endswith("_react_implicit"):
            return raw_surface
        candidate = f"{raw_surface}_react_implicit" if raw_surface else ""
        if candidate in READ_STATE_SOURCES:
            return candidate
        # Default to MCP because unannotated API calls are most likely from MCP/agent clients.
        return "mcp_react_implicit"

    def _batch_feed_reactions(d: DuggDB, resource_ids: list[str], user_id: str) -> dict[str, dict]:
        if not resource_ids:
            return {}
        placeholders = ",".join("?" for _ in resource_ids)
        state = {
            resource_id: {
                "star_count": 0,
                "thumbsup_count": 0,
                "viewer_starred": False,
                "viewer_thumbsup": False,
            }
            for resource_id in resource_ids
        }
        count_rows = d.conn.execute(
            f"""SELECT resource_id, reaction_type, COUNT(*) AS count
                FROM reactions
                WHERE resource_id IN ({placeholders})
                GROUP BY resource_id, reaction_type""",
            resource_ids,
        ).fetchall()
        for row in count_rows:
            if row["reaction_type"] == "star":
                state[row["resource_id"]]["star_count"] = row["count"]
            elif row["reaction_type"] == "thumbsup":
                state[row["resource_id"]]["thumbsup_count"] = row["count"]

        viewer_rows = d.conn.execute(
            f"""SELECT resource_id, reaction_type
                FROM reactions
                WHERE user_id = ? AND resource_id IN ({placeholders})""",
            [user_id, *resource_ids],
        ).fetchall()
        for row in viewer_rows:
            if row["reaction_type"] == "star":
                state[row["resource_id"]]["viewer_starred"] = True
            elif row["reaction_type"] == "thumbsup":
                state[row["resource_id"]]["viewer_thumbsup"] = True
        return state

    def _slack_resource_action_buttons(resource_id: str) -> list[dict]:
        return [
            {"type": "button", "text": {"type": "plain_text", "text": ":book: Mark as Read", "emoji": True},
             "action_id": "dugg_mark_read", "value": resource_id},
            {"type": "button", "text": {"type": "plain_text", "text": ":star: Star", "emoji": True},
             "action_id": "dugg_react_star", "value": resource_id},
            {"type": "button", "text": {"type": "plain_text", "text": ":+1: Thumbs Up", "emoji": True},
             "action_id": "dugg_react_thumbsup", "value": resource_id},
        ]

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
        submitter_name = resource_data.get("submitter_name", "")
        # `submitter_remote_id` is the origin server's local users.id UUID
        # for the author. Pairs with source_server to form a globally
        # stable identity. NEVER an api_key — the publish-side wire builder
        # (sync.deliver_publish) is documented to send users.id only.
        submitter_remote_id = resource_data.get("submitter_remote_id", "") or ""

        # Resolve attribution. Two prongs:
        #   1. Resource row owner (`submitted_by`). Defaults to the authed
        #      caller because resources always need a local owner for the
        #      moderation gate.
        #   2. Sibling note local link (`note_submitter_id`). Set ONLY when
        #      we have a high-confidence link via user_remote_identities,
        #      OR when the authed caller's own name matches submitter_name
        #      (self-post: the caller is asserting they're the author and
        #      they're proving it via their auth). Otherwise leave empty
        #      and let the (source_server, submitter_remote_id) pair be the
        #      identity — viewer_owns_note() resolves it through the link
        #      table at edit time.
        submitter_id = user["id"]
        note_submitter_id = ""
        if source_server and submitter_remote_id:
            linked = d.lookup_remote_identity(source_server, submitter_remote_id)
            if linked:
                submitter_id = linked
                note_submitter_id = linked
        if not note_submitter_id and submitter_name and user.get("name") == submitter_name:
            # Self-post fallback (no link yet): a brand-new server where the
            # author is also the federation caller. Trust the auth.
            note_submitter_id = user["id"]
            submitter_id = user["id"]

        result = d.ingest_remote_publish(
            resource_data, source_instance_id, coll_id,
            source_server=source_server, submitted_by=submitter_id,
            note_submitter_id=note_submitter_id,
            note_remote_id=submitter_remote_id,
        )
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

        # Inject the API key so the tool handler resolves the same user.
        # Use the already-resolved user's key so cookie-auth (/feed search JS)
        # and header-auth both land on the same identity in the tool layer.
        args["api_key"] = user["api_key"]

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

    def _html_page(title: str, body: str, wide: bool = False) -> str:
        """Minimal HTML page wrapper. wide=True for feed-style pages."""
        wrap_class = "page-wrap" if wide else "page-wrap form-page"
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
  .page-wrap {{ max-width: 720px; width: 100%; }}
  .page-wrap.form-page {{ max-width: 480px; background: #1a1a1a; border: 1px solid #333;
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
  .card {{ max-width: 720px; width: 100%; background: #1a1a1a; border: 1px solid #2a2a2a;
           border-radius: 12px; margin-bottom: 1rem; overflow: hidden; }}
  .card-media {{ width: 100%; }}
  .card-thumb {{ width: 100%; height: auto; display: block; border-bottom: 1px solid #2a2a2a; }}
  .card-body {{ padding: 1.25rem; }}
  .card h3 {{ font-size: 1.05rem; margin-bottom: 0.4rem; display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap; }}
  .card h3 a {{ color: #93c5fd; text-decoration: none; }}
  .card h3 a:hover {{ text-decoration: underline; }}
  .card .meta {{ font-size: 0.8rem; color: #666; margin-bottom: 0.5rem; }}
  .card .submitted-by {{ color: #888; }}
  .card .author {{ color: #a78bfa; }}
  .card-desc {{ font-size: 0.85rem; color: #999; margin: 0.5rem 0; line-height: 1.5;
                display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden; }}
  .card .note {{ font-size: 0.85rem; color: #aaa; margin-top: 0.5rem; padding: 0.6rem 0.8rem;
                border-radius: 6px; border-left: 3px solid #333; background: #111;
                display: flex; align-items: flex-start; gap: 0.5rem; flex-wrap: wrap; }}
  .card .note-body {{ flex: 1 1 auto; min-width: 0; word-break: break-word; }}
  .card .note-actions {{ flex: 0 0 auto; display: flex; gap: 0.25rem; opacity: 0.4; transition: opacity 0.15s; }}
  .card .note:hover .note-actions {{ opacity: 1; }}
  .card .note-action-btn {{ font-size: 0.7rem; padding: 0.1rem 0.4rem; background: transparent;
                           border: 1px solid #333; color: #888; border-radius: 3px; cursor: pointer; width: auto; }}
  .card .note-action-btn:hover {{ color: #ccc; border-color: #555; background: #1a1a1a; }}
  .card .note-action-btn.note-action-del:hover {{ color: #f87171; border-color: #7f1d1d; }}
  .card .note-edit-form, .card .add-note-form {{ flex: 1 1 100%; margin-top: 0.4rem; }}
  .card .add-note-form {{ margin-top: 0.6rem; padding: 0.5rem; border: 1px dashed #333; border-radius: 6px; }}
  .card .note-local-mine {{ border-left-color: #6366f1; background: rgba(99, 102, 241, 0.08); color: #c7c8ff; }}
  .card .note-remote-mine {{ border-left-color: #3b82f6; background: rgba(59, 130, 246, 0.08); color: #93c5fd; }}
  .card .note-other {{ border-left-color: #f59e0b; background: rgba(245, 158, 11, 0.06); color: #fcd34d; }}
  .card .note.sibling {{ margin-top: 0.3rem; }}
  .card .sib-who {{ font-weight: 600; margin-right: 0.3rem; }}
  .card .sib-origin {{ font-size: 0.75rem; color: #666; }}
  .card-tags {{ display: flex; flex-wrap: wrap; gap: 0.3rem; margin-top: 0.5rem; }}
  .tag {{ font-size: 0.7rem; color: #888; background: #222; border: 1px solid #333;
          border-radius: 3px; padding: 0.1rem 0.4rem; }}
  .type-badge {{ font-size: 0.65rem; color: #fff; padding: 0.1rem 0.35rem; border-radius: 3px;
                 font-weight: 600; text-transform: uppercase; letter-spacing: 0.03em; }}
  .type-badge.yt {{ background: #dc2626; }}
  .type-badge.article {{ background: #2563eb; }}
  .search-bar {{ margin-bottom: 1.5rem; display: flex; gap: 0.75rem; align-items: center; flex-wrap: wrap; }}
  .search-input-wrap {{ flex: 1 1 320px; position: relative; min-width: 0; }}
  .search-bar input {{ width: 100%; padding: 0.6rem 2.2rem 0.6rem 0.8rem; background: #111; border: 1px solid #333;
                       border-radius: 8px; color: #fff; font-size: 0.9rem; }}
  .search-bar input:focus {{ outline: none; border-color: #6366f1; }}
  .search-bar input::placeholder {{ color: #555; }}
  .unread-toggle {{ display: inline-flex; align-items: center; gap: 0.45rem; color: #aaa; font-size: 0.8rem; white-space: nowrap; }}
  .unread-toggle input {{ margin: 0; accent-color: #6366f1; }}
  .search-clear {{ position: absolute; right: 8px; top: 50%; transform: translateY(-50%);
                   background: none; border: none; color: #555; font-size: 1.1rem; cursor: pointer;
                   padding: 0 4px; line-height: 1; display: none; width: auto; }}
  .search-clear:hover {{ color: #ccc; background: none; }}
  .feed-stats {{ font-size: 0.8rem; color: #555; margin-bottom: 1rem; }}
  .empty {{ color: #666; text-align: center; padding: 2rem 0; }}
  .item-actions {{ margin-top: 0.4rem; display: flex; gap: 0.5rem; }}
  .action-btn {{ width: auto; padding: 0.2rem 0.5rem; font-size: 0.75rem; background: transparent;
                 color: #666; border: 1px solid #333; border-radius: 4px; cursor: pointer; font-weight: 400; }}
  .action-btn:hover {{ color: #ccc; border-color: #555; background: #1a1a1a; }}
  .reaction-btn {{ display: inline-flex; align-items: center; gap: 0.3rem; }}
  .reaction-btn .reaction-icon {{ font-size: 0.9rem; line-height: 1; }}
  .reaction-btn .reaction-count {{ color: #888; min-width: 0.9rem; text-align: right; }}
  .reaction-btn.is-active {{ color: #fcd34d; border-color: #854d0e; background: rgba(133, 77, 14, 0.16); }}
  .reaction-btn.is-active .reaction-count {{ color: #fcd34d; }}
  .delete-btn:hover {{ color: #f87171; border-color: #7f1d1d; }}
  .save-btn {{ color: #4ade80; border-color: #166534; }}
  .save-btn:hover {{ background: #052e16; }}
  .mark-unread-btn {{ color: #ccc; border-color: #555; opacity: 0; pointer-events: none; transition: opacity 0.15s, color 0.15s, border-color 0.15s, background 0.15s; }}
  .card.is-read:hover .mark-unread-btn, .card.is-read:focus-within .mark-unread-btn {{ opacity: 1; pointer-events: auto; }}
  .mark-unread-btn:hover {{ color: #fcd34d; border-color: #854d0e; background: #2a1b05; }}
  .publish-btn {{ color: #60a5fa; border-color: #1e3a5f; }}
  .publish-btn:hover {{ background: #0c1f3a; }}
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
  .meta-read-state {{ display: inline-flex; align-items: center; gap: 0.35rem; margin-right: 0.2rem; }}
  .unread-dot {{ width: 6px; height: 6px; border-radius: 50%; background: #f59e0b; display: inline-block; flex: 0 0 auto; }}
  .read-pill {{ font-size: 0.72rem; color: #888; }}
  .meta-read-state[data-state="read"] .unread-dot {{ display: none; }}
  .meta-read-state[data-state="unread"] .read-pill {{ display: none; }}
  @media (hover: none) {{
    .card.is-read .mark-unread-btn {{ opacity: 1; pointer-events: auto; }}
  }}
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
<body><div class="{wrap_class}">{body}</div></body>
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
        """POST /invite/{token}/redeem — process the invite redemption.

        Optional ``home_server`` + ``home_user_id`` (JSON body only) link
        the new local user to their home-server identity via
        ``user_remote_identities`` so federated notes from the home
        server resolve to this account locally. The redemption itself is
        the attestation: the token was issued by this server's owner and
        is single-use, so a malicious redeemer can at worst link their
        own newly-minted local user to a chosen home identity once. This
        is NOT a generic claim endpoint -- there is no surface for
        retroactively re-linking an existing account.
        """
        token = request.path_params["token"]
        d = get_db()

        # Accept both form-encoded and JSON
        content_type = request.headers.get("content-type", "")
        home_server = ""
        home_user_id = ""
        if "application/json" in content_type:
            body = await request.body()
            data = json.loads(body)
            name = data.get("name", "")
            home_server = (data.get("home_server") or "").strip()
            home_user_id = (data.get("home_user_id") or "").strip()
        else:
            form = await request.form()
            name = form.get("name", "")
            home_server = (form.get("home_server") or "").strip()
            home_user_id = (form.get("home_user_id") or "").strip()

        if not name:
            invite = d.get_invite_token(token)
            name = invite["name_hint"] if invite and invite.get("name_hint") else "New User"

        result = d.redeem_invite_token(token, name)
        if result and result.get("user") and home_server and home_user_id:
            # Best-effort: a UNIQUE conflict here means the home identity
            # was already linked to a different local user on this server.
            # We swallow the conflict rather than fail redemption -- the
            # user still got their account; only attribution-link is
            # missed. Admins can resolve via `dugg admin link`.
            d.link_remote_identity(
                local_user_id=result["user"]["id"],
                source_server=home_server,
                remote_user_id=home_user_id,
                source="invite_redeem",
            )

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
                        "tools": ["dugg_welcome", "dugg_feed", "dugg_search", "dugg_react", "dugg_unreact"],
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

    def _wants_atom(request: Request) -> bool:
        accept = request.headers.get("accept", "")
        return "application/atom+xml" in accept or "application/rss+xml" in accept

    def _render_feed_atom(request: Request, user: dict, api_key: str) -> HTMLResponse:
        """Render the Atom feed for a resolved user. `api_key` is embedded in self-link."""
        d = get_db()
        feed = d.get_feed(user["id"], limit=50)
        page_title = f"{user['name']}'s Dugg"
        srv_url = d.get_config("server_url", "")
        accessible = d._accessible_collection_ids(user["id"])
        def _skill_link(resource_id: str) -> str:
            if srv_url:
                return f"{srv_url.rstrip('/')}/s/{resource_id}"
            return f"/s/{resource_id}"

        def _xml_cdata(value: str) -> str:
            safe = (value or "").replace("]]>", "]]]]><![CDATA[>")
            return f"<![CDATA[{safe}]]>"

        def _render_atom_entry(resource: dict) -> str:
            title = resource.get("title") or resource["url"]
            author_xml = ""
            if resource.get("author"):
                author_xml = f"\n  <author><name>{_xml_escape(resource['author'])}</name></author>"
            pub_date = _resource_pub_date(resource)
            published_xml = ""
            if pub_date:
                published_xml = f"\n  <published>{pub_date}T00:00:00Z</published>"
            tags = resource.get("tags", [])
            categories_xml = ""
            if resource.get("source_type") == "skill":
                skill = d.get_skill(resource["id"])
                skill_markdown = render_skill_markdown(
                    (skill or {}).get("frontmatter") or {},
                    (skill or {}).get("body") or "",
                )
                categories_xml += '\n  <category term="dugg:skill"/>'
                for t in tags:
                    categories_xml += f'\n  <category term="{_xml_escape(t["label"])}"/>'
                summary_xml = _xml_escape(resource.get("description", ""))
                content_xml = f"\n  <content type=\"text\">{_xml_cdata(skill_markdown)}</content>"
                link_href = _skill_link(resource["id"])
            else:
                desc = resource.get("description", "")
                note = resource.get("note", "")
                sibling_notes = d.list_resource_notes(resource["id"])
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
                summary_xml = _xml_escape(content)
                content_xml = ""
                link_href = _resolve_display_url(resource["url"], srv_url)
                for t in tags:
                    categories_xml += f'\n  <category term="{_xml_escape(t["label"])}"/>'
            return f"""<entry>
  <title>{_xml_escape(title)}</title>
  <link href="{_xml_escape(link_href)}"/>
  <id>{_xml_escape(resource['id'])}</id>
  <updated>{resource['created_at']}</updated>{published_xml}{author_xml}{categories_xml}
  <summary>{summary_xml}</summary>{content_xml}
</entry>\n"""

        tombstones_xml = ""
        for coll_id in accessible:
            for tomb in d.list_recent_deletions(coll_id):
                tombstones_xml += f"""<at:deleted-entry ref="{_xml_escape(tomb['resource_id'])}" when="{tomb['deleted_at']}">
  <at:comment>Removed: {_xml_escape(tomb.get('title') or tomb['url'])}</at:comment>
  <link href="{_xml_escape(tomb['url'])}"/>
</at:deleted-entry>\n"""
                if (tomb.get("url") or "").startswith("skill://"):
                    tombstones_xml += f"""<at:deleted-entry ref="{_xml_escape(tomb['resource_id'])}" when="{tomb['deleted_at']}">
  <at:comment>Skill removed: {_xml_escape(tomb.get('title') or tomb['resource_id'])}</at:comment>
  <link href="{_xml_escape(_skill_link(tomb['resource_id']))}"/>
</at:deleted-entry>\n"""
        entries = ""
        for r in feed:
            entries += _render_atom_entry(r)
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

    def _render_feed_html(request: Request, user: dict) -> HTMLResponse:
        """Render the HTML feed for a cookie-authed user. No key appears in URLs or JS."""
        d = get_db()
        feed = d.get_feed(user["id"], limit=50)
        page_title = f"{user['name']}'s Dugg"
        page_topic = ""
        feed_reactions = _batch_feed_reactions(d, [r["id"] for r in feed], user["id"])

        submitter_cache: dict[str, str] = {}
        if not feed:
            items_html = '<p class="empty">Nothing here yet. Check back later.</p>'
        else:
            items_html = ""
            for r in feed:
                title = r.get("title") or r["url"]
                sibling_notes = d.list_resource_notes(r["id"])
                # Submitter name
                sub_id = r.get("submitted_by", "")
                if sub_id and sub_id not in submitter_cache:
                    u = d.get_user(sub_id)
                    submitter_cache[sub_id] = u["name"] if u else sub_id
                submitter_name = submitter_cache.get(sub_id, "")
                meta_bits: list[str] = []
                if r.get("author"):
                    meta_bits.append(f'<span class="author">{_xml_escape(r["author"])}</span>')
                if submitter_name:
                    meta_bits.append(f'<span class="submitted-by">{_xml_escape(submitter_name)}</span>')
                added_date = _short_date(r.get("created_at"))
                pub_date = _resource_pub_date(r)
                pub_html = f" (published {pub_date})" if pub_date and pub_date != added_date else ""
                meta_bits.append(f"{added_date}{pub_html}")
                meta_html = " · ".join(bit for bit in meta_bits if bit)
                source_type = r.get("source_type", "")
                url = r["url"]
                if url.startswith("dugg://content/"):
                    url = "/r/" + url.removeprefix("dugg://content/")
                coll_id = r.get("collection_id", "")
                source_srv = r.get("source_server") or ""
                is_submitter = sub_id == user["id"]
                is_local = not source_srv

                # Render notes (primary + siblings) as individual rows with
                # per-note edit/delete buttons gated on ownership. The JS
                # side routes primary (data-note-kind=primary) through
                # /api/edit and siblings through /api/note/edit.
                def _render_note_row(note_id: str, kind: str,
                                     who: str, text: str, origin: str,
                                     can_mutate: bool, color_class: str) -> str:
                    who_html = ""
                    if kind == "sibling":
                        origin_label = f' <span class="sib-origin">via {_xml_escape(origin)}</span>' if origin else ""
                        who_html = f'<span class="sib-who">{_xml_escape(who)}{origin_label}:</span> '
                    actions = ""
                    if can_mutate:
                        actions = (
                            '<span class="note-actions">'
                            '<button class="note-action-btn" onclick="beginNoteEdit(this)">edit</button>'
                            '<button class="note-action-btn note-action-del" onclick="deleteNoteRow(this)">delete</button>'
                            '</span>'
                        )
                    return (
                        f'<div class="note {color_class}" '
                        f'data-note-id="{_xml_escape(note_id)}" '
                        f'data-note-kind="{kind}" '
                        f'data-resource-id="{r["id"]}">'
                        f'<span class="note-body">{who_html}<span class="note-text">{_xml_escape(text)}</span></span>'
                        f'{actions}'
                        f'</div>'
                    )

                notes_html_parts: list[str] = []
                if r.get("note"):
                    primary_class = "note-local-mine" if is_submitter and is_local else ("note-remote-mine" if is_submitter else "note-other")
                    notes_html_parts.append(_render_note_row(
                        note_id="", kind="primary",
                        who=submitter_name, text=r["note"],
                        origin="", can_mutate=is_submitter,
                        color_class=primary_class,
                    ))
                for sn in sibling_notes:
                    # Direct submitter match OR remote-identity link match.
                    sib_is_mine = d.viewer_owns_note(sn, user["id"])
                    sib_class = "sibling note-local-mine" if sib_is_mine else "sibling note-other"
                    notes_html_parts.append(_render_note_row(
                        note_id=sn.get("id", ""), kind="sibling",
                        who=sn.get("submitter_name") or "someone",
                        text=sn.get("note") or "",
                        origin=sn.get("source_server") or "",
                        can_mutate=sib_is_mine,
                        color_class=sib_class,
                    ))
                notes_html = "".join(notes_html_parts)

                # Thumbnail
                thumb = r.get("thumbnail") or ""
                thumb_html = f'<img src="{_xml_escape(thumb)}" alt="" class="card-thumb" loading="lazy">' if thumb else ""
                # Description preview
                desc = r.get("description") or ""
                desc_html = f'<p class="card-desc">{_xml_escape(desc[:280])}</p>' if desc else ""
                # Tags
                tags = r.get("tags", [])
                tags_html = ""
                if tags:
                    tag_labels = [t["label"] if isinstance(t, dict) else t for t in tags]
                    tags_html = '<div class="card-tags">' + "".join(f'<span class="tag">{_xml_escape(t)}</span>' for t in tag_labels[:6]) + "</div>"
                # Source type badge
                type_badge = ""
                if source_type == "youtube":
                    type_badge = '<span class="type-badge yt">YouTube</span>'
                elif source_type == "article":
                    type_badge = '<span class="type-badge article">Article</span>'
                reaction_state = feed_reactions.get(r["id"], {})
                star_count = int(reaction_state.get("star_count", 0))
                thumbsup_count = int(reaction_state.get("thumbsup_count", 0))
                viewer_starred = "true" if reaction_state.get("viewer_starred") else "false"
                viewer_thumbsup = "true" if reaction_state.get("viewer_thumbsup") else "false"

                items_html += f"""<div class="card" id="item-{r["id"]}" data-collection="{coll_id}" data-source-server="{_xml_escape(source_srv)}" data-url="{_xml_escape(r['url'])}" data-resource-id="{r["id"]}">
  {f'<div class="card-media">{thumb_html}</div>' if thumb_html else ""}
  <div class="card-body">
    <h3><a href="{url}" target="_blank" rel="noopener" data-dugg-resource-id="{r["id"]}">{_xml_escape(title)}</a> {type_badge}</h3>
    <p class="meta"><span class="meta-read-state" data-state="unread"><span class="unread-dot" aria-hidden="true"></span><span class="read-pill">✓ read</span></span>{meta_html}</p>
    {desc_html}
    <div class="notes-block">{notes_html}</div>
    {tags_html}
    <div class="item-actions">
      <button class="action-btn reaction-btn" onclick="toggleReaction(this)" data-resource-id="{r["id"]}" data-reaction-type="star" data-active="{viewer_starred}" data-count="{star_count}" aria-pressed="{viewer_starred}">
        <span class="reaction-icon" aria-hidden="true">{"★" if viewer_starred == "true" else "☆"}</span>
        <span class="reaction-label">Star</span>
        <span class="reaction-count">{star_count}</span>
      </button>
      <button class="action-btn reaction-btn" onclick="toggleReaction(this)" data-resource-id="{r["id"]}" data-reaction-type="thumbsup" data-active="{viewer_thumbsup}" data-count="{thumbsup_count}" aria-pressed="{viewer_thumbsup}">
        <span class="reaction-icon" aria-hidden="true">{"👍" if viewer_thumbsup == "true" else "◦"}</span>
        <span class="reaction-label">Thumbs Up</span>
        <span class="reaction-count">{thumbsup_count}</span>
      </button>
      <button class="action-btn mark-unread-btn" onclick="markUnread(this)" data-resource-id="{r["id"]}" aria-label="Mark item unread">mark unread</button>
      <button class="action-btn add-note-btn" onclick="beginAddNote(this)" data-resource-id="{r["id"]}">add note</button>
      <button class="action-btn delete-btn" onclick="deleteItem('{r["id"]}')">delete item</button>
    </div>
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
        search_bar = """<div class="search-bar">
  <div class="search-input-wrap">
    <input type="text" id="feedSearch" placeholder="Search this feed... (searches full article text)" autocomplete="off">
    <button class="search-clear" id="searchClear" title="Clear search">&times;</button>
  </div>
  <label class="unread-toggle" for="unreadOnlyToggle">
    <input type="checkbox" id="unreadOnlyToggle">
    <span>Unread only</span>
  </label>
</div>
<div id="searchStatus" style="font-size:0.75rem;color:#666;margin-top:-1rem;margin-bottom:1rem;display:none;"></div>"""
        feed_js = """
<script>
// Same-origin fetch auto-includes the dugg_key cookie, so no X-Dugg-Key header
// or API key is ever written into JS or URLs on this page.
const BASE = window.location.origin;
const READ_SINCE = '1970-01-01T00:00:00+00:00';
const readResourceIds = new Set();

let searchTimeout = null;
const searchInput = document.getElementById('feedSearch');
const clearBtn = document.getElementById('searchClear');
const unreadOnlyToggle = document.getElementById('unreadOnlyToggle');
let fullTextMatchIds = null;

function getFeedCards() {
  return Array.from(document.querySelectorAll('.card[data-resource-id]'));
}

function updateCardReadUi(card) {
  if (!card) return;
  const resourceId = card.dataset.resourceId;
  const isRead = readResourceIds.has(resourceId);
  const state = card.querySelector('.meta-read-state');
  const markUnreadBtn = card.querySelector('.mark-unread-btn');
  card.classList.toggle('is-read', isRead);
  card.dataset.readState = isRead ? 'read' : 'unread';
  if (state) {
    state.dataset.state = isRead ? 'read' : 'unread';
  }
  if (markUnreadBtn) {
    markUnreadBtn.hidden = !isRead;
  }
}

function updateReactionButton(btn) {
  if (!btn) return;
  const reactionType = btn.dataset.reactionType;
  const isActive = btn.dataset.active === 'true';
  const count = Number(btn.dataset.count || '0');
  const icon = btn.querySelector('.reaction-icon');
  const countEl = btn.querySelector('.reaction-count');
  btn.classList.toggle('is-active', isActive);
  btn.setAttribute('aria-pressed', isActive ? 'true' : 'false');
  if (icon) {
    icon.textContent = reactionType === 'star'
      ? (isActive ? '★' : '☆')
      : (isActive ? '👍' : '◦');
  }
  if (countEl) {
    countEl.textContent = String(count);
  }
}

function markCardRead(resourceId) {
  if (!resourceId) return;
  readResourceIds.add(resourceId);
  updateCardReadUi(document.getElementById('item-' + resourceId));
  applyCardFilters();
}

function markCardUnreadLocal(resourceId) {
  if (!resourceId) return;
  readResourceIds.delete(resourceId);
  updateCardReadUi(document.getElementById('item-' + resourceId));
  applyCardFilters();
}

function applyCardFilters() {
  const query = (searchInput.value || '').trim().toLowerCase();
  const unreadOnly = unreadOnlyToggle.checked;
  getFeedCards().forEach(card => {
    const resourceId = card.dataset.resourceId;
    const clientMatch = !query || card.textContent.toLowerCase().includes(query);
    const serverMatch = !query || !fullTextMatchIds || fullTextMatchIds.has(resourceId);
    const isUnreadVisible = !unreadOnly || !readResourceIds.has(resourceId);
    card.style.display = (clientMatch || serverMatch) && isUnreadVisible ? '' : 'none';
  });
}

function attachOutboundReadBeacons() {
  document.querySelectorAll('a[data-dugg-resource-id]').forEach(link => {
    link.addEventListener('click', function() {
      const resourceId = this.dataset.duggResourceId;
      if (!resourceId) return;
      try {
        const payload = new Blob([JSON.stringify({ source: 'web_outbound' })], { type: 'application/json' });
        navigator.sendBeacon('/api/read/' + encodeURIComponent(resourceId), payload);
      } catch (e) {
        // Best-effort only; navigation should never be blocked.
      }
      markCardRead(resourceId);
    });
  });
}

async function loadReadStateCache() {
  try {
    const res = await fetch(BASE + '/api/read?since=' + encodeURIComponent(READ_SINCE));
    if (!res.ok) return;
    const data = await res.json();
    (data.resources || []).forEach(row => {
      if (row && row.resource_id) readResourceIds.add(row.resource_id);
    });
    getFeedCards().forEach(updateCardReadUi);
    applyCardFilters();
  } catch (e) {
    // Keep the page usable even if read-state sync fails.
  }
}

function resetSearch() {
  searchInput.value = '';
  clearBtn.style.display = 'none';
  clearTimeout(searchTimeout);
  fullTextMatchIds = null;
  applyCardFilters();
  document.getElementById('searchStatus').style.display = 'none';
}

clearBtn.addEventListener('click', resetSearch);
unreadOnlyToggle.addEventListener('change', applyCardFilters);

searchInput.addEventListener('input', function() {
  const q = this.value.trim();
  clearBtn.style.display = q ? 'block' : 'none';
  clearTimeout(searchTimeout);

  if (!q) {
    resetSearch();
    return;
  }

  const ql = q.toLowerCase();
  fullTextMatchIds = null;
  applyCardFilters();

  searchTimeout = setTimeout(async () => {
    const status = document.getElementById('searchStatus');
    status.textContent = 'Searching full text...';
    status.style.display = 'block';
    try {
      const res = await fetch(BASE + '/tools/dugg_search', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ query: q, limit: 50 }),
      });
      if (!res.ok) throw new Error('search failed');
      const data = await res.json();
      const text = typeof data === 'string' ? data : (data.text || data.result || JSON.stringify(data));
      const matchIds = new Set();
      const idRegex = /(?:\\[|id[=: ]+)([a-f0-9]{12})/gi;
      let m;
      while ((m = idRegex.exec(String(text))) !== null) matchIds.add(m[1]);

      fullTextMatchIds = matchIds;
      applyCardFilters();
      const visibleAfter = document.querySelectorAll('.card[data-url]:not([style*="display: none"])').length;
      status.textContent = visibleAfter + ' result' + (visibleAfter !== 1 ? 's' : '') + (matchIds.size > 0 ? ' (includes full-text matches)' : '');
    } catch (e) {
      status.textContent = 'Full-text search unavailable';
    }
  }, 400);
});

// --- Per-note edit/delete (primary + siblings) ---
//
// Notes render as `<div class="note" data-note-id="…" data-note-kind="primary|sibling" data-resource-id="…">`.
// Primary notes carry empty note-id and route edits through /api/edit; sibling notes carry a real id
// and route through /api/note/edit and /api/note/delete. Ownership was already decided server-side;
// non-owners never see the buttons.

function _findNoteRow(btn) {
  return btn.closest('.note');
}

function beginNoteEdit(btn) {
  const row = _findNoteRow(btn);
  if (!row) return;
  const textEl = row.querySelector('.note-text');
  const current = textEl ? textEl.textContent : '';
  // Stash the original so cancel restores it verbatim.
  row.dataset.originalText = current;
  const bodyEl = row.querySelector('.note-body');
  const actionsEl = row.querySelector('.note-actions');
  const editor = document.createElement('div');
  editor.className = 'note-edit-form';
  const ta = document.createElement('textarea');
  ta.className = 'edit-input';
  ta.value = current;
  const btnRow = document.createElement('div');
  btnRow.className = 'edit-buttons';
  const save = document.createElement('button');
  save.className = 'action-btn save-btn';
  save.textContent = 'save';
  save.onclick = () => saveNoteEdit(row, ta);
  const cancel = document.createElement('button');
  cancel.className = 'action-btn';
  cancel.textContent = 'cancel';
  cancel.onclick = () => cancelNoteEdit(row);
  btnRow.appendChild(save);
  btnRow.appendChild(cancel);
  editor.appendChild(ta);
  editor.appendChild(btnRow);
  if (bodyEl) bodyEl.style.display = 'none';
  if (actionsEl) actionsEl.style.display = 'none';
  row.appendChild(editor);
  ta.focus();
}

function cancelNoteEdit(row) {
  const form = row.querySelector('.note-edit-form');
  if (form) form.remove();
  const body = row.querySelector('.note-body');
  const actions = row.querySelector('.note-actions');
  if (body) body.style.display = '';
  if (actions) actions.style.display = '';
}

async function saveNoteEdit(row, ta) {
  const newText = (ta.value || '').trim();
  if (!newText) { alert('Note cannot be empty. Use delete instead.'); return; }
  const kind = row.dataset.noteKind;
  const noteId = row.dataset.noteId || '';
  const resourceId = row.dataset.resourceId;
  try {
    let res;
    if (kind === 'primary') {
      res = await fetch(BASE + '/api/edit', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ resource_id: resourceId, note: newText }),
      });
    } else {
      res = await fetch(BASE + '/api/note/edit', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ note_id: noteId, text: newText }),
      });
    }
    if (!res.ok) { alert('Failed to save (' + res.status + ')'); return; }
    const textEl = row.querySelector('.note-text');
    if (textEl) textEl.textContent = newText;
    cancelNoteEdit(row);
  } catch (e) { alert('Error: ' + e.message); }
}

async function deleteNoteRow(btn) {
  const row = _findNoteRow(btn);
  if (!row) return;
  if (!confirm('Delete this note?')) return;
  const kind = row.dataset.noteKind;
  const noteId = row.dataset.noteId || '';
  const resourceId = row.dataset.resourceId;
  try {
    let res;
    if (kind === 'primary') {
      res = await fetch(BASE + '/api/edit', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ resource_id: resourceId, note: '' }),
      });
    } else {
      res = await fetch(BASE + '/api/note/delete', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ note_id: noteId }),
      });
    }
    if (!res.ok) { alert('Failed to delete (' + res.status + ')'); return; }
    row.style.opacity = '0.3';
    setTimeout(() => row.remove(), 250);
  } catch (e) { alert('Error: ' + e.message); }
}

async function markUnread(btn) {
  const resourceId = btn.dataset.resourceId;
  if (!resourceId) return;
  try {
    const res = await fetch(BASE + '/api/read/' + encodeURIComponent(resourceId), {
      method: 'DELETE',
    });
    if (!res.ok) { alert('Failed to mark unread (' + res.status + ')'); return; }
    markCardUnreadLocal(resourceId);
  } catch (e) { alert('Error: ' + e.message); }
}

async function toggleReaction(btn) {
  const resourceId = btn.dataset.resourceId;
  const reactionType = btn.dataset.reactionType;
  if (!resourceId || !reactionType || btn.dataset.pending === 'true') return;
  const wasActive = btn.dataset.active === 'true';
  const wasRead = readResourceIds.has(resourceId);
  const previousCount = Number(btn.dataset.count || '0');
  btn.dataset.pending = 'true';
  btn.disabled = true;
  if (!wasActive) markCardRead(resourceId);
  try {
    if (wasActive) {
      btn.dataset.active = 'false';
      btn.dataset.count = String(Math.max(0, previousCount - 1));
      updateReactionButton(btn);
    }
    const res = await fetch(
      wasActive
        ? BASE + '/api/react/' + encodeURIComponent(resourceId) + '?type=' + encodeURIComponent(reactionType)
        : BASE + '/api/react',
      {
        method: wasActive ? 'DELETE' : 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Dugg-Surface': 'web',
        },
        body: wasActive ? undefined : JSON.stringify({ resource_id: resourceId, type: reactionType }),
      }
    );
    if (!res.ok) {
      if (wasActive) {
        btn.dataset.active = 'true';
        btn.dataset.count = String(previousCount);
        updateReactionButton(btn);
      } else if (!wasRead) {
        markCardUnreadLocal(resourceId);
      }
      alert('Failed to ' + (wasActive ? 'remove reaction' : 'react') + ' (' + res.status + ')');
      return;
    }
    if (!wasActive) {
      btn.dataset.active = 'true';
      btn.dataset.count = String(previousCount + 1);
      updateReactionButton(btn);
    }
  } catch (e) {
    if (wasActive) {
      btn.dataset.active = 'true';
      btn.dataset.count = String(previousCount);
      updateReactionButton(btn);
    } else if (!wasRead) {
      markCardUnreadLocal(resourceId);
    }
    alert('Error: ' + e.message);
  } finally {
    btn.dataset.pending = 'false';
    btn.disabled = false;
  }
}

function beginAddNote(btn) {
  const resourceId = btn.dataset.resourceId;
  const card = document.getElementById('item-' + resourceId);
  if (!card) return;
  if (card.querySelector('.add-note-form')) return; // already open
  const notesBlock = card.querySelector('.notes-block');
  const form = document.createElement('div');
  form.className = 'add-note-form';
  const ta = document.createElement('textarea');
  ta.className = 'edit-input';
  ta.placeholder = 'Your note…';
  const btnRow = document.createElement('div');
  btnRow.className = 'edit-buttons';
  const post = document.createElement('button');
  post.className = 'action-btn save-btn';
  post.textContent = 'post note';
  post.onclick = () => submitNewNote(resourceId, ta, form, card);
  const cancel = document.createElement('button');
  cancel.className = 'action-btn';
  cancel.textContent = 'cancel';
  cancel.onclick = () => form.remove();
  btnRow.appendChild(post);
  btnRow.appendChild(cancel);
  form.appendChild(ta);
  form.appendChild(btnRow);
  notesBlock.appendChild(form);
  ta.focus();
}

async function submitNewNote(resourceId, ta, form, card) {
  const text = (ta.value || '').trim();
  if (!text) return;
  try {
    const res = await fetch(BASE + '/api/note', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ resource_id: resourceId, note: text }),
    });
    if (!res.ok) { alert('Failed to post (' + res.status + ')'); return; }
    // Soft-reload the card by navigating — easier than re-rendering the
    // sibling row client-side from the returned payload (we'd need the
    // viewer's name and the ownership class logic).
    window.location.reload();
  } catch (e) { alert('Error: ' + e.message); }
}

async function deleteItem(id) {
  if (!confirm('Delete this item?')) return;
  try {
    const collectionId = document.getElementById('item-' + id).dataset.collection;
    const res = await fetch(BASE + '/tools/dugg_delete_resource', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
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
      headers: { 'Content-Type': 'application/json' },
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

attachOutboundReadBeacons();
getFeedCards().forEach(updateCardReadUi);
document.querySelectorAll('.reaction-btn').forEach(updateReactionButton);
loadReadStateCache();
</script>"""
        # Stats bar
        n_items = len(feed)
        contributors = set(submitter_cache.values())
        n_contributors = len(contributors)
        source_servers = set(r.get("source_server") or "" for r in feed) - {""}
        n_sources = len(source_servers)
        source_types = {}
        for r in feed:
            st = r.get("source_type") or "other"
            source_types[st] = source_types.get(st, 0) + 1
        def _type_label(typ: str, count: int) -> str:
            if typ == "youtube":
                return f"{count} YouTube Video{'s' if count != 1 else ''}"
            if typ == "article":
                return f"{count} Article{'s' if count != 1 else ''}"
            if typ == "email":
                return f"{count} Email{'s' if count != 1 else ''}"
            return f"{count} {typ}"
        type_parts = " · ".join(_type_label(typ, count) for typ, count in sorted(source_types.items(), key=lambda x: -x[1]) if typ != "other")
        stats_detail = f" · {type_parts}" if type_parts else ""
        contrib_label = f" · {n_contributors} contributor{'s' if n_contributors != 1 else ''}" if n_contributors > 1 else ""
        source_label = f" · {n_sources} source{'s' if n_sources != 1 else ''}" if n_sources > 0 else ""
        stats_html = f'<p class="feed-stats">{n_items} item{"s" if n_items != 1 else ""}{contrib_label}{source_label}{stats_detail}</p>' if n_items > 0 else ""

        body = f"""<h1>{page_title}</h1>
{stats_html}
{sync_html}
{topic_html}
{search_bar}
{items_html}
{feed_js}"""
        return HTMLResponse(_html_page(page_title, body, wide=True))

    def _skill_view_path(skill_id: str, server_url: str = "") -> str:
        if server_url:
            return f"{server_url.rstrip('/')}/s/{skill_id}"
        return f"/s/{skill_id}"

    def _skill_download_path(skill_id: str, server_url: str = "") -> str:
        if server_url:
            return f"{server_url.rstrip('/')}/s/{skill_id}.md"
        return f"/s/{skill_id}.md"

    def _accessible_skill_collections(d: DuggDB, user_id: str) -> dict[str, dict]:
        return {c["id"]: c for c in d.list_collections(user_id)}

    def _list_accessible_skills(d: DuggDB, user_id: str, limit: int = 50) -> list[dict]:
        accessible = d._accessible_collection_ids(user_id)
        if not accessible:
            return []
        placeholders = ",".join("?" for _ in accessible)
        rows = d.conn.execute(
            f"""SELECT r.id, r.title, r.description, r.author, r.collection_id,
                       r.submitted_by, r.created_at, r.updated_at,
                       s.name, s.supersedes_id, s.is_exportable
                FROM resources r
                JOIN skills s ON s.resource_id = r.id
                WHERE r.source_type = 'skill'
                  AND r.collection_id IN ({placeholders})
                ORDER BY r.created_at DESC
                LIMIT ?""",
            [*accessible, limit],
        ).fetchall()
        skills = []
        for row in rows:
            skill = dict(row)
            skill["is_exportable"] = bool(skill.get("is_exportable", 1))
            skills.append(skill)
        return skills

    def _get_accessible_skill(d: DuggDB, user_id: str, skill_id: str) -> Optional[dict]:
        skill = d.get_skill(skill_id)
        if not skill:
            return None
        accessible = set(d._accessible_collection_ids(user_id))
        if skill.get("collection_id") not in accessible:
            return None
        return skill

    def _render_skills_feed_html(skills: list[dict], server_url: str, collection_names: dict[str, str]) -> str:
        if not skills:
            items_html = """<div class="card">
  <div class="card-body">
    <h3>No skills yet</h3>
    <p class="card-desc" style="display:block;-webkit-line-clamp:unset;">Add one from MCP with <code>dugg_skill_add</code> or from the CLI with <code>dugg skill add path/to/SKILL.md</code>.</p>
  </div>
</div>"""
        else:
            items_html = ""
            for skill in skills:
                view_href = _skill_view_path(skill["id"], server_url)
                download_html = (
                    f'<a class="action-btn" href="{_xml_escape(_skill_download_path(skill["id"], server_url))}">Install</a>'
                    if skill.get("is_exportable", True)
                    else '<span class="action-btn" style="cursor:default;color:#888;">Server-only</span>'
                )
                items_html += f"""<div class="card" id="skill-{skill["id"]}">
  <div class="card-body">
    <p class="meta"><code>{_xml_escape(skill.get("name") or "")}</code></p>
    <h3><a href="{_xml_escape(view_href)}">{_xml_escape(skill.get("title") or skill.get("name") or skill["id"])}</a></h3>
    <p class="meta">{_xml_escape(skill.get("author") or "Unknown")} · {_xml_escape(collection_names.get(skill.get("collection_id", ""), skill.get("collection_id", "")))} · {_short_date(skill.get("created_at"))}</p>
    <p class="card-desc" style="display:block;-webkit-line-clamp:2;">{_xml_escape((skill.get("description") or "").strip())}</p>
    <div class="item-actions">
      <a class="action-btn" href="{_xml_escape(view_href)}">View</a>
      {download_html}
    </div>
    <p class="meta" style="margin-top:0.65rem;">Fork via CLI: <code>dugg skill fork {_xml_escape(skill["id"])}</code></p>
  </div>
</div>
"""
        body = f"""<h1>Skills</h1>
<p class="topic">Every skill in your accessible collections, newest first.</p>
{items_html}"""
        return _html_page("Skills", body, wide=True)

    async def handle_feed(request: Request):
        """GET /feed/{key} — content-negotiated.

        Atom/RSS requests serve the XML feed unchanged (machine clients can't
        carry a cookie). Browser HTML requests silent-migrate: set the dugg_key
        cookie and 302 to `/feed`, so the key never appears in the URL bar again.
        """
        api_key = request.path_params["key"]
        d = get_db()
        user = d.get_user_by_api_key(api_key)
        if not user:
            if _wants_atom(request):
                return _problem_response(404, "Invalid key")
            return HTMLResponse(_html_page("Not Found", "<h1>Invalid key</h1><p>This feed URL is not valid.</p>"), status_code=404)

        d.touch_user(user["id"])

        if _wants_atom(request):
            return _render_feed_atom(request, user, api_key)

        # Browser visit: silent migrate to cookie-authed bare path.
        from starlette.responses import RedirectResponse
        resp = RedirectResponse(url="/feed", status_code=303)
        _set_session_cookie(resp, api_key, request)
        return resp

    async def handle_feed_bare(request: Request):
        """GET /feed — cookie-authed HTML feed. Redirects to /session/unlock if unauthed."""
        user = _resolve_user_from_cookie_or_header(request)
        if not user:
            from starlette.responses import RedirectResponse
            return RedirectResponse(url="/session/unlock?return_to=/feed", status_code=303)
        get_db().touch_user(user["id"])
        return _render_feed_html(request, user)

    # --- Compact URL cache for Chrome extension ---

    async def handle_feed_urls(request: Request):
        """GET /feed/urls/{key} — compact JSON of all URLs for extension cache.

        Returns a lightweight payload for the Chrome extension to sync hourly.
        Used for badge matching and StumbleUpon-style discovery.
        """
        api_key = request.path_params["key"]
        d = get_db()
        user = d.get_user_by_api_key(api_key)
        if not user:
            return JSONResponse({"error": "invalid key"}, status_code=404)

        d.touch_user(user["id"])
        feed = d.get_feed(user["id"], limit=500)
        submitter_cache: dict[str, str] = {}
        read_states = d.batch_read_states(user["id"], [r["id"] for r in feed])

        entries = []
        for r in feed:
            sub_id = r.get("submitted_by", "")
            if sub_id and sub_id not in submitter_cache:
                u = d.get_user(sub_id)
                submitter_cache[sub_id] = u["name"] if u else sub_id
            submitter_name = submitter_cache.get(sub_id, "")

            entries.append({
                "url": r["url"],
                "title": r.get("title") or "",
                "id": r["id"],
                "by": submitter_name,
                "read_at": (read_states.get(r["id"]) or {}).get("read_at"),
            })

        return JSONResponse({"urls": entries, "count": len(entries)})

    # --- Structured JSON API (mobile / typed clients) ---
    #
    # The /tools/{tool_name} endpoint returns MCP text blobs intended for
    # agent consumption. Mobile clients need structured records they can
    # decode into models, so these endpoints return the raw dicts directly
    # from DuggDB rather than formatted text. Auth is X-Dugg-Key header
    # (same as /tools/*), resolved via resolve_user_from_request.

    def _can_mutate_resource(d: DuggDB, r: dict, user_id: str) -> bool:
        """Submitter-or-owner gate shared by edit/delete authorization."""
        if r.get("submitted_by") == user_id:
            return True
        member = d.get_member_status(r.get("collection_id", ""), user_id)
        return bool(member and member["role"] == "owner")

    def _serialize_resource(
        d: DuggDB,
        r: dict,
        submitter_cache: dict[str, str],
        sibling_notes: Optional[list[dict]] = None,
        *,
        viewer_id: str = "",
        edit_count: Optional[int] = None,
    ) -> dict:
        """Map a DB resource row to the JSON shape the iOS client decodes.

        `sibling_notes` is the list from ``resource_notes`` for this resource
        (pass-through so callers can batch the lookup). When provided, the
        serializer emits a `notes` array that merges the primary submitter's
        note with any sibling notes in chronological order. Cross-server
        ingest stores the incoming note as a sibling note (quarantined from
        re-federation), so this is the only way iOS sees notes on resources
        that arrived via /ingest.
        """
        sub_id = r.get("submitted_by", "") or ""
        if sub_id and sub_id not in submitter_cache:
            u = d.get_user(sub_id)
            submitter_cache[sub_id] = u["name"] if u else sub_id

        # Pull published_at from raw_metadata (matches _mcp_pub_date logic).
        published_at = ""
        raw = r.get("raw_metadata")
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (ValueError, TypeError):
                raw = None
        if isinstance(raw, dict):
            val = raw.get("published_at") or raw.get("updated_at") or ""
            s = str(val).strip()
            if len(s) >= 10 and s[4] == "-" and s[7] == "-":
                published_at = s[:10]

        tags = [t["label"] for t in r.get("tags", []) if isinstance(t, dict) and t.get("label")]

        notes: list[dict] = []
        if r.get("note"):
            # Primary note is attached to the resource row (resource.note)
            # rather than resource_notes. It carries no stable note-level
            # id, so clients address it as id="" and route edits through
            # /api/edit (resource-level) instead of /api/note/edit.
            notes.append({
                "id": "",
                "author": submitter_cache.get(sub_id, ""),
                "note": r["note"],
                "added_at": r.get("created_at") or "",
                "source_server": r.get("source_server") or "",
                "can_edit": bool(viewer_id and sub_id == viewer_id),
                "can_delete": bool(viewer_id and sub_id == viewer_id),
            })
        for sn in sibling_notes or []:
            # Two ways to be the author: direct local attribution OR a
            # user_remote_identities link from viewer to (source_server,
            # submitter_remote_id). The DB helper does both checks.
            mine = d.viewer_owns_note(sn, viewer_id) if viewer_id else False
            notes.append({
                "id": sn.get("id") or "",
                "author": sn.get("submitter_name") or "",
                "note": sn.get("note") or "",
                "added_at": sn.get("added_at") or "",
                "source_server": sn.get("source_server") or "",
                "can_edit": mine,
                "can_delete": mine,
            })

        source_type = r.get("source_type") or ""
        payload = {
            "id": r["id"],
            "url": r["url"],
            "title": r.get("title") or "",
            "description": r.get("description") or "",
            "body": r.get("transcript") or "",
            "note": r.get("note") or "",
            "notes": notes,
            "tags": tags,
            "submitter": submitter_cache.get(sub_id, ""),
            "added_at": r.get("created_at") or "",
            "published_at": published_at,
            "source_type": source_type,
            "source_label": r.get("source_server") or "",
            "thumbnail": r.get("thumbnail") or "",
        }
        hints = hints_for(source_type, r["url"])
        if hints is not None:
            payload["source_hints"] = hints

        if viewer_id:
            can_mutate = _can_mutate_resource(d, r, viewer_id)
            payload["can_edit"] = can_mutate
            payload["can_delete"] = can_mutate
        if edit_count is not None:
            payload["edit_count"] = edit_count

        return payload

    def _serialize_feed_url_entries(d: DuggDB, user_id: str, feed: list[dict]) -> list[dict]:
        submitter_cache: dict[str, str] = {}
        read_states = d.batch_read_states(user_id, [r["id"] for r in feed])
        entries = []
        for r in feed:
            sub_id = r.get("submitted_by", "")
            if sub_id and sub_id not in submitter_cache:
                u = d.get_user(sub_id)
                submitter_cache[sub_id] = u["name"] if u else sub_id
            entries.append({
                "url": r["url"],
                "title": r.get("title") or "",
                "id": r["id"],
                "by": submitter_cache.get(sub_id, ""),
                "read_at": (read_states.get(r["id"]) or {}).get("read_at"),
            })
        return entries

    async def handle_api_feed(request: Request):
        """GET /api/feed — structured JSON feed for typed clients (iOS, etc.).

        Auth: X-Dugg-Key header (or dugg_key cookie).
        Query: ?limit=N (default 50, max 500).
        Response: {"resources": [{id, url, title, description, note, notes, tags, submitter, added_at, published_at, source_label}, ...]}

        `notes` is an array of `{author, note, added_at, source_server}` merging
        the primary submitter's note with any sibling notes from cross-server
        ingest. Clients should prefer `notes` over the single `note` field.
        """
        try:
            user = resolve_user_from_request(request)
        except ValueError as e:
            return _problem_response(401, str(e))

        try:
            limit = int(request.query_params.get("limit", "50"))
        except ValueError:
            limit = 50
        limit = max(1, min(limit, 500))
        unread = _query_flag(request, "unread")

        d = get_db()
        d.mark_invite_onboarded(user["id"])
        feed = d.get_feed(user["id"], limit=limit, unread=unread)

        submitter_cache: dict[str, str] = {}
        notes_by_id = d.batch_resource_notes([r["id"] for r in feed])
        resources = [
            _serialize_resource(d, r, submitter_cache, notes_by_id.get(r["id"]))
            for r in feed
        ]
        return JSONResponse({"resources": resources, "count": len(resources)})

    async def handle_api_feed_urls(request: Request):
        """GET /api/feed/urls — compact JSON feed for typed clients."""
        try:
            user = resolve_user_from_request(request)
        except ValueError as e:
            return _problem_response(401, str(e))

        try:
            limit = int(request.query_params.get("limit", "500"))
        except ValueError:
            limit = 500
        limit = max(1, min(limit, 500))
        unread = _query_flag(request, "unread")

        d = get_db()
        d.mark_invite_onboarded(user["id"])
        feed = d.get_feed(user["id"], limit=limit, unread=unread)
        entries = _serialize_feed_url_entries(d, user["id"], feed)
        return JSONResponse({"urls": entries, "count": len(entries)})

    async def handle_api_read(request: Request):
        try:
            user = resolve_user_from_request(request)
        except ValueError as e:
            return _problem_response(401, str(e))

        d = get_db()
        if request.method == "GET":
            since_iso = (request.query_params.get("since") or "").strip()
            if not since_iso:
                return _problem_response(400, "Missing since")
            cursor = (request.query_params.get("cursor") or "").strip() or None
            try:
                limit = int(request.query_params.get("limit", "200"))
            except ValueError:
                limit = 200
            return JSONResponse(d.list_read_since(user["id"], since_iso, cursor=cursor, limit=limit))

        resource_id = (request.path_params.get("resource_id") or "").strip()
        if not resource_id:
            return _problem_response(400, "Missing resource_id")
        resource = d.get_resource(resource_id)
        if not resource:
            return _problem_response(404, "Resource not found")
        if resource.get("collection_id") not in d._accessible_collection_ids(user["id"]):
            return _problem_response(404, "Resource not found")

        if request.method == "DELETE":
            d.unmark_read(user["id"], resource_id)
            return JSONResponse({"status": "ok"})

        try:
            body = await request.json()
        except Exception:
            return _problem_response(400, "Invalid JSON payload")
        source = (body.get("source") or "").strip()
        if source not in READ_STATE_SOURCES:
            return _problem_response(400, "Invalid read source")
        return JSONResponse(d.mark_read(user["id"], resource_id, source))

    async def handle_api_react(request: Request):
        try:
            user = resolve_user_from_request(request)
        except ValueError as e:
            return _problem_response(401, str(e))

        if request.method == "DELETE":
            resource_id = (request.path_params.get("resource_id") or "").strip()
            reaction_type = (request.query_params.get("type") or "").strip().lower()
        else:
            try:
                body = await request.json()
            except Exception:
                return _problem_response(400, "Invalid JSON payload")
            resource_id = (body.get("resource_id") or "").strip()
            reaction_type = (body.get("type") or "").strip().lower()

        if not resource_id:
            return _problem_response(400, "Missing resource_id")
        if reaction_type == "tap":
            return JSONResponse(
                {"error": "reaction_type 'tap' is no longer supported; use POST /api/read instead"},
                status_code=400,
            )
        if reaction_type not in {"star", "thumbsup"}:
            return _problem_response(400, "Invalid reaction_type")

        d = get_db()
        resource = d.get_resource(resource_id)
        if not resource:
            return _problem_response(404, "Resource not found")
        if resource.get("collection_id") not in d._accessible_collection_ids(user["id"]):
            return _problem_response(404, "Resource not found")

        if request.method == "DELETE":
            removed = d.unreact(user["id"], resource_id, reaction_type)
            return JSONResponse({"removed": removed})

        reaction = d.react_to_resource(resource_id, user["id"], reaction_type)
        d.mark_read(user["id"], resource_id, _reaction_implicit_source(request))
        return JSONResponse({"reaction": reaction})

    async def handle_api_search(request: Request):
        """GET /api/search?q=... — structured JSON search for typed clients.

        Auth: X-Dugg-Key header (or dugg_key cookie).
        Query: ?q=...&limit=N (default 20, max 100).
        Response: {"resources": [...same shape as /api/feed...]}
        """
        try:
            user = resolve_user_from_request(request)
        except ValueError as e:
            return _problem_response(401, str(e))

        query = (request.query_params.get("q") or "").strip()
        if not query:
            return JSONResponse({"resources": [], "count": 0})

        try:
            limit = int(request.query_params.get("limit", "20"))
        except ValueError:
            limit = 20
        limit = max(1, min(limit, 100))

        d = get_db()
        d.mark_invite_onboarded(user["id"])
        hits = d.search(query, user["id"], limit=limit)

        submitter_cache: dict[str, str] = {}
        notes_by_id = d.batch_resource_notes([r["id"] for r in hits])
        resources = [
            _serialize_resource(d, r, submitter_cache, notes_by_id.get(r["id"]))
            for r in hits
        ]
        return JSONResponse({"resources": resources, "count": len(resources)})

    async def handle_api_resource(request: Request):
        """GET /api/resource/{id} — structured JSON for a single resource.

        Auth: X-Dugg-Key header (or dugg_key cookie). 404 if the caller can't
        see the resource via accessible collections.
        Response: {"resource": {id, url, title, description, body, note, tags, submitter, added_at, published_at, source_label}}
        """
        try:
            user = resolve_user_from_request(request)
        except ValueError as e:
            return _problem_response(401, str(e))

        resource_id = request.path_params["id"]
        d = get_db()
        r = d.get_resource(resource_id)
        if not r:
            return _problem_response(404, "Resource not found")

        accessible = d._accessible_collection_ids(user["id"])
        if r.get("collection_id") not in accessible:
            return _problem_response(404, "Resource not found")

        submitter_cache: dict[str, str] = {}
        sibs = d.list_resource_notes(resource_id)
        edit_count = len(d.list_resource_edits(resource_id))
        return JSONResponse({"resource": _serialize_resource(
            d, r, submitter_cache, sibs,
            viewer_id=user["id"], edit_count=edit_count,
        )})

    async def handle_api_note(request: Request):
        """POST /api/note — attach a sibling note to an existing resource.

        Auth: X-Dugg-Key header (or dugg_key cookie).
        Body: {"resource_id": "...", "note": "..."}
        Any authenticated user who can see the resource (via accessible
        collections) can add their own sibling note. The note is attributed
        to the caller's user id + display name. Idempotent via the
        resource_notes UNIQUE constraint — the same author posting the same
        text silently no-ops.

        Response: {"note": {author, note, added_at, source_server}} — the
        serialized note in the same shape the feed/search/resource endpoints
        emit inside their `notes[]` arrays.
        """
        try:
            user = resolve_user_from_request(request)
        except ValueError as e:
            return _problem_response(401, str(e))

        try:
            body = await request.json()
        except Exception:
            return _problem_response(400, "Invalid JSON payload")

        resource_id = (body.get("resource_id") or "").strip()
        note_text = (body.get("note") or "").strip()
        if not resource_id:
            return _problem_response(400, "Missing resource_id")
        if not note_text:
            return _problem_response(400, "Missing note")

        d = get_db()
        resource = d.get_resource(resource_id)
        if not resource:
            return _problem_response(404, "Resource not found")

        accessible = d._accessible_collection_ids(user["id"])
        if resource.get("collection_id") not in accessible:
            return _problem_response(404, "Resource not found")

        result = d.add_resource_note(
            resource_id,
            note_text,
            submitter_user_id=user["id"],
            submitter_name=user.get("name", ""),
        )
        if not result:
            return _problem_response(400, "Empty note")

        return JSONResponse({
            "note": {
                "id": result.get("id", ""),
                "author": user.get("name", ""),
                "note": note_text,
                "added_at": result.get("added_at", ""),
                "source_server": "",
                "can_edit": True,
                "can_delete": True,
            }
        }, status_code=201)

    async def handle_api_note_edit(request: Request):
        """POST /api/note/edit — edit a sibling note you authored.

        Body: {"note_id": "...", "text": "..."}
        The primary note on a resource is not addressable here (id="" in
        the serializer) — callers editing the primary note route through
        /api/edit with field=note instead.

        Logs to ``resource_edits`` with ``field='note'`` so note-swap
        attacks show up in the audit trail alongside URL/title changes.
        """
        try:
            user = resolve_user_from_request(request)
        except ValueError as e:
            return _problem_response(401, str(e))
        try:
            body = await request.json()
        except Exception:
            return _problem_response(400, "Invalid JSON payload")

        note_id = (body.get("note_id") or "").strip()
        text = (body.get("text") or "").strip()
        if not note_id:
            return _problem_response(400, "Missing note_id")
        if not text:
            return _problem_response(400, "Missing text")

        d = get_db()
        existing = d.get_resource_note(note_id)
        if not existing:
            return _problem_response(404, "Note not found")
        # Visibility gate: caller must see the parent resource.
        resource = d.get_resource(existing["resource_id"])
        if not resource or resource.get("collection_id") not in d._accessible_collection_ids(user["id"]):
            return _problem_response(404, "Note not found")
        try:
            updated = d.update_resource_note(note_id, text, user["id"])
        except PermissionError:
            return _problem_response(403, "Only the note author can edit this note")
        if not updated:
            return _problem_response(400, "Empty note")

        return JSONResponse({
            "note": {
                "id": updated["id"],
                "author": updated.get("submitter_name") or "",
                "note": updated["note"],
                "added_at": updated.get("added_at", ""),
                "source_server": updated.get("source_server") or "",
                "can_edit": True,
                "can_delete": True,
            }
        })

    async def handle_api_note_delete(request: Request):
        """POST /api/note/delete — delete a sibling note you authored.

        Body: {"note_id": "..."}
        """
        try:
            user = resolve_user_from_request(request)
        except ValueError as e:
            return _problem_response(401, str(e))
        try:
            body = await request.json()
        except Exception:
            return _problem_response(400, "Invalid JSON payload")

        note_id = (body.get("note_id") or "").strip()
        if not note_id:
            return _problem_response(400, "Missing note_id")

        d = get_db()
        existing = d.get_resource_note(note_id)
        if not existing:
            return _problem_response(404, "Note not found")
        resource = d.get_resource(existing["resource_id"])
        if not resource or resource.get("collection_id") not in d._accessible_collection_ids(user["id"]):
            return _problem_response(404, "Note not found")
        try:
            ok = d.delete_resource_note(note_id, user["id"])
        except PermissionError:
            return _problem_response(403, "Only the note author can delete this note")
        if not ok:
            return _problem_response(404, "Note not found")
        return JSONResponse({"status": "deleted", "note_id": note_id})

    async def handle_api_edit(request: Request):
        """POST /api/edit — modify a resource the caller owns (or owns the
        collection of).

        Auth: X-Dugg-Key header (or dugg_key cookie).
        Body: {"resource_id": "...", "url"?, "title"?, "description"?,
               "note"?, "source_type"?, "author"?, "tags"? }
        Unknown fields are ignored. Only the submitter or collection owner
        may edit; everyone else gets 403. Every user-visible field change
        is written to `resource_edits` so the community can audit content
        mutations (link-swap, note-swap) after the fact.

        Response: {"resource": {...updated serialization...}}
        """
        try:
            user = resolve_user_from_request(request)
        except ValueError as e:
            return _problem_response(401, str(e))

        try:
            body = await request.json()
        except Exception:
            return _problem_response(400, "Invalid JSON payload")

        resource_id = (body.get("resource_id") or "").strip()
        if not resource_id:
            return _problem_response(400, "Missing resource_id")

        d = get_db()
        resource = d.get_resource(resource_id)
        if not resource:
            return _problem_response(404, "Resource not found")

        accessible = d._accessible_collection_ids(user["id"])
        if resource.get("collection_id") not in accessible:
            return _problem_response(404, "Resource not found")

        if not _can_mutate_resource(d, resource, user["id"]):
            return _problem_response(403, "Only the submitter or collection owner can edit this resource")

        editable = {"url", "title", "description", "note", "source_type", "author"}
        update_fields = {k: body[k] for k in editable if k in body and body[k] is not None}

        tags = body.get("tags")
        if isinstance(tags, list):
            for tag in tags:
                if isinstance(tag, str) and tag.strip():
                    d._add_tag(resource_id, tag.strip(), "user", _now())
            d.conn.commit()

        if update_fields:
            d.update_resource(resource_id, actor_id=user["id"], **update_fields)

        updated = d.get_resource(resource_id)
        submitter_cache: dict[str, str] = {}
        sibs = d.list_resource_notes(resource_id)
        edit_count = len(d.list_resource_edits(resource_id))
        return JSONResponse({"resource": _serialize_resource(
            d, updated, submitter_cache, sibs,
            viewer_id=user["id"], edit_count=edit_count,
        )})

    async def handle_api_resource_edits(request: Request):
        """GET /api/resource/{id}/edits — audit trail for a resource.

        Auth: any authenticated caller who can see the resource via their
        accessible collections. Transparency default: if you can view a
        resource, you can see whether its content has been rewritten.

        Response: {"edits": [{id, actor, actor_id, field, old_value,
                              new_value, edited_at}, ...]} newest first.
        """
        try:
            user = resolve_user_from_request(request)
        except ValueError as e:
            return _problem_response(401, str(e))

        resource_id = request.path_params["id"]
        d = get_db()
        resource = d.get_resource(resource_id)
        if not resource:
            return _problem_response(404, "Resource not found")

        accessible = d._accessible_collection_ids(user["id"])
        if resource.get("collection_id") not in accessible:
            return _problem_response(404, "Resource not found")

        rows = d.list_resource_edits(resource_id)
        edits = [{
            "id": r["id"],
            "actor_id": r["actor_id"],
            "actor": r.get("actor_name") or "",
            "field": r["field"],
            "old_value": r["old_value"],
            "new_value": r["new_value"],
            "edited_at": r["edited_at"],
        } for r in rows]
        return JSONResponse({"edits": edits, "count": len(edits)})

    # --- Note Publishing (upstream federation) ---

    async def handle_publish_note(request: Request):
        """POST /publish-note or /publish-note/{key} — push a local note upstream.

        Body: {"resource_id": "...", "note": "..."}
        Auth: dugg_key cookie, X-Dugg-Key header, or legacy path key.
        The resource must have a source_server set (i.e. it came from an RSS sync).
        The user's API key for that server is extracted from their RSS subscription
        feed URL. The note is POSTed to the source server's /tools/dugg_add endpoint,
        which will attach it as a sibling note via the URL collision path.
        """
        d = get_db()
        user = _resolve_user_from_cookie_or_header(request)
        if not user:
            api_key = request.path_params.get("key", "")
            user = d.get_user_by_api_key(api_key) if api_key else None
        if not user:
            return JSONResponse({"error": "Invalid key"}, status_code=401)

        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON"}, status_code=400)

        resource_id = (body.get("resource_id") or "").strip()
        note = (body.get("note") or "").strip()
        if not resource_id or not note:
            return JSONResponse({"error": "resource_id and note are required"}, status_code=400)

        resource = d.get_resource(resource_id)
        if not resource:
            return JSONResponse({"error": "Resource not found"}, status_code=404)

        source_server = (resource.get("source_server") or "").strip()
        if not source_server:
            return JSONResponse({"error": "Resource has no source server — cannot publish upstream"}, status_code=400)

        # Find the user's RSS subscription for this source server to extract their remote API key
        subs = d.list_rss_subscriptions(user["id"])
        remote_key = ""
        for sub in subs:
            feed_url = sub.get("feed_url", "")
            parsed = urlparse(feed_url)
            sub_origin = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme else ""
            if sub_origin == source_server:
                # Extract API key from feed URL path: /feed/{key}
                path_parts = parsed.path.strip("/").split("/")
                if len(path_parts) >= 2 and path_parts[0] == "feed":
                    remote_key = path_parts[1]
                    break

        if not remote_key:
            return JSONResponse({"error": f"No subscription found for {source_server} — cannot authenticate"}, status_code=400)

        # Resolve the resource URL for the upstream server
        url = resource["url"]
        if url.startswith("dugg://content/"):
            # Local-only content can't be published by URL match — skip
            return JSONResponse({"error": "Cannot publish notes on local-only content (no external URL)"}, status_code=400)

        # POST to the source server's dugg_add — the collision path will attach the note as a sibling
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    f"{source_server.rstrip('/')}/tools/dugg_add",
                    headers={
                        "Content-Type": "application/json",
                        "X-Dugg-Key": remote_key,
                    },
                    json={
                        "url": url,
                        "note": note,
                        "title": resource.get("title", ""),
                    },
                )
            if resp.status_code >= 400:
                logger.warning(f"Publish note failed: {source_server} returned {resp.status_code}")
                return JSONResponse({"error": f"Upstream server returned {resp.status_code}"}, status_code=502)
        except (httpx.HTTPError, httpx.TimeoutException) as e:
            logger.warning(f"Publish note failed: {e}")
            return JSONResponse({"error": f"Could not reach {source_server}"}, status_code=502)

        logger.info(f"Published note upstream to {source_server} for resource {resource_id}")
        return JSONResponse({"status": "published", "source_server": source_server})

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

    # --- Shared unlock form + session endpoints ---
    # `/session/unlock` is the canonical paste-your-key form for every browser-served
    # authenticated endpoint (/feed, /paste, /admin, /appeal, /r/{id}). On submit,
    # validates the key, sets the dugg_key cookie, and redirects to `return_to`.
    # The resource viewer (/r/{id}) is a thin wrapper that specifies its own return_to.

    def _unlock_form_html(return_to: str = "/feed", error: str = "") -> str:
        err_html = f'<p style="color:#f87171;margin-top:0.5rem;font-size:0.9rem;">{_xml_escape(error)}</p>' if error else ""
        return _html_page(
            "Unlock",
            f"""<h1>Unlock</h1>
<p style="margin-top:0.5rem;color:#aaa;">This page is only visible to Dugg members of this server. Paste your Dugg key to view.</p>
<form method="POST" action="/session/unlock" style="margin-top:1rem;">
  <input type="hidden" name="return_to" value="{_xml_escape(return_to)}">
  <input type="password" name="key" placeholder="dugg_..." autofocus required
         style="width:100%;padding:0.6rem;background:#1a1a1a;border:1px solid #333;color:#eee;border-radius:4px;font-family:monospace;">
  <button type="submit" style="margin-top:0.5rem;padding:0.6rem 1rem;background:#2563eb;color:white;border:0;border-radius:4px;cursor:pointer;">Unlock</button>
  {err_html}
</form>""",
        )

    async def handle_session_unlock_get(request: Request):
        """GET /session/unlock?return_to=/feed — render the paste-key form."""
        return_to = _safe_return_to(request.query_params.get("return_to"))
        return HTMLResponse(_unlock_form_html(return_to))

    async def handle_session_unlock_post(request: Request):
        """POST /session/unlock — validate posted key, set dugg_key cookie, redirect to return_to."""
        form = await request.form()
        key = (form.get("key") or "").strip()
        return_to = _safe_return_to(form.get("return_to"))
        d = get_db()
        user = d.get_user_by_api_key(key) if key else None
        if not user:
            return HTMLResponse(_unlock_form_html(return_to, error="Invalid key."), status_code=401)
        from starlette.responses import RedirectResponse
        resp = RedirectResponse(url=return_to, status_code=303)
        _set_session_cookie(resp, key, request)
        return resp

    async def handle_session_clear(request: Request):
        """GET /session/clear?return_to=/ — delete cookie and redirect (logout)."""
        return_to = _safe_return_to(request.query_params.get("return_to"), default="/")
        from starlette.responses import RedirectResponse
        resp = RedirectResponse(url=return_to, status_code=303)
        resp.delete_cookie(COOKIE_NAME, path="/")
        return resp

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

    def _render_skill_page(
        skill: dict,
        server_url: str,
        collection_names: dict[str, str],
        supersedes_url: str = "",
        history: list[dict] | None = None,
    ) -> str:
        title = skill.get("title") or skill.get("name") or skill["id"]
        supersedes_id = skill.get("supersedes_id") or ""
        if supersedes_id and supersedes_url:
            supersedes_html = f' · supersedes <a href="{_xml_escape(supersedes_url)}"><code>{_xml_escape(supersedes_id)}</code></a>'
        elif supersedes_id:
            supersedes_html = f" · supersedes <code>{_xml_escape(supersedes_id)}</code>"
        else:
            supersedes_html = ""
        exportable_badge = (
            '<span class="tag" style="margin-left:0.5rem;color:#4ade80;border-color:#166534;background:#052e16;">Exportable</span>'
            if skill.get("is_exportable", True)
            else '<span class="tag" style="margin-left:0.5rem;color:#fbbf24;border-color:#854d0e;background:#1c1917;">Server-only</span>'
        )
        download_html = (
            f'<a class="action-btn" href="{_xml_escape(_skill_download_path(skill["id"], server_url))}">Download SKILL.md</a>'
            if skill.get("is_exportable", True)
            else '<p class="meta" style="margin-top:0.75rem;">This skill is not shareable — viewable on this server only.</p>'
        )
        history_html = ""
        if history:
            items = []
            for item in history:
                item_title = item.get("title") or item.get("name") or item["id"]
                item_url = _skill_view_path(item["id"], server_url)
                items.append(
                    f'<li><a href="{_xml_escape(item_url)}">{_xml_escape(item_title)}</a> '
                    f'<code>{_xml_escape(item["id"])}</code> '
                    f'<span class="meta">· {_short_date(item.get("created_at"))}</span></li>'
                )
            history_html = (
                '<section style="margin-top:1.25rem;">'
                '<h2 style="margin-bottom:0.5rem;">Version history</h2>'
                f'<ol style="margin:0;padding-left:1.25rem;line-height:1.7;">{"".join(items)}</ol>'
                '</section>'
            )
        body = f"""<div class="card" style="max-width:none;">
  <div class="card-body">
    <h1>{_xml_escape(title)}</h1>
    <p class="meta">{_xml_escape(skill.get("author") or "Unknown")} · {_xml_escape(collection_names.get(skill.get("collection_id", ""), skill.get("collection_id", "")))} · {_short_date(skill.get("created_at"))}{supersedes_html} {exportable_badge}</p>
    <div class="item-actions" style="margin-bottom:0.75rem;">
      {download_html}
    </div>
    <p class="meta" style="margin-bottom:1rem;">Fork via CLI: <code>dugg skill fork {_xml_escape(skill["id"])}</code></p>
    <pre class="skill-body" style="margin-top:1rem;padding:1rem;background:#111;border:1px solid #333;border-radius:8px;color:#ddd;white-space:pre-wrap;word-break:break-word;overflow:auto;font-size:0.85rem;line-height:1.6;">{_xml_escape(skill.get("body") or "")}</pre>
    {history_html}
  </div>
</div>"""
        return _html_page(_xml_escape(title), body, wide=True)

    async def handle_resource_page(request: Request):
        """GET /r/{resource_id} — render a resource if viewer has access.

        Unauthenticated: show a form to paste a key (no key in URL ever).
        Authenticated via cookie or header: render if viewer is a member of a
        collection that contains the resource."""
        resource_id = request.path_params["resource_id"]
        d = get_db()
        user = _resolve_user_from_cookie_or_header(request)
        if not user:
            return HTMLResponse(_unlock_form_html(return_to=f"/r/{resource_id}"), status_code=401)

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
        d.mark_read(user["id"], resource["id"], "web_detail")
        return HTMLResponse(_render_resource(resource, sibling_notes=siblings))

    async def handle_resource_unlock(request: Request):
        """POST /r/{resource_id}/unlock — legacy alias for /session/unlock with resource return_to.

        Preserved for any bookmarked forms; the generalized `/session/unlock`
        endpoint is preferred.
        """
        resource_id = request.path_params["resource_id"]
        form = await request.form()
        key = (form.get("key") or "").strip()
        return_to = f"/r/{resource_id}"
        d = get_db()
        user = d.get_user_by_api_key(key) if key else None
        if not user:
            return HTMLResponse(_unlock_form_html(return_to, error="Invalid key."), status_code=401)
        from starlette.responses import RedirectResponse
        resp = RedirectResponse(url=return_to, status_code=303)
        _set_session_cookie(resp, key, request)
        return resp

    async def handle_skills_page(request: Request):
        """GET /skills — cookie/header authed skill feed across accessible collections."""
        d = get_db()
        user = _resolve_user_from_cookie_or_header(request)
        if not user:
            return HTMLResponse(_unlock_form_html(return_to="/skills"), status_code=401)

        d.touch_user(user["id"])
        collections = _accessible_skill_collections(d, user["id"])
        html = _render_skills_feed_html(
            _list_accessible_skills(d, user["id"], limit=50),
            d.get_config("server_url", ""),
            {coll_id: coll.get("name", coll_id) for coll_id, coll in collections.items()},
        )
        return HTMLResponse(html)

    async def handle_skill_page(request: Request):
        """GET /s/{skill_id} — render a raw SKILL.md body viewer for an accessible skill."""
        skill_id = request.path_params["skill_id"]
        d = get_db()
        user = _resolve_user_from_cookie_or_header(request)
        if not user:
            return HTMLResponse(_unlock_form_html(return_to=f"/s/{skill_id}"), status_code=401)

        d.touch_user(user["id"])
        skill = _get_accessible_skill(d, user["id"], skill_id)
        if not skill:
            return HTMLResponse(_html_page("Not Found", "<h1>Not found</h1>"), status_code=404)

        server_url = d.get_config("server_url", "")
        collections = _accessible_skill_collections(d, user["id"])
        supersedes_url = ""
        if skill.get("supersedes_id"):
            if _get_accessible_skill(d, user["id"], skill["supersedes_id"]):
                supersedes_url = _skill_view_path(skill["supersedes_id"], server_url)
        history = [
            item for item in d.get_skill_history(skill["id"])
            if item.get("collection_id") in collections
        ]
        html = _render_skill_page(
            skill,
            server_url,
            {coll_id: coll.get("name", coll_id) for coll_id, coll in collections.items()},
            supersedes_url=supersedes_url,
            history=history,
        )
        return HTMLResponse(html)

    async def handle_skill_unlock(request: Request):
        """POST /s/{skill_id}/unlock — legacy alias for /session/unlock with skill return_to."""
        skill_id = request.path_params["skill_id"]
        form = await request.form()
        key = (form.get("key") or "").strip()
        return_to = f"/s/{skill_id}"
        d = get_db()
        user = d.get_user_by_api_key(key) if key else None
        if not user:
            return HTMLResponse(_unlock_form_html(return_to, error="Invalid key."), status_code=401)
        from starlette.responses import RedirectResponse
        resp = RedirectResponse(url=return_to, status_code=303)
        _set_session_cookie(resp, key, request)
        return resp

    async def handle_skill_download(request: Request):
        """GET /s/{skill_id}.md — download rendered SKILL.md for accessible exportable skills."""
        skill_id = request.path_params["skill_id"]
        d = get_db()
        user = _resolve_user_from_cookie_or_header(request)
        if not user:
            return HTMLResponse(_unlock_form_html(return_to=f"/s/{skill_id}.md"), status_code=401)

        d.touch_user(user["id"])
        skill = _get_accessible_skill(d, user["id"], skill_id)
        if not skill:
            return JSONResponse({"error": "Skill not found."}, status_code=404)
        if not skill.get("is_exportable", True):
            return JSONResponse({"error": "Skill is not exportable."}, status_code=403)

        markdown = render_skill_markdown(skill.get("frontmatter") or {}, skill.get("body") or "")
        filename = f'{skill.get("name") or skill["id"]}.md'
        return Response(
            markdown,
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    # --- Ban Appeal Pages ---

    def _render_appeal_page_html(d, user: dict) -> HTMLResponse:
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
  <form method="POST" action="/appeal/submit" style="margin-top: 0.5rem;">
    <input type="hidden" name="collection_id" value="{coll_id}">
    <button type="submit">Submit Appeal</button>
  </form>
</div>\n"""

        body = f"""<h1>Ban Appeals</h1>
<p>Hi {_xml_escape(user['name'])}. You can appeal bans below. The collection owner will see your credit score and decide.</p>
{items_html}"""
        return HTMLResponse(_html_page("Ban Appeals", body))

    async def handle_appeal_page(request: Request):
        """GET /appeal/{key} — silent-migrate to cookie-authed /appeal."""
        api_key = request.path_params["key"]
        d = get_db()
        user = d.get_user_by_api_key(api_key)
        if not user:
            return HTMLResponse(_html_page("Not Found", "<h1>Invalid key</h1><p>This appeal URL is not valid.</p>"), status_code=404)
        from starlette.responses import RedirectResponse
        resp = RedirectResponse(url="/appeal", status_code=303)
        _set_session_cookie(resp, api_key, request)
        return resp

    async def handle_appeal_page_bare(request: Request):
        """GET /appeal — cookie-authed appeal page."""
        user = _resolve_user_from_cookie_or_header(request)
        if not user:
            from starlette.responses import RedirectResponse
            return RedirectResponse(url="/session/unlock?return_to=/appeal", status_code=303)
        return _render_appeal_page_html(get_db(), user)

    async def _do_appeal_submit(request: Request, user: dict, redirect_to: str) -> HTMLResponse:
        d = get_db()
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
<p style="margin-top: 1rem;"><a href="{_xml_escape(redirect_to)}" style="color: #93c5fd;">Back to appeals</a></p>"""
        return HTMLResponse(_html_page("Appeal Submitted", body))

    async def handle_appeal_submit(request: Request):
        """POST /appeal/{key}/submit — legacy path-based variant."""
        api_key = request.path_params["key"]
        d = get_db()
        user = d.get_user_by_api_key(api_key)
        if not user:
            return HTMLResponse(_html_page("Error", "<h1>Invalid key</h1>"), status_code=404)
        return await _do_appeal_submit(request, user, f"/appeal/{api_key}")

    async def handle_appeal_submit_bare(request: Request):
        """POST /appeal/submit — cookie-authed appeal submission."""
        user = _resolve_user_from_cookie_or_header(request)
        if not user:
            return HTMLResponse(_html_page("Error", "<h1>Unauthorized</h1>"), status_code=401)
        return await _do_appeal_submit(request, user, "/appeal")

    def _render_appeal_status_json(d, user: dict) -> JSONResponse:
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

    async def handle_appeal_status(request: Request):
        """GET /appeal/{key}/status — legacy JSON status endpoint (key in path)."""
        api_key = request.path_params["key"]
        d = get_db()
        user = d.get_user_by_api_key(api_key)
        if not user:
            return _problem_response(404, "Invalid key")
        return _render_appeal_status_json(d, user)

    async def handle_appeal_status_bare(request: Request):
        """GET /appeal/status — cookie-authed JSON status endpoint."""
        user = _resolve_user_from_cookie_or_header(request)
        if not user:
            return _problem_response(401, "Unauthorized")
        return _render_appeal_status_json(get_db(), user)

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

    def _slack_resolve_user(slack_user: str) -> Optional[dict]:
        rows = get_db().conn.execute("SELECT id, name, api_key FROM users").fetchall()
        user = None
        for row in rows:
            if row["name"].lower() == slack_user.lower():
                user = dict(row)
                break
        if not user and rows:
            user = dict(rows[0])
        return user

    def _slack_skill_help() -> str:
        return (
            "Usage:\n"
            "`/dugg skill` or `/dugg skill list [--limit N] [--collection NAME]`\n"
            "`/dugg skill get <id_or_name> [--collection NAME]`\n"
            "`/dugg skill search <query> [--collection NAME]`\n"
            "`/dugg skill add <FULL SKILL.md>`"
        )

    def _slack_skill_usage_response() -> JSONResponse:
        return JSONResponse({"response_type": "ephemeral", "text": _slack_skill_help()})

    def _slack_skill_markdown(skill: dict) -> str:
        return render_skill_markdown(skill.get("frontmatter") or {}, skill.get("body") or "")

    def _slack_skill_codeblock(skill: dict, max_chars: Optional[int] = None) -> str:
        markdown = _slack_skill_markdown(skill)
        footer = ""
        if max_chars and len(markdown) > max_chars:
            markdown = markdown[: max_chars - 1].rstrip() + "…"
            footer = f"\n…truncated, run `dugg skill get {skill['id']}` for full text"
        return f"```markdown\n{markdown}\n```{footer}"

    def _slack_skill_summary_lines(skill: dict) -> list[str]:
        title = skill.get("title") or skill.get("name") or skill.get("id", "")
        lines = [f"*{_xml_escape(title)}*"]
        if skill.get("name"):
            lines.append(f"`{_xml_escape(skill['name'])}`")
        if skill.get("description"):
            lines.append(_xml_escape(skill["description"]))
        if skill.get("author"):
            lines.append(f"by {_xml_escape(skill['author'])}")
        return lines

    def _slack_skill_actions(skill_id: str) -> dict:
        return {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": ":open_book: View", "emoji": True},
                    "action_id": "dugg_skill_view",
                    "value": skill_id,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": ":inbox_tray: Install", "emoji": True},
                    "action_id": "dugg_skill_install",
                    "value": skill_id,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": ":fork_and_knife: Fork", "emoji": True},
                    "action_id": "dugg_skill_fork",
                    "value": skill_id,
                },
            ],
        }

    def _slack_skill_blocks(skills: list[dict], heading: str) -> tuple[list[dict], str]:
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": heading}}]
        text_lines = [heading, ""]
        for skill in skills:
            summary_lines = _slack_skill_summary_lines(skill)
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(summary_lines)}})
            blocks.append(_slack_skill_actions(skill["id"]))
            blocks.append({"type": "divider"})
            text_lines.extend(summary_lines + [""])
        return blocks, "\n".join(text_lines).strip()

    def _slack_skill_limit_and_collection(rest: str, *, default_limit: int = 5) -> tuple[int, str]:
        import re

        limit = default_limit
        collection_name = ""
        if not rest:
            return limit, collection_name
        limit_match = re.search(r"--limit\s+(\d+)", rest)
        if limit_match:
            limit = min(int(limit_match.group(1)), 20)
        collection_match = re.search(r"--collection\s+(.+?)(?=\s+--\w+|$)", rest)
        if collection_match:
            collection_name = collection_match.group(1).strip().strip("\"'")
        return limit, collection_name

    def _slack_skill_find(d: DuggDB, user_id: str, id_or_name: str, collection_name: str = "") -> tuple[Optional[dict], Optional[str]]:
        from dugg.server import _find_skill

        return _find_skill(d, user_id, id_or_name, collection_name)

    def _slack_skill_list_rows(d: DuggDB, user_id: str, limit: int, collection_name: str = "") -> tuple[list[dict], Optional[str]]:
        from dugg.server import _resolve_collection_for_user

        accessible = d._accessible_collection_ids(user_id)
        if not accessible:
            return [], None
        if collection_name:
            coll_id = _resolve_collection_for_user(d, user_id, collection_name)
            if not coll_id:
                return [], f"Collection not found or not accessible: {collection_name}"
            accessible = [coll_id]
        placeholders = ",".join("?" for _ in accessible)
        rows = d.conn.execute(
            f"""SELECT r.id, r.title, r.description, r.author, r.collection_id, r.submitted_by,
                       r.created_at, s.name, s.supersedes_id, s.is_exportable
                FROM resources r
                JOIN skills s ON s.resource_id = r.id
                WHERE r.source_type = 'skill'
                  AND r.collection_id IN ({placeholders})
                ORDER BY r.created_at DESC
                LIMIT ?""",
            accessible + [limit],
        ).fetchall()
        return [dict(row) for row in rows], None

    def _slack_skill_search_rows(d: DuggDB, user_id: str, query: str, limit: int, collection_name: str = "") -> tuple[list[dict], Optional[str]]:
        from dugg.server import _resolve_collection_for_user

        coll_id = None
        if collection_name:
            coll_id = _resolve_collection_for_user(d, user_id, collection_name)
            if not coll_id:
                return [], f"Collection not found or not accessible: {collection_name}"

        combined: dict[str, dict] = {}
        for result in d.search(query, user_id, collection_id=coll_id, limit=max(limit * 5, 50)):
            if result.get("source_type") != "skill":
                continue
            skill = d.get_skill(result["id"])
            if skill:
                combined[skill["id"]] = skill

        accessible = [coll_id] if coll_id else d._accessible_collection_ids(user_id)
        if accessible:
            placeholders = ",".join("?" for _ in accessible)
            rows = d.conn.execute(
                f"""SELECT r.id
                    FROM resources r
                    JOIN skills s ON s.resource_id = r.id
                    WHERE r.source_type = 'skill'
                      AND r.collection_id IN ({placeholders})
                      AND LOWER(s.body) LIKE LOWER(?)
                    ORDER BY r.created_at DESC
                    LIMIT ?""",
                accessible + [f"%{query}%", max(limit * 5, 50)],
            ).fetchall()
            for row in rows:
                skill = d.get_skill(row["id"])
                if skill:
                    combined[skill["id"]] = skill

        skills = sorted(combined.values(), key=lambda skill: skill.get("created_at") or "", reverse=True)[:limit]
        return skills, None

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
        user = _slack_resolve_user(slack_user)

        if not user:
            return JSONResponse({"response_type": "ephemeral", "text": "No users on this Dugg server yet."})

        if text == "skill" or text.startswith("skill "):
            rest = text[5:].strip() if text.startswith("skill ") else ""
            if not rest or rest == "list" or rest.startswith("list "):
                limit, collection_name = _slack_skill_limit_and_collection(rest[4:].strip() if rest.startswith("list ") else rest)
                skills, error = _slack_skill_list_rows(d, user["id"], limit, collection_name)
                if error:
                    return JSONResponse({"response_type": "ephemeral", "text": error})
                if not skills:
                    return JSONResponse({"response_type": "ephemeral", "text": "No skills found. Add one with `/dugg skill add`."})
                blocks, fallback = _slack_skill_blocks(skills, f"*Recent skills ({len(skills)}):*")
                return JSONResponse({"response_type": "in_channel", "text": fallback, "blocks": blocks})

            if rest.startswith("get "):
                target = rest[4:].strip()
                collection_name = ""
                import re
                collection_match = re.search(r"\s+--collection\s+(.+)$", target)
                if collection_match:
                    collection_name = collection_match.group(1).strip().strip("\"'")
                    target = target[: collection_match.start()].strip()
                if not target:
                    return _slack_skill_usage_response()
                skill, error = _slack_skill_find(d, user["id"], target, collection_name)
                if error:
                    return JSONResponse({"response_type": "ephemeral", "text": error})
                return JSONResponse({"response_type": "ephemeral", "text": _slack_skill_codeblock(skill, max_chars=2800)})

            if rest.startswith("search "):
                query = rest[7:].strip()
                collection_name = ""
                import re
                collection_match = re.search(r"\s+--collection\s+(.+)$", query)
                if collection_match:
                    collection_name = collection_match.group(1).strip().strip("\"'")
                    query = query[: collection_match.start()].strip()
                if not query:
                    return _slack_skill_usage_response()
                skills, error = _slack_skill_search_rows(d, user["id"], query, 5, collection_name)
                if error:
                    return JSONResponse({"response_type": "ephemeral", "text": error})
                if not skills:
                    return JSONResponse({"response_type": "ephemeral", "text": f'No skills found for "{_xml_escape(query)}".'})
                blocks, fallback = _slack_skill_blocks(skills, f'*Skill results for "{_xml_escape(query)}":*')
                return JSONResponse({"response_type": "in_channel", "text": fallback, "blocks": blocks})

            if rest.startswith("add"):
                markdown = rest[3:].lstrip()
                if not markdown or not markdown.startswith("---\n"):
                    return JSONResponse({
                        "response_type": "ephemeral",
                        "text": "Paste a full SKILL.md with frontmatter. For longer skills, use the CLI: `dugg skill add path/to/SKILL.md`.",
                    })
                try:
                    frontmatter, body = parse_skill_markdown(markdown)
                    name = frontmatter["name"].strip()
                    validate_skill_name(name)
                except ValueError as exc:
                    return JSONResponse({
                        "response_type": "ephemeral",
                        "text": f"{exc}\n\nPaste a full SKILL.md with frontmatter. For longer skills, use the CLI: `dugg skill add path/to/SKILL.md`.",
                    })
                skill_id = d.add_skill(
                    name=name,
                    body=body,
                    frontmatter=frontmatter,
                    title=frontmatter.get("title") or name,
                    description=frontmatter.get("description") or "",
                    author=frontmatter.get("author") or user["name"],
                    collection_id=_ensure_default_collection(d, user["id"]),
                    submitted_by=user["id"],
                )
                d.wait_for_webhooks()
                skill = d.get_skill(skill_id)
                blocks, fallback = _slack_skill_blocks([skill], "*Added skill:*")
                return JSONResponse({"response_type": "in_channel", "text": fallback, "blocks": blocks})

            if rest == "help":
                return _slack_skill_usage_response()

            return _slack_skill_usage_response()

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
                        "elements": _slack_resource_action_buttons(resource_id),
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
                    "elements": _slack_resource_action_buttons(resource_id),
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
                    "elements": _slack_resource_action_buttons(resource_id),
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

        skill_action_ids = {"dugg_skill_view", "dugg_skill_install", "dugg_skill_fork"}
        if action_id in skill_action_ids:
            slack_user = data.get("user", {}).get("username", "")
            user = _slack_resolve_user(slack_user)
            if not user:
                return JSONResponse({"response_type": "ephemeral", "text": "No Dugg user found."})
            skill, error = _slack_skill_find(d, user["id"], resource_id)
            if error:
                return JSONResponse({"response_type": "ephemeral", "text": error})
            if action_id == "dugg_skill_view":
                return JSONResponse({"response_type": "ephemeral", "replace_original": False, "text": _slack_skill_codeblock(skill)})
            if action_id == "dugg_skill_install":
                return JSONResponse({
                    "response_type": "ephemeral",
                    "replace_original": False,
                    "text": (
                        f"{_slack_skill_codeblock(skill)}\n"
                        f"Use `dugg skill get {skill['id']} > skill.md` or the `dugg_skill_install` MCP tool to install it locally."
                    ),
                })
            return JSONResponse({
                "response_type": "ephemeral",
                "replace_original": False,
                "text": (
                    f"Fork this skill with `dugg skill fork --source {skill['id']}` "
                    f"or the `dugg_skill_fork` MCP tool."
                ),
            })

        if action_id == "dugg_react_tap":
            return JSONResponse({
                "response_type": "ephemeral",
                "replace_original": False,
                "blocks": [{
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "This button has been retired. New messages have a Mark as Read button — please refresh your feed.",
                    },
                }],
                "text": "This button has been retired. New messages have a Mark as Read button — please refresh your feed.",
            })

        # Map action_id to reaction type
        reaction_map = {
            "dugg_react_star": "star",
            "dugg_react_thumbsup": "thumbsup",
        }
        reaction_type = reaction_map.get(action_id)
        if action_id != "dugg_mark_read" and (not reaction_type or not resource_id):
            return JSONResponse({"text": ""})

        # Resolve Slack user to Dugg user
        slack_user = data.get("user", {}).get("username", "")
        user = _slack_resolve_user(slack_user)
        if not user:
            return JSONResponse({"text": "No Dugg user found."})

        # Verify resource exists
        resource = d.get_resource(resource_id)
        if not resource:
            return JSONResponse({"text": "Resource not found."})

        title = resource.get("title") or resource.get("url", "")
        if action_id == "dugg_mark_read":
            d.mark_read(resource_id=resource_id, user_id=user["id"], source="slack_button")
            d.wait_for_webhooks()
            return JSONResponse({
                "response_type": "ephemeral",
                "replace_original": False,
                "text": f":book: You marked *{title}* as read",
            })

        emoji = {"star": ":star:", "thumbsup": ":thumbsup:"}.get(reaction_type, "")
        d.react_to_resource(resource_id, user["id"], reaction_type)
        d.mark_read(resource_id=resource_id, user_id=user["id"], source="slack_react_implicit")
        d.wait_for_webhooks()

        return JSONResponse({
            "response_type": "ephemeral",
            "replace_original": False,
            "text": f"{emoji} You reacted {reaction_type} to *{title}*",
        })

    # --- Browser admin panel ---

    def _admin_resolve_user(request: Request):
        """Resolve user from cookie/header first, then path key (legacy)."""
        d = get_db()
        user = _resolve_user_from_cookie_or_header(request)
        if user:
            return d, user
        key = request.path_params.get("key", "")
        if key:
            return d, d.get_user_by_api_key(key)
        return d, None

    # --- Paste Pages ---

    def _render_paste_page_html(d, user: dict) -> HTMLResponse:
        """Render the paste form for a resolved user (cookie-authed submit)."""
        instances = d.list_instances(user["id"])
        page_title = instances[0]["name"] if instances else "Dugg"
        body = f"""<h1>Paste Content</h1>
<p class="topic">Add raw content to {_xml_escape(page_title)} — no URL needed.</p>
<form method="POST" action="/paste/submit" enctype="multipart/form-data">
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

    async def handle_paste_page(request: Request):
        """GET /paste/{key} — silent-migrate to cookie-authed /paste."""
        api_key = request.path_params["key"]
        d = get_db()
        user = d.get_user_by_api_key(api_key)
        if not user:
            return HTMLResponse(_html_page("Not Found", "<h1>Invalid key</h1><p>This paste URL is not valid.</p>"), status_code=404)
        from starlette.responses import RedirectResponse
        resp = RedirectResponse(url="/paste", status_code=303)
        _set_session_cookie(resp, api_key, request)
        return resp

    async def handle_paste_page_bare(request: Request):
        """GET /paste — cookie-authed paste form."""
        user = _resolve_user_from_cookie_or_header(request)
        if not user:
            from starlette.responses import RedirectResponse
            return RedirectResponse(url="/session/unlock?return_to=/paste", status_code=303)
        return _render_paste_page_html(get_db(), user)

    async def _handle_paste_submit_for_user(request: Request, user: dict) -> HTMLResponse:
        d = get_db()
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
<p style="margin-top:1rem;"><a href="/paste" style="color:#93c5fd;">Paste another</a></p>"""
        return HTMLResponse(_html_page("Saved", success_body))

    async def handle_paste_submit(request: Request):
        """POST /paste/{key}/submit — legacy. Resolves via path key; prefer /paste/submit."""
        api_key = request.path_params["key"]
        d = get_db()
        user = d.get_user_by_api_key(api_key)
        if not user:
            return HTMLResponse(_html_page("Error", "<h1>Invalid key</h1>"), status_code=404)
        return await _handle_paste_submit_for_user(request, user)

    async def handle_paste_submit_bare(request: Request):
        """POST /paste/submit — cookie-authed paste submission."""
        user = _resolve_user_from_cookie_or_header(request)
        if not user:
            return HTMLResponse(_html_page("Error", "<h1>Unauthorized</h1>"), status_code=401)
        return await _handle_paste_submit_for_user(request, user)

    def _render_admin_page_html(d, user: dict) -> HTMLResponse:
        """Render admin dashboard for a resolved user. All form actions use bare /admin/* paths."""
        collections = d.list_collections(user["id"])
        server_url = d.get_config("server_url", "")

        sections = []
        for c in collections:
            member = d.get_member_status(c["id"], user["id"])
            is_owner = member and member["role"] == "owner"

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
                        actions = f' <form method="POST" action="/admin/ban" style="display:inline;"><input type="hidden" name="collection_id" value="{c["id"]}"><input type="hidden" name="user_id" value="{m["user_id"]}"><button type="submit" style="background:#dc2626;padding:0.2rem 0.5rem;font-size:0.75rem;border-radius:4px;border:none;color:#fff;cursor:pointer;">Ban</button></form>'
                    elif m["status"] in ("banned", "appealing"):
                        actions = f' <form method="POST" action="/admin/unban" style="display:inline;"><input type="hidden" name="collection_id" value="{c["id"]}"><input type="hidden" name="user_id" value="{m["user_id"]}"><button type="submit" style="background:#16a34a;padding:0.2rem 0.5rem;font-size:0.75rem;border-radius:4px;border:none;color:#fff;cursor:pointer;">Unban</button></form>'
                member_html += f'<div style="padding:0.4rem 0;border-bottom:1px solid #222;display:flex;justify-content:space-between;align-items:center;"><span>{_xml_escape(m["name"])} <span style="color:#666;">({m["role"]})</span>{status_badge}</span>{actions}</div>'

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
                    remove_btn = f' <form method="POST" action="/admin/remove" style="display:inline;"><input type="hidden" name="resource_id" value="{r["id"]}"><input type="hidden" name="collection_id" value="{c["id"]}"><button type="submit" style="background:#dc2626;padding:0.15rem 0.4rem;font-size:0.7rem;border-radius:4px;border:none;color:#fff;cursor:pointer;">Remove</button></form>'
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

    async def handle_admin_page(request: Request):
        """GET /admin/{key} — silent-migrate to cookie-authed /admin."""
        api_key = request.path_params["key"]
        d = get_db()
        user = d.get_user_by_api_key(api_key)
        if not user:
            return HTMLResponse(_html_page("Unauthorized", "<h1>Invalid API key</h1><p>Check your admin URL.</p>"), status_code=401)
        from starlette.responses import RedirectResponse
        resp = RedirectResponse(url="/admin", status_code=303)
        _set_session_cookie(resp, api_key, request)
        return resp

    async def handle_admin_page_bare(request: Request):
        """GET /admin — cookie-authed admin dashboard."""
        user = _resolve_user_from_cookie_or_header(request)
        if not user:
            from starlette.responses import RedirectResponse
            return RedirectResponse(url="/session/unlock?return_to=/admin", status_code=303)
        return _render_admin_page_html(get_db(), user)

    async def _do_admin_ban(request: Request, user: dict, redirect_to: str) -> HTMLResponse:
        d = get_db()
        form = await request.form()
        collection_id = form.get("collection_id", "")
        target_user_id = form.get("user_id", "")
        member = d.get_member_status(collection_id, user["id"])
        if not member or member["role"] != "owner":
            return HTMLResponse(_html_page("Forbidden", "<h1>Not the collection owner</h1>"), status_code=403)
        d.conn.execute("UPDATE collection_members SET status = 'banned' WHERE collection_id = ? AND user_id = ?", (collection_id, target_user_id))
        d.conn.commit()
        from starlette.responses import RedirectResponse
        return RedirectResponse(redirect_to, status_code=303)

    async def _do_admin_unban(request: Request, user: dict, redirect_to: str) -> HTMLResponse:
        d = get_db()
        form = await request.form()
        collection_id = form.get("collection_id", "")
        target_user_id = form.get("user_id", "")
        member = d.get_member_status(collection_id, user["id"])
        if not member or member["role"] != "owner":
            return HTMLResponse(_html_page("Forbidden", "<h1>Not the collection owner</h1>"), status_code=403)
        d.conn.execute("UPDATE collection_members SET status = 'active' WHERE collection_id = ? AND user_id = ?", (collection_id, target_user_id))
        d.conn.commit()
        from starlette.responses import RedirectResponse
        return RedirectResponse(redirect_to, status_code=303)

    async def _do_admin_remove(request: Request, user: dict, redirect_to: str) -> HTMLResponse:
        d = get_db()
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
        from starlette.responses import RedirectResponse
        return RedirectResponse(redirect_to, status_code=303)

    async def handle_admin_ban(request: Request):
        """POST /admin/{key}/ban — legacy path-based variant."""
        d, user = _admin_resolve_user(request)
        if not user:
            return HTMLResponse(_html_page("Unauthorized", "<h1>Invalid API key</h1>"), status_code=401)
        key = request.path_params.get("key", "")
        redirect_to = f"/admin/{key}" if key else "/admin"
        return await _do_admin_ban(request, user, redirect_to)

    async def handle_admin_unban(request: Request):
        """POST /admin/{key}/unban — legacy path-based variant."""
        d, user = _admin_resolve_user(request)
        if not user:
            return HTMLResponse(_html_page("Unauthorized", "<h1>Invalid API key</h1>"), status_code=401)
        key = request.path_params.get("key", "")
        redirect_to = f"/admin/{key}" if key else "/admin"
        return await _do_admin_unban(request, user, redirect_to)

    async def handle_admin_remove(request: Request):
        """POST /admin/{key}/remove — legacy path-based variant."""
        d, user = _admin_resolve_user(request)
        if not user:
            return HTMLResponse(_html_page("Unauthorized", "<h1>Invalid API key</h1>"), status_code=401)
        key = request.path_params.get("key", "")
        redirect_to = f"/admin/{key}" if key else "/admin"
        return await _do_admin_remove(request, user, redirect_to)

    async def handle_admin_ban_bare(request: Request):
        """POST /admin/ban — cookie-authed ban action."""
        user = _resolve_user_from_cookie_or_header(request)
        if not user:
            return HTMLResponse(_html_page("Unauthorized", "<h1>Unauthorized</h1>"), status_code=401)
        return await _do_admin_ban(request, user, "/admin")

    async def handle_admin_unban_bare(request: Request):
        """POST /admin/unban — cookie-authed unban action."""
        user = _resolve_user_from_cookie_or_header(request)
        if not user:
            return HTMLResponse(_html_page("Unauthorized", "<h1>Unauthorized</h1>"), status_code=401)
        return await _do_admin_unban(request, user, "/admin")

    async def handle_admin_remove_bare(request: Request):
        """POST /admin/remove — cookie-authed remove action."""
        user = _resolve_user_from_cookie_or_header(request)
        if not user:
            return HTMLResponse(_html_page("Unauthorized", "<h1>Unauthorized</h1>"), status_code=401)
        return await _do_admin_remove(request, user, "/admin")

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
        Route("/session/unlock", endpoint=handle_session_unlock_get),
        Route("/session/unlock", endpoint=handle_session_unlock_post, methods=["POST"]),
        Route("/session/clear", endpoint=handle_session_clear),
        Route("/feed/urls/{key}", endpoint=handle_feed_urls),
        Route("/feed", endpoint=handle_feed_bare),
        Route("/feed/{key}", endpoint=handle_feed),
        Route("/api/feed", endpoint=handle_api_feed),
        Route("/api/feed/urls", endpoint=handle_api_feed_urls),
        Route("/api/read", endpoint=handle_api_read, methods=["GET"]),
        Route("/api/read/{resource_id}", endpoint=handle_api_read, methods=["POST", "DELETE"]),
        Route("/api/react", endpoint=handle_api_react, methods=["POST"]),
        Route("/api/react/{resource_id}", endpoint=handle_api_react, methods=["DELETE"]),
        Route("/api/search", endpoint=handle_api_search),
        Route("/api/resource/{id}", endpoint=handle_api_resource),
        Route("/api/note", endpoint=handle_api_note, methods=["POST"]),
        Route("/api/note/edit", endpoint=handle_api_note_edit, methods=["POST"]),
        Route("/api/note/delete", endpoint=handle_api_note_delete, methods=["POST"]),
        Route("/api/edit", endpoint=handle_api_edit, methods=["POST"]),
        Route("/api/resource/{id}/edits", endpoint=handle_api_resource_edits),
        Route("/paste", endpoint=handle_paste_page_bare),
        Route("/paste/submit", endpoint=handle_paste_submit_bare, methods=["POST"]),
        Route("/paste/{key}", endpoint=handle_paste_page),
        Route("/paste/{key}/submit", endpoint=handle_paste_submit, methods=["POST"]),
        Route("/appeal", endpoint=handle_appeal_page_bare),
        Route("/appeal/submit", endpoint=handle_appeal_submit_bare, methods=["POST"]),
        Route("/appeal/status", endpoint=handle_appeal_status_bare),
        Route("/appeal/{key}", endpoint=handle_appeal_page),
        Route("/appeal/{key}/submit", endpoint=handle_appeal_submit, methods=["POST"]),
        Route("/appeal/{key}/status", endpoint=handle_appeal_status),
        Route("/skills", endpoint=handle_skills_page),
        Route("/r/{resource_id}", endpoint=handle_resource_page),
        Route("/r/{resource_id}/unlock", endpoint=handle_resource_unlock, methods=["POST"]),
        Route("/s/{skill_id}.md", endpoint=handle_skill_download),
        Route("/s/{skill_id}", endpoint=handle_skill_page),
        Route("/s/{skill_id}/unlock", endpoint=handle_skill_unlock, methods=["POST"]),
        Route("/publish-note", endpoint=handle_publish_note, methods=["POST"]),
        Route("/publish-note/{key}", endpoint=handle_publish_note, methods=["POST"]),
        Route("/rotate-key", endpoint=handle_rotate_key, methods=["POST"]),
        Route("/events/stream", endpoint=handle_events_stream),
        Route("/tools/{tool_name}", endpoint=handle_tools, methods=["POST"]),
        Route("/slack/command", endpoint=handle_slack_command, methods=["POST"]),
        Route("/slack/actions", endpoint=handle_slack_actions, methods=["POST"]),
        Route("/admin", endpoint=handle_admin_page_bare),
        Route("/admin/ban", endpoint=handle_admin_ban_bare, methods=["POST"]),
        Route("/admin/unban", endpoint=handle_admin_unban_bare, methods=["POST"]),
        Route("/admin/remove", endpoint=handle_admin_remove_bare, methods=["POST"]),
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
