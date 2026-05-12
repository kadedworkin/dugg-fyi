# Read State and Reactions

Two related-but-distinct primitives for marking resources you've engaged with. Read state is for triage. Reactions are for signal.

Both are per-user. Both are private to you (with one exception — see below).

## Read state

Read state records that you've seen a resource. It has three values:

- **Unread** — the default for newly-arrived resources
- **Read** — you marked it as such, or it auto-marks via reaction
- (no separate "archived" state — read items just stay read)

### Marking read / unread

Every surface exposes both a **Mark Read** and **Mark Unread** button. Across all surfaces, they're symmetric: anything you can mark read, you can mark unread.

Surface map:

| Surface         | Tool / button                                     |
|-----------------|---------------------------------------------------|
| MCP             | `dugg_mark_read`, `dugg_mark_unread`              |
| CLI             | `dugg read <id>`, `dugg unmark <id>`              |
| Web feed        | "Mark Read" / "Mark Unread" button on each card  |
| iOS             | Card row buttons, also synced from share extension|
| Chrome ext      | Mark via popup; badge reflects unread count       |
| Slack           | Reaction buttons (`✅ mark read`)                 |

State is recorded with a `source` field so you can later tell where the read came from: `web_button`, `mcp`, `cli`, `auto_react`, `ios`, etc.

### Auto-mark on reaction

When you react (star or thumbs up), Dugg auto-marks the resource as read with `source: "auto_react"`. Reacting implies engagement, which implies you've read it.

If you really want to react without marking read, the API allows it via explicit flag, but the default is auto-mark. This is the right default 95% of the time.

### Filter pills

The web feed's filter row gives you single-select filters:

- **Unread** — only items you haven't marked read
- **Read** — only items you've already read
- **Starred** — only items you've starred
- **Thumbs Up** — only items you've thumbs-upped
- **Noted by You** — only items where you've added a note

Filter state is persisted in the URL via `?filter=`, so a shared filtered URL survives reload, back-navigation, and share. The same `?filter=` query parameter is honored on the API endpoints (`/api/feed`, `/api/feed/urls`, `/api/search`), so iOS, MCP, and any other client get the same predicate.

## Reactions

Two reaction types:

- **Star (★)** — "this is good"
- **Thumbs up (👍)** — "this helped me"

The split is intentional: `star` is a quality signal ("worth your time"), `thumbsup` is a utility signal ("solved my problem"). Many publishers care about both for different reasons.

### Reacting

| Surface      | How                                                  |
|--------------|------------------------------------------------------|
| MCP          | `dugg_react(resource_id, type="star" \| "thumbsup")` |
| CLI          | `dugg react <id> [--type star\|thumbsup]`           |
| Web feed     | Star / Thumbs Up buttons on each card                |
| iOS          | Card buttons                                         |
| Slack        | Block Kit reaction buttons in notifications          |

To remove a reaction, react again with the same type — Dugg toggles. (CLI: `dugg react <id> --remove`.)

### Privacy model

- **Your reactions are private to you.** No public list of "who reacted with what."
- **Aggregate counts are visible to the original publisher.** Whoever submitted the resource sees how many stars and thumbs ups it got, total. No names attached.
- **You can also see your own reactions** on every surface. Filter the feed by `Starred` or `Thumbs Up` to find what you've engaged with.

This is a deliberate quality-feedback model with no leaderboard, no social pressure, and no race-to-react. React honestly.

## Notes on resources

A note is a short text comment attached to a resource. Two flavors:

- **Submitter note** — the original `resource.note` from whoever first added the resource. Travels through federation, shows up on every surface as the canonical "why this matters."
- **Cross-server / cross-user notes** — additional notes from other users, surfaced through the API as a `notes[]` array. Quarantined separately so a federated copy doesn't lose attribution.

To add or edit a note:

| Surface      | How                                       |
|--------------|-------------------------------------------|
| MCP          | `dugg_edit(resource_id, note="...")`      |
| CLI          | `dugg edit <id> --note "..."`             |
| Web feed     | Click the resource card → edit            |
| iOS          | Tap card → edit                           |

Notes are color-coded on the web feed by whether you authored them ("Noted by You" filter), which makes scanning your own annotations across a busy server fast.

## Cross-surface sync

Read state and reactions both sync to whichever server owns the resource:

- A reaction set on iOS shows up on the web feed
- A read mark on MCP advances the badge in Chrome
- A note edit on the CLI shows up next time you open the iOS app

Sync is server-authoritative — there's no conflict resolution per se, last write wins. State only diverges when a device is offline; reconnect resolves it.

## What's next

- [TOOLS](TOOLS.md) — `dugg_mark_read`, `dugg_react`, `dugg_edit`, full reference
- [CLI](CLI.md) — terminal commands
- [HTTP](HTTP.md) — REST endpoints for read/react/edit
