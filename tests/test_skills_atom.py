import asyncio
import tempfile
from pathlib import Path

import feedparser
import pytest
from starlette.testclient import TestClient

from dugg.db import DuggDB
from dugg.http import create_app
from dugg.rss import FeedEntry, _entry_to_normalized, ingest_entry, sync_feed


VALID_SKILL = """---
name: atom-skill
description: Use this to verify Atom skill federation.
author: Skill Author
---
# Atom Skill

Procedure body with a tricky terminator ]]> inside.
"""


@pytest.fixture
def db():
    with tempfile.TemporaryDirectory() as tmpdir:
        d = DuggDB(Path(tmpdir) / "test.db")
        yield d
        d.close()


@pytest.fixture
def user_and_collection(db):
    user = db.create_user("Skill Tester")
    coll_id = db.ensure_default_collection(user["id"])
    return user, coll_id


def _add_skill(db, collection_id, user_id, *, name="atom-skill", description="Use this to verify Atom skill federation.", body="# Atom Skill\n\nProcedure body."):
    return db.add_skill(
        name=name,
        body=body,
        frontmatter={
            "name": name,
            "description": description,
            "author": "Skill Author",
        },
        title="Atom Skill",
        description=description,
        author="Skill Author",
        collection_id=collection_id,
        submitted_by=user_id,
    )


def _make_feed_entry(**overrides):
    payload = {
        "entry_id": "entry-1",
        "url": "https://example.com/post",
        "title": "Example Post",
        "description": "Post summary.",
        "published_at": "",
        "author": "Author",
        "is_private": False,
        "categories": [],
        "updated_at": "",
        "raw_content": "",
    }
    payload.update(overrides)
    return FeedEntry(**payload)


def test_render_feed_atom_includes_skill_category_and_cdata(db, user_and_collection):
    user, coll_id = user_and_collection
    db.set_config("server_url", "https://dugg.example")
    skill_id = db.add_skill(
        name="atom-skill",
        body="# Atom Skill\n\nProcedure body with ]]> terminator.",
        frontmatter={
            "name": "atom-skill",
            "description": "Use this to verify Atom skill federation.",
            "author": "Skill Author",
        },
        title="Atom Skill",
        description="Use this to verify Atom skill federation.",
        author="Skill Author",
        collection_id=coll_id,
        submitted_by=user["id"],
    )

    with TestClient(create_app(db.db_path)) as client:
        response = client.get(
            f"/feed/{user['api_key']}",
            headers={"accept": "application/atom+xml"},
        )

    text = response.text
    assert response.status_code == 200
    assert '<category term="dugg:skill"/>' in text
    assert f'<link href="https://dugg.example/s/{skill_id}"/>' in text
    assert "<content type=\"text\"><![CDATA[---" in text
    assert "Procedure body with ]]]]><![CDATA[> terminator." in text
    assert "<summary>Use this to verify Atom skill federation.</summary>" in text


def test_render_feed_atom_emits_skill_tombstone_with_viewer_link(db, user_and_collection):
    user, coll_id = user_and_collection
    db.set_config("server_url", "https://dugg.example")
    skill_id = _add_skill(db, coll_id, user["id"])
    db.delete_resource(skill_id, coll_id, user["id"])

    with TestClient(create_app(db.db_path)) as client:
        response = client.get(
            f"/feed/{user['api_key']}",
            headers={"accept": "application/atom+xml"},
        )

    text = response.text
    assert response.status_code == 200
    assert f'<at:deleted-entry ref="{skill_id}"' in text
    assert f'<link href="https://dugg.example/s/{skill_id}"/>' in text
    assert "<at:comment>Skill removed: Atom Skill</at:comment>" in text


def test_ingest_entry_routes_skill_entries_to_add_skill(db, user_and_collection):
    user, coll_id = user_and_collection
    entry = _make_feed_entry(
        entry_id="skill-1",
        url="https://server.example/s/skill-1",
        title="Atom Skill",
        description="Use this to verify Atom skill federation.",
        author="Remote Author",
        categories=["dugg:skill", "rss"],
        raw_content=VALID_SKILL,
    )

    result = ingest_entry(
        db,
        entry,
        collection_id=coll_id,
        submitted_by=user["id"],
        source_server="https://server.example",
    )

    skill = db.get_skill(result["id"])
    assert skill is not None
    assert skill["name"] == "atom-skill"
    assert skill["body"] == "# Atom Skill\n\nProcedure body with a tricky terminator ]]> inside.\n"
    stored = db.get_resource(result["id"])
    assert stored["source_type"] == "skill"
    assert stored["source_server"] == "https://server.example"
    assert db.conn.execute("SELECT COUNT(*) FROM skills WHERE resource_id = ?", (result["id"],)).fetchone()[0] == 1


def test_ingest_entry_routes_non_skill_entries_to_add_resource(db, user_and_collection):
    user, coll_id = user_and_collection
    entry = _make_feed_entry(
        entry_id="post-1",
        url="https://example.com/post",
        title="Plain Post",
        description="Post summary.",
        categories=["rss"],
    )

    result = ingest_entry(db, entry, collection_id=coll_id, submitted_by=user["id"])

    stored = db.get_resource(result["id"])
    assert stored["source_type"] == "article"
    assert db.conn.execute("SELECT COUNT(*) FROM skills WHERE resource_id = ?", (result["id"],)).fetchone()[0] == 0


def test_ingest_invalid_skill_payload_falls_back_to_resource_and_tags(db, user_and_collection):
    user, coll_id = user_and_collection
    entry = _make_feed_entry(
        entry_id="skill-bad",
        url="https://server.example/s/bad-skill",
        title="Broken Skill",
        description="Broken summary.",
        categories=["dugg:skill"],
        raw_content="# not valid skill markdown",
    )

    result = ingest_entry(db, entry, collection_id=coll_id, submitted_by=user["id"])

    stored = db.get_resource(result["id"])
    assert stored["source_type"] == "article"
    labels = {tag["label"] for tag in stored["tags"]}
    assert "dugg:skill" in labels
    assert "skill-invalid" in labels
    assert db.conn.execute("SELECT COUNT(*) FROM skills WHERE resource_id = ?", (result["id"],)).fetchone()[0] == 0


def test_round_trip_atom_skill_feed_ingests_matching_body():
    with tempfile.TemporaryDirectory() as tmpdir:
        server_a = DuggDB(Path(tmpdir) / "server-a.db")
        server_b = DuggDB(Path(tmpdir) / "server-b.db")
        try:
            user_a = server_a.create_user("Publisher")
            coll_a = server_a.ensure_default_collection(user_a["id"])
            server_a.set_config("server_url", "https://publisher.example")
            _add_skill(
                server_a,
                coll_a,
                user_a["id"],
                body="# Atom Skill\n\nRound-trip body.",
            )

            with TestClient(create_app(server_a.db_path)) as client:
                response = client.get(
                    f"/feed/{user_a['api_key']}",
                    headers={"accept": "application/atom+xml"},
                )

            parsed = feedparser.parse(response.content)
            normalized = _entry_to_normalized(parsed.entries[0])
            assert normalized is not None
            assert "dugg:skill" in normalized.categories

            user_b = server_b.create_user("Subscriber")
            coll_b = server_b.ensure_default_collection(user_b["id"])
            result = ingest_entry(
                server_b,
                normalized,
                collection_id=coll_b,
                submitted_by=user_b["id"],
                source_server="https://publisher.example",
            )

            skill = server_b.get_skill(result["id"])
            assert skill is not None
            assert skill["frontmatter"]["name"] == "atom-skill"
            assert skill["body"] == "# Atom Skill\n\nRound-trip body."
        finally:
            server_a.close()
            server_b.close()


def test_sync_feed_deletes_skill_tombstone_by_id(db, user_and_collection, monkeypatch):
    user, coll_id = user_and_collection
    skill_id = _add_skill(db, coll_id, user["id"])
    subscription = db.add_rss_subscription(
        user_id=user["id"],
        collection_id=coll_id,
        feed_url="https://remote.example/feed/key",
    )

    async def _mock_fetch(*args, **kwargs):
        return [], [
            type("Tomb", (), {
                "ref": skill_id,
                "when": "2026-04-19T12:00:00+00:00",
                "url": "https://remote.example/s/some-other-id",
            })()
        ], {
            "etag": "",
            "last_modified": "",
            "status": 200,
            "feed_title": "Remote Feed",
            "feed_description": "",
        }

    monkeypatch.setattr("dugg.rss.fetch_and_parse", _mock_fetch)

    result = asyncio.run(sync_feed(db, dict(subscription)))

    assert result["deleted"] == 1
    assert db.get_skill(skill_id) is None
