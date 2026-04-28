# iOS App

Native SwiftUI client for Dugg. Feed, search, and a Share Extension that drops anything you can hit "Share →" on into your active Dugg server in two taps.

## Status

Currently distributed via TestFlight or self-built from source. Not yet on the App Store. The iOS app repo is separate from the server repo: [github.com/kadedworkin/dugg-ios](https://github.com/kadedworkin/dugg-ios).

## Features (shipped)

- **Merged feed across servers.** Add multiple servers, see one combined "All" feed. Per-item origin badge tells you which server it came from.
- **Per-server color.** Each server gets a color you assign. Cards on the feed are tinted with that color, so at a glance you know whether you're looking at your private library, your team server, or a public feed.
- **Share Extension.** Hit Share → Dugg from Safari, Mail, anywhere. Pick the target server, add a note, save. Two taps end-to-end.
- **URL deduplication.** If you've already saved a URL to that server, the Share Extension shows you the existing entry instead of creating a duplicate.
- **Per-server error banners.** If one of your servers is unreachable, that server's section gets a banner — the rest of the feed keeps working.
- **Offline queue.** Save resources offline. They get queued locally and pushed when the device reconnects.
- **Read / Unread / Star / Thumbs up.** Cross-surface read state and reactions, fully synced to whichever server owns the resource.
- **Edit note, edit metadata.** Change a resource's title, note, tags, etc. from the app.
- **Delete.** Delete from the originating server; tombstones propagate to subscribers.

## Build from source

```bash
# Prerequisites
brew install xcodegen   # required — Xcode project is generated, not committed

# Clone and generate
git clone https://github.com/kadedworkin/dugg-ios.git
cd dugg-ios
xcodegen generate
open Dugg.xcodeproj
```

Build the `Dugg` target (main app) and `DuggShare` target (Share Extension) to your device or simulator.

**Why XcodeGen?** The project's `xcodeproj` is git-ignored; the source of truth is `project.yml`. Anyone touching project structure should edit `project.yml` and regenerate.

## Configuring servers

The app supports multiple Dugg servers simultaneously. Add each one in **Settings → Servers**:

1. **Server URL** — `https://your-dugg-server.example.com` (no trailing slash)
2. **API Key** — your `dugg_...` key for that server
3. **Display name** — what to call it in the UI (defaults to the server's self-reported name)
4. **Color** — pick a color for cards from this server

Your private Dugg can be added too if it's reachable on your local network (e.g., `http://192.168.1.50:8411`). Most users only add remote servers to the iOS app.

## ATS (Arbitrary Loads)

The iOS app's `Info.plist` enables `NSAllowsArbitraryLoads` so it can connect to self-hosted servers on plain HTTP and self-signed certs during local dev. For production deployments, use HTTPS with a real cert — the toggle is there for flexibility, not as a recommendation.

## Share Extension flow

The Share Extension uses `SLComposeServiceViewController`. When the user shares a URL:

1. The extension reads the shared URL and any selected text.
2. Pre-fills the title (from page metadata if available) and note field (from selected text).
3. Lets the user pick the target server from a dropdown (defaults to the most recently used).
4. Posts to the target server's `/api/add` endpoint via the stored API key.

**Bundle IDs:**

| Target      | Bundle ID             | Purpose                                  |
|-------------|-----------------------|------------------------------------------|
| `Dugg`      | `fyi.dugg.app`        | Main app (Feed, Search, Settings, etc.)  |
| `DuggShare` | `fyi.dugg.app.share`  | Share Extension                          |

Settings, server list, and API keys are shared between the main app and the extension via App Groups (`group.fyi.dugg.app`).

## What's not in iOS yet

The current build covers ~95% of the consumer feature path. Remaining gaps (as of late 2026-04):

- **Reactions-received** view (which of your shared resources got starred)
- **Related resources** (the knowledge graph / `dugg_related` endpoint)
- **Skills** (SKILL.md browsing and forking)
- **Invite redemption** in-app (currently requires browser or CLI redeem)

These will land in subsequent versions. For now, fall back to web for those features.

## Troubleshooting

- **Server unreachable** — banner appears on that server's section in the feed. Check the URL, make sure your API key is correct, and verify the server is reachable from your device's network.
- **Share Extension missing from share sheet** — long-press in the share sheet, hit "Edit Actions," enable Dugg.
- **Offline saves not syncing** — bring the app to the foreground when reconnected. Background sync is best-effort; foreground triggers an immediate flush of the queue.

## What's next

- [Chrome Extension](CHROME-EXTENSION.md) — desktop-side capture
- [CLI](CLI.md) — terminal-side everything
- [Read state and reactions](READ-STATE-AND-REACTIONS.md) — how cross-surface read state works
