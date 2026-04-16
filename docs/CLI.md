# CLI

Dugg ships a full management CLI alongside the MCP server.

## Commands

| Command | Description |
|---------|-------------|
| `dugg init` | Initialize the database |
| `dugg serve` | Start the MCP server (stdio or HTTP mode) |
| `dugg add-user <name>` | Create a user and get an API key |
| `dugg login <key>` | Save your API key so you don't need `--key` on every command |
| `dugg add <url> [--note ...]` | Add a resource (URL auto-detected — `dugg https://...` works too) |
| `dugg feed` | Show recent resources with server health footer |
| `dugg search <query>` | Full-text search |
| `dugg paste <title> [--body ... \| --file ...] [--published-at ...]` | Add raw content (no URL) — emails, newsletter excerpts, notes |
| `dugg link <resource-id>` | Print the shareable `/r/{id}` viewer URL for a resource |
| `dugg status` | Dashboard: user, DB path, server, collections, resources, webhooks, health |
| `dugg health` | Ping the configured server and show status + timestamp |
| `dugg servers` | List this server, subscribed instances, and publish targets |
| `dugg remove <id-or-url>` | Delete a resource (submitter or owner) |
| `dugg edit <id-or-url> [--title ...] [--note ...]` | Edit a resource's title or note (submitter only) |
| `dugg email` | Show your Dugg email forwarding address |
| `dugg webhook add <url>` | Subscribe a webhook (Slack URLs auto-detected) |
| `dugg webhook list` | List active webhooks |
| `dugg webhook remove <id>` | Remove a webhook |
| `dugg webhook test` | Fire a test event to all webhooks |
| `dugg rotate-key [--key OLD] [--server URL]` | Issue a new API key, invalidating the current one (works against local DB or remote server) |
| `dugg set-config <key> <value>` | Set server config (e.g., `server_url`, `server_name`) |
| `dugg enable-shared-default <collection-id>` | Owner-only: turn a collection into the shared Default for all users on a hosted server |
| `dugg add-user <name> [--server URL]` | Create a user (local or remote) |
| `dugg invite-user <name>` | Generate an invite token with a redemption link |
| `dugg invites` | List invite tokens (pending, redeemed, expired) |
| `dugg redeem <token>` | Redeem an invite token (both keys auto-saved to `.dugg-env`) |
| `dugg admin` | Launch the terminal admin UI |

## Feed & search output

Both `dugg feed` and `dugg search` render resources in a three-line block:

```
  {title}
    {url}
    by {submitter} on {YYYY-MM-DD} (published {YYYY-MM-DD})
```

The `(published ...)` segment appears when the resource carries a publication date in `raw_metadata.published_at` (set automatically during URL enrichment, via the email worker's `Date` header, or explicitly with `dugg paste --published-at`). It's omitted when missing or equal to the added date.

## URL auto-routing

`dugg https://example.com --note "cool stuff"` automatically maps to `dugg add` — no subcommand needed.

## Admin TUI

Keyboard-driven terminal UI for server management:

```bash
dugg admin              # launch with local user
dugg admin --key <key>  # launch with specific API key
```

Keys: `[c]`ollections, `[m]`embers, `[a]`ppeals, `[s]` resources, `[b]`an, `[p]` ban+purge, `[x]` delete resource, `[u]`nban/approve, `[d]`eny, `[r]`efresh.

## `/dugg` slash command

A portable `/dugg` command ships at `commands/dugg.md` in the repo. Works with any MCP-compatible agent that supports slash commands.

**Install for Claude Code:**

```bash
mkdir -p ~/.claude/commands
cp commands/dugg.md ~/.claude/commands/dugg.md
```

**Usage:**
- `/dugg` — show your latest resources (feed)
- `/dugg https://example.com this is why it matters` — add a URL with a note
- `/dugg search terms` — search your knowledge base

See [SETUP.md](../SETUP.md) for detailed setup instructions across agent platforms.
