"""Publish sync daemon and webhook delivery for Dugg.

Runs as an async background task alongside the MCP server.
Handles outbound delivery of published resources to remote instances
and webhook notifications for subscribed agents.
"""

import asyncio
import hashlib
import hmac
import json
import logging
from typing import Optional
from urllib.parse import urljoin

logger = logging.getLogger("dugg.sync")


try:
    import httpx
    HAS_HTTPX = True
except ImportError:
    HAS_HTTPX = False


async def deliver_publish(db, queue_entry: dict) -> bool:
    """Attempt to deliver a single publish queue entry to its target instance.

    POSTs the resource data to the remote instance's endpoint_url/ingest.
    Returns True on success, False on failure.
    """
    if not HAS_HTTPX:
        logger.warning("httpx not installed — cannot deliver publishes. Install with: pip install httpx")
        return False

    queue_id = queue_entry["id"]
    endpoint_url = queue_entry["endpoint_url"]
    resource_id = queue_entry["resource_id"]
    target_name = queue_entry["target_name"]

    resource = db.get_resource(resource_id)
    if not resource:
        db.mark_publish_delivered(queue_id)  # Resource deleted, nothing to deliver
        return True

    # Resolve submitter name for remote attribution
    submitter = db.get_user(resource.get("submitted_by", ""))
    submitter_name = submitter["name"] if submitter else ""

    # Build the payload
    payload = {
        "resource": {
            "url": resource["url"],
            "title": resource.get("title", ""),
            "description": resource.get("description", ""),
            "thumbnail": resource.get("thumbnail", ""),
            "source_type": resource.get("source_type", "unknown"),
            "author": resource.get("author", ""),
            "transcript": resource.get("transcript", ""),
            "note": resource.get("note", ""),
            "tags": resource.get("tags", []),
            "enriched_at": resource.get("enriched_at"),
            "submitter_name": submitter_name,
        },
        "target": target_name,
        "source_instance_id": queue_entry["target_instance_id"],
        "source_server": db.get_config("server_url", ""),
    }

    db.mark_publish_delivering(queue_id)

    try:
        ingest_url = urljoin(endpoint_url.rstrip("/") + "/", "ingest")
        headers = {}
        ingest_key = queue_entry.get("ingest_api_key", "")
        if ingest_key:
            headers["X-Dugg-Key"] = ingest_key
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(ingest_url, json=payload, headers=headers)
            response.raise_for_status()

        db.mark_publish_delivered(queue_id)
        db.emit_event("publish_delivered",
                       payload={"resource_id": resource_id, "target": target_name,
                                "instance": queue_entry.get("instance_name", "")})
        logger.info(f"Delivered {resource_id} to {endpoint_url} ({target_name})")
        return True

    except Exception as e:
        error_msg = f"{type(e).__name__}: {e}"
        db.mark_publish_retry(queue_id, error_msg)
        logger.warning(f"Failed to deliver {resource_id} to {endpoint_url}: {error_msg}")
        return False


async def deliver_upstream_delete(endpoint_url: str, api_key: str, url: str, source_instance_id: str = "") -> bool:
    """POST a delete request to a remote Dugg server.

    Mirrors the /ingest→/delete CRUD symmetry. Called when a user deletes
    a resource locally that was previously published to a remote server.
    Returns True on success, False on failure.
    """
    if not HAS_HTTPX:
        logger.warning("httpx not installed — cannot deliver upstream delete")
        return False

    try:
        delete_url = urljoin(endpoint_url.rstrip("/") + "/", "delete")
        headers = {}
        if api_key:
            headers["X-Dugg-Key"] = api_key
        payload = {"url": url}
        if source_instance_id:
            payload["source_instance_id"] = source_instance_id
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(delete_url, json=payload, headers=headers)
            if response.status_code == 404:
                logger.info(f"Upstream delete: {url} not found on {endpoint_url} (already removed)")
                return True
            response.raise_for_status()
        logger.info(f"Upstream delete delivered: {url} → {endpoint_url}")
        return True
    except Exception as e:
        logger.warning(f"Upstream delete failed for {url} → {endpoint_url}: {e}")
        return False


async def deliver_webhook(db, webhook: dict, event: dict) -> bool:
    """Deliver an event to a webhook subscriber.

    POSTs the event payload to the callback URL with optional HMAC signing.
    Returns True on success, False on failure.
    """
    if not HAS_HTTPX:
        return False

    callback_url = webhook["callback_url"]
    webhook_id = webhook["id"]

    headers = {"Content-Type": "application/json"}

    # HMAC signing if secret is set
    body = json.dumps(event, sort_keys=True)
    if webhook.get("secret"):
        signature = hmac.new(
            webhook["secret"].encode(), body.encode(), hashlib.sha256
        ).hexdigest()
        headers["X-Dugg-Signature"] = f"sha256={signature}"

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(callback_url, content=body, headers=headers)
            response.raise_for_status()

        db.mark_webhook_success(webhook_id)
        return True

    except Exception as e:
        logger.warning(f"Webhook delivery failed for {callback_url}: {e}")
        db.mark_webhook_failure(webhook_id)
        return False


async def process_webhooks_for_event(db, event: dict):
    """Find all webhook subscriptions matching an event and deliver."""
    event_type = event["event_type"]
    instance_id = event.get("instance_id")

    webhooks = db.get_webhooks_for_event(event_type, instance_id=instance_id)
    if not webhooks:
        return

    tasks = [deliver_webhook(db, wh, event) for wh in webhooks]
    await asyncio.gather(*tasks, return_exceptions=True)


async def run_storage_eviction(db):
    """Check all instances and evict content if storage cap exceeded."""
    try:
        rows = db.conn.execute("SELECT id FROM dugg_instances").fetchall()
        for row in rows:
            evicted = db.run_eviction(row["id"])
            if evicted > 0:
                logger.info(f"Evicted {evicted} resource(s) for instance {row['id']}")
    except Exception as e:
        logger.error(f"Storage eviction error: {e}")


async def sync_loop(db, interval: int = 30):
    """Main sync daemon loop. Runs forever, processing pending publishes.

    Args:
        db: DuggDB instance
        interval: Seconds between sync cycles (default 30)
    """
    logger.info(f"Publish sync daemon started (interval: {interval}s)")

    eviction_counter = 0
    ttl_counter = 0
    while True:
        try:
            # Use FIFO-gated fetch to prevent backlog leapfrogging
            pending = db.get_pending_publishes_fifo(limit=20)
            if pending:
                logger.info(f"Processing {len(pending)} pending publish(es)")
                for entry in pending:
                    success = await deliver_publish(db, entry)
                    if success:
                        # Deliver webhooks for successful publishes
                        event = {
                            "event_type": "publish_delivered",
                            "resource_id": entry["resource_id"],
                            "target": entry["target_name"],
                            "instance_name": entry.get("instance_name", ""),
                        }
                        await process_webhooks_for_event(db, event)
        except Exception as e:
            logger.error(f"Sync loop error: {e}")

        # Run storage eviction every ~10 cycles (5 minutes at default 30s interval)
        eviction_counter += 1
        if eviction_counter >= 10:
            eviction_counter = 0
            await run_storage_eviction(db)

        # Purge old failed publishes every ~100 cycles (~50 min at 30s)
        ttl_counter += 1
        if ttl_counter >= 100:
            ttl_counter = 0
            try:
                purged = db.purge_old_failed_publishes()
                if purged > 0:
                    logger.info(f"Purged {purged} expired failed publish(es)")
            except Exception as e:
                logger.error(f"TTL purge error: {e}")

        await asyncio.sleep(interval)


def start_sync_daemon(db, interval: int = 30) -> asyncio.Task:
    """Start the sync daemon as a background asyncio task.

    Call this from the MCP server startup to run alongside stdio serving.
    Returns the task handle.
    """
    return asyncio.create_task(sync_loop(db, interval))
