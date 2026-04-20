"""Tests for CLI skill versioning commands."""

import tempfile
from argparse import Namespace
from pathlib import Path

import pytest

from dugg.cli import cmd_skill_edit, cmd_skill_fork, cmd_skill_history
from dugg.db import DuggDB


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
def owner(db):
    return db.create_user("Owner")


@pytest.fixture
def default_collection(db, owner):
    return db.create_collection("Default", owner["id"])


def _args(db_path: Path, user: dict, **overrides) -> Namespace:
    base = {
        "db": str(db_path),
        "key": user["api_key"],
        "collection": None,
        "name": None,
        "id": None,
    }
    base.update(overrides)
    return Namespace(**base)


def _add_skill(db, *, user, collection, name, body="body\n", supersedes_id=None):
    return db.add_skill(
        name=name,
        body=body,
        frontmatter={"name": name, "description": f"Description for {name}", "title": name},
        title=name,
        description=f"Description for {name}",
        author=user["name"],
        collection_id=collection["id"],
        submitted_by=user["id"],
        supersedes_id=supersedes_id,
    )


def test_cmd_skill_fork_is_idempotent(db_path, db, owner, default_collection, capsys):
    source_id = _add_skill(db, user=owner, collection=default_collection, name="forkable")

    cmd_skill_fork(_args(db_path, owner, id=source_id))
    first = capsys.readouterr().out
    cmd_skill_fork(_args(db_path, owner, id=source_id))
    second = capsys.readouterr().out

    assert "Forked skill forkable" in first
    assert "Fork already exists" in second


def test_cmd_skill_edit_versions_skill_through_editor(db_path, db, owner, default_collection, monkeypatch, capsys):
    source_id = _add_skill(db, user=owner, collection=default_collection, name="editable", body="old body\n")

    def _fake_run(cmd, check):
        target = Path(cmd[-1])
        target.write_text(
            """---
name: editable
description: Updated description
title: editable
---
new body
""",
            encoding="utf-8",
        )

        class _Result:
            returncode = 0

        return _Result()

    monkeypatch.setattr("dugg.cli.subprocess.run", _fake_run)
    monkeypatch.setenv("EDITOR", "fake-editor")

    cmd_skill_edit(_args(db_path, owner, id=source_id))
    output = capsys.readouterr().out

    d = DuggDB(db_path)
    try:
        edited = d.find_skill_version(
            collection_id=default_collection["id"],
            submitted_by=owner["id"],
            name="editable",
            supersedes_id=source_id,
        )
        assert edited is not None
        rows = d.get_skill_history(edited["id"])
        assert len(rows) == 2
        assert rows[0]["supersedes_id"] == source_id
        assert rows[0]["body"] == "new body\n"
    finally:
        d.close()
    assert "Edited skill editable" in output


def test_cmd_skill_fork_rejects_non_member_target_collection(db_path, db, owner, default_collection, capsys):
    outsider = db.create_user("Outsider")
    outsider_collection = db.create_collection("Outsider's", outsider["id"])
    source_id = _add_skill(db, user=outsider, collection=outsider_collection, name="plantable")

    with pytest.raises(SystemExit) as exc:
        cmd_skill_fork(
            _args(db_path, outsider, id=source_id, collection=default_collection["id"])
        )

    assert exc.value.code == 1
    assert f"Collection not found: {default_collection['id']}" in capsys.readouterr().out
    d = DuggDB(db_path)
    try:
        planted = d.find_skill_version(
            collection_id=default_collection["id"],
            submitted_by=outsider["id"],
            name="plantable",
            supersedes_id=source_id,
        )
        assert planted is None
    finally:
        d.close()


def test_cmd_skill_edit_rejects_demoted_submitter(db_path, db, owner, default_collection, monkeypatch, capsys):
    contributor = db.create_user("Contributor")
    db.add_collection_member(default_collection["id"], contributor["id"])
    source_id = _add_skill(db, user=contributor, collection=default_collection, name="demoted")
    db.conn.execute(
        "UPDATE collection_members SET member_type = 'subscriber' WHERE collection_id = ? AND user_id = ?",
        (default_collection["id"], contributor["id"]),
    )
    db.conn.commit()

    def _fail_editor(cmd, check):
        raise AssertionError("Editor should not be invoked when permission is denied")

    monkeypatch.setattr("dugg.cli.subprocess.run", _fail_editor)

    with pytest.raises(SystemExit) as exc:
        cmd_skill_edit(_args(db_path, contributor, id=source_id))

    assert exc.value.code == 1
    assert "Permission denied" in capsys.readouterr().out


def test_cmd_skill_history_prints_chain(db_path, db, owner, default_collection, capsys):
    original_id = _add_skill(db, user=owner, collection=default_collection, name="history-skill")
    current_id = _add_skill(
        db,
        user=owner,
        collection=default_collection,
        name="history-skill",
        supersedes_id=original_id,
    )

    cmd_skill_history(_args(db_path, owner, id=current_id))
    output = capsys.readouterr().out

    assert "Version history:" in output
    assert current_id[:8] in output
    assert original_id[:8] in output
