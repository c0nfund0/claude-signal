#!/usr/bin/env python3
"""Deterministic (non-AI) gatekeeper for anything that "leaves the sandbox" -
the Squid domain allowlist, and (as of the git-integration pass) git pushes and
GitHub repo creation too. Same yes/no Signal UX for all of them.

Runs TWO separate HTTP listeners, deliberately not one:

  - "public" listener (0.0.0.0:$RELAY_PORT), reachable from the ai instance's
    security group: POST /request-url-access, GET /request-url-access/<id>,
    POST /request-git-action, GET /request-git-action/<id>. These can only
    ever CREATE a pending request or POLL its status. There is no path here
    that grants or performs anything, for any kind of request.

  - "admin" listener (127.0.0.1:$LOCAL_ADMIN_PORT), reachable only from this
    same host: POST /resolve, POST /allowlist/allow, POST /allowlist/block,
    GET /allowlist. Only signal_bridge.py calls these, after a command
    actually arrived over Signal from $ALLOWED_SENDER.

This split is the actual security boundary, not just the bearer secret: even
if something running on the ai instance learned RELAY_SECRET, it has no
network path to the admin listener at all. Claude Code cannot grant itself
internet access, and it cannot approve its own push or repo-creation request
either - /resolve only decides yes/no, it never performs the underlying
action; the ai-side MCP tool that's polling for the decision does that itself,
once (and only once) it sees "approved".

Allowlist state lives in $ALLOWLIST_STATE_PATH as JSON:
  {"domain": {"permanent": bool, "expires_at": float|null}}
A background thread prunes expired entries and regenerates Squid's allowlist
file whenever the state changes. git_push/repo_create pending requests are
NOT persisted here or anywhere else - they're purely in-memory, same as url
requests, and don't survive a restart of this process.
"""
import http.server
import json
import os
import re
import secrets
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

RELAY_SECRET = os.environ["RELAY_SECRET"]
RELAY_PORT = int(os.environ.get("RELAY_PORT", "8443"))
LOCAL_ADMIN_PORT = int(os.environ.get("LOCAL_ADMIN_PORT", "7802"))
BRIDGE_PORT = os.environ.get("BRIDGE_PORT", "7801")
SQUID_ALLOWLIST_PATH = os.environ.get("SQUID_ALLOWLIST_PATH", "/etc/squid/allowed_domains.txt")
ALLOWLIST_STATE_PATH = os.environ.get("ALLOWLIST_STATE_PATH", "/etc/claude-signal/allowlist.json")
DEFAULT_APPROVAL_SECONDS = int(os.environ.get("DEFAULT_APPROVAL_SECONDS", str(3600)))
PERMANENT_SEED_DOMAINS = [
    d.strip() for d in os.environ.get("PERMANENT_SEED_DOMAINS", "api.anthropic.com").split(",") if d.strip()
]

# Deploy instance relay - not configured until the deploy role has run (empty string
# is fine here, /deploy-trigger and /web/* just fail with a clear error until then).
DEPLOY_PRIVATE_IP = os.environ.get("DEPLOY_PRIVATE_IP", "")
DEPLOY_RELAY_PORT = os.environ.get("DEPLOY_RELAY_PORT", "8443")
DEPLOY_BASE_URL = f"http://{DEPLOY_PRIVATE_IP}:{DEPLOY_RELAY_PORT}"
WEB_GATE_SCRIPT = os.environ.get("WEB_GATE_SCRIPT", "/usr/local/sbin/claude-signal-web-gate")

# Single-link start (see README's "Custom domain" section) - empty WEB_OPEN_SECRET
# means the feature is off, same convention as DEPLOY_PRIVATE_IP above; the listener
# still starts (harmless - nginx only forwards to it via the app_domain vhost, which
# itself doesn't exist without a domain configured) but every request 401s.
WEB_OPEN_SECRET = os.environ.get("WEB_OPEN_SECRET", "")
WEB_OPEN_PORT = int(os.environ.get("WEB_OPEN_PORT", "7803"))
APP_DOMAIN = os.environ.get("APP_DOMAIN", "")

# GitHub token lives here and ONLY here now - confirmed live that giving it to the
# ai-side MCP tool via `claude mcp add --env` persists it in plaintext in
# ~/.claude.json inside the sandbox container, readable by the same `coder` user
# Claude runs as (i.e. Claude could just read it directly, bypassing the whole
# approval gate for repo creation). This process is not reachable from the ai
# instance's network at all except via the request/poll endpoints below, so this
# is an actual isolation boundary, not just "Claude isn't told about it."
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")

BRIDGE_SEND_URL = f"http://127.0.0.1:{BRIDGE_PORT}/send"

state_lock = threading.Lock()
pending = {}  # id -> {"url":..., "domain":..., "status": "pending"|"approved"|"denied", "created": ts}


def load_allowlist():
    if os.path.exists(ALLOWLIST_STATE_PATH):
        with open(ALLOWLIST_STATE_PATH) as f:
            return json.load(f)
    return {}


def save_allowlist(allowlist):
    os.makedirs(os.path.dirname(ALLOWLIST_STATE_PATH), exist_ok=True)
    tmp = ALLOWLIST_STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(allowlist, f, indent=2)
    os.replace(tmp, ALLOWLIST_STATE_PATH)


def regenerate_squid(allowlist):
    # Written in place (not tmp-file-then-rename) deliberately: /etc/squid/ itself is
    # root-owned (the squid package's default), so claude-signal has write permission
    # on the allowlist file itself (chowned by install.sh) but not on the directory,
    # which a rename would require. In-place write only needs file-level permission.
    now = time.time()
    active = [d for d, meta in allowlist.items() if meta.get("permanent") or (meta.get("expires_at") or 0) > now]
    with open(SQUID_ALLOWLIST_PATH, "w") as f:
        f.write("\n".join(sorted(active)) + "\n")
    subprocess.run(["sudo", "/usr/sbin/squid", "-k", "reconfigure"], check=False, capture_output=True)


def ensure_seed(allowlist):
    changed = False
    for domain in PERMANENT_SEED_DOMAINS:
        if domain not in allowlist or not allowlist[domain].get("permanent"):
            allowlist[domain] = {"permanent": True, "expires_at": None}
            changed = True
    return changed


allowlist_lock = threading.Lock()
with allowlist_lock:
    _allowlist = load_allowlist()
    if ensure_seed(_allowlist):
        save_allowlist(_allowlist)
    regenerate_squid(_allowlist)


def add_domain(domain, permanent, duration_seconds):
    with allowlist_lock:
        allowlist = load_allowlist()
        allowlist[domain] = {
            "permanent": permanent,
            "expires_at": None if permanent else time.time() + duration_seconds,
        }
        save_allowlist(allowlist)
        regenerate_squid(allowlist)


def remove_domain(domain):
    with allowlist_lock:
        allowlist = load_allowlist()
        existed = allowlist.pop(domain, None) is not None
        save_allowlist(allowlist)
        regenerate_squid(allowlist)
        return existed


def describe_allowlist():
    with allowlist_lock:
        allowlist = load_allowlist()
    now = time.time()
    lines = []
    for domain, meta in sorted(allowlist.items()):
        if meta.get("permanent"):
            lines.append(f"{domain}: permanent")
        else:
            remaining = int((meta.get("expires_at") or now) - now)
            if remaining <= 0:
                continue
            lines.append(f"{domain}: expires in {remaining // 60}m")
    with state_lock:
        pend = [
            f"{rid} -> {p.get('domain') or p.get('summary', p.get('kind', '?'))} (pending)"
            for rid, p in pending.items() if p["status"] == "pending"
        ]
    body = "\n".join(lines) or "(nothing allowed)"
    if pend:
        body += "\n\nPending:\n" + "\n".join(pend)
    return body


def qualifier_to_grant(qualifier):
    """Returns (permanent, duration_seconds) for a yes/allow qualifier string."""
    if qualifier and qualifier.lower() in ("permanent", "forever"):
        return True, None
    if qualifier:
        m = re.match(r"^(\d+)([mhd])$", qualifier.lower())
        if m:
            n, unit = int(m.group(1)), m.group(2)
            mult = {"m": 60, "h": 3600, "d": 86400}[unit]
            return False, n * mult
    return False, DEFAULT_APPROVAL_SECONDS


def prune_loop():
    while True:
        time.sleep(60)
        with allowlist_lock:
            allowlist = load_allowlist()
            now = time.time()
            expired = [d for d, meta in allowlist.items()
                       if not meta.get("permanent") and (meta.get("expires_at") or 0) <= now]
            if expired:
                for d in expired:
                    del allowlist[d]
                save_allowlist(allowlist)
                regenerate_squid(allowlist)


def notify_signal(text):
    import urllib.request
    req = urllib.request.Request(
        BRIDGE_SEND_URL,
        data=json.dumps({"text": text}).encode(),
        method="POST",
        headers={"Authorization": f"Bearer {RELAY_SECRET}", "Content-Type": "application/json"},
    )
    urllib.request.urlopen(req, timeout=15)


class BaseHandler(http.server.BaseHTTPRequestHandler):
    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth(self):
        return self.headers.get("Authorization") == f"Bearer {RELAY_SECRET}"

    def _read_json(self):
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length) or b"{}")

    def log_message(self, fmt, *args):
        pass


class PublicHandler(BaseHandler):
    """Reachable from the ai instance. Can only create/poll pending requests - there
    is no path here that grants anything, for ANY kind of request."""

    def do_POST(self):
        if not self._auth():
            self._json(401, {"error": "unauthorized"})
            return
        if self.path == "/request-url-access":
            body = self._read_json()
            url = body.get("url", "")
            domain = urllib.parse.urlparse(url).hostname or url
            req_id = secrets.token_hex(3)
            with state_lock:
                pending[req_id] = {
                    "kind": "url", "url": url, "domain": domain,
                    "status": "pending", "created": time.time(),
                }
            try:
                notify_signal(
                    f"Claude wants access to: {domain}\n({url})\n\n"
                    f"Reply 'yes {req_id}' to allow for {DEFAULT_APPROVAL_SECONDS // 3600}h, "
                    f"'yes {req_id} permanent' to always allow, or 'no {req_id}' to deny."
                )
            except Exception as exc:  # noqa: BLE001
                self._json(502, {"error": f"could not notify via signal: {exc}"})
                return
            self._json(200, {"id": req_id, "status": "pending"})
            return

        if self.path == "/request-git-action":
            # Generic gate for anything that "leaves the sandbox" via git/GitHub -
            # push, repo creation, and deployment all share this same mechanism and
            # the same yes/no Signal UX as URL access, just without ever touching
            # the Squid allowlist on resolution. The actual push/create action
            # itself is performed by the caller (the ai-side MCP tool) once it
            # sees "approved" here - this daemon only ever decides yes/no, never
            # executes anything on the ai instance's behalf. Deploy is the one
            # exception to "the caller performs it": the ai instance has no
            # network path to the deploy instance at all, only the proxy does, so
            # the deploy step itself necessarily runs as a relay here (see
            # /deploy-trigger below) - but it still only fires once, and only
            # for a request this same handler already marked "approved".
            body = self._read_json()
            kind = body.get("kind")
            summary = body.get("summary", "")
            if kind not in ("git_push", "repo_create", "deploy"):
                self._json(400, {"error": "invalid kind"})
                return
            req_id = secrets.token_hex(3)
            with state_lock:
                pending[req_id] = {"kind": kind, "summary": summary, "status": "pending", "created": time.time()}
            verb = {"git_push": "push", "repo_create": "create a new private repo", "deploy": "push and deploy"}[kind]
            try:
                notify_signal(
                    f"Claude wants to {verb}:\n{summary}\n\n"
                    f"Reply 'yes {req_id}' to approve or 'no {req_id}' to deny."
                )
            except Exception as exc:  # noqa: BLE001
                self._json(502, {"error": f"could not notify via signal: {exc}"})
                return
            self._json(200, {"id": req_id, "status": "pending"})
            return

        if self.path == "/deploy-trigger":
            # Relays an already-approved deploy to the deploy instance - the ai
            # instance can't reach it directly (no route at either the network or
            # security-group layer, same isolation as its lack of internet access).
            # One-shot per request id: "executed" is set the instant we forward the
            # call, so a request can never trigger the actual deploy twice even if
            # the ai-side tool retries after a timeout.
            body = self._read_json()
            req_id = body.get("id", "")
            repo, branch = body.get("repo", ""), body.get("branch", "")
            with state_lock:
                entry = pending.get(req_id)
                if not entry or entry.get("kind") != "deploy" or entry.get("status") != "approved":
                    self._json(403, {"error": "no matching approved deploy request"})
                    return
                if entry.get("executed"):
                    self._json(409, {"error": "already executed"})
                    return
                entry["executed"] = True
            if not DEPLOY_PRIVATE_IP:
                self._json(503, {"error": "deploy instance not configured"})
                return
            req = urllib.request.Request(
                DEPLOY_BASE_URL + "/deploy",
                data=json.dumps({"repo": repo, "branch": branch}).encode(),
                method="POST",
                headers={"Authorization": f"Bearer {RELAY_SECRET}", "Content-Type": "application/json"},
            )
            try:
                with urllib.request.urlopen(req, timeout=600) as resp:
                    self._json(200, json.loads(resp.read()))
            except urllib.error.HTTPError as exc:
                # Forward the deploy instance's own error detail (e.g. which stage
                # of clone/build/run failed) rather than swallowing it.
                self._json(exc.code, json.loads(exc.read() or b"{}"))
            except Exception as exc:  # noqa: BLE001
                self._json(502, {"error": f"deploy relay failed: {exc}"})
            return

        if self.path == "/repo-create-trigger":
            # Same one-shot-per-approved-request-id pattern as /deploy-trigger,
            # and the same reason: the actual privileged action (a GitHub API
            # call using GITHUB_TOKEN) has to happen somewhere Claude cannot
            # reach, which is here - never inside the sandbox container.
            body = self._read_json()
            req_id = body.get("id", "")
            name, description = body.get("name", ""), body.get("description", "")
            with state_lock:
                entry = pending.get(req_id)
                if not entry or entry.get("kind") != "repo_create" or entry.get("status") != "approved":
                    self._json(403, {"error": "no matching approved repo-creation request"})
                    return
                if entry.get("executed"):
                    self._json(409, {"error": "already executed"})
                    return
                entry["executed"] = True
            if not GITHUB_TOKEN:
                self._json(503, {"error": "GITHUB_TOKEN not configured on the proxy"})
                return
            req = urllib.request.Request(
                "https://api.github.com/user/repos",
                data=json.dumps({"name": name, "description": description, "private": True}).encode(),
                method="POST",
                headers={
                    "Authorization": f"token {GITHUB_TOKEN}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                    "User-Agent": "claude-signal",
                    "Content-Type": "application/json",
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    result = json.loads(resp.read())
                self._json(200, {"ok": True, "ssh_url": result.get("ssh_url", "?")})
            except urllib.error.HTTPError as exc:
                body_text = exc.read().decode(errors="replace")
                self._json(200, {"ok": False, "error": f"GitHub API error {exc.code}: {body_text[:300]}"})
            except Exception as exc:  # noqa: BLE001
                self._json(200, {"ok": False, "error": str(exc)})
            return

        self._json(404, {"error": "not found"})

    def do_GET(self):
        if not self._auth():
            self._json(401, {"error": "unauthorized"})
            return
        if self.path.startswith("/request-url-access/") or self.path.startswith("/request-git-action/"):
            req_id = self.path.rsplit("/", 1)[-1]
            with state_lock:
                entry = pending.get(req_id)
            if not entry:
                self._json(404, {"error": "unknown request id"})
                return
            self._json(200, {"status": entry["status"]})
            return
        self._json(404, {"error": "not found"})


class AdminHandler(BaseHandler):
    """127.0.0.1 only - never reachable from the ai instance's security group."""

    def do_POST(self):
        if not self._auth():
            self._json(401, {"error": "unauthorized"})
            return
        body = self._read_json()

        if self.path == "/resolve":
            req_id, approved, qualifier = body.get("id"), body.get("approved"), body.get("qualifier")
            with state_lock:
                entry = pending.get(req_id)
            if not entry:
                self._json(404, {"message": f"No pending request {req_id} (may have expired)."})
                return
            kind = entry.get("kind", "url")
            if kind == "url":
                if approved:
                    permanent, duration = qualifier_to_grant(qualifier)
                    add_domain(entry["domain"], permanent, duration)
                    entry["status"] = "approved"
                    how = "permanently" if permanent else f"for {duration // 60}m"
                    self._json(200, {"message": f"Approved {entry['domain']} {how}."})
                else:
                    entry["status"] = "denied"
                    self._json(200, {"message": f"Denied {entry['domain']}."})
            else:
                # git_push / repo_create: this daemon only ever records the
                # decision - it never touches the Squid allowlist for these, and it
                # never performs the push/create itself. The ai-side MCP tool is
                # polling /request-git-action/<id> and does the actual git/GitHub
                # work only once it sees "approved" there.
                entry["status"] = "approved" if approved else "denied"
                verb = "Approved" if approved else "Denied"
                self._json(200, {"message": f"{verb}: {entry.get('summary', kind)}"})
            return

        if self.path == "/allowlist/allow":
            domain, qualifier = body.get("domain"), body.get("qualifier")
            permanent, duration = qualifier_to_grant(qualifier)
            add_domain(domain, permanent, duration)
            how = "permanently" if permanent else f"for {duration // 60}m"
            self._json(200, {"message": f"Allowed {domain} {how}."})
            return

        if self.path == "/allowlist/block":
            domain = body.get("domain")
            existed = remove_domain(domain)
            self._json(200, {"message": f"Blocked {domain}." if existed else f"{domain} wasn't allowed anyway."})
            return

        if self.path in ("/web/open", "/web/close"):
            # Direct admin action, not approval-gated the way git/deploy requests
            # are - the person issuing this command over Signal IS the approver,
            # same trust model as "allow <domain>". Network-layer posture never
            # changes (port 80 on the proxy is always reachable - see network.tf);
            # what toggles is whether the reverse proxy forwards to the deploy
            # instance or refuses with 503, mirroring how the Squid allowlist gates
            # outbound traffic on an always-listening port.
            state = "open" if self.path == "/web/open" else "closed"
            result = subprocess.run(
                ["sudo", WEB_GATE_SCRIPT, state], capture_output=True, text=True, check=False,
            )
            if result.returncode != 0:
                self._json(500, {"message": f"Failed to set web gate to {state}: {result.stderr.strip()}"})
                return
            self._json(200, {"message": f"Web is now {state}."})
            return

        self._json(404, {"error": "not found"})

    def do_GET(self):
        if not self._auth():
            self._json(401, {"error": "unauthorized"})
            return
        if self.path == "/allowlist":
            self._json(200, {"message": describe_allowlist()})
            return
        if self.path == "/web/status":
            if not DEPLOY_PRIVATE_IP:
                self._json(200, {"message": "Deploy instance not configured yet."})
                return
            try:
                req = urllib.request.Request(
                    DEPLOY_BASE_URL + "/status",
                    headers={"Authorization": f"Bearer {RELAY_SECRET}"},
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    deployed = json.loads(resp.read())
            except Exception as exc:  # noqa: BLE001
                self._json(200, {"message": f"Couldn't reach the deploy instance (is it running? try the /web start URL): {exc}"})
                return
            if deployed.get("repo"):
                body_msg = (
                    f"Deployed: {deployed['repo']}@{deployed['branch']} ({deployed['commit']})\n"
                    f"Container running: {deployed['container_running']}"
                )
            else:
                body_msg = "Nothing deployed yet."
            self._json(200, {"message": body_msg})
            return
        self._json(404, {"error": "not found"})


class WebOpenHandler(BaseHandler):
    """127.0.0.1 only, same as AdminHandler - but reached from the internet
    indirectly, via app_domain's nginx vhost proxying POST /_claude-signal/open
    here (see claude-signal-web-ssl.nginx.j2). That path is deliberately outside
    the gated `location /` block, so it works even while the gate is closed - the
    WEB_OPEN_SECRET header is the only thing standing between the open internet
    and this, which is why it's a dedicated secret rather than RELAY_SECRET
    (different trust boundary: this is reachable from anyone who finds
    web_domain, not just the ai/deploy instances)."""

    def do_POST(self):
        if not WEB_OPEN_SECRET or self.headers.get("X-Web-Open-Secret") != WEB_OPEN_SECRET:
            self._json(401, {"error": "unauthorized"})
            return
        if self.path != "/open":
            self._json(404, {"error": "not found"})
            return
        result = subprocess.run(
            ["sudo", WEB_GATE_SCRIPT, "open"], capture_output=True, text=True, check=False,
        )
        if result.returncode != 0:
            self._json(500, {"message": f"Failed to open web gate: {result.stderr.strip()}"})
            return
        try:
            notify_signal(f"\U0001f310 {APP_DOMAIN or 'the site'} was opened via the start link.")
        except Exception:  # noqa: BLE001 - the gate opened either way; a missed notification isn't fatal
            pass
        self._json(200, {"message": "Web is now open."})


def main():
    threading.Thread(target=prune_loop, daemon=True).start()
    public = http.server.ThreadingHTTPServer(("0.0.0.0", RELAY_PORT), PublicHandler)
    admin = http.server.ThreadingHTTPServer(("127.0.0.1", LOCAL_ADMIN_PORT), AdminHandler)
    web_open = http.server.ThreadingHTTPServer(("127.0.0.1", WEB_OPEN_PORT), WebOpenHandler)
    threading.Thread(target=admin.serve_forever, daemon=True).start()
    threading.Thread(target=web_open.serve_forever, daemon=True).start()
    public.serve_forever()


if __name__ == "__main__":
    main()
