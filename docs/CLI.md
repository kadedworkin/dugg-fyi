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
| `dugg react <id-or-url> [--type tap\|star\|thumbsup]` | Silently react to a resource (default: tap) |
| `dugg email` | Show your Dugg email forwarding address |
| `dugg rss subscribe <url> [--collection ...] [--tag ...] [--interval 1h] [--now]` | Subscribe a collection to an RSS/Atom feed |
| `dugg rss list` | List your RSS subscriptions with state (active/paused) and last-poll timestamp |
| `dugg rss remove <sub-id>` | Unsubscribe (ingested resources are kept) |
| `dugg rss pause <sub-id>` / `rss resume <sub-id>` | Temporarily disable / re-enable polling |
| `dugg rss poll [<sub-id>]` | Force a poll right now (one subscription, or all active subscriptions) |
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
| `dugg export <file> [--collection ...] [--tag ...] [--since ...] [--pretty]` | Export resources to a portable `.dugg.json` file |
| `dugg import <file> [--collection ...] [--tag ...] [--on-conflict skip\|update] [--dry-run]` | Import resources from a `.dugg.json` file |

## Feed & search output

Both `dugg feed` and `dugg search` render resources in a three-line block:

```
  {title}
    {url}
    by {submitter} on {YYYY-MM-DD} (published {YYYY-MM-DD})
```

The `(published ...)` segment appears when the resource carries a publication date in `raw_metadata.published_at` (set automatically during URL enrichment, via the email worker's `Date` header, or explicitly with `dugg paste --published-at`). It's omitted when missing or equal to the added date.

## RSS subscriptions

Dugg can poll RSS/Atom feeds on a schedule and ingest new entries as resources. Subscriptions belong to a user + collection pair; the server-side polling daemon runs automatically in HTTP mode.

```bash
dugg rss subscribe https://daringfireball.net/feeds/main
dugg rss subscribe https://atp.fm/rss?token=PRIVATE_TOKEN --tag podcasts --interval 6h
dugg rss subscribe https://every.to/feed --collection Reading --now
dugg rss list
dugg rss poll <sub-id>
```

**Parameterized / authenticated feed URLs** are preserved as-is — subscription-gated feeds like ATP premium, Every.to, or Stratechery work. Private links are flagged in `raw_metadata.is_private_link` so other viewers know they may need their own subscription; titles, descriptions, and publication dates still land in everyone's feed.

**Intervals:** `1h` / `30m` / `6h` / `1d` or bare seconds (minimum 60s).

For a client-side / single-player watcher that pushes into one or more Dugg servers without touching the server's polling daemon, see `agent/dugg_rss_agent.py`.

## Export & import

Move resources between Dugg servers or create offline backups using the portable `.dugg.json` format.

```bash
# Export everything
dugg export backup.dugg.json --pretty

# Export one collection, filtered by tag
dugg export ai-agents.dugg.json --collection "AI Research" --tag agents

# Export resources added since a date
dugg export recent.dugg.json --since 2026-04-01

# Pipe between servers (stdout/stdin)
dugg export - --key $SOURCE_KEY | dugg import - --key $DEST_KEY

# Import into a specific collection
dugg import backup.dugg.json --collection "Imported"

# Preview what would be imported
dugg import backup.dugg.json --dry-run

# Import and update existing resources on URL collision
dugg import backup.dugg.json --on-conflict update

# Tag everything on import
dugg import backup.dugg.json --tag imported --tag archive
```

**Format:** `.dugg.json` files contain a `dugg_version`, `exported_at` timestamp, `source_server`, and a `resources` array. Each resource includes URL, title, description, source type, author, transcript, note, summary, thumbnail, raw metadata, and tags.

**What's excluded:** Sibling notes (quarantined enrichment), reactions, edges, user accounts, and collection structure. These are server-local state, not portable content.

**Collision handling:** On import, if a URL already exists in the target collection, `--on-conflict skip` (default) leaves the existing resource untouched. `--on-conflict update` merges metadata (incoming values overwrite) and unions tags.

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
