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
- **Share rules** — Tag-based filters that control what collaborators see (e.g., share AI stuff with Rocco, but not personal vlogs)
- **Publishing** — Flag resources for publishing to named targets (e.g., `public`, `aev-team`, `inner-circle`). One local source of truth, selective outward publishing
- **Silent reactions** — Subscribers can tap/star resources. Only the publisher sees aggregate counts — no public signal, no social pressure
- **Instances** — Hosted Dugg deployments with topics and access modes (public or invite-only)
- **Invite trees** — Member-invites-member with tracked lineage. Accountability flows through the chain
- **Ban cascades** — Ban a user and prune their invite tree. Depth 1 = hard prune, depth 2+ = credit score decides survival
- **Appeals** — Banned users appeal with their contribution history. Owner (or owner's agent) decides
- **Auto-routing** — Agents pull topic descriptors from subscribed instances and auto-route published content to matching targets
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
| `dugg_share` | Share a collection with another user, with optional tag filters. |
| `dugg_create_user` | Create a new user and get their API key. |

## Architecture

```
Your Agent (Claude, Miles, etc.)
    |
    | MCP protocol (stdio or HTTP)
    |
    v
+-------------------+
|   Dugg MCP Server  |
|                   |
|  - Tool handlers  |
|  - Auth (API key) |
|  - Enrichment     |
+-------------------+
    |
    v
+-------------------+
|  SQLite + FTS5    |
|                   |
|  - Resources      |
|  - Collections    |
|  - Tags           |
|  - Share rules    |
|  - Publish targets|
|  - Reactions      |
|  - Instances      |
|  - Invite trees   |
+-------------------+
```

**Zero LLM cost in the server.** All AI processing happens in the agent layer, using the user's own tokens. Dugg is just storage, indexing, and retrieval.

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

## Roadmap

- [ ] HTTP/SSE transport for remote/shared deployments
- [ ] YouTube history sync (passive intake)
- [ ] Browser extension for share-from-anywhere
- [ ] Vector embeddings for semantic search
- [x] Resource-level publish flags with named targets
- [x] Silent reactions (tap, star, thumbsup) with private aggregates
- [x] Hosted instances with topic descriptors and access modes
- [x] Invite trees with member-invites-member tracking
- [x] Ban cascades with depth-aware credit score pruning
- [x] Appeal system with contribution-based credit scores
- [x] Routing manifest for agent-driven auto-publishing
- [ ] Resource relationship mapping (agent-built connections between resources)
- [ ] Publish sync daemon (push flagged resources to remote Dugg instances)
- [ ] Event emission for real-time notifications to connected agents
- [ ] Django/AEV web UI wrapper

## License

MIT
