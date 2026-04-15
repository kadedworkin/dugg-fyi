# Publishing & Federation

Dugg instances federate through a publish/subscribe model. Your local Dugg is the source of truth — publishing pushes selected content to remote instances.

## Publishing

```
dugg_publish(resource_id="abc123", targets=["public", "aev-team"])
dugg_unpublish(resource_id="abc123", targets=["public"])
dugg_unpublish(resource_id="abc123")  # unpublish from everything
```

- Every resource starts local and unpublished
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

- **Deduplication** — same URL in the same collection = skip
- **Source tracking** — originating instance ID and server URL stored in metadata
- Feed and search results display the source server

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
