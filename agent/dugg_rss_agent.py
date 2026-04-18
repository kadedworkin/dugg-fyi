"""Dugg RSS Agent — single-player / agent-side RSS watcher.

Runs on the user's machine (not the Dugg server) and pushes new feed entries
into one or more Dugg instances via their HTTP `/tools/dugg_add` endpoint.

Two modes:
  python dugg_rss_agent.py --once    # poll every feed, exit
  python dugg_rss_agent.py --watch   # run forever, polling on each feed's interval

Config (YAML) at `--config` or `~/.dugg/rss.yaml`:

    servers:
      - name: chino-bandido
        url: https://chino-bandido.kadedworkin.com
        api_key: dugg_xxx
      - name: local
        url: http://localhost:8411
        api_key: dugg_yyy

    default_target:
      server: chino-bandido
      collection: Default

    feeds:
      - url: https://daringfireball.net/feeds/main
        tag: daringfireball
        interval: 1h
        target: { server: chino-bandido, collection: Reading }

      - url: https://atp.fm/rss?token=PRIVATE_TOKEN
        tag: atp
        interval: 6h
        target: { server: chino-bandido, collection: Podcasts }

      # Routing rules — entry goes to first target whose match_tags overlap
      - url: https://example.com/mixed.xml
        tag: mixed
        rules:
          - match_tags: [python, golang]
            target: { server: chino-bandido, collection: Code }
          - match_tags: [photography]
            target: { server: local, collection: Images }

State cache (seen entry IDs, ETag, Last-Modified) lives at `~/.dugg/rss-state.json`.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import httpx
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from dugg.rss import FeedEntry, fetch_and_parse, is_private_link  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("dugg-rss-agent")

DEFAULT_CONFIG_PATH = Path(os.path.expanduser("~/.dugg/rss.yaml"))
DEFAULT_STATE_PATH = Path(os.path.expanduser("~/.dugg/rss-state.json"))
MAX_SEEN_IDS = 500


@dataclass
class ServerSpec:
    name: str
    url: str
    api_key: str


@dataclass
class RouteTarget:
    server: str
    collection: str = ""  # empty → remote server's Default


@dataclass
class RoutingRule:
    match_tags: list[str]
    target: RouteTarget


@dataclass
class FeedSpec:
    url: str
    tag: str = "rss"
    interval_seconds: int = 3600
    target: Optional[RouteTarget] = None
    rules: list[RoutingRule] = field(default_factory=list)


@dataclass
class Config:
    servers: dict[str, ServerSpec]
    feeds: list[FeedSpec]
    default_target: Optional[RouteTarget]


def _parse_interval(raw) -> int:
    if raw is None:
        return 3600
    if isinstance(raw, int):
        return max(60, raw)
    s = str(raw).strip().lower()
    if s.isdigit():
        return max(60, int(s))
    try:
        num = int(s[:-1])
    except ValueError:
        return 3600
    suffix = s[-1]
    mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}.get(suffix, 3600)
    return max(60, num * mult)


def _parse_target(raw) -> Optional[RouteTarget]:
    if not raw:
        return None
    if isinstance(raw, str):
        return RouteTarget(server=raw)
    return RouteTarget(
        server=raw["server"],
        collection=raw.get("collection", ""),
    )


def load_config(path: Path) -> Config:
    with open(path) as f:
        data = yaml.safe_load(f) or {}

    servers: dict[str, ServerSpec] = {}
    for s in data.get("servers") or []:
        spec = ServerSpec(name=s["name"], url=s["url"].rstrip("/"), api_key=s["api_key"])
        servers[spec.name] = spec
    if not servers:
        raise ValueError("At least one server must be configured.")

    feeds: list[FeedSpec] = []
    for f in data.get("feeds") or []:
        rules = [
            RoutingRule(
                match_tags=[t.lower() for t in r.get("match_tags") or []],
                target=_parse_target(r.get("target")),
            )
            for r in f.get("rules") or []
        ]
        feeds.append(FeedSpec(
            url=f["url"],
            tag=f.get("tag", "rss"),
            interval_seconds=_parse_interval(f.get("interval")),
            target=_parse_target(f.get("target")),
            rules=[r for r in rules if r.target],
        ))

    default_target = _parse_target(data.get("default_target"))
    return Config(servers=servers, feeds=feeds, default_target=default_target)


# --- state cache ---

def load_state(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        log.warning("Could not read state at %s, starting fresh", path)
        return {}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(path)


# --- ingestion ---

async def push_entry(
    client: httpx.AsyncClient,
    server: ServerSpec,
    entry: FeedEntry,
    *,
    collection: str,
    tag_label: str,
    feed_title: str,
) -> bool:
    """Send one entry to a Dugg server via HTTP /tools/dugg_add."""
    metadata = {
        "source": "rss",
        "rss_entry_id": entry.entry_id,
        "source_label": feed_title or "RSS",
    }
    if entry.published_at:
        metadata["published_at"] = entry.published_at
    if entry.is_private or is_private_link(entry.url):
        metadata["is_private_link"] = True

    payload = {
        "url": entry.url,
        "title": entry.title,
        "description": entry.description,
        "tags": [tag_label] if tag_label else [],
        "raw_metadata": metadata,
        "agent_enriched": False,
    }
    if collection:
        payload["collection"] = collection

    try:
        r = await client.post(
            f"{server.url}/tools/dugg_add",
            headers={"X-Dugg-Key": server.api_key, "Content-Type": "application/json"},
            json=payload,
            timeout=30.0,
        )
        r.raise_for_status()
    except httpx.HTTPError as e:
        log.error("push_entry to %s failed: %s", server.name, e)
        return False
    return True


def _route_entry(feed: FeedSpec, entry: FeedEntry, default: Optional[RouteTarget]) -> Optional[RouteTarget]:
    if feed.rules:
        entry_tags: set[str] = set()
        if feed.tag:
            entry_tags.add(feed.tag.lower())
        for rule in feed.rules:
            if any(t in entry_tags for t in rule.match_tags):
                return rule.target
    if feed.target:
        return feed.target
    return default


async def poll_feed(
    client: httpx.AsyncClient,
    feed: FeedSpec,
    cfg: Config,
    state: dict,
) -> dict:
    """Poll one feed, push new entries, update state in place. Returns result dict."""
    key = feed.url
    cached = state.get(key) or {}
    entries, _tombstones, meta = await fetch_and_parse(
        feed.url,
        etag=cached.get("etag", ""),
        last_modified=cached.get("last_modified", ""),
    )

    seen = set(cached.get("seen_entry_ids") or [])
    new_count = 0
    skipped = 0
    feed_title = meta.get("feed_title") or cached.get("feed_title") or ""

    for entry in entries:
        if entry.entry_id in seen:
            skipped += 1
            continue
        target = _route_entry(feed, entry, cfg.default_target)
        if not target:
            log.warning("No target for entry %s — skipping", entry.url)
            continue
        server = cfg.servers.get(target.server)
        if not server:
            log.warning("Unknown server %r in routing — skipping", target.server)
            continue
        ok = await push_entry(
            client,
            server,
            entry,
            collection=target.collection,
            tag_label=feed.tag,
            feed_title=feed_title,
        )
        if ok:
            seen.add(entry.entry_id)
            new_count += 1

    seen_list = list(seen)
    if len(seen_list) > MAX_SEEN_IDS:
        seen_list = seen_list[-MAX_SEEN_IDS:]

    state[key] = {
        "etag": meta.get("etag") or cached.get("etag", ""),
        "last_modified": meta.get("last_modified") or cached.get("last_modified", ""),
        "feed_title": feed_title,
        "seen_entry_ids": seen_list,
        "last_polled_at": int(time.time()),
    }
    return {"new": new_count, "skipped": skipped, "status": meta.get("status", 0), "feed_title": feed_title}


async def run_once(cfg: Config, state_path: Path) -> int:
    state = load_state(state_path)
    async with httpx.AsyncClient() as client:
        for feed in cfg.feeds:
            try:
                res = await poll_feed(client, feed, cfg, state)
                log.info("%s: +%d new, %d skipped (HTTP %d)",
                         feed.url, res["new"], res["skipped"], res["status"])
            except Exception as e:
                log.error("poll_feed error for %s: %s", feed.url, e)
    save_state(state_path, state)
    return 0


async def run_watch(cfg: Config, state_path: Path) -> int:
    state = load_state(state_path)
    next_due: dict[str, float] = {feed.url: 0.0 for feed in cfg.feeds}
    async with httpx.AsyncClient() as client:
        while True:
            now = time.time()
            for feed in cfg.feeds:
                if now < next_due.get(feed.url, 0.0):
                    continue
                try:
                    res = await poll_feed(client, feed, cfg, state)
                    if res["new"]:
                        log.info("%s: +%d new (HTTP %d)", feed.url, res["new"], res["status"])
                except Exception as e:
                    log.error("poll_feed error for %s: %s", feed.url, e)
                next_due[feed.url] = now + feed.interval_seconds
            save_state(state_path, state)
            await asyncio.sleep(30)


def main() -> int:
    parser = argparse.ArgumentParser(description="Dugg RSS Agent (client-side watcher)")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH, help="YAML config path")
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE_PATH, help="State cache path")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="Poll each feed once and exit")
    mode.add_argument("--watch", action="store_true", help="Run forever, polling on each feed's interval")
    args = parser.parse_args()

    if not args.config.exists():
        print(f"Config not found: {args.config}", file=sys.stderr)
        print(f"See {Path(__file__).with_name('dugg_rss_example.yaml')} for a starting template.", file=sys.stderr)
        return 1

    cfg = load_config(args.config)
    if not cfg.feeds:
        print("No feeds configured.", file=sys.stderr)
        return 1
    if not (cfg.default_target or all(f.target or f.rules for f in cfg.feeds)):
        print("Missing default_target and not every feed has its own target.", file=sys.stderr)
        return 1

    if args.watch:
        return asyncio.run(run_watch(cfg, args.state))
    return asyncio.run(run_once(cfg, args.state))


if __name__ == "__main__":
    sys.exit(main())
