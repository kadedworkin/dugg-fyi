"""RSS / Atom ingestion for Dugg.

Shared between the server-side polling daemon and the agent-side watcher.
The core function `sync_feed(db, subscription, *, now=...)` parses a feed URL,
diffs against previously-seen entry IDs, and adds new entries as resources
via `db.add_resource`. Supports parameterized (authenticated) feed URLs —
titles and publication dates are still captured even when the linked
resources require a subscription.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Optional
from urllib.parse import parse_qs, urlparse

import feedparser
import httpx

logger = logging.getLogger("dugg.rss")


MAX_SEEN_IDS = 500  # bounded per subscription — rolling window


_AUTH_PARAM_HINTS = {
    "token", "auth", "auth_token", "authtoken", "access_token",
    "key", "apikey", "api_key", "user", "user_id", "uid",
    "subscriber", "subscriber_id", "sub", "sig", "signature",
    "session", "sessionid", "s", "t",
}


def is_private_link(url: str) -> bool:
    """Heuristic: does this URL look like it carries per-user authentication?

    Private links are preserved as-is but flagged in `raw_metadata` so the
    UI can warn other viewers that the link may require their own subscription.
    """
    if not url:
        return False
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    params = parse_qs(parsed.query, keep_blank_values=True)
    lowered = {k.lower() for k in params.keys()}
    if lowered & _AUTH_PARAM_HINTS:
        return True
    # Long opaque query values (signed URLs) without clear-text params
    for values in params.values():
        for v in values:
            if len(v) > 32 and any(ch.isalnum() for ch in v):
                return True
    return False


@dataclass
class FeedEntry:
    """Normalized representation of one feed entry."""
    entry_id: str      # GUID from the feed, or URL if the feed lacks one
    url: str
    title: str
    description: str
    published_at: str  # ISO 8601, "" if not provided
    author: str
    is_private: bool
    categories: list[str] = field(default_factory=list)


def _entry_to_normalized(entry) -> Optional[FeedEntry]:
    """Convert a raw feedparser entry into a FeedEntry, or None if unusable."""
    url = (entry.get("link") or "").strip()
    entry_id = (entry.get("id") or entry.get("guid") or url or "").strip()
    if not entry_id or not url:
        return None

    title = (entry.get("title") or url).strip()

    description = ""
    if entry.get("summary"):
        description = entry["summary"]
    elif entry.get("description"):
        description = entry["description"]

    published_at = ""
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        tm = entry.get(key)
        if tm:
            try:
                published_at = datetime.fromtimestamp(time.mktime(tm), tz=timezone.utc).isoformat()
                break
            except Exception:
                pass
    if not published_at:
        for key in ("published", "updated", "created"):
            raw = entry.get(key)
            if raw:
                published_at = str(raw).strip()
                break

    author = (entry.get("author") or "").strip()

    # Extract categories/tags from <category> elements
    categories = []
    for tag in entry.get("tags", []):
        term = (tag.get("term") or tag.get("label") or "").strip()
        if term:
            categories.append(term)

    return FeedEntry(
        entry_id=entry_id,
        url=url,
        title=title,
        description=description,
        published_at=published_at,
        author=author,
        is_private=is_private_link(url),
        categories=categories,
    )


async def fetch_and_parse(
    feed_url: str,
    *,
    etag: str = "",
    last_modified: str = "",
    timeout: float = 20.0,
) -> tuple[list[FeedEntry], dict]:
    """Fetch a feed URL and parse it. Returns (entries, metadata).

    `metadata` is a dict with keys `etag`, `last_modified`, `status`,
    `feed_title`, `feed_description` — suitable for stashing alongside the
    subscription record so the next poll can send conditional-GET headers.
    """
    headers = {"User-Agent": "Dugg-RSS/0.1 (+https://dugg.fyi)"}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified

    meta = {"etag": etag, "last_modified": last_modified, "status": 0, "feed_title": "", "feed_description": ""}

    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=timeout) as client:
            resp = await client.get(feed_url, headers=headers)
    except (httpx.HTTPError, httpx.TimeoutException) as e:
        logger.warning(f"RSS fetch failed for {feed_url}: {e}")
        return [], meta

    meta["status"] = resp.status_code
    if resp.status_code == 304:
        return [], meta
    if resp.status_code >= 400:
        logger.warning(f"RSS {resp.status_code} for {feed_url}")
        return [], meta

    meta["etag"] = resp.headers.get("etag", "") or etag
    meta["last_modified"] = resp.headers.get("last-modified", "") or last_modified

    parsed = feedparser.parse(resp.content)
    if parsed.get("bozo") and not parsed.get("entries"):
        logger.warning(f"Feed parse error for {feed_url}: {parsed.get('bozo_exception')}")
        return [], meta

    feed_info = parsed.get("feed", {}) or {}
    meta["feed_title"] = (feed_info.get("title") or "").strip()
    meta["feed_description"] = (feed_info.get("subtitle") or feed_info.get("description") or "").strip()

    entries: list[FeedEntry] = []
    for e in parsed.get("entries", []):
        norm = _entry_to_normalized(e)
        if norm:
            entries.append(norm)
    return entries, meta


def ingest_entry(
    db,
    entry: FeedEntry,
    *,
    collection_id: str,
    submitted_by: str,
    source_label: str = "",
    tag_label: str = "rss",
) -> dict:
    """Add a single feed entry to Dugg as a resource."""
    raw_metadata = {
        "source": "rss",
        "rss_entry_id": entry.entry_id,
    }
    if source_label:
        raw_metadata["source_label"] = source_label
    if entry.published_at:
        raw_metadata["published_at"] = entry.published_at
    if entry.is_private:
        raw_metadata["is_private_link"] = True

    tags = [tag_label] if tag_label else []
    for cat in entry.categories:
        if cat.lower() not in {t.lower() for t in tags}:
            tags.append(cat)

    return db.add_resource(
        url=entry.url,
        collection_id=collection_id,
        submitted_by=submitted_by,
        title=entry.title,
        description=entry.description,
        author=entry.author,
        source_type="article",
        raw_metadata=raw_metadata,
        tags=tags,
        tag_source="agent",
    )


async def sync_feed(
    db,
    subscription: dict,
    *,
    source_label: str = "",
) -> dict:
    """Poll one subscription, add any new entries, return a result summary.

    `subscription` is a dict with keys: id, user_id, collection_id, feed_url,
    etag, last_modified, seen_entry_ids (JSON-encoded list), tag_label.

    Returns `{new: int, skipped: int, status: int, etag: str, last_modified: str,
    feed_title: str}` so the caller can persist state and report back.
    """
    entries, meta = await fetch_and_parse(
        subscription["feed_url"],
        etag=subscription.get("etag", "") or "",
        last_modified=subscription.get("last_modified", "") or "",
    )

    seen_raw = subscription.get("seen_entry_ids", "[]") or "[]"
    try:
        seen = list(json.loads(seen_raw))
    except (ValueError, TypeError):
        seen = []
    seen_set = set(seen)

    tag_label = subscription.get("tag_label") or "rss"
    new_count = 0
    skipped = 0
    for entry in entries:
        if entry.entry_id in seen_set:
            skipped += 1
            continue
        ingest_entry(
            db,
            entry,
            collection_id=subscription["collection_id"],
            submitted_by=subscription["user_id"],
            source_label=source_label or meta.get("feed_title", ""),
            tag_label=tag_label,
        )
        seen.append(entry.entry_id)
        seen_set.add(entry.entry_id)
        new_count += 1

    if len(seen) > MAX_SEEN_IDS:
        seen = seen[-MAX_SEEN_IDS:]

    return {
        "new": new_count,
        "skipped": skipped,
        "status": meta["status"],
        "etag": meta["etag"],
        "last_modified": meta["last_modified"],
        "feed_title": meta["feed_title"],
        "seen_entry_ids": json.dumps(seen),
    }


async def rss_loop(db, *, interval: int = 60):
    """Background polling loop. Wakes every `interval` seconds, syncs any
    subscription whose next_poll_at has passed."""
    while True:
        try:
            due = db.list_due_rss_subscriptions()
        except Exception as e:
            logger.error(f"RSS due-list error: {e}")
            due = []

        for sub in due:
            try:
                result = await sync_feed(db, dict(sub))
                db.update_rss_subscription_state(
                    sub["id"],
                    etag=result["etag"],
                    last_modified=result["last_modified"],
                    seen_entry_ids=result["seen_entry_ids"],
                    feed_title=result["feed_title"] or sub.get("feed_title") or "",
                )
                if result["new"]:
                    logger.info(f"RSS[{sub['feed_url']}]: +{result['new']} new")
            except Exception as e:
                logger.error(f"RSS sync error for {sub.get('feed_url')}: {e}")

        await asyncio.sleep(interval)


def start_rss_daemon(db, interval: int = 60) -> asyncio.Task:
    """Launch the RSS polling loop as a background asyncio task."""
    return asyncio.create_task(rss_loop(db, interval=interval))
