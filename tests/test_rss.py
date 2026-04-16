"""Tests for RSS/Atom ingestion — shared parse, private-link heuristic,
db subscription CRUD, sync loop with mocked feed."""

import asyncio
import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from dugg.db import DuggDB
from dugg.rss import (
    FeedEntry,
    ingest_entry,
    is_private_link,
    sync_feed,
)


@pytest.fixture
def db():
    with tempfile.TemporaryDirectory() as tmpdir:
        d = DuggDB(Path(tmpdir) / "test.db")
        yield d
        d.close()


@pytest.fixture
def user_and_collection(db):
    user = db.create_user("Test User")
    coll_id = db.ensure_default_collection(user["id"])
    return user, coll_id


# --- private link heuristic ---

def test_is_private_link_plain_url():
    assert is_private_link("https://example.com/feed.xml") is False


def test_is_private_link_token_param():
    assert is_private_link("https://atp.fm/rss?token=SECRET123") is True


def test_is_private_link_apikey_param():
    assert is_private_link("https://every.to/feed?apikey=xyz") is True


def test_is_private_link_long_opaque_value():
    # Signed URL pattern — long opaque value with no clear auth name
    url = "https://cdn.example.com/feed?sig=abcdefghijklmnopqrstuvwxyz0123456789"
    assert is_private_link(url) is True


def test_is_private_link_short_values_ignored():
    # Bare short query values shouldn't trip the heuristic
    assert is_private_link("https://example.com/feed?page=1&size=20") is False


def test_is_private_link_handles_bad_url():
    assert is_private_link("") is False


# --- subscription CRUD ---

def test_add_and_list_subscription(db, user_and_collection):
    user, coll_id = user_and_collection
    sub = db.add_rss_subscription(
        user_id=user["id"],
        collection_id=coll_id,
        feed_url="https://example.com/rss.xml",
        tag_label="rss",
        poll_interval_seconds=1800,
    )
    assert sub["id"]
    subs = db.list_rss_subscriptions(user_id=user["id"])
    assert len(subs) == 1
    assert subs[0]["feed_url"] == "https://example.com/rss.xml"
    assert subs[0]["poll_interval_seconds"] == 1800


def test_duplicate_subscription_raises(db, user_and_collection):
    import sqlite3
    user, coll_id = user_and_collection
    db.add_rss_subscription(user_id=user["id"], collection_id=coll_id,
                            feed_url="https://example.com/rss.xml")
    with pytest.raises(sqlite3.IntegrityError):
        db.add_rss_subscription(user_id=user["id"], collection_id=coll_id,
                                feed_url="https://example.com/rss.xml")


def test_pause_and_resume(db, user_and_collection):
    user, coll_id = user_and_collection
    sub = db.add_rss_subscription(user_id=user["id"], collection_id=coll_id,
                                   feed_url="https://example.com/rss.xml")
    assert db.set_rss_subscription_enabled(sub["id"], False) is True
    s = db.get_rss_subscription(sub["id"])
    assert s["enabled"] == 0
    assert db.set_rss_subscription_enabled(sub["id"], True) is True
    s = db.get_rss_subscription(sub["id"])
    assert s["enabled"] == 1


def test_remove_subscription(db, user_and_collection):
    user, coll_id = user_and_collection
    sub = db.add_rss_subscription(user_id=user["id"], collection_id=coll_id,
                                   feed_url="https://example.com/rss.xml")
    assert db.remove_rss_subscription(sub["id"]) is True
    assert db.get_rss_subscription(sub["id"]) is None


def test_list_due_skips_paused(db, user_and_collection):
    user, coll_id = user_and_collection
    sub = db.add_rss_subscription(user_id=user["id"], collection_id=coll_id,
                                   feed_url="https://example.com/rss.xml")
    db.set_rss_subscription_enabled(sub["id"], False)
    due = db.list_due_rss_subscriptions()
    assert not any(s["id"] == sub["id"] for s in due)


def test_list_due_respects_interval(db, user_and_collection):
    user, coll_id = user_and_collection
    sub = db.add_rss_subscription(user_id=user["id"], collection_id=coll_id,
                                   feed_url="https://example.com/rss.xml",
                                   poll_interval_seconds=3600)
    # Freshly created — last_polled_at is empty → should be due immediately.
    due = db.list_due_rss_subscriptions()
    assert any(s["id"] == sub["id"] for s in due)

    # Mark polled "now" — should no longer be due.
    db.update_rss_subscription_state(sub["id"], etag="e", last_modified="lm",
                                      seen_entry_ids="[]", feed_title="")
    due = db.list_due_rss_subscriptions()
    assert not any(s["id"] == sub["id"] for s in due)


# --- ingestion + sync ---

def test_ingest_entry_adds_resource_with_metadata(db, user_and_collection):
    user, coll_id = user_and_collection
    entry = FeedEntry(
        entry_id="entry-1",
        url="https://example.com/post",
        title="A Post",
        description="Some words.",
        published_at="2026-04-01T12:00:00+00:00",
        author="Someone",
        is_private=False,
    )
    res = ingest_entry(db, entry, collection_id=coll_id, submitted_by=user["id"],
                       source_label="Example Feed", tag_label="rss")
    assert res["url"] == "https://example.com/post"
    full = db.get_resource(res["id"])
    meta = full.get("raw_metadata") or {}
    if isinstance(meta, str):
        meta = json.loads(meta)
    assert meta.get("source") == "rss"
    assert meta.get("rss_entry_id") == "entry-1"
    assert meta.get("source_label") == "Example Feed"
    assert meta.get("published_at") == "2026-04-01T12:00:00+00:00"
    assert "rss" in [t["label"] if isinstance(t, dict) else t for t in full.get("tags") or []]


def test_ingest_entry_flags_private_link(db, user_and_collection):
    user, coll_id = user_and_collection
    entry = FeedEntry(
        entry_id="e1",
        url="https://atp.fm/episode/1?token=SECRET",
        title="Ep 1",
        description="",
        published_at="",
        author="",
        is_private=True,
    )
    res = ingest_entry(db, entry, collection_id=coll_id, submitted_by=user["id"])
    full = db.get_resource(res["id"])
    meta = full["raw_metadata"]
    if isinstance(meta, str):
        meta = json.loads(meta)
    assert meta.get("is_private_link") is True


def _mock_fetch_result(entries):
    async def _mock(feed_url, *, etag="", last_modified="", timeout=20.0):
        return entries, {
            "etag": "new-etag",
            "last_modified": "Tue, 01 Apr 2026 12:00:00 GMT",
            "status": 200,
            "feed_title": "Mock Feed",
            "feed_description": "",
        }
    return _mock


def test_sync_feed_ingests_new_and_skips_seen(db, user_and_collection):
    user, coll_id = user_and_collection
    sub = db.add_rss_subscription(user_id=user["id"], collection_id=coll_id,
                                   feed_url="https://example.com/rss.xml")

    e1 = FeedEntry(entry_id="a", url="https://example.com/a", title="A", description="",
                   published_at="", author="", is_private=False)
    e2 = FeedEntry(entry_id="b", url="https://example.com/b", title="B", description="",
                   published_at="", author="", is_private=False)

    with patch("dugg.rss.fetch_and_parse", _mock_fetch_result([e1, e2])):
        result = asyncio.run(sync_feed(db, dict(sub)))
    assert result["new"] == 2
    assert result["skipped"] == 0
    seen = json.loads(result["seen_entry_ids"])
    assert set(seen) == {"a", "b"}

    # Second sync — both entries already seen, nothing new.
    sub2 = dict(sub)
    sub2["seen_entry_ids"] = result["seen_entry_ids"]
    with patch("dugg.rss.fetch_and_parse", _mock_fetch_result([e1, e2])):
        result2 = asyncio.run(sync_feed(db, sub2))
    assert result2["new"] == 0
    assert result2["skipped"] == 2
