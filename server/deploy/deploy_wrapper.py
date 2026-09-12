#!/usr/bin/env python3
"""Runs on the deploy instance. Bearer-token protected (RELAY_SECRET, shared
with the proxy) HTTP interface, reachable only from the proxy's security
group - never directly from the ai instance or the internet.

POST /deploy {"repo": "<name under c0nfund0>", "branch": "..."} -> clean pull +
             rebuild + run. Only ever called by approval_daemon's
             /deploy-trigger relay, and only after a Signal approval has
             already been granted - this process trusts whoever can reach it
             on the network (the proxy), same trust model as claude_wrapper.py
             trusting the proxy for /prompt. Assigns the repo a host port out
             of DEPLOY_PORT_RANGE_START..DEPLOY_PORT_RANGE_END the first time
             it's deployed (stable across later redeploys of the same repo,
             recorded in the state file), so nothing on the proxy/network side
             needs to know port numbers in advance - see approval_daemon.py's
             post-deploy nginx sync for how the proxy learns the assignment.
POST /stop {"repo": "..."} -> stop the repo's container without removing it
             (data volume and port assignment both survive - `start`s again on
             the next redeploy or the next resume_if_needed() boot check).
POST /remove {"repo": "..."} -> stop AND remove the repo's container, image,
             and its port assignment (freeing the port for reuse). The named
             data volume is deliberately left alone - removing a deployment
             should not silently destroy whatever it had persisted; delete the
             volume by hand (`podman volume rm`) if that's really the intent.
GET  /status -> what's currently deployed (repo/branch/commit/http_port per
                repo) and whether each one's container is actually running.

Multiple repos can be deployed side by side - each gets its own container
(named after the repo) and its own host port. Deploying, stopping, or removing
one repo never touches another repo's already-running container. Every deploy
is "clean OS" for that repo: its previous container is stopped and removed,
its repo is re-cloned from scratch into a fresh directory (not `git pull`ed in
place - a stale local state should never leak into a new deploy), and a new
container is built and started from that. Nothing about that repo's previous
deployment is reused - other repos' deployments are simply left alone.

Every deployed container is airgapped by default (no proxy env vars, no
credentials) - DEPLOY_EXTRA_ENV (JSON: {repo: {ENV_VAR: value}}, see
deploy_extra_env in ansible/group_vars/all.yml) opts a specific repo into
extra env vars (e.g. HTTP_PROXY/HTTPS_PROXY for outbound access through the
proxy's Squid allowlist, or a credential that repo's own app needs), injected
only into that repo's own container.
"""
import http.server
import json
import os
import re
import shutil
import subprocess
import threading
import time

RELAY_SECRET = os.environ["RELAY_SECRET"]
RELAY_PORT = int(os.environ.get("RELAY_PORT", "8443"))
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
GITHUB_ORG = os.environ.get("GITHUB_ORG", "c0nfund0")
# A repo keeps whatever port it was first assigned (recorded in the state file)
# for as long as it stays deployed, so the proxy's routing for it never needs to
# change across redeploys - only a /remove (which frees the port) changes that.
PORT_RANGE_START = int(os.environ.get("DEPLOY_PORT_RANGE_START", "8080"))
PORT_RANGE_END = int(os.environ.get("DEPLOY_PORT_RANGE_END", "8099"))
WORKDIR = os.environ.get("DEPLOY_WORKDIR", "/opt/claude-signal/deploy-work")
STATE_FILE = os.environ.get("DEPLOY_STATE_FILE", "/opt/claude-signal/deploy-state.json")
XDG_RUNTIME_DIR = f"/run/user/{os.getuid()}"
# repo -> {ENV_VAR: value}, injected only into that repo's own container - see
# deploy_extra_env in ansible/group_vars/all.yml. Deliberately opt-in per repo:
# most deployed apps are fully airgapped on purpose (no proxy env vars, no
# credentials), and that should never change just because a repo happens to
# get deployed here.
try:
    EXTRA_ENV = json.loads(os.environ.get("DEPLOY_EXTRA_ENV", "{}"))
except json.JSONDecodeError:
    EXTRA_ENV = {}

deploy_lock = threading.Lock()


def _podman(*args, timeout=120):
    env = dict(os.environ, XDG_RUNTIME_DIR=XDG_RUNTIME_DIR)
    return subprocess.run(
        ["podman", *args], capture_output=True, text=True, timeout=timeout, env=env,
    )


def _safe_name(repo: str) -> str:
    """A repo name made safe for container/volume/work-dir names, which allow a
    narrower character set (or in the work-dir case, just fewer surprises) than
    repo names do."""
    return re.sub(r"[^A-Za-z0-9_.-]", "-", repo)


def _container_name(repo: str) -> str:
    return "claude-signal-deploy-" + _safe_name(repo)


def _image_tag(repo: str) -> str:
    return f"claude-signal-deploy:{repo}"


def _data_volume(repo: str) -> str:
    """The podman volume holding a repo's /data between deploys. Volume names
    allow a narrower character set than repo names do."""
    return "claude-signal-data-" + _safe_name(repo)


def _allocate_port(repo: str, state: dict) -> int:
    existing = state.get(repo, {}).get("http_port")
    if existing:
        return existing
    used = {info["http_port"] for info in state.values() if info.get("http_port")}
    for port in range(PORT_RANGE_START, PORT_RANGE_END + 1):
        if port not in used:
            return port
    raise RuntimeError(f"no free port left in {PORT_RANGE_START}-{PORT_RANGE_END}")


def _save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)


def _load_state():
    """repo -> {repo, branch, commit, deployed_at, data_volume, container, http_port}."""
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def do_deploy(repo, branch):
    """Runs entirely under deploy_lock - one deploy at a time across all repos
    (simplicity over throughput; deploys are rare and human-triggered), always to
    completion or a clean failure, never interleaved with another deploy of
    anything. Only ever touches this repo's own container/clone/volume."""
    clone_dir = os.path.join(WORKDIR, _safe_name(repo))
    shutil.rmtree(clone_dir, ignore_errors=True)
    os.makedirs(WORKDIR, exist_ok=True)

    clone_url = f"https://x-access-token:{GITHUB_TOKEN}@github.com/{GITHUB_ORG}/{repo}.git"
    clone = subprocess.run(
        ["git", "clone", "--depth", "1", "--branch", branch, clone_url, clone_dir],
        capture_output=True, text=True, timeout=180,
    )
    if clone.returncode != 0:
        # Never let a failed clone leak the token into logs/replies.
        stderr = clone.stderr.replace(GITHUB_TOKEN, "***")
        return {"ok": False, "stage": "clone", "error": stderr[-1500:]}

    containerfile = os.path.join(clone_dir, "Containerfile")
    if not os.path.exists(containerfile):
        containerfile = os.path.join(clone_dir, "Dockerfile")
    if not os.path.exists(containerfile):
        return {"ok": False, "stage": "build", "error": "no Containerfile or Dockerfile at repo root"}

    commit = subprocess.run(
        ["git", "-C", clone_dir, "rev-parse", "--short", "HEAD"],
        capture_output=True, text=True, timeout=15,
    ).stdout.strip()

    state = _load_state()
    try:
        http_port = _allocate_port(repo, state)
    except RuntimeError as exc:
        return {"ok": False, "stage": "port", "error": str(exc)}

    image_tag = _image_tag(repo)
    build = _podman("build", "-t", image_tag, "-f", containerfile, clone_dir, timeout=600)
    if build.returncode != 0:
        return {"ok": False, "stage": "build", "error": (build.stdout + build.stderr)[-1500:]}

    container_name = _container_name(repo)
    _podman("rm", "-f", container_name, timeout=30)
    run = _podman(
        "run", "-d", "--name", container_name,
        "--cap-drop=all", "--security-opt", "no-new-privileges",
        # 700m on a 914MB (t3.micro) host: current deploys (chess-coach) sit
        # around 270MB at rest, but Stockfish spikes got OOM-killed at the
        # old 500m cap - confirmed via `dmesg -T | grep -i oom`. 700m leaves
        # roughly 200MB for the host's own overhead (podman, sshd,
        # unattended-upgrades, this process), which is comfortably more than
        # it's ever actually used - if a future deploy needs more than this,
        # move to a bigger instance type rather than raising this further.
        # Applies per-container, not shared - see network.tf's swap note for
        # what happens on genuine host-wide pressure instead. On a t3.micro,
        # realistically only 1-2 of these can run at once regardless of how
        # many ports are free - see /stop and /remove above for freeing one up.
        "--pids-limit=512", "--memory=700m",
        # A *named* volume, so /data outlives the container. An image's own
        # VOLUME directive creates an anonymous one, which the `podman rm -f`
        # above orphans - every deploy then started the app with an empty data
        # directory and no sign that anything had been lost. Named per repo, so
        # deploying a different app never inherits another one's state.
        "-v", f"{_data_volume(repo)}:/data",
        "-p", f"{http_port}:{http_port}",
        "-e", f"PORT={http_port}",
        *[arg for key, value in EXTRA_ENV.get(repo, {}).items() for arg in ("-e", f"{key}={value}")],
        image_tag,
        timeout=60,
    )
    if run.returncode != 0:
        return {"ok": False, "stage": "run", "error": (run.stdout + run.stderr)[-1500:]}

    state[repo] = {
        "repo": repo, "branch": branch, "commit": commit, "deployed_at": time.time(),
        "data_volume": _data_volume(repo), "container": container_name, "http_port": http_port,
    }
    _save_state(state)
    return {"ok": True, **state[repo]}


def do_stop(repo):
    state = _load_state()
    info = state.get(repo)
    if not info:
        return {"ok": False, "error": f"{repo} isn't deployed"}
    container_name = info.get("container", _container_name(repo))
    result = _podman("stop", container_name, timeout=30)
    if result.returncode != 0:
        return {"ok": False, "error": (result.stdout + result.stderr).strip()[-1000:]}
    return {"ok": True, "repo": repo, "stopped": True}


def do_remove(repo):
    """Stops and removes the container + image, and drops the repo from state
    (freeing its port for a future deploy of something else). Deliberately
    leaves the named data volume alone - see the module docstring."""
    state = _load_state()
    info = state.get(repo)
    if not info:
        return {"ok": False, "error": f"{repo} isn't deployed"}
    container_name = info.get("container", _container_name(repo))
    _podman("rm", "-f", container_name, timeout=30)
    _podman("rmi", "-f", _image_tag(repo), timeout=60)
    del state[repo]
    _save_state(state)
    return {"ok": True, "repo": repo, "removed": True}


def get_status():
    state = _load_state()
    deployments = {}
    for repo, info in state.items():
        container_name = info.get("container", _container_name(repo))
        ps = _podman("ps", "--filter", f"name=^{container_name}$", "--format", "{{.Status}}", timeout=15)
        deployments[repo] = {**info, "container_running": bool(ps.stdout.strip())}
    return {"deployments": deployments}


def resume_if_needed():
    """`podman run` above carries no --restart policy, and this instance stops/
    starts routinely (idle auto-stop, manual /stop, a fresh terraform apply) -
    without this, every deployed container is SIGTERM'd on every stop and never
    comes back, silently, until someone happens to notice and redeploy it. Since
    this process itself is a systemd unit that starts on every boot, checking
    here is enough: for each repo the state file knows about, if that container
    exists but isn't running, just start it - no rebuild, no re-clone, identical
    to what was last successfully deployed for that repo. Runs once per repo
    independently, so one repo's container failing to resume never blocks
    another's. A repo stopped on purpose via POST /stop resumes here too on the
    next boot - same as an idle-triggered instance stop, this process has no way
    to tell the two apart, and re-arriving at "whatever was last deployed is
    running" is the right default either way."""
    state = _load_state()
    for repo, info in state.items():
        container_name = info.get("container", _container_name(repo))
        ps = _podman("ps", "-a", "--filter", f"name=^{container_name}$", "--format", "{{.Status}}", timeout=15)
        status = ps.stdout.strip()
        if not status or status.startswith("Up"):
            continue  # nothing to resume (never deployed / already removed), or already running
        print(f"resuming {repo}@{info.get('branch')} ({info.get('commit')}) after restart: {status}")
        result = _podman("start", container_name, timeout=30)
        if result.returncode != 0:
            print(f"failed to resume {repo}'s container: {(result.stdout + result.stderr).strip()}")


class Handler(http.server.BaseHTTPRequestHandler):
    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth(self):
        return self.headers.get("Authorization") == f"Bearer {RELAY_SECRET}"

    def _read_repo(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        return body.get("repo", "")

    def do_GET(self):
        if not self._auth():
            self._json(401, {"error": "unauthorized"})
            return
        if self.path == "/status":
            self._json(200, get_status())
            return
        self._json(404, {"error": "not found"})

    def do_POST(self):
        if not self._auth():
            self._json(401, {"error": "unauthorized"})
            return
        if self.path == "/deploy":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            repo, branch = body.get("repo", ""), body.get("branch", "")
            if not repo or not branch:
                self._json(400, {"error": "repo and branch are required"})
                return
            if not deploy_lock.acquire(blocking=False):
                self._json(409, {"error": "a deploy is already in progress"})
                return
            try:
                result = do_deploy(repo, branch)
            finally:
                deploy_lock.release()
            # Always 200 - "ok" in the body is the semantic success/failure
            # signal, deliberately not the HTTP status. A conditional 500 here
            # made the caller (approval_daemon's relay, then mcp_git_gate.py on
            # the ai side) take a completely different code path on failure
            # (HTTPError handling that blindly truncates the raw JSON text)
            # instead of the same clean success/failure formatting either way -
            # confirmed live, produced a confusing mid-string-cutoff error.
            self._json(200, result)
            return
        if self.path == "/stop":
            repo = self._read_repo()
            if not repo:
                self._json(400, {"error": "repo is required"})
                return
            if not deploy_lock.acquire(blocking=False):
                self._json(409, {"error": "a deploy is already in progress"})
                return
            try:
                result = do_stop(repo)
            finally:
                deploy_lock.release()
            self._json(200, result)
            return
        if self.path == "/remove":
            repo = self._read_repo()
            if not repo:
                self._json(400, {"error": "repo is required"})
                return
            if not deploy_lock.acquire(blocking=False):
                self._json(409, {"error": "a deploy is already in progress"})
                return
            try:
                result = do_remove(repo)
            finally:
                deploy_lock.release()
            self._json(200, result)
            return
        self._json(404, {"error": "not found"})

    def log_message(self, fmt, *args):
        pass


def main():
    resume_if_needed()
    server = http.server.ThreadingHTTPServer(("0.0.0.0", RELAY_PORT), Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()
