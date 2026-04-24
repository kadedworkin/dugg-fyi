"""Tests for the Dugg agent's home-server + publish-scope logic.

Focused on the routing decisions the agent makes. Network / SSE / HTTP
transport is intentionally out of scope — those are mocked.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

# The agent lives in <repo>/agent/dugg_agent.py — not on the package path.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "agent"))

import dugg_agent  # noqa: E402
from dugg_agent import DuggAgent, DuggClient, ServerConfig  # noqa: E402


def _make_agent(*, with_home=True, two_servers=True) -> DuggAgent:
    servers = [ServerConfig(name="private", url="http://localhost:8411", api_key="k1", home=with_home)]
    if two_servers:
        servers.append(ServerConfig(name="chino-bandido", url="https://example.com", api_key="k2"))
    return DuggAgent(servers, anthropic_key=None)


def test_home_server_picks_flagged_server():
    agent = _make_agent(with_home=True)
    assert agent.home_server == "private"


def test_home_server_falls_back_to_first_when_none_flagged():
    # Neither server has home=True — fall back to first-listed.
    servers = [
        ServerConfig(name="first", url="http://a", api_key="ka"),
        ServerConfig(name="second", url="http://b", api_key="kb"),
    ]
    agent = DuggAgent(servers, anthropic_key=None)
    assert agent.home_server == "first"


def test_home_server_none_when_no_servers_configured():
    agent = DuggAgent([], anthropic_key=None)
    assert agent.home_server is None


def test_federate_with_explicit_targets_skips_scoring():
    agent = _make_agent()
    client = agent.clients["private"]
    client.publish = AsyncMock(return_value={"ok": True})
    client.routing_manifest = AsyncMock(return_value={"instances": []})

    asyncio.run(agent._federate(
        "private", "res-123", "scoring text doesn't matter here",
        explicit_targets=["chino-bandido"],
    ))

    client.publish.assert_awaited_once_with("res-123", ["chino-bandido"])
    client.routing_manifest.assert_not_awaited()


def test_federate_without_targets_scores_against_manifest():
    agent = _make_agent()
    client = agent.clients["private"]
    client.publish = AsyncMock(return_value={"ok": True})
    # Manifest with one matching instance — two keywords present in scoring text
    client.routing_manifest = AsyncMock(return_value={
        "instances": [{"name": "food-dugg", "topic": "food, restaurants, cooking"}],
    })

    asyncio.run(agent._federate(
        "private", "res-123", "a post about food and restaurants around town",
    ))

    client.publish.assert_awaited_once_with("res-123", ["food-dugg"])


def test_federate_picks_no_targets_when_manifest_empty():
    agent = _make_agent()
    client = agent.clients["private"]
    client.publish = AsyncMock()
    client.routing_manifest = AsyncMock(return_value={"instances": []})

    asyncio.run(agent._federate("private", "res-123", "any text"))

    client.publish.assert_not_awaited()


def test_handle_event_skips_federation_when_scope_is_none():
    """resource_added for a collection with publish_scope='none' must not federate."""
    agent = _make_agent()
    client = agent.clients["private"]
    my_uid = "user-123"
    agent.user_ids["private"] = my_uid
    agent._federate = AsyncMock()
    client.get_resource = AsyncMock(return_value={"result": "URL: https://example.com/x\nTitle: X\n"})
    client.edit_resource = AsyncMock()

    event = {
        "event_type": "resource_added",
        "payload": {
            "resource_id": "res-1",
            "submitted_by": my_uid,
            "collection_publish_scope": "none",
            "collection_id": "coll-1",
        },
    }

    asyncio.run(agent._handle_event("private", event))

    agent._federate.assert_not_awaited()


def test_handle_event_federates_when_scope_is_auto():
    agent = _make_agent()
    client = agent.clients["private"]
    my_uid = "user-123"
    agent.user_ids["private"] = my_uid
    agent._federate = AsyncMock()
    client.get_resource = AsyncMock(return_value={"result": "URL: https://example.com/x\nTitle: X\n"})
    client.edit_resource = AsyncMock()

    event = {
        "event_type": "resource_added",
        "payload": {
            "resource_id": "res-1",
            "submitted_by": my_uid,
            "collection_publish_scope": "auto",
            "collection_id": "coll-1",
        },
    }

    asyncio.run(agent._handle_event("private", event))

    agent._federate.assert_awaited_once()


def test_handle_event_reads_nested_payload_shape():
    """Regression: the agent previously read fields from top-level `event`,
    but the SSE generator wraps them under `payload`. Make sure we find the
    resource_id + actor via the payload path."""
    agent = _make_agent()
    client = agent.clients["private"]
    agent.user_ids["private"] = "user-123"
    agent._federate = AsyncMock()
    client.get_resource = AsyncMock(return_value={"result": "URL: https://example.com/x\n"})
    client.edit_resource = AsyncMock()

    event = {
        "event_type": "resource_added",
        "payload": {"resource_id": "res-42", "submitted_by": "user-123"},
    }

    asyncio.run(agent._handle_event("private", event))

    # We expect to have fetched the resource — if payload weren't parsed,
    # _handle_event bails before get_resource.
    client.get_resource.assert_awaited_once_with("res-42")


def test_parse_kv_strips_parenthetical_key_suffix():
    """`dugg_get` annotates transcript length as `Transcript (39 words): ...`.
    Without stripping the `(N words)` suffix, the key normalizes to
    `transcript_(39_words)` and the agent cannot find existing enrichment,
    causing redundant YouTube backfill on every event replay."""
    text = (
        "Resource: Me at the zoo\n"
        "Type: youtube\n"
        "Description: A short clip.\n"
        "Transcript (39 words): All right, so here we are in front of the elephants.\n"
    )
    parsed = DuggClient._parse_kv(text)
    assert parsed["transcript"] == "All right, so here we are in front of the elephants."
    assert parsed["description"] == "A short clip."
    assert parsed["type"] == "youtube"


def test_parse_kv_handles_nested_and_missing_parens():
    """Keys without parens parse unchanged; stray whitespace tolerated."""
    text = (
        "URL: https://example.com\n"
        "Tags (3): one, two, three\n"
        "  Tags  : four\n"
    )
    parsed = DuggClient._parse_kv(text)
    assert parsed["url"] == "https://example.com"
    # Later `Tags:` line wins (dict overwrite) — exercises the normalized
    # collision path.
    assert parsed["tags"] == "four"


def test_youtube_event_with_existing_transcript_skips_backfill():
    """If a YouTube resource already has description + transcript, the event
    handler must NOT call _enrich_youtube_locally. Prevents runaway backfill
    when the event stream replays the same event."""
    agent = _make_agent()
    client = agent.clients["private"]
    agent.user_ids["private"] = "user-123"
    agent._federate = AsyncMock()
    client.edit_resource = AsyncMock()
    client.get_resource = AsyncMock(return_value={
        "result": (
            "URL: https://www.youtube.com/watch?v=abc\n"
            "Type: youtube\n"
            "Description: Existing description text.\n"
            "Transcript (39 words): Already enriched transcript body.\n"
        )
    })

    called = []

    async def _fake_enrich(url):
        called.append(url)
        return {"description": "x", "transcript": "y"}

    original_enrich = dugg_agent._enrich_youtube_locally
    dugg_agent._enrich_youtube_locally = _fake_enrich
    try:
        event = {
            "event_type": "resource_added",
            "payload": {"resource_id": "res-yt", "submitted_by": "user-other"},
        }
        asyncio.run(agent._handle_event("private", event))
    finally:
        dugg_agent._enrich_youtube_locally = original_enrich

    assert called == [], "YouTube backfill should not run when transcript is already present"
