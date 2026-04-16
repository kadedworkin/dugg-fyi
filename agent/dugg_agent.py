"""Dugg Agent — reference implementation of an enrichment + federation agent.

Runs alongside a user's Dugg server(s). Two responsibilities:

1. **Direct ingest (Option A)** — HTTP listener on a configurable port.
   The Chrome extension (or anything else) POSTs URLs here. The agent
   enriches them and publishes to the appropriate Dugg server(s).

2. **Event stream (Option B)** — SSE subscriber to each Dugg server.
   Watches for `resource_added` events from the user's own submissions
   that arrived through other paths (email, paste, Slack). Enriches
   them via `dugg_edit` and federates via `dugg_publish`.

De-dup: resources marked with metadata flag `agent_enriched=true` are
skipped by the event listener. Option A sets this flag on every submission.

LLM enrichment is pluggable. By default it uses the Anthropic API if
`ANTHROPIC_API_KEY` is set, otherwise it falls back to keyword heuristics.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("dugg-agent")


# --- Config ---

@dataclass
class ServerConfig:
    """One Dugg server the agent works with."""
    name: str
    url: str
    api_key: str
    user_id: Optional[str] = None  # if set, used directly for Option B event filtering


def load_config() -> tuple[list[ServerConfig], int, Optional[str]]:
    """Load servers, listen port, and optional Anthropic key.

    Servers come from DUGG_AGENT_SERVERS env var as JSON:
        [{"name": "chino-bandido", "url": "https://...", "api_key": "dugg_..."}]
    Or from ~/.dugg-agent.json.
    """
    raw = os.environ.get("DUGG_AGENT_SERVERS")
    if not raw:
        path = os.path.expanduser("~/.dugg-agent.json")
        if os.path.exists(path):
            with open(path) as f:
                raw = f.read()
        else:
            raise RuntimeError(
                "No config found. Set DUGG_AGENT_SERVERS env or create ~/.dugg-agent.json"
            )
    data = json.loads(raw)
    servers = [ServerConfig(**s) for s in data.get("servers", data)]
    port = int(os.environ.get("DUGG_AGENT_PORT", data.get("port", 8412) if isinstance(data, dict) else 8412))
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY") or (data.get("anthropic_api_key") if isinstance(data, dict) else None)
    return servers, port, anthropic_key


# --- Dugg HTTP client ---

class DuggClient:
    def __init__(self, server: ServerConfig):
        self.server = server
        self.client = httpx.AsyncClient(timeout=30.0)

    async def call_tool(self, tool: str, args: dict) -> dict:
        r = await self.client.post(
            f"{self.server.url}/tools/{tool}",
            headers={"X-Dugg-Key": self.server.api_key, "Content-Type": "application/json"},
            json=args,
        )
        r.raise_for_status()
        return r.json()

    @staticmethod
    def _parse_kv(text: str) -> dict:
        """Parse `Key: value` lines from a tool's text result into a dict."""
        out = {}
        for line in (text or "").splitlines():
            if ":" in line:
                k, _, v = line.partition(":")
                k = k.strip().lower().replace(" ", "_")
                v = v.strip()
                if k and v:
                    out[k] = v
        return out

    async def add_resource(self, url: str, note: str = "", tags: Optional[list[str]] = None,
                           title: str = "", description: str = "", transcript: str = "",
                           agent_enriched: bool = False) -> dict:
        args = {"url": url}
        if note:
            args["note"] = note
        if tags:
            args["tags"] = tags
        if title:
            args["title"] = title
        if description:
            args["description"] = description
        if transcript:
            args["transcript"] = transcript
        if agent_enriched:
            args["raw_metadata"] = {"agent_enriched": True}
        return await self.call_tool("dugg_add", args)

    async def get_resource(self, resource_id: str) -> dict:
        return await self.call_tool("dugg_get", {"resource_id": resource_id})

    async def edit_resource(self, resource_id: str, **fields) -> dict:
        args = {"resource_id": resource_id, **fields}
        return await self.call_tool("dugg_edit", args)

    async def routing_manifest(self) -> dict:
        return await self.call_tool("dugg_routing_manifest", {})

    async def publish(self, resource_id: str, targets: list[str]) -> dict:
        return await self.call_tool("dugg_publish", {"resource_id": resource_id, "targets": targets})

    async def get_user(self) -> dict:
        """Returns user info via dugg_welcome (includes user_id)."""
        return await self.call_tool("dugg_welcome", {})

    async def stream_events(self):
        """SSE iterator yielding event dicts."""
        async with self.client.stream(
            "GET",
            f"{self.server.url}/events/stream",
            headers={"X-Dugg-Key": self.server.api_key, "Accept": "text/event-stream"},
            timeout=None,
        ) as r:
            r.raise_for_status()
            async for line in r.aiter_lines():
                if line.startswith("data: "):
                    payload = line[6:].strip()
                    if payload:
                        try:
                            yield json.loads(payload)
                        except json.JSONDecodeError:
                            log.warning("skipping malformed SSE payload: %s", payload[:80])


# --- Enrichment ---

async def fetch_text(url: str) -> str:
    """Best-effort content fetch for enrichment context."""
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as c:
            r = await c.get(url, headers={"User-Agent": "DuggAgent/1.0 (+https://dugg.fyi)"})
            r.raise_for_status()
            text = re.sub(r"<[^>]+>", " ", r.text)
            text = re.sub(r"\s+", " ", text).strip()
            return text[:8000]
    except Exception as e:
        log.warning("fetch_text failed for %s: %s", url, e)
        return ""


async def llm_enrich(url: str, title: str, content: str, anthropic_key: Optional[str]) -> dict:
    """Generate summary + tags via LLM. Falls back to heuristics if no key."""
    if not anthropic_key:
        return heuristic_enrich(url, title, content)

    prompt = f"""Analyze this web resource and produce JSON with fields: summary (2-3 sentences), tags (5-8 lowercase keywords).

URL: {url}
Title: {title}
Content (first 8000 chars): {content[:8000]}

Respond ONLY with JSON: {{"summary": "...", "tags": ["..."]}}"""

    try:
        async with httpx.AsyncClient(timeout=60.0) as c:
            r = await c.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": anthropic_key,
                    "anthropic-version": "2023-06-01",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": 800,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            r.raise_for_status()
            data = r.json()
            text = data["content"][0]["text"]
            m = re.search(r"\{.*\}", text, re.DOTALL)
            if m:
                return json.loads(m.group(0))
    except Exception as e:
        log.warning("llm_enrich failed, falling back to heuristics: %s", e)

    return heuristic_enrich(url, title, content)


def heuristic_enrich(url: str, title: str, content: str) -> dict:
    """No-LLM fallback: derive tags from URL host and title words."""
    host = urlparse(url).netloc.replace("www.", "")
    domain_tag = host.split(".")[0]
    title_words = re.findall(r"[a-zA-Z]{4,}", title.lower())
    skip = {"the", "this", "that", "with", "from", "your", "have", "what", "when", "they", "their"}
    title_tags = [w for w in title_words if w not in skip][:5]
    summary = content[:280] + ("..." if len(content) > 280 else "") if content else (title or url)
    return {"summary": summary, "tags": list({domain_tag, *title_tags})}


def score_routing(content_text: str, manifest: list[dict]) -> list[str]:
    """Pick instance names whose topic keywords overlap with the content."""
    text_lower = (content_text or "").lower()
    matches = []
    for inst in manifest:
        topic = (inst.get("topic") or "").lower()
        if not topic:
            continue
        # crude keyword overlap — split topic on commas/spaces, count hits
        topic_words = [w.strip() for w in re.split(r"[,;\s]+", topic) if len(w.strip()) > 3]
        hits = sum(1 for w in topic_words if w in text_lower)
        if hits >= 2:
            matches.append(inst.get("name") or inst.get("instance_id"))
    return matches


# --- Main agent ---

class DuggAgent:
    def __init__(self, servers: list[ServerConfig], anthropic_key: Optional[str]):
        self.clients = {s.name: DuggClient(s) for s in servers}
        self.anthropic_key = anthropic_key
        self.user_ids: dict[str, str] = {}  # server_name -> user_id

    async def init_user_ids(self):
        for name, client in self.clients.items():
            if client.server.user_id:
                self.user_ids[name] = client.server.user_id
                log.info("server %s: configured as user %s", name, client.server.user_id)
                continue
            try:
                info = await client.get_user()
                uid = info.get("user", {}).get("id") or info.get("user_id")
                if uid:
                    self.user_ids[name] = uid
                    log.info("server %s: identified as user %s", name, uid)
                else:
                    log.warning("server %s: dugg_welcome returned no user_id — Option B filtering disabled. Add 'user_id' to config.", name)
            except Exception as e:
                log.warning("could not identify user on %s: %s", name, e)

    async def enrich_and_publish(self, server_name: str, url: str, note: str = "",
                                  pre_title: str = "") -> dict:
        """Option A flow: fetch, enrich, add to local server, federate."""
        client = self.clients[server_name]
        log.info("[%s] direct ingest: %s", server_name, url)

        content = await fetch_text(url)
        title_guess = pre_title
        if not title_guess and content:
            m = re.search(r"<title>(.*?)</title>", content, re.IGNORECASE)
            if m:
                title_guess = m.group(1).strip()

        enriched = await llm_enrich(url, title_guess, content, self.anthropic_key)

        result = await client.add_resource(
            url=url,
            note=note,
            tags=enriched.get("tags", []),
            description=enriched.get("summary", ""),
            agent_enriched=True,
        )
        parsed = DuggClient._parse_kv(result.get("result", ""))
        resource_id = parsed.get("id") or result.get("id") or (result.get("resource") or {}).get("id")
        log.info("[%s] added resource %s with tags %s", server_name, resource_id, enriched.get("tags"))

        await self._federate(server_name, resource_id, content + " " + (enriched.get("summary") or ""))
        return {"ok": True, "resource_id": resource_id, "tags": enriched.get("tags", [])}

    async def _federate(self, source_server: str, resource_id: str, scoring_text: str):
        client = self.clients[source_server]
        try:
            manifest = await client.routing_manifest()
            instances = manifest.get("instances", manifest if isinstance(manifest, list) else [])
            targets = score_routing(scoring_text, instances)
            if targets:
                log.info("[%s] auto-routing %s to %s", source_server, resource_id, targets)
                await client.publish(resource_id, targets)
        except Exception as e:
            log.warning("[%s] federation skipped: %s", source_server, e)

    async def listen_to_server(self, server_name: str):
        """Option B flow: SSE event stream. Enriches user's own un-enriched resources."""
        client = self.clients[server_name]
        backoff = 1
        while True:
            try:
                log.info("[%s] subscribing to event stream", server_name)
                async for event in client.stream_events():
                    backoff = 1
                    await self._handle_event(server_name, event)
            except Exception as e:
                log.warning("[%s] event stream dropped: %s — retry in %ds", server_name, e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def _handle_event(self, server_name: str, event: dict):
        if event.get("event_type") != "resource_added":
            return
        my_uid = self.user_ids.get(server_name)
        if not my_uid:
            return
        actor = event.get("user_id") or event.get("submitted_by") or event.get("actor_id")
        if actor != my_uid:
            return

        resource_id = event.get("resource_id") or (event.get("data") or {}).get("resource_id")
        if not resource_id:
            return

        client = self.clients[server_name]
        try:
            res = await client.get_resource(resource_id)
            parsed = DuggClient._parse_kv(res.get("result", ""))
        except Exception as e:
            log.warning("[%s] get_resource failed: %s", server_name, e)
            return

        # Server's text response doesn't expose raw_metadata, so we can't read
        # the agent_enriched flag back. We rely on the event stream filter:
        # Option A injects agent_enriched=True at write time, which means an
        # un-enriched event must have come from another path (paste, email,
        # CLI). Re-enriching is safe; dugg_edit replaces description/tags.
        url = parsed.get("url", "")
        title = parsed.get("title", "") or parsed.get("resource", "")
        content = await fetch_text(url) if url else ""

        log.info("[%s] enriching %s via event stream", server_name, resource_id)
        enriched = await llm_enrich(url, title, content, self.anthropic_key)

        await client.edit_resource(
            resource_id,
            description=enriched.get("summary", ""),
            tags=enriched.get("tags", []),
            raw_metadata={"agent_enriched": True},
        )
        await self._federate(server_name, resource_id, content + " " + (enriched.get("summary") or ""))


# --- HTTP API for Option A ---

class IngestRequest(BaseModel):
    url: str
    note: str = ""
    title: str = ""
    server: Optional[str] = None  # which configured server to add to


def make_app(agent: DuggAgent) -> FastAPI:
    app = FastAPI(title="Dugg Agent")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/")
    async def root():
        return {
            "service": "dugg-agent",
            "servers": list(agent.clients.keys()),
            "anthropic": bool(agent.anthropic_key),
        }

    @app.get("/health")
    async def health():
        return {"ok": True}

    @app.post("/dugg/ingest")
    async def ingest(req: IngestRequest):
        server_name = req.server or next(iter(agent.clients))
        if server_name not in agent.clients:
            raise HTTPException(404, f"unknown server {server_name}")
        try:
            return await agent.enrich_and_publish(server_name, req.url, req.note, req.title)
        except httpx.HTTPStatusError as e:
            raise HTTPException(e.response.status_code, str(e))

    @app.post("/tools/dugg_add")
    async def compat_add(req: IngestRequest):
        """Drop-in compat for the Chrome extension's existing dugg_add target.
        Lets users point the extension at the agent without code changes —
        the agent enriches before forwarding to the configured Dugg server."""
        return await ingest(req)

    return app


async def main():
    servers, port, anthropic_key = load_config()
    agent = DuggAgent(servers, anthropic_key)
    await agent.init_user_ids()

    listeners = [asyncio.create_task(agent.listen_to_server(s.name)) for s in servers]

    import uvicorn
    config = uvicorn.Config(make_app(agent), host="0.0.0.0", port=port, log_level="info")
    server = uvicorn.Server(config)
    log.info("Dugg agent listening on 0.0.0.0:%d for %d server(s); LLM=%s",
             port, len(servers), "anthropic" if anthropic_key else "heuristic")
    await server.serve()
    for t in listeners:
        t.cancel()


if __name__ == "__main__":
    asyncio.run(main())
