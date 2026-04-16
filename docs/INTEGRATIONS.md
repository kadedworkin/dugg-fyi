# Integrations

## Slack

### Incoming webhooks (notifications)

Wire up Slack incoming webhooks to get notified when new resources are added:

```bash
dugg webhook add https://hooks.slack.com/services/T.../B.../...
dugg webhook test
```

Dugg auto-detects Slack URLs and formats messages with rich blocks (title, URL, submitter, note, tags).

**New user on an existing server?** See [SLACK_NOTIFICATIONS.md](SLACK_NOTIFICATIONS.md) for a step-by-step walkthrough covering Slack app setup, webhook URL, and registration via agent / curl / CLI.

### Slash command (`/dugg` in Slack)

Set up a Slack app with a slash command pointing to your server:

1. Create a Slack app at api.slack.com
2. Add a slash command `/dugg` with Request URL: `https://your-server/slack/command`
3. (Optional) Set a signing secret: `dugg set-config slack_signing_secret <secret>`

**Behavior:**
- `/dugg` — shows last 5 resources (visible to channel)
- `/dugg https://...` — adds the URL (rest of text is the note)
- `/dugg search terms` — searches and shows results

## Email forwarding

Forward emails directly into Dugg using self-describing email addresses. No email stored on the server — no PII, no registration, no lookup tables.

### How it works

Each user gets an email address encoding their server and API key:

```
{api-key}@{server-hostname-with-double-dashes}.dugg.fyi
```

Dots in the hostname become `--` in the subdomain:

```
dugg_2c7e...@chino-bandido--kadedworkin--com.dugg.fyi
```

When an email arrives:
1. Cloudflare Email Worker receives it (wildcard MX on `*.dugg.fyi`)
2. Parses subdomain -> server hostname, local part -> API key
3. POSTs to `https://{hostname}/tools/dugg_paste` with the API key
4. Subject becomes the title, content is parsed and indexed
5. The email's `Date` header is normalized to ISO 8601 and passed as `published_at`, so the feed can show both "added on" and "published" (original send) dates

Fire-and-forget — no retries, no queues.

### Deploy the worker

The email worker lives in `email-worker/`:

```bash
cd email-worker
npm install
wrangler login
wrangler deploy
```

Then in Cloudflare dashboard: **Email Routing** -> enable for `dugg.fyi`, set catch-all -> route to `dugg-email-worker`.

### Use cases

- **Newsletter forwarding** — set your Dugg email as a subscription address
- **Email-to-Dugg** — forward interesting emails from any client
- **Automated ingestion** — any system that can send email can push content

## Chrome extension ("Dugg This")

One-click URL submission from the browser. Lives in `chrome-extension/`.

### Install

1. `chrome://extensions` -> enable Developer Mode -> Load Unpacked -> select `chrome-extension/`
2. Click the extension icon -> open Settings
3. Enter your Dugg server URL and API key
4. Click "Dugg it" on any page

Features:
- Grabs current tab URL and title
- Includes selected text as a note
- Success/error toast feedback
- Works in Chrome, Edge, Brave, Arc — any Chromium browser

## RSS subscriptions

Dugg ingests RSS/Atom feeds at two different tiers:

### Server-side (multi-user shared feeds)

When `dugg serve` runs in HTTP mode, a polling daemon wakes every minute and syncs every subscription whose interval has elapsed.

```bash
dugg rss subscribe https://daringfireball.net/feeds/main --interval 1h
dugg rss subscribe https://atp.fm/rss?token=PRIVATE_TOKEN --tag podcasts --collection Podcasts
```

New entries land as normal resources in the configured collection, tagged with the subscription's `tag_label` (default `rss`). `raw_metadata` carries `source=rss`, `rss_entry_id`, `source_label` (the feed title), and `published_at` from the feed entry. **ETag / Last-Modified** conditional GETs are sent on every poll so well-behaved feeds return `304 Not Modified` and cost nothing.

Agents can manage subscriptions via MCP: `dugg_rss_subscribe`, `dugg_rss_list`, `dugg_rss_remove`, `dugg_rss_poll`.

### Parameterized / authenticated feed URLs

Premium feeds (ATP.fm, Every.to, Stratechery, Substack subscriber-only) typically embed a per-user token in the feed URL. Dugg preserves the URL as-is when storing the resource. The query string is inspected for auth-ish parameters (`token`, `auth`, `apikey`, `subscriber`, `sig`, `session`, …) or long opaque signed values; when detected, `raw_metadata.is_private_link = true` is set so the UI can warn other viewers that the link may require their own subscription. Titles, descriptions, authors, and publication dates still land in the feed for everyone regardless.

### Agent-side (single-player watcher)

For users who want to ingest feeds without configuring them on the server — or to push a single feed into multiple Dugg instances with routing rules — the repo ships `agent/dugg_rss_agent.py`. Config lives in YAML at `~/.dugg/rss.yaml`:

```yaml
servers:
  - name: chino-bandido
    url: https://chino-bandido.kadedworkin.com
    api_key: dugg_xxx

default_target: { server: chino-bandido, collection: Default }

feeds:
  - url: https://daringfireball.net/feeds/main
    tag: daringfireball
    interval: 1h
    target: { server: chino-bandido, collection: Reading }

  - url: https://atp.fm/rss?token=PRIVATE_TOKEN
    tag: atp
    interval: 6h
```

Run `python agent/dugg_rss_agent.py --once` to poll every feed once, or `--watch` to stay resident and poll on each feed's own interval. State (seen entries, ETag, Last-Modified) persists to `~/.dugg/rss-state.json`.

See `agent/dugg_rss_example.yaml` for a commented starter template.

## Browser feed

Every user gets a read-only feed at `/feed/{key}`:

- **HTML** — clean dark-themed page with titles, links, dates, and notes
- **Atom XML** — send `Accept: application/atom+xml` for RSS readers
- No agent, no CLI, no setup — just a browser

## Browser admin panel

Server owners get an admin panel at `/admin/{api_key}`:

- View all collections, members, and resources
- Ban/unban users (owner only)
- Remove resources (owner or submitter)
- API key in the URL acts as authentication
