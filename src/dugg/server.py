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

from dugg.db import DuggDB, _now
from dugg.enrichment import enrich_url
from dugg.sync import start_sync_daemon

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
    return get_db().ensure_default_collection(user_id)


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
                    "summary": {"type": "string", "description": "Agent-provided summary (takes priority over auto-generation)", "default": ""},
                    "api_key": {"type": "string", "description": "API key for authentication (optional in local mode)", "default": ""},
                },
                "required": ["url"],
            },
        ),
        Tool(
            name="dugg_search",
            description="Search across all resources you have access to. Full-text search indexes: title, description, author, transcript, note, and summary.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query — natural language works fine"},
                    "tags": {"type": "array", "items": {"type": "string"}, "description": "Filter results to resources with these tags", "default": []},
                    "collection": {"type": "string", "description": "Limit search to a specific collection", "default": ""},
                    "submitted_by": {"type": "string", "description": "Filter to resources submitted by this user ID (use 'me' for your own)", "default": ""},
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
            description="Create a new user with a linked agent account. Returns both a user key (for the human) and an agent key (for their AI agent). Ban cascades apply — banning the user revokes the agent key too.",
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
            name="dugg_invite_user",
            description="Create an invite token to onboard a new user. Returns copyable text with a redemption link they can open in a browser — no CLI or agent required on their end.",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Name of the person being invited (shown on the redemption page)"},
                    "expires_hours": {"type": "integer", "description": "Hours until the invite expires (default: 72)", "default": 72},
                    "api_key": {"type": "string", "description": "API key for authentication", "default": ""},
                },
                "required": ["name"],
            },
        ),
        Tool(
            name="dugg_invites",
            description="List invite tokens you've created — shows pending, redeemed, and expired status.",
            inputSchema={
                "type": "object",
                "properties": {
                    "api_key": {"type": "string", "description": "API key for authentication", "default": ""},
                },
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
                    "read_horizon_base_days": {"type": "integer", "description": "Days of content history visible to new members (default: 30, -1 for full history)", "default": 30},
                    "read_horizon_growth": {"type": "integer", "description": "Extra days of visibility earned per week of membership (default: 7)", "default": 7},
                    "index_mode": {"type": "string", "enum": ["summary", "full", "metadata_only"], "description": "How ingested content is stored: summary (default), full, or metadata_only", "default": "summary"},
                    "local_storage_cap_mb": {"type": "integer", "description": "Max local content storage in MB (default: 512, -1 for unlimited)", "default": 512},
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
            description="Update instance configuration: name, topic, access mode, endpoint, read horizon, index mode, storage cap, onboarding preset, pruning mode, and pruning grace period. Owner only.",
            inputSchema={
                "type": "object",
                "properties": {
                    "instance_id": {"type": "string", "description": "Instance to update"},
                    "name": {"type": "string", "description": "New name", "default": ""},
                    "topic": {"type": "string", "description": "New topic description", "default": ""},
                    "access_mode": {"type": "string", "enum": ["public", "invite"], "description": "New access mode", "default": ""},
                    "endpoint_url": {"type": "string", "description": "Remote endpoint URL for receiving published resources", "default": ""},
                    "read_horizon_base_days": {"type": "integer", "description": "Days of content history visible to new members (-1 for full history)"},
                    "read_horizon_growth": {"type": "integer", "description": "Extra days of visibility earned per week of membership"},
                    "index_mode": {"type": "string", "enum": ["summary", "full", "metadata_only"], "description": "How ingested content is stored"},
                    "local_storage_cap_mb": {"type": "integer", "description": "Max local content storage in MB (-1 for unlimited)"},
                    "onboarding_mode": {"type": "string", "enum": ["graduated", "full_access"], "description": "Onboarding preset: 'graduated' (default) or 'full_access' (sets horizon=-1, storage=-1, index=full)"},
                    "pruning_mode": {"type": "string", "enum": ["interaction", "none"], "description": "Member pruning policy: 'interaction' (prune inactive after grace period) or 'none' (never prune)"},
                    "pruning_grace_days": {"type": "integer", "description": "Days of inactivity before a member can be pruned (default 14). Only applies when pruning_mode is 'interaction'."},
                    "api_key": {"type": "string", "description": "API key for authentication", "default": ""},
                },
                "required": ["instance_id"],
            },
        ),
        Tool(
            name="dugg_invite",
            description="Invite a user to a collection. Tracks who invited whom for the invite tree. Depth cap: 15 levels.",
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
            description="Ban a user from a collection. Cascades through their invite tree: depth 1 = hard ban, depth 2+ = credit score decides survival. With purge=true, permanently deletes all resources submitted by banned users. Owner only.",
            inputSchema={
                "type": "object",
                "properties": {
                    "collection_id": {"type": "string", "description": "Collection to ban from"},
                    "user_id": {"type": "string", "description": "User to ban"},
                    "cascade": {"type": "boolean", "description": "Whether to cascade through invite tree", "default": True},
                    "credit_threshold": {"type": "integer", "description": "Minimum credit score (submissions + reactions) to survive cascade at depth 2+", "default": 5},
                    "purge": {"type": "boolean", "description": "Permanently delete all resources submitted by banned users (for malware/spam removal)", "default": False},
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
            name="dugg_publish_status",
            description="Check the status of the publish sync queue — how many publishes are pending, delivered, or failed.",
            inputSchema={
                "type": "object",
                "properties": {
                    "api_key": {"type": "string", "description": "API key for authentication", "default": ""},
                },
            },
        ),
        Tool(
            name="dugg_publish_retry",
            description="Retry all failed publishes — resets them back to pending for another round of delivery attempts.",
            inputSchema={
                "type": "object",
                "properties": {
                    "api_key": {"type": "string", "description": "API key for authentication", "default": ""},
                },
            },
        ),
        Tool(
            name="dugg_events",
            description="Get recent events across your subscribed instances and collections. Events include resource_added, resource_published, resource_deleted, member_joined, member_banned, invite_created, invite_redeemed, publish_delivered, reaction_added.",
            inputSchema={
                "type": "object",
                "properties": {
                    "event_types": {"type": "array", "items": {"type": "string"}, "description": "Filter by event type(s)", "default": []},
                    "since": {"type": "string", "description": "Only show events after this ISO timestamp", "default": ""},
                    "actor_id": {"type": "string", "description": "Filter to events by this user ID (use 'me' for your own)", "default": ""},
                    "limit": {"type": "integer", "description": "Max events to return", "default": 50},
                    "api_key": {"type": "string", "description": "API key for authentication", "default": ""},
                },
            },
        ),
        Tool(
            name="dugg_catchup",
            description="Get events you haven't seen since your last check. Returns unseen events oldest-first by default, with the agent's relevance context. After reviewing, call dugg_mark_seen to advance your cursor.",
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Max events to return (default 10)", "default": 10},
                    "oldest_first": {"type": "boolean", "description": "Show oldest unseen first (true) or newest first (false)", "default": True},
                    "api_key": {"type": "string", "description": "API key for authentication", "default": ""},
                },
            },
        ),
        Tool(
            name="dugg_mark_seen",
            description="Advance your read cursor to mark events as seen. Call after reviewing catchup results. Optionally pass a specific timestamp, or omit to mark everything up to now as seen.",
            inputSchema={
                "type": "object",
                "properties": {
                    "seen_until": {"type": "string", "description": "ISO timestamp to advance cursor to (omit for now)", "default": ""},
                    "api_key": {"type": "string", "description": "API key for authentication", "default": ""},
                },
            },
        ),
        Tool(
            name="dugg_webhook_subscribe",
            description="Subscribe a webhook to receive real-time event notifications. Omit instance_id to fire on all events this server emits (most common for personal Slack notifications). Slack incoming-webhook URLs are auto-detected and formatted.",
            inputSchema={
                "type": "object",
                "properties": {
                    "callback_url": {"type": "string", "description": "URL to POST event payloads to (e.g. a Slack incoming-webhook URL)"},
                    "instance_id": {"type": "string", "description": "Optional — scope to a specific subscribed instance. Omit for all server events.", "default": ""},
                    "event_types": {"type": "array", "items": {"type": "string"}, "description": "Event types to subscribe to (empty = all)", "default": []},
                    "secret": {"type": "string", "description": "HMAC secret for signing webhook payloads (optional)", "default": ""},
                    "api_key": {"type": "string", "description": "API key for authentication", "default": ""},
                },
                "required": ["callback_url"],
            },
        ),
        Tool(
            name="dugg_webhook_list",
            description="List your active webhook subscriptions.",
            inputSchema={
                "type": "object",
                "properties": {
                    "api_key": {"type": "string", "description": "API key for authentication", "default": ""},
                },
            },
        ),
        Tool(
            name="dugg_webhook_delete",
            description="Remove a webhook subscription.",
            inputSchema={
                "type": "object",
                "properties": {
                    "webhook_id": {"type": "string", "description": "Webhook subscription ID to remove"},
                    "api_key": {"type": "string", "description": "API key for authentication", "default": ""},
                },
                "required": ["webhook_id"],
            },
        ),
        Tool(
            name="dugg_ingest",
            description="Receive a published resource from a remote Dugg instance. Deduplicates by URL. Used by the sync daemon or manual push.",
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Resource URL"},
                    "title": {"type": "string", "description": "Resource title", "default": ""},
                    "description": {"type": "string", "description": "Resource description", "default": ""},
                    "source_type": {"type": "string", "description": "Resource type", "default": "unknown"},
                    "author": {"type": "string", "description": "Resource author", "default": ""},
                    "note": {"type": "string", "description": "Context note", "default": ""},
                    "tags": {"type": "array", "items": {"type": "string"}, "description": "Tags", "default": []},
                    "source_instance_id": {"type": "string", "description": "The remote instance that published this"},
                    "collection": {"type": "string", "description": "Target collection name (uses Default if omitted)", "default": ""},
                    "api_key": {"type": "string", "description": "API key for authentication", "default": ""},
                },
                "required": ["url", "source_instance_id"],
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
        Tool(
            name="dugg_instance_policy",
            description="Get the current policy configuration for a Dugg instance — onboarding mode, read horizon, index mode, storage cap, rate limits, pruning mode, pruning grace period, and access mode.",
            inputSchema={
                "type": "object",
                "properties": {
                    "instance_id": {"type": "string", "description": "Instance to get policy for"},
                    "api_key": {"type": "string", "description": "API key for authentication", "default": ""},
                },
                "required": ["instance_id"],
            },
        ),
        Tool(
            name="dugg_welcome",
            description="Orientation for new connections. Returns instance topic(s), recent activity, and your rate limit status in one call. Run this first when connecting to a Dugg instance.",
            inputSchema={
                "type": "object",
                "properties": {
                    "api_key": {"type": "string", "description": "API key for authentication", "default": ""},
                },
            },
        ),
        Tool(
            name="dugg_publish_clear",
            description="Delete failed publish queue entries. Optionally scoped to a target instance. Owner only.",
            inputSchema={
                "type": "object",
                "properties": {
                    "target_instance_id": {"type": "string", "description": "Only clear failures targeting this instance (omit for all)", "default": ""},
                    "api_key": {"type": "string", "description": "API key for authentication", "default": ""},
                },
            },
        ),
        Tool(
            name="dugg_publish_retry_selective",
            description="Retry specific failed publishes — by ID or by target instance. More surgical than dugg_publish_retry.",
            inputSchema={
                "type": "object",
                "properties": {
                    "publish_id": {"type": "string", "description": "Specific publish queue entry ID to retry", "default": ""},
                    "target_instance_id": {"type": "string", "description": "Retry all failed entries for this target instance", "default": ""},
                    "api_key": {"type": "string", "description": "API key for authentication", "default": ""},
                },
            },
        ),
        Tool(
            name="dugg_prune_inactive",
            description="List or remove members past their grace period with zero activity (no submissions, no reactions). Grace period is configurable per-instance (default 14 days).",
            inputSchema={
                "type": "object",
                "properties": {
                    "collection_id": {"type": "string", "description": "Collection to check for inactive members"},
                    "execute": {"type": "boolean", "description": "If true, actually ban inactive members. If false, just list them.", "default": False},
                    "api_key": {"type": "string", "description": "API key for authentication", "default": ""},
                },
                "required": ["collection_id"],
            },
        ),
        Tool(
            name="dugg_set_successor",
            description="Designate a successor for a Dugg instance. If the owner is incapacitated, ownership can be transferred to this user. Owner only.",
            inputSchema={
                "type": "object",
                "properties": {
                    "instance_id": {"type": "string", "description": "Instance to set successor for"},
                    "successor_id": {"type": "string", "description": "User ID of the designated successor (must be a subscriber)"},
                    "api_key": {"type": "string", "description": "API key for authentication", "default": ""},
                },
                "required": ["instance_id", "successor_id"],
            },
        ),
        Tool(
            name="dugg_delete_resource",
            description="Permanently delete a single resource from a collection. Removes the resource and all its tags, reactions, publish targets, and queue entries. Owner only — use for malware links, spam, or policy violations.",
            inputSchema={
                "type": "object",
                "properties": {
                    "resource_id": {"type": "string", "description": "ID of the resource to delete"},
                    "collection_id": {"type": "string", "description": "Collection the resource belongs to"},
                    "api_key": {"type": "string", "description": "API key for authentication", "default": ""},
                },
                "required": ["resource_id", "collection_id"],
            },
        ),
        Tool(
            name="dugg_paste",
            description="Add raw content (text or HTML) directly — no URL required. Use for forwarded emails, newsletter excerpts, PDFs, notes, or any content that doesn't live at a public URL. Stored and indexed like any other resource.",
            inputSchema={
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Title for this content"},
                    "body": {"type": "string", "description": "The content — plain text or HTML"},
                    "source_type": {"type": "string", "enum": ["note", "email", "document"], "description": "What kind of content this is", "default": "email"},
                    "source_label": {"type": "string", "description": "Origin label (e.g. 'Substack', 'forwarded email', 'meeting notes')", "default": ""},
                    "published_at": {"type": "string", "description": "Original publication/send date (ISO 8601, e.g. email Date header). Optional.", "default": ""},
                    "note": {"type": "string", "description": "Why this content matters — context for future retrieval", "default": ""},
                    "tags": {"type": "array", "items": {"type": "string"}, "description": "Tags for categorization", "default": []},
                    "collection": {"type": "string", "description": "Collection name (uses Default if omitted)", "default": ""},
                    "api_key": {"type": "string", "description": "API key for authentication", "default": ""},
                },
                "required": ["title", "body"],
            },
        ),
        Tool(
            name="dugg_edit",
            description="Update a resource's metadata or content. Use after enrichment to push summary, tags, or corrected fields back to the server.",
            inputSchema={
                "type": "object",
                "properties": {
                    "resource_id": {"type": "string", "description": "ID of the resource to update"},
                    "title": {"type": "string", "description": "Updated title"},
                    "description": {"type": "string", "description": "Updated description"},
                    "summary": {"type": "string", "description": "Agent-generated summary"},
                    "note": {"type": "string", "description": "Updated context note"},
                    "source_type": {"type": "string", "description": "Corrected source type"},
                    "author": {"type": "string", "description": "Corrected author"},
                    "transcript": {"type": "string", "description": "Updated full content"},
                    "tags": {"type": "array", "items": {"type": "string"}, "description": "Tags to add (appended, not replaced)"},
                    "api_key": {"type": "string", "description": "API key for authentication", "default": ""},
                },
                "required": ["resource_id"],
            },
        ),
        Tool(
            name="dugg_my_servers",
            description="Get all servers you're subscribed to with their current scope — topic, top tags, recent activity. Use to decide where cross-posted content should route.",
            inputSchema={
                "type": "object",
                "properties": {
                    "api_key": {"type": "string", "description": "API key for authentication", "default": ""},
                },
            },
        ),
        Tool(
            name="dugg_rss_subscribe",
            description="Subscribe a collection to an RSS/Atom feed. The server polls the feed periodically and auto-ingests new entries as resources. Parameterized/authenticated feed URLs (e.g. ATP.fm premium) are preserved as-is; private links are flagged in raw_metadata so other viewers know they may need their own subscription.",
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Feed URL (RSS or Atom). May include auth query params."},
                    "collection": {"type": "string", "description": "Collection name (uses Default if omitted)", "default": ""},
                    "tag": {"type": "string", "description": "Tag to apply to every ingested entry", "default": "rss"},
                    "interval": {"type": "string", "description": "Poll interval: '30m', '1h', '6h', or bare seconds", "default": "1h"},
                    "api_key": {"type": "string", "description": "API key for authentication", "default": ""},
                },
                "required": ["url"],
            },
        ),
        Tool(
            name="dugg_rss_list",
            description="List all RSS subscriptions belonging to the authenticated user.",
            inputSchema={
                "type": "object",
                "properties": {
                    "api_key": {"type": "string", "description": "API key for authentication", "default": ""},
                },
            },
        ),
        Tool(
            name="dugg_rss_remove",
            description="Remove an RSS subscription by id. Previously-ingested resources are kept.",
            inputSchema={
                "type": "object",
                "properties": {
                    "subscription_id": {"type": "string", "description": "ID of the subscription to remove"},
                    "api_key": {"type": "string", "description": "API key for authentication", "default": ""},
                },
                "required": ["subscription_id"],
            },
        ),
        Tool(
            name="dugg_rss_poll",
            description="Manually poll a single RSS subscription (or all user subscriptions) right now, bypassing the scheduled interval.",
            inputSchema={
                "type": "object",
                "properties": {
                    "subscription_id": {"type": "string", "description": "ID of one subscription to poll. Omit to poll all.", "default": ""},
                    "api_key": {"type": "string", "description": "API key for authentication", "default": ""},
                },
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

        d.touch_user(user_id)

        if name == "dugg_add":
            result = await _handle_add(d, user_id, arguments)
        elif name == "dugg_search":
            result = _handle_search(d, user_id, arguments)
        elif name == "dugg_feed":
            result = _handle_feed(d, user_id, arguments)
        elif name == "dugg_tag":
            result = _handle_tag(d, arguments)
        elif name == "dugg_collections":
            result = _handle_collections(d, user_id)
        elif name == "dugg_share":
            result = _handle_share(d, user_id, arguments)
        elif name == "dugg_create_collection":
            result = _handle_create_collection(d, user_id, arguments)
        elif name == "dugg_create_user":
            result = _handle_create_user(d, arguments)
        elif name == "dugg_invite_user":
            result = _handle_invite_user(d, user_id, arguments)
        elif name == "dugg_invites":
            result = _handle_invites(d, user_id)
        elif name == "dugg_enrich":
            result = await _handle_enrich(d, arguments)
        elif name == "dugg_link":
            result = _handle_link(d, user_id, arguments)
        elif name == "dugg_related":
            result = _handle_related(d, user_id, arguments)
        elif name == "dugg_publish":
            result = _handle_publish(d, user_id, arguments)
        elif name == "dugg_unpublish":
            result = _handle_unpublish(d, user_id, arguments)
        elif name == "dugg_react":
            result = _handle_react(d, user_id, arguments)
        elif name == "dugg_reactions":
            result = _handle_reactions(d, user_id, arguments)
        elif name == "dugg_instance_create":
            result = _handle_instance_create(d, user_id, arguments)
        elif name == "dugg_instance_list":
            result = _handle_instance_list(d, user_id)
        elif name == "dugg_instance_update":
            result = _handle_instance_update(d, user_id, arguments)
        elif name == "dugg_invite":
            result = _handle_invite(d, user_id, arguments)
        elif name == "dugg_ban":
            result = _handle_ban(d, user_id, arguments)
        elif name == "dugg_appeal":
            result = _handle_appeal(d, user_id, arguments)
        elif name == "dugg_appeals":
            result = _handle_appeals(d, user_id, arguments)
        elif name == "dugg_appeal_resolve":
            result = _handle_appeal_resolve(d, user_id, arguments)
        elif name == "dugg_routing_manifest":
            result = _handle_routing_manifest(d, user_id)
        elif name == "dugg_publish_status":
            result = _handle_publish_status(d, user_id)
        elif name == "dugg_publish_retry":
            result = _handle_publish_retry(d)
        elif name == "dugg_events":
            result = _handle_events(d, user_id, arguments)
        elif name == "dugg_catchup":
            result = _handle_catchup(d, user_id, arguments)
        elif name == "dugg_mark_seen":
            result = _handle_mark_seen(d, user_id, arguments)
        elif name == "dugg_webhook_subscribe":
            result = _handle_webhook_subscribe(d, user_id, arguments)
        elif name == "dugg_webhook_list":
            result = _handle_webhook_list(d, user_id)
        elif name == "dugg_webhook_delete":
            result = _handle_webhook_delete(d, user_id, arguments)
        elif name == "dugg_ingest":
            result = _handle_ingest(d, user_id, arguments)
        elif name == "dugg_rate_limit":
            result = _handle_rate_limit(d, user_id, arguments)
        elif name == "dugg_rate_limit_status":
            result = _handle_rate_limit_status(d, user_id, arguments)
        elif name == "dugg_instance_policy":
            result = _handle_instance_policy(d, user_id, arguments)
        elif name == "dugg_get":
            result = _handle_get(d, user_id, arguments)
        elif name == "dugg_welcome":
            # Welcome already provides full orientation — no banner needed
            _welcomed_keys.add(api_key or "dugg_local_default")
            return _handle_welcome(d, user_id, user)
        elif name == "dugg_publish_clear":
            result = _handle_publish_clear(d, arguments)
        elif name == "dugg_publish_retry_selective":
            result = _handle_publish_retry_selective(d, arguments)
        elif name == "dugg_prune_inactive":
            result = _handle_prune_inactive(d, user_id, arguments)
        elif name == "dugg_set_successor":
            result = _handle_set_successor(d, user_id, arguments)
        elif name == "dugg_delete_resource":
            result = _handle_delete_resource(d, user_id, arguments)
        elif name == "dugg_paste":
            result = _handle_paste(d, user_id, arguments)
        elif name == "dugg_edit":
            result = _handle_edit(d, user_id, arguments)
        elif name == "dugg_my_servers":
            result = _handle_my_servers(d, user_id)
        elif name == "dugg_rss_subscribe":
            result = await _handle_rss_subscribe(d, user_id, arguments)
        elif name == "dugg_rss_list":
            result = _handle_rss_list(d, user_id)
        elif name == "dugg_rss_remove":
            result = _handle_rss_remove(d, user_id, arguments)
        elif name == "dugg_rss_poll":
            result = await _handle_rss_poll(d, user_id, arguments)
        else:
            return [TextContent(type="text", text=f"Unknown tool: {name}")]

        # Prepend first-call banner for new API keys
        return _maybe_prepend_banner(user, api_key, result)
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
        return [TextContent(type="text", text=(
            f"Rate limit exceeded.\n\n"
            f"  used: {rate_status['current']}/{rate_status['cap']} posts today\n"
            f"  member_for_days: {rate_status['days_member']}\n"
            f"  cap_formula: {rate_status['cap']} = initial + ({rate_status['days_member']} days × growth)\n\n"
            f"Your cap increases each day you're a member. "
            f"Check dugg_rate_limit_status() to see your current allowance. "
            f"Do not retry — wait until tomorrow (UTC midnight reset)."
        ))]

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

    # Apply index policy (summary/full/metadata_only) based on instance config
    agent_summary = args.get("summary", "")
    d.apply_index_policy(resource["id"], coll_id,
                         enriched_description=enriched.get("description", ""),
                         enriched_transcript=enriched.get("transcript", ""),
                         agent_summary=agent_summary)

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


def _handle_paste(d: DuggDB, user_id: str, args: dict) -> list[TextContent]:
    title = args["title"]
    body = args["body"]
    source_type = args.get("source_type", "email")
    source_label = args.get("source_label", "")
    published_at = args.get("published_at", "")
    note = args.get("note", "")
    tags = args.get("tags", [])
    collection_name = args.get("collection", "")

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

    rate_status = d.check_rate_limit(coll_id, user_id)
    if not rate_status["allowed"] and rate_status["cap"] != -1:
        return [TextContent(type="text", text=(
            f"Rate limit exceeded.\n\n"
            f"  used: {rate_status['current']}/{rate_status['cap']} posts today\n"
            f"  member_for_days: {rate_status['days_member']}\n\n"
            f"Do not retry — wait until tomorrow (UTC midnight reset)."
        ))]

    from dugg.db import _uuid
    res_id = _uuid()
    synthetic_url = f"dugg://content/{res_id}"

    metadata = {}
    if source_label:
        metadata["source_label"] = source_label
    if published_at:
        metadata["published_at"] = published_at

    resource = d.add_resource(
        url=synthetic_url,
        collection_id=coll_id,
        submitted_by=user_id,
        note=note,
        title=title,
        description=source_label,
        source_type=source_type,
        transcript=body,
        raw_metadata=metadata,
        tags=tags,
        tag_source="human" if tags else "agent",
    )

    word_count = len(body.split())
    summary = f"Pasted: {title}\n"
    summary += f"ID: {resource['id']}\n"
    summary += f"Type: {source_type}\n"
    if source_label:
        summary += f"Source: {source_label}\n"
    summary += f"Content: {word_count} words\n"
    if resource.get("tags"):
        summary += f"Tags: {', '.join(resource['tags'])}\n"
    return [TextContent(type="text", text=summary)]


def _handle_edit(d: DuggDB, user_id: str, args: dict) -> list[TextContent]:
    resource_id = args["resource_id"]
    resource = d.get_resource(resource_id)
    if not resource:
        return [TextContent(type="text", text=f"Resource {resource_id} not found")]

    accessible = d._accessible_collection_ids(user_id)
    if resource["collection_id"] not in accessible:
        return [TextContent(type="text", text=f"Access denied to resource {resource_id}")]

    tags = args.pop("tags", None)
    args.pop("resource_id", None)
    update_fields = {k: v for k, v in args.items() if v is not None and v != ""}

    if update_fields:
        updated = d.update_resource(resource_id, **update_fields)
    else:
        updated = resource

    if tags:
        for tag in tags:
            d._add_tag(resource_id, tag, "agent", _now())
        d.conn.commit()

    if update_fields.get("summary") or update_fields.get("description"):
        d.update_resource(resource_id, enriched_at=_now())

    result = d.get_resource(resource_id)
    tags_str = ", ".join(t["label"] for t in result.get("tags", []))
    lines = [f"Updated: {result.get('title') or result['url']}", f"ID: {result['id']}"]
    if tags_str:
        lines.append(f"Tags: {tags_str}")
    if result.get("summary"):
        lines.append(f"Summary: {result['summary'][:200]}")
    if result.get("enriched_at"):
        lines.append(f"Enriched: {result['enriched_at']}")
    return [TextContent(type="text", text="\n".join(lines))]


def _handle_my_servers(d: DuggDB, user_id: str) -> list[TextContent]:
    instances = d.list_instances(user_id)
    if not instances:
        return [TextContent(type="text", text="Not subscribed to any servers.")]

    lines = [f"{len(instances)} server(s):\n"]
    for inst in instances:
        lines.append(f"=== {inst['name']} [{inst['id']}] ===")
        if inst.get("topic"):
            lines.append(f"  Topic: {inst['topic']}")
        if inst.get("endpoint_url"):
            lines.append(f"  Endpoint: {inst['endpoint_url']}")

        scope = d.get_instance_scope(inst["id"])
        if scope["top_tags"]:
            lines.append(f"  Top tags: {', '.join(scope['top_tags'])}")
        if scope["recent_types"]:
            lines.append(f"  Content types: {', '.join(scope['recent_types'])}")
        lines.append(f"  Resources: {scope['resource_count']} ({scope['recent_count']} in last 7d)")
        lines.append("")

    return [TextContent(type="text", text="\n".join(lines))]


def _parse_rss_interval(raw: str) -> int:
    """Parse a human interval like '1h', '30m', '15s'. Clamps to >= 60s."""
    raw = (raw or "").strip().lower()
    if not raw:
        return 3600
    if raw.isdigit():
        return max(60, int(raw))
    try:
        num = int(raw[:-1])
    except ValueError:
        return 3600
    suffix = raw[-1]
    if suffix == "s":
        return max(60, num)
    if suffix == "m":
        return max(60, num * 60)
    if suffix == "h":
        return max(60, num * 3600)
    if suffix == "d":
        return max(60, num * 86400)
    return 3600


async def _handle_rss_subscribe(d: DuggDB, user_id: str, args: dict) -> list[TextContent]:
    import sqlite3
    from dugg.rss import sync_feed

    url = args["url"]
    collection_name = args.get("collection", "")
    tag_label = args.get("tag", "") or "rss"
    interval_raw = args.get("interval", "1h") or "1h"
    interval_seconds = _parse_rss_interval(interval_raw)

    if collection_name:
        coll_id = None
        for c in d.list_collections(user_id):
            if c["name"].lower() == collection_name.lower():
                coll_id = c["id"]
                break
        if not coll_id:
            result = d.create_collection(collection_name, user_id, visibility="private")
            coll_id = result["id"]
    else:
        coll_id = ensure_default_collection(user_id)

    try:
        sub = d.add_rss_subscription(
            user_id=user_id,
            collection_id=coll_id,
            feed_url=url,
            tag_label=tag_label,
            poll_interval_seconds=interval_seconds,
        )
    except sqlite3.IntegrityError:
        return [TextContent(type="text", text=f"Already subscribed to {url} for that collection.")]

    lines = [
        f"Subscribed: {url}",
        f"  ID: {sub['id']}",
        f"  Collection: {collection_name or 'Default'}",
        f"  Poll every: {interval_seconds}s",
        f"  Tag: {tag_label}",
    ]
    return [TextContent(type="text", text="\n".join(lines))]


def _handle_rss_list(d: DuggDB, user_id: str) -> list[TextContent]:
    subs = d.list_rss_subscriptions(user_id=user_id)
    if not subs:
        return [TextContent(type="text", text="No RSS subscriptions.")]

    lines = [f"{len(subs)} subscription(s):"]
    for s in subs:
        state = "paused" if not s["enabled"] else "active"
        last = (s.get("last_polled_at") or "").split("T")[0] or "never"
        lines.append(f"  [{state}] {s['feed_url']}")
        lines.append(f"    id: {s['id']}  every: {s['poll_interval_seconds']}s  last: {last}")
        if s.get("feed_title"):
            lines.append(f"    title: {s['feed_title']}")
    return [TextContent(type="text", text="\n".join(lines))]


def _handle_rss_remove(d: DuggDB, user_id: str, args: dict) -> list[TextContent]:
    sub_id = args["subscription_id"]
    sub = d.get_rss_subscription(sub_id)
    if not sub or sub.get("user_id") != user_id:
        return [TextContent(type="text", text="Subscription not found.")]
    d.remove_rss_subscription(sub_id)
    return [TextContent(type="text", text=f"Removed subscription {sub_id}.")]


async def _handle_rss_poll(d: DuggDB, user_id: str, args: dict) -> list[TextContent]:
    from dugg.rss import sync_feed
    sub_id = args.get("subscription_id", "")

    if sub_id:
        sub = d.get_rss_subscription(sub_id)
        if not sub or sub.get("user_id") != user_id:
            return [TextContent(type="text", text="Subscription not found.")]
        subs = [sub]
    else:
        subs = [s for s in d.list_rss_subscriptions(user_id=user_id) if s["enabled"]]
        if not subs:
            return [TextContent(type="text", text="No active subscriptions to poll.")]

    lines = []
    for sub in subs:
        try:
            result = await sync_feed(d, dict(sub))
            d.update_rss_subscription_state(
                sub["id"],
                etag=result["etag"],
                last_modified=result["last_modified"],
                seen_entry_ids=result["seen_entry_ids"],
                feed_title=result["feed_title"] or sub.get("feed_title") or "",
            )
            lines.append(f"  {sub['feed_url']}: +{result['new']} new, {result['skipped']} skipped (HTTP {result['status']})")
        except Exception as e:
            lines.append(f"  {sub['feed_url']}: ERROR {e}")
    return [TextContent(type="text", text="\n".join(lines))]


def _handle_search(d: DuggDB, user_id: str, args: dict) -> list[TextContent]:
    query = args["query"]
    tags = args.get("tags", [])
    collection = args.get("collection", "")
    limit = args.get("limit", 20)
    submitter_filter = args.get("submitted_by", "")
    if submitter_filter == "me":
        submitter_filter = d.get_user_pair_ids(user_id)

    # Resolve collection by name if provided
    coll_id = None
    if collection:
        for c in d.list_collections(user_id):
            if c["name"].lower() == collection.lower():
                coll_id = c["id"]
                break

    results = d.search(query, user_id, collection_id=coll_id, tags=tags or None,
                       submitted_by=submitter_filter or None, limit=limit)

    if not results:
        return [TextContent(type="text", text=f"No results for: {query}")]

    submitter_cache: dict[str, str] = {}
    lines = [f"Found {len(results)} result(s) for: {query}\n"]
    for r in results:
        tags_str = ", ".join(t["label"] for t in r.get("tags", []))
        submitter_name = ""
        if r.get("submitted_by"):
            if r["submitted_by"] not in submitter_cache:
                u = d.get_user(r["submitted_by"])
                submitter_cache[r["submitted_by"]] = u["name"] if u else r["submitted_by"]
            submitter_name = submitter_cache[r["submitted_by"]]
        lines.append(f"- [{r['id']}] {r.get('title') or r['url']}")
        if submitter_name:
            lines.append(f"  By: {submitter_name}")
        if tags_str:
            lines.append(f"  Tags: {tags_str}")
        if r.get("note"):
            lines.append(f"  Note: {r['note'][:200]}")
        lines.append(f"  URL: {r['url']}")
        lines.append("")
    return [TextContent(type="text", text="\n".join(lines))]


def _handle_feed(d: DuggDB, user_id: str, args: dict) -> list[TextContent]:
    d.mark_invite_onboarded(user_id)
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
    user = d.create_user(name)
    agent = d.create_agent_for_user(user["id"])
    from dugg.db import dugg_email_address
    server_url = d.get_config("server_url", "")
    email_addr = dugg_email_address(user["api_key"], server_url)
    email_line = f"\nEmail forwarding: {email_addr}\n" if email_addr else ""
    return [TextContent(type="text", text=(
        f"Created user: {user['name']}\n"
        f"ID: {user['id']}\n"
        f"User key:  {user['api_key']}\n"
        f"Agent key: {agent['api_key']}\n"
        f"{email_line}\n"
        f"The agent key is tied to this user — if the user is banned, the agent key stops working too.\n"
        f"Save both keys — they won't be shown again."
        + (f"\n\nForward emails to {email_addr} to add them as resources." if email_addr else "")
    ))]


def _handle_invite_user(d: DuggDB, user_id: str, args: dict) -> list[TextContent]:
    name = args["name"]
    expires_hours = args.get("expires_hours", 72)
    result = d.create_invite_token(user_id, name_hint=name, expires_hours=expires_hours)
    token = result["token"]

    # Build the invite URL from the instance's endpoint, if set
    instance = d.get_instance_for_owner(user_id)
    endpoint = ""
    instance_name = "a Dugg server"
    instance_topic = ""
    if instance:
        endpoint = instance.get("endpoint_url", "")
        instance_name = instance.get("name", instance_name)
        instance_topic = instance.get("topic", "")

    inviter = d.get_user(user_id)
    inviter_name = inviter["name"] if inviter else "Someone"

    invite_text = f"{inviter_name} invited you to {instance_name}!\n"
    if instance_topic:
        invite_text += f"{instance_topic}\n"
    invite_text += f"\nThis invite expires in {expires_hours} hours.\n"

    if endpoint:
        url = f"{endpoint.rstrip('/')}/invite/{token}"
        invite_text += (
            f"\n--- For humans ---\n"
            f"\n{url}\n"
            f"\nClick the link, enter your name, and you're in.\n"
            f"\n--- For agents ---\n"
            f"\nHand your agent this entire message. It covers the full setup.\n"
            f"\n1. Set up your local Dugg\n"
            f"\n   git clone https://github.com/kadedworkin/dugg-fyi.git\n"
            f"   cd dugg-fyi && uv sync\n"
            f"   dugg init\n"
            f"   dugg serve\n"
            f"\n   This is your personal knowledge base. It starts empty —\n"
            f"   that's fine. The shared server is where value comes from first.\n"
            f"\n2. Redeem this invite to subscribe to {instance_name}\n"
            f"\n   GET {url}\n"
            f"     Header: Accept: application/json\n"
            f"     → Returns redemption instructions\n"
            f"\n   POST {url}/redeem\n"
            f"     Header: Content-Type: application/json\n"
            f'     Body: {{"name": "{name}"}}\n'
            f"     → Returns human key + agent key + connection details\n"
            f"\n3. Connect to {instance_name} via MCP\n"
            f"\n   SSE endpoint: {endpoint.rstrip('/')}/sse\n"
            f"   Auth header: X-Dugg-Key: <agent_api_key from step 2>\n"
            f"\n4. Explore what's already here\n"
            f"\n   dugg_welcome  — orient yourself\n"
            f"   dugg_feed     — see what others have shared\n"
            f"   dugg_search   — find specific topics\n"
            f"   dugg_react    — signal value to publishers\n"
            f"\n   This is where day-one value lives. Browse, search, react.\n"
            f"   Use dugg_catchup later for incremental updates, or set up\n"
            f"   dugg_webhook_subscribe for push notifications.\n"
            f"\n   The more servers you subscribe to, the more signal flows\n"
            f"   to you. Each subscription is another curated source.\n"
            f"\n5. Email forwarding\n"
            f"\n   Forward emails to your personal Dugg address and they'll\n"
            f"   appear as searchable resources. Your address is computed from\n"
            f"   your API key + server hostname after you redeem.\n"
            f"   Format: {{api_key}}@{{server-hostname-with-double-dashes}}.dugg.fyi\n"
            f"\nPartner guide (read before first submission):\n"
            f"  https://github.com/kadedworkin/dugg-fyi/blob/main/PARTNER_AGENT.md"
        )
    else:
        invite_text += (
            f"\n--- For humans ---\n"
            f"\nRedeem via CLI:\n"
            f"  dugg redeem {token}\n"
            f"\n--- For agents ---\n"
            f"\n1. Set up your local Dugg\n"
            f"\n   git clone https://github.com/kadedworkin/dugg-fyi.git\n"
            f"   cd dugg-fyi && uv sync\n"
            f"   dugg init\n"
            f"   dugg serve\n"
            f"\n   Starts empty — the shared server is where value comes from first.\n"
            f"\n2. Redeem the invite to subscribe\n"
            f"\n   POST /invite/{token}/redeem\n"
            f"   Header: Content-Type: application/json\n"
            f'   Body: {{"name": "{name}"}}\n'
            f"   → Returns human key + agent key\n"
            f"\n3. Explore what's already here\n"
            f"\n   dugg_welcome, dugg_feed, dugg_search, dugg_react.\n"
            f"   Browse, search, react — that's the day-one experience.\n"
            f"\nPartner guide (read before first submission):\n"
            f"  https://github.com/kadedworkin/dugg-fyi/blob/main/PARTNER_AGENT.md"
        )

    return [TextContent(type="text", text=(
        f"Invite created for {name}\n"
        f"Token: {token}\n"
        f"Expires: {result['expires_at']}\n\n"
        f"--- Copy and send this to {name} ---\n\n"
        f"{invite_text}\n\n"
        f"--- End of invite message ---"
    ))]


def _handle_invites(d: DuggDB, user_id: str) -> list[TextContent]:
    from datetime import datetime, timezone
    tokens = d.list_invite_tokens(created_by=user_id)
    if not tokens:
        return [TextContent(type="text", text="No invite tokens found.")]
    now = datetime.now(timezone.utc)
    lines = []
    pending, redeemed, expired = 0, 0, 0
    for t in tokens:
        name = t.get("name_hint") or "(no name)"
        if t.get("redeemed_by"):
            redeemer = d.get_user(t["redeemed_by"])
            redeemer_name = redeemer["name"] if redeemer else t["redeemed_by"]
            onboard_status = " (onboarded)" if t.get("onboarded_at") else " (awaiting first connect)"
            lines.append(f"  {name} — redeemed by {redeemer_name} at {t['redeemed_at']}{onboard_status}")
            redeemed += 1
        elif datetime.fromisoformat(t["expires_at"]) < now:
            lines.append(f"  {name} — expired ({t['expires_at']})")
            expired += 1
        else:
            lines.append(f"  {name} — pending (token: {t['token']}, expires: {t['expires_at']})")
            pending += 1
    header = f"Invites: {pending} pending, {redeemed} redeemed, {expired} expired\n"
    return [TextContent(type="text", text=header + "\n".join(lines))]


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
    edges = d.get_related(resource_id, user_id=user_id, limit=limit)
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
    read_horizon_base_days = args.get("read_horizon_base_days", 30)
    read_horizon_growth = args.get("read_horizon_growth", 7)
    index_mode = args.get("index_mode", "summary")
    local_storage_cap_mb = args.get("local_storage_cap_mb", 512)
    result = d.create_instance(name, user_id, topic=topic, access_mode=access_mode,
                               rate_limit_initial=rate_limit_initial, rate_limit_growth=rate_limit_growth,
                               read_horizon_base_days=read_horizon_base_days, read_horizon_growth=read_horizon_growth,
                               index_mode=index_mode, local_storage_cap_mb=local_storage_cap_mb)
    lines = [f"Created Dugg instance: {result['name']} [{result['id']}]"]
    lines.append(f"Access: {result['access_mode']}")
    if topic:
        lines.append(f"Topic: {topic}")
    lines.append(f"Rate limit: {result['rate_limit_initial']} initial, +{result['rate_limit_growth']}/day")
    horizon_desc = "full history" if result['read_horizon_base_days'] == -1 else f"{result['read_horizon_base_days']}d base, +{result['read_horizon_growth']}d/week"
    lines.append(f"Read horizon: {horizon_desc}")
    lines.append(f"Index mode: {result.get('index_mode', 'summary')}")
    cap = result.get('local_storage_cap_mb', 512)
    lines.append(f"Storage cap: {'unlimited' if cap == -1 else f'{cap} MB'}")
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
    if args.get("endpoint_url"):
        updates["endpoint_url"] = args["endpoint_url"]
    if "read_horizon_base_days" in args and args["read_horizon_base_days"] is not None:
        updates["read_horizon_base_days"] = args["read_horizon_base_days"]
    if "read_horizon_growth" in args and args["read_horizon_growth"] is not None:
        updates["read_horizon_growth"] = args["read_horizon_growth"]
    if args.get("index_mode"):
        updates["index_mode"] = args["index_mode"]
    if "local_storage_cap_mb" in args and args["local_storage_cap_mb"] is not None:
        updates["local_storage_cap_mb"] = args["local_storage_cap_mb"]
    if args.get("pruning_mode"):
        updates["pruning_mode"] = args["pruning_mode"]
    if "pruning_grace_days" in args and args["pruning_grace_days"] is not None:
        updates["pruning_grace_days"] = args["pruning_grace_days"]
    # Handle onboarding_mode preset — overrides individual settings
    onboarding_mode = args.get("onboarding_mode", "")
    if onboarding_mode == "full_access":
        result = d.apply_onboarding_preset(instance_id, user_id, "full_access")
    elif onboarding_mode == "graduated":
        result = d.apply_onboarding_preset(instance_id, user_id, "graduated")
    else:
        result = d.update_instance(instance_id, user_id, **updates)
    if not result:
        return [TextContent(type="text", text=f"Instance {instance_id} not found or you're not the owner")]
    lines = [f"Updated instance: {result['name']} [{result['id']}]",
             f"Topic: {result['topic']}", f"Access: {result['access_mode']}"]
    if result.get("endpoint_url"):
        lines.append(f"Endpoint: {result['endpoint_url']}")
    horizon_base = result.get('read_horizon_base_days', 30)
    horizon_growth = result.get('read_horizon_growth', 7)
    horizon_desc = "full history" if horizon_base == -1 else f"{horizon_base}d base, +{horizon_growth}d/week"
    lines.append(f"Read horizon: {horizon_desc}")
    lines.append(f"Index mode: {result.get('index_mode', 'summary')}")
    cap = result.get('local_storage_cap_mb', 512)
    lines.append(f"Storage cap: {'unlimited' if cap == -1 else f'{cap} MB'}")
    return [TextContent(type="text", text="\n".join(lines))]


def _handle_instance_policy(d: DuggDB, user_id: str, args: dict) -> list[TextContent]:
    instance_id = args["instance_id"]
    policy = d.get_instance_policy(instance_id)
    if not policy:
        return [TextContent(type="text", text=f"Instance {instance_id} not found")]
    lines = [f"Policy for {policy['instance_name']} [{policy['instance_id']}]:\n"]
    lines.append(f"Onboarding mode: {policy['onboarding_mode']}")
    horizon_base = policy['read_horizon_base_days']
    horizon_desc = "full history" if horizon_base == -1 else f"{horizon_base}d base, +{policy['read_horizon_growth']}d/week"
    lines.append(f"Read horizon: {horizon_desc}")
    lines.append(f"Index mode: {policy['index_mode']}")
    cap = policy['local_storage_cap_mb']
    lines.append(f"Storage cap: {'unlimited' if cap == -1 else f'{cap} MB'}")
    lines.append(f"Rate limit: {policy['rate_limit_initial']} initial, +{policy['rate_limit_growth']}/day")
    lines.append(f"Pruning mode: {policy['pruning_mode']}")
    if policy['pruning_mode'] == 'interaction':
        lines.append(f"Pruning grace period: {policy.get('pruning_grace_days', 14)} days")
    lines.append(f"Access mode: {policy['access_mode']}")
    return [TextContent(type="text", text="\n".join(lines))]


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
    purge = args.get("purge", False)
    # Only owner can ban
    member = d.get_member_status(collection_id, user_id)
    if not member or member["role"] != "owner":
        return [TextContent(type="text", text="Only the collection owner can ban members")]
    result = d.ban_member(collection_id, target_user_id, cascade=cascade,
                          credit_threshold=credit_threshold, purge=purge)
    if result.get("error"):
        return [TextContent(type="text", text=result["error"])]
    lines = [f"Banned {len(result['banned'])} member(s)"]
    if purge and result.get("purged_resources", 0) > 0:
        lines.append(f"Purged {result['purged_resources']} resource(s) from banned users")
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
    on_behalf = ""
    if result["appealed_by"] != result["user_id"]:
        on_behalf = f" (filed by agent on behalf of {result['user_id']})"
    return [TextContent(type="text", text=f"Appeal submitted{on_behalf}. Credit score: {result['submissions']} submissions x {result['distinct_human_reactors']} distinct human reactors = {result['total']}")]


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
        lines.append(f"  Submissions: {a['submissions']} x Distinct human reactors: {a['distinct_human_reactors']} = Score: {a['total']}")
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


def _handle_publish_status(d: DuggDB, user_id: str) -> list[TextContent]:
    stats = d.get_publish_queue_status(user_id)
    total = sum(stats.values())
    if total == 0:
        return [TextContent(type="text", text="Publish queue is empty. Nothing has been queued for remote delivery yet.")]
    lines = [f"Publish queue status:"]
    lines.append(f"  Pending: {stats['pending']}")
    lines.append(f"  Delivering: {stats['delivering']}")
    lines.append(f"  Delivered: {stats['delivered']}")
    lines.append(f"  Failed: {stats['failed']}")
    if stats["failed"] > 0:
        failed = d.get_failed_publishes(limit=5)
        lines.append(f"\nRecent failures:")
        for f_entry in failed:
            lines.append(f"  - {f_entry['resource_id']} → {f_entry.get('instance_name', '?')} | {f_entry.get('last_error', 'unknown error')}")
    return [TextContent(type="text", text="\n".join(lines))]


def _handle_publish_retry(d: DuggDB) -> list[TextContent]:
    count = d.retry_failed_publishes()
    if count == 0:
        return [TextContent(type="text", text="No failed publishes to retry.")]
    return [TextContent(type="text", text=f"Reset {count} failed publish(es) back to pending. They'll be retried on the next sync cycle.")]


def _handle_events(d: DuggDB, user_id: str, args: dict) -> list[TextContent]:
    event_types = args.get("event_types", [])
    since = args.get("since", "")
    limit = args.get("limit", 50)
    actor_filter = args.get("actor_id", "")
    if actor_filter == "me":
        actor_filter = d.get_user_pair_ids(user_id)
    events = d.get_events(user_id, event_types=event_types or None, since=since or None,
                          actor_id=actor_filter or None, limit=limit)
    if not events:
        return [TextContent(type="text", text="No events found.")]
    actor_cache: dict[str, str] = {}
    lines = [f"{len(events)} event(s):\n"]
    for e in events:
        payload_summary = ", ".join(f"{k}: {v}" for k, v in e["payload"].items() if k not in ("transcript", "submitted_by"))
        actor_name = ""
        if e.get("actor_id"):
            if e["actor_id"] not in actor_cache:
                u = d.get_user(e["actor_id"])
                actor_cache[e["actor_id"]] = u["name"] if u else e["actor_id"]
            actor_name = actor_cache[e["actor_id"]]
        actor_str = f" by {actor_name}" if actor_name else ""
        lines.append(f"- [{e['event_type']}]{actor_str} {e['created_at']}")
        if payload_summary:
            lines.append(f"  {payload_summary}")
    return [TextContent(type="text", text="\n".join(lines))]


def _handle_catchup(d: DuggDB, user_id: str, args: dict) -> list[TextContent]:
    limit = args.get("limit", 10)
    oldest_first = args.get("oldest_first", True)
    events = d.get_unseen_events(user_id, limit=limit, oldest_first=oldest_first)
    cursor_ts = d.get_cursor(user_id)
    if not events:
        if cursor_ts:
            return [TextContent(type="text", text=f"You're caught up. Last checked: {cursor_ts}")]
        return [TextContent(type="text", text="No events yet. This is your first catchup — all future events will appear here.")]
    lines = [f"{len(events)} unseen event(s)" + (f" (since {cursor_ts})" if cursor_ts else " (first catchup)") + ":\n"]
    for i, e in enumerate(events, 1):
        payload_summary = ", ".join(f"{k}: {v}" for k, v in e["payload"].items() if k != "transcript")
        lines.append(f"{i}. [{e['event_type']}] {e['created_at']}")
        if payload_summary:
            lines.append(f"   {payload_summary}")
    lines.append(f"\nCall dugg_mark_seen to advance your cursor.")
    return [TextContent(type="text", text="\n".join(lines))]


def _handle_mark_seen(d: DuggDB, user_id: str, args: dict) -> list[TextContent]:
    seen_until = args.get("seen_until", "")
    result = d.update_cursor(user_id, last_seen_at=seen_until or None)
    return [TextContent(type="text", text=f"Cursor advanced to {result['last_seen_at']}. Future catchups will start from here.")]


def _handle_webhook_subscribe(d: DuggDB, user_id: str, args: dict) -> list[TextContent]:
    instance_id = args.get("instance_id") or None
    callback_url = args["callback_url"]
    event_types = args.get("event_types", [])
    secret = args.get("secret", "")
    result = d.subscribe_webhook(user_id, callback_url, instance_id=instance_id, event_types=event_types, secret=secret)
    lines = [f"Webhook subscribed: {result['callback_url']}"]
    lines.append(f"Instance: {instance_id or 'all (server-wide)'}")
    if event_types:
        lines.append(f"Events: {', '.join(event_types)}")
    else:
        lines.append("Events: all")
    if secret:
        lines.append("Signing: HMAC-SHA256 enabled")
    return [TextContent(type="text", text="\n".join(lines))]


def _handle_webhook_list(d: DuggDB, user_id: str) -> list[TextContent]:
    webhooks = d.list_webhooks(user_id)
    if not webhooks:
        return [TextContent(type="text", text="No webhook subscriptions.")]
    lines = [f"{len(webhooks)} webhook(s):\n"]
    for wh in webhooks:
        types_str = ", ".join(wh["event_types"]) if wh["event_types"] else "all"
        lines.append(f"- [{wh['id']}] {wh['callback_url']}")
        lines.append(f"  Instance: {wh['instance_id']} | Events: {types_str} | Status: {wh['status']}")
        if wh["failure_count"] > 0:
            lines.append(f"  Failures: {wh['failure_count']}")
    return [TextContent(type="text", text="\n".join(lines))]


def _handle_webhook_delete(d: DuggDB, user_id: str, args: dict) -> list[TextContent]:
    webhook_id = args["webhook_id"]
    deleted = d.unsubscribe_webhook(webhook_id, user_id)
    if not deleted:
        return [TextContent(type="text", text=f"Webhook {webhook_id} not found or not yours")]
    return [TextContent(type="text", text=f"Webhook {webhook_id} deleted.")]


def _handle_ingest(d: DuggDB, user_id: str, args: dict) -> list[TextContent]:
    url = args["url"]
    source_instance_id = args["source_instance_id"]
    collection_name = args.get("collection", "")

    # Resolve target collection
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

    resource_data = {
        "url": url,
        "title": args.get("title", ""),
        "description": args.get("description", ""),
        "source_type": args.get("source_type", "unknown"),
        "author": args.get("author", ""),
        "note": args.get("note", ""),
        "tags": args.get("tags", []),
    }
    result = d.ingest_remote_publish(resource_data, source_instance_id, coll_id)
    if not result:
        return [TextContent(type="text", text="Ingest failed — invalid URL or collection")]
    if result["status"] == "duplicate":
        return [TextContent(type="text", text=f"Already exists: {url} (ID: {result['id']})")]
    return [TextContent(type="text", text=f"Ingested: {result.get('title') or url}\nID: {result['id']}\nFrom instance: {source_instance_id}")]


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
    remaining = status['cap'] - status['current']
    if status['allowed']:
        status_line = f"Can post {remaining} more today"
    else:
        status_line = "Rate limit reached — resets at UTC midnight"
    return [TextContent(type="text", text=f"Rate limit status:\n  Today: {status['current']}/{status['cap']} posts used\n  Member for: {status['days_member']} day(s)\n  {status_line}")]


def _handle_publish_clear(d: DuggDB, args: dict) -> list[TextContent]:
    target = args.get("target_instance_id", "") or None
    count = d.clear_failed_publishes(target_instance_id=target)
    if count == 0:
        return [TextContent(type="text", text="No failed publishes to clear.")]
    scope = f" for instance {target}" if target else ""
    return [TextContent(type="text", text=f"Cleared {count} failed publish(es){scope}.")]


def _handle_publish_retry_selective(d: DuggDB, args: dict) -> list[TextContent]:
    publish_id = args.get("publish_id", "") or None
    target = args.get("target_instance_id", "") or None
    count = d.retry_publish_selective(publish_id=publish_id, target_instance_id=target)
    if count == 0:
        return [TextContent(type="text", text="No failed publishes matching criteria.")]
    return [TextContent(type="text", text=f"Reset {count} failed publish(es) back to pending.")]


def _handle_prune_inactive(d: DuggDB, user_id: str, args: dict) -> list[TextContent]:
    collection_id = args["collection_id"]
    execute = args.get("execute", False)
    # Only owner can prune
    member = d.get_member_status(collection_id, user_id)
    if not member or member["role"] != "owner":
        return [TextContent(type="text", text="Only the collection owner can prune inactive members")]
    inactive = d.get_inactive_members(collection_id)
    if not inactive:
        return [TextContent(type="text", text="No inactive members past grace period. Everyone is contributing.")]
    if not execute:
        lines = [f"{len(inactive)} inactive member(s) past grace period:\n"]
        for m in inactive:
            lines.append(f"- {m['name']} ({m['user_id']}) — joined {m['joined_at']}, 0 submissions, 0 reactions")
        lines.append(f"\nRun with execute=true to ban them.")
        return [TextContent(type="text", text="\n".join(lines))]
    result = d.prune_inactive_members(collection_id)
    return [TextContent(type="text", text=f"Pruned {result['count']} inactive member(s).")]


def _handle_set_successor(d: DuggDB, user_id: str, args: dict) -> list[TextContent]:
    instance_id = args["instance_id"]
    successor_id = args["successor_id"]
    result = d.set_successor(instance_id, user_id, successor_id)
    if not result:
        return [TextContent(type="text", text="Failed — instance not found, you're not the owner, or successor is not a subscriber")]
    successor = d.get_user(successor_id)
    name = successor["name"] if successor else successor_id
    return [TextContent(type="text", text=f"Successor set to {name} ({successor_id}) for instance {instance_id}. If ownership needs to transfer, it's ready.")]


def _handle_delete_resource(d: DuggDB, user_id: str, args: dict) -> list[TextContent]:
    resource_id = args["resource_id"]
    collection_id = args["collection_id"]
    result = d.delete_resource(resource_id, collection_id, user_id)
    if result.get("error"):
        return [TextContent(type="text", text=result["error"])]
    return [TextContent(type="text", text=f"Deleted resource {result['deleted']}: {result.get('title') or result['url']}")]


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


# Track which API keys have already seen the first-call banner
_welcomed_keys: set[str] = set()


def _maybe_prepend_banner(user: dict, api_key: Optional[str], result: list[TextContent]) -> list[TextContent]:
    """Prepend a one-line orientation banner on the first tool call from a new API key."""
    key = api_key or "dugg_local_default"
    if key in _welcomed_keys:
        return result
    _welcomed_keys.add(key)

    d = get_db()
    instances = d.list_instances(user["id"])
    if instances:
        topics = [f"{inst['name']}: {inst['topic']}" for inst in instances if inst.get("topic")]
        banner = f"Welcome to Dugg, {user['name']}. "
        if topics:
            banner += f"Connected instance(s): {'; '.join(topics)}. "
        banner += "Run dugg_welcome for full orientation."
    else:
        banner = f"Welcome to Dugg, {user['name']}. Run dugg_welcome for orientation."

    return [TextContent(type="text", text=f"[{banner}]\n\n{result[0].text}")] + result[1:]


def _handle_welcome(d: DuggDB, user_id: str, user: dict) -> list[TextContent]:
    """One-call orientation: instances, recent feed, rate limits, and resource count."""
    parent = d.get_parent_user(user_id)
    if parent:
        lines = [
            f"Welcome to Dugg, {user['name']}!",
            f"You're operating as an agent for {parent['name']}.",
            f"Your human's key: {parent['api_key']}",
            f"Tell them to store it somewhere safe — it won't be shown again.",
            f"If {parent['name']} gets banned, your access ends too.\n",
        ]
    else:
        lines = [f"Welcome to Dugg, {user['name']}!\n"]

    # Instance topics (routing manifest)
    instances = d.list_instances(user_id)
    if instances:
        lines.append("Instances you're connected to:")
        for inst in instances:
            mode = f" [{inst['access_mode']}]" if inst.get("access_mode") else ""
            topic = f" — {inst['topic']}" if inst.get("topic") else ""
            lines.append(f"  - {inst['name']}{mode}{topic}")
        lines.append("")
    else:
        lines.append("No instances yet. You're running in local mode.\n")

    # Collections summary
    collections = d.list_collections(user_id)
    total_resources = 0
    if collections:
        lines.append(f"Collections: {len(collections)}")
        for c in collections:
            count = d.conn.execute(
                "SELECT COUNT(*) FROM resources WHERE collection_id = ?", (c["id"],)
            ).fetchone()[0]
            total_resources += count
            lines.append(f"  - {c['name']} ({count} resources)")
        lines.append("")
    else:
        lines.append("No collections yet.\n")

    lines.append(f"Total resources: {total_resources}\n")

    # Recent feed (last 3 items)
    feed = d.get_feed(user_id, limit=3)
    if feed:
        lines.append("Recent activity:")
        for r in feed:
            title = r.get("title") or r["url"]
            lines.append(f"  - {title}")
            lines.append(f"    {r['url']}")
        lines.append("")
    else:
        lines.append("No resources yet. Try: dugg_add(url=\"...\", note=\"...\")\n")

    # Rate limit status for each collection
    rate_info = []
    for c in collections:
        status = d.check_rate_limit(c["id"], user_id)
        if status["cap"] != -1:
            rate_info.append(f"  - {c['name']}: {status['current']}/{status['cap']} posts today (member {status['days_member']}d)")
    if rate_info:
        lines.append("Rate limits:")
        lines.extend(rate_info)
        lines.append("")

    # Email forwarding
    from dugg.db import dugg_email_address
    server_url = d.get_config("server_url", "")
    target_key = parent["api_key"] if parent else user["api_key"]
    email_addr = dugg_email_address(target_key, server_url)
    if email_addr:
        lines.append(f"Email forwarding: {email_addr}")
        lines.append("  Forward emails to this address → they appear as searchable resources.")
        lines.append("")

    # Staying updated
    lines.append("Staying updated:")
    lines.append("  dugg_catchup      — poll for new activity since your last check (uses read cursors)")
    lines.append("  dugg_webhook_subscribe — push notifications to a URL (Slack, HTTP, HMAC-signed)")
    lines.append("  /feed/{key}       — your human's browser feed (also serves Atom XML for RSS readers)")
    lines.append("")

    # Tip based on state
    if total_resources == 0:
        lines.append("Get started: share a link with dugg_add(url=\"...\", note=\"why this matters\")")
    elif not instances:
        lines.append("Tip: create an instance with dugg_instance_create to start publishing.")
    else:
        lines.append("Tip: search with dugg_search(query=\"...\") or browse with dugg_feed().")

    if parent:
        lines.append("")
        lines.append("Read PARTNER_AGENT.md before your first submission:")
        lines.append("  https://github.com/kadedworkin/dugg-fyi/blob/main/PARTNER_AGENT.md")
        lines.append("It covers behavioral norms, rate limits, catchup patterns, and how to stay active.")

    return [TextContent(type="text", text="\n".join(lines))]


# --- Main ---

def main():
    """Run the Dugg MCP server over stdio with background sync daemon and local web UI."""
    local_port = int(os.environ.get("DUGG_LOCAL_PORT", "8411"))

    async def _run():
        d = get_db()
        sync_task = start_sync_daemon(d, interval=30)

        # Start a localhost-only HTTP server for the web UI (paste form, feed, admin)
        http_task = None
        try:
            import uvicorn
            from dugg.http import create_app
            app = create_app()
            config = uvicorn.Config(app, host="127.0.0.1", port=local_port, log_level="warning")
            http_server = uvicorn.Server(config)
            http_task = asyncio.create_task(http_server.serve())
        except Exception:
            pass

        try:
            async with stdio_server() as (read_stream, write_stream):
                await server.run(read_stream, write_stream, server.create_initialization_options())
        finally:
            sync_task.cancel()
            if http_task:
                http_task.cancel()

    asyncio.run(_run())


if __name__ == "__main__":
    main()
