# Contributing

Thanks for considering it. Dugg is MIT-licensed open source. Contributions of any size are welcome — bug reports, fixes, features, docs.

## Repo layout

```
dugg-fyi/
├── src/dugg/         # Server, CLI, MCP tools (Python)
├── tests/            # pytest suite
├── chrome-extension/ # Manifest v3 Chrome extension
├── email-worker/     # Cloudflare Email Worker
├── agent/            # Reference agent helpers (Python)
├── docs/             # Public documentation (this folder)
├── scripts/          # One-off utilities (backfills, migrations)
├── README.md         # Top-level overview
├── SETUP.md          # Self-host setup
├── DEPLOY.md         # Server deployment
└── CHANGELOG.md      # Date-versioned releases
```

The iOS app lives in a separate repo: [github.com/kadedworkin/dugg-ios](https://github.com/kadedworkin/dugg-ios).

## Development setup

```bash
# Prerequisites: Python 3.11+, uv
git clone https://github.com/kadedworkin/dugg-fyi.git
cd dugg-fyi
uv sync

# Run the test suite
uv run pytest

# Run the server in dev mode
uv run dugg serve --http --port 8411
```

Most development happens against your private Dugg (`dugg init` to seed a local DB).

## Tests

The test suite covers the server, MCP tools, CLI, and federation logic. Pre-PR target: all tests pass.

```bash
uv run pytest                          # full suite
uv run pytest tests/test_publish.py    # one file
uv run pytest -k "test_invite"         # by name pattern
```

Add tests for any new public surface: a new tool, CLI command, HTTP endpoint, or behavior change. Tests should hit a real SQLite DB (Dugg's tests run against in-memory SQLite for speed; do not mock the DB).

## Code style

- Python, formatted with the project's pre-existing style (no enforced formatter beyond what's already in the codebase).
- Type hints on public APIs. Internal helpers: optional but encouraged.
- Docstrings on every MCP tool — they become part of the agent-facing tool description.
- Avoid premature abstraction. Three similar lines is better than a generalized helper that obscures intent.

## Adding a new MCP tool

1. Implement the function in `src/dugg/server.py` or a topical module.
2. Register it on the MCP server with `@mcp.tool()`.
3. Write a docstring — agents read it. Include input/output description, side effects, and any rate-limit behavior.
4. Add a CLI mirror if appropriate (parity is a feature; see the surface parity principle).
5. Add tests covering happy path, error path, and rate limit interaction.
6. Update `docs/TOOLS.md` and `docs/CLI.md`.

## Adding an HTTP endpoint

1. Implement in `src/dugg/server_http.py`.
2. Add request/response shape to `docs/HTTP.md`.
3. Add tests including auth (missing key, wrong key, banned user).

## Pull requests

1. Branch off `main`.
2. Keep PRs focused. One feature or one fix per PR.
3. Run `uv run pytest` before submitting.
4. Update CHANGELOG.md under a new heading if your change is user-visible. The release script tags `vYYYY.MM.DD` and bumps `.N` if multiple ships in a day.
5. If your change touches a public surface (CLI command, MCP tool, HTTP endpoint, behavior), make sure the relevant doc in `docs/` reflects it. The pre-merge docs gate (forthcoming) will flag missing doc updates automatically.

## Release flow

Date-versioned releases: `vYYYY.MM.DD` per shipped day, with `.N` suffix for same-day reships. The release process:

1. `git tag v2026.04.28` (or `.1`, `.2`, ...)
2. Push the tag.
3. GitHub Releases auto-attaches the CHANGELOG section.

## Surface parity principle

Consumer-facing features must reach parity across CLI, MCP, web, iOS, Chrome — at minimum the read/save/react/edit primitives. Server-management features (admin, blocklist, set-config) can stay CLI-only.

When you add a feature, ask: "Does this need to ship on every consumer surface?" If yes, list the surfaces in your PR description and ship them together (or open follow-up issues for gaps).

## Reporting bugs

GitHub Issues. Include:

- Dugg version (`dugg --version` or commit SHA)
- Surface (CLI / MCP / web / iOS / Chrome)
- Repro steps
- Expected vs. actual behavior
- Server logs if relevant (`dugg serve` stderr)

For security issues, please don't file a public issue. Email Kade directly.

## Questions

- Architecture / design — file a discussion or ping in the project's chat
- "How do I do X?" — check [TOOLS.md](TOOLS.md), [CLI.md](CLI.md), [HTTP.md](HTTP.md), [GLOSSARY.md](GLOSSARY.md), then ask
- Federation specifics — [PUBLISHING.md](PUBLISHING.md), [SCALING.md](SCALING.md), [RFC.md](RFC.md)

## License

MIT. Contributions are made under the same license.
