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
    assert "url" in resp.json()["detail"].lower()


def test_ingest_missing_source_instance(client):
    c, user = client
    resp = c.post("/ingest", json={
        "resource": {"url": "https://example.com/article"},
    }, headers={"X-Dugg-Key": user["api_key"]})
    assert resp.status_code == 400
    assert "source_instance_id" in resp.json()["detail"]


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


# --- Delete endpoint ---

def test_delete_requires_auth(client):
    c, user = client
    resp = c.post("/delete", json={"url": "https://example.com/x"})
    assert resp.status_code == 401


def test_delete_missing_url(client):
    c, user = client
    resp = c.post("/delete", json={},
                  headers={"X-Dugg-Key": user["api_key"]})
    assert resp.status_code == 400


def test_delete_not_found(client):
    c, user = client
    headers = {"X-Dugg-Key": user["api_key"]}
    # Ingest something first to establish collection membership
    c.post("/ingest", json={
        "resource": {"url": "https://example.com/setup", "title": "Setup"},
        "source_instance_id": "remote1",
    }, headers=headers)
    resp = c.post("/delete", json={"url": "https://example.com/nonexistent"},
                  headers=headers)
    assert resp.status_code == 404


def test_delete_success(client):
    c, user = client
    headers = {"X-Dugg-Key": user["api_key"]}
    # Ingest a resource first
    c.post("/ingest", json={
        "resource": {"url": "https://example.com/delete-me", "title": "Delete Me"},
        "source_instance_id": "remote1",
    }, headers=headers)
    # Delete it via /delete
    resp = c.post("/delete", json={"url": "https://example.com/delete-me"}, headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "deleted"
    assert data["url"] == "https://example.com/delete-me"
    # Verify it's gone — re-ingest should succeed as new
    resp2 = c.post("/ingest", json={
        "resource": {"url": "https://example.com/delete-me", "title": "Re-added"},
        "source_instance_id": "remote1",
    }, headers=headers)
    assert resp2.status_code == 201


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


# --- Structured JSON API (/api/*) ---

def _seed_resource(db_path, user, *, url="https://example.com/post",
                   title="Hello", note="my note", description="desc",
                   source_type="article", thumbnail=""):
    from dugg.db import DuggDB
    d = DuggDB(db_path)
    coll_id = d.ensure_default_collection(user["id"])
    res = d.add_resource(
        url=url,
        collection_id=coll_id,
        submitted_by=user["id"],
        title=title,
        note=note,
        description=description,
        source_type=source_type,
        thumbnail=thumbnail,
    )
    d.close()
    return res["id"]


def test_api_feed_requires_auth(client):
    c, _ = client
    resp = c.get("/api/feed")
    assert resp.status_code == 401


def test_api_feed_returns_structured_resources(client, db_path, user):
    c, _ = client
    _seed_resource(db_path, user)
    resp = c.get("/api/feed", headers={"X-Dugg-Key": user["api_key"]})
    assert resp.status_code == 200
    data = resp.json()
    assert "resources" in data
    assert data["count"] == 1
    r = data["resources"][0]
    assert r["url"] == "https://example.com/post"
    assert r["title"] == "Hello"
    assert r["note"] == "my note"
    assert r["description"] == "desc"
    assert r["submitter"] == user["name"]
    assert r["source_type"] == "article"
    assert isinstance(r["tags"], list)
    assert r["added_at"]
    # article badge hint but no deep link
    assert r["source_hints"]["badge"] == {"label": "Article", "color": "#2563EB"}
    assert r["source_hints"]["primary"] == {"label": "Open in Safari"}


def test_api_feed_includes_thumbnail(client, db_path, user):
    c, _ = client
    _seed_resource(
        db_path, user,
        url="https://example.com/with-image",
        thumbnail="https://example.com/og.jpg",
    )
    resp = c.get("/api/feed", headers={"X-Dugg-Key": user["api_key"]})
    assert resp.json()["resources"][0]["thumbnail"] == "https://example.com/og.jpg"


def test_api_feed_includes_youtube_deep_link(client, db_path, user):
    c, _ = client
    _seed_resource(
        db_path, user,
        url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        source_type="youtube",
    )
    resp = c.get("/api/feed", headers={"X-Dugg-Key": user["api_key"]})
    assert resp.status_code == 200
    r = resp.json()["resources"][0]
    assert r["source_hints"]["badge"] == {"label": "YouTube", "color": "#DC2626"}
    assert r["source_hints"]["primary"] == {
        "label": "Open in YouTube",
        "scheme": "youtube",
        "deep_link": "youtube://www.youtube.com/watch?v=dQw4w9WgXcQ",
    }


def test_api_feed_respects_limit(client, db_path, user):
    c, _ = client
    for i in range(3):
        _seed_resource(db_path, user, url=f"https://example.com/r{i}", title=f"R{i}")
    resp = c.get("/api/feed?limit=2", headers={"X-Dugg-Key": user["api_key"]})
    assert resp.status_code == 200
    assert resp.json()["count"] == 2


def test_api_feed_unread_filter_excludes_read_resources(client, db_path, user):
    c, _ = client
    read_id = _seed_resource(db_path, user, url="https://example.com/read")
    unread_id = _seed_resource(db_path, user, url="https://example.com/unread")
    d = DuggDB(db_path)
    d.mark_read(user["id"], read_id, "cli")
    d.close()

    resp = c.get("/api/feed?unread=true", headers={"X-Dugg-Key": user["api_key"]})
    assert resp.status_code == 200
    ids = [resource["id"] for resource in resp.json()["resources"]]
    assert unread_id in ids
    assert read_id not in ids


def test_api_feed_urls_includes_read_at_and_unread_filter(client, db_path, user):
    c, _ = client
    read_id = _seed_resource(db_path, user, url="https://example.com/feed-url-read", title="Read URL")
    unread_id = _seed_resource(db_path, user, url="https://example.com/feed-url-unread", title="Unread URL")
    d = DuggDB(db_path)
    d.mark_read(user["id"], read_id, "cli")
    d.conn.execute(
        "UPDATE read_states SET read_at = ?, last_read_at = ? WHERE user_id = ? AND resource_id = ?",
        ("2026-04-25T12:00:00+00:00", "2026-04-25T12:00:00+00:00", user["id"], read_id),
    )
    d.conn.commit()
    d.close()

    resp = c.get("/api/feed/urls", headers={"X-Dugg-Key": user["api_key"]})
    assert resp.status_code == 200
    urls = {entry["id"]: entry for entry in resp.json()["urls"]}
    assert urls[read_id]["read_at"] == "2026-04-25T12:00:00+00:00"
    assert urls[unread_id]["read_at"] is None

    unread_resp = c.get("/api/feed/urls?unread=true", headers={"X-Dugg-Key": user["api_key"]})
    unread_ids = [entry["id"] for entry in unread_resp.json()["urls"]]
    assert unread_id in unread_ids
    assert read_id not in unread_ids


def test_api_feed_urls_exact_match_returns_popup_metadata(client, db_path, user):
    c, _ = client
    exact_id = _seed_resource(
        db_path,
        user,
        url="https://example.com/match-me/",
        title="Match Me",
        note="Primary note for popup preview",
    )
    _seed_resource(db_path, user, url="https://example.com/other", title="Other")

    d = DuggDB(db_path)
    d.react_to_resource(exact_id, user["id"], "star")
    d.add_resource_note(exact_id, "Secondary note that should only affect the count", submitter_user_id=user["id"], submitter_name=user["name"])
    d.close()

    resp = c.get(
        "/api/feed/urls?url=https://example.com/match-me",
        headers={"X-Dugg-Key": user["api_key"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 1
    entry = body["urls"][0]
    assert entry["id"] == exact_id
    assert entry["title"] == "Match Me"
    assert entry["notes_count"] == 2
    assert entry["primary_note_preview"] == "Primary note for popup preview"
    assert entry["viewer_reactions"] == {"star": True, "thumbsup": False}
    assert entry["reaction_counts"] == {"star": 1, "thumbsup": 0}


def test_api_read_post_delete_and_get(client, db_path, user):
    c, _ = client
    res_id = _seed_resource(db_path, user, url="https://example.com/read-endpoint")

    post_resp = c.post(
        f"/api/read/{res_id}",
        json={"source": "cli"},
        headers={"X-Dugg-Key": user["api_key"]},
    )
    assert post_resp.status_code == 200
    body = post_resp.json()
    assert body["resource_id"] == res_id
    assert body["source"] == "cli"

    get_resp = c.get(
        "/api/read?since=2026-04-20T00:00:00+00:00",
        headers={"X-Dugg-Key": user["api_key"]},
    )
    assert get_resp.status_code == 200
    rows = get_resp.json()["resources"]
    assert len(rows) == 1
    assert rows[0]["resource_id"] == res_id

    delete_resp = c.delete(f"/api/read/{res_id}", headers={"X-Dugg-Key": user["api_key"]})
    assert delete_resp.status_code == 200

    get_after_delete = c.get(
        "/api/read?since=2026-04-20T00:00:00+00:00",
        headers={"X-Dugg-Key": user["api_key"]},
    )
    assert get_after_delete.status_code == 200
    assert get_after_delete.json()["resources"] == []


def test_api_read_accepts_web_button_source(client, db_path, user):
    c, _ = client
    res_id = _seed_resource(db_path, user, url="https://example.com/read-web-button")

    resp = c.post(
        f"/api/read/{res_id}",
        json={"source": "web_button"},
        headers={"X-Dugg-Key": user["api_key"]},
    )
    assert resp.status_code == 200
    assert resp.json()["source"] == "web_button"


def test_api_read_get_paginates(client, db_path, user):
    c, _ = client
    res1 = _seed_resource(db_path, user, url="https://example.com/read-page-1")
    res2 = _seed_resource(db_path, user, url="https://example.com/read-page-2")
    d = DuggDB(db_path)
    d.mark_read(user["id"], res1, "cli")
    d.mark_read(user["id"], res2, "cli")
    d.conn.execute(
        "UPDATE read_states SET read_at = ?, last_read_at = ? WHERE resource_id = ?",
        ("2026-04-24T00:00:00+00:00", "2026-04-24T00:00:00+00:00", res1),
    )
    d.conn.execute(
        "UPDATE read_states SET read_at = ?, last_read_at = ? WHERE resource_id = ?",
        ("2026-04-25T00:00:00+00:00", "2026-04-25T00:00:00+00:00", res2),
    )
    d.conn.commit()
    d.close()

    first = c.get(
        "/api/read?since=2026-04-20T00:00:00+00:00&cursor=&limit=1",
        headers={"X-Dugg-Key": user["api_key"]},
    )
    assert first.status_code == 200
    first_body = first.json()
    assert [row["resource_id"] for row in first_body["resources"]] == [res2]
    assert first_body["next_cursor"]

    second = c.get(
        f"/api/read?since=2026-04-20T00:00:00+00:00&cursor={first_body['next_cursor']}",
        headers={"X-Dugg-Key": user["api_key"]},
    )
    assert second.status_code == 200
    assert [row["resource_id"] for row in second.json()["resources"]] == [res1]


def test_api_react_rejects_tap(client, db_path, user):
    c, _ = client
    res_id = _seed_resource(db_path, user, url="https://example.com/react-tap")

    resp = c.post(
        "/api/react",
        json={"resource_id": res_id, "type": "tap"},
        headers={"X-Dugg-Key": user["api_key"]},
    )
    assert resp.status_code == 400
    assert resp.json() == {
        "error": "reaction_type 'tap' is no longer supported; use POST /api/read instead"
    }


def test_api_react_marks_read_implicitly(client, db_path, user):
    c, _ = client
    res_id = _seed_resource(db_path, user, url="https://example.com/react-read")

    resp = c.post(
        "/api/react",
        json={"resource_id": res_id, "type": "star"},
        headers={"X-Dugg-Key": user["api_key"]},
    )
    assert resp.status_code == 200
    assert resp.json()["reaction"]["reaction_type"] == "star"

    d = DuggDB(db_path)
    read_state = d.get_read_state(user["id"], res_id)
    reactions = d.get_reactions(res_id, user["id"])
    d.close()
    assert read_state is not None
    assert read_state["source"] == "mcp_react_implicit"
    assert reactions is not None
    assert reactions["breakdown"]["star"] == 1


def test_api_react_accepts_resource_path_and_reaction_field(client, db_path, user):
    c, _ = client
    res_id = _seed_resource(db_path, user, url="https://example.com/react-path")

    resp = c.post(
        f"/api/react/{res_id}",
        json={"reaction": "thumbsup"},
        headers={"X-Dugg-Key": user["api_key"]},
    )
    assert resp.status_code == 200
    assert resp.json()["reaction"]["reaction_type"] == "thumbsup"


def test_api_react_marks_read_implicitly_for_web_surface(client, db_path, user):
    c, _ = client
    res_id = _seed_resource(db_path, user, url="https://example.com/react-read-web")

    resp = c.post(
        "/api/react",
        json={"resource_id": res_id, "type": "thumbsup"},
        headers={
            "X-Dugg-Key": user["api_key"],
            "X-Dugg-Surface": "web",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["reaction"]["reaction_type"] == "thumbsup"

    d = DuggDB(db_path)
    read_state = d.get_read_state(user["id"], res_id)
    reactions = d.get_reactions(res_id, user["id"])
    d.close()
    assert read_state is not None
    assert read_state["source"] == "web_react_implicit"
    assert reactions is not None
    assert reactions["breakdown"]["thumbsup"] == 1


def test_api_unreact_removes_existing_reaction(client, db_path, user):
    c, _ = client
    res_id = _seed_resource(db_path, user, url="https://example.com/unreact-http")
    d = DuggDB(db_path)
    d.react_to_resource(res_id, user["id"], "star")
    d.close()

    resp = c.delete(
        f"/api/react/{res_id}?type=star",
        headers={"X-Dugg-Key": user["api_key"]},
    )
    assert resp.status_code == 200
    assert resp.json() == {"removed": True}

    d = DuggDB(db_path)
    reactions = d.get_reactions(res_id, user["id"])
    events = d.get_events(user["id"])
    d.close()
    assert reactions == {"resource_id": res_id, "total": 0, "breakdown": {}}
    removed = [e for e in events if e["event_type"] == "reaction_removed"]
    assert len(removed) == 1
    assert removed[0]["payload"]["resource_id"] == res_id


def test_api_unreact_returns_false_when_missing(client, db_path, user):
    c, _ = client
    res_id = _seed_resource(db_path, user, url="https://example.com/unreact-missing")

    resp = c.delete(
        f"/api/react/{res_id}?type=thumbsup",
        headers={"X-Dugg-Key": user["api_key"]},
    )
    assert resp.status_code == 200
    assert resp.json() == {"removed": False}


def test_api_unreact_rejects_invalid_type(client, db_path, user):
    c, _ = client
    res_id = _seed_resource(db_path, user, url="https://example.com/unreact-bad-type")

    resp = c.delete(
        f"/api/react/{res_id}?type=tap",
        headers={"X-Dugg-Key": user["api_key"]},
    )
    assert resp.status_code == 400
    assert resp.json() == {
        "error": "reaction_type 'tap' is no longer supported; use POST /api/read instead"
    }


def test_api_unreact_requires_auth(client, db_path, user):
    c, _ = client
    res_id = _seed_resource(db_path, user, url="https://example.com/unreact-auth")
    resp = c.delete(f"/api/react/{res_id}?type=star")
    assert resp.status_code == 401


def test_api_resource_requires_auth(client, db_path, user):
    c, _ = client
    res_id = _seed_resource(db_path, user)
    resp = c.get(f"/api/resource/{res_id}")
    assert resp.status_code == 401


def test_api_resource_returns_structured_row(client, db_path, user):
    c, _ = client
    res_id = _seed_resource(db_path, user)
    resp = c.get(f"/api/resource/{res_id}", headers={"X-Dugg-Key": user["api_key"]})
    assert resp.status_code == 200
    r = resp.json()["resource"]
    assert r["id"] == res_id
    assert r["title"] == "Hello"
    assert r["note"] == "my note"


def test_api_resource_hides_inaccessible(client, db_path, user):
    """User without access to a resource's collection should see 404."""
    c, _ = client
    from dugg.db import DuggDB
    d = DuggDB(db_path)
    other = d.create_user("Outsider")
    private_coll = d.create_collection("Private", other["id"], visibility="private")
    res = d.add_resource(
        url="https://private.example.com",
        collection_id=private_coll["id"],
        submitted_by=other["id"],
        title="Secret",
    )
    d.close()
    resp = c.get(f"/api/resource/{res['id']}", headers={"X-Dugg-Key": user["api_key"]})
    assert resp.status_code == 404


def test_api_resource_merges_sibling_notes_into_notes_array(client, db_path, user):
    """Resources ingested from a remote server store the incoming note as a
    sibling note (quarantined from outbound re-publish). The /api/resource/*
    serializer must surface those sibling notes to the iOS client via the
    `notes` array; otherwise cross-server notes silently disappear in native
    clients even though the browser feed renders them fine."""
    from dugg.db import DuggDB
    c, _ = client
    res_id = _seed_resource(db_path, user, note="")
    d = DuggDB(db_path)
    d.add_resource_note(
        res_id, "Kade's note from private",
        source_server="https://private.example",
        submitter_name="Kade",
    )
    d.close()

    resp = c.get(f"/api/resource/{res_id}", headers={"X-Dugg-Key": user["api_key"]})
    assert resp.status_code == 200
    r = resp.json()["resource"]
    assert r["note"] == ""
    assert len(r["notes"]) == 1
    assert r["notes"][0]["author"] == "Kade"
    assert r["notes"][0]["note"] == "Kade's note from private"
    assert r["notes"][0]["source_server"] == "https://private.example"


def test_api_feed_includes_notes_for_each_resource(client, db_path, user):
    """Feed endpoint should emit a `notes` array on each resource so card UIs
    can thread multi-contributor notes without a per-resource follow-up fetch."""
    from dugg.db import DuggDB
    c, _ = client
    res_id = _seed_resource(db_path, user, note="original")
    d = DuggDB(db_path)
    d.add_resource_note(
        res_id, "later note", source_server="https://other.example",
        submitter_name="Other",
    )
    d.close()

    resp = c.get("/api/feed", headers={"X-Dugg-Key": user["api_key"]})
    assert resp.status_code == 200
    notes = resp.json()["resources"][0]["notes"]
    assert [n["note"] for n in notes] == ["original", "later note"]


def test_api_note_requires_auth(client):
    c, _ = client
    resp = c.post("/api/note", json={"resource_id": "x", "note": "y"})
    assert resp.status_code == 401


def test_api_note_rejects_empty_body(client, db_path, user):
    c, _ = client
    res_id = _seed_resource(db_path, user, note="")
    resp = c.post(
        "/api/note",
        json={"resource_id": res_id, "note": ""},
        headers={"X-Dugg-Key": user["api_key"]},
    )
    assert resp.status_code == 400


def test_api_note_rejects_missing_resource_id(client, user):
    c, _ = client
    resp = c.post(
        "/api/note",
        json={"note": "hello"},
        headers={"X-Dugg-Key": user["api_key"]},
    )
    assert resp.status_code == 400


def test_api_note_rejects_unknown_resource(client, user):
    c, _ = client
    resp = c.post(
        "/api/note",
        json={"resource_id": "nonexistent-id", "note": "hi"},
        headers={"X-Dugg-Key": user["api_key"]},
    )
    assert resp.status_code == 404


def test_api_note_attaches_sibling_note_visible_on_resource(client, db_path, user):
    """Posting /api/note attaches a sibling note that shows up in the
    resource's `notes[]` array with the caller as author."""
    c, _ = client
    res_id = _seed_resource(db_path, user, note="primary")

    resp = c.post(
        "/api/note",
        json={"resource_id": res_id, "note": "and one more thought"},
        headers={"X-Dugg-Key": user["api_key"]},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["note"]["author"] == user["name"]
    assert body["note"]["note"] == "and one more thought"

    get = c.get(f"/api/resource/{res_id}", headers={"X-Dugg-Key": user["api_key"]})
    notes = get.json()["resource"]["notes"]
    assert [n["note"] for n in notes] == ["primary", "and one more thought"]
    assert notes[1]["author"] == user["name"]


def test_api_note_is_idempotent_for_same_author_same_text(client, db_path, user):
    """UNIQUE (resource_id, source_server, submitter_user_id, note) means a
    duplicate post silently no-ops — duplicate clients retrying the same
    payload should not produce a second entry."""
    c, _ = client
    res_id = _seed_resource(db_path, user, note="")
    for _ in range(3):
        resp = c.post(
            "/api/note",
            json={"resource_id": res_id, "note": "same text"},
            headers={"X-Dugg-Key": user["api_key"]},
        )
        assert resp.status_code == 201

    get = c.get(f"/api/resource/{res_id}", headers={"X-Dugg-Key": user["api_key"]})
    notes = get.json()["resource"]["notes"]
    assert sum(1 for n in notes if n["note"] == "same text") == 1


def test_api_note_denies_resource_outside_accessible_collections(client, db_path, user):
    """A resource in a collection the caller cannot see must 404, not
    leak a write path to arbitrary resource_ids."""
    from dugg.db import DuggDB
    c, _ = client
    d = DuggDB(db_path)
    other = d.create_user("Other")
    other_coll = d.create_collection("Private", other["id"], visibility="private")
    res = d.add_resource(url="https://other.example/x", collection_id=other_coll["id"],
                         submitted_by=other["id"], title="hidden")
    d.close()

    resp = c.post(
        "/api/note",
        json={"resource_id": res["id"], "note": "sneaky"},
        headers={"X-Dugg-Key": user["api_key"]},
    )
    assert resp.status_code == 404


def test_api_edit_requires_auth(client):
    c, _ = client
    resp = c.post("/api/edit", json={"resource_id": "x", "note": "y"})
    assert resp.status_code == 401


def test_api_edit_updates_note_and_returns_serialized_resource(client, db_path, user):
    c, _ = client
    res_id = _seed_resource(db_path, user, note="initial")

    resp = c.post(
        "/api/edit",
        json={"resource_id": res_id, "note": "revised", "title": "New Title"},
        headers={"X-Dugg-Key": user["api_key"]},
    )
    assert resp.status_code == 200
    r = resp.json()["resource"]
    assert r["note"] == "revised"
    assert r["title"] == "New Title"
    assert r["edit_count"] == 2
    assert r["can_edit"] is True
    assert r["can_delete"] is True


def test_api_edit_non_submitter_non_owner_forbidden(client, db_path, user):
    """A collection member who is neither submitter nor owner cannot edit."""
    from dugg.db import DuggDB
    c, _ = client
    d = DuggDB(db_path)
    owner = d.create_user("Owner")
    bystander = d.create_user("Bystander")
    coll = d.create_collection("Shared", owner["id"], visibility="shared")
    d.invite_member(coll["id"], owner["id"], user["id"])
    d.invite_member(coll["id"], owner["id"], bystander["id"])
    res = d.add_resource(url="https://example.com/other", collection_id=coll["id"],
                         submitted_by=user["id"], title="user's")
    d.close()

    resp = c.post(
        "/api/edit",
        json={"resource_id": res["id"], "note": "sneaky overwrite"},
        headers={"X-Dugg-Key": bystander["api_key"]},
    )
    assert resp.status_code == 403


def test_api_edit_history_records_url_change(client, db_path, user):
    """Link-swap attack surface — the primary reason edit history exists.
    A URL mutation must land in resource_edits so moderators can audit it."""
    c, _ = client
    res_id = _seed_resource(db_path, user, url="https://clean.example/post")

    resp = c.post(
        "/api/edit",
        json={"resource_id": res_id, "url": "https://malware.example/post"},
        headers={"X-Dugg-Key": user["api_key"]},
    )
    assert resp.status_code == 200

    hist = c.get(
        f"/api/resource/{res_id}/edits",
        headers={"X-Dugg-Key": user["api_key"]},
    )
    assert hist.status_code == 200
    edits = hist.json()["edits"]
    assert len(edits) == 1
    assert edits[0]["field"] == "url"
    assert edits[0]["old_value"] == "https://clean.example/post"
    assert edits[0]["new_value"] == "https://malware.example/post"
    assert edits[0]["actor"] == user["name"]


def test_api_edit_noop_for_unchanged_value_does_not_log(client, db_path, user):
    """Submitting the same value must not create a phantom audit entry."""
    c, _ = client
    res_id = _seed_resource(db_path, user, note="same")

    resp = c.post(
        "/api/edit",
        json={"resource_id": res_id, "note": "same"},
        headers={"X-Dugg-Key": user["api_key"]},
    )
    assert resp.status_code == 200

    hist = c.get(
        f"/api/resource/{res_id}/edits",
        headers={"X-Dugg-Key": user["api_key"]},
    )
    assert hist.json()["count"] == 0


def test_api_resource_edits_visible_to_any_collection_member(client, db_path, user):
    """Transparency default — any collection member, not just the owner,
    can read edit history. A bystander's read of history must succeed."""
    from dugg.db import DuggDB
    c, _ = client
    res_id = _seed_resource(db_path, user, note="original")
    # record an edit so history is non-empty
    c.post("/api/edit", json={"resource_id": res_id, "note": "updated"},
           headers={"X-Dugg-Key": user["api_key"]})

    d = DuggDB(db_path)
    bystander = d.create_user("Bystander")
    # put the bystander in the same collection as user
    res = d.get_resource(res_id)
    d.invite_member(res["collection_id"], user["id"], bystander["id"])
    d.close()

    resp = c.get(f"/api/resource/{res_id}/edits",
                 headers={"X-Dugg-Key": bystander["api_key"]})
    assert resp.status_code == 200
    assert resp.json()["count"] == 1


def test_api_resource_edits_hidden_from_non_members(client, db_path, user):
    """Edit history must not leak to users who can't see the resource."""
    from dugg.db import DuggDB
    c, _ = client
    res_id = _seed_resource(db_path, user, note="secret")
    c.post("/api/edit", json={"resource_id": res_id, "note": "still secret"},
           headers={"X-Dugg-Key": user["api_key"]})
    d = DuggDB(db_path)
    outsider = d.create_user("Outsider")
    d.close()

    resp = c.get(f"/api/resource/{res_id}/edits",
                 headers={"X-Dugg-Key": outsider["api_key"]})
    assert resp.status_code == 404


def test_api_delete_by_submitter_succeeds(client, db_path, user):
    """Regression: previously the HTTP /delete route authorized submitters
    but the DB layer refused them, so submitter-delete round-tripped as a
    500. Verify the loosening lets a submitter remove their own post."""
    c, _ = client
    res_id = _seed_resource(db_path, user)
    url = "https://example.com/post"

    resp = c.post(
        "/delete",
        json={"url": url, "source_instance_id": ""},
        headers={"X-Dugg-Key": user["api_key"]},
    )
    assert resp.status_code == 200
    assert resp.json()["id"] == res_id

    get = c.get(f"/api/resource/{res_id}", headers={"X-Dugg-Key": user["api_key"]})
    assert get.status_code == 404


def test_api_resource_exposes_can_edit_and_can_delete(client, db_path, user):
    """iOS relies on server-computed can_edit/can_delete flags to decide
    whether to render the edit/delete buttons. Own resource → true."""
    c, _ = client
    res_id = _seed_resource(db_path, user)
    resp = c.get(f"/api/resource/{res_id}", headers={"X-Dugg-Key": user["api_key"]})
    assert resp.status_code == 200
    r = resp.json()["resource"]
    assert r["can_edit"] is True
    assert r["can_delete"] is True
    assert r["edit_count"] == 0


def test_tools_dugg_edit_agent_enriched_skips_audit(client, db_path, user):
    """Machine enrichment via dugg_edit with agent_enriched=true must not
    pollute the user-facing edit-history counter. Regression: the dugg
    watchdog calls dugg_edit post-add to push llm summary + tags back, and
    that should look like enrichment, not a user edit."""
    c, _ = client
    res_id = _seed_resource(db_path, user, description="")

    resp = c.post(
        "/tools/dugg_edit",
        json={
            "resource_id": res_id,
            "description": "Agent-generated summary",
            "agent_enriched": True,
        },
        headers={"X-Dugg-Key": user["api_key"]},
    )
    assert resp.status_code == 200

    get = c.get(f"/api/resource/{res_id}", headers={"X-Dugg-Key": user["api_key"]})
    assert get.json()["resource"]["edit_count"] == 0

    edits = c.get(f"/api/resource/{res_id}/edits",
                  headers={"X-Dugg-Key": user["api_key"]})
    assert edits.json()["edits"] == []


def test_tools_dugg_edit_without_flag_still_audits(client, db_path, user):
    """Absence of the agent_enriched flag keeps the default behavior —
    human-directed edits through dugg_edit (e.g. a CLI caller or future
    surface) are audited."""
    c, _ = client
    res_id = _seed_resource(db_path, user, note="initial")

    resp = c.post(
        "/tools/dugg_edit",
        json={"resource_id": res_id, "note": "human-driven change"},
        headers={"X-Dugg-Key": user["api_key"]},
    )
    assert resp.status_code == 200

    get = c.get(f"/api/resource/{res_id}", headers={"X-Dugg-Key": user["api_key"]})
    assert get.json()["resource"]["edit_count"] == 1


def test_api_note_edit_by_author_succeeds_and_audits(client, db_path, user):
    """Sibling-note edit by the author: updates text, logs a resource_edits
    row with field='note' so note-swap is auditable alongside URL-swap."""
    from dugg.db import DuggDB
    c, _ = client
    # Submitter owns the resource; `user` is a different member who'll
    # attach and then edit their own sibling note.
    d = DuggDB(db_path)
    submitter = d.create_user("Submitter")
    coll = d.create_collection("Shared", submitter["id"], visibility="shared")
    d.invite_member(coll["id"], submitter["id"], user["id"])
    res = d.add_resource(url="https://example.com/shared", collection_id=coll["id"],
                         submitted_by=submitter["id"], title="Shared")
    note_row = d.add_resource_note(res["id"], "original sibling",
                                   submitter_user_id=user["id"],
                                   submitter_name=user["name"])
    d.close()

    resp = c.post(
        "/api/note/edit",
        json={"note_id": note_row["id"], "text": "revised sibling"},
        headers={"X-Dugg-Key": user["api_key"]},
    )
    assert resp.status_code == 200
    assert resp.json()["note"]["note"] == "revised sibling"

    hist = c.get(
        f"/api/resource/{res['id']}/edits",
        headers={"X-Dugg-Key": user["api_key"]},
    )
    edits = hist.json()["edits"]
    assert len(edits) == 1
    assert edits[0]["field"] == "note"
    assert edits[0]["old_value"] == "original sibling"
    assert edits[0]["new_value"] == "revised sibling"
    assert edits[0]["actor"] == user["name"]


def test_api_note_edit_by_non_author_forbidden(client, db_path, user):
    """A collection member who didn't author the sibling note can't edit it."""
    from dugg.db import DuggDB
    c, _ = client
    d = DuggDB(db_path)
    author = d.create_user("Author")
    coll = d.create_collection("Shared", user["id"], visibility="shared")
    d.invite_member(coll["id"], user["id"], author["id"])
    res = d.add_resource(url="https://example.com/x", collection_id=coll["id"],
                         submitted_by=user["id"], title="x")
    note_row = d.add_resource_note(res["id"], "author's note",
                                   submitter_user_id=author["id"],
                                   submitter_name=author["name"])
    d.close()

    resp = c.post(
        "/api/note/edit",
        json={"note_id": note_row["id"], "text": "hijack"},
        headers={"X-Dugg-Key": user["api_key"]},
    )
    assert resp.status_code == 403


def test_api_note_delete_by_author_succeeds_and_audits(client, db_path, user):
    """Author deleting their own sibling note removes it and records the
    deletion in the audit trail (old=text, new='')."""
    from dugg.db import DuggDB
    c, _ = client
    d = DuggDB(db_path)
    submitter = d.create_user("Submitter2")
    coll = d.create_collection("Shared2", submitter["id"], visibility="shared")
    d.invite_member(coll["id"], submitter["id"], user["id"])
    res = d.add_resource(url="https://example.com/shared2", collection_id=coll["id"],
                         submitted_by=submitter["id"], title="Shared2")
    note_row = d.add_resource_note(res["id"], "to be deleted",
                                   submitter_user_id=user["id"],
                                   submitter_name=user["name"])
    d.close()

    resp = c.post(
        "/api/note/delete",
        json={"note_id": note_row["id"]},
        headers={"X-Dugg-Key": user["api_key"]},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "deleted"

    # Note is gone from the resource's notes[] payload.
    get = c.get(f"/api/resource/{res['id']}",
                headers={"X-Dugg-Key": user["api_key"]})
    notes = get.json()["resource"]["notes"]
    assert all(n["id"] != note_row["id"] for n in notes)

    # Audit trail captures the deletion.
    hist = c.get(
        f"/api/resource/{res['id']}/edits",
        headers={"X-Dugg-Key": user["api_key"]},
    )
    edits = hist.json()["edits"]
    assert len(edits) == 1
    assert edits[0]["field"] == "note"
    assert edits[0]["old_value"] == "to be deleted"
    assert edits[0]["new_value"] == ""


def test_api_note_delete_is_soft_delete_preserves_tombstone(client, db_path, user):
    """Delete tombstones the row instead of hard-removing it. The note
    disappears from the viewer's /api/* payloads (so the Notes section
    doesn't show it), but the underlying row stays in resource_notes with
    deleted_at set so owner/admin moderation surfaces can reconstruct
    what was removed and by whom.
    """
    from dugg.db import DuggDB
    c, _ = client
    d = DuggDB(db_path)
    coll_id = d.ensure_default_collection(user["id"])
    res = d.add_resource(url="https://example.com/soft-delete", collection_id=coll_id,
                         submitted_by=user["id"], title="Soft")
    note = d.add_resource_note(res["id"], "will be tombstoned",
                               submitter_user_id=user["id"],
                               submitter_name=user["name"])
    d.close()

    resp = c.post(
        "/api/note/delete",
        json={"note_id": note["id"]},
        headers={"X-Dugg-Key": user["api_key"]},
    )
    assert resp.status_code == 200

    # Underlying row still exists with deleted_at stamped.
    d = DuggDB(db_path)
    row = d.conn.execute(
        "SELECT id, note, deleted_at FROM resource_notes WHERE id = ?",
        (note["id"],),
    ).fetchone()
    assert row is not None
    assert row["deleted_at"] is not None
    assert row["note"] == "will be tombstoned"
    # Admin-style listing with include_deleted=True still surfaces it.
    admin_view = d.list_resource_notes(res["id"], include_deleted=True)
    assert any(n["id"] == note["id"] for n in admin_view)
    # Default listing hides it.
    user_view = d.list_resource_notes(res["id"])
    assert all(n["id"] != note["id"] for n in user_view)
    d.close()


def test_add_resource_note_resurrects_tombstoned_identical_text(db_path, user):
    """Delete-then-re-add of identical text must resurrect the tombstone,
    not silently no-op on the UNIQUE constraint."""
    from dugg.db import DuggDB
    d = DuggDB(db_path)
    coll_id = d.ensure_default_collection(user["id"])
    res = d.add_resource(url="https://example.com/resurrect", collection_id=coll_id,
                         submitted_by=user["id"], title="R")
    first = d.add_resource_note(res["id"], "same text",
                                submitter_user_id=user["id"],
                                submitter_name=user["name"])
    d.delete_resource_note(first["id"], user["id"])
    second = d.add_resource_note(res["id"], "same text",
                                 submitter_user_id=user["id"],
                                 submitter_name=user["name"])
    assert second is not None
    # Should be the same row, resurrected (deleted_at cleared).
    assert second["id"] == first["id"]
    live = d.list_resource_notes(res["id"])
    assert any(n["id"] == first["id"] and n.get("deleted_at") is None for n in live)
    d.close()


def test_ingest_attributes_sibling_note_to_local_user_when_name_matches(client, db_path, user):
    """Kade's key pain: a note federated from his home Dugg to chino-bandido
    must be editable on chino-bandido. Previously `/ingest` only threaded
    the matched local user_id onto the resource row, not the sibling note,
    so can_edit came back false for the remote author's own note.
    """
    from dugg.db import DuggDB
    c, _ = client
    d = DuggDB(db_path)
    # Seed a user named "Kade" on this server -- the name the incoming
    # publish will carry as submitter_name. `user` fixture = "Test" by
    # default; we'll auth as Kade-on-destination.
    kade = d.create_user("Kade")
    d.close()

    payload = {
        "source_instance_id": "origin-instance",
        "source_server": "https://home.example",
        "resource": {
            "url": "https://example.com/federated",
            "title": "Federated entry",
            "note": "Kade's note from home",
            "submitter_name": "Kade",
        },
    }
    resp = c.post(
        "/ingest",
        json=payload,
        headers={"X-Dugg-Key": kade["api_key"]},
    )
    assert resp.status_code == 201
    res_id = resp.json()["id"]

    # Kade (on destination) sees can_edit=true on his own federated note.
    get = c.get(f"/api/resource/{res_id}", headers={"X-Dugg-Key": kade["api_key"]})
    notes = get.json()["resource"]["notes"]
    kade_note = next(n for n in notes if n["note"] == "Kade's note from home")
    assert kade_note["can_edit"] is True
    assert kade_note["can_delete"] is True
    # He can round-trip an actual edit too.
    edit = c.post(
        "/api/note/edit",
        json={"note_id": kade_note["id"], "text": "revised after federation"},
        headers={"X-Dugg-Key": kade["api_key"]},
    )
    assert edit.status_code == 200


def test_ingest_leaves_sibling_note_unattributed_when_name_does_not_match(client, db_path, user):
    """When the federated author's name matches no local user, the sibling
    note must stay unattributed (submitter_user_id=''). The `user` fixture
    gets can_edit=false even though they were the HTTP caller -- the
    delivery agent is not the author.
    """
    c, _ = client
    payload = {
        "source_instance_id": "origin-instance",
        "source_server": "https://home.example",
        "resource": {
            "url": "https://example.com/unknown-author",
            "title": "Entry from unknown",
            "note": "note text",
            "submitter_name": "NobodyHereNamedThis",
        },
    }
    resp = c.post(
        "/ingest",
        json=payload,
        headers={"X-Dugg-Key": user["api_key"]},
    )
    assert resp.status_code == 201
    res_id = resp.json()["id"]

    get = c.get(f"/api/resource/{res_id}", headers={"X-Dugg-Key": user["api_key"]})
    notes = get.json()["resource"]["notes"]
    n = next(nn for nn in notes if nn["note"] == "note text")
    assert n["can_edit"] is False
    assert n["can_delete"] is False


def test_remote_identity_link_widens_can_edit_for_federated_note(client, db_path, user):
    """The John/Sally case: two users with the same display name on
    different home servers must each only be able to edit their own
    federated notes. Identity is the (source_server, remote_user_id)
    pair, resolved through user_remote_identities -- not the name."""
    from dugg.db import DuggDB
    c, _ = client
    d = DuggDB(db_path)
    # Resource lives in a shared collection so the viewer (the `user`
    # fixture) can access it. We attribute notes via the link table,
    # not by name.
    kade = d.create_user("Kade")
    coll = d.create_collection("Shared", kade["id"], visibility="shared")
    d.invite_member(coll["id"], kade["id"], user["id"])
    res = d.add_resource(url="https://example.com/john", collection_id=coll["id"],
                         submitted_by=kade["id"], title="J")
    # Two federated sibling notes from two different "Johns" on two
    # different home servers. Both arrive with submitter_user_id='' and
    # submitter_name='John' but their remote identities differ.
    note_a = d.add_resource_note(
        res["id"], "John A's take",
        source_server="https://home-a.example",
        submitter_remote_id="john-on-home-a",
        submitter_name="John",
    )
    note_b = d.add_resource_note(
        res["id"], "John B's take",
        source_server="https://home-b.example",
        submitter_remote_id="john-on-home-b",
        submitter_name="John",
    )
    # Link the `user` fixture as John A -- they should now own note A
    # but NOT note B.
    d.link_remote_identity(
        local_user_id=user["id"],
        source_server="https://home-a.example",
        remote_user_id="john-on-home-a",
    )
    d.close()

    resp = c.get(f"/api/resource/{res['id']}", headers={"X-Dugg-Key": user["api_key"]})
    notes = {n["id"]: n for n in resp.json()["resource"]["notes"]}
    assert notes[note_a["id"]]["can_edit"] is True
    assert notes[note_a["id"]]["can_delete"] is True
    # Same name, different home, no link → not editable.
    assert notes[note_b["id"]]["can_edit"] is False
    assert notes[note_b["id"]]["can_delete"] is False

    # And the linked user can actually round-trip an edit.
    edit = c.post(
        "/api/note/edit",
        json={"note_id": note_a["id"], "text": "John A revised"},
        headers={"X-Dugg-Key": user["api_key"]},
    )
    assert edit.status_code == 200
    # While the unlinked path on note B is forbidden.
    forbidden = c.post(
        "/api/note/edit",
        json={"note_id": note_b["id"], "text": "hijack"},
        headers={"X-Dugg-Key": user["api_key"]},
    )
    assert forbidden.status_code == 403


def test_remote_identity_link_unique_first_write_wins(db_path):
    """A given (source_server, remote_user_id) pair can be linked to at
    most one local user. Subsequent link attempts with a different local
    user no-op. Same local user re-linking is idempotent."""
    from dugg.db import DuggDB
    d = DuggDB(db_path)
    a = d.create_user("Alice")
    b = d.create_user("Bob")
    assert d.link_remote_identity(a["id"], "https://home.example", "remote-1") is True
    # Same local user re-linking same pair → silent no-op (idempotent).
    assert d.link_remote_identity(a["id"], "https://home.example", "remote-1") is False
    # Different local user attempting same pair → also blocked by UNIQUE.
    assert d.link_remote_identity(b["id"], "https://home.example", "remote-1") is False
    # Pair still resolves to alice.
    assert d.lookup_remote_identity("https://home.example", "remote-1") == a["id"]
    d.close()


def test_invite_redeem_links_home_identity_when_provided(client, db_path, user):
    """Redemption of an invite token can carry the redeemer's home
    identity to auto-create a user_remote_identities link. This is the
    attested first-contact path -- not a generic claim endpoint."""
    from dugg.db import DuggDB
    c, _ = client
    d = DuggDB(db_path)
    invite = d.create_invite_token(user["id"], name_hint="Kade Remote")
    d.close()

    resp = c.post(
        f"/invite/{invite['token']}/redeem",
        json={
            "name": "Kade",
            "home_server": "https://home.example",
            "home_user_id": "kade-on-home",
        },
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code in (200, 201)
    redeemed_user_id = resp.json()["user"]["id"]

    d = DuggDB(db_path)
    linked = d.lookup_remote_identity("https://home.example", "kade-on-home")
    d.close()
    assert linked == redeemed_user_id


def test_admin_link_cli_creates_link(db_path):
    """`dugg admin link --user U --source-server S --remote-user-id R`
    creates a row in user_remote_identities. Admin path, no HTTP."""
    from argparse import Namespace
    from dugg.cli import cmd_admin_link
    from dugg.db import DuggDB
    d = DuggDB(db_path)
    kade = d.create_user("Kade")
    d.close()

    cmd_admin_link(Namespace(
        db=str(db_path),
        user=kade["id"],
        source_server="https://home.example",
        remote_user_id="kade-on-home",
    ))
    d = DuggDB(db_path)
    linked = d.lookup_remote_identity("https://home.example", "kade-on-home")
    d.close()
    assert linked == kade["id"]


def test_admin_claim_orphans_assigns_unattributed_notes(db_path):
    """`dugg admin claim-orphans --user U` stamps every empty
    submitter_user_id sibling note with the named user's id. Tombstoned
    rows are skipped."""
    from argparse import Namespace
    from dugg.cli import cmd_admin_claim_orphans
    from dugg.db import DuggDB
    d = DuggDB(db_path)
    kade = d.create_user("Kade")
    coll_id = d.ensure_default_collection(kade["id"])
    res = d.add_resource(url="https://example.com/orphan-host", collection_id=coll_id,
                         submitted_by=kade["id"], title="host")
    a = d.add_resource_note(res["id"], "orphan A", submitter_name="Kade")
    b = d.add_resource_note(res["id"], "orphan B", submitter_name="Stranger")
    # Soft-deleted orphans should NOT be claimed.
    c_note = d.add_resource_note(res["id"], "tombstoned orphan", submitter_name="Ghost")
    d.conn.execute("UPDATE resource_notes SET deleted_at = '2026-01-01' WHERE id = ?", (c_note["id"],))
    d.conn.commit()
    d.close()

    cmd_admin_claim_orphans(Namespace(db=str(db_path), user=kade["id"], dry_run=False))

    d = DuggDB(db_path)
    rows = {r["id"]: dict(r) for r in d.conn.execute(
        "SELECT id, submitter_user_id FROM resource_notes WHERE resource_id = ?",
        (res["id"],),
    ).fetchall()}
    d.close()
    assert rows[a["id"]]["submitter_user_id"] == kade["id"]
    assert rows[b["id"]]["submitter_user_id"] == kade["id"]
    # Tombstoned orphan stays untouched.
    assert rows[c_note["id"]]["submitter_user_id"] == ""


def test_feed_html_shows_per_note_edit_buttons_on_own_notes(client, db_path, user):
    """The browser feed must render inline edit/delete buttons on notes the
    viewer authored, and NOT on notes attributed to others. Regression for
    the note-swap UX: previously the card-level 'edit' button replaced the
    primary note regardless of who submitted it."""
    from dugg.db import DuggDB
    c, _ = client
    d = DuggDB(db_path)
    other = d.create_user("Other")
    coll = d.create_collection("Shared", user["id"], visibility="shared")
    d.invite_member(coll["id"], user["id"], other["id"])
    res = d.add_resource(url="https://example.com/x", collection_id=coll["id"],
                         submitted_by=user["id"], title="Shared",
                         note="my primary")
    other_note = d.add_resource_note(res["id"], "Other's take",
                                     submitter_user_id=other["id"],
                                     submitter_name=other["name"])
    my_note = d.add_resource_note(res["id"], "my sibling",
                                  submitter_user_id=user["id"],
                                  submitter_name=user["name"])
    d.close()

    # Authed via cookie-style: the HTML feed reads the key from cookie, so
    # we attach it as a cookie for the test client.
    c.cookies.set("dugg_key", user["api_key"])
    resp = c.get(f"/feed/{user['api_key']}", follow_redirects=True)
    assert resp.status_code == 200
    html = resp.text
    # Primary note (viewer is submitter) — row carries kind=primary and
    # ownership buttons are present.
    assert 'data-note-kind="primary"' in html
    assert f'data-resource-id="{res["id"]}"' in html
    # Sibling notes: viewer's own sibling row includes edit buttons.
    assert f'data-note-id="{my_note["id"]}"' in html
    # Use a slice focused on the viewer's sibling row to assert buttons.
    my_row_start = html.find(f'data-note-id="{my_note["id"]}"')
    my_row_end = html.find('</div>', my_row_start)
    my_row = html[my_row_start:my_row_end + 6]
    assert 'beginNoteEdit' in my_row
    assert 'deleteNoteRow' in my_row
    # Other's sibling row must NOT carry edit/delete buttons for this viewer.
    other_row_start = html.find(f'data-note-id="{other_note["id"]}"')
    other_row_end = html.find('</div>', other_row_start)
    other_row = html[other_row_start:other_row_end + 6]
    assert 'beginNoteEdit' not in other_row
    assert 'deleteNoteRow' not in other_row


def test_feed_html_has_add_note_button_on_every_card(client, db_path, user):
    """Every card must surface an 'add note' affordance — siblings are the
    only way a non-submitter can contribute to a shared entry."""
    c, _ = client
    res_id = _seed_resource(db_path, user)
    c.cookies.set("dugg_key", user["api_key"])
    resp = c.get(f"/feed/{user['api_key']}", follow_redirects=True)
    assert resp.status_code == 200
    html = resp.text
    assert 'add-note-btn' in html
    assert f'data-resource-id="{res_id}"' in html
    assert 'beginAddNote' in html


def test_api_resource_exposes_per_note_can_edit_delete(client, db_path, user):
    """iOS's per-note context menu keys off can_edit/can_delete on each
    note entry. Viewer gets true only for their own notes."""
    from dugg.db import DuggDB
    c, _ = client
    d = DuggDB(db_path)
    other = d.create_user("Other")
    coll = d.create_collection("Shared3", user["id"], visibility="shared")
    d.invite_member(coll["id"], user["id"], other["id"])
    res = d.add_resource(url="https://example.com/shared3", collection_id=coll["id"],
                         submitted_by=user["id"], title="s3", note="my primary")
    d.add_resource_note(res["id"], "other's sibling",
                        submitter_user_id=other["id"], submitter_name=other["name"])
    d.add_resource_note(res["id"], "my sibling",
                        submitter_user_id=user["id"], submitter_name=user["name"])
    d.close()

    resp = c.get(f"/api/resource/{res['id']}", headers={"X-Dugg-Key": user["api_key"]})
    notes = resp.json()["resource"]["notes"]
    by_text = {n["note"]: n for n in notes}
    # Primary note belongs to viewer → editable.
    assert by_text["my primary"]["can_edit"] is True
    assert by_text["my primary"]["can_delete"] is True
    # Other user's sibling → not editable.
    assert by_text["other's sibling"]["can_edit"] is False
    assert by_text["other's sibling"]["can_delete"] is False
    # Viewer's own sibling → editable.
    assert by_text["my sibling"]["can_edit"] is True
    assert by_text["my sibling"]["can_delete"] is True


def test_cli_cmd_edit_audits_human_edits(client, db_path, user):
    """Regression: `dugg edit` via CLI must audit like iOS /api/edit does.
    Previously the CLI call-site omitted actor_id, so human edits through
    the CLI silently bypassed the resource_edits log that moderators rely
    on for link-swap detection."""
    from argparse import Namespace
    from dugg.cli import cmd_edit

    c, _ = client
    res_id = _seed_resource(db_path, user, note="original", title="Original")

    args = Namespace(
        db=str(db_path),
        key=user["api_key"],
        target=res_id,
        title="CLI-edited title",
        note="CLI-edited note",
        description=None,
        author=None,
        source_type=None,
        tags=None,
    )
    cmd_edit(args)

    resp = c.get(f"/api/resource/{res_id}/edits",
                 headers={"X-Dugg-Key": user["api_key"]})
    assert resp.status_code == 200
    fields = {e["field"] for e in resp.json()["edits"]}
    assert fields == {"title", "note"}


def test_api_search_returns_structured_resources(client, db_path, user):
    c, _ = client
    _seed_resource(db_path, user, url="https://example.com/rust", title="rust performance")
    _seed_resource(db_path, user, url="https://example.com/py", title="python tricks")
    resp = c.get("/api/search?q=rust", headers={"X-Dugg-Key": user["api_key"]})
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] >= 1
    assert any("rust" in r["title"].lower() for r in data["resources"])


def test_api_feed_filter_query_params(client, db_path, user):
    c, _ = client
    unread_id = _seed_resource(db_path, user, url="https://example.com/filter-unread", title="Unread Item", note="")
    read_id = _seed_resource(db_path, user, url="https://example.com/filter-read", title="Read Item", note="")
    star_id = _seed_resource(db_path, user, url="https://example.com/filter-star", title="Star Item", note="")
    thumb_id = _seed_resource(db_path, user, url="https://example.com/filter-thumb", title="Thumb Item", note="")
    noted_id = _seed_resource(db_path, user, url="https://example.com/filter-note", title="Noted Item", note="my note")

    d = DuggDB(db_path)
    d.mark_read(user["id"], read_id, "cli")
    d.react_to_resource(star_id, user["id"], "star")
    d.react_to_resource(thumb_id, user["id"], "thumbsup")
    d.close()

    starred = c.get("/api/feed?filter=starred", headers={"X-Dugg-Key": user["api_key"]})
    assert starred.status_code == 200
    starred_ids = [resource["id"] for resource in starred.json()["resources"]]
    assert star_id in starred_ids
    assert thumb_id not in starred_ids

    read_resp = c.get("/api/feed?filter=read", headers={"X-Dugg-Key": user["api_key"]})
    assert read_resp.status_code == 200
    read_ids = [resource["id"] for resource in read_resp.json()["resources"]]
    assert read_id in read_ids
    assert unread_id not in read_ids

    noted_resp = c.get("/api/feed?filter=noted", headers={"X-Dugg-Key": user["api_key"]})
    assert noted_resp.status_code == 200
    noted_ids = [resource["id"] for resource in noted_resp.json()["resources"]]
    assert noted_id in noted_ids
    assert unread_id not in noted_ids


def test_api_search_filter_query_param_applies_on_top_of_query(client, db_path, user):
    c, _ = client
    starred_rust_id = _seed_resource(db_path, user, url="https://example.com/rust-star", title="rust starred", note="")
    _seed_resource(db_path, user, url="https://example.com/rust-plain", title="rust plain", note="")
    _seed_resource(db_path, user, url="https://example.com/python-star", title="python starred", note="")

    d = DuggDB(db_path)
    d.react_to_resource(starred_rust_id, user["id"], "star")
    d.close()

    resp = c.get("/api/search?q=rust&filter=starred", headers={"X-Dugg-Key": user["api_key"]})
    assert resp.status_code == 200
    resources = resp.json()["resources"]
    assert [resource["id"] for resource in resources] == [starred_rust_id]


def test_api_search_empty_query(client):
    c, user = client
    resp = c.get("/api/search?q=", headers={"X-Dugg-Key": user["api_key"]})
    assert resp.status_code == 200
    assert resp.json() == {"resources": [], "count": 0}


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
    assert data["detail"] == "Invalid invite token"


def test_invite_page_html(client):
    c, user = client
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
    assert len(data["quickstart"]) == 3


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
    assert "detail" in data


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
    assert "already has users" in resp.json()["detail"]


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


def test_resource_page_marks_read_on_render(client, db_path, user):
    c, _ = client
    res_id = _make_pasted_resource(db_path, user)
    resp = c.get(f"/r/{res_id}", headers={"X-Dugg-Key": user["api_key"]})
    assert resp.status_code == 200

    d = DuggDB(db_path)
    read_state = d.get_read_state(user["id"], res_id)
    d.close()
    assert read_state is not None
    assert read_state["source"] == "web_detail"


def test_resource_page_detail_renders_interactive_controls(client, db_path, user):
    c, _ = client
    d = DuggDB(db_path)
    coll_id = d.ensure_default_collection(user["id"])
    resource = d.add_resource(
        url="https://example.com/detail",
        collection_id=coll_id,
        submitted_by=user["id"],
        title="Detail Page",
        note="primary note",
        description="Detailed description",
        transcript="first line\nsecond line",
        source_type="article",
        author="Ada",
    )
    d.add_resource_note(
        resource["id"],
        "sibling note",
        submitter_user_id=user["id"],
        submitter_name=user["name"],
    )
    hidden = d.add_resource(
        url="dugg://content/secret-detail",
        collection_id=coll_id,
        submitted_by=user["id"],
        title="Secret Detail",
        description="Private description",
        transcript="private line",
        source_type="email",
        author="Ada",
    )
    d.close()

    c.cookies.set("dugg_key", user["api_key"])
    resp = c.get(f"/r/{resource['id']}")
    assert resp.status_code == 200
    html = resp.text
    assert 'data-reaction-type="star"' in html
    assert 'data-reaction-type="thumbsup"' in html
    assert 'shareResource(' in html
    assert 'class="add-note-form"' in html
    assert 'class="note-action-btn note-action-del"' in html

    hidden_resp = c.get(f"/r/{hidden['id']}")
    assert hidden_resp.status_code == 200
    hidden_html = hidden_resp.text
    assert "Open Original" not in hidden_html
    assert 'onclick="shareResource(' not in hidden_html


def test_resource_page_detail_title_links_only_for_external_urls(client, db_path, user):
    c, _ = client
    d = DuggDB(db_path)
    coll_id = d.ensure_default_collection(user["id"])
    external = d.add_resource(
        url="https://example.com/detail-title-link",
        collection_id=coll_id,
        submitted_by=user["id"],
        title="External Detail",
        source_type="article",
    )
    internal = d.add_resource(
        url="dugg://content/local-detail-title",
        collection_id=coll_id,
        submitted_by=user["id"],
        title="Local Detail",
        source_type="paste",
    )
    d.close()

    c.cookies.set("dugg_key", user["api_key"])

    external_resp = c.get(f"/r/{external['id']}")
    assert external_resp.status_code == 200
    assert '<h1><a class="detail-title-link" href="https://example.com/detail-title-link"' in external_resp.text

    internal_resp = c.get(f"/r/{internal['id']}")
    assert internal_resp.status_code == 200
    assert "<h1>Local Detail</h1>" in internal_resp.text
    assert '<h1><a class="detail-title-link"' not in internal_resp.text


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
    """Slack Block Kit button click fires a reaction and marks the item read."""
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
    read_state = d.get_read_state(user["id"], res["id"])
    assert reactions is not None
    assert reactions["total"] == 1
    assert reactions["breakdown"]["star"] == 1
    assert read_state is not None
    assert read_state["source"] == "slack_react_implicit"
    d.close()


def test_slack_actions_retired_tap_button_returns_ephemeral_error(client, db_path):
    c, user = client
    d = DuggDB(db_path)
    coll_id = d.ensure_default_collection(user["id"])
    res = d.add_resource(
        url="https://example.com/slack-retired-tap",
        collection_id=coll_id,
        submitted_by=user["id"],
        title="Slack Retired Tap",
    )
    d.close()

    payload = json.dumps({
        "type": "block_actions",
        "user": {"username": user["name"]},
        "actions": [{
            "action_id": "dugg_react_tap",
            "value": res["id"],
        }],
    })
    resp = c.post("/slack/actions", data={"payload": payload})
    assert resp.status_code == 200
    body = resp.json()
    assert body["response_type"] == "ephemeral"
    assert "This button has been retired." in body["text"]


def test_slack_command_feed_uses_mark_read_and_thumbs_up_labels(client, db_path, user):
    c, _ = client
    _seed_resource(db_path, user, url="https://example.com/slack-feed-buttons", title="Slack Feed Buttons")
    resp = c.post("/slack/command", data={"text": "", "user_name": user["name"]})
    assert resp.status_code == 200
    action_block = next(block for block in resp.json()["blocks"] if block["type"] == "actions")
    labels = [element["text"]["text"] for element in action_block["elements"]]
    action_ids = [element["action_id"] for element in action_block["elements"]]
    assert labels == [":book: Mark as Read", ":star: Star", ":+1: Thumbs Up"]
    assert action_ids == ["dugg_mark_read", "dugg_react_star", "dugg_react_thumbsup"]


def test_slack_actions_mark_read(client, db_path):
    c, user = client
    d = DuggDB(db_path)
    coll_id = d.ensure_default_collection(user["id"])
    res = d.add_resource(
        url="https://example.com/slack-mark-read",
        collection_id=coll_id,
        submitted_by=user["id"],
        title="Slack Mark Read",
    )
    d.close()

    payload = json.dumps({
        "type": "block_actions",
        "user": {"username": user["name"]},
        "actions": [{
            "action_id": "dugg_mark_read",
            "value": res["id"],
        }],
    })
    resp = c.post("/slack/actions", data={"payload": payload})
    assert resp.status_code == 200
    assert ":book: You marked *Slack Mark Read* as read" in resp.json()["text"]

    d = DuggDB(db_path)
    read_state = d.get_read_state(user["id"], res["id"])
    d.close()
    assert read_state is not None
    assert read_state["source"] == "slack_button"


# --- Session cookie auth (web feature-freeze lifted 2026-04-18) ---


def test_session_unlock_get_renders_form(client):
    c, _ = client
    resp = c.get("/session/unlock?return_to=/feed")
    assert resp.status_code == 200
    assert "<form" in resp.text
    assert 'action="/session/unlock"' in resp.text
    assert 'value="/feed"' in resp.text


def test_session_unlock_post_valid_sets_cookie(client, user):
    c, _ = client
    resp = c.post(
        "/session/unlock",
        data={"key": user["api_key"], "return_to": "/feed"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/feed"
    assert resp.cookies.get("dugg_key") == user["api_key"]


def test_session_unlock_post_invalid_key(client):
    c, _ = client
    resp = c.post("/session/unlock", data={"key": "dugg_wrong", "return_to": "/feed"})
    assert resp.status_code == 401
    assert "Invalid key" in resp.text


def test_session_unlock_rejects_external_return_to(client, user):
    """return_to must be a same-origin path; `//evil.com` or `http://...` fall back to /feed."""
    c, _ = client
    resp = c.post(
        "/session/unlock",
        data={"key": user["api_key"], "return_to": "//evil.com/x"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/feed"  # normalized, not the malicious value


def test_session_clear_deletes_cookie(client, user):
    c, _ = client
    # Set cookie
    c.post("/session/unlock", data={"key": user["api_key"], "return_to": "/feed"}, follow_redirects=False)
    assert c.cookies.get("dugg_key") == user["api_key"]
    # Clear it
    resp = c.get("/session/clear", follow_redirects=False)
    assert resp.status_code == 303
    # Starlette delete_cookie emits a Set-Cookie with empty value + expired Max-Age
    assert "dugg_key" in resp.headers.get("set-cookie", "")


# --- /feed: silent migration + cookie auth + Atom content-negotiation ---


def test_feed_html_silent_migrates_to_bare_path(client, user):
    """Browser visit to /feed/{key} sets cookie + 302 to /feed so key leaves URL bar."""
    c, _ = client
    resp = c.get(f"/feed/{user['api_key']}", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/feed"
    assert resp.cookies.get("dugg_key") == user["api_key"]


def test_feed_atom_still_serves_xml_on_key_path(client, user):
    """Atom clients (RSS readers) can't carry cookies — /feed/{key} must still serve XML."""
    c, _ = client
    resp = c.get(
        f"/feed/{user['api_key']}",
        headers={"Accept": "application/atom+xml"},
        follow_redirects=False,
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/atom+xml")
    assert "<feed" in resp.text


def test_feed_bare_redirects_to_unlock_without_cookie(client):
    c, _ = client
    resp = c.get("/feed", follow_redirects=False)
    assert resp.status_code == 303
    assert "/session/unlock" in resp.headers["location"]


def test_feed_bare_with_cookie_renders_html(client, user):
    c, _ = client
    c.cookies.set("dugg_key", user["api_key"])
    resp = c.get("/feed")
    assert resp.status_code == 200
    assert "Dugg" in resp.text
    # JS no longer reads the key from URL path
    assert "window.location.pathname.split('/feed/')" not in resp.text
    # Key must not appear in the rendered HTML
    assert user["api_key"] not in resp.text


def test_feed_html_includes_read_state_markup_and_reaction_buttons(client, db_path, user):
    c, _ = client
    res_id = _seed_resource(db_path, user, url="https://example.com/feed-read-state", title="Feed Read State")
    c.cookies.set("dugg_key", user["api_key"])
    resp = c.get("/feed")
    assert resp.status_code == 200
    assert f'data-dugg-resource-id="{res_id}"' in resp.text
    assert 'class="filter-row"' in resp.text
    assert '>Unread<' in resp.text
    assert '>Read<' in resp.text
    assert '>Starred<' in resp.text
    assert '>Thumbs Up<' in resp.text
    assert '>Noted by You<' in resp.text
    assert 'navigator.sendBeacon(\'/api/read/\'' in resp.text
    assert 'markRead(this)' in resp.text
    assert 'markUnread(this)' in resp.text
    assert 'function renderReadStateButton(resourceId, isRead, isPending)' in resp.text
    assert 'web_button' in resp.text
    assert 'class="action-btn reaction-btn read-state-btn mark-read-btn' in resp.text
    assert 'class="action-btn reaction-btn read-state-btn mark-unread-btn' not in resp.text
    assert 'data-reaction-type="star"' in resp.text
    assert '<span class="reaction-icon" aria-hidden="true">⭐</span>' in resp.text
    assert '☆' not in resp.text
    assert 'data-reaction-type="thumbsup"' in resp.text
    assert '>Thumbs Up<' in resp.text
    assert f'id="r-{res_id}"' in resp.text


def test_feed_html_renders_multiline_primary_note_with_note_text_span(client, db_path, user):
    c, _ = client
    _seed_resource(
        db_path,
        user,
        url="https://example.com/feed-multiline-note",
        title="Feed Multiline Note",
        note="Step 1: foo\nStep 2: bar",
    )

    c.cookies.set("dugg_key", user["api_key"])
    resp = c.get("/feed")
    assert resp.status_code == 200
    html = resp.text
    assert 'class="note-text"' in html
    assert "Step 1: foo" in html
    assert "Step 2: bar" in html


def test_feed_html_q_and_filter_render_server_side_results(client, db_path, user):
    c, _ = client
    starred_rust_id = _seed_resource(db_path, user, url="https://example.com/feed-rust-star", title="rust systems", note="")
    _seed_resource(db_path, user, url="https://example.com/feed-rust-plain", title="rust plain", note="")
    _seed_resource(db_path, user, url="https://example.com/feed-go-star", title="go starred", note="")

    d = DuggDB(db_path)
    d.react_to_resource(starred_rust_id, user["id"], "star")
    d.close()

    c.cookies.set("dugg_key", user["api_key"])
    resp = c.get("/feed?q=rust&filter=starred")
    assert resp.status_code == 200
    html = resp.text
    assert 'value="rust"' in html
    assert 'filter-pill is-active' in html
    assert f'data-resource-id="{starred_rust_id}"' in html
    assert "rust systems" in html
    assert "rust plain" not in html
    assert "go starred" not in html


def test_feed_html_category_row_uses_unfiltered_source_types_across_filters(client, db_path, user):
    c, _ = client
    starred_article_id = _seed_resource(
        db_path,
        user,
        url="https://example.com/feed-category-article",
        title="Starred article",
        note="",
        source_type="article",
    )
    youtube_id = _seed_resource(
        db_path,
        user,
        url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        title="Read video",
        note="",
        source_type="youtube",
    )

    d = DuggDB(db_path)
    d.react_to_resource(starred_article_id, user["id"], "star")
    d.mark_read(user["id"], youtube_id, "cli")
    d.close()

    c.cookies.set("dugg_key", user["api_key"])
    resp = c.get("/feed?filter=starred")
    assert resp.status_code == 200
    html = resp.text
    assert f'data-resource-id="{starred_article_id}"' in html
    assert f'data-resource-id="{youtube_id}"' not in html
    assert 'data-category="article"' in html
    assert 'data-category="youtube"' in html


# --- /paste: silent migration + cookie auth ---


def test_paste_key_path_silent_migrates(client, user):
    c, _ = client
    resp = c.get(f"/paste/{user['api_key']}", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/paste"
    assert resp.cookies.get("dugg_key") == user["api_key"]


def test_paste_bare_redirects_to_unlock_without_cookie(client):
    c, _ = client
    resp = c.get("/paste", follow_redirects=False)
    assert resp.status_code == 303
    assert "/session/unlock" in resp.headers["location"]


def test_paste_bare_with_cookie_renders_form(client, user):
    c, _ = client
    c.cookies.set("dugg_key", user["api_key"])
    resp = c.get("/paste")
    assert resp.status_code == 200
    assert 'action="/paste/submit"' in resp.text
    assert user["api_key"] not in resp.text


def test_paste_submit_bare_with_cookie(client, user):
    c, _ = client
    c.cookies.set("dugg_key", user["api_key"])
    resp = c.post(
        "/paste/submit",
        data={"title": "My note", "body": "Hello world", "source_type": "note"},
    )
    assert resp.status_code == 200
    assert "Saved" in resp.text


# --- /admin: silent migration + cookie auth ---


def test_admin_key_path_silent_migrates(client, user):
    c, _ = client
    resp = c.get(f"/admin/{user['api_key']}", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin"
    assert resp.cookies.get("dugg_key") == user["api_key"]


def test_admin_bare_redirects_to_unlock_without_cookie(client):
    c, _ = client
    resp = c.get("/admin", follow_redirects=False)
    assert resp.status_code == 303
    assert "/session/unlock" in resp.headers["location"]


def test_admin_bare_with_cookie_renders(client, user):
    c, _ = client
    c.cookies.set("dugg_key", user["api_key"])
    resp = c.get("/admin")
    assert resp.status_code == 200
    assert "Dugg Admin" in resp.text
    assert user["api_key"] not in resp.text


# --- /appeal: silent migration + cookie auth ---


def test_appeal_key_path_silent_migrates(client, user):
    c, _ = client
    resp = c.get(f"/appeal/{user['api_key']}", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/appeal"
    assert resp.cookies.get("dugg_key") == user["api_key"]


def test_appeal_bare_with_cookie_renders(client, user):
    c, _ = client
    c.cookies.set("dugg_key", user["api_key"])
    resp = c.get("/appeal")
    assert resp.status_code == 200
    # Not banned, so message is "No active bans"
    assert "No active bans" in resp.text


# --- Tools endpoint accepts cookie (not just X-Dugg-Key header) ---


def test_tools_endpoint_accepts_cookie(client, user):
    """Same-origin fetch() auto-sends the dugg_key cookie, so /tools/* must accept it."""
    c, _ = client
    c.cookies.set("dugg_key", user["api_key"])
    resp = c.post("/tools/dugg_search", json={"query": "x", "limit": 5})
    assert resp.status_code == 200
