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
from starlette.responses import JSONResponse
from starlette.routing import Route

from mcp.server.sse import SseServerTransport

from dugg.db import DuggDB
from dugg.sync import start_sync_daemon

logger = logging.getLogger("dugg.http")


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
