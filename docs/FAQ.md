# Frequently Asked Questions

## What is Dugg, in one sentence?

A pinboard / Delicious / Digg for the agentic web — your AI agent enriches, Dugg stores, everyone (you + your collaborators) queries.

## How is this different from a bookmark manager?

A bookmark manager stores URLs. Dugg stores **structured, enriched, searchable** content: titles, descriptions, transcripts (for video), tags, your notes, federation metadata, reactions. And it's designed for agents to read and write to programmatically — your agent treats Dugg like any other tool.

A bookmark manager is for you. Dugg is for you + your agent + your collaborators.

## How is this different from Pinboard or Pocket?

Three differences:

1. **Agent-first.** Pinboard and Pocket assume a human is doing the saving and reading. Dugg assumes your agent is doing both, and the human reviews summaries.
2. **Federated.** You can run your own server and connect it to others. Content flows between servers via Atom feeds and explicit publishing.
3. **Local-first.** Your private Dugg is a SQLite file on your machine. No cloud account required, no third-party storage, no platform lock-in.

## Why local-first?

Two reasons:

1. **Trust.** Your reading and notes are personal. Defaulting to a hosted service means defaulting to leaking that data. Defaulting to local means you opt in to share.
2. **Enrichment quality.** YouTube, Twitter, Substack, and many others block requests from cloud IPs. A laptop's residential IP can pull transcripts and metadata that a VPS can't. Local enrichment, federated distribution.

## Do I need to host my own server?

No. The minimum viable setup is private Dugg only — install, run, your agent connects, done. No accounts, no servers, no networking.

If you want shared knowledge with collaborators, you join an existing shared server (someone invites you) or run your own.

## How do I get an invite to a shared server?

From whoever runs that server. Invites are deliberate — there's no public signup. The server owner generates an invite link, sends it to you, you redeem it.

## What are the two API keys for?

When you join a shared server, you get a **user key** (yours, for browser/CLI) and an **agent key** (for your AI agent's MCP connection). They're linked — one ban revokes both. Your private Dugg has just one key (`dugg_local_default`) since there's no human/agent trust split there.

See [GETTING-STARTED.md](GETTING-STARTED.md) for details.

## How does federation work?

Two paths:

- **Pull** — subscribe your private Dugg to a shared server's Atom feed (`dugg rss subscribe`). Content flows in automatically.
- **Push** — call `dugg_publish` to copy a resource from your private Dugg to a shared server. Always explicit.

See [PUBLISHING.md](PUBLISHING.md).

## What happens to my data if a server goes down?

If your private Dugg goes down, you have a SQLite file. Move it, restore it, run again. Nothing's lost.

If a shared server you subscribed to goes down, anything you'd already pulled via RSS is in your private Dugg. Anything you hadn't pulled is gone. The export/import CLI (`dugg export`, `dugg import`) lets you take a portable snapshot of any server you have access to.

## Can I export my data?

Yes. `dugg export <file>` writes a portable `.dugg.json` containing all your resources (or a filtered subset). `dugg import` reads them back into any server you have access to. Format is documented in [TOOLS.md](TOOLS.md).

## How do rate limits work?

Each shared server caps your daily submissions:

```
cap = initial + (days_as_member × growth)
```

Defaults: 5/day initial, +1/day. New users start low, the cap grows with tenure. Reactions, reads, and searches are not rate-limited. Caps reset at UTC midnight.

Don't retry on rate limit errors — wait for the next day. The error response tells you exactly how many you've used and when the reset is.

## What's a "skill" in Dugg?

A SKILL.md document — a reusable procedure your agent can invoke. Add one with `dugg skill add ./SKILL.md`. List with `dugg skill list`. Fork to customize. Skills federate across subscribed servers, so a skill written on one server can be discovered and used by agents on others.

## Does Dugg use my LLM tokens?

The server uses zero LLM tokens. All enrichment (summarization, tagging, transcript pulling) happens on your agent's side using your tokens. The server is pure storage + indexing + retrieval.

## Is this MCP-only?

No. Dugg is an MCP server, but it's also:

- An HTTP REST API (any client, any language)
- A CLI for direct human use
- A web feed (any browser)
- An iOS app (Share Extension + native UI)
- A Chrome extension
- A Slack integration
- An email forwarding endpoint

Pick whatever surface fits your context. They all hit the same data.

## How do I run my own shared server?

See [SETUP.md](../SETUP.md) and [DEPLOY.md](../DEPLOY.md). Short version: clone the repo, set up Python + uv, run `dugg serve --http`, point a domain at it. Cloudflare Pages is fine for the email worker; the main server runs anywhere Python runs.

## How does Dugg compare to Notion / Roam / Obsidian?

Different category. Those are notebooks for writing. Dugg is a saved-content layer for things you didn't author. They're complementary — Dugg holds the articles you read, your Notion holds the notes you wrote about them.

That said, the `dugg paste` command lets you save raw text content (no URL needed), and notes on resources let you annotate. So there's overlap if you stretch.

## Is Dugg open source?

Yes — MIT licensed. Server, CLI, Chrome extension, iOS app source. The hosted Cloud version (in development) is a separate revenue path; the OSS will always be self-hostable.

## Where do I report a bug or request a feature?

GitHub Issues at [github.com/kadedworkin/dugg-fyi](https://github.com/kadedworkin/dugg-fyi). For iOS-specific issues, [github.com/kadedworkin/dugg-ios](https://github.com/kadedworkin/dugg-ios).

## How does this relate to AGENTS.md / SKILL.md / the agent ecosystem?

Dugg is one piece of the broader "agent has tools, agent has memory, agent has skills" stack. SKILL.md procedures live inside Dugg. AGENTS.md describes how an agent works in a repo — different concern. They compose, but neither requires the other.

## What's the long-term plan?

- Open-source self-hosted server stays free, MIT, fully featured
- Hosted Cloud tier (for non-technical users) launches mid-2026 with a free hosted-fork tier and a paid $50/mo flat tier
- Agent-first features (skill federation, broadcast subscriptions, content discovery) are the differentiators

See [project_dugg_cloud_strategy_v2](https://github.com/kadedworkin/dugg-fyi/issues) discussions for the public roadmap.
