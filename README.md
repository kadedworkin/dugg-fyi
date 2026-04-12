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
- [ ] Resource relationship mapping (agent-built connections between resources)
- [ ] Event emission for real-time notifications to connected agents
- [ ] Django/AEV web UI wrapper

## License

MIT
