#!/usr/bin/env python3
"""Stops ai and deploy independently once each goes idle, and proxy once
neither is doing anything for it anymore:

- ai: idle once Signal and Claude Code have both been idle for IDLE_SECONDS.
  Calls the Lambda's /ai/stop, which stops only ai.
- deploy: idle once the deployed app's nginx access log shows no requests
  for WEB_IDLE_SECONDS (no Claude Code activity to measure against here, so
  request traffic is the signal instead). Calls /web/stop, which stops only
  deploy.
- If both are idle (or not running) at once, calls /stop instead, which also
  stops proxy - there's nothing left for it to serve at that point.

This lets a mixed session (both ai and deploy running, e.g. Claude Code
deployed something and the user is now also browsing it) wind down each side
on its own schedule instead of an active deploy session being killed by ai's
inactivity or vice versa.

Must run on the proxy - it's the only instance with a route to the internet,
and therefore to the Lambda controller's URL.
"""
import datetime
import json
import os
import subprocess
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
WEB_IDLE_SECONDS = int(os.environ.get("WEB_IDLE_SECONDS", str(30 * 60)))
WEB_GATE_SCRIPT = os.environ.get("WEB_GATE_SCRIPT", "/usr/local/sbin/claude-signal-web-gate")
# The no-PII access log for the deployed app - see claude_signal_no_pii in
# ansible/roles/proxy/templates/claude-signal-nolog-format.nginx.j2. Its
# $time_local is the only per-request signal available (no IP/User-Agent,
# deliberately), so it doubles as the "is anyone using the deployed app"
# check here.
WEB_LOG_PATH = "/var/log/nginx/claude-signal-web.log"
POLL_SECONDS = 60
POST_STOP_COOLDOWN_SECONDS = 10 * 60


def get_json(url, headers=None, timeout=10):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _tail_line(path, chunk=4096):
    """Last non-empty line of a file, without reading the whole thing."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - chunk))
            data = f.read()
    except OSError:
        return None
    lines = [line for line in data.split(b"\n") if line.strip()]
    return lines[-1].decode(errors="replace") if lines else None


def _last_web_activity():
    """Timestamp of the most recent request to the deployed app, parsed out
    of claude-signal-web.log's $time_local. Falls back to the previous day's
    rotated file if the current one is still empty (e.g. right after
    logrotate's daily rotation - confirmed happening on this log too, via
    `ls -la /var/log/nginx` on the live proxy). Using the *file's own mtime*
    instead would make every rotation look like fresh traffic and silently
    reset the idle clock during a genuinely idle stretch.

    Returns None if neither file has a parseable line (nothing deployed yet,
    or nothing served yet) - callers treat that as "not enough signal to
    stop", not as "idle", so a session that was just started doesn't get
    stopped before its first request has even landed.
    """
    for path in (WEB_LOG_PATH, WEB_LOG_PATH + ".1"):
        line = _tail_line(path)
        if not line:
            continue
        parts = line.split(" ", 2)
        if len(parts) < 2:
            continue
        try:
            when = datetime.datetime.strptime(f"{parts[0]} {parts[1]}", "%d/%b/%Y:%H:%M:%S %z")
        except ValueError:
            continue
        return when.timestamp()
    return None


def _close_web_gate():
    # Otherwise web_gate_state.conf keeps saying "open" from this session even once
    # deploy is gone - nginx would just 502/hang on it instead of a clean "closed"
    # 503, and the next real re-open would wrongly look like "already open" to
    # approval_daemon's _gate_already_open() and skip its Signal notification.
    subprocess.run(["sudo", WEB_GATE_SCRIPT, "closed"], capture_output=True, text=True, check=False)


def _call(path):
    print(f"calling {path}")
    try:
        req = urllib.request.Request(
            f"{CONTROLLER_URL}{path}", method="GET",
            headers={"X-Stop-Secret": STOP_SECRET},
        )
        urllib.request.urlopen(req, timeout=15)
    except Exception as exc:  # noqa: BLE001
        print(f"failed to call {path}: {exc}")
    time.sleep(POST_STOP_COOLDOWN_SECONDS)


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
            controller_status = get_json(f"{CONTROLLER_URL}/status")
        except Exception as exc:  # noqa: BLE001
            print(f"controller status check failed: {exc}")
            continue

        ai_running = controller_status.get("ai", {}).get("state") == "running"
        deploy_running = controller_status.get("deploy", {}).get("state") == "running"

        ai_stop_due = False
        if ai_running:
            try:
                ai_status = get_json(
                    f"http://{AI_PRIVATE_IP}:{RELAY_PORT}/status",
                    headers={"Authorization": f"Bearer {RELAY_SECRET}"},
                )
            except (urllib.error.URLError, TimeoutError, OSError):
                # ai's EC2 state is "running" but its relay didn't answer - a real,
                # transient problem rather than "ai just isn't part of this session".
                # Treat it as idle rather than skipping the check, so bridge_status's
                # own activity still governs and this self-heals instead of running
                # forever on a stuck ai.
                ai_status = {"busy": False, "last_activity": 0}
            if not ai_status.get("busy"):
                now = time.time()
                ai_idle_for = now - max(bridge_status.get("last_activity", now), ai_status.get("last_activity", 0))
                ai_stop_due = ai_idle_for >= IDLE_SECONDS
                if ai_stop_due:
                    print(f"ai idle for {ai_idle_for:.0f}s >= {IDLE_SECONDS}s")

        deploy_stop_due = False
        if deploy_running:
            last_web_activity = _last_web_activity()
            if last_web_activity is not None:
                web_idle_for = time.time() - last_web_activity
                deploy_stop_due = web_idle_for >= WEB_IDLE_SECONDS
                if deploy_stop_due:
                    print(f"deploy (web) idle for {web_idle_for:.0f}s >= {WEB_IDLE_SECONDS}s")

        # Both sides done (or never running to begin with, on whichever side just
        # crossed its own threshold) - nothing is left for proxy to serve, so take
        # it down too. Deliberately keyed off the *_stop_due flags rather than just
        # "not running", so a brief window where one side hasn't finished booting
        # yet (EC2 "pending", not yet "running") never looks like "idle" and
        # triggers a stop moments after the user just asked for a start.
        stop_everything = (ai_running and ai_stop_due and (not deploy_running or deploy_stop_due)) or (
            deploy_running and deploy_stop_due and (not ai_running or ai_stop_due)
        )

        if stop_everything:
            if deploy_running:
                _close_web_gate()
            _call("/stop")
        elif ai_stop_due:
            _call("/ai/stop")
        elif deploy_stop_due:
            _close_web_gate()
            _call("/web/stop")


if __name__ == "__main__":
    main()
