# Dugg

**Agentic-first shared knowledge base.** Agents process, Dugg stores, everyone queries.

Dugg is an MCP server that acts as a shared, searchable filing cabinet for links, articles, videos, and resources. Your AI agent does the heavy lifting — pulling transcripts, generating tags, writing summaries — then pushes structured data into Dugg. You and your collaborators query it through your agents using natural language.

## How it works

1. **You share a link** (via your agent, a Slack channel, a share sheet — whatever)
2. **Your agent processes it** — pulls metadata, transcripts, generates tags
3. **Dugg stores it** — indexed for full-text search, organized into collections
4. **Anyone with access can query it** — "that video about webhook architectures" just works

## Key concepts

- **Resources** — URLs with enriched metadata, transcripts, notes, and tags
- **Collections** — Groups of resources (private or shared)
- **Tags** — Human or agent-generated labels for categorization and filtering
- **Share rules** — Tag-based filters that control what collaborators see (e.g., share AI stuff with colleagues who care, but not personal vlogs)
- **Publishing** — Flag resources for publishing to named targets (e.g., `public`, `aev-team`, `inner-circle`). One local source of truth, selective outward publishing
- **Silent reactions** — Subscribers can tap/star resources. Only the publisher sees aggregate counts — no public signal, no social pressure
- **Instances** — Hosted Dugg deployments with topics and access modes (public or invite-only)
- **Invite trees** — Member-invites-member with tracked lineage. Accountability flows through the chain
- **Ban cascades** — Ban a user and prune their invite tree. Depth 1 = hard prune, depth 2+ = credit score decides survival
- **Appeals** — Banned users appeal with their contribution history. Owner (or owner's agent) decides
- **Rate limiting** — Tenure-based submission caps. New members start at N posts/day (owner-configured, default 5), growing by X per day of membership. Longer in the mix = higher cap. Prevents fresh-account spam without punishing established contributors
- **Publish sync** — Durable outbound delivery of published resources to remote Dugg instances. Queue with exponential backoff retry (30s → 2m → 8m → 32m → ~2h). Failed publishes can be retried manually
- **Event log** — Every significant action (resource added, published, member joined, banned, publish delivered, reaction added) is logged. Agents poll events or use catchup to stay informed
- **Read cursors** — Per-user cursor tracking so agents can ask "what's new since I last checked" without managing state themselves. Powers the catchup flow
- **Webhooks** — Subscribe callback URLs to receive real-time POST notifications for events on subscribed instances. HMAC-SHA256 signing, auto-pause after 5 consecutive failures
- **Remote ingest** — Receive published resources from other Dugg instances. URL-level deduplication, source instance tracked in metadata
- **Auto-routing** — Agents pull topic descriptors from subscribed instances and auto-route published content to matching targets
- **Invite tokens** — Generate short-lived, single-use invite tokens to onboard new users. Send them a link via any channel (iMessage, email, Telegram, Discord). They click, enter their name, and get an API key — no CLI or agent needed on their end
- **Browser feed** — Every user gets a `/feed/{key}` URL that renders an HTML feed in any browser. Also serves Atom XML for RSS readers. Read-only access without any setup
- **Feed** — Reverse-chron view of everything across your accessible collections

## Quick start

### Install

```bash
# Clone the repo
git clone https://github.com/kadedworkin/dugg-fyi.git
cd dugg-fyi

# Install with uv (recommended)
uv sync

# Or with pip
pip install -e .
```

### Set up and run

```bash
# Initialize the database
dugg init

# Create a user and get an API key
dugg add-user "Kade"

# Start the MCP server (stdio mode for local agent connections)
dugg serve

# Or with a custom database path
dugg --db /path/to/dugg.db serve
```

### HTTP/SSE mode (remote agents)

```bash
# Start the HTTP server with SSE transport
dugg serve --transport http --port 8411

# Custom host/port
dugg serve --transport http --host 127.0.0.1 --port 9000

# With a custom database
dugg --db /path/to/dugg.db serve --transport http
```

**Endpoints:**

| Endpoint | Method | Auth | Description |
|----------|--------|------|-------------|
| `/sse` | GET | Key | MCP SSE transport — connect MCP clients over HTTP |
| `/messages` | POST | Key | MCP message endpoint (used by SSE clients) |
| `/ingest` | POST | Key | Receive published resources from remote instances |
| `/tools/{name}` | POST | Key | HTTP dispatch for any MCP tool |
| `/events/stream` | GET | Key | SSE stream of real-time Dugg events |
| `/invite/{token}` | GET | None | Browser invite redemption page |
| `/invite/{token}/redeem` | POST | None | Process invite (form or JSON) |
| `/feed/{key}` | GET | None | Browser-friendly feed (HTML or Atom XML) |
| `/health` | GET | None | Liveness check |

**Authentication:** Endpoints marked "Key" require an `X-Dugg-Key` header. Invite and feed endpoints are unauthenticated by design — the token/key in the URL acts as the credential.

**Example — ingest via HTTP:**

```bash
curl -X POST http://localhost:8411/ingest \
  -H "X-Dugg-Key: dugg_your_api_key" \
  -H "Content-Type: application/json" \
  -d '{
    "resource": {"url": "https://example.com/article", "title": "Cool Article"},
    "source_instance_id": "remote123"
  }'
```

**Example — call a tool via HTTP:**

```bash
curl -X POST http://localhost:8411/tools/dugg_search \
  -H "X-Dugg-Key: dugg_your_api_key" \
  -H "Content-Type: application/json" \
  -d '{"query": "webhook architectures"}'
```

### Connect to Claude Code

Add to your Claude Code MCP config (`~/.claude/claude_desktop_config.json` or equivalent):

```json
{
  "mcpServers": {
    "dugg": {
      "command": "dugg",
      "env": {
        "DUGG_DB_PATH": "/path/to/dugg.db"
      }
    }
  }
}
```

### Connect to OpenClaw

Add to your OpenClaw config:

```json
{
  "mcp": {
    "servers": {
      "dugg": {
        "command": "dugg",
        "env": {
          "DUGG_DB_PATH": "/path/to/dugg.db"
        }
      }
    }
  }
}
```

## MCP Tools

| Tool | Description |
|------|-------------|
| `dugg_add` | Add a resource (URL + note + tags). Auto-enriches with metadata and transcripts. |
| `dugg_search` | Full-text search across titles, descriptions, transcripts, and notes. |
| `dugg_feed` | Latest resources across all your collections, filtered by share rules. |
| `dugg_tag` | Add tags to a resource for categorization and share filtering. |
| `dugg_get` | Get full details for a specific resource. |
| `dugg_enrich` | Re-trigger enrichment (metadata, transcript) for a resource. |
| `dugg_collections` | List all collections you have access to. |
| `dugg_create_collection` | Create a new collection. |
| `dugg_link` | Create a relationship between two resources (builds a knowledge graph). |
| `dugg_related` | Get resources related to a given resource via agent-built connections. |
| `dugg_publish` | Publish a resource to named targets (e.g. `public`, `aev-team`). Only submitter can publish. |
| `dugg_unpublish` | Remove a resource from publish targets. Omit targets to unpublish from all. |
| `dugg_react` | Silently react to a resource (`tap`, `star`, `thumbsup`). Only the publisher sees counts. |
| `dugg_reactions` | View reaction counts on your resources. Only visible to the resource submitter. |
| `dugg_instance_create` | Create a hosted Dugg instance with topic and access mode. |
| `dugg_instance_list` | List instances you're subscribed to with their topics. |
| `dugg_instance_update` | Update an instance's topic or access mode (owner only). |
| `dugg_invite` | Invite a user to a collection with invite tree tracking. |
| `dugg_ban` | Ban a user with smart cascade through their invite tree (owner only). |
| `dugg_appeal` | Appeal a ban — shows your credit score to the owner. |
| `dugg_appeals` | List pending appeals with credit scores (owner only). |
| `dugg_appeal_resolve` | Approve or deny a ban appeal (owner only). |
| `dugg_routing_manifest` | Get topic descriptors for agent auto-routing decisions. |
| `dugg_rate_limit` | Set tenure-based rate limit config for an instance (owner only). |
| `dugg_rate_limit_status` | Check your current daily post usage vs. cap for a collection. |
| `dugg_publish_status` | Check publish sync queue status — pending, delivered, failed counts. |
| `dugg_publish_retry` | Retry all failed publishes — resets them back to pending. |
| `dugg_events` | Get recent events across subscribed instances (add, publish, join, ban, reaction). |
| `dugg_catchup` | Get unseen events since your last check. Oldest-first by default for timeline reading. |
| `dugg_mark_seen` | Advance your read cursor after reviewing catchup results. |
| `dugg_webhook_subscribe` | Subscribe a callback URL to receive real-time event notifications. |
| `dugg_webhook_list` | List your active webhook subscriptions. |
| `dugg_webhook_delete` | Remove a webhook subscription. |
| `dugg_ingest` | Receive a published resource from a remote Dugg instance. Deduplicates by URL. |
| `dugg_share` | Share a collection with another user, with optional tag filters. |
| `dugg_create_user` | Create a new user and get their API key. |
| `dugg_invite_user` | Create an invite token with a browser redemption link — send via any channel. |
| `dugg_welcome` | Orientation for new connections. Returns instance topics, recent activity, and rate limit status. |

## Architecture

```
┌─────────────────────────────────────────────┐
│  Agent Layer (yours — Claude, OpenClaw, etc) │
│                                             │
│  - Enrichment: metadata, transcripts, tags  │
│  - Judgment: auto-routing, ban appeals      │
│  - Orchestration: catchup → process → act   │
│  - All LLM costs live here                  │
└──────────────────┬──────────────────────────┘
                   │
                   │ MCP protocol (stdio or HTTP/SSE)
                   │
┌──────────────────▼──────────────────────────┐
│  Dugg MCP Server — tool handlers (38)       │
│                                             │
│  - Auth (API key per user)                  │
│  - Rate limiting (tenure-based)             │
│  - Event emission + webhook dispatch        │
│  - Publish sync daemon (async background)   │
└───────┬─────────────────────┬───────────────┘
        │                     │
        │ SQLite              │ HTTP (:8411)
        │                     │
┌───────▼───────┐    ┌────────▼────────┐
│  Storage      │    │  Endpoints      │
│  16 tables    │    │  /ingest        │
│  FTS5 index   │    │  /tools/{name}  │
│  Event log    │    │  /events/stream │
│  Publish queue│    │  /feed/{key}    │
│  Invite trees │    │  /invite/{token}│
└───────────────┘    └─────────────────┘
```

**Zero LLM cost in the server.** Dugg is storage, indexing, and retrieval. All AI processing — enrichment, tagging, routing decisions, appeal evaluation — happens in the agent layer, using the user's own tokens.

## Share rules

Control what collaborators see with tag-based filters:

```
# Share everything tagged "ai" or "marketing" with Rocco
# Exclude anything tagged "personal"
dugg_share(collection_id="abc", user_id="rocco", include_tags=["ai", "marketing"], exclude_tags=["personal"])
```

## Publishing

Flag individual resources for publishing to named targets. Your local Dugg is the source of truth — publishing pushes selected content to remote instances.

```
# Publish to multiple targets
dugg_publish(resource_id="abc123", targets=["public", "aev-team"])

# Unpublish from a specific target
dugg_unpublish(resource_id="abc123", targets=["public"])

# Unpublish from everything
dugg_unpublish(resource_id="abc123")
```

**How it works:**
- Every resource starts local and unpublished
- You (or your agent) flag resources with named publish targets
- Each target maps to a remote Dugg instance (e.g., `public` → dugg.fyi, `aev-team` → team server)
- Non-concentric circles: publish to any combination of targets independently
- Only the resource submitter can publish/unpublish

## Silent reactions

Subscribers can silently react to resources. The publisher sees aggregate counts — nobody else sees anything.

```
# React to a resource (tap, star, or thumbsup)
dugg_react(resource_id="abc123", reaction="tap")

# Publisher checks reactions on a specific resource
dugg_reactions(resource_id="abc123")

# Publisher gets summary across all their resources
dugg_reactions()
```

**Privacy model:**
- The reactor knows they reacted. The publisher sees aggregate counts. Nobody else knows anything.
- No public like counts, no emoji piles, no social proof pressure
- Reactions are idempotent — same user + same type = one reaction
- Only the resource submitter can view reaction data

## Instances

Hosted Dugg deployments with topic descriptors and access control.

```
# Create an invite-only instance
dugg_instance_create(name="Chino Bandito", topic="Food, restaurants, recipes in the Phoenix area", access_mode="invite")

# Create a public instance anyone can subscribe to
dugg_instance_create(name="AI Research", topic="AI agents, LLMs, machine learning papers", access_mode="public")

# Update the topic (owner only)
dugg_instance_update(instance_id="abc123", topic="Updated topic description")
```

**Access modes:**
- `invite` — member-invites-member. Grow organically through trust networks.
- `public` — anyone can subscribe. Open knowledge base.

## Invite trees

Every invitation is tracked. When someone joins via invite, the system records who brought them in, forming a tree rooted at the collection owner.

```
# Invite someone to a collection
dugg_invite(collection_id="abc", user_id="rocco")

# Rocco can now invite others — tracked in the tree
dugg_invite(collection_id="abc", user_id="clint")  # called by Rocco
```

The invite tree enables accountability: the inviter is responsible for who they bring in.

## Invite tokens

Onboard new users without sharing raw API keys. Generate a short-lived invite token and send it via any channel — iMessage, email, Telegram, Discord, whatever.

### CLI

```bash
# Create an invite
dugg invite-user "James"
# → prints a sendable message with a redemption link or CLI command

# Recipient redeems it
dugg redeem abc-def-1234
# → creates their account, prints their API key
```

### MCP tool

```
# Agent creates an invite
dugg_invite_user(name="James", expires_hours=72)
# → returns copyable invite text with the URL
```

### Browser flow (HTTP mode)

When the instance has an `endpoint_url` set, the invite text includes a link like:

```
https://kade.dugg.fyi/invite/abc-def-1234
```

The recipient clicks it and sees:
1. **Invite page** — who invited them, the instance topic, a name field, and a Join button
2. **Welcome page** — their API key (shown once), plus three paths forward:
   - **Agent** — set `X-Dugg-Key` and connect
   - **CLI** — `dugg welcome --key <key>`
   - **Browser** — bookmark their personal feed at `/feed/{key}`

### Token details

- Short slugs like `r5y6-9761-bm5h` — human-friendly, copy-paste safe
- Single-use — redeemed once, then done
- Expire after 72 hours by default (configurable via `--expires` or `expires_hours`)
- Not the API key — the key is only revealed on redemption
- Invite tree lineage is preserved: `created_by` maps to the inviter

## Browser feed

Every user gets a read-only feed at `/feed/{key}`:

```
https://kade.dugg.fyi/feed/dugg_8a3f...
```

- **HTML** — clean dark-themed page with titles, links, dates, and notes
- **Atom XML** — send `Accept: application/atom+xml` for RSS readers
- Shows the instance name and topic at the top
- No agent, no CLI, no setup — just a browser

This is the answer for non-technical users who just want to read what's being shared.

## Ban cascades

Ban a user and their invite tree gets pruned with depth-aware logic:

```
# Ban with cascade (default)
dugg_ban(collection_id="abc", user_id="spammer_id")

# Ban without cascade
dugg_ban(collection_id="abc", user_id="user_id", cascade=false)
```

**How cascading works:**
- **Depth 1** (directly invited by banned user): hard ban, no exceptions
- **Depth 2+** (further downstream): credit score decides
  - Credit score = submissions count + reactions received
  - If score >= threshold: survive, auto-promoted under the owner
  - If score < threshold: banned with the rest
- The system self-calibrates — early communities prune aggressively, mature communities with established contributors prune surgically

## Appeals

Banned users can appeal. The owner sees their contribution history and decides.

```
# Banned user submits appeal
dugg_appeal(collection_id="abc")

# Owner views pending appeals with credit scores
dugg_appeals(collection_id="abc")

# Owner approves — user is re-rooted under owner, active again
dugg_appeal_resolve(collection_id="abc", user_id="user_id", action="approve")
```

**Links are social capital.** The appeal shows submissions, reactions received, join date — a credit score for community participation. The owner's agent can auto-approve obvious good actors caught in branch prunes.

## Auto-routing

Agents use instance topic descriptors to auto-route published content — the user doesn't manually pick targets.

```
# Agent pulls the routing manifest
dugg_routing_manifest()
# Returns: [{name: "Food Dugg", topic: "food, restaurants..."}, {name: "AI Dugg", topic: "AI, agents..."}]

# Agent scores content against topics and calls dugg_publish with matching targets
# The decision layer lives in the agent, not in Dugg
```

**The user flow:**
1. `/dugg [link] [note]` — that's it
2. Agent enriches, auto-tags, scores against subscribed instance topics
3. Agent calls `dugg_publish` with the targets it picked
4. User verifies/overrides after the fact if needed

The extra step is verification, not decision. Don't slow the user down on provenance.

## Rate limiting

Tenure-based submission caps prevent fresh accounts from flooding a Dugg instance while letting established members post freely.

```
cap = initial + (days_as_member × growth)
```

| Setting | Default | Description |
|---------|---------|-------------|
| `rate_limit_initial` | 5 | Posts/day for a brand-new member |
| `rate_limit_growth` | 2 | Extra posts/day earned per day of membership |

**Example:** With defaults, a member who joined 10 days ago can post 25 times/day (5 + 10×2). A member who joined today can post 5 times.

```
# Owner configures rate limits
dugg_rate_limit(instance_id="abc123", initial=5, growth=2)

# Members check their status
dugg_rate_limit_status(collection_id="xyz789")
# Returns: 3/25 posts used today, member for 10 days
```

**Design:**
- Rate limits are per-instance, set by the owner
- Cap is enforced on `dugg_add` — the submission is rejected before enrichment runs
- The counter resets daily (UTC midnight)
- No instance configured = no rate limit (unlimited)
- Pairs with ban cascades — bot armies invited by a bad actor all start at the initial cap and can't firehose

## Publish sync daemon

When you call `dugg_publish`, the resource is flagged locally _and_ queued for delivery to remote Dugg instances with `endpoint_url` set.

**How it works:**

1. `dugg_publish(resource_id, targets)` → writes to `publish_targets` (local) + `publish_queue` (outbound)
2. Background sync loop picks up pending entries every 30 seconds
3. POSTs resource data to each remote instance's `/ingest` endpoint
4. Success → marked `delivered`. Failure → exponential backoff retry

**Retry schedule:** 30s → 2min → 8min → 32min → ~2h. After 5 failures, marked `failed`. Use `dugg_publish_retry` to reset all failed entries for another round.

```
# Set a remote endpoint on an instance
dugg_instance_update(instance_id="abc123", endpoint_url="https://remote.dugg.fyi")

# Check what's stuck
dugg_publish_status()
# Returns: pending: 2, delivering: 0, delivered: 47, failed: 1

# Retry failures
dugg_publish_retry()
# Returns: Reset 1 failed publish(es) back to pending
```

**Design:** The sync daemon runs as an asyncio background task alongside the MCP server. No separate process needed. If httpx isn't installed, deliveries are skipped with a warning — install it for remote sync.

## Event emission

Every significant action emits an event to the event log. Agents use this to stay informed without polling individual resources.

**Event types:**

| Event | Emitted when |
|-------|-------------|
| `resource_added` | A resource is added to any collection |
| `resource_published` | A resource is published to a target |
| `member_joined` | A member is invited to a collection |
| `member_banned` | A ban cascade is executed |
| `publish_delivered` | A publish is successfully delivered to a remote instance |
| `invite_created` | An invite token is generated |
| `invite_redeemed` | An invite token is redeemed by a new user |
| `reaction_added` | A user reacts to a resource (tap, star, thumbsup). Includes `resource_owner_id` for routing |

```
# Poll for recent events
dugg_events()
dugg_events(event_types=["resource_published"], since="2026-04-12T00:00:00Z")

# Catchup — see what's new since your last check
dugg_catchup()
dugg_catchup(limit=5, oldest_first=false)

# Mark everything as seen after reviewing
dugg_mark_seen()
dugg_mark_seen(seen_until="2026-04-12T20:00:00Z")  # or advance to a specific point
```

Events are scoped — you only see events for instances you're subscribed to and collections you're a member of. Catchup uses a per-user read cursor so agents don't need to track timestamps themselves.

## Webhooks

For agents that want push instead of poll. Subscribe a callback URL to an instance and receive POST requests when events happen.

```
# Subscribe to all events
dugg_webhook_subscribe(instance_id="abc123", callback_url="https://my-agent.com/hooks/dugg")

# Subscribe to specific events with HMAC signing
dugg_webhook_subscribe(
    instance_id="abc123",
    callback_url="https://my-agent.com/hooks/dugg",
    event_types=["resource_published", "member_joined"],
    secret="my-webhook-secret"
)

# List and manage
dugg_webhook_list()
dugg_webhook_delete(webhook_id="def456")
```

**Reliability:**
- Each delivery attempt has a 15-second timeout
- On success, failure counter resets to 0
- On failure, counter increments
- After 5 consecutive failures, webhook auto-pauses (status = `failed`)
- Re-subscribe to reactivate

**Signing:** If a `secret` is set, payloads are signed with HMAC-SHA256. The signature is sent in the `X-Dugg-Signature` header as `sha256=<hex>`.

## Remote ingest

The receiving side of publish sync. When a remote Dugg instance pushes content, `dugg_ingest` (or the `/ingest` HTTP endpoint) handles it.

```
dugg_ingest(
    url="https://example.com/cool-article",
    title="Cool Article",
    source_type="article",
    tags=["ai", "agents"],
    source_instance_id="remote123"
)
```

**Deduplication:** Same URL in the same collection = skip (returns `duplicate` status). Different collections = allowed (cross-pollination is intentional).

**Source tracking:** The originating instance ID is stored in `raw_metadata._source_instance` so you always know where content came from.

## Enrichment

When you add a URL, Dugg automatically:

- **YouTube**: Pulls title, description, thumbnail via oEmbed. Fetches full transcript via yt-dlp.
- **Articles**: Extracts Open Graph metadata (title, description, image).
- **Everything else**: Pulls whatever OG metadata is available.

Agents can also pre-process resources and pass in their own title, description, transcript, and tags — Dugg stores whatever it gets.

## Development

```bash
# Install with dev dependencies
uv sync --all-extras

# Run tests
uv run pytest

# Run a specific test
uv run pytest tests/test_db.py -v
```

## What's built

**Storage & retrieval** — Full-text search (FTS5), collections, tag-based share rules, resource relationship mapping

**Publishing** — Named publish targets, publish sync daemon with exponential backoff retry, remote ingest with URL deduplication

**Social layer** — Silent reactions with private aggregates, invite trees, ban cascades with depth-aware credit scoring, appeals

**Infrastructure** — Dual transport (stdio + HTTP/SSE), hosted instances with topic descriptors, agent auto-routing via routing manifest, tenure-based rate limiting

**Observability** — Event emission (8 event types), read cursors with catchup, webhook subscriptions with HMAC signing and auto-pause

**Onboarding** — Invite tokens with browser redemption, read-only browser feed (HTML + Atom), welcome orientation tool

## License

MIT
