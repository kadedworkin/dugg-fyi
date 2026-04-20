"""Tests for MCP skill handlers."""

import json
import tempfile
from pathlib import Path

import pytest

from dugg.db import DuggDB
from dugg.server import (
    _handle_skill_add,
    _handle_skill_edit,
    _handle_skill_fork,
    _handle_skill_get,
    _handle_skill_install,
    _handle_skill_list,
    _handle_skill_search,
)


VALID_SKILL = """---
name: alpha-skill
description: Build an alpha workflow.
title: Alpha Skill
author: Skill Author
---
# Alpha

Use this when the agent needs the hidden body phrase.
"""


def _decode(result):
    assert len(result) == 1
    return json.loads(result[0].text)


@pytest.fixture
def db():
    with tempfile.TemporaryDirectory() as tmpdir:
        d = DuggDB(Path(tmpdir) / "test.db")
        yield d
        d.close()


@pytest.fixture
def owner(db):
    return db.create_user("Owner")


@pytest.fixture
def default_collection(db, owner):
    return db.create_collection("Default", owner["id"])


def _add_skill(
    db,
    *,
    user,
    collection,
    name,
    title=None,
    description=None,
    body=None,
    author=None,
    is_exportable=True,
    supersedes_id=None,
):
    return db.add_skill(
        name=name,
        body=body or f"Body for {name}",
        frontmatter={"name": name, "description": description or f"Description for {name}"},
        title=title or name,
        description=description or f"Description for {name}",
        author=author or user["name"],
        collection_id=collection["id"],
        submitted_by=user["id"],
        supersedes_id=supersedes_id,
        is_exportable=is_exportable,
    )


def test_skill_list_filters_by_collection_and_author_me(db, owner, default_collection):
    other_user = db.create_user("Other")
    other_collection = db.create_collection("Other", other_user["id"])
    db.add_collection_member(default_collection["id"], other_user["id"])

    own_skill = _add_skill(db, user=owner, collection=default_collection, name="mine")
    _add_skill(db, user=other_user, collection=default_collection, name="theirs")
    _add_skill(db, user=other_user, collection=other_collection, name="other-space")

    payload = _decode(_handle_skill_list(db, owner["id"], {
        "collection": "Default",
        "author": "me",
        "limit": 20,
    }))

    assert [item["id"] for item in payload] == [own_skill]
    assert payload[0]["name"] == "mine"


def test_skill_get_by_id_and_name_and_not_found(db, owner, default_collection):
    skill_id = _add_skill(
        db,
        user=owner,
        collection=default_collection,
        name="alpha-skill",
        title="Alpha Skill",
        description="Build an alpha workflow.",
        body="# Alpha\n\nProcedure body.",
        author="Skill Author",
    )

    by_id = _decode(_handle_skill_get(db, owner["id"], {"id_or_name": skill_id}))
    assert by_id["id"] == skill_id
    assert by_id["name"] == "alpha-skill"
    assert by_id["markdown"].startswith("---\nname: alpha-skill")

    by_name = _decode(_handle_skill_get(db, owner["id"], {
        "id_or_name": "alpha-skill",
        "collection": "Default",
    }))
    assert by_name["id"] == skill_id

    missing = _decode(_handle_skill_get(db, owner["id"], {"id_or_name": "missing"}))
    assert missing == {"error": "Skill not found: missing"}


def test_skill_search_matches_title_and_body_with_filter_and_dedupe(db, owner, default_collection):
    body_match = _add_skill(
        db,
        user=owner,
        collection=default_collection,
        name="body-match",
        title="Quiet title",
        body="This body contains neon cactus and more neon cactus.",
    )
    both_match = _add_skill(
        db,
        user=owner,
        collection=default_collection,
        name="title-and-body",
        title="Neon Cactus Procedure",
        body="neon cactus appears here too",
    )
    other_collection = db.create_collection("Elsewhere", owner["id"])
    _add_skill(
        db,
        user=owner,
        collection=other_collection,
        name="elsewhere-skill",
        title="Neon elsewhere",
        body="outside the filtered collection",
    )

    payload = _decode(_handle_skill_search(db, owner["id"], {
        "query": "neon cactus",
        "collection": "Default",
        "limit": 10,
    }))

    ids = [item["id"] for item in payload]
    assert set(ids) == {body_match, both_match}
    assert ids.count(both_match) == 1


def test_skill_install_returns_markdown_and_blocks_non_exportable(db, owner, default_collection):
    exportable = _add_skill(
        db,
        user=owner,
        collection=default_collection,
        name="install-me",
        title="Install Me",
        description="Installable skill",
        body="# Install Me\n\nBody.",
        is_exportable=True,
    )
    locked = _add_skill(
        db,
        user=owner,
        collection=default_collection,
        name="locked-skill",
        is_exportable=False,
    )

    payload = _decode(_handle_skill_install(db, owner["id"], {
        "id_or_name": exportable,
        "to_filename": "custom/{name}.md",
    }))
    assert payload["name"] == "install-me"
    assert payload["filename"] == "custom/install-me.md"
    assert payload["markdown"].startswith("---\nname: install-me")

    error = _decode(_handle_skill_install(db, owner["id"], {"id_or_name": locked}))
    assert error == {"error": f"Skill {locked} is not exportable."}


def test_skill_add_creates_skill_and_rejects_invalid_markdown(db, owner, default_collection):
    created = _decode(_handle_skill_add(db, owner["id"], {"markdown": VALID_SKILL}))
    stored = db.get_skill(created["id"])

    assert created["name"] == "alpha-skill"
    assert created["collection_id"] == default_collection["id"]
    assert stored is not None
    assert stored["name"] == "alpha-skill"

    error = _decode(_handle_skill_add(db, owner["id"], {"markdown": "# no frontmatter"}))
    assert "Invalid SKILL.md" in error["error"]


def test_skill_fork_sets_supersedes_and_rejects_subscriber_target(db, owner, default_collection):
    source_id = _add_skill(
        db,
        user=owner,
        collection=default_collection,
        name="fork-source",
        title="Fork Source",
        description="fork this",
        body="# Fork Source\n\nProcedure.",
    )

    forker = db.create_user("Forker")
    db.add_collection_member(default_collection["id"], forker["id"])
    forker_default = db.create_collection("Forker Default", forker["id"])

    forked = _decode(_handle_skill_fork(db, forker["id"], {
        "source_id": source_id,
        "target_collection": "Forker Default",
    }))
    stored = db.get_skill(forked["id"])
    assert forked["supersedes_id"] == source_id
    assert stored is not None
    assert stored["supersedes_id"] == source_id

    subscriber = db.create_user("Subscriber")
    db.add_collection_member(
        default_collection["id"],
        subscriber["id"],
        member_type="subscriber",
    )
    rejected = _decode(_handle_skill_fork(db, subscriber["id"], {"source_id": source_id}))
    assert rejected == {"error": f"Write access denied for collection {default_collection['id']}"}


def test_skill_fork_is_idempotent_for_same_target_and_submitter(db, owner, default_collection):
    source_id = _add_skill(db, user=owner, collection=default_collection, name="fork-source")

    first = _decode(_handle_skill_fork(db, owner["id"], {"source_id": source_id}))
    second = _decode(_handle_skill_fork(db, owner["id"], {"source_id": source_id}))

    assert second["status"] == "exists"
    assert second["id"] == first["id"]


def test_skill_edit_creates_new_version_for_submitter(db, owner, default_collection):
    source_id = _add_skill(
        db,
        user=owner,
        collection=default_collection,
        name="editable-skill",
        title="Editable Skill",
        description="Original body",
        body="# Editable\n\nOriginal body.\n",
    )

    edited = _decode(_handle_skill_edit(db, owner["id"], {
        "id": source_id,
        "new_body": """---
name: editable-skill
description: Edited body
title: Editable Skill
---
# Editable

Edited body.
""",
    }))

    stored = db.get_skill(edited["id"])
    assert stored is not None
    assert stored["supersedes_id"] == source_id
    assert stored["body"] == "# Editable\n\nEdited body.\n"


def test_skill_edit_allows_collection_owner_and_rejects_other_members(db, owner, default_collection):
    author = db.create_user("Author")
    db.add_collection_member(default_collection["id"], author["id"])
    source_id = _add_skill(db, user=author, collection=default_collection, name="owner-editable")

    owner_edit = _decode(_handle_skill_edit(db, owner["id"], {
        "id": source_id,
        "new_body": """---
name: owner-editable
description: owner edit
---
owner body
""",
    }))
    assert owner_edit["supersedes_id"] == source_id

    member = db.create_user("Member")
    db.add_collection_member(default_collection["id"], member["id"])
    denied = _decode(_handle_skill_edit(db, member["id"], {
        "id": source_id,
        "new_body": """---
name: owner-editable
description: member edit
---
member body
""",
    }))
    assert denied == {
        "error": "Permission denied — you didn't submit this skill and aren't the collection owner."
    }


def test_skill_edit_rejects_demoted_submitter(db, owner, default_collection):
    contributor = db.create_user("Contributor")
    db.add_collection_member(default_collection["id"], contributor["id"])
    source_id = _add_skill(db, user=contributor, collection=default_collection, name="demoted-submitter")
    db.conn.execute(
        "UPDATE collection_members SET member_type = 'subscriber' WHERE collection_id = ? AND user_id = ?",
        (default_collection["id"], contributor["id"]),
    )
    db.conn.commit()

    denied = _decode(_handle_skill_edit(db, contributor["id"], {
        "id": source_id,
        "new_body": """---
name: demoted-submitter
description: Attempted update
---
should not land
""",
    }))
    assert denied == {
        "error": "Permission denied — you didn't submit this skill and aren't the collection owner."
    }
