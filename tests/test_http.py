"""Tests for the Dugg HTTP/SSE transport layer."""

import json
import tempfile
from pathlib import Path

import pytest
import httpx

from starlette.testclient import TestClient

from dugg.db import DuggDB
from dugg.http import create_app


@pytest.fixture
def db_path():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir) / "test.db"


@pytest.fixture
def db(db_path):
    d = DuggDB(db_path)
    yield d
    d.close()


@pytest.fixture
def user(db):
    return db.create_user("TestUser")


@pytest.fixture
def client(db_path, db, user):
    """Test client with a pre-initialized database and user."""
    # Close the fixture db so the app can open its own connection
    db.close()
    import os
    os.environ["DUGG_DB_PATH"] = str(db_path)
    # Reset server.py's global db so it picks up the new path
    import dugg.server as srv
    srv.db = None
    app = create_app(db_path=db_path)
    with TestClient(app) as c:
        yield c, user
    srv.db = None


# --- Health ---

def test_health(client):
    c, user = client
    resp = c.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["db"] == "connected"
    assert data["transport"] == "http+sse"


# --- Ingest ---

def test_ingest_requires_auth(client):
    c, user = client
    resp = c.post("/ingest", json={
        "resource": {"url": "https://example.com/article"},
        "source_instance_id": "remote1",
    })
    assert resp.status_code == 401


def test_ingest_rejects_invalid_key(client):
    c, user = client
    resp = c.post("/ingest", json={
        "resource": {"url": "https://example.com/article"},
        "source_instance_id": "remote1",
    }, headers={"X-Dugg-Key": "bad_key_123"})
    assert resp.status_code == 401


def test_ingest_missing_url(client):
    c, user = client
    resp = c.post("/ingest", json={
        "resource": {},
        "source_instance_id": "remote1",
    }, headers={"X-Dugg-Key": user["api_key"]})
    assert resp.status_code == 400
    assert "url" in resp.json()["error"].lower()


def test_ingest_missing_source_instance(client):
    c, user = client
    resp = c.post("/ingest", json={
        "resource": {"url": "https://example.com/article"},
    }, headers={"X-Dugg-Key": user["api_key"]})
    assert resp.status_code == 400
    assert "source_instance_id" in resp.json()["error"]


def test_ingest_success(client):
    c, user = client
    resp = c.post("/ingest", json={
        "resource": {
            "url": "https://example.com/cool-article",
            "title": "Cool Article",
            "source_type": "article",
        },
        "source_instance_id": "remote123",
        "target": "public",
    }, headers={"X-Dugg-Key": user["api_key"]})
    assert resp.status_code == 201
    data = resp.json()
    assert data["status"] == "ingested"
    assert data["id"]
    assert data["source_instance_id"] == "remote123"


def test_ingest_dedup(client):
    c, user = client
    headers = {"X-Dugg-Key": user["api_key"]}
    payload = {
        "resource": {"url": "https://example.com/same-url", "title": "First"},
        "source_instance_id": "remote1",
    }
    resp1 = c.post("/ingest", json=payload, headers=headers)
    assert resp1.status_code == 201

    resp2 = c.post("/ingest", json=payload, headers=headers)
    assert resp2.status_code == 200
    assert resp2.json()["status"] == "duplicate"


def test_ingest_dedup_preserves_foreign_note_in_feed(client):
    """Cross-server duplicate ingest surfaces the foreign note in the HTML feed."""
    c, user = client
    headers = {"X-Dugg-Key": user["api_key"]}
    first = {
        "resource": {"url": "https://example.com/collision", "title": "Collision page",
                     "note": "original note"},
        "source_instance_id": "remoteA",
        "source_server": "https://a.example.com",
    }
    assert c.post("/ingest", json=first, headers=headers).status_code == 201
    second = {
        "resource": {"url": "https://example.com/collision", "title": "Collision page",
                     "note": "rocco's take", "submitter_name": "Remote Rocco"},
        "source_instance_id": "remoteB",
        "source_server": "https://b.example.com",
    }
    resp2 = c.post("/ingest", json=second, headers=headers)
    assert resp2.status_code == 200
    assert resp2.json()["status"] == "duplicate"

    feed = c.get(f"/feed/{user['api_key']}")
    assert feed.status_code == 200
    body = feed.text
    # Both foreign notes should render as siblings in the feed
    assert "original note" in body
    assert "rocco's take" in body


def test_ingest_invalid_json(client):
    c, user = client
    resp = c.post("/ingest", content=b"not json",
                  headers={"X-Dugg-Key": user["api_key"], "Content-Type": "application/json"})
    assert resp.status_code == 400


# --- Tool dispatch ---

def test_tool_dispatch_requires_auth(client):
    c, user = client
    resp = c.post("/tools/dugg_collections", json={})
    assert resp.status_code == 401


def test_tool_dispatch_collections(client):
    c, user = client
    headers = {"X-Dugg-Key": user["api_key"]}
    resp = c.post("/tools/dugg_collections", json={}, headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["tool"] == "dugg_collections"
    assert "result" in data


def test_tool_dispatch_create_collection(client):
    c, user = client
    headers = {"X-Dugg-Key": user["api_key"]}
    resp = c.post("/tools/dugg_create_collection", json={
        "name": "HTTP Test Collection",
        "description": "Created via HTTP transport",
    }, headers=headers)
    assert resp.status_code == 200
    assert "HTTP Test Collection" in resp.json()["result"]


def test_tool_dispatch_search(client):
    c, user = client
    headers = {"X-Dugg-Key": user["api_key"]}
    resp = c.post("/tools/dugg_search", json={"query": "test"}, headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["tool"] == "dugg_search"


def test_tool_dispatch_create_user(client):
    c, user = client
    headers = {"X-Dugg-Key": user["api_key"]}
    resp = c.post("/tools/dugg_create_user", json={"name": "NewUser"}, headers=headers)
    assert resp.status_code == 200
    assert "NewUser" in resp.json()["result"]
    assert "User key:" in resp.json()["result"]
    assert "Agent key:" in resp.json()["result"]


def test_tool_dispatch_feed(client):
    c, user = client
    headers = {"X-Dugg-Key": user["api_key"]}
    resp = c.post("/tools/dugg_feed", json={}, headers=headers)
    assert resp.status_code == 200


# --- Events stream ---

def test_events_stream_requires_auth(client):
    c, user = client
    resp = c.get("/events/stream")
    assert resp.status_code == 401


# --- SSE endpoint exists ---

def test_messages_endpoint_rejects_get(client):
    c, user = client
    resp = c.get("/messages")
    assert resp.status_code == 405  # Method not allowed — POST only


# --- Welcome tool via HTTP ---

def test_tool_dispatch_welcome(client):
    c, user = client
    resp = c.post("/tools/dugg_welcome", json={},
                  headers={"X-Dugg-Key": user["api_key"]})
    assert resp.status_code == 200
    data = resp.json()
    assert "Welcome to Dugg" in data["result"]
    assert user["name"] in data["result"]


# --- Compact format ---

def test_tool_dispatch_compact_format(client):
    c, user = client
    resp = c.post("/tools/dugg_welcome", json={},
                  headers={"X-Dugg-Key": user["api_key"], "X-Dugg-Format": "compact"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["format"] == "compact"
    # Compact mode strips blank lines
    lines = data["result"].split("\n")
    assert all(ln.strip() for ln in lines)


def test_tool_dispatch_rich_format_default(client):
    c, user = client
    resp = c.post("/tools/dugg_welcome", json={},
                  headers={"X-Dugg-Key": user["api_key"]})
    assert resp.status_code == 200
    data = resp.json()
    assert data["format"] == "rich"


# --- Invite Flow (HTTP) ---

def test_invite_page_invalid_token(client):
    c, user = client
    resp = c.get("/invite/nonexistent-token")
    assert resp.status_code == 404
    assert "Invalid invite" in resp.text


def test_invite_page_invalid_token_json(client):
    c, user = client
    resp = c.get("/invite/nonexistent-token", headers={"Accept": "application/json"})
    assert resp.status_code == 404
    data = resp.json()
    assert data["error"] == "Invalid invite token"


def test_invite_page_html(client):
    c, user = client
    db = DuggDB(Path(c.app.state._db_path) if hasattr(c.app.state, '_db_path') else None)
    # Reopen the DB to create an invite
    import os
    db_path = os.environ.get("DUGG_DB_PATH")
    db = DuggDB(Path(db_path))
    invite = db.create_invite_token(user["id"], name_hint="Rocco")
    db.close()
    resp = c.get(f"/invite/{invite['token']}")
    assert resp.status_code == 200
    assert "Rocco" in resp.text
    assert "Join" in resp.text


def test_invite_page_json(client):
    c, user = client
    import os
    db = DuggDB(Path(os.environ["DUGG_DB_PATH"]))
    invite = db.create_invite_token(user["id"], name_hint="Rocco")
    db.close()
    resp = c.get(f"/invite/{invite['token']}", headers={"Accept": "application/json"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "pending"
    assert data["invite"]["invited_by"] == "TestUser"
    assert data["invite"]["name_hint"] == "Rocco"
    assert "redeem" in data
    assert data["redeem"]["method"] == "POST"
    assert "after_redeem" in data
    assert data["after_redeem"]["first_call"].startswith("dugg_welcome")
    assert "partner_guide" in data["after_redeem"]


def test_invite_redeem_html(client):
    c, user = client
    import os
    db = DuggDB(Path(os.environ["DUGG_DB_PATH"]))
    invite = db.create_invite_token(user["id"], name_hint="Rocco")
    db.close()
    resp = c.post(f"/invite/{invite['token']}/redeem", data={"name": "Rocco"})
    assert resp.status_code == 200
    assert "You're in" in resp.text
    assert "Rocco" in resp.text
    assert "dugg_" in resp.text  # API keys should be visible


def test_invite_redeem_json(client):
    c, user = client
    import os
    db = DuggDB(Path(os.environ["DUGG_DB_PATH"]))
    invite = db.create_invite_token(user["id"], name_hint="Rocco")
    db.close()
    resp = c.post(f"/invite/{invite['token']}/redeem",
                  json={"name": "Rocco"},
                  headers={"Content-Type": "application/json"})
    assert resp.status_code == 201
    data = resp.json()
    assert data["status"] == "redeemed"
    assert data["user"]["name"] == "Rocco"
    assert "api_key" in data["user"]
    assert "api_key" in data["agent"]
    assert data["user"]["api_key"] != data["agent"]["api_key"]
    assert "endpoints" in data
    assert "quickstart" in data
    assert len(data["quickstart"]) == 2


def test_invite_redeem_already_used(client):
    c, user = client
    import os
    db = DuggDB(Path(os.environ["DUGG_DB_PATH"]))
    invite = db.create_invite_token(user["id"], name_hint="Rocco")
    db.close()
    # First redemption
    c.post(f"/invite/{invite['token']}/redeem", data={"name": "Rocco"})
    # Second attempt
    resp = c.post(f"/invite/{invite['token']}/redeem", data={"name": "Someone Else"})
    assert resp.status_code == 400
    assert "invalid" in resp.text.lower() or "expired" in resp.text.lower() or "already" in resp.text.lower()


def test_invite_redeem_already_used_json(client):
    c, user = client
    import os
    db = DuggDB(Path(os.environ["DUGG_DB_PATH"]))
    invite = db.create_invite_token(user["id"], name_hint="Rocco")
    db.close()
    c.post(f"/invite/{invite['token']}/redeem",
           json={"name": "Rocco"},
           headers={"Content-Type": "application/json"})
    resp = c.post(f"/invite/{invite['token']}/redeem",
                  json={"name": "Someone"},
                  headers={"Content-Type": "application/json"})
    assert resp.status_code == 400
    data = resp.json()
    assert "error" in data


def test_invite_expired_token(client):
    c, user = client
    import os
    db = DuggDB(Path(os.environ["DUGG_DB_PATH"]))
    invite = db.create_invite_token(user["id"], name_hint="Rocco", expires_hours=0)
    db.close()
    import time
    time.sleep(0.1)
    resp = c.get(f"/invite/{invite['token']}")
    assert resp.status_code == 410
    assert "expired" in resp.text.lower()


def test_invite_page_shows_keys_before_onboarding(client):
    """After redemption, invite URL shows keys until user visits their feed."""
    c, user = client
    import os
    db = DuggDB(Path(os.environ["DUGG_DB_PATH"]))
    invite = db.create_invite_token(user["id"], name_hint="Rocco")
    db.close()
    # Redeem
    resp = c.post(f"/invite/{invite['token']}/redeem", data={"name": "Rocco"})
    assert resp.status_code == 200
    # Visit invite page again — should show keys (not "already redeemed")
    resp = c.get(f"/invite/{invite['token']}")
    assert resp.status_code == 200
    assert "Welcome back" in resp.text
    assert "dugg_" in resp.text  # keys visible


def test_invite_page_shows_keys_json_before_onboarding(client):
    """JSON: after redemption, invite URL returns keys until feed is visited."""
    c, user = client
    import os
    db = DuggDB(Path(os.environ["DUGG_DB_PATH"]))
    invite = db.create_invite_token(user["id"], name_hint="Rocco")
    db.close()
    c.post(f"/invite/{invite['token']}/redeem",
           json={"name": "Rocco"},
           headers={"Content-Type": "application/json"})
    resp = c.get(f"/invite/{invite['token']}", headers={"Accept": "application/json"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "redeemed_pending_onboarding"
    assert "api_key" in data["user"]
    assert "api_key" in data["agent"]


def test_invite_page_does_not_lock_on_feed_visit(client):
    """Feed visit alone should NOT lock the invite page — only SSE/tool call does."""
    c, user = client
    import os
    db = DuggDB(Path(os.environ["DUGG_DB_PATH"]))
    invite = db.create_invite_token(user["id"], name_hint="Rocco")
    db.close()
    resp = c.post(f"/invite/{invite['token']}/redeem",
                  json={"name": "Rocco"},
                  headers={"Content-Type": "application/json"})
    new_user_key = resp.json()["user"]["api_key"]
    c.get(f"/feed/{new_user_key}")
    # Invite page should still show keys — feed visit no longer locks it
    resp = c.get(f"/invite/{invite['token']}")
    assert resp.status_code == 200
    assert "dugg_" in resp.text


def test_invite_page_locks_after_tool_call(client):
    """After an authenticated tool call, invite page should lock."""
    c, user = client
    import os
    db = DuggDB(Path(os.environ["DUGG_DB_PATH"]))
    invite = db.create_invite_token(user["id"], name_hint="Rocco")
    db.close()
    resp = c.post(f"/invite/{invite['token']}/redeem",
                  json={"name": "Rocco"},
                  headers={"Content-Type": "application/json"})
    agent_key = resp.json()["agent"]["api_key"]
    # Invite page should still show keys before any tool call
    resp = c.get(f"/invite/{invite['token']}")
    assert resp.status_code == 200
    assert "dugg_" in resp.text
    # Make an authenticated tool call
    c.post("/tools/dugg_welcome", json={}, headers={"X-Dugg-Key": agent_key})
    # Now invite page should be locked
    resp = c.get(f"/invite/{invite['token']}")
    assert resp.status_code == 410
    assert "Already redeemed" in resp.text


def test_invite_full_agent_flow(client):
    """End-to-end: agent discovers invite via JSON, redeems it, then uses the agent key."""
    c, user = client
    import os
    db = DuggDB(Path(os.environ["DUGG_DB_PATH"]))
    invite = db.create_invite_token(user["id"], name_hint="Miles")
    db.close()
    # Step 1: Discover
    resp = c.get(f"/invite/{invite['token']}", headers={"Accept": "application/json"})
    assert resp.status_code == 200
    discover = resp.json()
    assert discover["invite"]["name_hint"] == "Miles"
    # Step 2: Redeem
    resp = c.post(f"/invite/{invite['token']}/redeem",
                  json={"name": "Miles (Agent)"},
                  headers={"Content-Type": "application/json"})
    assert resp.status_code == 201
    redeem = resp.json()
    agent_key = redeem["agent"]["api_key"]
    # Step 3: Use the agent key to call a tool
    resp = c.post("/tools/dugg_welcome", json={},
                  headers={"X-Dugg-Key": agent_key})
    assert resp.status_code == 200
    data = resp.json()
    assert "Miles" in data["result"]


# --- Bootstrap ---


def test_bootstrap_creates_first_user(db_path):
    """POST /bootstrap creates the first user when DB is empty."""
    db = DuggDB(db_path)
    db.close()
    import os
    os.environ["DUGG_DB_PATH"] = str(db_path)
    import dugg.server as srv
    srv.db = None
    app = create_app(db_path=db_path)
    with TestClient(app) as c:
        resp = c.post("/bootstrap", json={"name": "FirstAdmin"})
        assert resp.status_code == 201
        data = resp.json()
        assert data["status"] == "bootstrapped"
        assert data["user"]["name"] == "FirstAdmin"
        assert "dugg_" in data["user"]["api_key"]
    srv.db = None


def test_bootstrap_fails_when_users_exist(client):
    """POST /bootstrap returns 400 when users already exist."""
    c, user = client
    resp = c.post("/bootstrap", json={"name": "Intruder"})
    assert resp.status_code == 400
    assert "already has users" in resp.json()["error"]


# --- Rotate Key ---

def test_rotate_key_requires_auth(client):
    c, user = client
    resp = c.post("/rotate-key")
    assert resp.status_code == 401


def test_rotate_key_returns_new_key_and_invalidates_old(client):
    c, user = client
    old_key = user["api_key"]
    resp = c.post("/rotate-key", headers={"X-Dugg-Key": old_key})
    assert resp.status_code == 200
    data = resp.json()
    new_key = data["api_key"]
    assert new_key.startswith("dugg_")
    assert new_key != old_key
    # Old key is dead
    r2 = c.post("/rotate-key", headers={"X-Dugg-Key": old_key})
    assert r2.status_code == 401
    # New key works
    r3 = c.post("/tools/dugg_feed", json={}, headers={"X-Dugg-Key": new_key})
    assert r3.status_code == 200


# --- Resource Viewer (/r/{id}) ---

def _make_pasted_resource(db_path, user):
    """Helper: insert a pasted content resource with collection_id set."""
    from dugg.db import DuggDB, _uuid
    d = DuggDB(db_path)
    coll_id = d.ensure_default_collection(user["id"])
    res_id = _uuid()
    d.add_resource(
        url=f"dugg://content/{res_id}",
        collection_id=coll_id,
        submitted_by=user["id"],
        title="Secret Notes",
        transcript="line one\nline two",
        source_type="email",
    )
    # Retrieve actual stored id (add_resource generates its own)
    row = d.conn.execute(
        "SELECT id FROM resources WHERE url = ? AND submitted_by = ?",
        (f"dugg://content/{res_id}", user["id"]),
    ).fetchone()
    d.close()
    return row[0]


def test_resource_page_unauth_returns_form(client, db_path, user):
    c, _ = client
    res_id = _make_pasted_resource(db_path, user)
    resp = c.get(f"/r/{res_id}")
    assert resp.status_code == 401
    assert "<form" in resp.text
    assert "/unlock" in resp.text
    assert user["api_key"] not in resp.text  # no key leaked


def test_resource_page_with_header_key_renders(client, db_path, user):
    c, _ = client
    res_id = _make_pasted_resource(db_path, user)
    resp = c.get(f"/r/{res_id}", headers={"X-Dugg-Key": user["api_key"]})
    assert resp.status_code == 200
    assert "Secret Notes" in resp.text
    assert "line one" in resp.text


def test_resource_unlock_invalid_key(client, db_path, user):
    c, _ = client
    res_id = _make_pasted_resource(db_path, user)
    resp = c.post(f"/r/{res_id}/unlock", data={"key": "dugg_wrong"})
    assert resp.status_code == 401
    assert "Invalid key" in resp.text


def test_resource_unlock_sets_cookie_and_redirects(client, db_path, user):
    c, _ = client
    res_id = _make_pasted_resource(db_path, user)
    resp = c.post(f"/r/{res_id}/unlock", data={"key": user["api_key"]}, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/r/{res_id}"
    cookies = resp.cookies
    assert cookies.get("dugg_key") == user["api_key"]
    # Follow-up GET uses the cookie
    r2 = c.get(f"/r/{res_id}")
    assert r2.status_code == 200
    assert "Secret Notes" in r2.text


def test_resource_page_403_without_membership(client, db_path, user):
    """Valid key, but resource lives in a collection the user isn't a member of."""
    from dugg.db import DuggDB, _uuid
    d = DuggDB(db_path)
    other = d.create_user("Stranger")
    other_coll = d.create_collection("Private", other["id"], visibility="private")
    res_id = _uuid()
    d.add_resource(
        url=f"dugg://content/{res_id}",
        collection_id=other_coll["id"],
        submitted_by=other["id"],
        title="Not yours",
        transcript="nope",
    )
    row = d.conn.execute(
        "SELECT id FROM resources WHERE url = ?", (f"dugg://content/{res_id}",)
    ).fetchone()
    actual_id = row[0]
    d.close()
    c, _ = client
    resp = c.get(f"/r/{actual_id}", headers={"X-Dugg-Key": user["api_key"]})
    assert resp.status_code == 404  # not found (we don't leak existence)


# --- Slack actions ---


def test_slack_actions_react(client, db_path):
    """Slack Block Kit button click fires a reaction."""
    c, user = client
    d = DuggDB(db_path)
    coll_id = d.ensure_default_collection(user["id"])
    res = d.add_resource(
        url="https://example.com/slack-react",
        collection_id=coll_id,
        submitted_by=user["id"],
        title="Slack React Test",
    )
    d.close()

    payload = json.dumps({
        "type": "block_actions",
        "user": {"username": user["name"]},
        "actions": [{
            "action_id": "dugg_react_star",
            "value": res["id"],
        }],
    })
    resp = c.post("/slack/actions", data={"payload": payload})
    assert resp.status_code == 200
    body = resp.json()
    assert "star" in body.get("text", "")

    # Verify reaction was stored
    d = DuggDB(db_path)
    reactions = d.get_reactions(res["id"], user["id"])
    assert reactions is not None
    assert reactions["total"] == 1
    assert reactions["breakdown"]["star"] == 1
    d.close()
