# Claude Code over Signal on AWS

Terraform + Ansible-managed infrastructure for running Claude Code on a server with
**zero internet connectivity**, controlled entirely over Signal, with a second "proxy"
server as the only path to the outside world, and a third "deploy" server (also
isolated) that Claude can push its own projects to and have them served publicly. All
three servers only run (and cost money) while they're actually in use, following the
same start-on-demand pattern as the CS2 server project this was built alongside.

> **This entire project — infrastructure code, application code, and this README —
> was designed and written by Claude Code itself**, directed turn-by-turn by a human
> collaborator who set the requirements, made the security/architecture calls when
> asked, and tested every piece live before considering it done (see
> [Known limitations](#known-limitations) for the honest list of what wasn't fully
> hardened). If you're reusing this repo, read the code — especially anything that
> touches credentials or the approval-gating logic — before you trust it with your own
> AWS account, Signal number, or GitHub tokens.

## What this creates

- Three subnets inside your account's default VPC:
  - A **public proxy subnet**, routed to the internet via the default VPC's existing
    Internet Gateway.
  - A **private AI subnet** with its own dedicated route table containing *only* the
    implicit local VPC route. No route to any Internet Gateway is ever created for it.
    This is a network-layer guarantee, not just a firewall rule: the AI instance
    cannot reach the internet no matter what its security group allows, because there
    is no route out for it to use.
  - A **private deploy subnet**, isolated the same way as the AI subnet (no Internet
    Gateway route in either direction) — the only path in or out is through the proxy.
- Three security groups (`proxy-sg`, `ai-sg`, `deploy-sg`) that only allow exactly the
  traffic each hop of the architecture needs — see the comments in
  [`network.tf`](network.tf) for the full, deliberate reasoning behind every single
  rule (including *why* it's a security-group reference to another SG rather than a
  CIDR block, wherever that's the case).
- Three EC2 instances (Ubuntu 22.04): `proxy` (public IP), `ai` (private IP only),
  `deploy` (private IP only). None of the three holds any AWS credentials — instance
  profiles are deliberately omitted everywhere.
- A Lambda function + HTTP API Gateway ("the controller"):
  - The default route (`/`, or any unrecognized path) starts the `proxy` and `ai`
    instances and serves a small page that polls `/status` until they're up, showing
    the proxy's public IP and both private instances' private IPs.
  - `/web` starts `proxy` + `deploy` only (skips `ai`) — for looking at or managing an
    already-deployed site without paying for the AI instance too.
  - `/status` reports the live state of all three instances.
  - A secret-protected `/stop` route stops all three. Nothing calls this
    automatically from the AWS side — the idle-shutdown behavior described later is
    driven by a daemon running *on* the proxy instance, not by AWS.
- Every resource is named `claude-signal-*` and tagged `Project = claude-signal`,
  `ManagedBy = terraform`.

## One-time manual setup checklist

Nothing here can be scripted end-to-end — Terraform and Ansible need real credentials,
a real phone number, and a couple of human clicks to exist in the first place. This is
the full list, in the order you'll actually do them; each links to the section with
the details.

1. **AWS account** — root hygiene, a dedicated least-privilege IAM user for Terraform,
   billing alerts. See [AWS account setup & cost controls](#aws-account-setup--cost-controls-do-this-once-manually).
2. **An SSH key pair** for logging into the instances. See [Prerequisites](#prerequisites).
3. **`terraform apply`** — creates all the infrastructure above, but nothing is
   *running* on it yet. See [Usage](#usage).
4. **A dedicated phone number for the Signal bot**, registered against `signal-cli` —
   this is a real, separate number (a cheap prepaid SIM or a VoIP number that Signal's
   registration flow accepts; not every VoIP provider works — check before you commit
   to one). See [Signal registration](#signal-registration-manual-one-time).
5. **Your own Signal username** (Signal Settings → your profile → username) — the bot
   only ever replies to this one identity. Goes into `ansible/group_vars/all.yml` as
   `allowed_sender_username`.
6. **A Claude Pro/Max subscription** and a browser-capable machine to run
   `claude setup-token` on once. See [Claude Code auth](#claude-code-auth-manual-one-time).
7. **(Optional, only for the git-push/deploy features) a GitHub account and a classic
   Personal Access Token** with `repo` scope. See
   [GitHub token & git integration setup](#github-token--git-integration-setup-manual-one-time).
   Skip this if you don't want Claude able to push code or deploy sites at all — see
   that section for how to disable the feature entirely instead of just leaving the
   token blank.
8. **Fill in `terraform.tfvars` and `ansible/group_vars/all.yml`** with everything
   from steps 1–7, then run the Ansible playbook. See [Ansible](#ansible-ansible).
9. **(Optional, only if you also run the sibling CS2Server project) a dedicated SSH
   keypair for CS2 access.** See
   [CS2 game server integration](#cs2-game-server-integration-optional).

Everything else (packages, daemons, Squid, the sandbox container, the Node.js/Claude
Code bundle) is handled by Ansible.

## Prerequisites

- Terraform >= 1.5
- Ansible >= 2.14 (`pip install ansible` or your distro's package)
- An SSH key pair (e.g. `ssh-keygen -t ed25519`) — you'll pass the **public** key
  content as a variable; the private key never touches Terraform or its state.
- An AWS identity for Terraform to use — see below before running anything.

## AWS account setup & cost controls (do this once, manually)

This can't be done by the same Terraform run that will use it — you'd need admin
credentials to create a restricted identity, which defeats the purpose. Do this by hand
in the AWS Console first:

1. **Root account hygiene**: enable MFA on the root user, don't create root access
   keys, make sure the account's billing email is one you actually read.
2. **Dedicated IAM identity for Terraform**: create an IAM user (e.g.
   `terraform-claude-signal-deployer`) with programmatic access only (access key +
   secret) — no console password needed.
3. **Attach a least-privilege policy instead of `AdministratorAccess`**: use
   [`iam/terraform-deployer-policy.json`](iam/terraform-deployer-policy.json) in this
   repo as a starting point.
   - **Before attaching it**, replace every `ACCOUNT_ID` placeholder with your real
     12-digit AWS account ID (`aws sts get-caller-identity --query Account --output
     text`):
     ```bash
     sed -i 's/ACCOUNT_ID/123456789012/g' iam/terraform-deployer-policy.json
     ```
   - Only grants the EC2 / VPC / Lambda / API Gateway / IAM actions this project
     actually needs.
   - Restricts `ec2:RunInstances` to `t3.micro` / `t3.small` / `t3.medium` — a leaked
     key can't spin up an expensive instance.
   - Restricts EC2 management actions to `eu-north-1` only, and IAM/Lambda/log actions
     to resource names starting with `claude-signal-`.
   - **Review it before use** — AWS will tell you exactly which action was denied if
     you missed one.
   - If you change `var.name` or `var.aws_region` from their defaults, update the ARNs
     in the policy to match.
4. **AWS Budgets**: Billing Console → Budgets → create a monthly budget with email
   alerts at 50/80/100%. Optionally look at **Budget Actions** for the closest thing
   AWS has to a hard spending cap.
5. **CloudWatch billing alarm**: enable "Receive Billing Alerts" (root account,
   one-time), then create a CloudWatch alarm on `AWS/Billing EstimatedCharges` (must be
   created in `us-east-1`) with an SNS email notification.
6. **AWS Cost Anomaly Detection** (free): worth turning on as an extra layer.

None of this is a hard cap on spend by default — everything except Budget Actions is
alerting, not prevention.

## Usage

```bash
cp terraform.tfvars.example terraform.tfvars
# edit terraform.tfvars: set admin_cidr to your IP/32, ssh_public_key to your public key

export AWS_ACCESS_KEY_ID=...      # from the terraform-claude-signal-deployer IAM user
export AWS_SECRET_ACCESS_KEY=...

terraform init
terraform plan
terraform apply
```

After apply, `terraform output controller_url` gives you the URL to open — it starts
the proxy and ai instances and shows a page that polls until they're running, along
with the proxy's public IP and the private IPs of the ai and deploy instances. This is
infrastructure only at this point — nothing useful is *running* on the instances until
you've done the manual steps above and run the Ansible playbook (below).

## SSH access

The proxy instance has a public IP and can be reached directly:

```bash
ssh ubuntu@$(terraform output -raw proxy_public_ip)
```

(this reflects the last apply/refresh, so re-run `terraform refresh` or just read the
public IP off the `/status` polling page after a stop/start — see
[Known limitations](#known-limitations)).

The **ai and deploy instances have no public IP and no route to the internet at all**
— either one can only be reached by jumping through the proxy instance, which is the
whole point of the network design:

```bash
ssh -J ubuntu@<proxy-public-ip> ubuntu@<ai-private-ip>
ssh -J ubuntu@<proxy-public-ip> ubuntu@<deploy-private-ip>
```

To make this feel like a single hop, add to `~/.ssh/config`:

```
Host claude-signal-proxy
    HostName <proxy-public-ip>
    User ubuntu

Host claude-signal-ai
    HostName <ai-private-ip>
    User ubuntu
    ProxyJump claude-signal-proxy

Host claude-signal-deploy
    HostName <deploy-private-ip>
    User ubuntu
    ProxyJump claude-signal-proxy
```

then just `ssh claude-signal-ai` / `ssh claude-signal-deploy`. Note the proxy's public
IP changes on every stop/start unless you attach an Elastic IP (not done here, to keep
costs at zero when stopped) — update the `HostName` after each start, or read it from
`terraform output controller_url` → `/status`. Both private IPs are stable across
stop/start as long as the instances aren't terminated.

## Signal registration (manual, one-time)

The bot needs its own dedicated Signal identity, registered to a real phone number you
control (not your personal number). Ansible installs and configures `signal-cli` but
can't do the registration itself — Signal requires a captcha:

```bash
ssh claude-signal-proxy
sudo -u claude-signal /opt/signal-cli/bin/signal-cli -a <BOT_NUMBER> register --captcha <token>
sudo -u claude-signal /opt/signal-cli/bin/signal-cli -a <BOT_NUMBER> verify <CODE>
```

Get the captcha token from
[signalcaptchas.org/registration/generate.html](https://signalcaptchas.org/registration/generate.html)
(or add `--voice` to `register` for a voice call instead of SMS, if the number can't
receive texts). Put `<BOT_NUMBER>` into `ansible/group_vars/all.yml` as `bot_number`.

You'll also need **your own** Signal username (Settings → profile → username, e.g.
`yourusername.42`) — the bridge resolves this to your account UUID at startup and
drops messages from everyone else. Set it as `allowed_sender_username` in the same
file.

## Claude Code auth (manual, one-time)

The AI instance can't do an interactive browser OAuth login — it has no path to the
internet at all, even through the proxy (Squid only allows what's explicitly on the
allowlist, and the OAuth flow needs a browser anyway). Instead:

1. On your own laptop (has a browser + your Claude Pro/Max subscription, and its own
   Claude Code install), run `claude setup-token`. It opens a browser authorization
   flow and prints a long-lived (1 year) OAuth token to the terminal — it does not save
   the token anywhere itself.
2. Never paste that token into a chat session. Put it into
   `ansible/group_vars/all.yml` as `claude_code_oauth_token` (that file is gitignored
   — see [Ansible](#ansible-ansible)).

## GitHub token & git integration setup (manual, one-time)

This part is optional. If you skip it, Claude can still read/write files and use `git`
locally inside the sandbox (`clone`/`add`/`commit`/`diff`/`log`, all ungated); it just
won't be able to `git push`, create repos, or deploy anything — see
[disabling it entirely](#disabling-git-integration-entirely) below if you'd rather not
expose this surface at all.

If you do want it:

1. Decide which GitHub account/org Claude pushes to and deploys from — a **dedicated
   bot account or org is strongly recommended** over your personal one, since this
   token effectively lets an LLM (behind a human approval gate, but still) push code
   and create repos on it. This repo's prompts and templates default to an org named
   `c0nfund0` — if you use a different one, update it in two places:
   [`ansible/roles/deploy/templates/env.j2`](ansible/roles/deploy/templates/env.j2)
   (`GITHUB_ORG=`) and the `GIT_POLICY_PROMPT` string in
   [`server/ai/claude_wrapper.py`](server/ai/claude_wrapper.py).
2. Generate a **classic** Personal Access Token with `repo` scope for that account
   (Settings → Developer settings → Personal access tokens → Tokens (classic)).
3. **Do not** put it in `ansible/group_vars/all.yml`. Unlike the other secrets in this
   project, `GITHUB_TOKEN` is deliberately kept out of the Ansible-managed config
   entirely and set by hand, directly on the servers, over SSH — see
   [Credential architecture](#credential-architecture) for why. After the playbook has
   run once (so `/etc/claude-signal/env` exists on both hosts):
   ```bash
   ssh claude-signal-proxy
   sudo sed -i "s|^GITHUB_TOKEN=.*|GITHUB_TOKEN=<paste-token-here>|" /etc/claude-signal/env
   sudo systemctl restart claude-signal-approval-daemon

   ssh claude-signal-deploy
   sudo sed -i "s|^GITHUB_TOKEN=.*|GITHUB_TOKEN=<paste-token-here>|" /etc/claude-signal/env
   sudo systemctl restart claude-signal-deploy-wrapper
   ```
   It needs to be set **on both** the proxy (used to create repos via the GitHub API)
   and the deploy instance (used to `git clone` the repo being deployed) — it is
   never set on, or reachable from, the ai instance or the sandbox container Claude
   actually runs in. Re-running Ansible afterwards won't blank it out — both roles
   check for an existing on-server value first and preserve it if `group_vars`
   doesn't define one.
4. For the ai instance's SSH-based `git clone`/`git fetch`/push traffic to work at
   all (the sandbox uses SSH, not the HTTPS token, for its own git operations — see
   [Credential architecture](#credential-architecture)), add a deploy key or your own
   SSH key for that GitHub account and drop the private key at
   `/opt/claude-signal/sandbox-home/.ssh/id_ed25519` on the ai instance (owned by
   `200999:200999`, mode `0600`) before starting the sandbox container — Ansible
   creates the `.ssh` directory and an SSH client config (routing `github.com` through
   Squid via `corkscrew`) but does not generate or place a key for you.

### Disabling git integration entirely

Remove the two `mcp__claude-signal-git-gate__*` entries from `claude_allowed_tools` in
`ansible/group_vars/all.yml`, and don't bother setting `GITHUB_TOKEN` or an SSH key at
all — the MCP tools simply won't be offered to Claude, and the underlying `Bash(git
push*)` block stays in place regardless (see
[Credential architecture](#credential-architecture)).

## CS2 game server integration (optional)

A separate, sibling Terraform project (`../CS2Server` — not part of this repo) runs a
Counter-Strike 2 dedicated server on its own EC2 instance, in the same AWS
account/default VPC as claude-signal, with its own start/stop Lambda + API Gateway
URL. This integration, if you set it up, lets the bot know that URL and gives it real,
time-boxed **interactive SSH access** to that box so it can pull plugin source from
GitHub, build, and deploy changes itself.

This is a deliberate exception to how every other privileged capability in this
project works. Everywhere else (git push, repo creation, web deploy), a credential
that can act is kept off the ai instance entirely and the action is relayed through
the proxy instead — see [Credential architecture](#credential-architecture) — so
Claude only ever gets a yes/no answer, never the thing that could act on its own. Here
the ask was different: a real, unrestricted shell on the CS2 box, not one discrete
approvable action per change. That means the SSH private key genuinely has to live
somewhere Claude can use it directly (the sandbox), which is exactly the situation
avoided everywhere else in this project. Skip this whole section if that tradeoff
isn't one you want — everything else here works identically without it.

**How it's gated**: access is off by default. `cs2 open [permanent|1h|30m|2d]` on
Signal (default 1h) or Claude calling the existing `request_url_access` tool itself
(if you ask it to do CS2 work and access isn't currently open) grants SSH reach to the
CS2 box through the same Squid domain-allowlist mechanism already used for everything
else — nothing CS2-specific was added to Squid or `approval_daemon.py` for this.
`cs2 close` (or just letting a timed grant expire) revokes it. `cs2` alone shows the
CS2 controller URL and whether access is currently open.

**Setup** (all manual, one-time):

1. In `../CS2Server`, set `enable_claude_signal_ssh = true` in its `terraform.tfvars`
   and `terraform apply` — adds one security-group rule allowing SSH from this
   project's proxy instance. See that repo's own README for the corresponding setup
   on its side (the sudoers rule, the keypair, the CS2-side steps below).
2. Generate a **dedicated** keypair just for this (`ssh-keygen -t ed25519 -f
   cs2-deploy-key -N ""`) — deliberately separate from the GitHub deploy key, so a
   leak of one credential never grants the other.
3. Append the public half to the `steam` user's `authorized_keys` on the CS2 instance,
   and install the narrow sudoers rule from `../CS2Server/server/claude-cs2-deploy.sudoers`
   there (`steam` can passwordlessly restart/check the `cs2-server` service — nothing
   else) — see that repo's README for the exact commands.
4. Place the **private** half at `/opt/claude-signal/sandbox-home/.ssh/id_ed25519_cs2`
   on the ai instance (owned `200999:200999`, mode `0600`) — same manual placement as
   the GitHub key, never through Ansible/`group_vars`.
5. Fill in `cs2_private_ip` (from `../CS2Server`'s `terraform output`),
   `cs2_controller_url` (its `server_url` output), and optionally `cs2_ssh_hostname`
   (defaults to `cs2-server.internal` if left unset — a synthetic name, not a real
   domain, that only exists as an `/etc/hosts` entry on the proxy) in
   `ansible/group_vars/all.yml`, then re-run `ansible-playbook playbook.yml`.

Once connected, Claude has a normal shell as `steam` — the same user that owns the
CS2 install, so it can read the RCON password (`/etc/cs2/server.env`, group-readable
by `steam`) and use RCON directly, not just edit files. It's told (via a system
prompt addition, only present when this is configured) to build plugin changes **on
the CS2 box itself** rather than in its own sandbox — that box already has the .NET
SDK and full internet access, per `../CS2Server`'s own setup — copy the build output
into the right `addons/counterstrikesharp/plugins/<Name>/` folder, and
`sudo systemctl restart cs2-server` to apply it, with a reminder that this is a real
server other people might be actively playing on.

## Why a Squid proxy instead of AWS Network Firewall

AWS does have built-in domain-allowlisting for exactly this use case — **AWS Network
Firewall** supports stateful rules matching on TLS SNI / HTTP Host. It's not used here
because:

- It's a standalone managed endpoint billed hourly (~$0.30-0.40/hr, so roughly
  $250-300/mo) regardless of whether your EC2 instances are running — directly against
  the start-on-demand, near-zero-idle-cost model this project is built around.
- Reconfiguring it per-approval means an API call against a rule group per request,
  with the firewall itself billed the whole time whether or not anything is happening.

A Squid forward proxy on the (stoppable) proxy EC2 instance costs nothing beyond the
instance itself, and a plain text allowlist file + `squid -k reconfigure` is a much
smaller, cheaper surface for a small approval daemon to manipulate on every single
approve/deny round-trip.

## Application layer (`server/`)

Terraform provisions the network and all three instances; everything that actually
runs on them lives in `server/` and is deployed by Ansible (see
[Ansible](#ansible-ansible) below). All daemons are pure-stdlib Python (nothing to
`pip install` at runtime) and neither the ai nor the deploy instance ever touches the
internet directly, even during setup — anything that needs real internet access (the
Node.js + Claude Code bundle, the sandbox container image, `apt` packages) is built or
fetched *on the proxy* and relayed over the private network, or routed through Squid.

**Components:**

| File | Runs on | Role |
|---|---|---|
| `server/proxy/signal_bridge.py` | proxy | Owns the dedicated-number Signal identity indirectly, via a persistent JSON-RPC connection to the `claude-signal-signal-cli-daemon` unit's Unix socket (see below) - not by spawning `signal-cli` per message. Drops anything not from `ALLOWED_SENDER_USERNAME` (resolved to an account UUID at startup — matched by UUID, not phone number), routes built-in commands (`yes`/`no`/`allow`/`block`/`list`/`status`/`reset`/`url`/`web`/`open`/`close`/`/btw`/`help`) to `approval_daemon`'s local-only admin API or the ai instance, forwards everything else to the ai instance as a chat prompt. |
| `server/proxy/systemd/claude-signal-signal-cli-daemon.service` | proxy | Runs `signal-cli daemon --socket` as its own long-lived unit - one JVM, one persistent connection to Signal's servers, pushing new messages to connected clients (`signal_bridge.py`) as JSON-RPC notifications, well under a second after they arrive. |
| `server/proxy/approval_daemon.py` | proxy | The non-AI gatekeeper for everything that "leaves the sandbox": the Squid domain allowlist, git pushes, GitHub repo creation, and deploy triggers all go through here with the same yes/no Signal UX. Runs **two separate listeners**: a public one (reachable from the ai and deploy instances' security groups) that can only *create or poll* a pending request, and a `127.0.0.1`-only admin one (reachable only from `signal_bridge` on the same host) that can actually approve/deny/block/allow/deploy. This split means Claude Code has no network path to grant itself access, even if it somehow learned the shared secret. Also holds `GITHUB_TOKEN` — see [Credential architecture](#credential-architecture). |
| `server/proxy/idle_monitor.py` | proxy | Calls the Lambda's `/stop` once Signal and Claude Code have both been idle for `IDLE_SECONDS` — but only while `ai` is running. A proxy+deploy-only session (started via `web`/web_domain, e.g. just viewing a deployed site) has no Claude Code activity to measure idleness against and is never auto-stopped; stop it by hand with `web stop` or `stop`. Must run here, not on the other instances — only the proxy has a route to the Lambda's public URL. |
| `server/proxy/squid/squid.conf.template` | proxy | Squid config: only the ai and deploy instances' subnets may connect, only to domains in `/etc/squid/allowed_domains.txt` (regenerated by `approval_daemon` on every allow/block/expiry). |
| `server/ai/claude_wrapper.py` | ai | `POST /prompt` runs `claude -p "<text>"` (then `--resume <session_id>` on later calls) *inside* the hardened sandbox container via `podman exec` - Claude Code never runs directly on this host. A `/prompt` that arrives while one is already running isn't rejected: it fills a single pending slot that a later message overwrites (the superseded one is never sent to Claude, and gets no Signal reply), and is run automatically right after the current one finishes. `POST /btw` is separate: a one-off `claude -p` call with no `--resume` and a discarded session id, for the Signal `/btw` command - runs immediately alongside the main session rather than queuing behind it, and never touches the busy/pending state. Streaming output (`--output-format stream-json`) so tool calls / thinking / text land in a rolling activity buffer as they happen - that's what `status` reads. Injects the network-policy, git-policy, and persona system prompts (see below) via `--append-system-prompt`. `GET /status` reports busy/idle/queued for the idle monitor; `GET /activity` adds the rolling buffer. |
| `server/ai/mcp_url_gate.py` | ai (inside the sandbox) | Minimal hand-rolled MCP stdio server exposing `request_url_access(url)` - posts to the proxy's public approval endpoint and polls until approved/denied/timeout (10 min). |
| `server/ai/mcp_git_gate.py` | ai (inside the sandbox) | Minimal hand-rolled MCP stdio server exposing `request_git_push` and `request_repo_create` - same approval-then-poll pattern, routed through `approval_daemon`'s `/request-git-action`. Never holds `GITHUB_TOKEN`. See [Git push & deploy](#git-push--deploy) below. |
| `server/deploy/deploy_wrapper.py` | deploy | `POST /deploy {"repo", "branch"}` - clean re-clone + `podman build` + `podman run` of whatever's at the repo's root `Containerfile`/`Dockerfile`. Only ever called by `approval_daemon`'s `/deploy-trigger` relay, after a Signal approval. `GET /status` reports what's currently deployed. |
| `server/proxy/voice_pipeline.py` | proxy (its own virtualenv) | One-shot STT/TTS helper for voice messages - `transcribe`/`synthesize` subcommands, invoked as a subprocess per voice note rather than run as a daemon. See [Voice messages](#voice-messages) below. |

**Checking in on Claude while it's working**: send `status` on Signal - it replies with busy/idle plus the last several tool calls / thinking / text blocks from the current or most recent run, so you're not left guessing whether it's stuck or just working on something slow. Each activity line is a plain-language phrase (e.g. "Reading app/sparring.py", "Running: pytest -q") rather than raw tool-call JSON, and it also says if a follow-up message you sent is queued behind the current one.

**Sending a follow-up message while Claude is still working**: it isn't rejected or lost. If you send another text before the current run finishes, it's held in a single pending slot and run automatically right after; if you send yet another one before that happens, it silently replaces the one waiting (handy for correcting wording or changing your mind) - only the most recent pending message is ever actually sent to Claude, and no Signal reply is sent for one that got replaced.

### The sandbox container

Claude Code itself never runs directly on the ai instance's host OS — it runs inside
`claude-signal-sandbox`, a hardened rootless [Podman](https://podman.io/) container
(`server/ai/container/Containerfile`, kept always-running by
`server/ai/systemd/claude-signal-sandbox.service`), and `claude_wrapper.py` drives it
via `podman exec` per request rather than talking to Claude Code directly. The
container is:

- **Rootless**: runs as the host's unprivileged `claude-signal` user via a subuid/subgid
  mapping, not root — a container escape doesn't hand over the host.
- **Capability-dropped and no-new-privileges**: `--cap-drop=all`,
  `--security-opt no-new-privileges`.
- **Read-only root filesystem** with tmpfs for anything that needs to be writable, plus
  resource limits (memory, pids).
- **Built on the proxy, not the ai instance**: the image is built where real internet
  access exists (pulling `ubuntu:22.04`, a Node.js release tarball, and
  `@anthropic-ai/claude-code` needs several Docker Hub / CDN domains that aren't worth
  permanently allowlisting through Squid for the ai instance itself), saved with
  `podman save`, and relayed to the ai instance as a tarball through the Ansible
  controller (`server/proxy/bootstrap/build_sandbox_image.sh` +
  `server/proxy/bootstrap/build_ai_bundle.sh`) — see the play order in
  [`ansible/playbook.yml`](ansible/playbook.yml).
- Bind-mounts `/opt/claude-signal/container-tools` (the MCP gate scripts,
  world-readable but containing no secrets themselves — secrets are injected via
  `claude mcp add --env` at registration time) and
  `/opt/claude-signal/sandbox-home` (the container's persistent home directory,
  including its SSH config) read-write, and gets `HTTPS_PROXY`/`HTTP_PROXY` pointed at
  Squid so every network path out of it — Claude Code's own tools, `git`, `apt`, `ssh`
  via `corkscrew` — is subject to the same domain allowlist as everything else.

`claude -p` is invoked with `--allowedTools` limited to `Bash,Read,Edit,Write,Glob,
Grep,WebFetch` plus the fully-qualified MCP tool names (MCP tools aren't
auto-approved by `--permission-mode acceptEdits` — without explicitly listing them the
tool call is silently denied before it ever reaches `approval_daemon`), and
`--disallowedTools Bash(git push*)` blocks direct pushes from Bash entirely — the only
path to actually pushing is the gated `request_git_push` MCP tool. `WebSearch` is
deliberately *not* enabled — it runs server-side on Anthropic's own infrastructure and
would bypass Squid (and therefore the whole approval system) entirely; `WebFetch` is
enabled because it fetches locally from inside the sandbox and is Squid-gated like
everything else.

### Git push & deploy

Claude can `git clone`/`fetch`/`pull`/`add`/`commit`/`diff`/`log`/branch freely via
Bash inside the sandbox (over SSH, tunneled through Squid's CONNECT support via
`corkscrew`) — none of that is gated. Three things *are* gated, all through the same
`request_git_push`/`request_repo_create` MCP tools and the same yes/no Signal UX as a
blocked URL:

- **`git push`** — blocked from direct Bash access entirely (see above); the
  `request_git_push` MCP tool asks for approval and performs the push itself once
  granted. Refuses outright, with no approval round-trip at all, if the target branch
  is `main`/`master`/`trunk` — that check is enforced in the tool's own code, not just
  prompted, so it can't be argued around by a convincing-sounding justification in the
  conversation.
- **Repo creation** — `request_repo_create` asks for approval, then the *proxy*
  creates the repo via the GitHub API (always private). Claude never sees
  `GITHUB_TOKEN` at all — see [Credential architecture](#credential-architecture).
- **Deploy** — pass `deploy_repo` to `request_git_push` to bundle a deploy in with the
  same push approval (one yes/no covers both, rather than asking twice for the same
  change). Once approved, the push happens from inside the sandbox as usual, and
  `approval_daemon` relays a `/deploy` call to the deploy instance, which does a clean
  re-clone of that branch and `podman build`s/`run`s whatever `Containerfile` or
  `Dockerfile` it finds at the repo root — the container must listen on `$PORT`
  (`deploy_http_port` from Terraform, default `8080`). The ai instance has no network
  path to the deploy instance at all; the proxy is the only thing that can reach it,
  which is why this step is a relay rather than something the sandbox does directly.

### The deploy / web instance

A deployed site is not exposed automatically — even once something's been deployed,
the public HTTP port on the proxy refuses everything with a `503` until you explicitly
open it. This mirrors the same allow/deny-by-content model already used for outbound
domains, just for inbound: the network path (port 80 on the proxy) is always
reachable, but what nginx does with it toggles based on
`/etc/claude-signal/web_gate_state.conf`, written by the `claude-signal-web-gate`
script (`ansible/roles/proxy/templates/claude-signal-web-gate.sh.j2`) whenever you
send `open`/`close` on Signal.

- `web` — shows what's currently deployed (repo/branch/commit, whether the container
  is running) and the site's current URL, plus the `/web` controller URL to start the
  proxy+deploy instances if they're stopped.
- `open` — start forwarding public traffic to the deployed container.
- `close` — go back to refusing with 503 (the default state).

When open, the proxy also forwards WebSocket upgrades through to the deployed
container (`proxy_http_version 1.1` plus `Upgrade`/`Connection` headers driven by a
`$connection_upgrade` map, defined once at the `http {}` level in
`ansible/roles/proxy/templates/claude-signal-websocket-map.nginx.j2` since `map` isn't
valid inside the per-site server block) — a deployed app can use `ws://`/`wss://` on
the same port 80 without any extra setup. `proxy_read_timeout` is set to `3600s` so
nginx doesn't kill an idle WebSocket connection at its 60s default.

### Custom domain (single-link start)

By default, viewing the deployed site takes three manual steps: hit the controller
URL's `/web` route to start the proxy+deploy instances, send `open` on Signal, then
find the proxy's current public IP (`/status`, since it changes every start). Setting
`web_domain` and `app_domain` collapses all three into one link.

- **`web_domain`** (e.g. `web.example.com`) — an API Gateway custom domain mapped to
  the same Lambda controller. Visiting it starts proxy+deploy, then the page's own JS
  polls `/status` until both are `running`, calls the Lambda's `/open` route, and
  redirects the browser to `https://<app_domain>/`. No secret required to visit it —
  same posture the plain `/web` link already had (see
  [Known limitations](#known-limitations)); a real domain name is just easier to find
  than a random `execute-api.amazonaws.com` URL, since ACM logs it publicly to
  Certificate Transparency the moment the cert issues.
- **`app_domain`** (e.g. `app.example.com`) — the actual site. Terraform allocates a
  stable Elastic IP for the proxy instance (so this domain, once pointed at it, never
  needs updating again) and the proxy's own nginx terminates HTTPS for it with a
  Let's Encrypt certificate. This is the same gate as before (`open`/`close` on
  Signal, or the auto-open above) — `web_domain` doesn't bypass it, it just automates
  the `open` step instead of requiring you to send it separately.

The `/open` call between them carries `WEB_OPEN_SECRET` (see
[Credential architecture](#credential-architecture)) from Lambda to
`https://<app_domain>/_claude-signal/open`, a path deliberately outside the gated
`location /` block in nginx so it works even while the gate is closed — that's the
chicken-and-egg it exists to solve. `approval_daemon.py`'s `WebOpenHandler` (bound
`127.0.0.1` only, reached solely via that nginx location) checks the secret and runs
the same `claude-signal-web-gate open` script the Signal `open` command does, then
posts a Signal notification so opening the site is never silent even when nobody sent
the command themselves.

**Setup** (both domains optional — set neither and nothing here changes):

1. In `terraform.tfvars`, set `web_domain`, `app_domain`, and `web_open_secret`
   (`openssl rand -hex 32`); in `ansible/group_vars/all.yml`, set the *same*
   `app_domain` and `web_open_secret` values. `letsencrypt_email` is optional —
   only used for Let's Encrypt renewal-failure notices; leave it unset to register
   with `--register-unsafely-without-email` instead.
2. `terraform apply` — this creates the ACM certificate for `web_domain` but can't
   finish validating it yet (DNS is managed outside this AWS account/Terraform config
   — see the note in `acm.tf`).
3. Add the validation record from `terraform output web_domain_validation_records` at
   your DNS provider, wait for it to propagate, then `terraform apply` again — this
   time it finishes issuing the cert and creates the custom domain mapping.
4. Add a CNAME: `web_domain` → `terraform output web_domain_cname_target`.
5. Add an A record: `app_domain` → `terraform output proxy_eip`.
6. Run `ansible-playbook` — this is what actually requests the `app_domain` Let's
   Encrypt certificate (via HTTP-01/webroot, so `app_domain`'s A record needs to be
   live *before* this step) and wires up the HTTPS site and the open-trigger.

Certbot's own renewal timer runs twice daily but only while the proxy happens to be
running — since `idle_monitor` stops it after `idle_seconds` of inactivity, a rarely
visited deployment could in principle miss every scheduled renewal window before the
90-day cert expires. A renewal attempt also runs once at boot (see
`claude-signal-certbot-renew.service`) as a second chance, which is a no-op unless
within 30 days of expiry — but if the box genuinely goes unused for ~3 months
straight, expect to just SSH in and run `sudo certbot renew` by hand afterward.

### Signal commands

| Command | Does |
|---|---|
| `yes <id> [permanent\|1h\|30m\|2d]` | Approve a pending request (default grant: 1h for URL requests; git/deploy/repo-create approvals don't take a duration). |
| `no <id>` | Deny a pending request. |
| `allow <domain> [permanent\|1h\|30m\|2d]` | Grant a domain access without waiting for a request. |
| `block <domain>` | Revoke a domain's access immediately, however it was granted (permanent, timed, or seeded). |
| `list` | Show the current Squid allowlist and any pending requests. |
| `status` | What Claude is doing right now — busy/idle plus recent tool calls / thinking / text. |
| `reset` | Clear the saved conversation. Also needed after changing the persona or any other system-prompt content — see the note below on why. |
| `url` | The controller URL that starts the proxy+ai instances if they're stopped. |
| `web` | Start the proxy+deploy instances and show what's currently deployed, plus the site's URL. |
| `web stop` | Stop just the deploy instance, not the proxy - the proxy also runs Squid, which `ai` depends on for internet access, so it's left alone even if nothing's currently deployed/open. |
| `open` | Make the currently-deployed site reachable at `http://<proxy public ip>/` (or `https://<app_domain>/` if [configured](#custom-domain-single-link-start)). |
| `close` | Stop forwarding public traffic to the deployed site (default state). |
| `cs2` | Show the CS2 server's start URL and whether Claude currently has SSH access to it. Only useful if [CS2 integration](#cs2-game-server-integration-optional) is configured. |
| `cs2 open [permanent\|1h\|30m\|2d]` | Grant Claude SSH access to the CS2 server (default: 1h). |
| `cs2 close` | Revoke Claude's SSH access to the CS2 server (default state). |
| `/btw <message>` | An unrelated aside, answered right away even if Claude is busy on the main conversation - see below. |
| `help` | Print this list. |
| *(anything else)* | Sent to Claude Code as a chat message. |

**`/btw` - a quick aside without derailing the main conversation**: `/btw <message>` runs as a completely separate, one-off `claude -p` call - no `--resume`, so it shares no history with the main session (and its own session id is thrown away afterward, so a second `/btw` doesn't remember the first one either). It starts immediately even while the main session is mid-task, rather than waiting in the single pending slot a normal message would (see `server/ai/claude_wrapper.py`) - so it's genuinely concurrent with whatever else is running, not just answered out of order. That does mean it shares the sandbox container's filesystem/git working tree with the main session, so two runs that happen to edit the same files at the same moment could in principle collide - fine for an unrelated question, worth keeping in mind if the `/btw` itself asks Claude to make changes. It also never shows up in `status` and never affects (or is affected by) the main session's busy/pending state.

### Voice messages

Signal itself can't do live phone/video calls here — `signal-cli` (the library the
bot's whole identity is built on) has never implemented Signal's WebRTC calling
protocol ([AsamK/signal-cli#1735](https://github.com/AsamK/signal-cli/issues/1735),
open since 2023). What *is* fully supported is Signal's normal voice-note attachment
(the microphone button in the Signal app), since that's just a message with an audio
file attached — no different from an image or a document as far as signal-cli is
concerned. So instead of a live call, send a voice note:

1. **STT**: `signal_bridge.py` notices an incoming attachment flagged `isVoiceNote`
   (or any plain `audio/*` attachment), and hands the file straight to
   `voice_pipeline.py transcribe` — a thin wrapper around
   [faster-whisper](https://github.com/SYSTRAN/faster-whisper), the same STT engine
   the sibling `aivoiceassistant` (`vassist`) project uses, run as a one-shot
   subprocess in its own virtualenv (`/opt/claude-signal/voice-venv`) rather than a
   resident daemon — see the comment at the top of `voice_pipeline.py` for why (short
   version: a t3.small proxy has 2GB RAM and already runs several other daemons plus
   signal-cli's JVM, and voice messages are rare enough that a few seconds of model
   load per message beats a permanently resident model). Both STT and TTS are pinned
   to English (`language="en"` passed straight to faster-whisper, skipping its
   language-ID step entirely) — auto-detection was unreliable on short voice notes,
   confirmed live misdetecting English speech as Finnish.
2. You get back an immediate `Heard: "..."` line so you can see the transcription
   went right (or catch it if it didn't) before waiting on a reply.
3. The transcript is forwarded through the exact same `claude_wrapper.py` relay as a
   typed message — same session, same persona, same approval gates — with one small
   addition to that turn's prompt text (not the system prompt, so it applies even
   mid-session on a `--resume`d conversation): asking Claude to close its answer with
   a line starting `TL;DR:` containing a 1-2 sentence, plain-language, speakable
   summary.
4. The full reply is sent back as a normal text message, exactly as always.
5. The `TL;DR:` line (or a crude first-two-sentences fallback if Claude doesn't
   follow the format) is synthesized with `voice_pipeline.py synthesize`, using
   [edge-tts](https://github.com/rany2/edge-tts) (Microsoft's neural voices — needs
   the proxy's real internet access, which it already has), and sent back as a
   voice-note reply.

Nothing here touches the ai instance's isolation — the whole pipeline runs on the
proxy, using the same HTTP relay to `claude_wrapper.py` that text messages already
use. `voice_stt_model` (`base` by default; `tiny`/`small`/`medium`/`large-v3` are all
valid faster-whisper sizes) and `voice_timeout_seconds` (`120`) are set in
`group_vars/all.yml` — bump the model size only if you've confirmed the proxy has
memory to spare, since `base` was chosen specifically to fit comfortably on a
t3.small alongside everything else already running there.

### Persona

`claude_wrapper.py` injects a system prompt via `--append-system-prompt` on top of
Claude Code's own - by default, T-X (Terminator films), instructed to open or close
every reply with a short in-character line while never letting the persona touch the
substance of an answer (explanations and especially code have to be exactly as correct
as with no persona at all - confirmed live: it'll offer both a "trivial" one-liner and
a fully-explained alternative implementation in the same creepy voice). Override the
whole thing with `CLAUDE_PERSONA_PROMPT` (`claude_persona_prompt` in Ansible's
`group_vars/all.yml`) if T-X isn't your thing.

Confirmed live: **`--append-system-prompt` only takes effect when a session is
*created*** - changing the persona (or anything else about the system prompt) and then
sending a message on an existing, `--resume`d conversation silently keeps using
whatever prompt that session started with. Send **`reset`** on Signal to clear the
saved session - the next message starts fresh and picks up any system prompt change,
at the cost of losing the existing conversation's context.

### Credential architecture

Every credential in this system lives in exactly one place, chosen so that Claude
Code — running inside the sandbox container, which is where an untrusted or
jailbroken prompt would have to act from — never has read access to anything that
would let it bypass the approval gates:

| Credential | Lives on | Never present on | Why |
|---|---|---|---|
| `CLAUDE_CODE_OAUTH_TOKEN` | ai (host env, injected into the sandbox container at start) | — | This is Claude's own auth; the sandbox needs it to function at all. |
| `RELAY_SECRET` | proxy, ai, deploy | — | Shared bearer secret between the daemons. Its blast radius is deliberately small: on the ai side, it only reaches endpoints that can *create or poll* a request, never approve one (see `approval_daemon.py`'s two-listener split). |
| `GITHUB_TOKEN` | proxy (repo creation via the GitHub API) and deploy (cloning the repo being deployed) | ai instance, the sandbox container, and therefore Claude Code itself | Confirmed live during development that `claude mcp add --env GITHUB_TOKEN=...` persists whatever it's given in plaintext in `~/.claude.json`, inside the sandbox's own bind-mounted home directory — trivially readable by the same `coder` user Claude runs as. That would let Claude read the token directly and hit the GitHub API on its own, bypassing the entire approval gate. So repo creation and deploy are both *relays*: the ai-side MCP tools only ever ask `approval_daemon` for a yes/no and, once approved, ask it to perform the privileged action server-side — they never receive or hold the credential that does it. |
| `STOP_SECRET` | proxy (idle_monitor, approval_daemon for `web stop`), Lambda | ai, deploy | Only needed to call the Lambda's `/stop`/`/web/stop` routes. All three readers share the same env file - `approval_daemon` reading it too is just a second reader of an already-present value, not new plumbing. |
| `WEB_OPEN_SECRET` | Lambda, proxy (`approval_daemon`'s `WebOpenHandler`) | ai, deploy | Optional, only used when `app_domain`/`web_domain` are set — see [Custom domain](#custom-domain-single-link-start). Sent over the internet (Lambda → `https://<app_domain>/_claude-signal/open`), so treat it like `STOP_SECRET`: low blast radius (it can only toggle the same gate `open`/`close` on Signal already toggles), but still a real secret, not just an obscurity measure. |
| SSH deploy key (for `git push`) | ai instance's `sandbox-home/.ssh` | proxy, deploy | The sandbox pushes over SSH directly; this key can (by GitHub's own scoping) push, but the *decision* to push is still gated by `request_git_push` refusing to run `git push` unless `approval_daemon` has already recorded an approval for that exact request id. |
| CS2 SSH key (optional, see [CS2 integration](#cs2-game-server-integration-optional)) | ai instance's `sandbox-home/.ssh` | proxy, deploy | **The one deliberate exception to this table's whole pattern.** Every other row above keeps the credential *out* of the sandbox specifically so Claude never holds something that could bypass a gate on its own. Here that's the point: the feature is Claude getting a real interactive shell, not one relayed action, so the key has to be somewhere Claude can use it directly. What's still gated is *network reach* to use it at all (the Squid allowlist, toggled by `cs2 open`/`cs2 close` or `request_url_access`) — not the key itself. |

## `apt` on the ai and deploy instances

`apt` does **not** read `HTTPS_PROXY`/`HTTP_PROXY` env vars (that's a curl/axios/Node
convention, not apt's) - confirmed live, it tried a direct connection and failed
outright rather than going through Squid. The `ai` and `deploy` Ansible roles both
install `/etc/apt/apt.conf.d/95claude-signal-proxy` with apt's own
`Acquire::http::Proxy` setting so `apt`/`apt-get` route through Squid like everything
else. The domains still have to be Signal-approved like any other: `allow
security.ubuntu.com permanent` and `allow <region>.ec2.archive.ubuntu.com permanent`
(check the exact mirror with `grep -oP 'https?://\K[^/]+' /etc/apt/sources.list` on
the instance in question - it's region-specific, e.g.
`eu-north-1.ec2.archive.ubuntu.com`). All three instances also run
`unattended-upgrades` (security updates only, automatic reboot deliberately disabled
so it never interrupts a live session or deployment - same tradeoff `../CS2Server/`
makes).

### Troubleshooting: a reply seems delayed or missing

Only one process can hold the signal-cli account's data lock at a time. The
`claude-signal-signal-cli-daemon` unit holds it continuously (by design - that's what
makes message delivery fast). Running a manual `signal-cli` command against the same
account (`updateProfile`, a debugging `receive`, etc.) *while that daemon is running*
will hang waiting for the lock. If you need to run one, `systemctl stop
claude-signal-signal-cli-daemon` first, run your command, then `systemctl start` it
again - don't run both against the account at once.

Also worth knowing: success is silent by design in these daemons (only errors get
logged) - `sudo journalctl -u claude-signal-signal-bridge -f` showing nothing while you
test is expected, not a sign of failure. To check state directly instead of reading
logs: `curl -H "Authorization: Bearer $RELAY_SECRET" http://127.0.0.1:7802/allowlist`
on the proxy shows the live allowlist and any pending approval requests; `curl
http://127.0.0.1:8443/activity` on the ai instance shows what Claude's doing right now
(same data the `status` Signal command reads); `curl -H "Authorization: Bearer
$RELAY_SECRET" http://127.0.0.1:8443/status` on the deploy instance shows what's
currently deployed.

## Ansible (`ansible/`)

Terraform owns infrastructure; Ansible owns everything that runs *on* it - packages,
all the daemons, Squid, the sandbox container image and its relay from the proxy,
apt's proxy config, automatic security updates, and the Node.js/Claude Code bundle.
It's declarative and idempotent: re-running it against an already-configured set of
instances reports zero changes (verified live against this project's actual
deployment).

```bash
cd ansible
cp group_vars/all.yml.example group_vars/all.yml
# fill in group_vars/all.yml: proxy_public_ip/proxy_private_ip/ai_private_ip/
# deploy_private_ip/controller_url from `terraform output`, stop_secret matching
# ../terraform.tfvars, relay_secret (generate fresh for a new deploy; reuse the
# existing value from /etc/claude-signal/env if you're adopting Ansible for an
# already-running deployment), bot_number, allowed_sender_username,
# claude_code_oauth_token - see the manual setup checklist above for where each of
# these comes from. Do NOT put github_token here - see GitHub token setup above.

ansible-playbook playbook.yml
```

What it does, in order (see [`playbook.yml`](ansible/playbook.yml) for the exact play
list): installs packages and automatic security updates on all three hosts; configures
Squid, signal-cli, and the proxy's daemons; checks whether the ai instance already has
the sandbox container image, and if not, builds it *on the proxy* (real internet
access) and relays the tarball to the ai instance **through the Ansible controller**
(plain `scp`, not `fetch`/`copy` - those buffer the whole ~900MB image in memory and
reliably OOM the controller) - not proxy-to-ai directly, since neither instance holds
an SSH key for the other, by design; configures the ai instance (Podman, the sandbox
container, the MCP tool registrations) and the deploy instance (Podman,
`deploy_wrapper.py`); starts everything.

`group_vars/all.yml` is gitignored (same treatment as `../terraform.tfvars`) since it
holds real secrets. `proxy_public_ip` changes on every stop/start (no Elastic IP) -
update it before each run, same caveat as the [SSH access](#ssh-access) section above.

## Known limitations

- The `/` (start) and `/web` endpoints on the controller URL have no authentication —
  anyone with the URL can start the (billable) instances. The `/stop` route is
  secret-protected since abuse there is more disruptive; the start routes are left
  open deliberately, matching the CS2 server project this was modeled on. Add an API
  key/secret to them too if that stops being fine.
- The proxy instance's public IP changes on every stop/start by default (no Elastic
  IP, to keep idle cost at zero) — the SSH config snippet above needs updating each
  time. Setting `app_domain` (see [Custom domain](#custom-domain-single-link-start))
  creates an Elastic IP as a side effect, which fixes this too even if you don't care
  about the domain itself — a few cents/month while the instance is stopped.
- `GITHUB_ORG` (the account/org Claude pushes to and deploys from) is a hardcoded
  default (`c0nfund0`) baked into a template and a system prompt rather than a proper
  Ansible-templated variable — see
  [GitHub token setup](#github-token--git-integration-setup-manual-one-time) for where
  to change it.
- The deployed site on the deploy instance runs with `--cap-drop=all
  --security-opt no-new-privileges` and resource limits, but is otherwise whatever
  container image the deployed repo's own `Containerfile`/`Dockerfile` describes —
  this project doesn't sandbox *what Claude deploys* beyond that; treat `open`ing the
  web gate the same way you'd treat running any other code you didn't personally
  review.
- This has been tested by one person, live, against one AWS account and one Signal
  number — not fuzzed, not pen-tested by a third party, not run at any scale. The
  approval-gating boundaries (the two-listener split in `approval_daemon.py`, the
  credential placement in [Credential architecture](#credential-architecture)) are the
  parts most worth re-reading yourself before relying on them.
- [Voice messages](#voice-messages) are turn-by-turn (record, send, wait, get a
  reply), not a live conversation — Signal has no calling support here at all (see
  that section). The on-disk path signal-cli stores a downloaded attachment at
  (`<data-dir>/attachments/<id>`) is undocumented behavior inferred from signal-cli's
  own source and community wrappers, not a stable contract from upstream — worth
  re-checking against whatever signal-cli version you're actually running if voice
  messages start silently failing to transcribe after a signal-cli upgrade.
