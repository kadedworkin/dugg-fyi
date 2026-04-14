# Agent Quickstart

Get Dugg running and connected to your AI agent in under 2 minutes.

## 1. Install

```bash
git clone https://github.com/kadedworkin/dugg-fyi.git
cd dugg-fyi && uv sync
```

## 2. Initialize

```bash
dugg init
```

This creates the SQLite database at `~/.dugg/dugg.db`.

## 3. Connect your agent

### Option A: Local stdio (simplest)

No user setup needed. In stdio mode, Dugg auto-creates a "Local User" with API key `dugg_local_default`. A default collection is also created on first use. You can skip `dugg add-user` entirely for local-only use.

**Claude Code / Claude Desktop** — add to `~/.claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "dugg": {
      "command": "uv",
      "args": ["--directory", "/path/to/dugg-fyi", "run", "dugg", "serve"],
      "env": {
        "DUGG_DB_PATH": "/path/to/.dugg/dugg.db"
      }
    }
  }
}
```

**OpenClaw** — add to your `openclaw.json` under `mcp.servers`:

```json
{
  "dugg": {
    "command": "uv",
    "args": ["--directory", "/path/to/dugg-fyi", "run", "dugg", "serve"],
    "env": {
      "DUGG_DB_PATH": "/path/to/.dugg/dugg.db"
    }
  }
}
```

> **Tip:** If `dugg` is on your PATH (e.g., via `uv tool install`), you can use `"command": "dugg"` directly instead of the `uv --directory` form.

### Option B: HTTP/SSE (remote agents, multi-user)

```bash
# Create a named user (save the API key!)
dugg add-user "YourName"

# Start the HTTP server
dugg serve --transport http --port 8411
```

Remote agents connect via SSE at `http://your-host:8411/sse` and authenticate with the `X-Dugg-Key` header.

### Option C: CLI wrapper (calling from outside the venv)

Use `dugg-cli-wrapper.sh` to call Dugg commands without activating the venv:

```bash
# From anywhere on the system
~/dugg-fyi/dugg-cli-wrapper.sh add-user "Kade"
~/dugg-fyi/dugg-cli-wrapper.sh serve --transport http
```

## 4. Verify it works

```bash
dugg doctor
```

This checks that:
- The database exists and schema is current
- At least one user exists
- FTS5 index is functional
- (HTTP mode) The server is reachable

If you don't have `dugg doctor` yet, do a manual smoke test:

```bash
# List users (should show at least one)
dugg list-users

# Start the server and try a search from your agent
dugg serve
# Then ask your agent: "Search Dugg for test"
```

## 5. First 3 things to try

Once connected, ask your agent to:

1. **Add a resource:**
   > "Dugg this: https://docs.mux.com/guides/play-your-videos — it's the Mux Player quickstart"

2. **Search for it:**
   > "Search Dugg for Mux Player"

3. **Create a collection and organize:**
   > "Create a Dugg collection called 'Video Infrastructure' and add that Mux resource to it"

That's it. Your agent handles enrichment (metadata, transcripts, tags) automatically. You share links, Dugg stores them, everyone queries.

---

## How local stdio auth works

When running in stdio mode (the default), Dugg doesn't require an API key. Instead, it silently creates a local user:

| Field | Value |
|-------|-------|
| User ID | `local` |
| Name | `Local User` |
| API Key | `dugg_local_default` |

This user is auto-created on first server start if no API key is provided. A "Default" collection is also auto-created on first use (e.g., when you add a resource without specifying a collection).

This means **a single `dugg init && dugg serve` is enough** for local agent use — no user management required.

For multi-user or remote deployments, create named users with `dugg add-user` and pass their API keys via the `X-Dugg-Key` header.

## Inviting others

Instead of sharing raw API keys, use invite tokens:

```bash
# Generate an invite for someone
dugg invite-user "James"
```

This prints a sendable message you can paste into any chat. If your instance has an `endpoint_url` set, the message includes a browser link:

```
https://your-server.dugg.fyi/invite/abc-def-1234
```

The recipient clicks the link, enters their name, and gets their API key. No CLI or agent needed on their end.

They can also redeem via CLI if they prefer:

```bash
dugg redeem abc-def-1234
```

### For non-technical users

Every user gets a bookmarkable feed at `/feed/{key}` — a browser-readable view of everything they have access to. No setup needed, just a URL.

## 6. Enable the `/dugg` slash command

The repo includes a ready-made `/dugg` command at `commands/dugg.md`. To enable it in your agent:

**Claude Code / Claude Desktop:**

```bash
# From the dugg-fyi directory
mkdir -p ~/.claude/commands
cp commands/dugg.md ~/.claude/commands/dugg.md
```

**Or have your agent do it:**

> "Copy the file `commands/dugg.md` from the dugg-fyi repo into `~/.claude/commands/dugg.md`"

Once installed, you can use:

- `/dugg` — show your latest resources
- `/dugg https://example.com this is why it matters` — add a URL with a note
- `/dugg search terms` — search your knowledge base

The command works with any MCP-compatible agent that supports slash commands. The file is self-contained — just drop it into your agent's command directory.
