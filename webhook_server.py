#!/usr/bin/env python3
"""
Event-driven (webhook) bidirectional Slack <-> Ada VIP handoff.

A single Flask app exposing two webhook endpoints:

  POST /slack/events  <- Slack pushes "message" events (customer messages)
  POST /ada/webhook    <- Ada pushes conversation.message (agent/AI replies)

Routing model (VIP handoff):
  - A brand-new TOP-LEVEL message in the channel               => new end_user
    + new conversation in Ada. Ada's greeting/handoff then flows back out via
    the Ada webhook into a freshly-opened Slack thread on that message.
  - A reply INSIDE an existing thread (thread_ts != ts)         => continues
    that thread's existing Ada conversation.

State (conversation <-> Slack thread-root mapping + event de-dup) lives in a
small SQLite file so redelivered webhooks (both Slack and Svix retry) never
double-post.

All secrets and instance-specific IDs come from environment variables -
nothing is hard-coded. See .env.example.

Run:
    pip install -r requirements.txt
    export $(grep -v '^#' .env | xargs)   # or use python-dotenv
    python3 webhook_server.py             # dev
    gunicorn -w 2 -b 0.0.0.0:8080 webhook_server:app   # prod
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from logging.handlers import RotatingFileHandler

import requests
from flask import Flask, request, jsonify, g

# --------------------------------------------------------------------------
# Config - everything from env, nothing hard-coded.
# --------------------------------------------------------------------------
ADA_BASE = os.environ["ADA_BASE_URL"]                       # e.g. https://<instance>.ada.support
ADA_API_KEY = os.environ["ADA_API_KEY"]
ADA_WEBHOOK_SECRET = os.environ.get("ADA_WEBHOOK_SECRET", "")  # Svix signing secret (whsec_...); /ada/webhook fails closed until set

SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]                # xoxb-...
SLACK_SIGNING_SECRET = os.environ.get("SLACK_SIGNING_SECRET", "")  # /slack/events fails closed until set

ADA_CHANNEL_ID = os.environ["ADA_CHANNEL_ID"]               # Ada custom channel id
SLACK_CHANNEL_ID = os.environ["SLACK_CHANNEL_ID"]           # the Slack channel we serve (C... or G...)
CHANNEL_JOIN_LINK = os.environ.get("CHANNEL_JOIN_LINK", "")

STATE_DB = os.path.join(os.path.dirname(__file__), "webhook_state.db")
PORT = int(os.environ.get("PORT", "8080"))

# Observability. LOG_LEVEL controls verbosity (DEBUG adds full payloads + full
# message text; INFO logs the lifecycle + decisions + errors and truncates
# customer text so we don't dump PII by default). LOG_FILE, if set, ALSO writes
# a rotating file you can zip and send your Ada contact when something breaks -
# stdout is always captured too (systemd/journald, Docker, or the PaaS log stream).
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
LOG_FILE = os.environ.get("LOG_FILE", "")

ADA_HEADERS = {"Authorization": f"Bearer {ADA_API_KEY}", "Content-Type": "application/json"}
SLACK_HEADERS = {"Authorization": f"Bearer {SLACK_BOT_TOKEN}", "Content-Type": "application/json"}


def _setup_logging():
    logger = logging.getLogger("slack_ada")
    logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
    logger.propagate = False
    if logger.handlers:  # avoid duplicate handlers when gunicorn imports the module twice
        return logger
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-5s [%(name)s] %(message)s", "%Y-%m-%dT%H:%M:%S%z"
    )
    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    logger.addHandler(stream)
    if LOG_FILE:
        rot = RotatingFileHandler(LOG_FILE, maxBytes=5_000_000, backupCount=5, encoding="utf-8")
        rot.setFormatter(fmt)
        logger.addHandler(rot)
    return logger


log = _setup_logging()


def _rid():
    """Short per-request id so all log lines from one webhook call group together.
    Cross-direction correlation (inbound vs relay) is via the conversation_id and
    message ids, which every line also carries - grep a conversation to see both."""
    rid = getattr(g, "rid", None)
    if rid is None:
        rid = uuid.uuid4().hex[:8]
        try:
            g.rid = rid
        except RuntimeError:  # outside an app context (e.g. unit tests calling helpers directly)
            pass
    return rid


def _client_ip():
    return request.headers.get("X-Forwarded-For", request.remote_addr or "?").split(",")[0].strip()


def _preview(text, n=80):
    """Truncated text for INFO logs - full text only leaks at DEBUG."""
    text = text or ""
    return text if len(text) <= n else text[:n] + "…"

# Fail closed: the public /slack/events endpoint MUST be able to authenticate
# inbound requests. Without a signing secret, anyone who learns the URL could
# forge Slack events (create Ada conversations, spam, etc.). Slack has exactly
# one signing mechanism (unlike Lark's token-or-encrypt-key choice), so this is
# a hard requirement, not an either/or.
if not SLACK_SIGNING_SECRET:
    raise SystemExit(
        "Refusing to start: set SLACK_SIGNING_SECRET in .env so /slack/events "
        "can verify that requests genuinely come from Slack."
    )

app = Flask(__name__)

# One redacted line at boot so a log always states what this instance is wired to
# (which channel, and whether each secret is present) - never the values.
log.info(
    "startup ada_base=%s channel=%s slack_channel=%s "
    "ada_webhook_secret=%s slack_signing_secret=%s "
    "log_level=%s log_file=%s",
    ADA_BASE, ADA_CHANNEL_ID, SLACK_CHANNEL_ID,
    "set" if ADA_WEBHOOK_SECRET else "MISSING",
    "set" if SLACK_SIGNING_SECRET else "MISSING",
    LOG_LEVEL, LOG_FILE or "(stdout only)",
)

# --------------------------------------------------------------------------
# State store (SQLite). Three concerns:
#   conv_root:  conversation_id -> Slack root message ts   (outbound threading)
#   root_conv:  Slack root message ts -> conversation_id   (inbound continuation)
#   seen:       de-dup key -> 1                              (webhook retries)
# --------------------------------------------------------------------------
_db_lock = threading.Lock()


def _db():
    conn = sqlite3.connect(STATE_DB)
    conn.execute("CREATE TABLE IF NOT EXISTS conv_root (conversation_id TEXT PRIMARY KEY, root_msg TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS root_conv (root_msg TEXT PRIMARY KEY, conversation_id TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS seen (k TEXT PRIMARY KEY, ts INTEGER)")
    return conn


def already_seen(key):
    """Return True if we've processed this event key before; else record it."""
    with _db_lock:
        conn = _db()
        try:
            cur = conn.execute("SELECT 1 FROM seen WHERE k=?", (key,))
            if cur.fetchone():
                return True
            conn.execute("INSERT INTO seen (k, ts) VALUES (?, ?)", (key, int(time.time())))
            conn.commit()
            return False
        finally:
            conn.close()


def map_conversation(conversation_id, root_msg):
    with _db_lock:
        conn = _db()
        try:
            conn.execute("INSERT OR REPLACE INTO conv_root VALUES (?, ?)", (conversation_id, root_msg))
            conn.execute("INSERT OR REPLACE INTO root_conv VALUES (?, ?)", (root_msg, conversation_id))
            conn.commit()
        finally:
            conn.close()


def root_for_conversation(conversation_id):
    with _db_lock:
        conn = _db()
        try:
            row = conn.execute("SELECT root_msg FROM conv_root WHERE conversation_id=?", (conversation_id,)).fetchone()
            return row[0] if row else None
        finally:
            conn.close()


def conversation_for_root(root_msg):
    with _db_lock:
        conn = _db()
        try:
            row = conn.execute("SELECT conversation_id FROM root_conv WHERE root_msg=?", (root_msg,)).fetchone()
            return row[0] if row else None
        finally:
            conn.close()


# --------------------------------------------------------------------------
# Ada API helpers (dynamic - no hard-coded conversation/end-user ids)
# --------------------------------------------------------------------------
def ada_create_end_user(slack_message_ts):
    """Fresh end_user per new conversation, flagged is_slack_vip_handoff so Ada's
    Greeting playbook skips the menu and hands off immediately. No external_id ->
    never deduped to a prior person.

    Also sets is_lark_vip_handoff=true alongside our own flag: the Salesforce
    handoff routing on your Ada instance may be keyed to that Lark-specific
    metadata name from an earlier integration rather than a platform-neutral
    one. Harmless to set both regardless of which (if either) fires."""
    resp = requests.post(
        f"{ADA_BASE}/api/v2/end-users/",
        headers=ADA_HEADERS,
        json={"profile": {"metadata": {
            "is_slack_vip_handoff": True,
            "is_lark_vip_handoff": True,
            "channel_link": CHANNEL_JOIN_LINK,
            "slack_message_ts": slack_message_ts,
        }}},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["end_user_id"]


def ada_create_conversation(end_user_id):
    resp = requests.post(
        f"{ADA_BASE}/api/v2/conversations/",
        headers=ADA_HEADERS,
        json={"channel_id": ADA_CHANNEL_ID, "end_user_id": end_user_id},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["id"]


def ada_post_message(conversation_id, end_user_id, text):
    resp = requests.post(
        f"{ADA_BASE}/api/v2/conversations/{conversation_id}/messages/",
        headers=ADA_HEADERS,
        json={"author": {"id": end_user_id, "role": "end_user"},
              "content": {"type": "text", "body": text}},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def ada_get_end_user_for_conversation(conversation_id):
    resp = requests.get(f"{ADA_BASE}/api/v2/conversations/{conversation_id}", headers=ADA_HEADERS, timeout=15)
    resp.raise_for_status()
    return resp.json().get("end_user_id")


def _is_conversation_expired_error(exc):
    """True if Ada rejected a message because the conversation it targeted has
    already ended/expired (confirmed empirically 18/08/2026: a human_agent
    calling /end/ on a conversation, then the customer replying in the same
    Slack thread, produces exactly this - a 400 with 'Conversation is expired').
    That's the signal to fall back to starting a fresh conversation on the same
    thread rather than silently dropping the customer's message."""
    resp = exc.response
    if resp is None or resp.status_code != 400:
        return False
    try:
        errors = resp.json().get("errors", [])
    except ValueError:
        return False
    return any("expired" in (e.get("message") or "").lower() for e in errors)


# --------------------------------------------------------------------------
# Slack API helpers (bot user id cached; reply-in-thread via chat.postMessage)
# --------------------------------------------------------------------------
_slack_bot_user_id = {"value": None}


def slack_bot_user_id():
    """The bot's own user id (U...), fetched once via auth.test and cached for the
    process lifetime - used by the loop guard to ignore the bot's own relayed
    messages even on workspaces where bot_id alone isn't a reliable enough check."""
    if _slack_bot_user_id["value"] is None:
        resp = requests.post("https://slack.com/api/auth.test", headers=SLACK_HEADERS, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"Slack auth.test failed: {data}")
        _slack_bot_user_id["value"] = data["user_id"]
    return _slack_bot_user_id["value"]


def slack_reply_in_thread(root_ts, text):
    resp = requests.post(
        "https://slack.com/api/chat.postMessage",
        headers=SLACK_HEADERS,
        json={"channel": SLACK_CHANNEL_ID, "thread_ts": root_ts, "text": text},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Slack chat.postMessage failed: {data}")
    return data


# --------------------------------------------------------------------------
# Slack request verification.
# (Confirmed against Slack API "Verifying requests from Slack" docs.)
# --------------------------------------------------------------------------
def _slack_signature_ok(body_bytes):
    """Verify X-Slack-Signature = 'v0=' + HMAC-SHA256(signing_secret, 'v0:{ts}:{body}')
    and reject requests whose timestamp is more than 5 minutes old (replay
    protection). Constant-time compare to avoid timing leaks."""
    ts = request.headers.get("X-Slack-Request-Timestamp", "")
    sig = request.headers.get("X-Slack-Signature", "")
    if not ts or not sig:
        return False
    try:
        if abs(time.time() - int(ts)) > 60 * 5:
            return False
    except ValueError:
        return False
    basestring = f"v0:{ts}:".encode() + body_bytes
    expected = "v0=" + hmac.new(SLACK_SIGNING_SECRET.encode(), basestring, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)


# --------------------------------------------------------------------------
# Ada (Svix) signature verification.
# --------------------------------------------------------------------------
def ada_verify(body_bytes, headers):
    """Prefer the official svix lib; fall back to manual HMAC if unavailable.
    Fails closed if the signing secret hasn't been configured yet."""
    if not ADA_WEBHOOK_SECRET:
        return False
    try:
        from svix.webhooks import Webhook
        Webhook(ADA_WEBHOOK_SECRET).verify(body_bytes, {
            "svix-id": headers.get("svix-id", ""),
            "svix-timestamp": headers.get("svix-timestamp", ""),
            "svix-signature": headers.get("svix-signature", ""),
        })
        return True
    except ImportError:
        import hmac as _hmac
        secret = base64.b64decode(ADA_WEBHOOK_SECRET.split("_", 1)[1])
        signed = f"{headers.get('svix-id','')}.{headers.get('svix-timestamp','')}.".encode() + body_bytes
        expected = base64.b64encode(_hmac.new(secret, signed, hashlib.sha256).digest()).decode()
        for part in headers.get("svix-signature", "").split():
            if "," in part and _hmac.compare_digest(part.split(",", 1)[1], expected):
                return True
        return False
    except Exception:
        return False


# --------------------------------------------------------------------------
# Inbound: Slack -> Ada
# --------------------------------------------------------------------------
@app.route("/slack/events", methods=["POST"])
def slack_events():
    rid = _rid()
    body_bytes = request.get_data()

    # 1. Signature check FIRST, over the raw body.
    if not _slack_signature_ok(body_bytes):
        log.warning("[%s] slack.auth REJECT reason=signature ip=%s", rid, _client_ip())
        return "bad signature", 401

    payload = request.get_json(force=True, silent=True) or {}

    # 2. URL verification handshake (Events API subscription setup).
    if payload.get("type") == "url_verification":
        log.info("[%s] slack.url_verification ok ip=%s", rid, _client_ip())
        return jsonify({"challenge": payload.get("challenge")})

    if payload.get("type") != "event_callback":
        log.debug("[%s] slack.skip reason=type=%s", rid, payload.get("type"))
        return "", 200  # ack anything else

    event = payload.get("event", {})
    event_id = payload.get("event_id")
    if event.get("type") != "message":
        log.debug("[%s] slack.skip reason=event_type=%s", rid, event.get("type"))
        return "", 200

    channel_id = event.get("channel")
    message_ts = event.get("ts")
    thread_ts = event.get("thread_ts")
    bot_id = event.get("bot_id")
    subtype = event.get("subtype")
    user = event.get("user")
    log.debug("[%s] slack.recv ts=%s channel=%s subtype=%s bot_id=%s user=%s",
              rid, message_ts, channel_id, subtype, bot_id, user)

    # Only serve our target channel.
    if channel_id != SLACK_CHANNEL_ID:
        log.info("[%s] slack.skip reason=other_channel channel=%s (serving %s) ts=%s",
                 rid, channel_id, SLACK_CHANNEL_ID, message_ts)
        return "", 200

    # Only genuine plain-text customer messages - edits/deletes/joins/etc. carry
    # a subtype and are not real new messages.
    if subtype:
        log.info("[%s] slack.skip reason=subtype=%s ts=%s", rid, subtype, message_ts)
        return "", 200

    # Only genuine customer messages. The bot relays agent replies back into this
    # same channel as itself - ignore those (bot_id set, or user == our own bot
    # user id), or they'd be fed straight back into Ada as fake customer messages
    # (echo / relay loop).
    try:
        own_bot_user_id = slack_bot_user_id()
    except Exception:
        log.exception("[%s] slack.auth_test failed - cannot confirm own bot identity", rid)
        own_bot_user_id = None
    if bot_id or (own_bot_user_id and user == own_bot_user_id):
        log.info("[%s] slack.skip reason=own_bot ts=%s (relay-loop guard)", rid, message_ts)
        return "", 200

    # De-dup on Slack event id (Slack retries on non-2xx / slow ack).
    if event_id and already_seen(f"slack:{event_id}"):
        log.info("[%s] slack.skip reason=dedup ts=%s event_id=%s", rid, message_ts, event_id)
        return "", 200

    text = event.get("text", "")
    if not text:
        log.info("[%s] slack.skip reason=empty_text ts=%s", rid, message_ts)
        return "", 200

    # Thread reply vs new top-level message. A thread reply carries a thread_ts
    # that differs from its own ts; a fresh top-level message has no thread_ts
    # (or thread_ts == ts, which Slack sends for the first message of a thread).
    is_reply = bool(thread_ts) and thread_ts != message_ts
    root_ts = thread_ts if is_reply else message_ts

    def _start_new_conversation():
        # Fresh end_user + conversation, mapped onto this thread's root ts.
        end_user_id = ada_create_end_user(message_ts)
        conversation_id = ada_create_conversation(end_user_id)
        map_conversation(conversation_id, root_ts)
        ada_post_message(conversation_id, end_user_id, text)
        return conversation_id, end_user_id

    try:
        if is_reply and conversation_for_root(root_ts):
            # Continue the existing thread's conversation.
            conversation_id = conversation_for_root(root_ts)
            try:
                end_user_id = ada_get_end_user_for_conversation(conversation_id)
                ada_post_message(conversation_id, end_user_id, text)
                log.info("[%s] slack->ada CONTINUE conv=%s root=%s ts=%s text=%r",
                         rid, conversation_id, root_ts, message_ts, _preview(text))
            except requests.HTTPError as exc:
                if not _is_conversation_expired_error(exc):
                    raise
                # The mapped conversation has ended/expired - rather than drop the
                # customer's message, start a fresh conversation on the same
                # thread so they never have to notice or re-send.
                log.info("[%s] slack->ada EXPIRED conv=%s root=%s - starting fresh",
                         rid, conversation_id, root_ts)
                conversation_id, end_user_id = _start_new_conversation()
                log.info("[%s] slack->ada NEW (post-expiry) conv=%s end_user=%s root=%s ts=%s text=%r",
                         rid, conversation_id, end_user_id, root_ts, message_ts, _preview(text))
        else:
            # New top-level message => fresh end_user + conversation.
            conversation_id, end_user_id = _start_new_conversation()
            log.info("[%s] slack->ada NEW conv=%s end_user=%s ts=%s text=%r",
                     rid, conversation_id, end_user_id, message_ts, _preview(text))
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "?"
        body = exc.response.text if exc.response is not None else ""
        log.error("[%s] slack->ada Ada API FAILED ts=%s status=%s body=%s",
                  rid, message_ts, status, body)
    except Exception:
        log.exception("[%s] slack->ada unexpected error ts=%s", rid, message_ts)

    return "", 200


# --------------------------------------------------------------------------
# Outbound: Ada -> Slack
# --------------------------------------------------------------------------
@app.route("/ada/webhook", methods=["POST"])
def ada_webhook():
    rid = _rid()
    body_bytes = request.get_data()
    if not ada_verify(body_bytes, {k.lower(): v for k, v in request.headers.items()}):
        # Distinguish "not configured yet" (a deploy step is missing) from a genuine
        # forged/bad-signature request - both 401, but the logs must tell them apart.
        reason = "secret_missing" if not ADA_WEBHOOK_SECRET else "signature"
        log.warning("[%s] ada.auth REJECT reason=%s ip=%s", rid, reason, _client_ip())
        return "bad signature", 401

    payload = request.get_json(force=True, silent=True) or {}
    if payload.get("type") != "v1.conversation.message":
        log.debug("[%s] ada.skip reason=event_type=%s", rid, payload.get("type"))
        return "", 200

    data = payload.get("data", {})
    message_id = data.get("message_id")
    conversation_id = data.get("conversation_id")
    author = data.get("author", {}) or {}
    role = author.get("role")
    log.debug("[%s] ada.recv conv=%s msg=%s role=%s", rid, conversation_id, message_id, role)

    if already_seen(f"ada:{message_id}"):
        log.info("[%s] ada.skip reason=dedup conv=%s msg=%s", rid, conversation_id, message_id)
        return "", 200

    # Only relay agent-side text messages (end_user came FROM Slack already).
    if role not in ("ai_agent", "human_agent"):
        log.info("[%s] ada.skip reason=not_agent role=%s conv=%s msg=%s",
                 rid, role, conversation_id, message_id)
        return "", 200
    content = data.get("content", {}) or {}
    if content.get("type") != "text":
        log.info("[%s] ada.skip reason=non_text type=%s conv=%s msg=%s",
                 rid, content.get("type"), conversation_id, message_id)
        return "", 200
    text = content.get("body", "")
    if not text:
        log.info("[%s] ada.skip reason=empty_text conv=%s msg=%s", rid, conversation_id, message_id)
        return "", 200

    root_ts = root_for_conversation(conversation_id)
    if not root_ts:
        # This is the classic "agent replied but nothing showed in Slack" case: the
        # conversation was never opened from a Slack message we're tracking.
        log.warning("[%s] ada->slack NO THREAD mapped conv=%s msg=%s role=%s; dropping",
                    rid, conversation_id, message_id, role)
        return "", 200

    try:
        slack_reply_in_thread(root_ts, text)
        log.info("[%s] ada->slack RELAYED role=%s conv=%s thread=%s msg=%s text=%r",
                 rid, role, conversation_id, root_ts, message_id, _preview(text))
    except (requests.HTTPError, RuntimeError) as exc:
        status = getattr(getattr(exc, "response", None), "status_code", "?")
        body = getattr(getattr(exc, "response", None), "text", str(exc))
        log.error("[%s] ada->slack Slack API FAILED conv=%s msg=%s thread=%s status=%s body=%s",
                  rid, conversation_id, message_id, root_ts, status, body)
    except Exception:
        log.exception("[%s] ada->slack unexpected error conv=%s msg=%s", rid, conversation_id, message_id)

    return "", 200


def _state_counts():
    with _db_lock:
        conn = _db()
        try:
            convs = conn.execute("SELECT COUNT(*) FROM conv_root").fetchone()[0]
            seen = conn.execute("SELECT COUNT(*) FROM seen").fetchone()[0]
            return {"mapped_conversations": convs, "seen_events": seen}
        finally:
            conn.close()


@app.route("/health", methods=["GET"])
def health():
    """Liveness + a quick state readout, so a single curl tells us whether the
    instance is up, correctly configured, and actually holding conversation state."""
    counts = _state_counts()
    return jsonify({
        "ok": True,
        "channel_id": SLACK_CHANNEL_ID,
        "ada_channel_id": ADA_CHANNEL_ID,
        "ada_webhook_secret": bool(ADA_WEBHOOK_SECRET),
        "slack_signing_secret": bool(SLACK_SIGNING_SECRET),
        **counts,
    })


if __name__ == "__main__":
    log.info("listening on :%s  (/slack/events, /ada/webhook, /health)", PORT)
    app.run(host="0.0.0.0", port=PORT)
