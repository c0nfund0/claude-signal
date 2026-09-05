#!/usr/bin/env python3
"""Bridges the dedicated-number Signal bot to the rest of claude-signal.

Talks to a persistent `signal-cli daemon --socket` process (a separate systemd
unit, claude-signal-signal-cli-daemon.service) over a JSON-RPC Unix socket, kept
open continuously via one dedicated reader thread. This replaced an earlier
subprocess-per-poll design (`signal-cli receive -t 10` spawned fresh every ~10s)
that measured 10-18s of connection/JVM-startup overhead PER CYCLE - the daemon
keeps one JVM and one Signal-server connection alive, and pushes new messages
as JSON-RPC notifications within well under a second of arrival.

The allowed sender is identified by Signal *username* (e.g. "yourusername.42"),
resolved once at startup to their stable account UUID via the `getUserStatus`
RPC method - incoming messages are matched against that UUID
(envelope.sourceUuid), not a phone number. Everyone else is dropped.

Recognized commands (yes/no/block/allow/list/status) are routed to
approval_daemon's *local-only* admin API (127.0.0.1:$LOCAL_ADMIN_PORT) or the ai
instance's claude_wrapper - that admin API is not reachable from the ai
instance, by design: the ai instance can only ever *ask* for access via
/request-url-access, never grant it. Anything else is treated as a chat prompt
and forwarded (in its own thread, so a slow reply never blocks the reader
thread from seeing an incoming yes/no) to the ai instance's claude_wrapper.

A voice-note attachment instead of text is handled the same way, plus STT/TTS
either side of the relay - see "Voice messages" below and in the README. That
STT/TTS work happens in voice_pipeline.py, run as a subprocess in its own
virtualenv (real dependencies: faster-whisper, edge-tts) so this file - and
every other proxy daemon - stays pure-stdlib.

Also runs a tiny local HTTP server (127.0.0.1:$BRIDGE_PORT) exposing POST
/send, used by approval_daemon to ask us to message the user, and GET /status
for idle_monitor.
"""
import http.server
import itertools
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

# BOT_NUMBER is not read here - the daemon service (a separate unit) is the one
# started with `-a $BOT_NUMBER daemon --socket ...`; this client just connects
# to its socket, which is already bound to that one account.
ALLOWED_SENDER_USERNAME = os.environ["ALLOWED_SENDER_USERNAME"]
RELAY_SECRET = os.environ["RELAY_SECRET"]
RELAY_PORT = os.environ.get("RELAY_PORT", "8443")
AI_PRIVATE_IP = os.environ["AI_PRIVATE_IP"]
CONTROLLER_URL = os.environ["CONTROLLER_URL"]
# Both empty by default - the CS2 integration is opt-in (see README's "CS2 game
# server integration" section). CS2_SSH_HOSTNAME is the synthetic /etc/hosts name
# (see the proxy role's "Add /etc/hosts entry for the CS2 server" task) that Squid's
# domain allowlist gates SSH access to, exactly like any other domain.
CS2_CONTROLLER_URL = os.environ.get("CS2_CONTROLLER_URL", "")
CS2_SSH_HOSTNAME = os.environ.get("CS2_SSH_HOSTNAME", "")
BRIDGE_PORT = int(os.environ.get("BRIDGE_PORT", "7801"))
LOCAL_ADMIN_PORT = os.environ.get("LOCAL_ADMIN_PORT", "7802")
SIGNAL_CLI_SOCKET = os.environ.get("SIGNAL_CLI_SOCKET", "/run/claude-signal/signal-cli.sock")
# Where signal-cli writes downloaded attachments, named by their "id" field -
# default matches its own default data dir ($HOME/.local/share/signal-cli) for
# the claude-signal user (home=/opt/claude-signal, see the common role).
SIGNAL_CLI_ATTACHMENTS_DIR = os.environ.get(
    "SIGNAL_CLI_ATTACHMENTS_DIR", "/opt/claude-signal/.local/share/signal-cli/attachments",
)
VOICE_VENV_PYTHON = os.environ.get("VOICE_VENV_PYTHON", "/opt/claude-signal/voice-venv/bin/python3")
VOICE_PIPELINE_SCRIPT = os.environ.get("VOICE_PIPELINE_SCRIPT", "/opt/claude-signal/voice_pipeline.py")
VOICE_STT_MODEL = os.environ.get("VOICE_STT_MODEL", "base")
VOICE_TIMEOUT_SECONDS = int(os.environ.get("VOICE_TIMEOUT_SECONDS", "120"))

ADMIN_BASE = f"http://127.0.0.1:{LOCAL_ADMIN_PORT}"
AI_BASE = f"http://{AI_PRIVATE_IP}:{RELAY_PORT}"

last_activity = time.time()
last_activity_lock = threading.Lock()


def touch():
    global last_activity
    with last_activity_lock:
        last_activity = time.time()


class SignalRpcClient:
    """Persistent JSON-RPC connection to `signal-cli daemon --socket`. One
    dedicated reader thread dispatches every incoming line: a 'receive'
    notification goes to on_message; a response to one of OUR requests
    (matched by id) unblocks whoever called .call() and is waiting on it.
    Reconnects with a backoff if the daemon restarts or the socket drops."""

    def __init__(self, path, on_message):
        self.path = path
        self.on_message = on_message
        self._file = None
        self._write_lock = threading.Lock()
        self._pending = {}
        self._pending_lock = threading.Lock()
        self._id_counter = itertools.count(1)
        self._connected = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while True:
            try:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.connect(self.path)
                self._file = sock.makefile("rw")
                self._connected.set()
                print("connected to signal-cli daemon socket", file=sys.stderr)
                while True:
                    line = self._file.readline()
                    if not line:
                        raise ConnectionError("daemon socket closed")
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if obj.get("method") == "receive":
                        try:
                            self.on_message(obj.get("params", {}).get("envelope", {}))
                        except Exception as exc:  # noqa: BLE001
                            print(f"on_message error: {exc}", file=sys.stderr)
                    elif "id" in obj:
                        with self._pending_lock:
                            slot = self._pending.pop(obj["id"], None)
                        if slot:
                            slot["result"] = obj
                            slot["event"].set()
            except Exception as exc:  # noqa: BLE001
                self._connected.clear()
                print(f"signal-cli daemon connection lost ({exc}), reconnecting in 3s", file=sys.stderr)
                # Fail every in-flight call immediately rather than leaving it to
                # wait out its own timeout (up to 30s) even though the connection
                # that would have answered it is already gone - confirmed live:
                # this is what produced "no response to send within 30s" right
                # after a daemon/bridge restart, when the actual delay was under a
                # second. A reconnect a moment later doesn't revive a request that
                # was in flight on the old, now-dead connection.
                with self._pending_lock:
                    stale = list(self._pending.items())
                    self._pending.clear()
                for _req_id, slot in stale:
                    slot["result"] = {"error": f"connection lost: {exc}"}
                    slot["event"].set()
                time.sleep(3)

    def call(self, method, params, timeout=30):
        self._connected.wait(30)
        req_id = next(self._id_counter)
        slot = {"event": threading.Event(), "result": None}
        with self._pending_lock:
            self._pending[req_id] = slot
        payload = json.dumps({"jsonrpc": "2.0", "method": method, "params": params, "id": req_id}) + "\n"
        with self._write_lock:
            self._file.write(payload)
            self._file.flush()
        if not slot["event"].wait(timeout):
            with self._pending_lock:
                self._pending.pop(req_id, None)
            raise TimeoutError(f"no response to {method} within {timeout}s")
        resp = slot["result"]
        if "error" in resp:
            raise RuntimeError(f"{method} failed: {resp['error']}")
        return resp.get("result")


def on_envelope(envelope):
    data = envelope.get("dataMessage")
    if not data:
        return
    sender_uuid = envelope.get("sourceUuid")
    if sender_uuid != ALLOWED_SENDER_UUID:
        print(f"ignoring message from unrecognized sender uuid={sender_uuid}", file=sys.stderr)
        return
    touch()
    # A voice note has isVoiceNote=true; also accept any plain audio/* file
    # attachment (e.g. one shared rather than recorded in Signal's own UI).
    audio = next(
        (a for a in (data.get("attachments") or [])
         if a.get("isVoiceNote") or (a.get("contentType") or "").startswith("audio/")),
        None,
    )
    # Off the reader thread, always - every built-in command handler ends by
    # calling signal_send(), which does rpc.call("send", ...) and blocks
    # waiting for THIS SAME thread to read the response back over the socket.
    # Calling a handler inline here is a guaranteed self-deadlock: the reader
    # thread can't loop back to read send's response until the handler
    # returns, and it can't return until that wait completes. Confirmed live
    # - every reply eats the full 30s timeout before raising, regardless of
    # the daemon's health. Only the chat fallback (dispatch_prompt) was ever
    # spared, because it already ran on its own thread; this makes every
    # handler (text command, chat prompt, voice note) behave the same way.
    if audio is not None:
        threading.Thread(target=handle_voice_message, args=(audio,), daemon=True).start()
        return
    if not data.get("message"):
        return
    threading.Thread(target=handle_message, args=(data["message"],), daemon=True).start()


rpc = SignalRpcClient(SIGNAL_CLI_SOCKET, on_envelope)


def resolve_allowed_sender_uuid():
    """Resolves ALLOWED_SENDER_USERNAME to its account UUID via the daemon.
    Raises (and lets systemd restart us) if the daemon isn't reachable yet or
    the username is unknown - better to crash-loop visibly than silently
    accept no sender."""
    entries = rpc.call("getUserStatus", {"username": [ALLOWED_SENDER_USERNAME]}, timeout=30)
    entry = entries[0]
    if not entry.get("isRegistered") or not entry.get("uuid"):
        raise RuntimeError(f"username {ALLOWED_SENDER_USERNAME!r} did not resolve to a registered account")
    return entry["uuid"]


ALLOWED_SENDER_UUID = resolve_allowed_sender_uuid()
print(f"allowed sender: {ALLOWED_SENDER_USERNAME} -> {ALLOWED_SENDER_UUID}", file=sys.stderr)


def signal_send(text):
    rpc.call("send", {"username": [ALLOWED_SENDER_USERNAME], "message": text})


def admin_call(method, path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        ADMIN_BASE + path,
        data=data,
        method=method,
        headers={"Authorization": f"Bearer {RELAY_SECRET}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


# claude_wrapper now queues a single message behind whatever's already
# running instead of rejecting it outright (see its /prompt docstring), so a
# call here can wait out the CURRENT run before its own even starts - up to
# roughly two runs' worth of time, not one.
PROMPT_TIMEOUT_SECONDS = 1900


def dispatch_prompt(text):
    """Runs off the reader thread (via on_envelope's dispatch, same as every
    other command handler) - a slow/hung claude_wrapper call must never block
    the RPC reader thread from processing an incoming yes/no reply."""
    try:
        req = urllib.request.Request(
            AI_BASE + "/prompt",
            data=json.dumps({"text": text}).encode(),
            method="POST",
            headers={"Authorization": f"Bearer {RELAY_SECRET}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=PROMPT_TIMEOUT_SECONDS) as resp:
            body = json.loads(resp.read())
        if body.get("superseded"):
            # Replaced by a later message before Claude ever saw this one -
            # exactly what was asked for, so stay quiet rather than report it.
            touch()
            return
        reply = body.get("reply", "[empty reply]")
    except urllib.error.HTTPError as exc:
        reply = f"[ai instance error {exc.code}] {exc.read().decode(errors='replace')[:300]}"
    except Exception as exc:  # noqa: BLE001 - report any failure back over Signal
        reply = f"[relay error] {exc}"
    touch()
    try:
        signal_send(reply)
    except Exception as exc:  # noqa: BLE001
        print(f"failed to send reply: {exc}", file=sys.stderr)


# /btw never waits behind the main session (see claude_wrapper's /btw
# docstring), so unlike PROMPT_TIMEOUT_SECONDS this only ever needs to cover
# one run, not two.
BTW_TIMEOUT_SECONDS = 950


def dispatch_btw(text):
    """A '/btw <text>' aside: relayed to claude_wrapper's /btw, which runs it
    as a completely separate, memory-less one-off turn that starts right away
    even if the main session is busy. Runs off the reader thread, same as
    dispatch_prompt, for the same reason - never block it on a slow reply."""
    try:
        req = urllib.request.Request(
            AI_BASE + "/btw",
            data=json.dumps({"text": text}).encode(),
            method="POST",
            headers={"Authorization": f"Bearer {RELAY_SECRET}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=BTW_TIMEOUT_SECONDS) as resp:
            reply = json.loads(resp.read()).get("reply", "[empty reply]")
    except urllib.error.HTTPError as exc:
        reply = f"[ai instance error {exc.code}] {exc.read().decode(errors='replace')[:300]}"
    except Exception as exc:  # noqa: BLE001
        reply = f"[relay error] {exc}"
    touch()
    try:
        signal_send(f"(btw) {reply}")
    except Exception as exc:  # noqa: BLE001
        print(f"failed to send reply: {exc}", file=sys.stderr)


# -- voice messages -------------------------------------------------------
# Ask Claude to close every voice-triggered reply with a plain-language
# TL;DR line, so we have something short enough to speak back without a
# second LLM call. This rides along in the per-turn prompt text (not
# --append-system-prompt) specifically because it must apply even mid-session
# on a --resume'd conversation - see claude_wrapper.py's note on why a system
# prompt change only takes effect on a freshly created session.
VOICE_INSTRUCTION = (
    "\n\n(This message came in as a Signal voice note - the text above is a "
    "transcription of it. Answer normally, then add one final line on its own, "
    "starting with exactly \"TL;DR:\", giving a plain-language 1-2 sentence "
    "summary of your answer suitable to be read aloud - no markdown, no code, "
    "no lists.)"
)
VOICE_TLDR_RE = re.compile(r"(?im)^\s*TL;DR:\s*(.+?)\s*$")


def _fallback_voice_summary(reply):
    """Best-effort spoken summary for when Claude doesn't emit a TL;DR line
    (wrong format, refusal, etc.) - crude, but better than staying silent on
    the voice side while the full text reply still goes out either way."""
    stripped = re.sub(r"```.*?```", "", reply, flags=re.DOTALL)
    stripped = " ".join(stripped.split())
    sentences = re.split(r"(?<=[.!?])\s+", stripped)
    return " ".join(sentences[:2])[:300]


def _run_voice_pipeline(args, timeout):
    result = subprocess.run(
        [VOICE_VENV_PYTHON, VOICE_PIPELINE_SCRIPT, *args],
        capture_output=True, text=True, timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip()[-500:] or f"exit {result.returncode}")
    return result.stdout


def dispatch_voice_prompt(text):
    """Same relay as dispatch_prompt, plus a short spoken-summary reply sent
    back as a voice-note attachment. Called from handle_voice_message, which
    is already off the reader thread (see on_envelope), so no extra thread
    hop is needed here."""
    try:
        req = urllib.request.Request(
            AI_BASE + "/prompt",
            data=json.dumps({"text": text + VOICE_INSTRUCTION}).encode(),
            method="POST",
            headers={"Authorization": f"Bearer {RELAY_SECRET}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=PROMPT_TIMEOUT_SECONDS) as resp:
            body = json.loads(resp.read())
        if body.get("superseded"):
            # Replaced by a later message before Claude ever saw this one - stay
            # quiet on both the text and voice reply, same as dispatch_prompt.
            touch()
            return
        reply = body.get("reply", "[empty reply]")
    except urllib.error.HTTPError as exc:
        reply = f"[ai instance error {exc.code}] {exc.read().decode(errors='replace')[:300]}"
    except Exception as exc:  # noqa: BLE001
        reply = f"[relay error] {exc}"
    touch()
    try:
        signal_send(reply)
    except Exception as exc:  # noqa: BLE001
        print(f"failed to send reply: {exc}", file=sys.stderr)
        return

    if reply.startswith("["):
        return  # An internal error marker (busy/timeout/relay failure), not worth speaking.

    match = VOICE_TLDR_RE.search(reply)
    summary = match.group(1) if match else _fallback_voice_summary(reply)
    if not summary:
        return

    out_path = None
    try:
        fd, out_path = tempfile.mkstemp(suffix=".mp3", dir="/tmp")
        os.close(fd)
        _run_voice_pipeline(["synthesize", summary, out_path], VOICE_TIMEOUT_SECONDS)
        rpc.call("send", {"username": [ALLOWED_SENDER_USERNAME], "attachment": [out_path]})
    except Exception as exc:  # noqa: BLE001 - a failed spoken reply shouldn't be fatal; the text already sent
        print(f"failed to send voice reply: {exc}", file=sys.stderr)
    finally:
        if out_path:
            try:
                os.remove(out_path)
            except OSError:
                pass


def handle_voice_message(attachment):
    """Transcribes an incoming voice-note attachment and forwards it through
    the same Claude relay as a typed message, then speaks back a short
    summary. Runs on its own thread (see on_envelope) - transcription and TTS
    both take real wall-clock time and must never block the RPC reader thread."""
    att_id = os.path.basename(attachment.get("id") or "")
    path = os.path.join(SIGNAL_CLI_ATTACHMENTS_DIR, att_id) if att_id else None
    if not att_id or not path or not os.path.isfile(path):
        signal_send("Couldn't find that voice message's attachment on disk.")
        return
    try:
        stdout = _run_voice_pipeline(["transcribe", path, VOICE_STT_MODEL], VOICE_TIMEOUT_SECONDS)
        parsed = json.loads(stdout)
    except Exception as exc:  # noqa: BLE001
        signal_send(f"[voice transcription failed] {exc}")
        return
    text = (parsed.get("text") or "").strip()
    if not text:
        signal_send("Didn't catch any speech in that voice message.")
        return
    touch()
    signal_send(f'Heard: "{text}"')
    dispatch_voice_prompt(text)


YES_NO_RE = re.compile(r"^(yes|no)\s+(\S+)(?:\s+(permanent|forever|\d+[mhd]))?$", re.IGNORECASE)
BLOCK_RE = re.compile(r"^block\s+(\S+)$", re.IGNORECASE)
ALLOW_RE = re.compile(r"^allow\s+(\S+)(?:\s+(permanent|forever|\d+[mhd]))?$", re.IGNORECASE)
LIST_RE = re.compile(r"^list$", re.IGNORECASE)
STATUS_RE = re.compile(r"^status$", re.IGNORECASE)
RESET_RE = re.compile(r"^reset$", re.IGNORECASE)
URL_RE = re.compile(r"^url$", re.IGNORECASE)
HELP_RE = re.compile(r"^help$", re.IGNORECASE)
WEB_RE = re.compile(r"^web$", re.IGNORECASE)
WEB_STOP_RE = re.compile(r"^web\s+stop$", re.IGNORECASE)
OPEN_RE = re.compile(r"^open$", re.IGNORECASE)
CLOSE_RE = re.compile(r"^close$", re.IGNORECASE)
CS2_RE = re.compile(r"^cs2$", re.IGNORECASE)
CS2_OPEN_RE = re.compile(r"^cs2 open(?:\s+(permanent|forever|\d+[mhd]))?$", re.IGNORECASE)
CS2_CLOSE_RE = re.compile(r"^cs2 close$", re.IGNORECASE)
BTW_RE = re.compile(r"^/btw\s+(.+)$", re.IGNORECASE | re.DOTALL)
BTW_EMPTY_RE = re.compile(r"^/btw\s*$", re.IGNORECASE)

# Kept as one source of truth so `help` can't drift from what's actually wired up -
# update this whenever a command is added or changed, rather than writing a second,
# separately-maintained description elsewhere.
HELP_TEXT = """claude-signal commands:

yes <id> [permanent|1h|30m|2d] - approve a pending request (default: 1h)
no <id> - deny a pending request
allow <domain> [permanent|1h|30m|2d] - allow a domain without waiting for a request
block <domain> - revoke a domain's access immediately, however it was granted
list - show the current allowlist and any pending requests
status - what Claude is doing right now (busy/idle + recent activity)
reset - clear the saved conversation (also needed after a persona/system-prompt change)
url - the controller URL that starts both instances if they're stopped
web - start the deploy/web instance, and show what's currently deployed
web stop - stop just the deploy instance (not the proxy - ai still needs it
    for internet access via Squid, even with nothing deployed/open right now)
open - make the deployed site reachable at http://<proxy public ip>/
close - stop forwarding public traffic to the deployed site (default state)
cs2 - show the CS2 server's start URL and whether Claude currently has SSH access to it
cs2 open [permanent|1h|30m|2d] - grant Claude SSH access to the CS2 server (default: 1h)
cs2 close - revoke Claude's SSH access to the CS2 server (default state)
/btw <message> - a one-off, unrelated aside answered right away even if Claude
    is busy on the main conversation - no memory of the main conversation or of
    any earlier /btw, and doesn't show up in "status" or wait behind anything
help - this message

Anything else is sent to Claude as a chat message. Send a voice note instead
of typing and it's transcribed, answered the same way, and answered back with
a short spoken summary too (as a voice-note reply), in addition to the full
text reply."""


def handle_help():
    signal_send(HELP_TEXT)


def handle_status():
    try:
        req = urllib.request.Request(
            AI_BASE + "/activity", headers={"Authorization": f"Bearer {RELAY_SECRET}"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
    except Exception as exc:  # noqa: BLE001
        signal_send(f"Couldn't reach the ai instance: {exc}")
        return
    line = f"Claude is {'busy' if data['busy'] else 'idle'}."
    if data.get("queued"):
        line += " A newer message is queued and will start as soon as this one's done."
    lines = [line]
    if data.get("recent"):
        lines.append("Recent activity:")
        lines.extend(f"- {line}" for line in data["recent"])
    signal_send("\n".join(lines))


def handle_reset():
    try:
        req = urllib.request.Request(
            AI_BASE + "/reset", method="POST",
            headers={"Authorization": f"Bearer {RELAY_SECRET}"},
        )
        with urllib.request.urlopen(req, timeout=10):
            pass
        signal_send("Session cleared. Next message starts a fresh conversation (also picks up any system prompt / persona changes - those only apply to new sessions).")
    except urllib.error.HTTPError as exc:
        if exc.code == 409:
            signal_send("Can't reset - Claude is still working on something. Try again once it's done.")
        else:
            signal_send(f"Couldn't reset: HTTP {exc.code}")
    except Exception as exc:  # noqa: BLE001
        signal_send(f"Couldn't reach the ai instance: {exc}")


def get_own_public_ip():
    """Reads this instance's own public IP via the EC2 instance metadata
    service (IMDSv2) - a link-local address, always reachable from inside the
    instance regardless of security groups/internet access, and needs no AWS
    credentials (neither instance holds any, by design). This is the only
    reliable way to give the user the CURRENT site URL - there's no Elastic
    IP, so it changes on every stop/start and nothing else on this host
    tracks it."""
    try:
        token_req = urllib.request.Request(
            "http://169.254.169.254/latest/api/token", method="PUT",
            headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"},
        )
        with urllib.request.urlopen(token_req, timeout=2) as resp:
            token = resp.read().decode()
        ip_req = urllib.request.Request(
            "http://169.254.169.254/latest/meta-data/public-ipv4",
            headers={"X-aws-ec2-metadata-token": token},
        )
        with urllib.request.urlopen(ip_req, timeout=2) as resp:
            return resp.read().decode().strip() or None
    except Exception:  # noqa: BLE001
        return None


def _site_url_line():
    public_ip = get_own_public_ip()
    return f"Site URL (while open): http://{public_ip}/" if public_ip else "Site URL: couldn't determine this instance's public IP"


def handle_web():
    start_url = CONTROLLER_URL.rstrip("/") + "/web"
    try:
        result = admin_call("GET", "/web/status")
        status_line = result.get("message", "(no status)")
    except Exception as exc:  # noqa: BLE001
        status_line = f"Couldn't reach the deploy status: {exc}"
    signal_send(f"{status_line}\n\n{_site_url_line()}\n\nStart URL (if the deploy instance is stopped): {start_url}")


def handle_web_stop():
    try:
        result = admin_call("POST", "/web/stop-instances")
        signal_send(result.get("message", "done"))
    except Exception as exc:  # noqa: BLE001
        signal_send(f"Couldn't stop: {exc}")


def handle_open():
    try:
        result = admin_call("POST", "/web/open")
        signal_send(f"{result.get('message', 'done')}\n{_site_url_line()}")
    except Exception as exc:  # noqa: BLE001
        signal_send(f"Couldn't open: {exc}")


def handle_close():
    try:
        result = admin_call("POST", "/web/close")
        signal_send(result.get("message", "done"))
    except Exception as exc:  # noqa: BLE001
        signal_send(f"Couldn't close: {exc}")


def handle_cs2():
    if not CS2_SSH_HOSTNAME:
        signal_send("CS2 integration isn't configured on this deployment.")
        return
    try:
        result = admin_call("GET", "/allowlist")
        allowlist_text = result.get("message", "")
    except Exception as exc:  # noqa: BLE001
        signal_send(f"Couldn't reach the allowlist: {exc}")
        return
    gate_line = next(
        (line for line in allowlist_text.splitlines() if line.startswith(f"{CS2_SSH_HOSTNAME}:")),
        f"{CS2_SSH_HOSTNAME}: not allowed (closed)",
    )
    lines = [gate_line]
    if CS2_CONTROLLER_URL:
        lines.append(f"Start URL (if the CS2 server is stopped): {CS2_CONTROLLER_URL}")
    signal_send("\n".join(lines))


def handle_cs2_open(qualifier):
    if not CS2_SSH_HOSTNAME:
        signal_send("CS2 integration isn't configured on this deployment.")
        return
    try:
        result = admin_call(
            "POST", "/allowlist/allow", {"domain": CS2_SSH_HOSTNAME, "qualifier": qualifier},
        )
        signal_send(result.get("message", "done"))
    except Exception as exc:  # noqa: BLE001
        signal_send(f"Couldn't open CS2 access: {exc}")


def handle_cs2_close():
    if not CS2_SSH_HOSTNAME:
        signal_send("CS2 integration isn't configured on this deployment.")
        return
    try:
        result = admin_call("POST", "/allowlist/block", {"domain": CS2_SSH_HOSTNAME})
        signal_send(result.get("message", "done"))
    except Exception as exc:  # noqa: BLE001
        signal_send(f"Couldn't close CS2 access: {exc}")


def handle_message(text):
    text = text.strip()

    m = YES_NO_RE.match(text)
    if m:
        decision, req_id, qualifier = m.group(1).lower(), m.group(2), m.group(3)
        try:
            result = admin_call(
                "POST", "/resolve",
                {"id": req_id, "approved": decision == "yes", "qualifier": qualifier},
            )
            signal_send(result.get("message", "done"))
        except Exception as exc:  # noqa: BLE001
            signal_send(f"Couldn't resolve {req_id}: {exc}")
        return

    m = BLOCK_RE.match(text)
    if m:
        try:
            result = admin_call("POST", "/allowlist/block", {"domain": m.group(1)})
            signal_send(result.get("message", "done"))
        except Exception as exc:  # noqa: BLE001
            signal_send(f"Couldn't block {m.group(1)}: {exc}")
        return

    m = ALLOW_RE.match(text)
    if m:
        try:
            result = admin_call(
                "POST", "/allowlist/allow", {"domain": m.group(1), "qualifier": m.group(2)},
            )
            signal_send(result.get("message", "done"))
        except Exception as exc:  # noqa: BLE001
            signal_send(f"Couldn't allow {m.group(1)}: {exc}")
        return

    if LIST_RE.match(text):
        try:
            result = admin_call("GET", "/allowlist")
            signal_send(result.get("message", "(empty)"))
        except Exception as exc:  # noqa: BLE001
            signal_send(f"Couldn't list allowlist: {exc}")
        return

    if STATUS_RE.match(text):
        # Runs synchronously (unlike dispatch_prompt) - GET /activity is a fast
        # read served on its own thread by claude_wrapper even while it's busy
        # running a prompt, so this doesn't need to avoid blocking the reader thread.
        handle_status()
        return

    if RESET_RE.match(text):
        handle_reset()
        return

    if URL_RE.match(text):
        # Only useful while this bot is actually running, obviously - if both
        # instances are stopped, nothing here can answer at all. This is just a
        # convenient way to have the controller URL on hand: send `url` when things
        # are up and save the reply, or send it periodically as a liveness check -
        # no reply means the instances are stopped and you need the saved URL to
        # start them back up.
        signal_send(f"Controller URL (open to start both instances): {CONTROLLER_URL}")
        return

    if HELP_RE.match(text):
        handle_help()
        return

    if WEB_RE.match(text):
        handle_web()
        return

    if WEB_STOP_RE.match(text):
        handle_web_stop()
        return

    if OPEN_RE.match(text):
        handle_open()
        return

    if CLOSE_RE.match(text):
        handle_close()
        return

    m = CS2_OPEN_RE.match(text)
    if m:
        handle_cs2_open(m.group(1))
        return

    if CS2_CLOSE_RE.match(text):
        handle_cs2_close()
        return

    if CS2_RE.match(text):
        handle_cs2()
        return

    if BTW_EMPTY_RE.match(text):
        signal_send("Usage: /btw <message> - say what the aside actually is.")
        return

    m = BTW_RE.match(text)
    if m:
        dispatch_btw(m.group(1))
        return

    # handle_message() itself already runs off the reader thread (see
    # on_envelope) - no need for a second thread hop here.
    dispatch_prompt(text)


class BridgeHandler(http.server.BaseHTTPRequestHandler):
    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth(self):
        return self.headers.get("Authorization") == f"Bearer {RELAY_SECRET}"

    def do_GET(self):
        if self.path == "/status":
            with last_activity_lock:
                self._json(200, {"last_activity": last_activity})
            return
        self._json(404, {"error": "not found"})

    def do_POST(self):
        if not self._auth():
            self._json(401, {"error": "unauthorized"})
            return
        if self.path == "/send":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            try:
                signal_send(body.get("text", ""))
            except Exception as exc:  # noqa: BLE001
                self._json(500, {"error": str(exc)})
                return
            self._json(200, {"sent": True})
            return
        self._json(404, {"error": "not found"})

    def log_message(self, fmt, *args):
        pass


def main():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", BRIDGE_PORT), BridgeHandler)
    server.serve_forever()


if __name__ == "__main__":
    main()
