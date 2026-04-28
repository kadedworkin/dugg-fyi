# Chrome Extension

"Dugg This" — save the current tab to your Dugg server in two clicks. Plus a few discovery features baked in.

## Install

The extension lives in the main Dugg repo at `chrome-extension/`. To install:

1. Open `chrome://extensions` in Chrome.
2. Toggle **Developer mode** on (top right).
3. Click **Load unpacked**.
4. Select the `chrome-extension/` folder.

The Web Store listing is not yet live. Loading unpacked is the supported install path.

## Configuration

After install, click the Dugg icon (top right) and hit **Options**. For each server you want to use, add:

- **Server URL** — `https://your-dugg-server.example.com` (no trailing slash; `http://localhost:8411` works for private Dugg)
- **API Key** — your `dugg_...` key for that server
- **Display name** — defaults to the server's self-reported name

You can configure multiple servers. The popup lets you pick the target each time, with the most-recent server as the default.

## What you get

### The popup

Click the Dugg icon while on any page. The popup shows:

- **Page title and URL** (auto-pulled from the current tab)
- **Note field** — type a why-this-matters comment
- **Server picker** — choose where to save (when you have multiple servers)
- **Tags field** — comma-separated tags
- **Save button** — fires the `/api/add` call

If you've already saved this URL, the popup tells you so and shows the existing entry instead of creating a duplicate.

### Badge counter

The toolbar icon shows a badge with your unread count for the active server. Click through to mark items read in the feed. The badge updates on a timer and on extension wake.

### Content banners

When you visit a page that's already in your Dugg, a small banner appears at the top of the page: "You dugg this on [date]" with the original note attached. No banner = you haven't saved this yet.

The banner is dismissible per-page and won't reappear on the same URL within a session.

### StumbleUpon discovery

In the popup, hit the **Surprise me** button to jump to a random resource from your feed. Repeated clicks cycle through different resources, weighted toward unread.

This is the discovery path: rediscover stuff you've already saved instead of always pulling new content.

### Page scraping

When you save a page, the extension grabs:

- The page title (from `<title>` or `og:title`)
- The selected text (if any) — defaulted into the note field
- The canonical URL (from `<link rel="canonical">` if present)
- Open Graph metadata (image, description) — sent along for the server to enrich further

If you want richer enrichment (transcripts, summaries), let your agent handle the resource after it's saved. The extension is fast/light by design.

## Permissions

The extension requests:

- `activeTab` — read the currently active tab when the popup opens
- `storage` — store your server list and API keys locally
- `scripting` — inject the content banner on pages already in Dugg
- `alarms` — periodic badge refresh
- `<all_urls>` host permission — to run the content banner check on every page

API keys are stored in Chrome's local extension storage. They're not synced to your Google account.

## Multi-server workflow

If you've configured a private Dugg + a shared server in the extension, the popup lets you pick the target. Common patterns:

- **Default to private.** Save to your private Dugg first; publish to shared servers later from the CLI / web / agent.
- **Default to shared.** If you primarily contribute to a single shared server, set that as default and skip the picker.

Server selection is sticky per-tab — pick once, subsequent saves on the same page default to the same target.

## Versioning

Current manifest version: `1.1.0`. The version is bumped on any user-visible change. Check the changelog in the main Dugg repo for what changed.

## Troubleshooting

- **Popup says "no servers configured"** — open Options and add at least one.
- **Save button does nothing** — open `chrome://extensions`, click the Dugg extension's "Service worker" link, look at the console for fetch errors. Most often it's a wrong API key or unreachable URL.
- **Banner doesn't appear on a known page** — the extension throttles checks. Refresh the page, or open the popup to force-check.
- **Already-saved popup is wrong** — your private Dugg might be unreachable. The extension falls back to "not saved" if it can't query.

## What's next

- [iOS App](IOS.md) — mobile-side capture
- [CLI](CLI.md) — terminal-side everything
- [Email forwarding](INTEGRATIONS.md) — save by forwarding email
