"""Tests for Slack skill slash-command parity."""

import json
import os
import tempfile
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from dugg.db import DuggDB
from dugg.http import create_app


VALID_SKILL = """---
name: alpha-skill
description: Build an alpha workflow.
title: Alpha Skill
author: Skill Author
---
# Alpha

Use this when the agent needs the hidden body phrase.
"""


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
    db.close()
    os.environ["DUGG_DB_PATH"] = str(db_path)
    import dugg.server as srv

    srv.db = None
    app = create_app(db_path=db_path)
    with TestClient(app) as c:
        yield c, user
    srv.db = None


def _add_skill(db_path: Path, user: dict, *, name: str, title: str, description: str, body: str) -> str:
    d = DuggDB(db_path)
    coll_id = d.ensure_default_collection(user["id"])
    skill_id = d.add_skill(
        name=name,
        body=body,
        frontmatter={
            "name": name,
            "description": description,
            "title": title,
            "author": user["name"],
        },
        title=title,
        description=description,
        author=user["name"],
        collection_id=coll_id,
        submitted_by=user["id"],
    )
    d.close()
    return skill_id


def test_slack_skill_bare_list_returns_blocks_with_action_buttons(client, db_path):
    c, user = client
    _add_skill(
        db_path,
        user,
        name="alpha-skill",
        title="Alpha Skill",
        description="Build an alpha workflow.",
        body="# Alpha\n\nProcedure body.",
    )

    resp = c.post("/slack/command", data={"text": "skill", "user_name": user["name"]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["response_type"] == "in_channel"
    assert "Recent skills" in body["text"]
    action_block = next(block for block in body["blocks"] if block["type"] == "actions")
    assert [element["action_id"] for element in action_block["elements"]] == [
        "dugg_skill_view",
        "dugg_skill_install",
        "dugg_skill_fork",
    ]


def test_slack_skill_get_returns_ephemeral_code_block(client, db_path):
    c, user = client
    skill_id = _add_skill(
        db_path,
        user,
        name="alpha-skill",
        title="Alpha Skill",
        description="Build an alpha workflow.",
        body="# Alpha\n\nProcedure body.",
    )

    resp = c.post("/slack/command", data={"text": f"skill get {skill_id}", "user_name": user["name"]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["response_type"] == "ephemeral"
    assert body["text"].startswith("```markdown\n---\nname: alpha-skill")
    assert "# Alpha" in body["text"]


def test_slack_skill_search_matches_same_corpus_as_mcp_logic(client, db_path):
    c, user = client
    _add_skill(
        db_path,
        user,
        name="body-match",
        title="Quiet Title",
        description="Search body only.",
        body="This body contains neon cactus and more neon cactus.",
    )
    _add_skill(
        db_path,
        user,
        name="title-match",
        title="Neon Cactus Procedure",
        description="Search title too.",
        body="Additional body.",
    )

    resp = c.post("/slack/command", data={"text": "skill search neon cactus", "user_name": user["name"]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["response_type"] == "in_channel"
    section_texts = [block["text"]["text"] for block in body["blocks"] if block["type"] == "section"][1:]
    assert any("body-match" in text for text in section_texts)
    assert any("title-match" in text for text in section_texts)


def test_slack_skill_add_valid_markdown_creates_skill_and_returns_success_block(client, db_path):
    c, user = client

    resp = c.post("/slack/command", data={"text": f"skill add {VALID_SKILL}", "user_name": user["name"]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["response_type"] == "in_channel"
    assert "Added skill" in body["text"]
    assert any(block["type"] == "actions" for block in body["blocks"])

    d = DuggDB(db_path)
    coll_id = d.ensure_default_collection(user["id"])
    stored = d.get_skill("alpha-skill", collection_id=coll_id)
    d.close()
    assert stored is not None
    assert stored["title"] == "Alpha Skill"


def test_slack_skill_add_invalid_markdown_returns_ephemeral_error(client):
    c, user = client

    resp = c.post("/slack/command", data={"text": "skill add # missing frontmatter", "user_name": user["name"]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["response_type"] == "ephemeral"
    assert "Paste a full SKILL.md with frontmatter" in body["text"]


def test_slack_skill_view_action_returns_full_code_block(client, db_path):
    c, user = client
    skill_id = _add_skill(
        db_path,
        user,
        name="alpha-skill",
        title="Alpha Skill",
        description="Build an alpha workflow.",
        body="# Alpha\n\nProcedure body.",
    )

    payload = json.dumps({
        "type": "block_actions",
        "user": {"username": user["name"]},
        "actions": [{"action_id": "dugg_skill_view", "value": skill_id}],
    })
    resp = c.post("/slack/actions", data={"payload": payload})
    assert resp.status_code == 200
    body = resp.json()
    assert body["response_type"] == "ephemeral"
    assert body["text"].startswith("```markdown\n---\nname: alpha-skill")
    assert "Procedure body." in body["text"]


def test_slack_skill_unknown_subcommand_returns_help(client):
    c, user = client

    resp = c.post("/slack/command", data={"text": "skill bogus", "user_name": user["name"]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["response_type"] == "ephemeral"
    assert "Usage:" in body["text"]
