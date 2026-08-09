#!/usr/bin/env bash
# Run as root on the ai instance. Idempotent - safe to re-run.
#
# Expects the bundle built by server/proxy/bootstrap/build_ai_bundle.sh to
# already be at /tmp/ai-bundle.tar.gz (scp'd over from the proxy instance -
# this instance has no internet access to fetch it itself).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUNDLE="${1:-/tmp/ai-bundle.tar.gz}"

echo "== claude-signal system user =="
id -u claude-signal &>/dev/null || useradd --system --home-dir /opt/claude-signal --shell /usr/sbin/nologin claude-signal

echo "== node + claude code bundle =="
if [ ! -x /opt/claude-signal/claude-code/bin/claude ]; then
  test -f "$BUNDLE" || { echo "Bundle not found at $BUNDLE - scp it from the proxy first" >&2; exit 1; }
  mkdir -p /opt/claude-signal
  tar -xzf "$BUNDLE" -C /opt/claude-signal
fi
/opt/claude-signal/node/bin/node --version
PATH="/opt/claude-signal/node/bin:$PATH" /opt/claude-signal/claude-code/bin/claude --version

echo "== app files =="
cp "$SCRIPT_DIR/claude_wrapper.py" "$SCRIPT_DIR/mcp_url_gate.py" /opt/claude-signal/
mkdir -p /opt/claude-signal/workspace
chown -R claude-signal:claude-signal /opt/claude-signal

echo "== config =="
mkdir -p /etc/claude-signal
if [ ! -f /etc/claude-signal/env ]; then
  cp "$SCRIPT_DIR/env.example" /etc/claude-signal/env
  echo "Created /etc/claude-signal/env - fill in RELAY_SECRET, PROXY_PRIVATE_IP,"
  echo "and CLAUDE_CODE_OAUTH_TOKEN before starting the service."
fi
chown root:claude-signal /etc/claude-signal/env
chmod 640 /etc/claude-signal/env

echo "== systemd unit =="
cp "$SCRIPT_DIR/systemd/claude-signal-wrapper.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable claude-signal-wrapper

echo
echo "Install done. NOT started yet - fill in /etc/claude-signal/env (including"
echo "CLAUDE_CODE_OAUTH_TOKEN), register the MCP tool as the claude-signal user:"
echo "  sudo -u claude-signal -H bash -c 'set -a; source /etc/claude-signal/env; set +a; \\"
echo "    PATH=/opt/claude-signal/node/bin:\$PATH /opt/claude-signal/claude-code/bin/claude mcp add --scope user \\"
echo "    --env PROXY_PRIVATE_IP=\$PROXY_PRIVATE_IP --env RELAY_PORT=\$RELAY_PORT --env RELAY_SECRET=\$RELAY_SECRET \\"
echo "    --transport stdio claude-signal-url-gate -- /usr/bin/python3 /opt/claude-signal/mcp_url_gate.py'"
echo "    (note: --transport stdio must come between the last --env and the server name.)"
echo "then: systemctl start claude-signal-wrapper"
