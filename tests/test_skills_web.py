"""Tests for the skills web viewer surfaces."""

import os
import tempfile
from pathlib import Path

import pytest

from starlette.testclient import TestClient

from dugg.db import DuggDB
from dugg.http import create_app
from dugg.skills import render_skill_markdown


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
    return db.create_user("SkillUser")


@pytest.fixture
def default_collection(db, user):
    return db.create_collection("Default", user["id"])


@pytest.fixture
def client(db_path, db, user, default_collection):
    db.close()
    os.environ["DUGG_DB_PATH"] = str(db_path)
    import dugg.server as srv

    srv.db = None
    app = create_app(db_path=db_path)
    with TestClient(app) as c:
        yield c, user, default_collection
    srv.db = None


def _add_skill(
    db_path: Path,
    *,
    user: dict,
    collection: dict,
    name: str,
    title: str | None = None,
    description: str | None = None,
    body: str | None = None,
    author: str | None = None,
    is_exportable: bool = True,
    supersedes_id: str | None = None,
) -> str:
    d = DuggDB(db_path)
    try:
        return d.add_skill(
            name=name,
            body=body or f"Body for {name}\n",
            frontmatter={
                "name": name,
                "description": description or f"Description for {name}",
                "title": title or name,
            },
            title=title or name,
            description=description or f"Description for {name}",
            author=author or user["name"],
            collection_id=collection["id"],
            submitted_by=user["id"],
            supersedes_id=supersedes_id,
            is_exportable=is_exportable,
        )
    finally:
        d.close()


def test_get_skills_unauthenticated_returns_unlock_form(client):
    c, _, _ = client

    resp = c.get("/skills", follow_redirects=False)

    assert resp.status_code == 401
    assert 'action="/session/unlock"' in resp.text
    assert 'name="return_to" value="/skills"' in resp.text


def test_get_skills_authenticated_renders_skill_cards(client, db_path):
    c, user, default_collection = client
    first_id = _add_skill(
        db_path,
        user=user,
        collection=default_collection,
        name="alpha-skill",
        title="Alpha Skill",
        description="First skill card.",
    )
    second_id = _add_skill(
        db_path,
        user=user,
        collection=default_collection,
        name="beta-skill",
        title="Beta Skill",
        description="Second skill card.",
    )
    c.cookies.set("dugg_key", user["api_key"])

    resp = c.get("/skills")

    assert resp.status_code == 200
    assert f'href="/s/{first_id}"' in resp.text
    assert f'href="/s/{second_id}"' in resp.text
    assert "<code>alpha-skill</code>" in resp.text
    assert "<code>beta-skill</code>" in resp.text


def test_get_skill_unauthenticated_returns_unlock_form(client, db_path):
    c, user, default_collection = client
    skill_id = _add_skill(db_path, user=user, collection=default_collection, name="alpha-skill")

    resp = c.get(f"/s/{skill_id}", follow_redirects=False)

    assert resp.status_code == 401
    assert 'action="/session/unlock"' in resp.text
    assert f'name="return_to" value="/s/{skill_id}"' in resp.text


def test_get_skill_authenticated_renders_raw_body_verbatim(client, db_path):
    c, user, default_collection = client
    skill_id = _add_skill(
        db_path,
        user=user,
        collection=default_collection,
        name="raw-skill",
        title="Raw Skill",
        body="## Raw Step\n\nUse <tag> literally.\n",
    )
    c.cookies.set("dugg_key", user["api_key"])

    resp = c.get(f"/s/{skill_id}")

    assert resp.status_code == 200
    assert '<pre class="skill-body"' in resp.text
    assert "## Raw Step" in resp.text
    assert "&lt;tag&gt;" in resp.text
    assert "<h2>Raw Step</h2>" not in resp.text


def test_get_skill_markdown_download_returns_rendered_skill(client, db_path):
    c, user, default_collection = client
    body = "# Install\n\nProcedure body.\n"
    skill_id = _add_skill(
        db_path,
        user=user,
        collection=default_collection,
        name="install-me",
        title="Install Me",
        description="Installable skill",
        body=body,
    )
    c.cookies.set("dugg_key", user["api_key"])

    resp = c.get(f"/s/{skill_id}.md")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/markdown")
    assert resp.headers["content-disposition"] == 'attachment; filename="install-me.md"'
    assert resp.text == render_skill_markdown(
        {
            "name": "install-me",
            "description": "Installable skill",
            "title": "Install Me",
        },
        body,
    )


def test_get_skill_markdown_download_non_exportable_returns_403(client, db_path):
    c, user, default_collection = client
    skill_id = _add_skill(
        db_path,
        user=user,
        collection=default_collection,
        name="server-only",
        is_exportable=False,
    )
    c.cookies.set("dugg_key", user["api_key"])

    resp = c.get(f"/s/{skill_id}.md")

    assert resp.status_code == 403
    assert resp.json() == {"error": "Skill is not exportable."}


def test_get_missing_skill_returns_404(client):
    c, user, _ = client
    c.cookies.set("dugg_key", user["api_key"])

    resp = c.get("/s/missing-skill")

    assert resp.status_code == 404


def test_post_skill_unlock_sets_cookie_and_redirects(client, db_path):
    c, user, default_collection = client
    skill_id = _add_skill(db_path, user=user, collection=default_collection, name="unlock-me")

    resp = c.post(
        f"/s/{skill_id}/unlock",
        data={"key": user["api_key"]},
        follow_redirects=False,
    )

    assert resp.status_code == 303
    assert resp.headers["location"] == f"/s/{skill_id}"
    assert "dugg_key=" in resp.headers["set-cookie"]


def test_skills_pages_hide_inaccessible_collection_membership(client, db_path):
    c, user, default_collection = client
    visible_id = _add_skill(
        db_path,
        user=user,
        collection=default_collection,
        name="visible-skill",
        title="Visible Skill",
    )

    d = DuggDB(db_path)
    try:
        other_user = d.create_user("OtherUser")
        hidden_collection = d.create_collection("Private", other_user["id"])
    finally:
        d.close()
    hidden_id = _add_skill(
        db_path,
        user=other_user,
        collection=hidden_collection,
        name="hidden-skill",
        title="Hidden Skill",
    )

    c.cookies.set("dugg_key", user["api_key"])

    feed_resp = c.get("/skills")
    detail_resp = c.get(f"/s/{hidden_id}")

    assert feed_resp.status_code == 200
    assert visible_id in feed_resp.text
    assert hidden_id not in feed_resp.text
    assert detail_resp.status_code == 404
