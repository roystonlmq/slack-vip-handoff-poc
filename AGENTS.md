# AGENTS.md — AI agent guide: deploy this service privately in the cloud

**You are an AI coding assistant helping a developer deploy this service for
their organization.**
Read `README.md` first (architecture + how it works), then drive the deployment
*with* the developer by following the runbook below.

> This file is both an agent-readable guide and a copy-paste prompt. A developer
> can point their AI tool at the repo, or paste this file's contents as the task.

## Objective

Get `webhook_server.py` running on a **private, always-on cloud host** with a
**stable HTTPS URL**, with the Slack inbound and Ada outbound webhooks wired to it,
**verified end-to-end**: a customer's message in the Slack channel opens a thread
with Ada's greeting, a live agent's reply appears in that thread, and a customer
reply inside the thread continues the same Ada conversation.

## Operating rules (do not violate)

- **Never put secrets in code or git.** Use the hosting platform's secret/env
  store. `.env` is git-ignored and is for local dev only.
- **Confirm before each outward/irreversible action** — creating cloud resources,
  registering a webhook, reinstalling the Slack app. Show the developer what
  you're about to do.
- **Ask, don't invent.** Every credential and ID below must come from the
  developer or a console you walk them through. Never fabricate IDs, tokens, or
  URLs.
- The server **fails closed** without a Slack signing secret — that is intended;
  do not "work around" it by disabling the check.
- **Text messages only** are relayed. Images, files, rich text, and reactions are
  ignored by design — tell the developer this is a known limitation, not a bug.
- **One running instance serves exactly one Slack channel** (`SLACK_CHANNEL_ID`).
  More VIP channels → run more instances (each with its own `.env` and URL).
- If the live Salesforce↔Ada handoff isn't confirmed working yet, don't block the
  demo on it — point the developer at `demo/fake_agent_server.py` (see README
  "Demo mode") to exercise the agent-reply half of the flow without it.

## Inputs to collect up front (ask the developer explicitly)

| Input | Where it comes from |
|---|---|
| `SLACK_SIGNING_SECRET` | the Slack app's Basic Information → App Credentials (create the app if it doesn't exist — Step 1) |
| `SLACK_BOT_TOKEN` | the app's OAuth & Permissions page, after granting scopes and installing (Step 1) |
| `SLACK_CHANNEL_ID` | the target channel's id (Step 1) |
| `CHANNEL_JOIN_LINK` | the channel's URL (Step 1) |
| `ADA_BASE_URL` | `https://<instance>.ada.support` |
| `ADA_API_KEY` | Ada → Config → Platform → API keys (needs `webhooks:write`) |
| `ADA_CHANNEL_ID` | the Ada custom channel these conversations use (Step 2) |
| Cloud provider preference | Step 3 |

---

## Step 1 — Slack app + channel

1. **App exists?** If your organization already has the "Ada VIP Handoff" app, grab its Signing
   Secret and Bot Token. If not, create one at
   [api.slack.com/apps](https://api.slack.com/apps) → Create New App → From
   scratch, in the target workspace.
2. **Scopes:** grant Bot Token Scopes `chat:write`, `channels:history` (or
   `groups:history` for a private channel), `channels:read`/`groups:read`. Then
   **Install to Workspace** (needs a workspace admin to approve) to get the Bot
   User OAuth Token (`xoxb-…`).
3. **Invite the bot to the channel** — see `CUSTOMER_SLACK_BOT_SETUP.md`.
4. **Get `SLACK_CHANNEL_ID`** (`C…`/`G…`): easiest via the channel's "View channel
   details" panel, or `GET /open-apis/conversations.list` with the bot token.
5. **`CHANNEL_JOIN_LINK`** is just `https://<workspace>.slack.com/archives/<SLACK_CHANNEL_ID>`.

## Step 2 — Ada side (may require an Ada admin, not just the Slack dev)

1. **`ADA_CHANNEL_ID`:** confirm the custom channel conversations are created on
   (Config → Channels, or ask the Ada admin). Conversations post to this channel.
   Use a channel dedicated to this Slack integration — don't reuse the Lark one.
2. **VIP handoff trigger:** the Salesforce handoff on the Ada instance is keyed to
   the metadata key `profile.metadata.is_lark_vip_handoff == true`, regardless of
   platform name. `webhook_server.py` sets both `is_lark_vip_handoff` and
   `is_slack_vip_handoff` on every new end user for this reason — no action needed.
   If a conversation is created but nothing hands off, confirm with the Ada admin
   that this is still the flag wired on the target instance before assuming the
   relay is broken.
3. **Salesforce handoff:** a successful handoff populates
   `agent_system_salesforce_conversation_id` on the conversation. If no live agent
   is watching the queue yet (e.g. during a demo), use
   `demo/fake_agent_server.py` to inject `human_agent` replies directly instead of
   waiting on one.

## Step 3 — Choose a private cloud host

Goal: not publicly discoverable, locked down, secrets in the platform's secret
store, stable HTTPS. Good options (pick one with the developer):

- **Fly.io / Render / Railway** — push from `requirements.txt`, managed TLS + stable
  URL, secrets stored as platform env vars. Fastest. Restrict/don't expose any
  public dashboard beyond the webhook routes.
- **Small cloud VM** (AWS t4g.nano, GCP e2-micro, DO droplet) + **Caddy** (auto
  Let's Encrypt TLS) + **gunicorn**, both under **systemd**. Most control; put it in
  a private subnet and expose only 443.
- **Cloudflare Tunnel + any box** — a *named* tunnel bound to a subdomain, no inbound
  ports opened at all. Strong default for "private."

## Step 4 — Deploy

1. `pip install -r requirements.txt`.
2. Set every value from the Inputs table as **platform secrets/env vars** (not a
   committed `.env`).
3. Run under a production server: `gunicorn -w 2 -b 0.0.0.0:$PORT webhook_server:app`,
   supervised (systemd / the platform's process manager) so it restarts on crash/boot.
4. **Set up logging so issues are diagnosable.** Set `LOG_FILE` to a persistent path
   (e.g. `/var/log/slack-ada/app.log`) so there's a rotating file to zip and send Ada
   if something breaks — stdout is always captured too. Keep `LOG_LEVEL=INFO`; switch
   to `DEBUG` only while chasing a specific issue (it logs full customer message text).
5. Health check: `GET https://<your-url>/health` → returns `{"ok": true, …}` plus a
   state readout (configured channel, which secrets are set, mapped-conversation
   count). One curl confirms the instance is up and correctly wired.

## Step 5 — Register the webhooks (to the stable URL)

- **Slack inbound:** app config → Event Subscriptions → turn on → Request URL =
  `https://<url>/slack/events`. Save; the running server auto-answers the
  verification challenge. **The server must be deployed and reachable before you
  save this URL.** Then add the bot event `message.channels`/`message.groups` and
  **reinstall the app** to the workspace so it takes effect.
- **Ada outbound:** create the delivery endpoint via API, then read its secret:
  ```bash
  curl -X POST "$ADA_BASE_URL/api/v2/webhooks/" \
    -H "Authorization: Bearer $ADA_API_KEY" -H "Content-Type: application/json" \
    -d '{"url":"https://<url>/ada/webhook","event_filters":["v1.conversation.message"],"enabled":true,"description":"Slack VIP handoff"}'
  curl "$ADA_BASE_URL/api/v2/webhooks/<ep_id>/secret/" -H "Authorization: Bearer $ADA_API_KEY"
  ```
  Put the returned `whsec_…` into the platform secret store as `ADA_WEBHOOK_SECRET`
  and restart the service.

## Step 6 — Lock it down

- If the host firewalls inbound, allowlist Svix's delivery IPs (Ada's docs →
  Webhooks → IP allowlist) so Ada webhooks aren't blocked.
- Expose only `/slack/events`, `/ada/webhook`, `/health`. Nothing else is served.
- If any secret was ever pasted into a terminal/log, rotate it (Slack: regenerate
  in App Credentials / revoke+reinstall for the bot token; Ada:
  `POST /api/v2/webhooks/<id>/secret/rotate/`).

## Step 7 — Verify end-to-end

1. Post a **new top-level message** in the Slack channel → within seconds a thread
   opens with Ada's greeting. (Server log: `slack->ada NEW conv=…`,
   `ada->slack RELAYED role=ai_agent …`.)
2. Have an agent reply (via Salesforce/Ada, or `demo/fake_agent_server.py` if
   Salesforce isn't wired yet) → it appears in that thread
   (`ada->slack RELAYED role=human_agent …`).
3. Reply **inside the thread** as the customer → it continues the *same* Ada
   conversation, not a new one (`slack->ada CONTINUE conv=…`).
4. Confirm no echo/loop and no duplicate posts.

If a step doesn't work, the logs say why: every drop is logged with a reason
(`slack.skip reason=…`, `ada->slack NO THREAD mapped …`), auth rejects log at WARNING,
and API failures log at ERROR with the HTTP status + body. `grep` the Ada conversation
id to trace one message across both directions.

Also run the unit tests before trusting a deploy: `pip install -r requirements-dev.txt && pytest`.

Report the deployed URL, the two registered webhook targets, and the verification
results back to the developer.
