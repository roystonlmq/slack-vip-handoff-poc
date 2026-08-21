"""Unit tests for the Slack <-> Ada webhook relay.

Run:
    pip install -r requirements-dev.txt
    pytest

These exercise the routing and security logic through Flask's test client with the
Ada/Slack network calls mocked out - no live instance, no real HTTP. They cover the
behaviours we care about most: signature verification (both reject and a real valid
signature), the URL-verification handshake, top-level => new-conversation vs
thread-reply => continue routing, the relay-loop guard, dedup, outbound relay, and
fail-closed behaviour when the Ada signing secret is missing.
"""
import hashlib
import hmac
import json
import os
import time
from unittest import mock

import requests

# Env MUST be set before importing the server (it reads config at import time and
# refuses to start without a Slack signing secret).
os.environ.setdefault("ADA_API_KEY", "test-key")
os.environ.setdefault("ADA_CHANNEL_ID", "chan-test")
os.environ.setdefault("ADA_WEBHOOK_SECRET", "")          # unset => outbound fails closed
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test")
os.environ.setdefault("SLACK_SIGNING_SECRET", "test-signing-secret")
os.environ.setdefault("SLACK_CHANNEL_ID", "C_TARGET")
os.environ.setdefault("LOG_LEVEL", "CRITICAL")            # keep test output quiet

import pytest  # noqa: E402

import webhook_server as ws  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def fresh_state(tmp_path, monkeypatch):
    """Isolate the SQLite state store per test."""
    monkeypatch.setattr(ws, "STATE_DB", str(tmp_path / "state.db"))
    yield


@pytest.fixture(autouse=True)
def stub_bot_identity(monkeypatch):
    """The loop guard needs the bot's own Slack user id - stub it out so tests
    never make a real call to auth.test."""
    monkeypatch.setattr(ws, "slack_bot_user_id", lambda: "U_BOT")
    yield


@pytest.fixture
def client():
    ws.app.testing = True
    return ws.app.test_client()


# ---------------------------------------------------------------------------
# Signing helpers
# ---------------------------------------------------------------------------
def _sign(body_bytes, timestamp=None):
    ts = str(timestamp if timestamp is not None else int(time.time()))
    basestring = f"v0:{ts}:".encode() + body_bytes
    sig = "v0=" + hmac.new(ws.SLACK_SIGNING_SECRET.encode(), basestring, hashlib.sha256).hexdigest()
    return {"X-Slack-Request-Timestamp": ts, "X-Slack-Signature": sig}


def slack_message_event(text="hi", ts="1000.0001", channel_id="C_TARGET",
                        user="U_customer", thread_ts=None, bot_id=None,
                        subtype=None, event_id="Ev1"):
    event = {"type": "message", "channel": channel_id, "user": user, "text": text, "ts": ts}
    if thread_ts:
        event["thread_ts"] = thread_ts
    if bot_id:
        event["bot_id"] = bot_id
    if subtype:
        event["subtype"] = subtype
    return {"type": "event_callback", "event_id": event_id, "event": event}


def post_slack(client, payload, sign=True, bad_signature=False):
    body = json.dumps(payload).encode()
    headers = {}
    if bad_signature:
        headers = _sign(b"something-else")
    elif sign:
        headers = _sign(body)
    return client.post("/slack/events", data=body, content_type="application/json", headers=headers)


def ada_message(role="ai_agent", text="hello", conversation_id="conv_x",
                message_id="msg_1", content_type="text"):
    return {
        "type": "v1.conversation.message",
        "data": {
            "message_id": message_id,
            "conversation_id": conversation_id,
            "author": {"role": role},
            "content": {"type": content_type, "body": text},
        },
    }


def post_ada(client, payload):
    return client.post("/ada/webhook", data=json.dumps(payload),
                       content_type="application/json")


# ---------------------------------------------------------------------------
# Inbound: Slack -> Ada
# ---------------------------------------------------------------------------
def test_url_verification_echoes_challenge(client):
    r = post_slack(client, {"type": "url_verification", "challenge": "abc123"})
    assert r.status_code == 200
    assert r.get_json()["challenge"] == "abc123"


def test_bad_signature_rejected(client):
    r = post_slack(client, {"type": "url_verification", "challenge": "abc123"}, bad_signature=True)
    assert r.status_code == 401


def test_missing_signature_headers_rejected(client):
    r = post_slack(client, {"type": "url_verification", "challenge": "abc123"}, sign=False)
    assert r.status_code == 401


def test_top_level_message_creates_new_conversation(client, monkeypatch):
    create_eu = mock.Mock(return_value="eu_1")
    create_conv = mock.Mock(return_value="conv_1")
    post_msg = mock.Mock()
    monkeypatch.setattr(ws, "ada_create_end_user", create_eu)
    monkeypatch.setattr(ws, "ada_create_conversation", create_conv)
    monkeypatch.setattr(ws, "ada_post_message", post_msg)

    r = post_slack(client, slack_message_event(text="my card is blocked", ts="1000.0001"))
    assert r.status_code == 200
    create_eu.assert_called_once_with("1000.0001")
    create_conv.assert_called_once_with("eu_1")
    post_msg.assert_called_once_with("conv_1", "eu_1", "my card is blocked")
    # mapping persisted so the agent's reply can thread back
    assert ws.conversation_for_root("1000.0001") == "conv_1"
    assert ws.root_for_conversation("conv_1") == "1000.0001"


def test_thread_reply_continues_existing_conversation(client, monkeypatch):
    ws.map_conversation("conv_1", "1000.0001")  # pretend the thread already exists
    get_eu = mock.Mock(return_value="eu_1")
    post_msg = mock.Mock()
    create_conv = mock.Mock()
    monkeypatch.setattr(ws, "ada_get_end_user_for_conversation", get_eu)
    monkeypatch.setattr(ws, "ada_post_message", post_msg)
    monkeypatch.setattr(ws, "ada_create_conversation", create_conv)

    r = post_slack(client, slack_message_event(text="still blocked", ts="1000.0002",
                                                thread_ts="1000.0001"))
    assert r.status_code == 200
    post_msg.assert_called_once_with("conv_1", "eu_1", "still blocked")
    create_conv.assert_not_called()  # continued, did NOT open a new conversation


def _http_error(status, body):
    resp = requests.Response()
    resp.status_code = status
    resp._content = json.dumps(body).encode()
    return requests.HTTPError(response=resp)


def test_continue_falls_back_to_new_conversation_when_expired(client, monkeypatch):
    """A customer replying in an old thread after the conversation was ended
    should get a fresh conversation transparently, not a dropped message."""
    ws.map_conversation("conv_old", "1000.0001")
    expired = _http_error(400, {"errors": [{"type": "bad_request",
                                             "message": "Error adding end user message to conversation: Conversation is expired"}]})
    monkeypatch.setattr(ws, "ada_get_end_user_for_conversation", mock.Mock(return_value="eu_old"))
    post_msg = mock.Mock(side_effect=[expired, None])
    monkeypatch.setattr(ws, "ada_post_message", post_msg)
    monkeypatch.setattr(ws, "ada_create_end_user", mock.Mock(return_value="eu_new"))
    monkeypatch.setattr(ws, "ada_create_conversation", mock.Mock(return_value="conv_new"))

    r = post_slack(client, slack_message_event(text="wait, one more question", ts="1000.0002",
                                                thread_ts="1000.0001"))
    assert r.status_code == 200
    assert post_msg.call_count == 2
    post_msg.assert_any_call("conv_old", "eu_old", "wait, one more question")
    post_msg.assert_any_call("conv_new", "eu_new", "wait, one more question")
    # thread now points at the fresh conversation, not the expired one
    assert ws.conversation_for_root("1000.0001") == "conv_new"
    assert ws.root_for_conversation("conv_new") == "1000.0001"


def test_continue_non_expired_400_does_not_fallback(client, monkeypatch):
    """A 400 for any other reason must NOT trigger the expired-conversation
    fallback - only the specific 'expired' error should."""
    ws.map_conversation("conv_old", "1000.0001")
    other_400 = _http_error(400, {"errors": [{"type": "bad_request", "message": "Some other validation error"}]})
    monkeypatch.setattr(ws, "ada_get_end_user_for_conversation", mock.Mock(return_value="eu_old"))
    monkeypatch.setattr(ws, "ada_post_message", mock.Mock(side_effect=other_400))
    create_eu_new = mock.Mock()
    monkeypatch.setattr(ws, "ada_create_end_user", create_eu_new)

    r = post_slack(client, slack_message_event(text="hi again", ts="1000.0002", thread_ts="1000.0001"))
    assert r.status_code == 200
    create_eu_new.assert_not_called()  # no fallback attempted


def test_bot_relayed_message_via_bot_id_ignored(client, monkeypatch):
    """A bot_id on the event == our own relayed agent reply. Must not loop back."""
    create_eu = mock.Mock()
    monkeypatch.setattr(ws, "ada_create_end_user", create_eu)
    r = post_slack(client, slack_message_event(bot_id="B123"))
    assert r.status_code == 200
    create_eu.assert_not_called()


def test_bot_relayed_message_via_own_user_id_ignored(client, monkeypatch):
    """No bot_id, but the sender IS our own bot user id - also a loop risk."""
    create_eu = mock.Mock()
    monkeypatch.setattr(ws, "ada_create_end_user", create_eu)
    r = post_slack(client, slack_message_event(user="U_BOT"))
    assert r.status_code == 200
    create_eu.assert_not_called()


def test_message_in_other_channel_ignored(client, monkeypatch):
    create_eu = mock.Mock()
    monkeypatch.setattr(ws, "ada_create_end_user", create_eu)
    r = post_slack(client, slack_message_event(channel_id="C_someone_else"))
    assert r.status_code == 200
    create_eu.assert_not_called()


def test_subtype_message_ignored(client, monkeypatch):
    create_eu = mock.Mock()
    monkeypatch.setattr(ws, "ada_create_end_user", create_eu)
    r = post_slack(client, slack_message_event(subtype="message_changed"))
    assert r.status_code == 200
    create_eu.assert_not_called()


def test_duplicate_event_deduped(client, monkeypatch):
    create_eu = mock.Mock(return_value="eu_1")
    monkeypatch.setattr(ws, "ada_create_end_user", create_eu)
    monkeypatch.setattr(ws, "ada_create_conversation", mock.Mock(return_value="conv_1"))
    monkeypatch.setattr(ws, "ada_post_message", mock.Mock())

    payload = slack_message_event(event_id="Ev_dup")
    assert post_slack(client, payload).status_code == 200
    assert post_slack(client, payload).status_code == 200  # redelivery
    create_eu.assert_called_once()  # only the first delivery created anything


# ---------------------------------------------------------------------------
# Outbound: Ada -> Slack
# ---------------------------------------------------------------------------
def test_agent_message_relayed_to_thread(client, monkeypatch):
    monkeypatch.setattr(ws, "ada_verify", lambda *a, **k: True)
    reply = mock.Mock()
    monkeypatch.setattr(ws, "slack_reply_in_thread", reply)
    ws.map_conversation("conv_x", "1000.0001")

    r = post_ada(client, ada_message(role="human_agent", text="Priya here, how can I help?"))
    assert r.status_code == 200
    reply.assert_called_once_with("1000.0001", "Priya here, how can I help?")


def test_end_user_message_not_relayed(client, monkeypatch):
    monkeypatch.setattr(ws, "ada_verify", lambda *a, **k: True)
    reply = mock.Mock()
    monkeypatch.setattr(ws, "slack_reply_in_thread", reply)
    ws.map_conversation("conv_x", "1000.0001")

    r = post_ada(client, ada_message(role="end_user", text="echo of the customer"))
    assert r.status_code == 200
    reply.assert_not_called()


def test_relay_skipped_when_no_thread_mapping(client, monkeypatch):
    monkeypatch.setattr(ws, "ada_verify", lambda *a, **k: True)
    reply = mock.Mock()
    monkeypatch.setattr(ws, "slack_reply_in_thread", reply)

    r = post_ada(client, ada_message(conversation_id="conv_unknown"))
    assert r.status_code == 200
    reply.assert_not_called()


def test_ada_webhook_fails_closed_without_secret(client):
    """ADA_WEBHOOK_SECRET is unset in the test env -> real ada_verify must reject."""
    assert ws.ADA_WEBHOOK_SECRET == ""
    r = post_ada(client, ada_message())
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------
def test_health_reports_state(client):
    ws.map_conversation("conv_a", "1000.0001")
    r = client.get("/health")
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["channel_id"] == "C_TARGET"
    assert body["mapped_conversations"] == 1
    assert body["slack_signing_secret"] is True
