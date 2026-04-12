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
