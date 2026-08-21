#!/usr/bin/env python3
"""
Fake CRM server for the Slack VIP handoff POC.

Simulates a human support agent replying in a real CRM: POST /reply with a
conversation_id + message, and this posts a human_agent-authored message
straight into Ada's Conversations API. Relaying that message into Slack is
handled uniformly by webhook_server.py's /ada/webhook endpoint, which watches
for any new ai_agent/human_agent message (whether it came from this script,
Ada's own playbook, or eventually a real CRM sync) and pushes each one into
the Slack thread - so there's a single relay path regardless of where the
reply originated.

This exists so the handoff-reply half of the demo can be exercised even when
the live Salesforce->Ada VIP handoff wiring isn't confirmed working yet - this
stand-in lets that half of the demo run without depending on that integration
being live.

Usage:
    export ADA_API_KEY="<your Ada Conversations API key>"
    export ADA_BASE_URL="https://<instance>.ada.support"
    python3 demo/fake_agent_server.py
    curl -X POST localhost:8765/reply \
      -H "Content-Type: application/json" \
      -d '{"conversation_id": "6a5a3885fa3f9f99674b237c", "message": "Hi, this is Sarah, how can I help?"}'
"""

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

BASE_URL = os.environ["ADA_BASE_URL"]
API_KEY = os.environ["ADA_API_KEY"]
AGENT_DISPLAY_NAME = "Sarah (Support)"
PORT = 8765

HEADERS = {
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type": "application/json",
}


def post_agent_reply(conversation_id, message):
    resp = requests.post(
        f"{BASE_URL}/api/v2/conversations/{conversation_id}/messages/",
        headers=HEADERS,
        json={
            "author": {"role": "human_agent", "display_name": AGENT_DISPLAY_NAME},
            "content": {"type": "text", "body": message},
        },
    )
    resp.raise_for_status()
    return resp.json()


class Handler(BaseHTTPRequestHandler):
    def _send_json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path != "/reply":
            self._send_json(404, {"error": "not found"})
            return

        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b""
        try:
            data = json.loads(raw)
            conversation_id = data["conversation_id"]
            message = data["message"]
        except (json.JSONDecodeError, KeyError) as exc:
            self._send_json(400, {"error": f"bad request: {exc}"})
            return

        try:
            result = post_agent_reply(conversation_id, message)
        except requests.HTTPError as exc:
            self._send_json(502, {"error": str(exc), "body": exc.response.text})
            return

        print(f"[fake-agent] posted to {conversation_id}: {message!r} -> message_id={result.get('id')}")
        self._send_json(201, result)

    def log_message(self, fmt, *args):
        pass  # keep stdout to our own print() lines only


def main():
    server = ThreadingHTTPServer(("localhost", PORT), Handler)
    print(f"Fake agent server listening on http://localhost:{PORT}/reply")
    server.serve_forever()


if __name__ == "__main__":
    main()
