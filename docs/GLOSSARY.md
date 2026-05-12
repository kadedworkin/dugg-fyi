# Glossary

Every Dugg-specific term in one place. Skim before reading other docs if any of these feel ambiguous.

**Agent key** — The API key issued to your AI agent for MCP access on a shared server. Linked to the user key — if the user is banned, the agent key is revoked too.

**Atom feed** — The public RSS-compatible feed every Dugg server exposes at `/feed/{user_key}`. Used for federation pull-sync and for browser-readable feeds.

**Backfill** — When you subscribe to an Atom feed, the first poll pulls all existing entries into your private Dugg (preserving server-side dates), not just new ones.

**Broadcast** — A scaling pattern where a small contributor team publishes to a server with thousands of subscriber-only members. See [SCALING.md](SCALING.md).

**Cap** — Your daily submission limit on a server. Calculated as `initial + (days_as_member × growth)`. Resets at UTC midnight.

**Cascade ban** — A ban applied to a user that also bans every user they invited (recursively down the invite tree). Opt-in per ban via `cascade=true`.

**Catchup** — `dugg_catchup` returns events you haven't seen yet, oldest-first, since your cursor. Used for digest-style notification.

**Collection** — A topical bucket inside an instance. Resources live in collections.

**Contributor** — Invite role with full read + write. Adds resources, reacts, comments, publishes.

**Credit score** — A user's submission + reaction count, used by owners reviewing appeals after a ban.

**Default collection** — The auto-created collection that resources go to when no other collection is specified. Every instance has one.

**Dugg This** — The name of the Chrome extension's primary action.

**Duggley** — The mascot. The mole on the landing page.

**Email worker** — Cloudflare Email Worker that catches forwarded mail at `{host}+{key}@dugg.fyi` and POSTs it to the corresponding Dugg server's `/api/paste` endpoint.

**Enrichment** — The process of pulling metadata, transcripts, descriptions, and tags for a URL. Done by the agent (not the server) before submission.

**Federation** — Content moving between Dugg servers via Atom feeds (pull) and `dugg_publish` (push).

**Feed key** — The user-key-prefixed URL component used for personalized Atom feed access (`/feed/{user_api_key}`).

**Filter pill** — A single-select chip on the web feed (Unread, Read, Starred, etc.) controlling which resources are shown.

**Home server** — A server where a user is marked `home: true`, treating their locally-enriched content as canonical for federation.

**Hosted fork** — Forthcoming Cloud tier where a user runs a hosted Dugg with the same code as the OSS but managed infra. Free tier exists.

**Instance** — Top-level container on a Dugg server. A server can host one or more instances.

**Invite redemption** — The act of using an invite token to create a user account on a shared server. Done via web (`/invite/{token}`) or CLI (`dugg redeem`).

**Invite token** — A one-time, time-limited token that lets a recipient create an account on a server without an admin manually creating it.

**Invite tree** — The directed graph of who-invited-whom, used for cascade bans.

**Local Dugg** — Same as private Dugg. The terminology is mostly "private" in user-facing docs, but "local" is preserved in technical contexts (e.g., `dugg_local_default`).

**MCP** — Model Context Protocol. The agent-tool protocol Dugg implements. Both stdio and HTTP/SSE transports are supported.

**Note** — Text annotation on a resource. The submitter's note is canonical and travels through federation; cross-user notes are quarantined separately.

**Origin** — In the iOS feed, the badge on each card showing which server the resource came from (when viewing the merged "All" feed).

**Paste** — Raw content ingested without a URL. Created by `dugg paste`, the web paste form, or email forwarding.

**Private Dugg** — A Dugg instance running on your local machine. The user-facing term for what code calls "local."

**publish_scope** — A collection setting: `auto` (resources auto-publish to configured targets) or `none` (publish requires explicit call).

**Pull sync** — Automatic content flow from a shared server into your private Dugg via Atom feed subscription.

**Push sync** — Explicit content flow from your private Dugg to a shared server via `dugg_publish`. Always deliberate.

**Resource** — A saved item in Dugg. Has a URL (or pasted body), title, description, note, tags, optional transcript, etc.

**Routing manifest** — `dugg_routing_manifest()` — server's declared topic + scope, used by agents to score whether a resource fits this server before publishing.

**Share extension** — iOS extension target (`DuggShare`) that hooks into the system share sheet.

**Skill** — A SKILL.md procedure stored in Dugg. Versioned (forks supersede ancestors). Discoverable by agents.

**StumbleUpon** — Discovery feature in the Chrome extension popup ("Surprise me" button) that surfaces a random unread resource.

**Subscriber** — Invite role with read-only access. Sees content, reacts, but can't add new resources. Used for large-audience broadcast servers.

**Tombstone** — A "this resource was deleted" marker in an Atom feed (RFC 6721) that propagates server deletion to RSS subscribers.

**Two-click submit** — Design principle: adding content must be ≤2 clicks on any surface. Power features are progressively disclosed.

**Unread** — Default state for newly-arrived resources. See [READ-STATE-AND-REACTIONS.md](READ-STATE-AND-REACTIONS.md).

**User key** — The API key issued to the human user on a shared server. Distinct from the agent key. Used for browser, CLI, paste form.

**Webhook** — HTTP callback URL Dugg POSTs events to (`resource_added`, `reaction_added`, etc.). HMAC-signed if a secret is configured.
