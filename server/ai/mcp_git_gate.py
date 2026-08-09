#!/usr/bin/env python3
"""Minimal hand-rolled MCP stdio server (same pattern as mcp_url_gate.py, no SDK)
exposing two tools: request_git_push and request_repo_create. Both go through
approval_daemon's generic /request-git-action gate on the proxy - same yes/no
Signal UX as request_url_access, but this daemon never touches the Squid
allowlist for these.

`git push` itself is blocked from Claude's direct Bash access (see
CLAUDE_ALLOWED_TOOLS / disallowed_tools in claude_wrapper.py) specifically so
this tool is the only path to actually pushing - Claude can `git add`/`commit`/
`diff`/`pull` etc. freely via Bash, just not `push`.

GITHUB_TOKEN deliberately does NOT live here (or anywhere else this process, or
therefore Claude, can read it) - confirmed live that `claude mcp add --env`
persists whatever it's given in plaintext in ~/.claude.json, which is inside the
container's own bind-mounted home directory and trivially readable by the same
`coder` user Claude runs as. That would let Claude read the token directly and
create repos via the GitHub API on its own, bypassing this entire approval gate.
So repo creation is a relay, not a local action: this tool only ever asks for
approval and, once approved, tells approval_daemon (on the proxy, which DOES
hold the token, in a place Claude has no access to at all) to actually create
the repo - the same pattern already used for deploy triggering, and for the
same reason (an action that must happen somewhere Claude can't reach).
"""
import json
import os
import subprocess
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

# Enforced here, not just prompted - a tool-level check can't be talked out of by
# a persona or a convincing-sounding justification the way a system-prompt
# instruction can.
PROTECTED_BRANCHES = {"main", "master", "trunk"}


def _call(method, path, payload=None, timeout=15):
    # Default (15s) is right for the fast create/poll calls. /deploy-trigger and
    # /repo-create-trigger block on a server-side action with its own much longer
    # timeout (approval_daemon.py: 600s for the deploy relay, 30s for the GitHub
    # API call) - callers of those two MUST pass a timeout that exceeds the
    # server-side one, or this client gives up while the server-side action is
    # still legitimately in progress. Confirmed live: a real deploy (image pull +
    # build + run) routinely takes well over 15s.
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE_URL + path, data=data, method=method,
        headers={"Authorization": f"Bearer {RELAY_SECRET}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def request_approval(kind, summary):
    """Shared create-then-poll flow for both tools. Returns (outcome, req_id):
    outcome is "approved"/"denied"/"timeout"/"error:...". req_id is returned even
    on success so a caller that needs a follow-up call tied to this same approval
    (deploy) can use it - approval_daemon only allows one deploy-trigger per
    approved request id."""
    try:
        resp = _call("POST", "/request-git-action", {"kind": kind, "summary": summary})
    except urllib.error.URLError as exc:
        return f"error:Could not reach the approval service: {exc}", None
    req_id = resp["id"]
    deadline = time.time() + TIMEOUT_SECONDS
    while time.time() < deadline:
        time.sleep(POLL_INTERVAL_SECONDS)
        try:
            status = _call("GET", f"/request-git-action/{req_id}")["status"]
        except urllib.error.URLError:
            continue
        if status in ("approved", "denied"):
            return status, req_id
    return "timeout", req_id


def do_git_push(repo_path, branch):
    result = subprocess.run(
        ["git", "-C", repo_path, "push", "-u", "origin", branch],
        capture_output=True, text=True, timeout=120,
    )
    output = (result.stdout + result.stderr).strip()
    if result.returncode != 0:
        return f"Push failed (exit {result.returncode}):\n{output[-1000:]}"
    return f"Pushed to {branch}:\n{output[-1000:]}" if output else f"Pushed to {branch}."


def trigger_deploy(req_id, deploy_repo, branch):
    """Relays an already-approved deploy through approval_daemon - the ai instance
    has no network path to the deploy instance itself, only the proxy does."""
    try:
        resp = _call(
            "POST", "/deploy-trigger", {"id": req_id, "repo": deploy_repo, "branch": branch},
            timeout=620,  # exceeds approval_daemon's own 600s deploy-relay timeout
        )
    except urllib.error.HTTPError as exc:
        # Genuine relay-level failure now (deploy_wrapper.py itself always
        # returns 200) - e.g. approval_daemon rejecting the request outright.
        body = exc.read().decode(errors="replace")
        return f"Deploy failed: {body[:1500]}"
    except urllib.error.URLError as exc:
        return f"Deploy failed: could not reach the approval service: {exc}"
    if not resp.get("ok"):
        # 1500, not 500 - matches deploy_wrapper.py's own bound on the build
        # log it returns, so this doesn't truncate further and lose the actual
        # failure line (confirmed live: build error text easily exceeds 500
        # chars, e.g. a long signed CDN URL in a registry-pull failure).
        return f"Deploy failed at stage '{resp.get('stage', '?')}': {resp.get('error', '?')[:1500]}"
    return f"Deployed {resp['repo']}@{resp['branch']} ({resp['commit']})."


def request_git_push(repo_path, branch, summary, deploy_repo=""):
    if branch.strip().lower() in PROTECTED_BRANCHES:
        return (
            f"Refusing: '{branch}' is a protected branch (main/master/trunk). "
            "Push to a feature branch instead - that's the policy here, not something "
            "any approval can override."
        )
    # deploy_repo set -> one combined approval covers both push and deploy, per
    # policy (the user should only be asked once for "push and deploy this",
    # not twice in a row for the same change).
    kind = "deploy" if deploy_repo else "git_push"
    approval_summary = f"repo: {repo_path}\nbranch: {branch}\n{summary}"
    if deploy_repo:
        approval_summary += f"\nwill also deploy: {deploy_repo}@{branch}"
    outcome, req_id = request_approval(kind, approval_summary)
    if outcome == "approved":
        try:
            push_result = do_git_push(repo_path, branch)
        except Exception as exc:  # noqa: BLE001
            return f"Approved, but the push itself failed: {exc}"
        if not deploy_repo:
            return push_result
        deploy_result = trigger_deploy(req_id, deploy_repo, branch)
        return f"{push_result}\n\n{deploy_result}"
    if outcome == "denied":
        return "Denied by the user. Nothing was pushed" + (" or deployed." if deploy_repo else ".")
    if outcome.startswith("error:"):
        return outcome[len("error:"):]
    return f"Timed out after {TIMEOUT_SECONDS}s waiting for approval. Nothing was pushed" + (
        " or deployed." if deploy_repo else "."
    )


def trigger_repo_create(req_id, name, description):
    """Relays an already-approved repo creation through approval_daemon, which
    holds the GitHub token itself - this process never sees it. Same one-shot-
    per-approved-request-id pattern as trigger_deploy."""
    try:
        resp = _call(
            "POST", "/repo-create-trigger", {"id": req_id, "name": name, "description": description},
            timeout=40,  # exceeds approval_daemon's own 30s GitHub-API-call timeout
        )
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        return f"Repo creation failed: {body[:500]}"
    except urllib.error.URLError as exc:
        return f"Repo creation failed: could not reach the approval service: {exc}"
    if not resp.get("ok"):
        return f"Repo creation failed: {resp.get('error', '?')[:500]}"
    return f"Created private repo: {resp['ssh_url']}"


def request_repo_create(name, description):
    approval_summary = f"repo name: {name}\ndescription: {description or '(none)'}\nvisibility: private"
    outcome, req_id = request_approval("repo_create", approval_summary)
    if outcome == "approved":
        return trigger_repo_create(req_id, name, description)
    if outcome == "denied":
        return "Denied by the user. No repo was created."
    if outcome.startswith("error:"):
        return outcome[len("error:"):]
    return f"Timed out after {TIMEOUT_SECONDS}s waiting for approval. No repo was created."


TOOLS = [
    {
        "name": "request_git_push",
        "description": (
            "Ask the user, over Signal, for permission to `git push` a branch. Blocks "
            "until they reply yes or no, or times out after 10 minutes; performs the "
            "actual push itself once approved (you don't need to run `git push` "
            "separately - it's blocked from direct Bash access on purpose). Refuses "
            "outright, with no approval round-trip, if the branch is main/master/trunk - "
            "push to a feature branch instead. Pass deploy_repo to also deploy the pushed "
            "branch to the deploy/web instance under one combined approval (a single "
            "yes/no covers push+deploy together - never ask for two separate approvals "
            "for the same change)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo_path": {"type": "string", "description": "Local path to the git repo, e.g. /home/coder/repos/myproject"},
                "branch": {"type": "string", "description": "Branch to push (must not be main/master/trunk)"},
                "summary": {"type": "string", "description": "Short human-readable summary of what this push contains, shown to the user"},
                "deploy_repo": {
                    "type": "string",
                    "description": (
                        "Optional: the repo's short name under c0nfund0 (e.g. 'myproject'). "
                        "If set, the pushed branch is also deployed (clean rebuild + run on "
                        "the deploy instance) as part of this same approval."
                    ),
                },
            },
            "required": ["repo_path", "branch", "summary"],
        },
    },
    {
        "name": "request_repo_create",
        "description": (
            "Ask the user, over Signal, for permission to create a new GitHub repository "
            "under the c0nfund0 account. Always created private. Blocks until they reply "
            "yes or no, or times out after 10 minutes; creates the repo itself once "
            "approved."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Repository name"},
                "description": {"type": "string", "description": "Short repository description"},
            },
            "required": ["name"],
        },
    },
]


def handle(msg):
    method = msg.get("method")
    msg_id = msg.get("id")

    if method == "initialize":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {
            "protocolVersion": msg.get("params", {}).get("protocolVersion", "2024-11-05"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "claude-signal-git-gate", "version": "0.1.0"},
        }}
    if method == "notifications/initialized":
        return None
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = msg.get("params", {})
        name = params.get("name")
        args = params.get("arguments", {})
        try:
            if name == "request_git_push":
                text = request_git_push(
                    args.get("repo_path", ""), args.get("branch", ""), args.get("summary", ""),
                    args.get("deploy_repo", ""),
                )
            elif name == "request_repo_create":
                text = request_repo_create(args.get("name", ""), args.get("description", ""))
            else:
                return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": "unknown tool"}}
        except Exception as exc:  # noqa: BLE001
            text = f"Error: {exc}"
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"content": [{"type": "text", "text": text}]}}
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
