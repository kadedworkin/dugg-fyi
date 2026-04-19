"""Tests for the Dugg skills layer (parse + DB)."""

import tempfile
from pathlib import Path

import pytest

from dugg.db import DuggDB
from dugg.skills import (
    parse_skill_markdown,
    render_skill_markdown,
    validate_skill_name,
)


@pytest.fixture
def db():
    with tempfile.TemporaryDirectory() as tmpdir:
        d = DuggDB(Path(tmpdir) / "test.db")
        yield d
        d.close()


VALID_SKILL = """---
name: cinematic-hero
description: Build a cinematic-landing hero section with autoplay video and bold type.
when-to-use: New landing-page hero for a filmic-aesthetic product
tags: [landing, video, hero]
---
# Cinematic Hero

1. 12s muted autoplay loop at 1080p.
2. Pair an oversized display serif with a small-caps sans.
3. Warm, high-contrast hero imagery, dark background.
"""


def test_parse_skill_markdown_basic():
    frontmatter, body = parse_skill_markdown(VALID_SKILL)
    assert frontmatter["name"] == "cinematic-hero"
    assert "autoplay" in frontmatter["description"]
    assert frontmatter["tags"] == ["landing", "video", "hero"]
    assert body.startswith("# Cinematic Hero")
    assert "12s muted autoplay" in body


def test_parse_skill_markdown_missing_frontmatter():
    with pytest.raises(ValueError, match="frontmatter"):
        parse_skill_markdown("# just a body\n\nno frontmatter here")


def test_parse_skill_markdown_missing_name():
    text = "---\ndescription: hello\n---\nbody\n"
    with pytest.raises(ValueError, match="name"):
        parse_skill_markdown(text)


def test_parse_skill_markdown_missing_description():
    text = "---\nname: foo\n---\nbody\n"
    with pytest.raises(ValueError, match="description"):
        parse_skill_markdown(text)


def test_parse_skill_markdown_empty_required():
    text = "---\nname: foo\ndescription: ''\n---\nbody\n"
    with pytest.raises(ValueError, match="description"):
        parse_skill_markdown(text)


def test_parse_skill_markdown_bad_yaml():
    text = "---\nname: foo\ndescription: [unclosed\n---\nbody\n"
    with pytest.raises(ValueError, match="valid YAML"):
        parse_skill_markdown(text)


def test_validate_skill_name_accepts_good():
    for name in ["foo", "cinematic-hero", "abc-123-def", "x" + "a" * 60]:
        validate_skill_name(name)


def test_validate_skill_name_rejects_bad():
    bad = [
        "Foo",
        "foo_bar",
        "foo bar",
        "1foo",
        "-foo",
        "a",
        "x" + "a" * 64,
        "",
        "foo/bar",
    ]
    for name in bad:
        with pytest.raises(ValueError):
            validate_skill_name(name)


def test_render_roundtrip():
    frontmatter, body = parse_skill_markdown(VALID_SKILL)
    rendered = render_skill_markdown(frontmatter, body)
    reparsed_fm, reparsed_body = parse_skill_markdown(rendered)
    assert reparsed_fm["name"] == frontmatter["name"]
    assert reparsed_fm["description"] == frontmatter["description"]
    assert "Cinematic Hero" in reparsed_body


def _seed_user_and_collection(db, user_name="Author", collection_name="Skills"):
    user = db.create_user(user_name)
    coll = db.create_collection(collection_name, user["id"])
    return user, coll


def test_add_and_get_skill(db):
    user, coll = _seed_user_and_collection(db)
    frontmatter, body = parse_skill_markdown(VALID_SKILL)
    skill_id = db.add_skill(
        name=frontmatter["name"],
        body=body,
        frontmatter=frontmatter,
        title="Cinematic Hero",
        description=frontmatter["description"],
        author=user["name"],
        collection_id=coll["id"],
        submitted_by=user["id"],
    )
    assert skill_id

    got = db.get_skill(skill_id)
    assert got is not None
    assert got["name"] == "cinematic-hero"
    assert got["title"] == "Cinematic Hero"
    assert got["source_type"] == "skill"
    assert got["body"] == body
    assert got["frontmatter"]["tags"] == ["landing", "video", "hero"]
    assert got["is_exportable"] is True
    assert got["url"].startswith("skill://")

    by_name = db.get_skill("cinematic-hero", collection_id=coll["id"])
    assert by_name is not None
    assert by_name["id"] == skill_id


def test_get_skill_unknown_returns_none(db):
    user, coll = _seed_user_and_collection(db)
    assert db.get_skill("nope") is None
    assert db.get_skill("nope", collection_id=coll["id"]) is None


def test_list_skills_filters(db):
    u1 = db.create_user("Alice")
    u2 = db.create_user("Bob")
    c1 = db.create_collection("Alpha", u1["id"])
    c2 = db.create_collection("Beta", u1["id"])

    def _add(name, user, coll):
        db.add_skill(
            name=name,
            body="body for " + name,
            frontmatter={"name": name, "description": f"desc for {name}"},
            title=name,
            description=f"desc for {name}",
            author=user["name"],
            collection_id=coll["id"],
            submitted_by=user["id"],
        )

    _add("alpha-one", u1, c1)
    _add("alpha-two", u2, c1)
    _add("beta-one", u1, c2)

    all_skills = db.list_skills()
    assert len(all_skills) == 3

    alpha_only = db.list_skills(collection_id=c1["id"])
    assert len(alpha_only) == 2
    assert {s["name"] for s in alpha_only} == {"alpha-one", "alpha-two"}

    u1_only = db.list_skills(author_user_id=u1["id"])
    assert len(u1_only) == 2
    assert {s["name"] for s in u1_only} == {"alpha-one", "beta-one"}

    alpha_by_u1 = db.list_skills(collection_id=c1["id"], author_user_id=u1["id"])
    assert len(alpha_by_u1) == 1
    assert alpha_by_u1[0]["name"] == "alpha-one"


def test_list_skills_excludes_body(db):
    user, coll = _seed_user_and_collection(db)
    db.add_skill(
        name="no-body",
        body="secret body content",
        frontmatter={"name": "no-body", "description": "d"},
        title="No Body",
        description="d",
        author=user["name"],
        collection_id=coll["id"],
        submitted_by=user["id"],
    )
    listed = db.list_skills()
    assert len(listed) == 1
    assert "body" not in listed[0]


def test_skill_supersedes_chain(db):
    user, coll = _seed_user_and_collection(db)
    v1 = db.add_skill(
        name="chain",
        body="v1",
        frontmatter={"name": "chain", "description": "v1"},
        title="Chain",
        description="v1",
        author=user["name"],
        collection_id=coll["id"],
        submitted_by=user["id"],
    )
    v2 = db.add_skill(
        name="chain",
        body="v2",
        frontmatter={"name": "chain", "description": "v2"},
        title="Chain",
        description="v2",
        author=user["name"],
        collection_id=coll["id"],
        submitted_by=user["id"],
        supersedes_id=v1,
    )

    v1_skill = db.get_skill(v1)
    v2_skill = db.get_skill(v2)
    assert v1_skill["supersedes_id"] is None
    assert v2_skill["supersedes_id"] == v1


def test_skill_name_unique_across_collections_allowed(db):
    """Same skill name is allowed in different collections (fork use case)."""
    user = db.create_user("Author")
    c1 = db.create_collection("One", user["id"])
    c2 = db.create_collection("Two", user["id"])
    db.add_skill(
        name="shared",
        body="a",
        frontmatter={"name": "shared", "description": "a"},
        title="Shared",
        description="a",
        author=user["name"],
        collection_id=c1["id"],
        submitted_by=user["id"],
    )
    db.add_skill(
        name="shared",
        body="b",
        frontmatter={"name": "shared", "description": "b"},
        title="Shared",
        description="b",
        author=user["name"],
        collection_id=c2["id"],
        submitted_by=user["id"],
    )
    assert len(db.list_skills()) == 2


def test_skill_reactions_work_via_resource_id(db):
    """Skills participate in the shared polymorphic reactions table."""
    user, coll = _seed_user_and_collection(db)
    skill_id = db.add_skill(
        name="react-me",
        body="body",
        frontmatter={"name": "react-me", "description": "d"},
        title="React Me",
        description="d",
        author=user["name"],
        collection_id=coll["id"],
        submitted_by=user["id"],
    )
    db.react_to_resource(resource_id=skill_id, user_id=user["id"], reaction_type="star")
    reactions = db.get_reactions(skill_id, user["id"])
    assert reactions is not None
    assert reactions["total"] == 1


def test_add_skill_emits_skill_added_event(db):
    user, coll = _seed_user_and_collection(db)
    skill_id = db.add_skill(
        name="emitter",
        body="b",
        frontmatter={"name": "emitter", "description": "d"},
        title="Emitter",
        description="d",
        author=user["name"],
        collection_id=coll["id"],
        submitted_by=user["id"],
    )
    rows = db.conn.execute(
        "SELECT event_type, actor_id, collection_id FROM event_log WHERE event_type LIKE 'skill_%'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["event_type"] == "skill_added"
    assert rows[0]["actor_id"] == user["id"]
    assert rows[0]["collection_id"] == coll["id"]

    fork_id = db.add_skill(
        name="emitter",
        body="b2",
        frontmatter={"name": "emitter", "description": "d2"},
        title="Emitter",
        description="d2",
        author=user["name"],
        collection_id=coll["id"],
        submitted_by=user["id"],
        supersedes_id=skill_id,
    )
    rows = db.conn.execute(
        "SELECT event_type FROM event_log WHERE event_type LIKE 'skill_%' ORDER BY created_at"
    ).fetchall()
    assert [r["event_type"] for r in rows] == ["skill_added", "skill_forked"]
