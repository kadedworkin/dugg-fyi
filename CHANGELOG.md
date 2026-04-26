# Changelog

Dugg follows date-versioned releases: `vYYYY.MM.DD` on each shipped day, with `.N` suffixes if a day ships multiple times. Each release ships from `main` with the commit tagged at release time.

## v2026.04.25.2

Web feed gets first-class re-discovery primitives. Pairs Mark Read with the existing Mark Unread, replaces the inline "Unread only" checkbox with a dedicated filter pill row, and exposes the same filter logic on the API endpoints so iOS and other surfaces can adopt without duplicating predicate code.

### Added

- **Mark Read button** alongside the existing Mark Unread on every card. Both are now always visible (no hover-required), styled like the Star/Thumbs Up reaction buttons, with `is-active` reflecting the current read state. POST `/api/read/{id}` source = `web_button`.
- **Filter pill row** under the search bar: `Unread`, `Read`, `Starred`, `Thumbs Up`, `Noted by You`. Single-select, persisted via `?filter=` query param so reload, share, and back-navigation preserve state.
- **`?filter=` parameter on `/feed`, `/api/feed`, `/api/feed/urls`, `/api/search`.** Same predicate logic across HTML and API. iOS / MCP / future surfaces can adopt without re-implementing.
- **`web_button`** added to `READ_STATE_SOURCES`.

### Changed

- **Feed search is now form-submit + server-side FTS.** The previous live-as-you-type client-side fuzzy match is replaced with `/feed?q=...` that hits the same `d.search()` path used by `/api/search`, so relevance now matches what iOS and MCP see. Clear-search is a link to the same path without `?q=`.
- **Cookie-auth redirect on `/feed/{key}` preserves the query string** so a shared filtered URL survives the silent session migration.
- **Removed** the hover-only `.mark-unread-btn` opacity rules (`.card.is-read:hover ...`) — both read-state buttons are always visible now.

### Tests

407 passing (+4 this release): `web_button` source accepted on POST `/api/read`; `/api/feed` honors `?filter=` for all five values; `/api/search` applies `?filter=` on top of the query; new HTML markup renders correctly with server-side `?q=` and `?filter=` results.

## v2026.04.25.1

Web Star/Thumbs Up could ADD a reaction but couldn't toggle it OFF. This ships the symmetric remove path across server, MCP, CLI, and the inline web feed JS.

### Added

- **`db.unreact(user_id, resource_id, reaction_type)`** — idempotent DELETE that returns `bool`, emits `reaction_removed` event with resource owner payload (mirrors `react_to_resource`).
- **`reaction_removed` event type** in `EVENT_TYPES`.
- **`DELETE /api/react/{resource_id}?type=star|thumbsup`** — same auth + collection-access checks as `POST /api/react`, returns `{"removed": bool}`.
- **MCP tool `dugg_unreact`** + **CLI command `dugg unreact <target> --type star|thumbsup`**.
- **Web feed/viewer toggle-off** — clicking an active reaction button now fires DELETE with optimistic UI decrement and rollback on failure.

### Changed

- **Webhook dispatch** — `reaction_removed` follows the same author-only filter as `reaction_added` (so removals don't leak to all collection subscribers) and renders a distinct Slack line ("X removed from your *resource*"). Slack aggregate-line gate flipped from `total > 1` to `total > 0` so users see the count after a removal.

### Tests

403 passing (+7 this release): DB removal/no-op/invalid-type/event emission; HTTP removal/no-op/invalid-type/auth.

## v2026.04.25

- Removed `tap` reaction; replaced with first-class Read primitive (`read_states` table, `/api/read` endpoints, surface-tagged source enum, MCP/CLI/Slack parity).

## v2026.04.24.5

Stable cross-server note editing needs a server-scoped remote identity, not name matching. The v2026.04.24.4 boot-time name-uniqueness backfill tried to force that invariant through local usernames and broke legitimate duplicates like John/Sally, so this release walks that back and replaces it with attested `(source_server, remote_user_id)` links.

### Changed

- **Stable federated identity schema.** `resource_notes` adds `submitter_remote_id` (the origin server's `users.id` UUID, never the API key) and the new `user_remote_identities` table records `(local_user_id, source_server, remote_user_id, source, created_at)` with a UNIQUE constraint on `(source_server, remote_user_id)` so the first attested link wins.
- **Walked back the v2026.04.24.4 boot-time backfill.** Startup no longer rewrites local users into fake uniqueness just to make federated note ownership work; duplicate local display names like John/Sally stay valid.
- **Attested link paths only.** Invite redemption (`POST /invite/{token}/redeem`) now accepts optional `home_server` + `home_user_id` and auto-links on redeem; local admins can also create links with `dugg admin link`. There is no public claim endpoint.
- **Ownership gate widened by explicit link map.** `viewer_owns_note` now treats a note as editable when `submitter_user_id` matches directly or when `user_remote_identities` maps the viewer to `(source_server, submitter_remote_id)`. That gate now drives `/api/note/edit`, `/api/note/delete`, per-note `can_edit` on `/api/resource/{id}`, and the browser feed renderer.
- **Federation now carries the remote identity through the wire.** Outbound publish includes `submitter_remote_id = resource.submitted_by`; `/ingest` threads it through on arrival and fills `submitter_user_id` from the link table when a local attested mapping exists.
- **Admin orphan-claim tooling.** `dugg admin claim-orphans --user U [--dry-run]` claims legacy unattributed notes for a linked user, so existing private/chino-bandido notes can pick up editability without a data rewrite.

### Tests

377 passing (+5 this release): John/Sally duplicate-name collision stays valid; remote identity link remains first-write-wins; invite redeem auto-links; `dugg admin link` works; `dugg admin claim-orphans` dry-run and apply paths both behave.

## v2026.04.24.3

Browser feed catches up to iOS for per-note editing. Previously the card-level "edit" button rewrote the resource's primary note regardless of who clicked it, which is why Kade's "secondary browser note" landed unattributed and uneditable — he was actually replacing the primary note (attributed to the resource submitter), not creating his own.

### Changed

- **Per-note edit/delete buttons in `/feed`.** Each rendered note (primary + every sibling) now carries `data-note-id` / `data-note-kind` / `data-resource-id` and renders inline `edit` / `delete` micro-buttons. Buttons are *only* rendered server-side for the note's actual author, so non-owners never see them and the markup itself is the gate.
- **Routing per kind.** Primary notes (kind=`primary`, empty note-id) route through `/api/edit` with `{resource_id, note}`; sibling notes route through `/api/note/edit` and `/api/note/delete` with the sibling's id. Both surfaces share the same audit-trail and soft-delete plumbing as iOS.
- **Add-note button on every card.** `add note` opens an inline composer that POSTs `/api/note` to attach a sibling — the only way a non-submitter can contribute. The card-level `delete` button (formerly "delete") is now `delete item` so the destructive scope is clear vs. per-note delete.

### Removed

- The single card-level `edit` button that called `/tools/dugg_edit` and replaced the primary note. Its job is now split across per-note edit (correctly attributed) and the add-note composer (creates a sibling).
- The publish-on-edit shortcut (`publishNote`) — it depended on the old card-level edit form. Federation publishing has its own paths and didn't need to be wedged into the inline edit flow.

### Tests

372 passing (+2 this release): GET `/feed` HTML renders edit/delete buttons only on the viewer's own notes; every card carries the add-note affordance.

## v2026.04.24.2

Three fixes Kade surfaced while stress-testing per-note edit across home + federated servers:

### Fixed

- **Federated notes were unattributed on the destination, so the remote author couldn't edit their own words.** `ingest_remote_publish` only threaded the resolved submitter id onto the resource row, not the sibling note, so on the destination server every federated note arrived with `submitter_user_id=""` and `can_edit=false`. Both paths now carry a stricter `note_submitter_id`: if the incoming `submitter_name` *confirms* a local user (or the authed caller claims to be the author by name match), the sibling note is attributed to that user. If no local user exists with that name, the note stays unattributed rather than defaulting to the delivery agent — which would have wrongly exposed Edit/Delete to anyone relaying notes on behalf of others.

- **Delete was a hard delete; no moderator recourse.** `delete_resource_note` now stamps a `deleted_at` tombstone instead of `DELETE FROM resource_notes`. The row stays in the DB, hidden from `list_resource_notes` / `batch_resource_notes` / `/api/resource/{id}` / FTS search by default; owner/admin surfaces can opt in via `include_deleted=True`. The audit trail entry (`field='note'`, `new_value=''`) still lands alongside the tombstone so two independent moderation signals exist.

- **Re-adding the same note text after delete silently no-op'd** because `resource_notes` has a UNIQUE constraint on `(resource_id, source_server, submitter_user_id, note)` and `INSERT OR IGNORE` would swallow the second write. `add_resource_note` now detects a matching tombstoned row and resurrects it (clears `deleted_at`, refreshes `added_at`) rather than letting the constraint eat the insert.

### Known gaps (not in this release)

- **Browser feed's "edit note" button edits the primary `resource.note` (attributed to the resource submitter), not the caller's sibling note.** If you're not the submitter and click "edit," you end up writing the primary note with the submitter's attribution — which is why Kade saw "secondary browser note" appear unattributed. Fix requires rendering sibling notes inline in `/feed` with per-note edit buttons and routing them through `/api/note/edit`. Deferred until we pick up the browser surface again.
- **Soft-delete for resources / skills / videos / articles.** Only notes are soft-deleted in this release. Resource-level soft-delete has a wider blast radius (47+ SELECT sites) and I didn't want to destabilize federation/Atom in the same drop. Tracked for a follow-up.

### Tests

370 passing (+4 this release): federated attribution when `submitter_name` matches a local user; federated stays unattributed when it doesn't; soft-delete tombstones the row and preserves it for admin view; resurrection on identical re-add.

## v2026.04.24.1

Per-note edit and delete. The .0 release let users edit the resource row but not individual notes attached to it — if a second user dropped a sibling note on someone else's entry, there was no way to revise or remove it. Long-press on a note now surfaces Edit/Delete for notes the viewer authored.

### Added

- **`POST /api/note/edit`.** Author-gated text edit on a sibling note. Logs `resource_edits` with `field='note'` so note-swap shows up in the audit trail alongside URL-swap.
- **`POST /api/note/delete`.** Author-gated hard-delete. Records the deletion in the audit trail (old=text, new="") so moderators can still see what was removed.
- **Per-note `id` / `can_edit` / `can_delete` on `/api/*` payloads.** Primary note has `id=""` and routes through `/api/edit`; sibling notes have real ids and route through the new endpoints. iOS uses the flags to gate the long-press context menu.

### iOS

Matching ship in `dugg-ios`:
- `NoteBlock` gets a `.contextMenu` with Edit / Delete, shown only for notes the viewer authored.
- `EditNoteSheet` for inline note editing.
- **Bug fix (merged feed):** `EditResourceSheet` and `EditNoteSheet` previously computed their target server as `activeServer ?? firstServer`, ignoring the origin-server pin from the merged "All" feed. Tapping a chino-bandido item while the active server was private routed the edit POST to private Dugg with a chino-bandido resource id — 403/404 from iOS's perspective, and editing appeared to work only against the active server. Both sheets now take `targetServerID` from the detail view and resolve the viewer's key on the right server.

### Tests

366 passing (+4 this release): sibling-note edit by author round-trips and audits; non-author edit is 403; sibling-note delete by author removes the row and records the deletion; per-note can_edit/can_delete shape (own primary true, other's sibling false, own sibling true).

## v2026.04.24

Edit, delete, and audit trail for resources. iOS clients (and any API caller) can now add sibling notes, edit URL / title / description / note, delete submitter-or-owner entries, and view a full per-field history of mutations — with a strict separation between human-driven edits (audited) and machine enrichment (not).

### Added

- **`POST /api/note`.** Adds a sibling note to an accessible resource. Re-reads federate back via the existing `notes[]` pathway.
- **`POST /api/edit`.** Submitter-or-owner gated. Accepts `url` / `title` / `description` / `note` / `source_type` / `author` / `tags`. Threads `actor_id` through `update_resource` so every mutation lands in `resource_edits`.
- **`GET /api/resource/{id}/edits`.** Full audit trail, visible to any collection member — transparency default: if you can see the resource you can see whether it's been rewritten. Newest-first, includes `actor`, `actor_id`, `field`, `old_value`, `new_value`, `edited_at`.
- **`resource_edits` table.** One row per field change, cascade-deletes with the parent resource. Backs the audit endpoint and the iOS history sheet.
- **`can_edit` / `can_delete` / `edit_count` on `/api/*` payloads.** Server computes ownership once so clients don't re-derive it. iOS renders edit/delete buttons and the history chip off these flags.
- **`agent_enriched: bool` on `dugg_edit` MCP tool.** Machine-driven enrichment (summary, tag backfill, metadata fill-in after `dugg_add`) passes this flag so the write skips the audit log — otherwise the watchdog pushing LLM-generated descriptions would flood the user-facing "Edited N times" counter.

### Fixed

- **Submitter delete returned 500.** `/delete` HTTP authorized submitters but the DB layer required owner, so submitter deletes round-tripped as errors. DB is now submitter-or-owner to match the HTTP contract.
- **Watchdog enrichment polluted audit trail.** The dugg-agent daemon calls `dugg_edit` post-add to push `llm_enrich` results back. `_handle_edit` hard-coded `actor_id=user_id`, so every machine enrichment showed up as a user edit and a fresh `dugg_add` produced `edit_count=1` with a ghost "description" entry. Agent now opts out via `agent_enriched=True`; `_handle_edit` passes empty `actor_id` when the flag is set.
- **CLI `dugg edit` bypassed audit.** `cmd_edit` called `db.update_resource` without `actor_id`, so human edits through the CLI silently escaped `resource_edits`. Now threads `actor_id=user["id"]`.

### Audit contract (now symmetric)

- **Logged:** `/api/edit` (iOS), `dugg edit` (CLI), `/tools/dugg_edit` without `agent_enriched`.
- **Not logged:** `dugg_add` enrichment bumps, `apply_index_policy` eviction writes, `/tools/dugg_enrich`, `/tools/dugg_edit` with `agent_enriched=true`, the dugg-agent watchdog.

### iOS

`dugg-ios` ships the matching UI on the same date:

- `ResourceDetailView` now shows an "Edited N times" chip when `edit_count > 0`, tapping it opens `EditHistorySheet` listing every change with strikethrough old values, new values, actor, and timestamp.
- `EditResourceSheet` surfaces the user-visible fields with offline-queue fallback through `MutationQueue`.
- `APIClient` gains `editResource`, `deleteResource`, `resourceEdits`, `ResourceEdit` model.

### Tests

362 passing on the server (+3 this release): `agent_enriched=true` suppresses audit; `agent_enriched=false` still audits; CLI `cmd_edit` produces audit rows. Earlier +16 covered the note endpoint, edit auth gates, URL-swap history, visibility boundaries, submitter delete, and `can_edit`/`can_delete`/`edit_count` shape.

### Why it matters

Without an audit trail, link-swap and note-rewrite attacks are invisible: a trusted submitter adds a clean URL, earns reactions, then flips it to malware 24 hours later with no way for anyone to tell the old link from the new. The `resource_edits` table is the moderation primitive that makes collaborative knowledge bases defensible — and the machine/human split keeps it signal-dense so reviewers aren't scrolling past hundreds of agent-generated summary writes to find the one suspicious URL change.

## v2026.04.20

Skills feature — SKILL.md as a first-class federated Dugg resource type. All six sprint phases merged, with post-merge auth hardening and migration fixes.

### Added

- **Skills resource type.** SKILL.md files flow through the existing polymorphic `resources` table with `source_type = 'skill'` and a new `skills` child table carrying `supersedes_id` for linear append-only versioning.
- **CLI — `dugg skill`.** `add`, `list`, `get`, `install`, `fork`, `edit`, `history`. `edit` opens `$EDITOR` (fallback `vi`) pre-loaded with the current SKILL.md and re-parses frontmatter on save. `fork` clones into the caller's default collection (or `--collection`) and is idempotent on re-run. `history` prints the supersedes chain.
- **MCP tools.** `dugg_skill_list`, `dugg_skill_get`, `dugg_skill_search`, `dugg_skill_install`, `dugg_skill_add`, `dugg_skill_fork`, `dugg_skill_edit` — full parity with CLI for agent authoring.
- **Slack surface.** `/dugg skill list|get|search|add` inline with Block Kit View / Install / Fork buttons returning ephemeral SKILL.md code blocks and CLI/MCP handoff instructions.
- **Web viewer.** `/skills` card feed, `/s/{id}` single-skill page with rendered SKILL.md body, `/s/{id}.md` raw download gated on `is_exportable`, `/s/{id}/unlock` form-based key submission mirroring the existing `/r/{id}/unlock` flow. Each skill page renders a "Version history" section linking every prior version via the supersedes chain.
- **Atom federation.** Skills publish through the existing feed pipeline as `<content type="text/markdown">` entries. Deletion propagates via RFC 6721 tombstones. Subscriber-side RSS ingestion branches on `source_type` to write into the `skills` table.
- **Event vocabulary.** New `skill_added`, `skill_forked`, `skill_superseded`, `skill_deleted` event types on the `event_log` CHECK constraint, emitted throughout the lifecycle.
- **`docs/RFC.md`.** Compliance catalog for the 12 IETF RFCs Dugg implements: Atom 4287 / 6721 / 8288, HTTP 7231 / 7232 / 7807 / 6585, HMAC 2104, cookies 6265, timestamps 3339, URIs 3986, email 5322. Each entry names the source file and describes how the standard is applied.

### Security

- **Cross-collection fork.** CLI `dugg skill fork --collection <uuid>` previously resolved the target without a membership check, letting any user plant a superseding skill in a collection they learned the UUID of. CLI now calls `_member_can_author` on the target and returns "Collection not found" to non-members so existence doesn't leak.
- **Demoted-submitter edit.** `_can_version_skill` previously allowed the original submitter to version a skill forever, including after demotion to `member_type='subscriber'`. Both CLI and MCP edit paths now require an active non-subscriber membership as a precondition.

### Fixed

- **Migration FK failure.** Phase 3a's `event_log` table rebuild ran its INSERT-from-SELECT under `PRAGMA foreign_keys=ON`, failing on production DBs with orphaned event_log rows (instance/collection/actor IDs that had been deleted over time). The original table never enforced those FKs at insert time, so preserving the rows is correct. Rebuild now disables FK enforcement across the ALTER-via-rebuild.
- **Migration retry safety.** `executescript()` is not atomic — a failed first attempt left `event_log_new` dangling and the retry hit "table event_log_new already exists". Defensive `DROP TABLE IF EXISTS` before the rebuild's CREATE.

### Tests

299 passing. 8 new test files: `test_skills.py`, `test_skills_cli.py`, `test_skills_web.py`, `test_skills_atom.py`, `test_mcp_skills.py`, `test_slack_skills.py`, plus negative-path tests for cross-collection fork and demoted-submitter edit.

### Strategic context

Anthropic is standardizing skills-over-MCP in the spec (expected ~June 2026). Shipping a working federated skill network now positions Dugg as the reference implementation when the spec lands — a concrete artifact for the AI-VC sponsorship conversation.
