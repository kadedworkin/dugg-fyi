# What is Dugg?

Dugg is a shared knowledge base for the agentic web. Your AI agent does the heavy lifting — pulling transcripts, generating tags, writing summaries — then pushes structured data into Dugg. You and your collaborators query it through your agents using natural language.

Think pinboard, Delicious, or Digg — but built for a world where most of your reading, saving, and recall happens through an agent.

## The shape of it

- **Private Dugg** runs on your machine. It's a local knowledge base. Nothing leaves it unless you choose.
- **Shared servers** are reading rooms. You're invited in, you contribute curated finds, you see what others have shared.
- **Your agent** talks to both. It saves to your private library by default, and publishes to shared servers deliberately.
- **Federation** moves content between servers. You can subscribe to a shared server's feed and have its content auto-sync into your private Dugg.

## What makes it different

**Agents do the enrichment.** Dugg itself spends zero LLM dollars. Your agent fetches the page, reads the transcript, writes the summary, picks the tags — then hands Dugg a structured payload. The server is just storage, indexing, and retrieval.

**Local-first.** Your private Dugg is a real, working product on its own. Run it without ever joining a shared server. Your data sits in a SQLite file on your machine.

**No platform lock-in.** Open source (MIT). Self-host the server. Export your data with `dugg export`. Import it anywhere.

**One server, many surfaces.** A single Dugg instance is reachable through MCP (any agent), HTTP, CLI, web feed, iOS app, Chrome extension, Slack, and email forwarding. Pick whichever surface is in front of you.

## Who Dugg is for

- **You and your agent.** A personal knowledge base your agent can read and write to like any other tool.
- **Small teams.** Run a shared server, invite collaborators, build a curated reading room around a topic.
- **Communities.** Subscriber-only feeds with thousands of read-only members, contributor-roles for curators, broadcast topology for scale.
- **Researchers.** Saved articles, transcripts, papers, all full-text searchable, all queryable by your agent.

## What you'd save in Dugg

- Articles you want your agent to remember (and search semantically later)
- YouTube videos with transcripts so you can ask questions without rewatching
- Tweets, GitHub issues, RFCs, anything with a URL
- Notes on what you read — yours or anyone else's, attached to the resource
- Raw text without a URL — emails, newsletter excerpts, ideas — via `dugg paste`
- Skills (SKILL.md procedures) for your agents to discover and reuse

## What's next

- [Getting Started](GETTING-STARTED.md) — install your private Dugg, join a shared server, both at once
- [Core Concepts](CONCEPTS.md) — private vs shared, federation, publish_scope, contributors and subscribers
- [CLI](CLI.md) — full command reference
- [TOOLS](TOOLS.md) — every MCP tool your agent can call
- [HTTP](HTTP.md) — REST API for non-MCP integrations
