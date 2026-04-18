# Publishing & Federation

Dugg instances federate through a publish/subscribe model. Your private Dugg is the source of truth — publishing pushes selected content to remote instances.

## Publishing

```
dugg_publish(resource_id="abc123", targets=["public", "aev-team"])
dugg_unpublish(resource_id="abc123", targets=["public"])
dugg_unpublish(resource_id="abc123")  # unpublish from everything
```

- Every resource starts private and unpublished
- You (or your agent) flag resources with named publish targets
- Each target maps to a remote Dugg instance
- Non-concentric circles: publish to any combination independently
- Only the resource submitter can publish/unpublish

## Auto-routing

Agents use instance topic descriptors to auto-route content — the user doesn't manually pick targets.

```
dugg_routing_manifest()
# Returns: [{name: "Food Dugg", topic: "food, restaurants..."},
#           {name: "AI Dugg", topic: "AI, agents..."}]
```

**User flow:**
1. `/dugg [link] [note]` — that's it
2. Agent enriches, auto-tags, scores against subscribed instance topics
3. Agent calls `dugg_publish` with the targets it picked
4. User verifies/overrides after the fact if needed

## Silent reactions

Subscribers silently react to resources. The publisher sees aggregate counts — nobody else sees anything.

```
dugg_react(resource_id="abc123", reaction="tap")
dugg_reactions(resource_id="abc123")  # publisher only
```

No public like counts, no emoji piles, no social proof pressure. Reactions are idempotent.

## Publish sync daemon

When you call `dugg_publish`, the resource is queued for delivery to remote instances.

1. Background sync loop picks up pending entries every 30 seconds
2. FIFO ordering — oldest-first per target
3. POSTs to each remote instance's `/ingest` endpoint
4. Success: marked `delivered`. Failure: exponential backoff retry

**Retry schedule:** 30s -> 2min -> 8min -> 32min -> ~2h. After 5 failures, marked `failed`.

**Queue management:**

```
dugg_publish_status()                                    # check queue
dugg_publish_retry()                                     # retry all failures
dugg_publish_retry_selective(target_instance_id="abc")   # retry specific target
dugg_publish_clear()                                     # clear failed entries
```

30-day TTL purge on failed entries. The sync daemon runs as an asyncio background task — no separate process.

## Remote ingest

The receiving side of federation. When a remote Dugg pushes content:

- **Deduplication, with preserved enrichment** — same URL in the same collection does not create a second row, but the incoming note is attached as a *sibling note* on the existing resource. Tags from the incoming payload are unioned onto the existing resource.
- **Source tracking** — originating instance ID and server URL stored in metadata
- Feed and search results display the source server

### Sibling notes (collision quarantine)

When a URL arrives that the receiving server already has (whether it was saved locally or received from a third server previously), the incoming note lives in a dedicated `resource_notes` table instead of overwriting `resources.note` or being dropped. This protects against two failure modes:

1. **Lost enrichment** — multiple people saving the same URL with different context would otherwise lose every note past the first.
2. **Outbound re-federation of foreign content** — if incoming notes merged into `resources.note`, the next outbound publish from this server would carry a foreign submitter's text onward to a third server. Storing siblings in a separate table that the publish payload builder never reads makes that leakage structurally impossible.

The same quarantine handles same-server collisions: when a second user (or the same user with a different highlight via the Chrome extension) saves a URL already present in the collection, their note becomes a sibling rather than a duplicate resource row.

**Search:** sibling text is searchable through a parallel FTS index. A ban on the contributing user (direct or via cascade) removes their sibling notes from search results at query time — no stored-state rewrite needed.

## Remote delete sync

Dugg implements full CRUD symmetry for federated content. Just as `POST /ingest` pushes resources to remote servers, `POST /delete` removes them.

When you delete a resource locally that was previously published to remote servers:

1. The MCP tool (`dugg_delete_resource`) checks `publish_targets` for the resource
2. For each target server, it fires `POST /delete` with the resource URL and your API key
3. The remote server verifies you're the submitter or collection owner, then deletes
4. The remote server records a tombstone so other subscribers are notified via the Atom feed
5. The local copy is deleted

**Directionality rule:** `publish_targets` determines whether a delete propagates upstream.

| Content source | `publish_targets` | On local delete |
|---|---|---|
| You added it, published to servers | Has entries | Upstream deletes fire automatically |
| Pulled via RSS from a shared server | Empty | Local-only delete, server unaffected |

This prevents cascading deletes: if you remove an RSS-synced article from your local library, the server copy (and every other subscriber's copy) stays intact. You're only cleaning your own shelf.

### The `/delete` endpoint

```
POST /delete
X-Dugg-Key: dugg_your_api_key
Content-Type: application/json

{"url": "https://example.com/article-to-remove"}
```

Returns `200` with `{"status": "deleted", "id": "...", "url": "..."}` on success, `404` if the resource doesn't exist (idempotent), `403` if you're not authorized.

## Atom tombstones (RFC 6721)

When a resource is deleted on a shared server, the deletion propagates to RSS subscribers via [Atom Tombstones (RFC 6721)](https://datatracker.ietf.org/doc/html/rfc6721).

The server's Atom feed includes `at:deleted-entry` elements alongside regular entries:

```xml
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:at="http://purl.org/atompub/tombstones/1.0">
  <at:deleted-entry ref="resource-id" when="2026-04-17T12:00:00+00:00">
    <at:comment>Removed: Malware link</at:comment>
    <link href="https://example.com/bad-link"/>
  </at:deleted-entry>
  <!-- regular entries follow -->
</feed>
```

The RSS poller processes tombstones **before** regular entries on each poll cycle. When a tombstone is encountered:

1. The matching local resource is looked up by URL
2. It's hard-deleted — gone from search, feed, CLI, everything
3. The tombstone ref is removed from `seen_entry_ids` to prevent stale tracking

**Safety property:** If a malware link gets published and an admin removes it, every subscriber's local copy is deleted on the next poll cycle (≤1 hour). No ghost links linger on subscriber machines.

**Retention:** Tombstones are retained for 30 days, then pruned automatically. Subscribers whose pollers are offline longer than 30 days will miss the tombstone — a full-sync reconciliation handles this edge case by comparing local entry IDs against the live feed.

**No loop risk:** Tombstone processing uses `delete_resource_by_url()` which bypasses the owner check (the upstream server's tombstone is authoritative) and does not create publish_targets entries, so no upstream delete fires. The deletion stays local.

## Events

Every significant action emits an event:

| Event | Emitted when |
|-------|-------------|
| `resource_added` | A resource is added |
| `resource_published` | A resource is published to a target |
| `member_joined` | A member joins a collection |
| `member_banned` | A ban cascade executes |
| `publish_delivered` | A publish is delivered to a remote instance |
| `invite_created` | An invite token is generated |
| `invite_redeemed` | An invite token is redeemed |
| `reaction_added` | A user reacts to a resource |

```
dugg_events()                           # recent events
dugg_catchup()                          # unseen since last check
dugg_mark_seen()                        # advance cursor
```

Events are scoped to your subscribed instances and collections.

## Webhooks

Push instead of poll. Subscribe callback URLs for real-time POST notifications.

```
dugg_webhook_subscribe(callback_url="https://my-agent.com/hooks/dugg")
dugg_webhook_subscribe(instance_id="abc123",
                       callback_url="https://...",
                       event_types=["resource_published"],
                       secret="my-webhook-secret")
```

- 15-second timeout per delivery
- Auto-pause after 5 consecutive failures
- HMAC-SHA256 signing via `X-Dugg-Signature` header
