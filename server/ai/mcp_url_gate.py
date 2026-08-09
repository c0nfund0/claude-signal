#!/usr/bin/env python3
"""Minimal hand-rolled MCP stdio server exposing one tool: request_url_access.

Implements just enough of MCP (initialize, tools/list, tools/call) over
line-delimited JSON-RPC 2.0 on stdin/stdout for Claude Code's stdio
transport - no SDK dependency, so nothing needs installing on the ai
instance beyond Python itself.

On tools/call, POSTs to the proxy's approval_daemon PUBLIC endpoint
(POST /request-url-access) - which can only create a pending request, never
approve one - then polls GET /request-url-access/<id> until the user
approves/denies over Signal, or a 10-minute timeout.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

PROXY_PRIVATE_IP = os.environ["PROXY_PRIVATE_IP"]
RELAY_PORT = os.environ.get("RELAY_PORT", "8443")
RELAY_SECRET = os.environ["RELAY_SECRET"]
BASE_URL = f"http://{PROXY_PRIVATE_IP}:{RELAY_PORT}"
POLL_INTERVAL_SECONDS = 5
TIMEOUT_SECONDS = 600


def _call(method, path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE_URL + path, data=data, method=method,
        headers={"Authorization": f"Bearer {RELAY_SECRET}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def request_url_access(url):
    try:
        resp = _call("POST", "/request-url-access", {"url": url})
    except urllib.error.URLError as exc:
        return f"Could not reach the approval service: {exc}"
    req_id = resp["id"]
    deadline = time.time() + TIMEOUT_SECONDS
    while time.time() < deadline:
        time.sleep(POLL_INTERVAL_SECONDS)
        try:
            status = _call("GET", f"/request-url-access/{req_id}")["status"]
        except urllib.error.URLError:
            continue
        if status == "approved":
            return f"Approved. {url} is now allowed through the proxy - retry your request."
        if status == "denied":
            return f"Denied by the user. {url} was not approved."
    return f"Timed out after {TIMEOUT_SECONDS}s waiting for approval of {url}."


TOOLS = [{
    "name": "request_url_access",
    "description": (
        "Ask the user, over Signal, for permission to reach a new URL/domain through the "
        "proxy. Blocks until they reply yes or no, or times out after 10 minutes. Call this "
        "BEFORE retrying any request that was blocked by the proxy (a blocked request looks "
        "like a connection failure/403 to an unfamiliar domain)."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {"url": {"type": "string", "description": "The full URL you need access to"}},
        "required": ["url"],
    },
}]


def handle(msg):
    method = msg.get("method")
    msg_id = msg.get("id")

    if method == "initialize":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {
            "protocolVersion": msg.get("params", {}).get("protocolVersion", "2024-11-05"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "claude-signal-url-gate", "version": "0.1.0"},
        }}
    if method == "notifications/initialized":
        return None
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = msg.get("params", {})
        if params.get("name") == "request_url_access":
            url = params.get("arguments", {}).get("url", "")
            try:
                text = request_url_access(url)
            except Exception as exc:  # noqa: BLE001
                text = f"Error: {exc}"
            return {"jsonrpc": "2.0", "id": msg_id, "result": {"content": [{"type": "text", "text": text}]}}
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": "unknown tool"}}
    if msg_id is not None:
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": "unknown method"}}
    return None


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        resp = handle(msg)
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
