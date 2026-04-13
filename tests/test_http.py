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
    assert "API Key" in resp.json()["result"]


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
