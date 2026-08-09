#!/usr/bin/env bash
# Run on the PROXY instance (it has real internet access). Builds a
# self-contained Node.js + Claude Code bundle to scp over to the ai instance,
# which must never touch npm/GitHub/apt mirrors directly.
set -euo pipefail

NODE_VERSION="${NODE_VERSION:-20.18.1}"
OUT="${1:-/tmp/ai-bundle.tar.gz}"

WORKDIR=$(mktemp -d)
trap 'rm -rf "$WORKDIR"' EXIT
cd "$WORKDIR"

echo "== downloading Node.js v$NODE_VERSION =="
curl -fsSL -o node.tar.xz "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-x64.tar.xz"
mkdir -p bundle/node
tar -xf node.tar.xz -C bundle/node --strip-components=1
export PATH="$WORKDIR/bundle/node/bin:$PATH"

echo "== installing @anthropic-ai/claude-code =="
mkdir -p bundle/claude-code
npm install --global --prefix bundle/claude-code @anthropic-ai/claude-code

echo "== packing =="
tar -czf "$OUT" -C bundle .
echo "Bundle written to $OUT"
echo "Next: scp $OUT to the ai instance, then run ai/install.sh there."
