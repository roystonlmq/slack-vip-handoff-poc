# Setup Guide — Slack ↔ Ada VIP Handoff

This is the single guide to get the Ada VIP Handoff app live in a Slack channel. Follow it top to bottom. Deeper technical detail for each step lives in `README.md` — this guide links to the relevant section instead of repeating it.

## End state

Once this is set up:

1. A customer (or internal VIP requester) sends a message in a designated Slack channel.
2. The Ada app in that channel automatically opens a thread and replies — the same Ada AI Agent you already use on other channels.
3. If the conversation needs a live agent, Ada hands it off through your existing Salesforce process, and the agent's replies appear in the same Slack thread.
4. The customer replies inside that thread to continue the conversation — nothing changes channels or gets lost.

One deployment covers one Slack channel. If you want this on more than one channel, repeat the deployment steps for each (see `README.md` → Architecture).

## Who needs to be involved

| Role | Does what |
|---|---|
| **Slack workspace admin** | Approves installing the app to the workspace (Step 1) |
| **A developer** (or point an AI coding assistant at this repo) | Deploys and hosts the server (Step 3), registers the webhooks (Step 4) |
| **Ada admin / your Ada contact** | Confirms the Ada channel and API key (Step 2), confirms the VIP handoff routing is active on your instance |

One person can hold more than one of these roles.

## What's being provided

This repository is meant to run in **your own environment**, with your own credentials. Everything needed is in it:

| File | Purpose |
|---|---|
| `webhook_server.py` | The service itself — do not need to modify it for a standard setup |
| `.env.example` | Every setting the service needs, with a comment on where each comes from |
| `requirements.txt` / `requirements-dev.txt` | Python dependencies (runtime / test) |
| `slack-app-manifest.json` | Paste this into Slack's "Create app from manifest" to skip manual scope entry (Step 1) |
| `tests/` | Automated tests — run these after any change or before trusting a new deployment |
| `demo/fake_agent_server.py` | Lets you demo the full round trip, including an agent's reply, before your live Salesforce agent queue is confirmed wired to this channel |
| `README.md` | Full technical reference (architecture, security, logging, troubleshooting) |
| `AGENTS.md` | A ready-to-use deployment runbook — if your developer uses an AI coding assistant, they can point it at this file directly and it will drive Steps 1–5 with them, step by step, collecting each required value along the way |

Ask your Ada contact for access to the repository (share the GitHub username(s) that need it).

## Before you start — confirm with your Ada contact

- **The VIP handoff routing itself must be active on your Ada instance.** This integration relays messages between Slack and Ada; the actual "hand this off to a live agent via Salesforce" logic is a separate piece of Ada configuration that has to exist on your instance already, or be set up alongside this. Confirm this with your Ada contact before deploying — otherwise conversations will reach Ada and get an AI reply, but won't hand off anywhere.
- **Use a Slack channel dedicated to this integration**, not a general team channel — every message posted there is treated as a new customer conversation.
- **Text messages only** are relayed today (no images, files, or reactions) — plan the channel's use around that.

## Step 1 — Create the Slack app

Do this in **your own Slack workspace** (not a test/sandbox workspace).

1. Go to [api.slack.com/apps](https://api.slack.com/apps) → **Create New App** → **From an app manifest**.
2. Select the target workspace, paste in the contents of `slack-app-manifest.json`, and create it. This sets the bot's name and all required scopes in one step (`chat:write`, `channels:history`/`groups:history`, `channels:read`/`groups:read`).
3. Go to **OAuth & Permissions** → **Install to Workspace** (a workspace admin needs to approve this) → copy the **Bot User OAuth Token** (`xoxb-…`).
4. Go to **Basic Information → App Credentials** → copy the **Signing Secret**.
5. Leave **Event Subscriptions** for Step 4 — the server needs to be deployed and reachable first.

Full detail: `README.md` → [How to obtain each value](README.md#how-to-obtain-each-value) (Slack section).

## Step 2 — Set up the Ada side

Your Ada contact can do this, or grant you access to do it directly:

1. **Ada channel**: a custom channel dedicated to this integration (Config → Channels). Don't reuse a channel from another integration.
2. **Ada API key**: Config → Platform → API keys → create one with webhook + conversation permissions.
3. Confirm the VIP handoff routing (see "Before you start" above) is wired to fire on conversations coming from this new channel.

Full detail: `README.md` → [How to obtain each value](README.md#how-to-obtain-each-value) (Ada section).

## Step 3 — Host the server

The server needs to run somewhere always-on, with a stable HTTPS URL. Three options are documented in `README.md` → [Deployment](README.md#deployment), in order of least to most operational effort:

- **Cloudflare Tunnel + any host** — cheapest, no inbound ports to open.
- **Small cloud VM** (systemd + Caddy for automatic TLS) — most control.
- **Managed PaaS** (Render, Railway, Fly.io) — least ops work, push and go.

Whichever you pick, all credentials (from Steps 1–2 plus `PORT`, `LOG_LEVEL`, optionally `LOG_FILE`) go into that platform's secret/environment-variable store — never into a committed file.

## Step 4 — Register the two webhooks

Once the server has a stable public URL:

1. **Slack → server**: in the app's **Event Subscriptions**, turn it on, set the Request URL to `https://<your-url>/slack/events`, add the bot event `message.channels` (or `message.groups` for a private channel), save, then **reinstall the app** to the workspace.
2. **Ada → server**: create a webhook endpoint pointing at `https://<your-url>/ada/webhook`, subscribed to `v1.conversation.message` events, then fetch its signing secret and add it to the server's environment as `ADA_WEBHOOK_SECRET`.

Exact commands: `README.md` → [Registering the webhooks](README.md#registering-the-webhooks).

## Step 5 — Invite the bot and verify

1. Invite the app to the target channel (`/invite @Ada VIP Handoff`, or see `CUSTOMER_SLACK_BOT_SETUP.md` for the full walkthrough, including the private-channel case).
2. Post a new message in the channel → a thread should open with Ada's reply within a few seconds.
3. Reply inside that thread → it should continue the same conversation, not start a new one.
4. If your Salesforce agent queue isn't confirmed wired to this channel yet, use `demo/fake_agent_server.py` to simulate an agent reply landing in the same thread — see `README.md` → [Demo mode](README.md#demo-mode-no-live-salesforce-needed).

That confirms the full loop: customer message → AI reply → handoff → agent reply → customer reply, all in one thread.

## If something doesn't work

`README.md` → [Troubleshooting](README.md#troubleshooting) covers the common cases. Every message the server processes (or drops) is logged with a reason, so the fastest way to diagnose an issue is to grep the logs for the affected Slack timestamp or Ada conversation id — see `README.md` → [Observability & logging](README.md#observability--logging).

If you're stuck, send your Ada contact: the log output for the affected time window, the Slack message timestamp or Ada conversation id, and the `/health` endpoint's output.
