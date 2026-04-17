# Scaling Dugg

Dugg is designed for small, trusted teams — 25 to 100 contributors per instance. But the contributor/subscriber model lets a single instance serve thousands of readers without architectural changes.

## Two roles, two resource profiles

| | Contributor | Subscriber |
|---|---|---|
| Can post | Yes (rate-limited by tenure) | No (zero cap, permanent) |
| Agent key | Yes (MCP/SSE access) | No |
| Feed access | Yes | Yes |
| Search | Yes (via agent) | Yes (via feed/browser) |
| Reactions | Yes | No |
| Server-side cost | SSE connection + write load | HTTP GET on cacheable feed |

Contributors are the curators — they run agents, connect via SSE, post resources, and generate events. Each contributor holds a persistent connection and creates write load.

Subscribers consume via the Atom/HTML feed at `/feed/{key}`. Each feed request is a standard HTTP GET with no persistent state on the server. Put a CDN or reverse proxy in front of it and it scales to tens of thousands with zero server impact.

## Creating subscriber invites

```bash
# CLI
dugg invite-user "Reader Name" --role subscriber

# MCP tool
dugg_invite_user(name="Reader Name", role="subscriber")
```

Subscriber invites create a user with:
- A human API key (for feed access)
- No agent key (no MCP/SSE connection)
- Zero post cap that never grows regardless of tenure

When a subscriber redeems an invite, they get a feed URL they can bookmark. That's their entire interface.

## Architecture at scale

```
Contributors (25-100)          Subscribers (1,000-10,000+)
┌──────────────┐               ┌──────────────┐
│ Agent + SSE  │               │ Browser/RSS  │
│ Full MCP     │               │ Atom feed    │
│ Write access │               │ HTTP GET     │
└──────┬───────┘               └──────┬───────┘
       │                              │
       │ SSE/MCP                      │ HTTP (cacheable)
       │                              │
┌──────▼──────────────────────────────▼───────┐
│              Dugg Instance                   │
│  SQLite + FTS5 │ Publish Queue │ Events      │
└──────────────────────────────────────────────┘
```

The server's resource-intensive features (SSE connections, event streaming, webhook dispatch) are only consumed by contributors. Subscribers never touch those paths.

## What breaks at what scale

| Component | Safe up to | Limit | Fix |
|---|---|---|---|
| SSE connections | ~1,024 | OS file descriptor limit | `ulimit -n 65535` |
| `/events/stream` polling | ~200 clients | DB read load (5s poll interval) | Use webhooks instead |
| Webhook threads | ~100/sec | Unbounded thread spawning | Thread pool (planned) |
| Rate limit queries | ~100K resources | Missing composite index | Add index (planned) |
| Feed requests (subscribers) | Unlimited | Standard HTTP | CDN/reverse proxy |

Subscribers don't contribute to any of the first four limits. They only generate feed requests, which are standard HTTP GETs that any reverse proxy can cache.

## Deployment recommendations

### Small team (5-25 people, all contributors)

Default config. No tuning needed.

### Medium team (25-100 contributors)

Add to your server startup:
- `PRAGMA busy_timeout=5000` (prevents SQLITE_BUSY under write contention)
- Ensure `ulimit -n 4096` or higher

### Large audience (100 contributors + 1,000+ subscribers)

- Put a reverse proxy (nginx, Caddy) in front of the server
- Cache `/feed/{key}` responses (1-5 minute TTL is fine)
- Contributors connect directly to the server via SSE
- Subscribers hit the cached feed endpoint
- Consider webhook-based delivery instead of `/events/stream` polling

### Enterprise broadcast (25-100 curators + 10,000 subscribers)

Same as large audience, plus:
- Use a CDN for feed delivery
- Subscriber agents can poll the Atom feed and ingest resources locally
- This means subscriber agents work even when the shared server is unreachable
- Rate limit the feed endpoint at the proxy level (e.g., 1 request/minute per key)

## The subscriber agent pattern

A subscriber's agent can still provide full retrieval value:

1. Agent polls the shared server's Atom feed periodically
2. Agent ingests new resources into the subscriber's **local** Dugg
3. When the human asks a question, the agent searches **local** Dugg (which now includes shared content)
4. Agent builds context from local results and sends it to the LLM

This means the subscriber gets the same "search across everything" experience as a contributor — the only difference is they can't post back to the shared server. Their local Dugg becomes a mirror of what they have access to.

## When to split into multiple instances

If you find yourself wanting more than 100 contributors on a single instance, consider splitting by topic:

- Instance A: "Engineering" (50 contributors)
- Instance B: "Product & Design" (30 contributors)
- Instance C: "Industry News" (20 contributors)

Contributors can subscribe to multiple instances. Federation handles cross-instance publishing. Each instance stays small and fast, and subscribers can follow any combination of feeds.
