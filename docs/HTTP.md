# HTTP/SSE Mode

Run Dugg as an HTTP server for remote agent connections, browser UIs, and federation.

## Start the server

```bash
# Start with SSE transport on default port
dugg serve --transport http --port 8411

# Custom host/port
dugg serve --transport http --host 127.0.0.1 --port 9000

# With a custom database
dugg --db /path/to/dugg.db serve --transport http
```

`server_url` is auto-detected from `--host`/`--port` on first HTTP serve. Override with `dugg set-config server_url https://your-domain`.

## Bootstrap (first user)

For HTTP-only deployments where CLI isn't available:

```bash
curl -X POST http://localhost:8411/bootstrap \
  -H "Content-Type: application/json" \
  -d '{"name": "Admin"}'
```

Returns the admin API key. The endpoint disables itself once any user exists.

## Connect a remote agent

Configure your MCP client with the SSE transport and your API key:

```json
{
  "mcpServers": {
    "dugg": {
      "transport": "sse",
      "url": "https://your-host:8411/sse",
      "headers": {
        "X-Dugg-Key": "dugg_your_api_key"
      }
    }
  }
}
```

## Endpoints

| Endpoint | Method | Auth | Description |
|----------|--------|------|-------------|
| `/sse` | GET | Key | MCP SSE transport — connect MCP clients over HTTP |
| `/messages` | POST | Key | MCP message endpoint (used by SSE clients) |
| `/ingest` | POST | Key | Receive published resources from remote instances |
| `/tools/{name}` | POST | Key | HTTP dispatch for any MCP tool |
| `/events/stream` | GET | Key | SSE stream of real-time Dugg events |
| `/invite/{token}` | GET | None | Invite page (HTML for browsers, JSON for agents via `Accept: application/json`) |
| `/invite/{token}/redeem` | POST | None | Process invite (form or JSON) |
| `/feed/{key}` | GET | None | Browser-friendly feed (HTML or Atom XML) |
| `/content/{key}/{id}` | GET | Key-in-URL | View full content of a pasted/forwarded resource |
| `/paste/{key}` | GET | Key-in-URL | Browser form for pasting raw content |
| `/health` | GET | None | Liveness check |
| `/bootstrap` | POST | None | Create first admin user (disabled once any user exists) |
| `/slack/command` | POST | Slack | Slack slash command endpoint |
| `/admin/{key}` | GET | Key-in-URL | Browser admin panel — collections, members, resources |
| `/admin/{key}/ban` | POST | Key-in-URL | Ban a user (owner only) |
| `/admin/{key}/unban` | POST | Key-in-URL | Unban a user (owner only) |
| `/admin/{key}/remove` | POST | Key-in-URL | Remove a resource (owner or submitter) |

**Authentication:** Endpoints marked "Key" require an `X-Dugg-Key` header. Invite and feed endpoints are unauthenticated by design — the token/key in the URL acts as the credential.

## Examples

**Ingest via HTTP:**

```bash
curl -X POST http://localhost:8411/ingest \
  -H "X-Dugg-Key: dugg_your_api_key" \
  -H "Content-Type: application/json" \
  -d '{
    "resource": {"url": "https://example.com/article", "title": "Cool Article"},
    "source_instance_id": "remote123",
    "source_server": "https://remote.dugg.fyi"
  }'
```

**Call a tool via HTTP:**

```bash
curl -X POST http://localhost:8411/tools/dugg_search \
  -H "X-Dugg-Key: dugg_your_api_key" \
  -H "Content-Type: application/json" \
  -d '{"query": "webhook architectures"}'
```
