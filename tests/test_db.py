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
    from datetime import datetime, timezone, timedelta
    kade = db.create_user("Kade")
    spammer = db.create_user("Spammer")
    middleman = db.create_user("Middleman")
    ghost = db.create_user("Ghost")
    coll = db.create_collection("Shared", kade["id"], visibility="shared")
    db.invite_member(coll["id"], kade["id"], spammer["id"])
    db.invite_member(coll["id"], spammer["id"], middleman["id"])
    db.invite_member(coll["id"], middleman["id"], ghost["id"])
    # Expire ghost's grace period so they're judged by credit score
    expired = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    db.conn.execute(
        "UPDATE collection_members SET grace_expires_at = ? WHERE collection_id = ? AND user_id = ?",
        (expired, coll["id"], ghost["id"]),
    )
    db.conn.commit()
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
    assert score["distinct_human_reactors"] == 1  # Only Kade reacted
    assert score["total"] == 3  # 3 submissions × 1 distinct human reactor


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


def test_agent_appeals_on_behalf_of_human(db):
    """An agent can file an appeal on behalf of its banned parent human."""
    kade = db.create_user("Kade")
    rocco = db.create_user("Rocco")
    rocco_bot = db.create_agent_for_user(rocco["id"])
    coll = db.create_collection("Shared", kade["id"], visibility="shared")
    db.invite_member(coll["id"], kade["id"], rocco["id"])
    db.invite_member(coll["id"], rocco["id"], rocco_bot["id"])
    db.ban_member(coll["id"], rocco["id"], cascade=False)
    assert db.get_member_status(coll["id"], rocco["id"])["status"] == "banned"
    assert db.get_member_status(coll["id"], rocco_bot["id"])["status"] == "banned"
    # Agent appeals on behalf of human
    result = db.appeal_ban(coll["id"], rocco_bot["id"])
    assert result is not None
    assert result["user_id"] == rocco["id"]  # Appeal is for the human
    assert result["appealed_by"] == rocco_bot["id"]  # Filed by the agent
    assert db.get_member_status(coll["id"], rocco["id"])["status"] == "appealing"
    # Approve the appeal — both human and agent should be restored
    approved = db.approve_appeal(coll["id"], rocco["id"])
    assert approved["status"] == "active"
    assert rocco_bot["id"] in approved["agents_unbanned"]
    assert db.get_member_status(coll["id"], rocco["id"])["status"] == "active"
    assert db.get_member_status(coll["id"], rocco_bot["id"])["status"] == "active"


def test_approve_appeal_cascades_to_agents(db):
    """Approving an appeal should also unban the user's agent tokens."""
    kade = db.create_user("Kade")
    rocco = db.create_user("Rocco")
    rocco_bot = db.create_agent_for_user(rocco["id"])
    coll = db.create_collection("Shared", kade["id"], visibility="shared")
    db.invite_member(coll["id"], kade["id"], rocco["id"])
    db.invite_member(coll["id"], rocco["id"], rocco_bot["id"])
    # Ban cascades to agent
    db.ban_member(coll["id"], rocco["id"], cascade=False)
    assert db.get_member_status(coll["id"], rocco["id"])["status"] == "banned"
    assert db.get_member_status(coll["id"], rocco_bot["id"])["status"] == "banned"
    # Appeal and approve
    db.appeal_ban(coll["id"], rocco["id"])
    result = db.approve_appeal(coll["id"], rocco["id"])
    assert result["status"] == "active"
    assert rocco_bot["id"] in result["agents_unbanned"]
    # Both human and agent are active again
    assert db.get_member_status(coll["id"], rocco["id"])["status"] == "active"
    assert db.get_member_status(coll["id"], rocco_bot["id"])["status"] == "active"


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

# --- Publish Queue ---

def test_enqueue_publish_no_remote_instances(db):
    """No instances with endpoint_url = nothing enqueued."""
    kade = db.create_user("Kade")
    coll = db.create_collection("AI", kade["id"])
    db.create_instance("Local", kade["id"])  # No endpoint_url
    res = db.add_resource(url="https://example.com/1", collection_id=coll["id"], submitted_by=kade["id"])
    entries = db.enqueue_publish(res["id"], "public")
    assert len(entries) == 0


def test_enqueue_publish_with_remote(db):
    kade = db.create_user("Kade")
    coll = db.create_collection("AI", kade["id"])
    inst = db.create_instance("Remote", kade["id"])
    db.update_instance(inst["id"], kade["id"], endpoint_url="https://remote.dugg.fyi")
    res = db.add_resource(url="https://example.com/1", collection_id=coll["id"], submitted_by=kade["id"])
    entries = db.enqueue_publish(res["id"], "public")
    assert len(entries) == 1
    assert entries[0]["status"] == "pending"
    assert entries[0]["target_name"] == "public"


def test_publish_queue_status(db):
    kade = db.create_user("Kade")
    coll = db.create_collection("AI", kade["id"])
    inst = db.create_instance("Remote", kade["id"])
    db.update_instance(inst["id"], kade["id"], endpoint_url="https://remote.dugg.fyi")
    res = db.add_resource(url="https://example.com/1", collection_id=coll["id"], submitted_by=kade["id"])
    db.enqueue_publish(res["id"], "public")
    status = db.get_publish_queue_status(kade["id"])
    assert status["pending"] == 1
    assert status["delivered"] == 0


def test_mark_publish_delivered(db):
    kade = db.create_user("Kade")
    coll = db.create_collection("AI", kade["id"])
    inst = db.create_instance("Remote", kade["id"])
    db.update_instance(inst["id"], kade["id"], endpoint_url="https://remote.dugg.fyi")
    res = db.add_resource(url="https://example.com/1", collection_id=coll["id"], submitted_by=kade["id"])
    entries = db.enqueue_publish(res["id"], "public")
    db.mark_publish_delivered(entries[0]["id"])
    status = db.get_publish_queue_status()
    assert status["delivered"] == 1
    assert status["pending"] == 0


def test_mark_publish_retry_with_backoff(db):
    kade = db.create_user("Kade")
    coll = db.create_collection("AI", kade["id"])
    inst = db.create_instance("Remote", kade["id"])
    db.update_instance(inst["id"], kade["id"], endpoint_url="https://remote.dugg.fyi")
    res = db.add_resource(url="https://example.com/1", collection_id=coll["id"], submitted_by=kade["id"])
    entries = db.enqueue_publish(res["id"], "public")
    queue_id = entries[0]["id"]
    db.mark_publish_retry(queue_id, "Connection refused")
    row = db.conn.execute("SELECT * FROM publish_queue WHERE id = ?", (queue_id,)).fetchone()
    assert dict(row)["retry_count"] == 1
    assert dict(row)["status"] == "pending"
    assert dict(row)["last_error"] == "Connection refused"


def test_mark_publish_fails_after_max_retries(db):
    kade = db.create_user("Kade")
    coll = db.create_collection("AI", kade["id"])
    inst = db.create_instance("Remote", kade["id"])
    db.update_instance(inst["id"], kade["id"], endpoint_url="https://remote.dugg.fyi")
    res = db.add_resource(url="https://example.com/1", collection_id=coll["id"], submitted_by=kade["id"])
    entries = db.enqueue_publish(res["id"], "public")
    queue_id = entries[0]["id"]
    # Exhaust all retries
    for i in range(5):
        db.mark_publish_retry(queue_id, f"Error {i}")
    row = db.conn.execute("SELECT * FROM publish_queue WHERE id = ?", (queue_id,)).fetchone()
    assert dict(row)["status"] == "failed"


def test_retry_failed_publishes(db):
    kade = db.create_user("Kade")
    coll = db.create_collection("AI", kade["id"])
    inst = db.create_instance("Remote", kade["id"])
    db.update_instance(inst["id"], kade["id"], endpoint_url="https://remote.dugg.fyi")
    res = db.add_resource(url="https://example.com/1", collection_id=coll["id"], submitted_by=kade["id"])
    entries = db.enqueue_publish(res["id"], "public")
    queue_id = entries[0]["id"]
    for i in range(5):
        db.mark_publish_retry(queue_id, f"Error {i}")
    # Now retry all failed
    count = db.retry_failed_publishes()
    assert count == 1
    status = db.get_publish_queue_status()
    assert status["pending"] == 1
    assert status["failed"] == 0


def test_get_pending_publishes(db):
    kade = db.create_user("Kade")
    coll = db.create_collection("AI", kade["id"])
    inst = db.create_instance("Remote", kade["id"])
    db.update_instance(inst["id"], kade["id"], endpoint_url="https://remote.dugg.fyi")
    res = db.add_resource(url="https://example.com/1", collection_id=coll["id"], submitted_by=kade["id"])
    db.enqueue_publish(res["id"], "public")
    pending = db.get_pending_publishes()
    assert len(pending) == 1
    assert pending[0]["endpoint_url"] == "https://remote.dugg.fyi"


# --- Event Log ---

def test_emit_event(db):
    kade = db.create_user("Kade")
    event = db.emit_event("resource_added", actor_id=kade["id"], payload={"url": "https://example.com"})
    assert event["event_type"] == "resource_added"
    assert event["actor_id"] == kade["id"]
    assert event["payload"]["url"] == "https://example.com"


def test_events_emitted_on_add_resource(db):
    kade = db.create_user("Kade")
    coll = db.create_collection("AI", kade["id"])
    db.add_resource(url="https://example.com/1", collection_id=coll["id"], submitted_by=kade["id"], title="Cool thing")
    events = db.get_events(kade["id"])
    resource_added = [e for e in events if e["event_type"] == "resource_added"]
    assert len(resource_added) >= 1
    assert resource_added[0]["payload"]["url"] == "https://example.com/1"


def test_events_emitted_on_publish(db):
    kade = db.create_user("Kade")
    coll = db.create_collection("AI", kade["id"])
    res = db.add_resource(url="https://example.com/1", collection_id=coll["id"], submitted_by=kade["id"])
    db.publish_resource(res["id"], ["public"])
    events = db.get_events(kade["id"])
    pub_events = [e for e in events if e["event_type"] == "resource_published"]
    assert len(pub_events) >= 1
    assert pub_events[0]["payload"]["target"] == "public"


def test_events_emitted_on_invite(db):
    kade = db.create_user("Kade")
    rocco = db.create_user("Rocco")
    coll = db.create_collection("Shared", kade["id"], visibility="shared")
    db.invite_member(coll["id"], kade["id"], rocco["id"])
    events = db.get_events(kade["id"])
    join_events = [e for e in events if e["event_type"] == "member_joined"]
    assert len(join_events) >= 1


def test_events_emitted_on_ban(db):
    kade = db.create_user("Kade")
    rocco = db.create_user("Rocco")
    coll = db.create_collection("Shared", kade["id"], visibility="shared")
    db.invite_member(coll["id"], kade["id"], rocco["id"])
    db.ban_member(coll["id"], rocco["id"], cascade=False)
    events = db.get_events(kade["id"])
    ban_events = [e for e in events if e["event_type"] == "member_banned"]
    assert len(ban_events) >= 1


def test_events_filtered_by_type(db):
    kade = db.create_user("Kade")
    coll = db.create_collection("AI", kade["id"])
    db.add_resource(url="https://example.com/1", collection_id=coll["id"], submitted_by=kade["id"])
    res = db.add_resource(url="https://example.com/2", collection_id=coll["id"], submitted_by=kade["id"])
    db.publish_resource(res["id"], ["public"])
    events = db.get_events(kade["id"], event_types=["resource_published"])
    assert all(e["event_type"] == "resource_published" for e in events)


def test_events_filtered_by_since(db):
    from datetime import datetime, timezone, timedelta
    kade = db.create_user("Kade")
    coll = db.create_collection("AI", kade["id"])
    db.add_resource(url="https://example.com/1", collection_id=coll["id"], submitted_by=kade["id"])
    # All events are from "now", so filtering since yesterday should include them
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    events = db.get_events(kade["id"], since=yesterday)
    assert len(events) >= 1
    # Filtering since tomorrow should exclude them
    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    events = db.get_events(kade["id"], since=tomorrow)
    assert len(events) == 0


# --- Webhook Subscriptions ---

def test_subscribe_webhook(db):
    kade = db.create_user("Kade")
    inst = db.create_instance("AI", kade["id"])
    result = db.subscribe_webhook(inst["id"], kade["id"], "https://hooks.example.com/dugg")
    assert result["callback_url"] == "https://hooks.example.com/dugg"
    assert result["status"] == "active"


def test_subscribe_webhook_with_event_filter(db):
    kade = db.create_user("Kade")
    inst = db.create_instance("AI", kade["id"])
    result = db.subscribe_webhook(inst["id"], kade["id"], "https://hooks.example.com/dugg",
                                   event_types=["resource_published"])
    assert result["event_types"] == ["resource_published"]


def test_list_webhooks(db):
    kade = db.create_user("Kade")
    inst = db.create_instance("AI", kade["id"])
    db.subscribe_webhook(inst["id"], kade["id"], "https://hooks.example.com/a")
    db.subscribe_webhook(inst["id"], kade["id"], "https://hooks.example.com/b")
    webhooks = db.list_webhooks(kade["id"])
    assert len(webhooks) == 2


def test_unsubscribe_webhook(db):
    kade = db.create_user("Kade")
    inst = db.create_instance("AI", kade["id"])
    result = db.subscribe_webhook(inst["id"], kade["id"], "https://hooks.example.com/dugg")
    deleted = db.unsubscribe_webhook(result["id"], kade["id"])
    assert deleted is True
    webhooks = db.list_webhooks(kade["id"])
    assert len(webhooks) == 0


def test_unsubscribe_webhook_wrong_user(db):
    kade = db.create_user("Kade")
    rocco = db.create_user("Rocco")
    inst = db.create_instance("AI", kade["id"])
    result = db.subscribe_webhook(inst["id"], kade["id"], "https://hooks.example.com/dugg")
    deleted = db.unsubscribe_webhook(result["id"], rocco["id"])
    assert deleted is False


def test_get_webhooks_for_event(db):
    kade = db.create_user("Kade")
    inst = db.create_instance("AI", kade["id"])
    db.subscribe_webhook(inst["id"], kade["id"], "https://hooks.example.com/all")  # All events
    db.subscribe_webhook(inst["id"], kade["id"], "https://hooks.example.com/pub",
                          event_types=["resource_published"])
    # resource_published should match both
    matches = db.get_webhooks_for_event("resource_published", instance_id=inst["id"])
    assert len(matches) == 2
    # resource_added should only match the "all" webhook
    matches = db.get_webhooks_for_event("resource_added", instance_id=inst["id"])
    assert len(matches) == 1


def test_webhook_failure_tracking(db):
    kade = db.create_user("Kade")
    inst = db.create_instance("AI", kade["id"])
    result = db.subscribe_webhook(inst["id"], kade["id"], "https://hooks.example.com/dugg")
    # Fail 4 times — still active
    for _ in range(4):
        db.mark_webhook_failure(result["id"])
    webhooks = db.list_webhooks(kade["id"])
    assert webhooks[0]["status"] == "active"
    assert webhooks[0]["failure_count"] == 4
    # 5th failure — auto-paused
    db.mark_webhook_failure(result["id"])
    webhooks = db.list_webhooks(kade["id"])
    assert webhooks[0]["status"] == "failed"


def test_webhook_success_resets_failures(db):
    kade = db.create_user("Kade")
    inst = db.create_instance("AI", kade["id"])
    result = db.subscribe_webhook(inst["id"], kade["id"], "https://hooks.example.com/dugg")
    db.mark_webhook_failure(result["id"])
    db.mark_webhook_failure(result["id"])
    db.mark_webhook_success(result["id"])
    webhooks = db.list_webhooks(kade["id"])
    assert webhooks[0]["failure_count"] == 0


# --- Inbound Publish (Remote Ingest) ---

def test_ingest_remote_publish(db):
    kade = db.create_user("Kade")
    coll = db.create_collection("Inbox", kade["id"])
    result = db.ingest_remote_publish(
        {"url": "https://example.com/cool", "title": "Cool thing", "source_type": "article", "tags": ["ai"]},
        source_instance_id="remote123",
        target_collection_id=coll["id"],
    )
    assert result["status"] == "ingested"
    assert result["title"] == "Cool thing"
    # Verify resource exists
    resource = db.get_resource(result["id"])
    assert resource["url"] == "https://example.com/cool"
    assert len(resource["tags"]) == 1


def test_ingest_remote_deduplicates(db):
    kade = db.create_user("Kade")
    coll = db.create_collection("Inbox", kade["id"])
    db.ingest_remote_publish(
        {"url": "https://example.com/cool", "title": "Cool thing"},
        source_instance_id="remote123", target_collection_id=coll["id"],
    )
    result = db.ingest_remote_publish(
        {"url": "https://example.com/cool", "title": "Same thing"},
        source_instance_id="remote123", target_collection_id=coll["id"],
    )
    assert result["status"] == "duplicate"


def test_ingest_tracks_source_instance(db):
    kade = db.create_user("Kade")
    coll = db.create_collection("Inbox", kade["id"])
    result = db.ingest_remote_publish(
        {"url": "https://example.com/cool", "title": "Cool thing"},
        source_instance_id="remote123", target_collection_id=coll["id"],
    )
    resource = db.get_resource(result["id"])
    import json
    meta = json.loads(resource["raw_metadata"])
    assert meta["_source_instance"] == "remote123"


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


# --- Reaction Events ---

def test_reaction_emits_event(db):
    kade = db.create_user("Kade")
    rocco = db.create_user("Rocco")
    coll = db.create_collection("AI", kade["id"], visibility="shared")
    db.add_collection_member(coll["id"], rocco["id"])
    res = db.add_resource(url="https://example.com/1", collection_id=coll["id"], submitted_by=kade["id"], title="Cool thing")
    db.react_to_resource(res["id"], rocco["id"], "tap")
    events = db.get_events(kade["id"])
    reaction_events = [e for e in events if e["event_type"] == "reaction_added"]
    assert len(reaction_events) == 1
    assert reaction_events[0]["actor_id"] == rocco["id"]
    assert reaction_events[0]["payload"]["resource_id"] == res["id"]
    assert reaction_events[0]["payload"]["reaction_type"] == "tap"
    assert reaction_events[0]["payload"]["resource_owner_id"] == kade["id"]


def test_duplicate_reaction_no_event(db):
    kade = db.create_user("Kade")
    rocco = db.create_user("Rocco")
    coll = db.create_collection("AI", kade["id"], visibility="shared")
    db.add_collection_member(coll["id"], rocco["id"])
    res = db.add_resource(url="https://example.com/1", collection_id=coll["id"], submitted_by=kade["id"])
    db.react_to_resource(res["id"], rocco["id"], "tap")
    db.react_to_resource(res["id"], rocco["id"], "tap")  # duplicate
    events = db.get_events(kade["id"])
    reaction_events = [e for e in events if e["event_type"] == "reaction_added"]
    assert len(reaction_events) == 1  # only one event, not two


# --- User Cursors ---

def test_cursor_starts_none(db):
    kade = db.create_user("Kade")
    assert db.get_cursor(kade["id"]) is None


def test_update_and_get_cursor(db):
    kade = db.create_user("Kade")
    result = db.update_cursor(kade["id"])
    assert result["cursor_type"] == "events"
    assert result["last_seen_at"] is not None
    cursor = db.get_cursor(kade["id"])
    assert cursor == result["last_seen_at"]


def test_cursor_advances(db):
    kade = db.create_user("Kade")
    db.update_cursor(kade["id"], last_seen_at="2026-01-01T00:00:00+00:00")
    first = db.get_cursor(kade["id"])
    db.update_cursor(kade["id"], last_seen_at="2026-06-01T00:00:00+00:00")
    second = db.get_cursor(kade["id"])
    assert second > first


def test_get_unseen_events(db):
    kade = db.create_user("Kade")
    rocco = db.create_user("Rocco")
    coll = db.create_collection("AI", kade["id"], visibility="shared")
    db.add_collection_member(coll["id"], rocco["id"])

    # Add a resource — this emits resource_added
    res = db.add_resource(url="https://example.com/1", collection_id=coll["id"], submitted_by=kade["id"], title="First")

    # Kade catches up and marks seen
    unseen = db.get_unseen_events(kade["id"])
    assert len(unseen) >= 1
    db.update_cursor(kade["id"])

    # Add another resource — new event after cursor
    db.add_resource(url="https://example.com/2", collection_id=coll["id"], submitted_by=kade["id"], title="Second")

    # Only the new event should be unseen
    unseen = db.get_unseen_events(kade["id"])
    assert len(unseen) >= 1
    assert all(e["payload"].get("url") != "https://example.com/1" for e in unseen)


def test_unseen_events_oldest_first(db):
    kade = db.create_user("Kade")
    coll = db.create_collection("AI", kade["id"])
    db.add_resource(url="https://example.com/1", collection_id=coll["id"], submitted_by=kade["id"], title="First")
    db.add_resource(url="https://example.com/2", collection_id=coll["id"], submitted_by=kade["id"], title="Second")
    unseen = db.get_unseen_events(kade["id"], oldest_first=True)
    assert len(unseen) >= 2
    # Oldest first means first event's timestamp <= last event's timestamp
    assert unseen[0]["created_at"] <= unseen[-1]["created_at"]


# --- User Agents ---


def test_create_agent_for_user(db):
    kade = db.create_user("Kade")
    agent = db.create_agent_for_user(kade["id"])
    assert agent["name"] == "Kade's agent"
    assert agent["api_key"].startswith("dugg_")
    assert agent["id"] != kade["id"]
    agents = db.get_agents_for_user(kade["id"])
    assert len(agents) == 1
    assert agents[0]["id"] == agent["id"]


def test_create_agent_custom_name(db):
    kade = db.create_user("Kade")
    agent = db.create_agent_for_user(kade["id"], agent_name="Kade-bot")
    assert agent["name"] == "Kade-bot"


def test_get_parent_user(db):
    kade = db.create_user("Kade")
    agent = db.create_agent_for_user(kade["id"])
    parent = db.get_parent_user(agent["id"])
    assert parent is not None
    assert parent["id"] == kade["id"]


def test_get_parent_user_returns_none_for_regular_user(db):
    kade = db.create_user("Kade")
    assert db.get_parent_user(kade["id"]) is None


def test_redeem_invite_creates_agent(db):
    kade = db.create_user("Kade")
    invite = db.create_invite_token(kade["id"], name_hint="Rocco")
    result = db.redeem_invite_token(invite["token"], "Rocco")
    assert result is not None
    assert "agent" in result
    agent = result["agent"]
    assert agent["api_key"].startswith("dugg_")
    assert agent["api_key"] != result["user"]["api_key"]
    # Agent is linked to the new user
    parent = db.get_parent_user(agent["id"])
    assert parent["id"] == result["user"]["id"]


def test_ban_cascades_to_agents(db):
    kade = db.create_user("Kade")
    rocco = db.create_user("Rocco")
    rocco_bot = db.create_agent_for_user(rocco["id"])
    coll = db.create_collection("Shared", kade["id"], visibility="shared")
    db.invite_member(coll["id"], kade["id"], rocco["id"])
    # Also add the agent as a member
    db.invite_member(coll["id"], rocco["id"], rocco_bot["id"])
    # Ban Rocco — agent should get banned too
    result = db.ban_member(coll["id"], rocco["id"], cascade=False)
    assert rocco["id"] in result["banned"]
    assert rocco_bot["id"] in result["banned"]
    status = db.get_member_status(coll["id"], rocco_bot["id"])
    assert status["status"] == "banned"


def test_ban_cascade_catches_agents_in_tree(db):
    """When banning with cascade, agents of depth-1 pruned users also get banned."""
    kade = db.create_user("Kade")
    spammer = db.create_user("Spammer")
    spammer_bot = db.create_agent_for_user(spammer["id"])
    victim = db.create_user("Victim")
    victim_bot = db.create_agent_for_user(victim["id"])
    coll = db.create_collection("Shared", kade["id"], visibility="shared")
    db.invite_member(coll["id"], kade["id"], spammer["id"])
    db.invite_member(coll["id"], spammer["id"], spammer_bot["id"])
    db.invite_member(coll["id"], spammer["id"], victim["id"])
    db.invite_member(coll["id"], victim["id"], victim_bot["id"])
    result = db.ban_member(coll["id"], spammer["id"], cascade=True)
    # Spammer, victim (depth 1), and both their bots should be banned
    assert spammer["id"] in result["banned"]
    assert victim["id"] in result["banned"]
    assert spammer_bot["id"] in result["banned"]
    assert victim_bot["id"] in result["banned"]


# --- Invite Tree: Cycle Detection & Depth Cap ---

def test_invite_tree_cycle_detection(db):
    """Circular invites should not cause infinite recursion."""
    kade = db.create_user("Kade")
    alice = db.create_user("Alice")
    bob = db.create_user("Bob")
    coll = db.create_collection("Shared", kade["id"], visibility="shared")
    db.invite_member(coll["id"], kade["id"], alice["id"])
    db.invite_member(coll["id"], alice["id"], bob["id"])
    # Manually create a cycle: bob invited_by -> alice, alice invited_by -> bob
    db.conn.execute(
        "UPDATE collection_members SET invited_by = ? WHERE collection_id = ? AND user_id = ?",
        (bob["id"], coll["id"], alice["id"]),
    )
    db.conn.commit()
    # Should terminate without infinite loop
    tree = db.get_invite_tree(coll["id"], kade["id"])
    # The tree should contain members but not loop infinitely
    assert isinstance(tree, list)


def test_invite_tree_depth_cap(db):
    """Tree should stop at MAX_INVITE_DEPTH."""
    from dugg.db import MAX_INVITE_DEPTH
    kade = db.create_user("Kade")
    coll = db.create_collection("Deep", kade["id"], visibility="shared")
    prev_id = kade["id"]
    users = []
    # Create a chain of 20 invites
    for i in range(20):
        u = db.create_user(f"User{i}")
        users.append(u)
        db.invite_member(coll["id"], prev_id, u["id"])
        prev_id = u["id"]
    tree = db.get_invite_tree(coll["id"], kade["id"])
    max_depth = max(m["depth"] for m in tree)
    assert max_depth <= MAX_INVITE_DEPTH


def test_ip_duplicate_check(db):
    """IP addresses should be tracked and flaggable."""
    kade = db.create_user("Kade")
    alice = db.create_user("Alice")
    bob = db.create_user("Bob")
    coll = db.create_collection("Shared", kade["id"], visibility="shared")
    db.invite_member(coll["id"], kade["id"], alice["id"], ip_address="1.2.3.4")
    # Same IP for different user
    dups = db.check_ip_duplicate(coll["id"], "1.2.3.4")
    assert len(dups) == 1
    assert dups[0]["user_id"] == alice["id"]


# --- Owner Ban Protection ---

def test_owner_cannot_be_banned(db):
    """Banning the collection owner should be rejected."""
    kade = db.create_user("Kade")
    coll = db.create_collection("Shared", kade["id"], visibility="shared")
    result = db.ban_member(coll["id"], kade["id"])
    assert result.get("error") is not None
    assert len(result["banned"]) == 0
    # Owner is still active
    status = db.get_member_status(coll["id"], kade["id"])
    assert status["status"] == "active"


# --- Succession ---

def test_set_and_trigger_succession(db):
    """Succession should transfer ownership."""
    kade = db.create_user("Kade")
    rocco = db.create_user("Rocco")
    inst = db.create_instance("AI", kade["id"])
    db.subscribe_to_instance(inst["id"], rocco["id"])
    # Set successor
    result = db.set_successor(inst["id"], kade["id"], rocco["id"])
    assert result is not None
    assert result["successor_id"] == rocco["id"]
    # Trigger succession
    result = db.trigger_succession(inst["id"])
    assert result["old_owner"] == kade["id"]
    assert result["new_owner"] == rocco["id"]
    # Verify instance ownership changed
    inst = db.get_instance(result["instance_id"])
    assert inst["owner_id"] == rocco["id"]
    assert inst["successor_id"] is None


# --- Credit Score: Multiplicative Formula ---

def test_credit_score_multiplicative(db):
    """Score = submissions * distinct_human_reactors."""
    kade = db.create_user("Kade")
    rocco = db.create_user("Rocco")
    miles = db.create_user("Miles")
    coll = db.create_collection("Shared", kade["id"], visibility="shared")
    db.invite_member(coll["id"], kade["id"], rocco["id"])
    db.invite_member(coll["id"], kade["id"], miles["id"])
    # Rocco submits 3 resources, Kade and Miles react
    for i in range(3):
        r = db.add_resource(url=f"https://example.com/r{i}", collection_id=coll["id"], submitted_by=rocco["id"])
        db.react_to_resource(r["id"], kade["id"], "tap")
        db.react_to_resource(r["id"], miles["id"], "star")
    score = db.get_member_credit_score(coll["id"], rocco["id"])
    assert score["submissions"] == 3
    assert score["distinct_human_reactors"] == 2
    assert score["total"] == 6  # 3 * 2


def test_credit_score_excludes_agent_reactions(db):
    """Agent reactions should not count toward credit score."""
    kade = db.create_user("Kade")
    rocco = db.create_user("Rocco")
    rocco_bot = db.create_agent_for_user(rocco["id"])
    coll = db.create_collection("Shared", kade["id"], visibility="shared")
    db.invite_member(coll["id"], kade["id"], rocco["id"])
    db.invite_member(coll["id"], rocco["id"], rocco_bot["id"])
    # Kade submits, rocco (human) reacts, rocco_bot (agent) also reacts
    r = db.add_resource(url="https://example.com/1", collection_id=coll["id"], submitted_by=kade["id"])
    db.react_to_resource(r["id"], rocco["id"], "tap")
    db.react_to_resource(r["id"], rocco_bot["id"], "tap")
    score = db.get_member_credit_score(coll["id"], kade["id"])
    # Only rocco counts as human, not rocco_bot
    assert score["distinct_human_reactors"] == 1
    assert score["total"] == 1  # 1 submission * 1 human reactor


# --- Grace Period ---

def test_grace_period_survives_ban_cascade(db):
    """Members at depth 2+ in their grace period survive ban cascades."""
    from datetime import datetime, timezone, timedelta
    kade = db.create_user("Kade")
    spammer = db.create_user("Spammer")
    middleman = db.create_user("Middleman")
    newbie = db.create_user("Newbie")
    coll = db.create_collection("Shared", kade["id"], visibility="shared")
    db.invite_member(coll["id"], kade["id"], spammer["id"])
    db.invite_member(coll["id"], spammer["id"], middleman["id"])
    db.invite_member(coll["id"], middleman["id"], newbie["id"])
    # Newbie is at depth 2 from spammer and in grace period (just joined)
    # Without grace period protection, newbie would need credit_threshold to survive
    # With grace period, newbie survives regardless of score
    result = db.ban_member(coll["id"], spammer["id"], cascade=True, credit_threshold=5)
    # Middleman at depth 1 = hard ban
    assert middleman["id"] in result["banned"]
    # Newbie at depth 2 in grace period = survives
    assert newbie["id"] in result["survived"]
    status = db.get_member_status(coll["id"], newbie["id"])
    assert status["status"] == "active"


# --- FIFO Publish Queue ---

def test_ban_cancels_pending_publishes(db):
    """Banning a user should cancel their pending publishes."""
    kade = db.create_user("Kade")
    rocco = db.create_user("Rocco")
    coll = db.create_collection("Shared", kade["id"], visibility="shared")
    inst = db.create_instance("Remote", kade["id"])
    db.update_instance(inst["id"], kade["id"], endpoint_url="https://remote.dugg.fyi")
    db.invite_member(coll["id"], kade["id"], rocco["id"])
    res = db.add_resource(url="https://example.com/1", collection_id=coll["id"], submitted_by=rocco["id"])
    db.enqueue_publish(res["id"], "public")
    # Ban Rocco
    db.ban_member(coll["id"], rocco["id"], cascade=False)
    # Pending publishes should be cancelled
    row = db.conn.execute(
        "SELECT status FROM publish_queue WHERE resource_id = ?", (res["id"],)
    ).fetchone()
    if row:
        assert dict(row)["status"] == "cancelled"


# --- 60-Day Egress Timeout ---

def test_egress_timeout_stale_members(db):
    """Members with old last_seen_at should be returned by get_stale_members."""
    from datetime import datetime, timezone, timedelta
    kade = db.create_user("Kade")
    rocco = db.create_user("Rocco")
    coll = db.create_collection("Shared", kade["id"], visibility="shared")
    db.invite_member(coll["id"], kade["id"], rocco["id"])
    # Set last_seen_at to 90 days ago
    old_date = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
    db.conn.execute(
        "UPDATE collection_members SET last_seen_at = ? WHERE collection_id = ? AND user_id = ?",
        (old_date, coll["id"], rocco["id"]),
    )
    db.conn.commit()
    stale = db.get_stale_members()
    stale_ids = [m["user_id"] for m in stale]
    assert rocco["id"] in stale_ids


# --- URL Validation ---

def test_url_validation_blocked_scheme():
    """javascript: and data: URLs should be rejected."""
    from dugg.enrichment import validate_url
    valid, reason = validate_url("javascript:alert(1)")
    assert not valid
    valid, reason = validate_url("data:text/html,<h1>hi</h1>")
    assert not valid
    valid, reason = validate_url("file:///etc/passwd")
    assert not valid


def test_url_validation_accepts_http():
    """HTTP and HTTPS URLs should be accepted."""
    from dugg.enrichment import validate_url
    valid, _ = validate_url("https://example.com")
    assert valid
    valid, _ = validate_url("http://example.com/path?q=1")
    assert valid


def test_url_sanitization_strips_tracking():
    """Tracking parameters should be stripped."""
    from dugg.enrichment import sanitize_url
    result = sanitize_url("https://example.com/page?utm_source=twitter&utm_medium=social&important=yes")
    assert "utm_source" not in result
    assert "utm_medium" not in result
    assert "important=yes" in result


# --- Inactive Member Pruning ---

def test_prune_inactive_members(db):
    """Members past grace period with zero activity should be prunable."""
    from datetime import datetime, timezone, timedelta
    kade = db.create_user("Kade")
    lurker = db.create_user("Lurker")
    coll = db.create_collection("Shared", kade["id"], visibility="shared")
    db.invite_member(coll["id"], kade["id"], lurker["id"])
    # Expire the grace period
    expired = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    db.conn.execute(
        "UPDATE collection_members SET grace_expires_at = ? WHERE collection_id = ? AND user_id = ?",
        (expired, coll["id"], lurker["id"]),
    )
    db.conn.commit()
    # Lurker has zero submissions and zero reactions
    inactive = db.get_inactive_members(coll["id"])
    assert len(inactive) == 1
    assert inactive[0]["user_id"] == lurker["id"]
    # Prune them
    result = db.prune_inactive_members(coll["id"])
    assert lurker["id"] in result["pruned"]
    status = db.get_member_status(coll["id"], lurker["id"])
    assert status["status"] == "banned"
