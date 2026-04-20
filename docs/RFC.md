# RFC Compliance

Dugg implements or references the following IETF RFCs across its codebase. This document catalogues each one, where it's used, and how.

---

## Syndication & Federation

### RFC 4287 — Atom Syndication Format

**Where:** `http.py` (feed endpoint), `rss.py` (feed parser)

The `/feed/{key}` endpoint generates a compliant Atom 1.0 feed with `<entry>`, `<title>`, `<link>`, `<id>`, `<updated>`, `<published>`, `<author>`, `<category>`, and `<summary>` elements. Content negotiation (see RFC 7231 below) returns Atom XML when the client sends `Accept: application/atom+xml`.

Inbound feeds are parsed via `feedparser` for RSS daemon polling, with manual ElementTree extraction for elements feedparser doesn't handle (tombstones).

### RFC 6721 — Atom Tombstones

**Where:** `http.py` (tombstone generation), `rss.py` (`_parse_tombstones()`), `db.py` (deletion records)

When a resource is deleted, Dugg records it in a `deleted_resources` table and emits `<at:deleted-entry>` elements in Atom feeds per RFC 6721. Subscribing instances parse these tombstones and prune their local copies, enabling bidirectional deletion propagation across federated Dugg instances.

### RFC 8288 — Web Linking

**Where:** `http.py` (feed endpoint)

The Atom feed response includes a `Link` header with `rel="self"` pointing to the feed's own URL, and the HTML feed view includes a `Link` header with `rel="alternate"` pointing to the Atom representation. The Atom XML also includes a `<link rel="self">` element. This enables feed auto-discovery and proper client caching behavior.

---

## HTTP Semantics

### RFC 7231 — HTTP Semantics and Content Negotiation

**Where:** `http.py` (feed endpoint, invite page)

The `/feed/{key}` endpoint inspects the `Accept` header to serve either Atom XML (`application/atom+xml`) or an HTML page for the same URL. Invite pages similarly negotiate between JSON and HTML responses based on the `Content-Type` / `Accept` header.

### RFC 7232 — Conditional Requests

**Where:** `rss.py` (RSS daemon polling)

The RSS polling daemon sends `If-Modified-Since` and `ETag` headers on feed fetches. When a remote server returns `304 Not Modified`, the daemon skips parsing entirely — saving bandwidth and processing for feeds that haven't changed.

### RFC 7807 — Problem Details for HTTP APIs

**Where:** `http.py` (all error responses)

Every HTTP error response uses the RFC 7807 `application/problem+json` media type with structured fields:

```json
{
  "type": "about:blank",
  "title": "Bad Request",
  "status": 400,
  "detail": "Missing resource.url"
}
```

This replaces ad-hoc `{"error": "..."}` payloads with a standardized format that clients can parse uniformly. Additional context fields (e.g., rate limit details) may be included as extension members.

### RFC 6585 — 429 Too Many Requests

**Where:** `http.py` (`handle_tools` endpoint)

When a rate-limited tool invocation hits the daily cap, the HTTP transport returns `429 Too Many Requests` with a `Retry-After` header set to the number of seconds until the next UTC midnight reset. MCP clients still receive the rate limit message as tool output text; the 429 status is specific to the HTTP REST interface.

---

## Security

### RFC 2104 — HMAC

**Where:** `http.py` (webhook signature verification, Slack request verification)

Webhook payloads are signed with HMAC-SHA256 via the `X-Dugg-Signature: sha256=<hex>` header. Verification uses `hmac.compare_digest()` for timing-safe comparison, preventing timing side-channel attacks. The same pattern secures Slack request signature validation.

### RFC 6265 — HTTP Cookies

**Where:** `http.py` (`/r/{resource_id}/unlock` viewer)

The shareable resource viewer sets an `HttpOnly`, `Secure`, `SameSite=Lax` cookie after key verification. This authenticates subsequent page views without exposing the API key in the URL, while the form gate prevents unauthorized access even if the URL leaks.

---

## Data Formats

### RFC 3339 — Date and Time on the Internet (Timestamps)

**Where:** `db.py` (all temporal columns)

Every timestamp in the database — `created_at`, `deleted_at`, `updated_at`, `published_at` — is stored as ISO 8601 / RFC 3339 format (`YYYY-MM-DDTHH:MM:SS+00:00`). Atom feed elements use the same format.

### RFC 3986 — URI Syntax

**Where:** `db.py` (`is_private_link()`), `rss.py` (URL parsing), `http.py` (URL resolution)

URL parsing and validation follows RFC 3986. The `is_private_link()` function inspects URI components to detect private/local network addresses. Query parameters in parameterized URLs (e.g., RSS feeds with `?tag=...`) are preserved through ingestion.

### RFC 5322 — Internet Message Format

**Where:** `dugg-email-worker` (Cloudflare Worker)

The email forwarding worker parses inbound email `Date` headers per RFC 5322 to extract `published_at` timestamps for ingested content.

---

## Summary

| RFC | Title | Status |
|-----|-------|--------|
| 4287 | Atom Syndication Format | Implemented |
| 6721 | Atom Tombstones | Implemented |
| 8288 | Web Linking | Implemented |
| 7231 | HTTP Semantics | Implemented |
| 7232 | Conditional Requests | Implemented |
| 7807 | Problem Details | Implemented |
| 6585 | 429 Too Many Requests | Implemented |
| 2104 | HMAC | Implemented |
| 6265 | HTTP Cookies | Implemented |
| 3339 | Timestamps | Implemented |
| 3986 | URI Syntax | Implemented |
| 5322 | Internet Message Format | Implemented |
