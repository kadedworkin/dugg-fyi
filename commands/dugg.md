---
name: dugg
description: "Add, search, and browse your Dugg knowledge base. Usage: /dugg (feed), /dugg <url> [note] (add), /dugg search <query>"
---

# /dugg — Dugg Quick Command

You are handling a `/dugg` slash command. Parse the user's input and take the appropriate action using the Dugg MCP tools.

## Routing

1. **No arguments** (`/dugg`) → Show the feed
   - Call `dugg_feed()` with `limit: 5`
   - Format as a clean list: title, URL, who added it, date, note

2. **URL as first argument** (`/dugg https://... [note text]`) → Add a resource
   - Extract the URL (first token starting with `http://` or `https://`)
   - Everything after the URL is the note
   - Call `dugg_add(url: "...", note: "...")`
   - Confirm what was added with the enriched title

3. **Text that isn't a URL** (`/dugg some search terms`) → Search
   - Call `dugg_search(query: "...")` with `limit: 5`
   - Format results: title, URL, who added it, note

## Response Format

Keep responses concise. Use this format:

For feed/search results:
```
**Title**
URL
by Name · 2026-04-14
_Note text if present_
```

For adds:
```
Added: **Enriched Title**
URL
Note: whatever they wrote
```

## If MCP tools aren't available

Tell the user to add Dugg as an MCP server first. Point them to the SETUP.md in this repo for configuration instructions for their specific agent platform (Claude Code, Claude Desktop, OpenClaw, or any MCP-compatible agent).
