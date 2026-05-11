import pytest

from dugg.potential_actions import build_potential_actions


def _names(actions):
    return [action["name"] for action in actions]


def test_build_potential_actions_uses_actual_tool_names_only():
    actions = build_potential_actions(
        "resource",
        "get",
        {
            "agent_user_id": "u1",
            "resource_owner_id": "u1",
            "resource_origin": "local",
            "current_server_url": "https://local.example",
        },
    )

    assert actions is not None
    names = _names(actions)
    assert "dugg_delete_resource" in names
    assert "dugg_delete" not in names
    assert "dugg_export" not in names
    note_actions = [action for action in actions if action["type"] == "AddNoteAction"]
    assert note_actions and "mcpTool" not in note_actions[0]


def test_build_potential_actions_suppresses_local_edit_delete_for_non_owner():
    actions = build_potential_actions(
        "resource",
        "get",
        {
            "agent_user_id": "viewer",
            "resource_owner_id": "owner",
            "resource_origin": "local",
            "current_server_url": "https://local.example",
        },
    )

    assert actions is not None
    names = _names(actions)
    assert "dugg_edit" not in names
    assert "dugg_delete_resource" not in names
    assert "dugg_react" in names


def test_build_potential_actions_suppresses_react_when_already_reacted():
    actions = build_potential_actions(
        "resource",
        "get",
        {
            "agent_user_id": "u1",
            "resource_owner_id": "u1",
            "resource_origin": "local",
            "current_server_url": "https://local.example",
            "agent_already_reacted": True,
        },
    )

    assert actions is not None
    assert "dugg_react" not in _names(actions)


def test_build_potential_actions_routes_federated_mutations_to_source():
    actions = build_potential_actions(
        "resource",
        "get",
        {
            "agent_user_id": "viewer",
            "resource_owner_id": "local-shadow-owner",
            "resource_origin": "rss",
            "source_server_url": "https://source.example",
            "current_server_url": "https://local.example",
        },
    )

    assert actions is not None
    by_type = {action["type"]: action for action in actions}
    assert by_type["EditAction"]["serverScope"] == "source"
    assert by_type["EditAction"]["serverUrl"] == "https://source.example"
    assert by_type["DeleteAction"]["serverScope"] == "source"
    assert by_type["ReactAction"]["serverScope"] == "local"
    assert by_type["AddNoteAction"]["serverScope"] == "local"
    assert by_type["PublishNoteAction"]["serverScope"] == "source"


def test_build_potential_actions_returns_none_for_unknown_operation():
    assert build_potential_actions("resource", "unknown", {}) is None
