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


# --- Publishing ---

def test_publish_resource(db):
    user = db.create_user("Kade")
    coll = db.create_collection("AI", user["id"])
    res = db.add_resource(url="https://example.com/1", collection_id=coll["id"], submitted_by=user["id"], title="AI article")
    results = db.publish_resource(res["id"], ["public", "aev-team"])
    assert len(results) == 2
    assert results[0]["target"] == "public"
    assert results[1]["target"] == "aev-team"


def test_get_publish_targets(db):
    user = db.create_user("Kade")
    coll = db.create_collection("AI", user["id"])
    res = db.add_resource(url="https://example.com/1", collection_id=coll["id"], submitted_by=user["id"])
    db.publish_resource(res["id"], ["public", "inner-circle"])
    targets = db.get_publish_targets(res["id"])
    assert len(targets) == 2
    target_names = {t["target"] for t in targets}
    assert target_names == {"public", "inner-circle"}


def test_unpublish_specific_target(db):
    user = db.create_user("Kade")
    coll = db.create_collection("AI", user["id"])
    res = db.add_resource(url="https://example.com/1", collection_id=coll["id"], submitted_by=user["id"])
    db.publish_resource(res["id"], ["public", "aev-team"])
    db.unpublish_resource(res["id"], ["public"])
    targets = db.get_publish_targets(res["id"])
    assert len(targets) == 1
    assert targets[0]["target"] == "aev-team"


def test_unpublish_all(db):
    user = db.create_user("Kade")
    coll = db.create_collection("AI", user["id"])
    res = db.add_resource(url="https://example.com/1", collection_id=coll["id"], submitted_by=user["id"])
    db.publish_resource(res["id"], ["public", "aev-team"])
    db.unpublish_resource(res["id"])
    targets = db.get_publish_targets(res["id"])
    assert len(targets) == 0


def test_get_published_resources(db):
    user = db.create_user("Kade")
    coll = db.create_collection("AI", user["id"])
    r1 = db.add_resource(url="https://example.com/1", collection_id=coll["id"], submitted_by=user["id"], title="Public article")
    r2 = db.add_resource(url="https://example.com/2", collection_id=coll["id"], submitted_by=user["id"], title="Team only")
    r3 = db.add_resource(url="https://example.com/3", collection_id=coll["id"], submitted_by=user["id"], title="Private")
    db.publish_resource(r1["id"], ["public", "aev-team"])
    db.publish_resource(r2["id"], ["aev-team"])
    # r3 not published anywhere

    public = db.get_published_resources("public")
    assert len(public) == 1
    assert public[0]["title"] == "Public article"

    team = db.get_published_resources("aev-team")
    assert len(team) == 2


def test_publish_idempotent(db):
    user = db.create_user("Kade")
    coll = db.create_collection("AI", user["id"])
    res = db.add_resource(url="https://example.com/1", collection_id=coll["id"], submitted_by=user["id"])
    db.publish_resource(res["id"], ["public"])
    db.publish_resource(res["id"], ["public"])  # Should not duplicate
    targets = db.get_publish_targets(res["id"])
    assert len(targets) == 1


# --- Reactions ---

def test_react_to_resource(db):
    user = db.create_user("Kade")
    rocco = db.create_user("Rocco")
    coll = db.create_collection("AI", user["id"], visibility="shared")
    db.add_collection_member(coll["id"], rocco["id"])
    res = db.add_resource(url="https://example.com/1", collection_id=coll["id"], submitted_by=user["id"], title="Cool thing")
    result = db.react_to_resource(res["id"], rocco["id"], "tap")
    assert result["reaction_type"] == "tap"
    assert result["user_id"] == rocco["id"]


def test_reaction_idempotent(db):
    user = db.create_user("Kade")
    rocco = db.create_user("Rocco")
    coll = db.create_collection("AI", user["id"], visibility="shared")
    db.add_collection_member(coll["id"], rocco["id"])
    res = db.add_resource(url="https://example.com/1", collection_id=coll["id"], submitted_by=user["id"])
    db.react_to_resource(res["id"], rocco["id"], "tap")
    db.react_to_resource(res["id"], rocco["id"], "tap")  # Duplicate — should be no-op
    reactions = db.get_reactions(res["id"], user["id"])
    assert reactions["total"] == 1


def test_multiple_reaction_types(db):
    user = db.create_user("Kade")
    rocco = db.create_user("Rocco")
    coll = db.create_collection("AI", user["id"], visibility="shared")
    db.add_collection_member(coll["id"], rocco["id"])
    res = db.add_resource(url="https://example.com/1", collection_id=coll["id"], submitted_by=user["id"])
    db.react_to_resource(res["id"], rocco["id"], "tap")
    db.react_to_resource(res["id"], rocco["id"], "star")
    reactions = db.get_reactions(res["id"], user["id"])
    assert reactions["total"] == 2
    assert reactions["breakdown"]["tap"] == 1
    assert reactions["breakdown"]["star"] == 1


def test_reactions_only_visible_to_publisher(db):
    kade = db.create_user("Kade")
    rocco = db.create_user("Rocco")
    miles = db.create_user("Miles")
    coll = db.create_collection("Shared", kade["id"], visibility="shared")
    db.add_collection_member(coll["id"], rocco["id"])
    db.add_collection_member(coll["id"], miles["id"])
    res = db.add_resource(url="https://example.com/1", collection_id=coll["id"], submitted_by=kade["id"])
    db.react_to_resource(res["id"], rocco["id"], "tap")
    db.react_to_resource(res["id"], miles["id"], "star")

    # Kade (publisher) sees reactions
    reactions = db.get_reactions(res["id"], kade["id"])
    assert reactions is not None
    assert reactions["total"] == 2

    # Rocco (reactor) cannot see aggregates
    reactions = db.get_reactions(res["id"], rocco["id"])
    assert reactions is None


def test_reactions_summary(db):
    kade = db.create_user("Kade")
    rocco = db.create_user("Rocco")
    miles = db.create_user("Miles")
    coll = db.create_collection("Shared", kade["id"], visibility="shared")
    db.add_collection_member(coll["id"], rocco["id"])
    db.add_collection_member(coll["id"], miles["id"])

    r1 = db.add_resource(url="https://example.com/1", collection_id=coll["id"], submitted_by=kade["id"], title="Article A")
    r2 = db.add_resource(url="https://example.com/2", collection_id=coll["id"], submitted_by=kade["id"], title="Article B")
    db.react_to_resource(r1["id"], rocco["id"], "tap")
    db.react_to_resource(r1["id"], miles["id"], "tap")
    db.react_to_resource(r2["id"], rocco["id"], "star")

    summary = db.get_my_reactions_summary(kade["id"])
    assert len(summary) == 2
    # Article A has more reactions, should be first
    assert summary[0]["title"] == "Article A"
    assert summary[0]["total"] == 2
    assert summary[1]["title"] == "Article B"
    assert summary[1]["total"] == 1


# --- Instances ---

def test_create_instance(db):
    user = db.create_user("Kade")
    inst = db.create_instance("Chino Bandito", user["id"], topic="Food, restaurants, recipes", access_mode="invite")
    assert inst["name"] == "Chino Bandito"
    assert inst["topic"] == "Food, restaurants, recipes"
    assert inst["access_mode"] == "invite"
    assert inst["owner_id"] == user["id"]


def test_list_instances(db):
    user = db.create_user("Kade")
    db.create_instance("Food Dugg", user["id"], topic="Food stuff")
    db.create_instance("AI Dugg", user["id"], topic="AI stuff")
    instances = db.list_instances(user["id"])
    assert len(instances) == 2


def test_update_instance(db):
    user = db.create_user("Kade")
    inst = db.create_instance("AI", user["id"])
    updated = db.update_instance(inst["id"], user["id"], topic="AI research, agents, LLMs")
    assert updated["topic"] == "AI research, agents, LLMs"


def test_update_instance_owner_only(db):
    kade = db.create_user("Kade")
    rocco = db.create_user("Rocco")
    inst = db.create_instance("AI", kade["id"])
    result = db.update_instance(inst["id"], rocco["id"], topic="Hacked")
    assert result is None


def test_subscribe_to_instance(db):
    kade = db.create_user("Kade")
    rocco = db.create_user("Rocco")
    inst = db.create_instance("AI", kade["id"])
    db.subscribe_to_instance(inst["id"], rocco["id"])
    instances = db.list_instances(rocco["id"])
    assert len(instances) == 1
    assert instances[0]["name"] == "AI"


# --- Invite Tree ---

def test_invite_member(db):
    kade = db.create_user("Kade")
    rocco = db.create_user("Rocco")
    coll = db.create_collection("Shared", kade["id"], visibility="shared")
    result = db.invite_member(coll["id"], kade["id"], rocco["id"])
    assert result["invited_by"] == kade["id"]
    assert result["user_id"] == rocco["id"]
    # Rocco should have access
    feed = db.get_feed(rocco["id"])
    assert isinstance(feed, list)


def test_invite_chain(db):
    kade = db.create_user("Kade")
    rocco = db.create_user("Rocco")
    clint = db.create_user("Clint")
    miles = db.create_user("Miles")
    coll = db.create_collection("Shared", kade["id"], visibility="shared")
    db.invite_member(coll["id"], kade["id"], rocco["id"])
    db.invite_member(coll["id"], rocco["id"], clint["id"])
    db.invite_member(coll["id"], clint["id"], miles["id"])
    tree = db.get_invite_tree(coll["id"], kade["id"])
    user_ids = {m["user_id"] for m in tree}
    assert rocco["id"] in user_ids
    assert clint["id"] in user_ids
    assert miles["id"] in user_ids
    assert len(tree) == 3


def test_invite_tree_depth(db):
    kade = db.create_user("Kade")
    rocco = db.create_user("Rocco")
    clint = db.create_user("Clint")
    coll = db.create_collection("Shared", kade["id"], visibility="shared")
    db.invite_member(coll["id"], kade["id"], rocco["id"])
    db.invite_member(coll["id"], rocco["id"], clint["id"])
    tree = db.get_invite_tree(coll["id"], kade["id"])
    by_user = {m["user_id"]: m for m in tree}
    assert by_user[rocco["id"]]["depth"] == 1
    assert by_user[clint["id"]]["depth"] == 2


def test_member_status(db):
    kade = db.create_user("Kade")
    rocco = db.create_user("Rocco")
    coll = db.create_collection("Shared", kade["id"], visibility="shared")
    db.invite_member(coll["id"], kade["id"], rocco["id"])
    status = db.get_member_status(coll["id"], rocco["id"])
    assert status["status"] == "active"
    assert status["invited_by"] == kade["id"]


# --- Ban Cascade ---

def test_ban_simple(db):
    kade = db.create_user("Kade")
    rocco = db.create_user("Rocco")
    coll = db.create_collection("Shared", kade["id"], visibility="shared")
    db.invite_member(coll["id"], kade["id"], rocco["id"])
    result = db.ban_member(coll["id"], rocco["id"], cascade=False)
    assert rocco["id"] in result["banned"]
    status = db.get_member_status(coll["id"], rocco["id"])
    assert status["status"] == "banned"


def test_ban_cascade_depth1_hard_prune(db):
    kade = db.create_user("Kade")
    spammer = db.create_user("Spammer")
    victim = db.create_user("Victim")
    coll = db.create_collection("Shared", kade["id"], visibility="shared")
    db.invite_member(coll["id"], kade["id"], spammer["id"])
    db.invite_member(coll["id"], spammer["id"], victim["id"])
    result = db.ban_member(coll["id"], spammer["id"], cascade=True)
    assert spammer["id"] in result["banned"]
    assert victim["id"] in result["banned"]  # Depth 1 from spammer = hard prune


def test_ban_cascade_depth2_credit_score_survives(db):
    kade = db.create_user("Kade")
    spammer = db.create_user("Spammer")
    middleman = db.create_user("Middleman")
    good_actor = db.create_user("GoodActor")
    coll = db.create_collection("Shared", kade["id"], visibility="shared")
    db.invite_member(coll["id"], kade["id"], spammer["id"])
    db.invite_member(coll["id"], spammer["id"], middleman["id"])
    db.invite_member(coll["id"], middleman["id"], good_actor["id"])

    # Good actor contributes a lot — above threshold
    for i in range(6):
        db.add_resource(url=f"https://example.com/{i}", collection_id=coll["id"], submitted_by=good_actor["id"], title=f"Resource {i}")

    result = db.ban_member(coll["id"], spammer["id"], cascade=True, credit_threshold=5)
    assert spammer["id"] in result["banned"]
    assert middleman["id"] in result["banned"]  # Depth 1 = hard prune
    assert good_actor["id"] in result["survived"]  # Depth 2, score >= 5

    # Good actor is re-rooted under owner
    status = db.get_member_status(coll["id"], good_actor["id"])
    assert status["status"] == "active"
    assert status["invited_by"] == kade["id"]


def test_ban_cascade_depth2_low_score_banned(db):
    kade = db.create_user("Kade")
    spammer = db.create_user("Spammer")
    middleman = db.create_user("Middleman")
    ghost = db.create_user("Ghost")
    coll = db.create_collection("Shared", kade["id"], visibility="shared")
    db.invite_member(coll["id"], kade["id"], spammer["id"])
    db.invite_member(coll["id"], spammer["id"], middleman["id"])
    db.invite_member(coll["id"], middleman["id"], ghost["id"])
    # Ghost has zero contributions
    result = db.ban_member(coll["id"], spammer["id"], cascade=True, credit_threshold=5)
    assert ghost["id"] in result["banned"]


def test_banned_member_loses_access(db):
    kade = db.create_user("Kade")
    rocco = db.create_user("Rocco")
    coll = db.create_collection("Shared", kade["id"], visibility="shared")
    db.invite_member(coll["id"], kade["id"], rocco["id"])
    db.add_resource(url="https://example.com/1", collection_id=coll["id"], submitted_by=kade["id"], title="Secret")
    # Rocco can see it
    feed_before = db.get_feed(rocco["id"])
    assert len(feed_before) == 1
    # Ban Rocco
    db.ban_member(coll["id"], rocco["id"], cascade=False)
    # Rocco can't see it anymore
    feed_after = db.get_feed(rocco["id"])
    assert len(feed_after) == 0


def test_credit_score(db):
    kade = db.create_user("Kade")
    rocco = db.create_user("Rocco")
    coll = db.create_collection("Shared", kade["id"], visibility="shared")
    db.invite_member(coll["id"], kade["id"], rocco["id"])
    # Rocco submits 3 resources
    for i in range(3):
        r = db.add_resource(url=f"https://example.com/{i}", collection_id=coll["id"], submitted_by=rocco["id"])
        db.react_to_resource(r["id"], kade["id"], "tap")
    score = db.get_member_credit_score(coll["id"], rocco["id"])
    assert score["submissions"] == 3
    assert score["reactions_received"] == 3
    assert score["total"] == 6


# --- Appeals ---

def test_appeal_ban(db):
    kade = db.create_user("Kade")
    rocco = db.create_user("Rocco")
    coll = db.create_collection("Shared", kade["id"], visibility="shared")
    db.invite_member(coll["id"], kade["id"], rocco["id"])
    db.ban_member(coll["id"], rocco["id"], cascade=False)
    result = db.appeal_ban(coll["id"], rocco["id"])
    assert result is not None
    assert result["status"] == "appealing"
    status = db.get_member_status(coll["id"], rocco["id"])
    assert status["status"] == "appealing"


def test_appeal_only_when_banned(db):
    kade = db.create_user("Kade")
    rocco = db.create_user("Rocco")
    coll = db.create_collection("Shared", kade["id"], visibility="shared")
    db.invite_member(coll["id"], kade["id"], rocco["id"])
    # Can't appeal when active
    result = db.appeal_ban(coll["id"], rocco["id"])
    assert result is None


def test_get_appeals(db):
    kade = db.create_user("Kade")
    rocco = db.create_user("Rocco")
    miles = db.create_user("Miles")
    coll = db.create_collection("Shared", kade["id"], visibility="shared")
    db.invite_member(coll["id"], kade["id"], rocco["id"])
    db.invite_member(coll["id"], kade["id"], miles["id"])
    db.ban_member(coll["id"], rocco["id"], cascade=False)
    db.ban_member(coll["id"], miles["id"], cascade=False)
    db.appeal_ban(coll["id"], rocco["id"])
    db.appeal_ban(coll["id"], miles["id"])
    appeals = db.get_appeals(coll["id"])
    assert len(appeals) == 2


def test_approve_appeal(db):
    kade = db.create_user("Kade")
    rocco = db.create_user("Rocco")
    coll = db.create_collection("Shared", kade["id"], visibility="shared")
    db.invite_member(coll["id"], kade["id"], rocco["id"])
    db.ban_member(coll["id"], rocco["id"], cascade=False)
    db.appeal_ban(coll["id"], rocco["id"])
    result = db.approve_appeal(coll["id"], rocco["id"])
    assert result["status"] == "active"
    assert result["invited_by"] == kade["id"]  # Re-rooted under owner
    # Rocco has access again
    feed = db.get_feed(rocco["id"])
    assert isinstance(feed, list)


def test_deny_appeal(db):
    kade = db.create_user("Kade")
    rocco = db.create_user("Rocco")
    coll = db.create_collection("Shared", kade["id"], visibility="shared")
    db.invite_member(coll["id"], kade["id"], rocco["id"])
    db.ban_member(coll["id"], rocco["id"], cascade=False)
    db.appeal_ban(coll["id"], rocco["id"])
    result = db.deny_appeal(coll["id"], rocco["id"])
    assert result["status"] == "banned"


# --- Routing Manifest ---

# --- Rate Limiting ---

def test_rate_limit_default_config(db):
    kade = db.create_user("Kade")
    inst = db.create_instance("AI", kade["id"])
    config = db.get_rate_limit_config(inst["id"])
    assert config["rate_limit_initial"] == 5
    assert config["rate_limit_growth"] == 2


def test_rate_limit_custom_config(db):
    kade = db.create_user("Kade")
    inst = db.create_instance("AI", kade["id"], rate_limit_initial=10, rate_limit_growth=3)
    config = db.get_rate_limit_config(inst["id"])
    assert config["rate_limit_initial"] == 10
    assert config["rate_limit_growth"] == 3


def test_set_rate_limit(db):
    kade = db.create_user("Kade")
    inst = db.create_instance("AI", kade["id"])
    result = db.set_rate_limit(inst["id"], kade["id"], initial=20, growth=5)
    assert result is not None
    config = db.get_rate_limit_config(inst["id"])
    assert config["rate_limit_initial"] == 20
    assert config["rate_limit_growth"] == 5


def test_set_rate_limit_owner_only(db):
    kade = db.create_user("Kade")
    rocco = db.create_user("Rocco")
    inst = db.create_instance("AI", kade["id"])
    result = db.set_rate_limit(inst["id"], rocco["id"], initial=100)
    assert result is None


def test_rate_limit_check_no_instance(db):
    """No instance = no rate limit (unlimited)."""
    kade = db.create_user("Kade")
    coll = db.create_collection("AI", kade["id"])
    status = db.check_rate_limit(coll["id"], kade["id"])
    assert status["allowed"] is True
    assert status["cap"] == -1


def test_rate_limit_check_under_limit(db):
    kade = db.create_user("Kade")
    inst = db.create_instance("AI", kade["id"], rate_limit_initial=5, rate_limit_growth=0)
    coll = db.create_collection("AI", kade["id"])
    # Add 3 resources — under the limit of 5
    for i in range(3):
        db.add_resource(url=f"https://example.com/{i}", collection_id=coll["id"], submitted_by=kade["id"])
    status = db.check_rate_limit(coll["id"], kade["id"])
    assert status["allowed"] is True
    assert status["current"] == 3
    assert status["cap"] == 5


def test_rate_limit_check_at_limit(db):
    kade = db.create_user("Kade")
    inst = db.create_instance("AI", kade["id"], rate_limit_initial=3, rate_limit_growth=0)
    coll = db.create_collection("AI", kade["id"])
    for i in range(3):
        db.add_resource(url=f"https://example.com/{i}", collection_id=coll["id"], submitted_by=kade["id"])
    status = db.check_rate_limit(coll["id"], kade["id"])
    assert status["allowed"] is False
    assert status["current"] == 3
    assert status["cap"] == 3
    assert status["reason"] == "rate limit exceeded"


def test_rate_limit_not_a_member(db):
    kade = db.create_user("Kade")
    rocco = db.create_user("Rocco")
    coll = db.create_collection("AI", kade["id"])
    status = db.check_rate_limit(coll["id"], rocco["id"])
    assert status["allowed"] is False
    assert status["reason"] == "not a member"


def test_rate_limit_tenure_growth(db):
    """Members who joined earlier get a higher cap."""
    from datetime import datetime, timezone, timedelta
    kade = db.create_user("Kade")
    rocco = db.create_user("Rocco")
    inst = db.create_instance("AI", kade["id"], rate_limit_initial=5, rate_limit_growth=2)
    coll = db.create_collection("AI", kade["id"], visibility="shared")
    db.invite_member(coll["id"], kade["id"], rocco["id"])

    # Manually backdate Rocco's join date to 10 days ago
    ten_days_ago = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    db.conn.execute(
        "UPDATE collection_members SET joined_at = ? WHERE collection_id = ? AND user_id = ?",
        (ten_days_ago, coll["id"], rocco["id"]),
    )
    db.conn.commit()

    status = db.check_rate_limit(coll["id"], rocco["id"])
    # Cap should be 5 + (10 * 2) = 25
    assert status["cap"] == 25
    assert status["days_member"] == 10
    assert status["allowed"] is True


# --- Routing Manifest ---

def test_routing_manifest(db):
    kade = db.create_user("Kade")
    db.create_instance("Food Dugg", kade["id"], topic="Food, restaurants, recipes")
    db.create_instance("AI Dugg", kade["id"], topic="AI, agents, LLMs, machine learning")
    manifest = db.get_routing_manifest(kade["id"])
    assert len(manifest) == 2
    topics = {m["topic"] for m in manifest}
    assert "Food, restaurants, recipes" in topics
    assert "AI, agents, LLMs, machine learning" in topics


def test_routing_manifest_only_subscribed(db):
    kade = db.create_user("Kade")
    rocco = db.create_user("Rocco")
    db.create_instance("Kade's Dugg", kade["id"], topic="Kade stuff")
    db.create_instance("Rocco's Dugg", rocco["id"], topic="Rocco stuff")
    # Kade only sees his own
    manifest = db.get_routing_manifest(kade["id"])
    assert len(manifest) == 1
    assert manifest[0]["topic"] == "Kade stuff"
