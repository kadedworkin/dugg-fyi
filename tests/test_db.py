"""Tests for the Dugg database layer."""

import tempfile
from pathlib import Path

import pytest

from dugg.db import DuggDB


@pytest.fixture
def db():
    with tempfile.TemporaryDirectory() as tmpdir:
        d = DuggDB(Path(tmpdir) / "test.db")
        yield d
        d.close()


def test_create_user(db):
    user = db.create_user("Kade")
    assert user["name"] == "Kade"
    assert user["api_key"].startswith("dugg_")
    assert user["id"]


def test_get_user_by_api_key(db):
    user = db.create_user("Kade")
    found = db.get_user_by_api_key(user["api_key"])
    assert found["id"] == user["id"]
    assert found["name"] == "Kade"


def test_create_collection(db):
    user = db.create_user("Kade")
    coll = db.create_collection("AI Research", user["id"], description="Agent stuff")
    assert coll["name"] == "AI Research"
    assert coll["visibility"] == "private"


def test_list_collections(db):
    user = db.create_user("Kade")
    db.create_collection("AI", user["id"])
    db.create_collection("Marketing", user["id"])
    colls = db.list_collections(user["id"])
    assert len(colls) == 2


def test_add_resource(db):
    user = db.create_user("Kade")
    coll = db.create_collection("AI", user["id"])
    res = db.add_resource(
        url="https://youtube.com/watch?v=abc123",
        collection_id=coll["id"],
        submitted_by=user["id"],
        note="Great video on agent architectures",
        title="Agent Architectures Deep Dive",
        source_type="youtube",
        tags=["ai", "agents"],
    )
    assert res["title"] == "Agent Architectures Deep Dive"
    assert res["tags"] == ["ai", "agents"]


def test_search_fts(db):
    user = db.create_user("Kade")
    coll = db.create_collection("AI", user["id"])
    db.add_resource(
        url="https://example.com/1",
        collection_id=coll["id"],
        submitted_by=user["id"],
        title="Deep dive into webhook architectures",
        description="How webhooks work at scale",
    )
    db.add_resource(
        url="https://example.com/2",
        collection_id=coll["id"],
        submitted_by=user["id"],
        title="Landing page optimization guide",
        description="Convert more visitors",
    )
    results = db.search("webhook", user["id"])
    assert len(results) == 1
    assert "webhook" in results[0]["title"].lower()


def test_search_across_collections(db):
    user = db.create_user("Kade")
    c1 = db.create_collection("AI", user["id"])
    c2 = db.create_collection("Marketing", user["id"])
    db.add_resource(url="https://example.com/1", collection_id=c1["id"], submitted_by=user["id"], title="AI agents")
    db.add_resource(url="https://example.com/2", collection_id=c2["id"], submitted_by=user["id"], title="AI marketing")
    results = db.search("AI", user["id"])
    assert len(results) == 2


def test_tag_resource(db):
    user = db.create_user("Kade")
    coll = db.create_collection("AI", user["id"])
    res = db.add_resource(url="https://example.com/1", collection_id=coll["id"], submitted_by=user["id"])
    tags = db.tag_resource(res["id"], ["ai", "agents", "tools"], source="agent")
    assert len(tags) == 3


def test_share_collection(db):
    kade = db.create_user("Kade")
    rocco = db.create_user("Rocco")
    coll = db.create_collection("Shared AI", kade["id"], visibility="shared")
    db.add_collection_member(coll["id"], rocco["id"])

    db.add_resource(url="https://example.com/1", collection_id=coll["id"], submitted_by=kade["id"], title="Cool AI thing")

    # Rocco should see it
    results = db.search("AI", rocco["id"])
    assert len(results) == 1


def test_share_rules_exclude(db):
    kade = db.create_user("Kade")
    rocco = db.create_user("Rocco")
    coll = db.create_collection("Mixed", kade["id"], visibility="shared")
    db.add_collection_member(coll["id"], rocco["id"])

    # Add two resources
    r1 = db.add_resource(url="https://example.com/1", collection_id=coll["id"], submitted_by=kade["id"], title="AI thing", tags=["ai"])
    r2 = db.add_resource(url="https://example.com/2", collection_id=coll["id"], submitted_by=kade["id"], title="Personal vlog", tags=["personal"])

    # Set share rule: exclude personal
    db.set_share_rule(coll["id"], rocco["id"], exclude_tags=["personal"])

    feed = db.get_feed(rocco["id"])
    titles = [r["title"] for r in feed]
    assert "AI thing" in titles
    assert "Personal vlog" not in titles


def test_share_rules_include(db):
    kade = db.create_user("Kade")
    rocco = db.create_user("Rocco")
    coll = db.create_collection("Filtered", kade["id"], visibility="shared")
    db.add_collection_member(coll["id"], rocco["id"])

    db.add_resource(url="https://example.com/1", collection_id=coll["id"], submitted_by=kade["id"], title="AI article", tags=["ai"])
    db.add_resource(url="https://example.com/2", collection_id=coll["id"], submitted_by=kade["id"], title="Cooking video", tags=["cooking"])

    # Only share AI stuff
    db.set_share_rule(coll["id"], rocco["id"], include_tags=["ai"])

    feed = db.get_feed(rocco["id"])
    titles = [r["title"] for r in feed]
    assert "AI article" in titles
    assert "Cooking video" not in titles


def test_feed_shows_own_resources_regardless_of_rules(db):
    kade = db.create_user("Kade")
    rocco = db.create_user("Rocco")
    coll = db.create_collection("Shared", kade["id"], visibility="shared")
    db.add_collection_member(coll["id"], rocco["id"])

    # Rocco adds something tagged personal
    db.add_resource(url="https://example.com/1", collection_id=coll["id"], submitted_by=rocco["id"], title="Rocco personal", tags=["personal"])

    # Even with an exclude rule, Rocco sees his own stuff
    db.set_share_rule(coll["id"], rocco["id"], exclude_tags=["personal"])
    feed = db.get_feed(rocco["id"])
    assert len(feed) == 1
