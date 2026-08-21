# Adding the Ada VIP Handoff app to a Slack channel

This guide is for the customer's Slack workspace admin or the person managing the target channel. It covers adding the "Ada VIP Handoff" app so it can read and post messages in a specific channel, which is what lets the Slack<->Ada handoff integration work.

## What the app needs, and why

The app is a Slack bot. To relay messages between a Slack channel and Ada, it needs to be a **member of that channel** - the same as any human member would need to be, to see and post messages there. Without membership, the bot can authenticate fine but every send/read call against that channel will fail.

## Step 1: Confirm which channel

Identify the exact Slack channel that will carry the VIP customer conversation. This is usually a dedicated channel set up for this integration, not a general team channel.

## Step 2: Install the app to the workspace (if not already done)

This is a one-time step, usually done by whoever set up the integration (see `README.md` / `AGENTS.md`), and needs a **workspace admin's approval**:

1. Open the app's page at [api.slack.com/apps](https://api.slack.com/apps) (or the install link your Ada contact shared).
2. Go to **OAuth & Permissions** and click **Install to Workspace** (or **Reinstall to Workspace** if scopes changed).
3. Review the requested permissions and **Allow**.

## Step 3: Invite the bot to the channel

1. Open the target channel in Slack.
2. Type `/invite @Ada VIP Handoff` in the message box (or use the channel name → **Integrations** → **Add apps**).
3. Confirm the app appears in the channel's member list (channel name → **Members**).

## Step 4: If the channel is private

Private channels need the bot to have the `groups:history` and `groups:read` scopes (in addition to the public-channel scopes) and the Event Subscriptions to include `message.groups` instead of `message.channels`. If you don't see the option to invite the bot, or messages aren't relaying, check with your Ada contact that these were granted - this is configured once when the app is set up, not per-channel.

## Step 5: Verify

1. Confirm "Ada VIP Handoff" appears in the channel's member list.
2. Send a test message in the channel.
3. Confirm the bot can see it (this depends on the integration being live on Ada's side - check with your Ada contact if you're not sure whether the relay is running yet).

## Troubleshooting reference

| Symptom | Likely cause | Fix |
|---|---|---|
| Can't find the app to invite | App hasn't been installed to your workspace yet | Contact your Ada representative - the app needs to be installed first (Step 2) |
| Bot invited, but messages aren't relaying | Integration not yet running on Ada's side, or bot was invited to the wrong channel | Confirm the channel id with your Ada contact |
| Bot can't be invited to a private channel | Missing private-channel scopes (`groups:history`/`groups:read`) on the app | Ask your Ada contact to add those scopes and reinstall the app |
