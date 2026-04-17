# Slack notifications for your Dugg server

Get a message in any Slack channel every time a resource is added to your Dugg server. Takes about five minutes end-to-end.

## What you'll see

Every new resource posts to your chosen Slack channel as a clean, linkable message:

```
*How to build an agentic knowledge base*
<https://example.com/article>
Added by Rocco · from chino-bandido
_context note if you included one_
```

Each message includes **Tap / Star / Nice** buttons — click one to silently react. The resource's author gets a separate notification with aggregate counts.

The formatting is automatic — Dugg detects Slack webhook URLs and uses Slack Block Kit payloads; other webhook targets get raw JSON instead.

## Step 1 — Create a Slack app

1. Go to https://api.slack.com/apps and click **Create New App**.
2. Choose **From scratch**.
3. Name it whatever you want (e.g. "Dugg") and pick the workspace you want notifications in.
4. Click **Create App**.

## Step 2 — Enable Incoming Webhooks

1. In the app's sidebar, click **Incoming Webhooks**.
2. Toggle **Activate Incoming Webhooks** to on.
3. Scroll down and click **Add New Webhook to Workspace**.
4. Pick the channel you want Dugg to post to (or create a new one like `#dugg-feed`).
5. Click **Allow**.

## Step 3 — Grab the webhook URL

You'll now see a row with your new webhook. Copy the **Webhook URL** — it looks like:

```
https://hooks.slack.com/services/T00000000/B00000000/XXXXXXXXXXXXXXXX
```

Keep it somewhere safe — anyone with this URL can post to your channel.

## Step 4 — Register it with Dugg

Three ways to do this. Pick whichever matches your setup.

### Option A — via your MCP agent

Tell your agent:

> Subscribe a Slack webhook on my Dugg server. Callback URL is `https://hooks.slack.com/services/...`.

The agent calls `dugg_webhook_subscribe` with your callback URL. Done.

### Option B — via curl

Replace `YOUR_DUGG_KEY` with your Dugg API key and the URL with your Slack webhook:

```bash
curl -X POST https://chino-bandido.kadedworkin.com/tools/dugg_webhook_subscribe \
  -H "X-Dugg-Key: YOUR_DUGG_KEY" \
  -H "Content-Type: application/json" \
  -d '{"callback_url": "https://hooks.slack.com/services/T00000000/B00000000/XXXXXXXXXXXXXXXX"}'
```

You should see a response like:

```
Webhook subscribed: https://hooks.slack.com/services/...
Instance: all (server-wide)
Events: all
```

### Option C — via the Dugg CLI (if you run Dugg locally)

```bash
dugg webhook add https://hooks.slack.com/services/T00000000/B00000000/XXXXXXXXXXXXXXXX
```

## Step 5 — Enable reactions (interactive buttons)

New resource messages include Tap / Star / Nice buttons. For these to work, you need to enable interactivity in your Slack app:

1. In the app's sidebar, click **Interactivity & Shortcuts**.
2. Toggle **Interactivity** to on.
3. Set the **Request URL** to: `https://your-server/slack/actions`
   (e.g. `https://chino-bandido.kadedworkin.com/slack/actions`)
4. Click **Save Changes**.

When someone clicks a reaction button:
- The reactor sees an ephemeral confirmation (only visible to them)
- The resource's author gets a webhook notification: ":star: Your resource *Title* got a star — 3 total reactions (1 star, 2 tap)"
- Reaction counts are only visible to the author — no one else knows who reacted

If you set a signing secret in Step 4 (slash command), the same secret is used to verify interactive payloads. No additional config needed.

## Step 6 — Test it

Add any resource to Dugg — via your agent, the Chrome extension, email, or paste. You should see a message show up in your Slack channel within a second or two.

If nothing arrives:

- **List your webhooks** to confirm it's registered: call `dugg_webhook_list` via your agent, or `GET /tools/dugg_webhook_list`.
- **Check status** — if the `status` field is `failed`, Dugg hit 5 consecutive delivery errors and disabled it. Verify the Slack URL is still valid and re-register.
- **Unsubscribe** with `dugg_webhook_delete` if you want to start over.

## Filtering events (optional)

By default your webhook fires on every event. If you want just the additions, pass `event_types`:

```json
{
  "callback_url": "https://hooks.slack.com/...",
  "event_types": ["resource_added"]
}
```

Common event types: `resource_added`, `resource_published`, `resource_deleted`, `reaction_added`, `member_joined`, `member_banned`, `invite_created`, `invite_redeemed`, `publish_delivered`. Omit `event_types` or pass `[]` for all.

## Security note

The Slack webhook URL is a bearer secret for your Slack channel — treat it like a password. If it leaks, delete it in the Slack app settings and create a new one; then re-register the new URL with Dugg and remove the old one.

For production webhooks pointing at your own infrastructure (not Slack), pass a `secret` when subscribing — Dugg will HMAC-SHA256 sign every payload with it via the `X-Dugg-Signature` header.
