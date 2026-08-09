#!/usr/bin/env bash
# Run as root on the proxy instance. Idempotent - safe to re-run.
#
# Required env vars: AI_SUBNET_CIDR, SQUID_PORT (e.g. from `terraform output`
# and variables.tf) - used only to render squid.conf, not stored at runtime.
#
#   AI_SUBNET_CIDR=172.31.130.0/24 SQUID_PORT=3128 ./install.sh
set -euo pipefail

: "${AI_SUBNET_CIDR:?set AI_SUBNET_CIDR, e.g. 172.31.130.0/24}"
: "${SQUID_PORT:=3128}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "== apt packages =="
apt-get update -y
apt-get install -y squid openjdk-25-jre-headless curl python3

echo "== claude-signal system user =="
id -u claude-signal &>/dev/null || useradd --system --home-dir /opt/claude-signal --shell /usr/sbin/nologin claude-signal

echo "== signal-cli =="
if [ ! -x /opt/signal-cli/bin/signal-cli ]; then
  # The release has several .tar.gz assets (json-schemas, Linux-native, Linux-client,
  # the plain JRE-based one). We want the plain one: signal-cli-X.Y.Z.tar.gz, no
  # extra hyphenated suffix before the version number's trailing .tar.gz.
  RELEASE_JSON=$(curl -fsSL https://api.github.com/repos/AsamK/signal-cli/releases/latest)
  DOWNLOAD_URL=$(echo "$RELEASE_JSON" | grep -oP '"browser_download_url":\s*"\K[^"]+/signal-cli-[0-9.]+\.tar\.gz(?=")' | head -n1)
  if [ -z "$DOWNLOAD_URL" ]; then
    echo "Could not find a signal-cli .tar.gz release asset - check https://github.com/AsamK/signal-cli/releases manually" >&2
    exit 1
  fi
  echo "Downloading $DOWNLOAD_URL"
  curl -fsSL -o /tmp/signal-cli.tar.gz "$DOWNLOAD_URL"
  mkdir -p /opt/signal-cli
  tar -xzf /tmp/signal-cli.tar.gz -C /opt/signal-cli --strip-components=1
  rm -f /tmp/signal-cli.tar.gz
fi
/opt/signal-cli/bin/signal-cli --version

echo "== claude-signal app files =="
mkdir -p /opt/claude-signal
cp "$SCRIPT_DIR/signal_bridge.py" "$SCRIPT_DIR/approval_daemon.py" "$SCRIPT_DIR/idle_monitor.py" /opt/claude-signal/
chown -R claude-signal:claude-signal /opt/claude-signal

echo "== config =="
mkdir -p /etc/claude-signal
chown claude-signal:claude-signal /etc/claude-signal
if [ ! -f /etc/claude-signal/env ]; then
  cp "$SCRIPT_DIR/env.example" /etc/claude-signal/env
  SECRET=$(openssl rand -hex 32)
  sed -i "s/^RELAY_SECRET=.*/RELAY_SECRET=$SECRET/" /etc/claude-signal/env
  echo "Generated RELAY_SECRET - copy it into the ai instance's env file too (same value on both sides)."
  echo "Now edit /etc/claude-signal/env: BOT_NUMBER, ALLOWED_SENDER_USERNAME, AI_PRIVATE_IP, CONTROLLER_URL, STOP_SECRET."
fi
chown root:claude-signal /etc/claude-signal/env
chmod 640 /etc/claude-signal/env

if [ ! -f /etc/claude-signal/allowlist.json ]; then
  echo '{}' > /etc/claude-signal/allowlist.json
fi
chown claude-signal:claude-signal /etc/claude-signal/allowlist.json

touch /etc/squid/allowed_domains.txt
chown claude-signal:claude-signal /etc/squid/allowed_domains.txt

echo "== squid.conf =="
sed -e "s|__AI_SUBNET_CIDR__|$AI_SUBNET_CIDR|" -e "s|__SQUID_PORT__|$SQUID_PORT|" \
  "$SCRIPT_DIR/squid/squid.conf.template" > /etc/squid/squid.conf
systemctl restart squid
systemctl enable squid

echo "== sudoers (claude-signal may only run 'squid -k reconfigure', nothing else) =="
cat > /etc/sudoers.d/claude-signal <<'EOF'
claude-signal ALL=(root) NOPASSWD: /usr/sbin/squid -k reconfigure
EOF
chmod 440 /etc/sudoers.d/claude-signal
visudo -c

echo "== systemd units =="
cp "$SCRIPT_DIR/systemd/"*.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable claude-signal-signal-cli-daemon claude-signal-signal-bridge claude-signal-approval-daemon claude-signal-idle-monitor

echo
echo "Install done. NOT started yet - fill in /etc/claude-signal/env, register signal-cli"
echo "(signal-cli -a <number> register / verify), then:"
echo "  systemctl start claude-signal-approval-daemon claude-signal-signal-cli-daemon claude-signal-signal-bridge claude-signal-idle-monitor"
