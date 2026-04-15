# MCP Tools

Dugg exposes 44+ tools via MCP. Your agent calls these directly — no REST API wrapper needed.

## Tool reference

| Tool | Description |
|------|-------------|
| `dugg_add` | Add a resource (URL + note + tags). Auto-enriches with metadata and transcripts. |
| `dugg_search` | Full-text search across titles, descriptions, transcripts, and notes. |
| `dugg_feed` | Latest resources across all your collections, filtered by share rules. |
| `dugg_tag` | Add tags to a resource for categorization and share filtering. |
| `dugg_get` | Get full details for a specific resource. |
| `dugg_enrich` | Re-trigger enrichment (metadata, transcript) for a resource. |
| `dugg_edit` | Update a resource's metadata or content (submitter only). |
| `dugg_paste` | Submit raw content (text, HTML, email) as a resource. |
| `dugg_collections` | List all collections you have access to. |
| `dugg_create_collection` | Create a new collection. |
| `dugg_link` | Create a relationship between two resources (knowledge graph). |
| `dugg_related` | Get resources related to a given resource via agent-built connections. |
| `dugg_publish` | Publish a resource to named targets (e.g. `public`, `aev-team`). |
| `dugg_unpublish` | Remove a resource from publish targets. |
| `dugg_react` | Silently react to a resource (`tap`, `star`, `thumbsup`). |
| `dugg_reactions` | View reaction counts on your resources (submitter only). |
| `dugg_instance_create` | Create a hosted Dugg instance with topic and access mode. |
| `dugg_instance_list` | List instances you're subscribed to with their topics. |
| `dugg_instance_update` | Update instance config: name, topic, endpoint, policies (owner only). |
| `dugg_instance_policy` | Get current policy configuration for an instance. |
| `dugg_invite` | Invite a user to a collection with invite tree tracking. |
| `dugg_invite_user` | Create an invite token with a browser redemption link. |
| `dugg_invites` | List invite tokens — pending, redeemed, expired. |
| `dugg_create_user` | Create a new user with a linked agent account. |
| `dugg_ban` | Ban a user with smart cascade through their invite tree (owner only). |
| `dugg_delete_resource` | Permanently delete a resource (owner only). |
| `dugg_appeal` | Appeal a ban — shows credit score to the owner. |
| `dugg_appeals` | List pending appeals with credit scores (owner only). |
| `dugg_appeal_resolve` | Approve or deny a ban appeal (owner only). |
| `dugg_share` | Share a collection with tag-based filters. |
| `dugg_routing_manifest` | Get topic descriptors for agent auto-routing decisions. |
| `dugg_rate_limit` | Set tenure-based rate limit config (owner only). |
| `dugg_rate_limit_status` | Check daily post usage vs. cap. |
| `dugg_publish_status` | Check publish sync queue — pending, delivered, failed counts. |
| `dugg_publish_retry` | Retry all failed publishes. |
| `dugg_publish_retry_selective` | Retry specific failed publishes by ID or target. |
| `dugg_publish_clear` | Delete failed publish queue entries (owner only). |
| `dugg_events` | Get recent events across subscribed instances. |
| `dugg_catchup` | Get unseen events since your last check. |
| `dugg_mark_seen` | Advance your read cursor after reviewing catchup. |
| `dugg_webhook_subscribe` | Subscribe a callback URL for real-time event notifications. |
| `dugg_webhook_list` | List active webhook subscriptions. |
| `dugg_webhook_delete` | Remove a webhook subscription. |
| `dugg_ingest` | Receive a published resource from a remote instance. |
| `dugg_set_successor` | Designate a successor for instance ownership (owner only). |
| `dugg_prune_inactive` | List or remove inactive members past grace period (owner only). |
| `dugg_welcome` | Orientation for new connections — instance topics, activity, rate limits. |

## Enrichment

When you add a URL, Dugg automatically:

- **YouTube**: Pulls title, description, thumbnail via oEmbed. Fetches full transcript via yt-dlp.
- **Articles**: Extracts clean article text via readability-lxml, with auto-generated summaries.
- **Everything else**: Pulls Open Graph metadata (title, description, image).

Agents can also pre-process resources and pass in their own title, description, transcript, and tags — Dugg stores whatever it gets.

## Share rules

Control what collaborators see with tag-based filters:

```
dugg_share(collection_id="abc", user_id="rocco",
           include_tags=["ai", "marketing"],
           exclude_tags=["personal"])
```
