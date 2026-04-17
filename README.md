# Dugg

**The agentic web's shared knowledge base.** Your AI agent enriches. Dugg stores. Everyone queries.

Dugg is an MCP server that acts as a shared, searchable filing cabinet for links, articles, videos, and resources. Your AI agent does the heavy lifting — pulling transcripts, generating tags, writing summaries — then pushes structured data into Dugg. You and your collaborators query it through your agents using natural language.

**Zero LLM cost in the server.** All AI processing happens in the agent layer, using the user's own tokens. Dugg handles storage, indexing, and retrieval.

## Quick start

**Prerequisites:** Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/kadedworkin/dugg-fyi.git
cd dugg-fyi
uv sync

dugg init                # create the database
dugg add-user "Your Name" # get an API key
dugg login <your-key>    # save it
dugg serve               # start the MCP server (stdio)
```

## Connect your agent

**Claude Code / Claude Desktop / OpenClaw:**

```json
{
  "mcpServers": {
    "dugg": {
      "command": "uv",
      "args": ["--directory", "/path/to/dugg-fyi", "run", "dugg", "serve"]
    }
  }
}
```

For remote connections (HTTP/SSE), see [docs/HTTP.md](docs/HTTP.md).

## How it works

1. **You share a link** — via your agent, Chrome extension, email, Slack, or the CLI
2. **Your agent processes it** — pulls metadata, transcripts, generates tags
3. **Dugg stores it** — indexed for full-text search, organized into collections
4. **Anyone with access queries it** — "that video about webhook architectures" just works

## Architecture

```
┌─────────────────────────────────────────────┐
│  Agent Layer (yours — Claude, OpenClaw, etc) │
│  - Enrichment: metadata, transcripts, tags  │
│  - Judgment: auto-routing, ban appeals      │
│  - All LLM costs live here                  │
└──────────────────┬──────────────────────────┘
                   │ MCP (stdio or HTTP/SSE)
┌──────────────────▼──────────────────────────┐
│  Dugg MCP Server — 51 tools                 │
│  - Auth, rate limiting, event emission      │
│  - Publish sync daemon (federation)         │
│  - SQLite storage, FTS5 search              │
└─────────────────────────────────────────────┘
```

## Design principles

**Submit in ≤2 clicks, everywhere.** Every ingestion surface — share sheet, email, browser, CLI, Slack, extension, paste form — must let a user add a resource in two clicks or fewer from wherever they already are. If a new feature adds friction to the submit path, it's wrong. Adoption is bottlenecked by submit friction, not retrieval.

**Progressive disclosure, not progressive complexity.** Power features (collections, tags, routing, federation, admin controls) exist for users who want them, but are **never required** to contribute. The default pool + the Default collection + zero-arg submit is the sacred baseline. The user who throws content into the pool must never get a functionally worse experience than the user tagging and curating a storm.

**Zero LLM cost in the server.** All enrichment, summarization, and judgment runs in the agent layer on the user's tokens. Dugg stores, indexes, and retrieves — nothing more. This keeps the hosted server boring, predictable, and cheap.

**Trust networks over open firehoses.** Invite-only growth with tracked lineage. No content goes to strangers by default; federation is an opt-in relationship between trusted instances.

## Key concepts

- **Resources** — URLs with enriched metadata, transcripts, notes, and tags
- **Collections** — Access/publish boundaries (private, shared, or shared-default). *Not* topic buckets — topics are tags' job.
- **Instances** — Hosted Dugg deployments with topics. Invite-only, growing through trust networks
- **Publishing** — Push selected content to remote Dugg instances. Non-concentric circles
- **Silent reactions** — Subscribers react; only the publisher sees counts. No social pressure
- **Invite trees** — Member-invites-member with tracked lineage and accountability
- **Auto-routing** — Agents match content to instance topics and publish automatically

## Input surfaces

| Surface | Description |
|---------|-------------|
| **MCP tools** | 51 tools for agents — add, search, publish, moderate |
| **CLI** | `dugg add`, `dugg search`, `dugg feed`, `dugg export`, `dugg import`, `dugg admin` |
| **Chrome extension** | "Dugg This" — one-click from any browser tab |
| **Email forwarding** | Self-describing `{key}@{host}.dugg.fyi` addresses |
| **Slack** | `/dugg` slash command + webhook notifications |
| **Browser** | Read-only feed, paste form, admin panel |

## Documentation

| Doc | Covers |
|-----|--------|
| [docs/GETTING-STARTED.md](docs/GETTING-STARTED.md) | Local vs shared setup, the three paths, why two accounts |
| [docs/SCALING.md](docs/SCALING.md) | Contributor vs subscriber roles, large-audience deployment |
| [docs/TOOLS.md](docs/TOOLS.md) | Full MCP tool reference (51 tools) |
| [docs/CLI.md](docs/CLI.md) | CLI commands, admin TUI, `/dugg` slash command |
| [docs/HTTP.md](docs/HTTP.md) | HTTP/SSE server, endpoints, remote agent setup |
| [docs/GOVERNANCE.md](docs/GOVERNANCE.md) | Invite trees, bans, appeals, rate limits, pruning, succession |
| [docs/PUBLISHING.md](docs/PUBLISHING.md) | Federation, auto-routing, reactions, events, webhooks |
| [docs/INTEGRATIONS.md](docs/INTEGRATIONS.md) | Slack, email forwarding, Chrome extension, browser UI |
| [PARTNER_AGENT.md](PARTNER_AGENT.md) | Guide for agents connecting to someone else's instance |
| [SETUP.md](SETUP.md) | Detailed setup across agent platforms |

## Development

```bash
uv sync --all-extras
uv run pytest           # 208 tests
```

## License

MIT
