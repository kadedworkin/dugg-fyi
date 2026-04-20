# Changelog

Dugg follows date-versioned releases: `vYYYY.MM.DD` on each shipped day, with `.N` suffixes if a day ships multiple times. Each release ships from `main` with the commit tagged at release time.

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
