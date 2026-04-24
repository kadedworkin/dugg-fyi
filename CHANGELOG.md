# Changelog

Dugg follows date-versioned releases: `vYYYY.MM.DD` on each shipped day, with `.N` suffixes if a day ships multiple times. Each release ships from `main` with the commit tagged at release time.

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
