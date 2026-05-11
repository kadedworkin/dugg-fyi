"""Potential next-step hints for Dugg resources and operation results."""

from __future__ import annotations

from typing import Optional


FEDERATED_ORIGINS = {"broadcast", "rss", "subscribed"}

_ACTION_TEMPLATES = {
    "view": {
        "type": "ViewAction",
        "description": "Fetch full details of this resource",
        "mcpTool": "dugg_get",
        "httpEndpoint": "GET /api/resource/{id}",
    },
    "edit": {
        "type": "EditAction",
        "description": "Update this resource",
        "mcpTool": "dugg_edit",
        "httpEndpoint": "POST /api/edit",
    },
    "delete": {
        "type": "DeleteAction",
        "description": "Delete this resource",
        "mcpTool": "dugg_delete_resource",
        "httpEndpoint": "POST /tools/dugg_delete_resource",
    },
    "react": {
        "type": "ReactAction",
        "description": "React to this resource",
        "mcpTool": "dugg_react",
        "httpEndpoint": "POST /api/react",
    },
    "note": {
        "type": "AddNoteAction",
        "description": "Attach a note to this resource",
        "httpEndpoint": "POST /api/note",
    },
    "publish_note": {
        "type": "PublishNoteAction",
        "description": "Publish your note back to the source server",
        "httpEndpoint": "POST /publish-note",
    },
    "share": {
        "type": "ShareAction",
        "description": "Share this resource to other Dugg instances",
        "mcpTool": "dugg_share",
        "httpEndpoint": "POST /tools/dugg_share",
    },
    "add": {
        "type": "AddAction",
        "description": "Add a new URL to Dugg",
        "mcpTool": "dugg_add",
        "httpEndpoint": "POST /tools/dugg_add",
    },
    "filter": {
        "type": "FilterAction",
        "description": "Search or filter this result set",
        "mcpTool": "dugg_search",
        "httpEndpoint": "GET /api/search",
    },
    "list": {
        "type": "ListAction",
        "description": "Browse your current feed",
        "mcpTool": "dugg_feed",
        "httpEndpoint": "GET /api/feed",
    },
}

_DEFAULT_ACTION_KEYS = {
    "paste": ["view", "edit", "delete", "react", "note"],
    "add": ["view", "edit", "delete", "react", "note"],
    "list": ["add", "filter"],
    "search": ["add", "filter"],
    "get": ["edit", "delete", "react", "note", "share"],
    "edit": ["view", "delete", "react", "note", "share"],
    "delete": ["list", "add"],
    "react": ["view", "list"],
    "note": ["view"],
}


def _action_dict(action_key: str, server_scope: str, server_url: str = "") -> Optional[dict]:
    template = _ACTION_TEMPLATES.get(action_key)
    if not template:
        return None
    action = {
        "type": template["type"],
        "name": template.get("mcpTool") or action_key,
        "description": template["description"],
        "serverScope": server_scope,
    }
    if template.get("mcpTool"):
        action["mcpTool"] = template["mcpTool"]
    if template.get("httpEndpoint"):
        action["httpEndpoint"] = template["httpEndpoint"]
    if server_scope != "local" and server_url:
        action["serverUrl"] = server_url
    return action


def build_potential_actions(
    resource_type: str,
    operation: str,
    state: dict,
) -> list | None:
    del resource_type

    action_keys = list(_DEFAULT_ACTION_KEYS.get(operation, []))
    if not action_keys:
        return None

    agent_user_id = state.get("agent_user_id") or ""
    resource_owner_id = state.get("resource_owner_id") or ""
    resource_origin = state.get("resource_origin") or "local"
    source_server_url = state.get("source_server_url") or ""
    current_server_url = state.get("current_server_url") or ""
    agent_already_reacted = bool(state.get("agent_already_reacted"))
    is_federated = resource_origin in FEDERATED_ORIGINS

    if operation in {"add", "paste", "get", "edit"}:
        if not is_federated and agent_user_id and resource_owner_id and agent_user_id != resource_owner_id:
            action_keys = [key for key in action_keys if key not in {"edit", "delete"}]
        if agent_already_reacted:
            action_keys = [key for key in action_keys if key != "react"]

    actions: list[dict] = []
    for action_key in action_keys:
        if action_key == "note" and is_federated:
            note_action = _action_dict("note", "local", current_server_url)
            if note_action:
                actions.append(note_action)
            publish_action = _action_dict("publish_note", "source", source_server_url)
            if publish_action:
                actions.append(publish_action)
            continue

        if action_key in {"edit", "delete"} and is_federated:
            action = _action_dict(action_key, "source", source_server_url)
        else:
            action = _action_dict(action_key, "local", current_server_url)
        if action:
            actions.append(action)

    return actions or None
