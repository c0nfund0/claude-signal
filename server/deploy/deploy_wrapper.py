#!/usr/bin/env python3
"""Runs on the deploy instance. Bearer-token protected (RELAY_SECRET, shared
with the proxy) HTTP interface, reachable only from the proxy's security
group - never directly from the ai instance or the internet.

POST /deploy {"repo": "<name under c0nfund0>", "branch": "..."} -> clean pull +
             rebuild + run. Only ever called by approval_daemon's
             /deploy-trigger relay, and only after a Signal approval has
             already been granted - this process trusts whoever can reach it
             on the network (the proxy), same trust model as claude_wrapper.py
             trusting the proxy for /prompt.
GET  /status -> what's currently deployed (repo/branch/commit) and whether the
                container is actually running.

Every deploy is "clean OS": the previous container is stopped and removed, the
repo is re-cloned from scratch into a fresh directory (not `git pull`ed in
place - a stale local state should never leak into a new deploy), and a new
container is built and started from that. Nothing about the previous
deployment is reused.
"""
import http.server
import json
import os
import shutil
import subprocess
import threading
import time

RELAY_SECRET = os.environ["RELAY_SECRET"]
RELAY_PORT = int(os.environ.get("RELAY_PORT", "8443"))
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
GITHUB_ORG = os.environ.get("GITHUB_ORG", "c0nfund0")
HTTP_PORT = int(os.environ.get("DEPLOY_HTTP_PORT", "8080"))
WORKDIR = os.environ.get("DEPLOY_WORKDIR", "/opt/claude-signal/deploy-work")
CONTAINER_NAME = "claude-signal-deploy"
STATE_FILE = os.environ.get("DEPLOY_STATE_FILE", "/opt/claude-signal/deploy-state.json")
XDG_RUNTIME_DIR = f"/run/user/{os.getuid()}"

deploy_lock = threading.Lock()


def _podman(*args, timeout=120):
    env = dict(os.environ, XDG_RUNTIME_DIR=XDG_RUNTIME_DIR)
    return subprocess.run(
        ["podman", *args], capture_output=True, text=True, timeout=timeout, env=env,
    )


def _save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)


def _load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"repo": None, "branch": None, "commit": None, "deployed_at": None}


def do_deploy(repo, branch):
    """Runs entirely under deploy_lock - one deploy at a time, always to
    completion or a clean failure, never interleaved with another."""
    clone_dir = os.path.join(WORKDIR, "current")
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

    image_tag = f"claude-signal-deploy:{repo}"
    build = _podman("build", "-t", image_tag, "-f", containerfile, clone_dir, timeout=600)
    if build.returncode != 0:
        return {"ok": False, "stage": "build", "error": (build.stdout + build.stderr)[-1500:]}

    _podman("rm", "-f", CONTAINER_NAME, timeout=30)
    run = _podman(
        "run", "-d", "--name", CONTAINER_NAME,
        "--cap-drop=all", "--security-opt", "no-new-privileges",
        "--pids-limit=512", "--memory=512m",
        "-p", f"{HTTP_PORT}:{HTTP_PORT}",
        "-e", f"PORT={HTTP_PORT}",
        image_tag,
        timeout=60,
    )
    if run.returncode != 0:
        return {"ok": False, "stage": "run", "error": (run.stdout + run.stderr)[-1500:]}

    state = {"repo": repo, "branch": branch, "commit": commit, "deployed_at": time.time()}
    _save_state(state)
    return {"ok": True, **state}


def get_status():
    state = _load_state()
    ps = _podman("ps", "--filter", f"name=^{CONTAINER_NAME}$", "--format", "{{.Status}}", timeout=15)
    running = bool(ps.stdout.strip())
    return {**state, "container_running": running}


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
        self._json(404, {"error": "not found"})

    def log_message(self, fmt, *args):
        pass


def main():
    server = http.server.ThreadingHTTPServer(("0.0.0.0", RELAY_PORT), Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()
