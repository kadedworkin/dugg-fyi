# Integrations

## Slack

### Incoming webhooks (notifications)

Wire up Slack incoming webhooks to get notified when new resources are added:

```bash
dugg webhook add https://hooks.slack.com/services/T.../B.../...
dugg webhook test
```

Dugg auto-detects Slack URLs and formats messages with rich blocks (title, URL, submitter, note, tags).

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
