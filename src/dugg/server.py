"""Dugg MCP Server - Agentic-first shared knowledge base."""

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Optional

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from dugg.db import DuggDB
from dugg.enrichment import enrich_url

# --- Server Setup ---

db: Optional[DuggDB] = None
server = Server("dugg")


def get_db() -> DuggDB:
    global db
    if db is None:
        db_path = os.environ.get("DUGG_DB_PATH")
        db = DuggDB(Path(db_path) if db_path else None)
    return db


def resolve_user(api_key: Optional[str] = None) -> dict:
    """Resolve user from API key, or create/use default local user."""
    d = get_db()
    if api_key:
        user = d.get_user_by_api_key(api_key)
        if user:
            return user
        raise ValueError(f"Invalid API key")

    # For local/stdio mode, use or create a default user
    user = d.get_user_by_api_key("dugg_local_default")
    if not user:
        d.conn.execute(
            "INSERT OR IGNORE INTO users (id, name, api_key, created_at) VALUES (?, ?, ?, ?)",
            ("local", "Local User", "dugg_local_default", "2024-01-01T00:00:00Z"),
        )
        d.conn.commit()
        user = d.get_user_by_api_key("dugg_local_default")
    return user


def ensure_default_collection(user_id: str) -> str:
    """Ensure user has a default collection, return its ID."""
    d = get_db()
    collections = d.list_collections(user_id)
    for c in collections:
        if c["name"] == "Default":
            return c["id"]
    result = d.create_collection("Default", user_id, description="Default collection", visibility="private")
    return result["id"]


# --- MCP Tools ---

@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="dugg_add",
            description="Add a resource (URL) to Dugg. The server will enrich it with metadata, transcripts, etc. Provide a note explaining why this resource matters.",
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "The URL to save"},
                    "note": {"type": "string", "description": "Why this resource matters — context for future retrieval", "default": ""},
                    "tags": {"type": "array", "items": {"type": "string"}, "description": "Tags for categorization and share filtering", "default": []},
                    "collection": {"type": "string", "description": "Collection name to add to (uses Default if omitted)", "default": ""},
                    "title": {"type": "string", "description": "Override title (auto-detected if omitted)", "default": ""},
                    "description": {"type": "string", "description": "Override description (auto-detected if omitted)", "default": ""},
                    "transcript": {"type": "string", "description": "Pre-processed transcript (auto-fetched for YouTube if omitted)", "default": ""},
                    "api_key": {"type": "string", "description": "API key for authentication (optional in local mode)", "default": ""},
                },
                "required": ["url"],
            },
        ),
        Tool(
            name="dugg_search",
            description="Search across all resources you have access to. Uses full-text search across titles, descriptions, transcripts, and notes.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query — natural language works fine"},
                    "tags": {"type": "array", "items": {"type": "string"}, "description": "Filter results to resources with these tags", "default": []},
                    "collection": {"type": "string", "description": "Limit search to a specific collection", "default": ""},
                    "limit": {"type": "integer", "description": "Max results to return", "default": 20},
                    "api_key": {"type": "string", "description": "API key for authentication", "default": ""},
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="dugg_feed",
            description="Get the latest resources across all collections you have access to. Respects share rules and tag filters.",
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Max results to return", "default": 50},
                    "api_key": {"type": "string", "description": "API key for authentication", "default": ""},
                },
            },
        ),
        Tool(
            name="dugg_tag",
            description="Add tags to a resource. Tags control share filtering — e.g. tag something 'personal' to exclude it from shared feeds.",
            inputSchema={
                "type": "object",
                "properties": {
                    "resource_id": {"type": "string", "description": "The resource ID to tag"},
                    "tags": {"type": "array", "items": {"type": "string"}, "description": "Tags to add"},
                    "source": {"type": "string", "enum": ["human", "agent"], "description": "Who is adding the tags", "default": "agent"},
                    "api_key": {"type": "string", "description": "API key for authentication", "default": ""},
                },
                "required": ["resource_id", "tags"],
            },
        ),
        Tool(
            name="dugg_collections",
            description="List all collections you have access to.",
            inputSchema={
                "type": "object",
                "properties": {
                    "api_key": {"type": "string", "description": "API key for authentication", "default": ""},
                },
            },
        ),
        Tool(
            name="dugg_share",
            description="Share a collection with another user, optionally with tag-based filtering rules.",
            inputSchema={
                "type": "object",
                "properties": {
                    "collection_id": {"type": "string", "description": "Collection to share"},
                    "user_id": {"type": "string", "description": "User ID to share with"},
                    "include_tags": {"type": "array", "items": {"type": "string"}, "description": "Only share resources with these tags (empty = share all)", "default": []},
                    "exclude_tags": {"type": "array", "items": {"type": "string"}, "description": "Exclude resources with these tags", "default": []},
                    "api_key": {"type": "string", "description": "API key for authentication", "default": ""},
                },
                "required": ["collection_id", "user_id"],
            },
        ),
        Tool(
            name="dugg_create_collection",
            description="Create a new collection for organizing resources.",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Collection name"},
                    "description": {"type": "string", "description": "What this collection is for", "default": ""},
                    "visibility": {"type": "string", "enum": ["private", "shared"], "description": "Whether others can be added to this collection", "default": "private"},
                    "api_key": {"type": "string", "description": "API key for authentication", "default": ""},
                },
                "required": ["name"],
            },
        ),
        Tool(
            name="dugg_create_user",
            description="Create a new user and get their API key. Use this to onboard collaborators.",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "User's display name"},
                    "api_key": {"type": "string", "description": "Admin API key for authentication", "default": ""},
                },
                "required": ["name"],
            },
        ),
        Tool(
            name="dugg_enrich",
            description="Manually trigger enrichment for a resource (re-fetch metadata, transcript, etc).",
            inputSchema={
                "type": "object",
                "properties": {
                    "resource_id": {"type": "string", "description": "The resource to enrich"},
                    "api_key": {"type": "string", "description": "API key for authentication", "default": ""},
                },
                "required": ["resource_id"],
            },
        ),
        Tool(
            name="dugg_link",
            description="Create a relationship between two resources. Agents use this to build a knowledge graph over time.",
            inputSchema={
                "type": "object",
                "properties": {
                    "resource_a": {"type": "string", "description": "First resource ID"},
                    "resource_b": {"type": "string", "description": "Second resource ID"},
                    "relationship": {"type": "string", "description": "Type of relationship (e.g. 'related', 'builds_on', 'contradicts', 'same_topic')"},
                    "confidence": {"type": "number", "description": "Confidence score 0-1", "default": 1.0},
                    "api_key": {"type": "string", "description": "API key for authentication", "default": ""},
                },
                "required": ["resource_a", "resource_b", "relationship"],
            },
        ),
        Tool(
            name="dugg_related",
            description="Get resources related to a given resource (via agent-built connections).",
            inputSchema={
                "type": "object",
                "properties": {
                    "resource_id": {"type": "string", "description": "The resource to find connections for"},
                    "limit": {"type": "integer", "description": "Max results", "default": 10},
                    "api_key": {"type": "string", "description": "API key for authentication", "default": ""},
                },
                "required": ["resource_id"],
            },
        ),
        Tool(
            name="dugg_publish",
            description="Publish a resource to one or more named targets (e.g. 'public', 'aev-team', 'inner-circle'). Published resources become available on remote Dugg instances matching those targets. Use dugg_unpublish to retract.",
            inputSchema={
                "type": "object",
                "properties": {
                    "resource_id": {"type": "string", "description": "The resource to publish"},
                    "targets": {"type": "array", "items": {"type": "string"}, "description": "Named publish targets (e.g. ['public', 'aev-team'])"},
                    "api_key": {"type": "string", "description": "API key for authentication", "default": ""},
                },
                "required": ["resource_id", "targets"],
            },
        ),
        Tool(
            name="dugg_unpublish",
            description="Remove a resource from publish targets. Omit targets to unpublish from all.",
            inputSchema={
                "type": "object",
                "properties": {
                    "resource_id": {"type": "string", "description": "The resource to unpublish"},
                    "targets": {"type": "array", "items": {"type": "string"}, "description": "Specific targets to remove from (omit for all)", "default": []},
                    "api_key": {"type": "string", "description": "API key for authentication", "default": ""},
                },
                "required": ["resource_id"],
            },
        ),
        Tool(
            name="dugg_react",
            description="Silently react to a resource. Only the publisher will see aggregate reaction counts — no one else knows you reacted. Reaction types: tap (default), star, thumbsup.",
            inputSchema={
                "type": "object",
                "properties": {
                    "resource_id": {"type": "string", "description": "The resource to react to"},
                    "reaction": {"type": "string", "enum": ["tap", "star", "thumbsup"], "description": "Type of reaction", "default": "tap"},
                    "api_key": {"type": "string", "description": "API key for authentication", "default": ""},
                },
                "required": ["resource_id"],
            },
        ),
        Tool(
            name="dugg_reactions",
            description="View silent reaction counts for your published resources. Only the resource submitter can see these — no one else.",
            inputSchema={
                "type": "object",
                "properties": {
                    "resource_id": {"type": "string", "description": "Specific resource to check (omit for summary across all your resources)", "default": ""},
                    "api_key": {"type": "string", "description": "API key for authentication", "default": ""},
                },
            },
        ),
        Tool(
            name="dugg_instance_create",
            description="Create a hosted Dugg instance with a topic and access mode (public or invite-only).",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Instance name (e.g. 'Chino Bandito', 'AEV Team')"},
                    "topic": {"type": "string", "description": "What belongs in this Dugg — used by agents for auto-routing", "default": ""},
                    "access_mode": {"type": "string", "enum": ["public", "invite"], "description": "public = anyone can subscribe, invite = member-invites-member", "default": "invite"},
                    "rate_limit_initial": {"type": "integer", "description": "Starting daily post cap for new members (default: 5)", "default": 5},
                    "rate_limit_growth": {"type": "integer", "description": "Additional posts/day earned per day of membership (default: 2)", "default": 2},
                    "api_key": {"type": "string", "description": "API key for authentication", "default": ""},
                },
                "required": ["name"],
            },
        ),
        Tool(
            name="dugg_instance_list",
            description="List all Dugg instances you're subscribed to, with their topics and access modes.",
            inputSchema={
                "type": "object",
                "properties": {
                    "api_key": {"type": "string", "description": "API key for authentication", "default": ""},
                },
            },
        ),
        Tool(
            name="dugg_instance_update",
            description="Update a Dugg instance's topic or access mode. Owner only.",
            inputSchema={
                "type": "object",
                "properties": {
                    "instance_id": {"type": "string", "description": "Instance to update"},
                    "name": {"type": "string", "description": "New name", "default": ""},
                    "topic": {"type": "string", "description": "New topic description", "default": ""},
                    "access_mode": {"type": "string", "enum": ["public", "invite"], "description": "New access mode", "default": ""},
                    "api_key": {"type": "string", "description": "API key for authentication", "default": ""},
                },
                "required": ["instance_id"],
            },
        ),
        Tool(
            name="dugg_invite",
            description="Invite a user to a collection. Tracks who invited whom for the invite tree.",
            inputSchema={
                "type": "object",
                "properties": {
                    "collection_id": {"type": "string", "description": "Collection to invite to"},
                    "user_id": {"type": "string", "description": "User to invite"},
                    "api_key": {"type": "string", "description": "API key for authentication", "default": ""},
                },
                "required": ["collection_id", "user_id"],
            },
        ),
        Tool(
            name="dugg_ban",
            description="Ban a user from a collection. Cascades through their invite tree: depth 1 = hard ban, depth 2+ = credit score decides survival. Owner only.",
            inputSchema={
                "type": "object",
                "properties": {
                    "collection_id": {"type": "string", "description": "Collection to ban from"},
                    "user_id": {"type": "string", "description": "User to ban"},
                    "cascade": {"type": "boolean", "description": "Whether to cascade through invite tree", "default": True},
                    "credit_threshold": {"type": "integer", "description": "Minimum credit score (submissions + reactions) to survive cascade at depth 2+", "default": 5},
                    "api_key": {"type": "string", "description": "API key for authentication", "default": ""},
                },
                "required": ["collection_id", "user_id"],
            },
        ),
        Tool(
            name="dugg_appeal",
            description="Appeal a ban. Only banned members can appeal. Shows your credit score to the collection owner.",
            inputSchema={
                "type": "object",
                "properties": {
                    "collection_id": {"type": "string", "description": "Collection to appeal ban from"},
                    "api_key": {"type": "string", "description": "API key for authentication", "default": ""},
                },
                "required": ["collection_id"],
            },
        ),
        Tool(
            name="dugg_appeals",
            description="List pending appeals for a collection with credit scores. Owner only.",
            inputSchema={
                "type": "object",
                "properties": {
                    "collection_id": {"type": "string", "description": "Collection to check appeals for"},
                    "api_key": {"type": "string", "description": "API key for authentication", "default": ""},
                },
                "required": ["collection_id"],
            },
        ),
        Tool(
            name="dugg_appeal_resolve",
            description="Approve or deny a ban appeal. Approved users are re-rooted under the owner. Owner only.",
            inputSchema={
                "type": "object",
                "properties": {
                    "collection_id": {"type": "string", "description": "Collection"},
                    "user_id": {"type": "string", "description": "User whose appeal to resolve"},
                    "action": {"type": "string", "enum": ["approve", "deny"], "description": "Whether to approve or deny"},
                    "api_key": {"type": "string", "description": "API key for authentication", "default": ""},
                },
                "required": ["collection_id", "user_id", "action"],
            },
        ),
        Tool(
            name="dugg_routing_manifest",
            description="Get topic descriptors for all subscribed Dugg instances. Agents use this to auto-route published content to the right targets.",
            inputSchema={
                "type": "object",
                "properties": {
                    "api_key": {"type": "string", "description": "API key for authentication", "default": ""},
                },
            },
        ),
        Tool(
            name="dugg_rate_limit",
            description="Set rate limit config for a Dugg instance. New members start at 'initial' posts/day, growing by 'growth' per day of membership. Owner only.",
            inputSchema={
                "type": "object",
                "properties": {
                    "instance_id": {"type": "string", "description": "Instance to configure"},
                    "initial": {"type": "integer", "description": "Starting daily post cap for new members (default: 5)", "default": 5},
                    "growth": {"type": "integer", "description": "Additional posts/day earned per day of membership (default: 2)", "default": 2},
                    "api_key": {"type": "string", "description": "API key for authentication", "default": ""},
                },
                "required": ["instance_id"],
            },
        ),
        Tool(
            name="dugg_rate_limit_status",
            description="Check your current rate limit status for a collection — how many posts you've used today vs. your cap.",
            inputSchema={
                "type": "object",
                "properties": {
                    "collection_id": {"type": "string", "description": "Collection to check rate limit for"},
                    "api_key": {"type": "string", "description": "API key for authentication", "default": ""},
                },
                "required": ["collection_id"],
            },
        ),
        Tool(
            name="dugg_get",
            description="Get full details for a specific resource by ID.",
            inputSchema={
                "type": "object",
                "properties": {
                    "resource_id": {"type": "string", "description": "The resource ID"},
                    "api_key": {"type": "string", "description": "API key for authentication", "default": ""},
                },
                "required": ["resource_id"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        api_key = arguments.pop("api_key", "") or None
        user = resolve_user(api_key)
        user_id = user["id"]
        d = get_db()

        if name == "dugg_add":
            return await _handle_add(d, user_id, arguments)
        elif name == "dugg_search":
            return _handle_search(d, user_id, arguments)
        elif name == "dugg_feed":
            return _handle_feed(d, user_id, arguments)
        elif name == "dugg_tag":
            return _handle_tag(d, arguments)
        elif name == "dugg_collections":
            return _handle_collections(d, user_id)
        elif name == "dugg_share":
            return _handle_share(d, user_id, arguments)
        elif name == "dugg_create_collection":
            return _handle_create_collection(d, user_id, arguments)
        elif name == "dugg_create_user":
            return _handle_create_user(d, arguments)
        elif name == "dugg_enrich":
            return await _handle_enrich(d, arguments)
        elif name == "dugg_link":
            return _handle_link(d, user_id, arguments)
        elif name == "dugg_related":
            return _handle_related(d, user_id, arguments)
        elif name == "dugg_publish":
            return _handle_publish(d, user_id, arguments)
        elif name == "dugg_unpublish":
            return _handle_unpublish(d, user_id, arguments)
        elif name == "dugg_react":
            return _handle_react(d, user_id, arguments)
        elif name == "dugg_reactions":
            return _handle_reactions(d, user_id, arguments)
        elif name == "dugg_instance_create":
            return _handle_instance_create(d, user_id, arguments)
        elif name == "dugg_instance_list":
            return _handle_instance_list(d, user_id)
        elif name == "dugg_instance_update":
            return _handle_instance_update(d, user_id, arguments)
        elif name == "dugg_invite":
            return _handle_invite(d, user_id, arguments)
        elif name == "dugg_ban":
            return _handle_ban(d, user_id, arguments)
        elif name == "dugg_appeal":
            return _handle_appeal(d, user_id, arguments)
        elif name == "dugg_appeals":
            return _handle_appeals(d, user_id, arguments)
        elif name == "dugg_appeal_resolve":
            return _handle_appeal_resolve(d, user_id, arguments)
        elif name == "dugg_routing_manifest":
            return _handle_routing_manifest(d, user_id)
        elif name == "dugg_rate_limit":
            return _handle_rate_limit(d, user_id, arguments)
        elif name == "dugg_rate_limit_status":
            return _handle_rate_limit_status(d, user_id, arguments)
        elif name == "dugg_get":
            return _handle_get(d, user_id, arguments)
        else:
            return [TextContent(type="text", text=f"Unknown tool: {name}")]
    except Exception as e:
        return [TextContent(type="text", text=f"Error: {e}")]


# --- Tool Handlers ---

async def _handle_add(d: DuggDB, user_id: str, args: dict) -> list[TextContent]:
    url = args["url"]
    note = args.get("note", "")
    tags = args.get("tags", [])
    collection_name = args.get("collection", "")
    provided_title = args.get("title", "")
    provided_desc = args.get("description", "")
    provided_transcript = args.get("transcript", "")

    # Resolve collection
    if collection_name:
        collections = d.list_collections(user_id)
        coll_id = None
        for c in collections:
            if c["name"].lower() == collection_name.lower():
                coll_id = c["id"]
                break
        if not coll_id:
            result = d.create_collection(collection_name, user_id, visibility="private")
            coll_id = result["id"]
    else:
        coll_id = ensure_default_collection(user_id)

    # Check rate limit before doing any work
    rate_status = d.check_rate_limit(coll_id, user_id)
    if not rate_status["allowed"] and rate_status["cap"] != -1:
        return [TextContent(type="text", text=f"Rate limit exceeded. You've used {rate_status['current']}/{rate_status['cap']} posts today (member for {rate_status['days_member']} day(s)). Try again tomorrow.")]

    # Enrich the URL
    enriched = await enrich_url(url)

    # Extract author from enrichment metadata
    author = enriched.get("raw_metadata", {}).get("author", "")

    resource = d.add_resource(
        url=url,
        collection_id=coll_id,
        submitted_by=user_id,
        note=note,
        title=provided_title or enriched.get("title", ""),
        description=provided_desc or enriched.get("description", ""),
        thumbnail=enriched.get("thumbnail", ""),
        source_type=enriched.get("source_type", "unknown"),
        author=author,
        transcript=provided_transcript or enriched.get("transcript", ""),
        raw_metadata=enriched.get("raw_metadata"),
        tags=tags,
        tag_source="human" if tags else "agent",
    )

    # Auto-tag with channel/author name for YouTube
    if author:
        author_tag = author.lower().strip()
        d.tag_resource(resource["id"], [author_tag], source="agent")

    # Mark enriched
    from dugg.db import _now
    d.update_resource(resource["id"], enriched_at=_now())

    summary = f"Added: {resource.get('title') or url}\n"
    summary += f"ID: {resource['id']}\n"
    summary += f"Type: {resource['source_type']}\n"
    if author:
        summary += f"Author: {author}\n"
    if resource.get("tags"):
        summary += f"Tags: {', '.join(resource['tags'])}\n"
    if enriched.get("transcript"):
        word_count = len(enriched["transcript"].split())
        summary += f"Transcript: {word_count} words captured\n"
    return [TextContent(type="text", text=summary)]


def _handle_search(d: DuggDB, user_id: str, args: dict) -> list[TextContent]:
    query = args["query"]
    tags = args.get("tags", [])
    collection = args.get("collection", "")
    limit = args.get("limit", 20)

    # Resolve collection by name if provided
    coll_id = None
    if collection:
        for c in d.list_collections(user_id):
            if c["name"].lower() == collection.lower():
                coll_id = c["id"]
                break

    results = d.search(query, user_id, collection_id=coll_id, tags=tags or None, limit=limit)

    if not results:
        return [TextContent(type="text", text=f"No results for: {query}")]

    lines = [f"Found {len(results)} result(s) for: {query}\n"]
    for r in results:
        tags_str = ", ".join(t["label"] for t in r.get("tags", []))
        lines.append(f"- [{r['id']}] {r.get('title') or r['url']}")
        if tags_str:
            lines.append(f"  Tags: {tags_str}")
        if r.get("note"):
            lines.append(f"  Note: {r['note'][:200]}")
        lines.append(f"  URL: {r['url']}")
        lines.append("")
    return [TextContent(type="text", text="\n".join(lines))]


def _handle_feed(d: DuggDB, user_id: str, args: dict) -> list[TextContent]:
    limit = args.get("limit", 50)
    results = d.get_feed(user_id, limit=limit)

    if not results:
        return [TextContent(type="text", text="Your feed is empty. Add some resources with dugg_add!")]

    lines = [f"Feed: {len(results)} resource(s)\n"]
    for r in results:
        tags_str = ", ".join(t["label"] for t in r.get("tags", []))
        submitted_by = r.get("submitted_by", "")
        submitter = d.get_user(submitted_by)
        submitter_name = submitter["name"] if submitter else submitted_by

        lines.append(f"- [{r['id']}] {r.get('title') or r['url']}")
        author_str = f" | Author: {r['author']}" if r.get("author") else ""
        lines.append(f"  By: {submitter_name} | Type: {r['source_type']}{author_str}")
        if tags_str:
            lines.append(f"  Tags: {tags_str}")
        if r.get("note"):
            lines.append(f"  Note: {r['note'][:200]}")
        lines.append(f"  URL: {r['url']}")
        lines.append(f"  Added: {r['created_at']}")
        lines.append("")
    return [TextContent(type="text", text="\n".join(lines))]


def _handle_tag(d: DuggDB, args: dict) -> list[TextContent]:
    resource_id = args["resource_id"]
    tags = args["tags"]
    source = args.get("source", "agent")
    result = d.tag_resource(resource_id, tags, source=source)
    labels = [t["label"] for t in result]
    return [TextContent(type="text", text=f"Tags on {resource_id}: {', '.join(labels)}")]


def _handle_collections(d: DuggDB, user_id: str) -> list[TextContent]:
    collections = d.list_collections(user_id)
    if not collections:
        return [TextContent(type="text", text="No collections yet. Create one with dugg_create_collection.")]

    lines = [f"{len(collections)} collection(s):\n"]
    for c in collections:
        count = len(d.list_resources(c["id"], limit=1000))
        lines.append(f"- [{c['id']}] {c['name']} ({c['visibility']}, {count} resources)")
        if c.get("description"):
            lines.append(f"  {c['description']}")
    return [TextContent(type="text", text="\n".join(lines))]


def _handle_share(d: DuggDB, user_id: str, args: dict) -> list[TextContent]:
    collection_id = args["collection_id"]
    target_user_id = args["user_id"]
    include_tags = args.get("include_tags", [])
    exclude_tags = args.get("exclude_tags", [])

    # Add as member
    d.add_collection_member(collection_id, target_user_id)

    # Set share rules if any tag filters
    if include_tags or exclude_tags:
        d.set_share_rule(collection_id, target_user_id, include_tags=include_tags, exclude_tags=exclude_tags)

    msg = f"Shared collection {collection_id} with user {target_user_id}"
    if include_tags:
        msg += f"\n  Include tags: {', '.join(include_tags)}"
    if exclude_tags:
        msg += f"\n  Exclude tags: {', '.join(exclude_tags)}"
    return [TextContent(type="text", text=msg)]


def _handle_create_collection(d: DuggDB, user_id: str, args: dict) -> list[TextContent]:
    name = args["name"]
    description = args.get("description", "")
    visibility = args.get("visibility", "private")
    result = d.create_collection(name, user_id, description=description, visibility=visibility)
    return [TextContent(type="text", text=f"Created collection: {result['name']} [{result['id']}] ({result['visibility']})")]


def _handle_create_user(d: DuggDB, args: dict) -> list[TextContent]:
    name = args["name"]
    result = d.create_user(name)
    return [TextContent(type="text", text=f"Created user: {result['name']}\nID: {result['id']}\nAPI Key: {result['api_key']}\n\nSave this API key — it won't be shown again.")]


async def _handle_enrich(d: DuggDB, args: dict) -> list[TextContent]:
    resource_id = args["resource_id"]
    resource = d.get_resource(resource_id)
    if not resource:
        return [TextContent(type="text", text=f"Resource {resource_id} not found")]

    enriched = await enrich_url(resource["url"])
    from dugg.db import _now
    updates = {}
    if enriched.get("title") and not resource.get("title"):
        updates["title"] = enriched["title"]
    if enriched.get("description") and not resource.get("description"):
        updates["description"] = enriched["description"]
    if enriched.get("thumbnail") and not resource.get("thumbnail"):
        updates["thumbnail"] = enriched["thumbnail"]
    if enriched.get("transcript") and not resource.get("transcript"):
        updates["transcript"] = enriched["transcript"]
    if enriched.get("source_type") and resource.get("source_type") == "unknown":
        updates["source_type"] = enriched["source_type"]
    enriched_author = enriched.get("raw_metadata", {}).get("author", "")
    if enriched_author and not resource.get("author"):
        updates["author"] = enriched_author
    if enriched.get("raw_metadata"):
        updates["raw_metadata"] = enriched["raw_metadata"]
    updates["enriched_at"] = _now()

    d.update_resource(resource_id, **updates)

    summary = f"Enriched: {resource_id}\n"
    for k, v in updates.items():
        if k == "transcript":
            summary += f"  transcript: {len(v.split())} words\n"
        elif k == "raw_metadata":
            summary += f"  raw_metadata: updated\n"
        elif k != "enriched_at":
            summary += f"  {k}: {str(v)[:100]}\n"
    return [TextContent(type="text", text=summary)]


def _handle_link(d: DuggDB, user_id: str, args: dict) -> list[TextContent]:
    resource_a = args["resource_a"]
    resource_b = args["resource_b"]
    relationship = args["relationship"]
    confidence = args.get("confidence", 1.0)
    edge = d.link_resources(resource_a, resource_b, relationship, confidence, created_by=user_id)
    return [TextContent(type="text", text=f"Linked {resource_a} <-[{relationship}]-> {resource_b} (confidence: {confidence})")]


def _handle_related(d: DuggDB, user_id: str, args: dict) -> list[TextContent]:
    resource_id = args["resource_id"]
    limit = args.get("limit", 10)
    edges = d.get_related(resource_id, limit=limit)
    if not edges:
        return [TextContent(type="text", text=f"No connections found for {resource_id}")]
    lines = [f"{len(edges)} connection(s):\n"]
    for e in edges:
        other = e["resource_b"] if e["resource_a"] == resource_id else e["resource_a"]
        other_res = d.get_resource(other)
        other_title = other_res.get("title", other) if other_res else other
        lines.append(f"- [{e['relationship_type']}] {other_title} ({other}) — confidence: {e['confidence']}")
    return [TextContent(type="text", text="\n".join(lines))]


def _handle_publish(d: DuggDB, user_id: str, args: dict) -> list[TextContent]:
    resource_id = args["resource_id"]
    targets = args["targets"]
    resource = d.get_resource(resource_id)
    if not resource:
        return [TextContent(type="text", text=f"Resource {resource_id} not found")]
    if resource["submitted_by"] != user_id:
        return [TextContent(type="text", text=f"Only the submitter can publish a resource")]
    results = d.publish_resource(resource_id, targets)
    target_list = ", ".join(r["target"] for r in results)
    title = resource.get("title") or resource["url"]
    return [TextContent(type="text", text=f"Published: {title}\nTargets: {target_list}")]


def _handle_unpublish(d: DuggDB, user_id: str, args: dict) -> list[TextContent]:
    resource_id = args["resource_id"]
    targets = args.get("targets", [])
    resource = d.get_resource(resource_id)
    if not resource:
        return [TextContent(type="text", text=f"Resource {resource_id} not found")]
    if resource["submitted_by"] != user_id:
        return [TextContent(type="text", text=f"Only the submitter can unpublish a resource")]
    d.unpublish_resource(resource_id, targets if targets else None)
    if targets:
        return [TextContent(type="text", text=f"Unpublished {resource_id} from: {', '.join(targets)}")]
    return [TextContent(type="text", text=f"Unpublished {resource_id} from all targets")]


def _handle_react(d: DuggDB, user_id: str, args: dict) -> list[TextContent]:
    resource_id = args["resource_id"]
    reaction_type = args.get("reaction", "tap")
    resource = d.get_resource(resource_id)
    if not resource:
        return [TextContent(type="text", text=f"Resource {resource_id} not found")]
    # Check user has access to the resource's collection
    accessible = d._accessible_collection_ids(user_id)
    if resource["collection_id"] not in accessible:
        return [TextContent(type="text", text=f"Access denied to resource {resource_id}")]
    d.react_to_resource(resource_id, user_id, reaction_type)
    return [TextContent(type="text", text=f"Reacted to {resource_id} with {reaction_type}")]


def _handle_reactions(d: DuggDB, user_id: str, args: dict) -> list[TextContent]:
    resource_id = args.get("resource_id", "")
    if resource_id:
        result = d.get_reactions(resource_id, user_id)
        if result is None:
            return [TextContent(type="text", text=f"No reactions found (or you're not the publisher of this resource)")]
        if result["total"] == 0:
            return [TextContent(type="text", text=f"No reactions yet on {resource_id}")]
        lines = [f"Reactions on {resource_id}: {result['total']} total"]
        for rtype, count in result["breakdown"].items():
            lines.append(f"  {rtype}: {count}")
        return [TextContent(type="text", text="\n".join(lines))]
    else:
        summary = d.get_my_reactions_summary(user_id)
        if not summary:
            return [TextContent(type="text", text="No reactions on any of your resources yet.")]
        lines = [f"Reactions across your resources:\n"]
        for item in summary:
            title = item["title"] or item["url"]
            breakdown = ", ".join(f"{k}: {v}" for k, v in item["breakdown"].items())
            lines.append(f"- {title} ({item['resource_id']}): {item['total']} total ({breakdown})")
        return [TextContent(type="text", text="\n".join(lines))]


def _handle_instance_create(d: DuggDB, user_id: str, args: dict) -> list[TextContent]:
    name = args["name"]
    topic = args.get("topic", "")
    access_mode = args.get("access_mode", "invite")
    rate_limit_initial = args.get("rate_limit_initial", 5)
    rate_limit_growth = args.get("rate_limit_growth", 2)
    result = d.create_instance(name, user_id, topic=topic, access_mode=access_mode,
                               rate_limit_initial=rate_limit_initial, rate_limit_growth=rate_limit_growth)
    lines = [f"Created Dugg instance: {result['name']} [{result['id']}]"]
    lines.append(f"Access: {result['access_mode']}")
    if topic:
        lines.append(f"Topic: {topic}")
    lines.append(f"Rate limit: {result['rate_limit_initial']} initial, +{result['rate_limit_growth']}/day")
    return [TextContent(type="text", text="\n".join(lines))]


def _handle_instance_list(d: DuggDB, user_id: str) -> list[TextContent]:
    instances = d.list_instances(user_id)
    if not instances:
        return [TextContent(type="text", text="Not subscribed to any Dugg instances.")]
    lines = [f"{len(instances)} instance(s):\n"]
    for inst in instances:
        lines.append(f"- [{inst['id']}] {inst['name']} ({inst['access_mode']})")
        if inst.get("topic"):
            lines.append(f"  Topic: {inst['topic']}")
    return [TextContent(type="text", text="\n".join(lines))]


def _handle_instance_update(d: DuggDB, user_id: str, args: dict) -> list[TextContent]:
    instance_id = args["instance_id"]
    updates = {}
    if args.get("name"):
        updates["name"] = args["name"]
    if args.get("topic"):
        updates["topic"] = args["topic"]
    if args.get("access_mode"):
        updates["access_mode"] = args["access_mode"]
    result = d.update_instance(instance_id, user_id, **updates)
    if not result:
        return [TextContent(type="text", text=f"Instance {instance_id} not found or you're not the owner")]
    return [TextContent(type="text", text=f"Updated instance: {result['name']} [{result['id']}]\nTopic: {result['topic']}\nAccess: {result['access_mode']}")]


def _handle_invite(d: DuggDB, user_id: str, args: dict) -> list[TextContent]:
    collection_id = args["collection_id"]
    invitee_id = args["user_id"]
    # Check inviter is an active member
    member = d.get_member_status(collection_id, user_id)
    if not member or member["status"] != "active":
        return [TextContent(type="text", text="You must be an active member to invite others")]
    result = d.invite_member(collection_id, user_id, invitee_id)
    invitee = d.get_user(invitee_id)
    invitee_name = invitee["name"] if invitee else invitee_id
    return [TextContent(type="text", text=f"Invited {invitee_name} to collection {collection_id}")]


def _handle_ban(d: DuggDB, user_id: str, args: dict) -> list[TextContent]:
    collection_id = args["collection_id"]
    target_user_id = args["user_id"]
    cascade = args.get("cascade", True)
    credit_threshold = args.get("credit_threshold", 5)
    # Only owner can ban
    member = d.get_member_status(collection_id, user_id)
    if not member or member["role"] != "owner":
        return [TextContent(type="text", text="Only the collection owner can ban members")]
    result = d.ban_member(collection_id, target_user_id, cascade=cascade, credit_threshold=credit_threshold)
    lines = [f"Banned {len(result['banned'])} member(s)"]
    if result["survived"]:
        lines.append(f"Survived via credit score: {len(result['survived'])} member(s) (re-rooted under owner)")
    for uid in result["banned"]:
        user = d.get_user(uid)
        name = user["name"] if user else uid
        lines.append(f"  Banned: {name} ({uid})")
    for uid in result["survived"]:
        user = d.get_user(uid)
        name = user["name"] if user else uid
        score = d.get_member_credit_score(collection_id, uid)
        lines.append(f"  Survived: {name} ({uid}) — score: {score['total']}")
    return [TextContent(type="text", text="\n".join(lines))]


def _handle_appeal(d: DuggDB, user_id: str, args: dict) -> list[TextContent]:
    collection_id = args["collection_id"]
    result = d.appeal_ban(collection_id, user_id)
    if not result:
        return [TextContent(type="text", text="You can only appeal if you are currently banned")]
    return [TextContent(type="text", text=f"Appeal submitted. Your credit score: {result['submissions']} submissions, {result['reactions_received']} reactions received ({result['total']} total)")]


def _handle_appeals(d: DuggDB, user_id: str, args: dict) -> list[TextContent]:
    collection_id = args["collection_id"]
    # Only owner can view appeals
    member = d.get_member_status(collection_id, user_id)
    if not member or member["role"] != "owner":
        return [TextContent(type="text", text="Only the collection owner can view appeals")]
    appeals = d.get_appeals(collection_id)
    if not appeals:
        return [TextContent(type="text", text="No pending appeals.")]
    lines = [f"{len(appeals)} pending appeal(s):\n"]
    for a in appeals:
        lines.append(f"- {a['name']} ({a['user_id']})")
        lines.append(f"  Submissions: {a['submissions']} | Reactions received: {a['reactions_received']} | Total score: {a['total']}")
        lines.append(f"  Joined: {a['joined_at']}")
    return [TextContent(type="text", text="\n".join(lines))]


def _handle_appeal_resolve(d: DuggDB, user_id: str, args: dict) -> list[TextContent]:
    collection_id = args["collection_id"]
    target_user_id = args["user_id"]
    action = args["action"]
    # Only owner can resolve
    member = d.get_member_status(collection_id, user_id)
    if not member or member["role"] != "owner":
        return [TextContent(type="text", text="Only the collection owner can resolve appeals")]
    if action == "approve":
        result = d.approve_appeal(collection_id, target_user_id)
        if not result:
            return [TextContent(type="text", text="No pending appeal found for this user")]
        return [TextContent(type="text", text=f"Appeal approved. {target_user_id} is now active and re-rooted under owner.")]
    else:
        result = d.deny_appeal(collection_id, target_user_id)
        if not result:
            return [TextContent(type="text", text="No pending appeal found for this user")]
        return [TextContent(type="text", text=f"Appeal denied. {target_user_id} remains banned.")]


def _handle_routing_manifest(d: DuggDB, user_id: str) -> list[TextContent]:
    manifest = d.get_routing_manifest(user_id)
    if not manifest:
        return [TextContent(type="text", text="No subscribed instances. Create one with dugg_instance_create.")]
    lines = ["Routing manifest — your agent uses these topics to auto-route published content:\n"]
    for inst in manifest:
        lines.append(f"- {inst['name']} [{inst['id']}] ({inst['access_mode']})")
        if inst.get("topic"):
            lines.append(f"  Topic: {inst['topic']}")
        else:
            lines.append(f"  Topic: (none set — set one with dugg_instance_update)")
    return [TextContent(type="text", text="\n".join(lines))]


def _handle_rate_limit(d: DuggDB, user_id: str, args: dict) -> list[TextContent]:
    instance_id = args["instance_id"]
    initial = args.get("initial")
    growth = args.get("growth")
    result = d.set_rate_limit(instance_id, user_id, initial=initial, growth=growth)
    if not result:
        return [TextContent(type="text", text=f"Instance {instance_id} not found or you're not the owner")]
    return [TextContent(type="text", text=f"Rate limit updated for {result['name']}:\n  Initial cap: {result['rate_limit_initial']} posts/day\n  Growth: +{result['rate_limit_growth']} posts/day per day of membership")]


def _handle_rate_limit_status(d: DuggDB, user_id: str, args: dict) -> list[TextContent]:
    collection_id = args["collection_id"]
    status = d.check_rate_limit(collection_id, user_id)
    if not status["allowed"] and status["reason"] == "not a member":
        return [TextContent(type="text", text="You're not a member of this collection")]
    if status["cap"] == -1:
        return [TextContent(type="text", text="No rate limit configured for this collection.")]
    return [TextContent(type="text", text=f"Rate limit status:\n  Today: {status['current']}/{status['cap']} posts used\n  Member for: {status['days_member']} day(s)\n  {'Can post' if status['allowed'] else 'Rate limit reached — try again tomorrow'}")]


def _handle_get(d: DuggDB, user_id: str, args: dict) -> list[TextContent]:
    resource_id = args["resource_id"]
    resource = d.get_resource(resource_id)
    if not resource:
        return [TextContent(type="text", text=f"Resource {resource_id} not found")]

    # Check access
    accessible = d._accessible_collection_ids(user_id)
    if resource["collection_id"] not in accessible:
        return [TextContent(type="text", text=f"Access denied to resource {resource_id}")]

    tags_str = ", ".join(t["label"] for t in resource.get("tags", []))
    submitter = d.get_user(resource["submitted_by"])
    submitter_name = submitter["name"] if submitter else resource["submitted_by"]

    lines = [
        f"Resource: {resource.get('title') or resource['url']}",
        f"ID: {resource['id']}",
        f"URL: {resource['url']}",
        f"Type: {resource['source_type']}",
    ]
    if resource.get("author"):
        lines.append(f"Author: {resource['author']}")
    lines.extend([
        f"By: {submitter_name}",
        f"Added: {resource['created_at']}",
    ])
    if tags_str:
        lines.append(f"Tags: {tags_str}")
    # Show publish targets if the requester is the submitter
    pub_targets = d.get_publish_targets(resource_id)
    if pub_targets:
        targets_str = ", ".join(t["target"] for t in pub_targets)
        lines.append(f"Published to: {targets_str}")
    if resource.get("note"):
        lines.append(f"\nNote: {resource['note']}")
    if resource.get("description"):
        lines.append(f"\nDescription: {resource['description']}")
    if resource.get("transcript"):
        word_count = len(resource["transcript"].split())
        # Show first 500 chars of transcript
        preview = resource["transcript"][:500]
        lines.append(f"\nTranscript ({word_count} words): {preview}...")
    return [TextContent(type="text", text="\n".join(lines))]


# --- Main ---

def main():
    """Run the Dugg MCP server over stdio."""
    async def _run():
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())

    asyncio.run(_run())


if __name__ == "__main__":
    main()
