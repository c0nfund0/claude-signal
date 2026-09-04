import json
import os
import urllib.error
import urllib.request

import boto3
from botocore.exceptions import ClientError

INSTANCE_ID_AI = os.environ["INSTANCE_ID_AI"]
INSTANCE_ID_PROXY = os.environ["INSTANCE_ID_PROXY"]
INSTANCE_ID_DEPLOY = os.environ["INSTANCE_ID_DEPLOY"]
STOP_SECRET = os.environ["STOP_SECRET"]

# Empty (the default) means the custom-domain feature is off - every request then
# falls through to the original behavior below, unchanged. See acm.tf / README's
# "Custom domain" section.
WEB_DOMAIN = os.environ.get("WEB_DOMAIN", "")
APP_DOMAIN = os.environ.get("APP_DOMAIN", "")
WEB_OPEN_SECRET = os.environ.get("WEB_OPEN_SECRET", "")

ec2 = boto3.client("ec2")


def _try_transition(action):
    """Start/stop calls throw IncorrectInstanceState while an instance is already
    transitioning (e.g. still "stopping" from a prior idle-shutdown) - that's not an
    error from the caller's perspective, just means the desired transition is already
    underway, so swallow it and let /status reflect the real state instead."""
    try:
        action()
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "IncorrectInstanceState":
            raise


HTML_PAGE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Claude Signal</title>
<style>
  body { font-family: system-ui, sans-serif; background: #111; color: #eee; display: flex;
         align-items: center; justify-content: center; height: 100vh; margin: 0; }
  .card { text-align: center; }
  .row { font-size: 1.1rem; font-family: monospace; margin-top: 0.8rem; }
  .state { color: #9ad; }
</style>
</head>
<body>
  <div class="card">
    <h1>Claude Signal</h1>
    <p class="state" id="state">Starting servers...</p>
    <p class="row" id="proxy"></p>
    <p class="row" id="ai"></p>
    <p class="row" id="deploy"></p>
  </div>
  <script>
    async function poll() {
      try {
        const res = await fetch("/status");
        const data = await res.json();
        document.getElementById("proxy").textContent =
          "proxy: " + data.proxy.state + (data.proxy.public_ip ? " (" + data.proxy.public_ip + ")" : "");
        document.getElementById("ai").textContent =
          "ai: " + data.ai.state + (data.ai.private_ip ? " (" + data.ai.private_ip + ", ssh via proxy)" : "");
        document.getElementById("deploy").textContent =
          "deploy: " + data.deploy.state + (data.deploy.private_ip ? " (" + data.deploy.private_ip + ", ssh via proxy)" : "");
        if (data.proxy.state === "running" && (data.ai.state === "running" || data.deploy.state === "running")) {
          document.getElementById("state").textContent = "Servers are up.";
          return;
        }
      } catch (e) {
        document.getElementById("state").textContent = "Waiting for servers...";
      }
      setTimeout(poll, 3000);
    }
    poll();
  </script>
</body>
</html>
"""


# Served only on WEB_DOMAIN (see _handle) - starts proxy+deploy like HTML_PAGE, but
# once both are up it also drives the auto-open call and redirects the browser
# straight to the live site, instead of just showing status text.
AUTO_HTML_PAGE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Claude Signal</title>
<style>
  body { font-family: system-ui, sans-serif; background: #111; color: #eee; display: flex;
         align-items: center; justify-content: center; height: 100vh; margin: 0; }
  .card { text-align: center; }
  .row { font-size: 1.1rem; font-family: monospace; margin-top: 0.8rem; }
  .state { color: #9ad; }
</style>
</head>
<body>
  <div class="card">
    <h1>Claude Signal</h1>
    <p class="state" id="state">Starting the server...</p>
    <p class="row" id="proxy"></p>
    <p class="row" id="deploy"></p>
  </div>
  <script>
    let opening = false;
    async function tryOpen() {
      if (opening) return;
      opening = true;
      try {
        const res = await fetch("/open", { method: "POST" });
        if (res.ok) {
          document.getElementById("state").textContent = "Redirecting...";
          window.location.href = "https://APP_DOMAIN/";
          return;
        }
      } catch (e) {
        // not ready yet - fall through and retry on the next poll tick
      }
      opening = false;
    }
    async function poll() {
      try {
        const res = await fetch("/status");
        const data = await res.json();
        document.getElementById("proxy").textContent =
          "proxy: " + data.proxy.state + (data.proxy.public_ip ? " (" + data.proxy.public_ip + ")" : "");
        document.getElementById("deploy").textContent = "deploy: " + data.deploy.state;
        if (data.proxy.state === "running" && data.deploy.state === "running") {
          document.getElementById("state").textContent = "Opening the site...";
          tryOpen();
        }
      } catch (e) {
        document.getElementById("state").textContent = "Waiting for the server...";
      }
      setTimeout(poll, 3000);
    }
    poll();
  </script>
</body>
</html>
""".replace("APP_DOMAIN", APP_DOMAIN)


def _response(status_code, body, content_type="application/json"):
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": content_type},
        "body": body,
    }


def _describe_all():
    """One batched call for all three instances rather than three sequential
    describe_instances round-trips - fewer round-trips means less exposure to a
    single slow/throttled call blowing the Lambda's timeout budget, and no
    partial-failure window between the three."""
    resp = ec2.describe_instances(InstanceIds=[INSTANCE_ID_PROXY, INSTANCE_ID_AI, INSTANCE_ID_DEPLOY])
    by_id = {}
    for reservation in resp["Reservations"]:
        for instance in reservation["Instances"]:
            by_id[instance["InstanceId"]] = instance
    return by_id


def _get_status():
    by_id = _describe_all()
    proxy = by_id[INSTANCE_ID_PROXY]
    ai = by_id[INSTANCE_ID_AI]
    deploy = by_id[INSTANCE_ID_DEPLOY]
    return {
        "proxy": {
            "state": proxy["State"]["Name"],
            "public_ip": proxy.get("PublicIpAddress"),
        },
        "ai": {
            "state": ai["State"]["Name"],
            "private_ip": ai.get("PrivateIpAddress"),
        },
        "deploy": {
            "state": deploy["State"]["Name"],
            "private_ip": deploy.get("PrivateIpAddress"),
        },
    }


def _handle_open():
    """Called by AUTO_HTML_PAGE's own JS once /status shows both instances up.
    Confirms that server-side (never trusts the client's poll result alone), then
    forwards the request to the proxy's real HTTPS hostname - not its IP, so this
    rides on the same Let's Encrypt cert the browser will land on next, rather than
    a bare-IP connection with no hostname to verify."""
    status = _get_status()
    if status["proxy"]["state"] != "running" or status["deploy"]["state"] != "running":
        return _response(409, json.dumps({"error": "not running yet"}))
    req = urllib.request.Request(
        f"https://{APP_DOMAIN}/_claude-signal/open",
        method="POST",
        headers={"X-Web-Open-Secret": WEB_OPEN_SECRET},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
    except urllib.error.URLError as exc:
        # Most commonly: nginx/certbot on the proxy isn't fully up yet even though
        # EC2 reports "running" - the caller's poll loop just retries.
        return _response(502, json.dumps({"error": f"{type(exc).__name__}: {exc}"}))
    return _response(200, json.dumps({"opened": True}))


def handler(event, context):
    try:
        return _handle(event)
    except Exception as exc:  # noqa: BLE001
        # Otherwise an unhandled exception here just shows up as an opaque 500
        # with no detail anywhere reachable without CloudWatch access.
        return _response(500, json.dumps({"error": f"{type(exc).__name__}: {exc}"}))


def _handle(event):
    path = event.get("rawPath", "/")
    domain_name = (event.get("requestContext") or {}).get("domainName", "")
    is_web_domain = bool(WEB_DOMAIN) and domain_name == WEB_DOMAIN

    if path == "/status":
        return _response(200, json.dumps(_get_status()))

    if path == "/stop":
        headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
        if headers.get("x-stop-secret") != STOP_SECRET:
            return _response(403, json.dumps({"error": "forbidden"}))
        _try_transition(
            lambda: ec2.stop_instances(InstanceIds=[INSTANCE_ID_PROXY, INSTANCE_ID_AI, INSTANCE_ID_DEPLOY])
        )
        return _response(200, json.dumps({"stopping": True}))

    if path == "/web/stop":
        # Mirrors /web (starts proxy+deploy only, leaves ai alone) but in the other
        # direction - same secret requirement as /stop, since stopping is the more
        # disruptive direction (kills the Signal bridge along with it, since that
        # runs on the proxy too - same as an idle auto-stop already does).
        headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
        if headers.get("x-stop-secret") != STOP_SECRET:
            return _response(403, json.dumps({"error": "forbidden"}))
        _try_transition(lambda: ec2.stop_instances(InstanceIds=[INSTANCE_ID_PROXY, INSTANCE_ID_DEPLOY]))
        return _response(200, json.dumps({"stopping": True}))

    if path == "/open" and is_web_domain:
        return _handle_open()

    if is_web_domain:
        # web_domain's whole purpose is the one-link "view the site" flow: start
        # proxy + deploy (not ai - same as /web below) and serve the page whose JS
        # opens the gate and redirects, instead of just showing status text. No
        # secret required, same posture as /web and the default route always had.
        _try_transition(lambda: ec2.start_instances(InstanceIds=[INSTANCE_ID_PROXY, INSTANCE_ID_DEPLOY]))
        return _response(200, AUTO_HTML_PAGE, content_type="text/html")

    if path == "/web":
        # Starts the proxy + deploy instances only, not ai - for viewing/managing a
        # previously-deployed site without needing Claude Code itself running. No
        # secret required, same as the default start route.
        _try_transition(lambda: ec2.start_instances(InstanceIds=[INSTANCE_ID_PROXY, INSTANCE_ID_DEPLOY]))
        return _response(200, HTML_PAGE, content_type="text/html")

    # Any other path (including "/") starts proxy + ai and serves the polling page.
    _try_transition(lambda: ec2.start_instances(InstanceIds=[INSTANCE_ID_PROXY, INSTANCE_ID_AI]))
    return _response(200, HTML_PAGE, content_type="text/html")
