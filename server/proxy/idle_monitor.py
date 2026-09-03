#!/usr/bin/env python3
"""Stops both instances once both Signal activity and Claude Code have been
idle for IDLE_SECONDS. Must run on the proxy - it's the only instance with a
route to the internet, and therefore to the Lambda controller's /stop URL."""
import json
import os
import time
import urllib.error
import urllib.request

RELAY_SECRET = os.environ["RELAY_SECRET"]
RELAY_PORT = os.environ.get("RELAY_PORT", "8443")
AI_PRIVATE_IP = os.environ["AI_PRIVATE_IP"]
BRIDGE_PORT = os.environ.get("BRIDGE_PORT", "7801")
CONTROLLER_URL = os.environ["CONTROLLER_URL"].rstrip("/")
STOP_SECRET = os.environ["STOP_SECRET"]
IDLE_SECONDS = int(os.environ.get("IDLE_SECONDS", str(30 * 60)))
POLL_SECONDS = 60
POST_STOP_COOLDOWN_SECONDS = 10 * 60


def get_json(url, headers=None, timeout=10):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def main():
    while True:
        time.sleep(POLL_SECONDS)
        try:
            bridge_status = get_json(f"http://127.0.0.1:{BRIDGE_PORT}/status")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            # The bridge is local and always started with the proxy - if this fails
            # something's actually wrong, not just "an instance is transitioning".
            # Nothing useful to do but wait for the next tick either way.
            print(f"bridge status check failed (unexpected): {exc}")
            continue

        try:
            ai_status = get_json(
                f"http://{AI_PRIVATE_IP}:{RELAY_PORT}/status",
                headers={"Authorization": f"Bearer {RELAY_SECRET}"},
            )
        except (urllib.error.URLError, TimeoutError, OSError):
            # ai is stopped (or mid-boot) - e.g. a proxy+deploy-only session started
            # via the web_domain/`/web` link, which never touches ai at all. That's
            # not a reason to stay up forever: treat ai as idle rather than skipping
            # the whole check, so bridge_status's own activity still governs.
            ai_status = {"busy": False, "last_activity": 0}

        if ai_status.get("busy"):
            continue

        now = time.time()
        idle_for = now - max(bridge_status.get("last_activity", now), ai_status.get("last_activity", 0))
        if idle_for < IDLE_SECONDS:
            continue

        print(f"idle for {idle_for:.0f}s >= {IDLE_SECONDS}s, calling /stop")
        try:
            req = urllib.request.Request(
                f"{CONTROLLER_URL}/stop", method="GET",
                headers={"X-Stop-Secret": STOP_SECRET},
            )
            urllib.request.urlopen(req, timeout=15)
        except Exception as exc:  # noqa: BLE001
            print(f"failed to call /stop: {exc}")
        time.sleep(POST_STOP_COOLDOWN_SECONDS)


if __name__ == "__main__":
    main()
