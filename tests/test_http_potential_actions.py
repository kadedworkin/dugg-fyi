import tempfile
from pathlib import Path

from starlette.testclient import TestClient

from dugg.db import DuggDB
from dugg.http import create_app


def _decode_actions(payload):
    return payload.get("potentialAction") or []


def _action_by_type(payload, action_type):
    for action in _decode_actions(payload):
        if action["type"] == action_type:
            return action
    return None


def _make_client():
    tmpdir = tempfile.TemporaryDirectory()
    db_path = Path(tmpdir.name) / "test.db"
    d = DuggDB(db_path)
    user = d.create_user("TestUser")
    d.close()

    import os
    import dugg.server as srv

    os.environ["DUGG_DB_PATH"] = str(db_path)
    srv.db = None
    app = create_app(db_path=db_path)
    client = TestClient(app)
    return tmpdir, db_path, client, user


def _seed_local_resource(db_path, user, **overrides):
    d = DuggDB(db_path)
    coll_id = d.ensure_default_collection(user["id"])
    resource = d.add_resource(
        url=overrides.get("url", "https://example.com/post"),
        collection_id=coll_id,
        submitted_by=overrides.get("submitted_by", user["id"]),
        title=overrides.get("title", "Hello"),
        note=overrides.get("note", "my note"),
        description=overrides.get("description", "desc"),
        source_type=overrides.get("source_type", "article"),
    )
    d.close()
    return resource["id"]


def _seed_federated_resource(db_path, user, *, source_type="article", source_server="https://source.example"):
    d = DuggDB(db_path)
    coll_id = d.ensure_default_collection(user["id"])
    result = d.ingest_remote_publish(
        {
            "url": "https://remote.example/post",
            "title": "Remote Post",
            "description": "federated",
            "source_type": source_type,
            "note": "from remote",
        },
        "remote-instance",
        coll_id,
        source_server=source_server,
        submitted_by=user["id"],
    )
    d.close()
    return result["id"]


def test_tools_get_exposes_potential_actions_and_omits_dead_hints():
    tmpdir, db_path, client, user = _make_client()
    try:
        resource_id = _seed_local_resource(db_path, user)
        resp = client.post(
            "/tools/dugg_get",
            json={"resource_id": resource_id},
            headers={"X-Dugg-Key": user["api_key"]},
        )
        data = resp.json()

        names = [action["name"] for action in _decode_actions(data)]
        assert "dugg_delete_resource" in names
        assert "dugg_delete" not in names
        assert "dugg_export" not in names
    finally:
        client.close()
        tmpdir.cleanup()


def test_tools_search_includes_list_actions_and_omits_field_when_not_applicable():
    tmpdir, db_path, client, user = _make_client()
    try:
        _seed_local_resource(db_path, user, title="searchable title")
        resp = client.post(
            "/tools/dugg_search",
            json={"query": "searchable"},
            headers={"X-Dugg-Key": user["api_key"]},
        )
        data = resp.json()
        names = [action["name"] for action in _decode_actions(data)]
        assert names == ["dugg_add", "dugg_search"]

        no_actions = client.post(
            "/tools/dugg_collections",
            json={},
            headers={"X-Dugg-Key": user["api_key"]},
        ).json()
        assert "potentialAction" not in no_actions
    finally:
        client.close()
        tmpdir.cleanup()


def test_api_resource_filters_local_non_owner_and_reacted_state():
    tmpdir, db_path, client, owner = _make_client()
    try:
        d = DuggDB(db_path)
        viewer = d.create_user("Viewer")
        coll_id = d.ensure_default_collection(owner["id"])
        d.add_collection_member(coll_id, viewer["id"])
        resource_id = d.add_resource(
            url="https://example.com/shared",
            collection_id=coll_id,
            submitted_by=owner["id"],
            title="Shared",
            source_type="article",
        )["id"]
        d.react_to_resource(resource_id, viewer["id"], "star")
        d.close()

        resp = client.get(
            f"/api/resource/{resource_id}",
            headers={"X-Dugg-Key": viewer["api_key"]},
        )
        data = resp.json()
        names = [action["name"] for action in _decode_actions(data)]

        assert "dugg_edit" not in names
        assert "dugg_delete_resource" not in names
        assert "dugg_react" not in names
        assert _action_by_type(data, "ShareAction") is not None
    finally:
        client.close()
        tmpdir.cleanup()


def test_api_resource_routes_federated_actions_to_source():
    tmpdir, db_path, client, user = _make_client()
    try:
        resource_id = _seed_federated_resource(db_path, user, source_type="rss")
        resp = client.get(
            f"/api/resource/{resource_id}",
            headers={"X-Dugg-Key": user["api_key"]},
        )
        data = resp.json()

        edit_action = _action_by_type(data, "EditAction")
        delete_action = _action_by_type(data, "DeleteAction")
        react_action = _action_by_type(data, "ReactAction")
        publish_note = _action_by_type(data, "PublishNoteAction")

        assert edit_action["serverScope"] == "source"
        assert edit_action["serverUrl"] == "https://source.example"
        assert delete_action["serverScope"] == "source"
        assert react_action["serverScope"] == "local"
        assert publish_note["serverScope"] == "source"
    finally:
        client.close()
        tmpdir.cleanup()


def test_api_note_and_delete_include_operation_level_actions():
    tmpdir, db_path, client, user = _make_client()
    try:
        federated_id = _seed_federated_resource(db_path, user)
        note_resp = client.post(
            "/api/note",
            json={"resource_id": federated_id, "note": "local follow-up"},
            headers={"X-Dugg-Key": user["api_key"]},
        )
        note_data = note_resp.json()
        assert _action_by_type(note_data, "ViewAction") is not None
        assert _action_by_type(note_data, "PublishNoteAction") is not None

        local_id = _seed_local_resource(db_path, user, url="https://example.com/delete-me", title="Delete Me")
        delete_resp = client.post(
            "/delete",
            json={"url": "https://example.com/delete-me"},
            headers={"X-Dugg-Key": user["api_key"]},
        )
        delete_data = delete_resp.json()
        names = [action["name"] for action in _decode_actions(delete_data)]
        assert names == ["dugg_feed", "dugg_add"]

        feed_urls = client.get("/api/feed/urls", headers={"X-Dugg-Key": user["api_key"]}).json()
        assert "potentialAction" not in feed_urls
    finally:
        client.close()
        tmpdir.cleanup()
