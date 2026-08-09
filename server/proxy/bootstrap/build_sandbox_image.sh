#!/usr/bin/env bash
# Run on the PROXY instance (real internet access - Docker Hub's pull path needs
# several domains not worth allowing through Squid). Builds the ai instance's
# hardened sandbox container image and saves it as a tarball to relay over to the
# ai instance, same pattern as build_ai_bundle.sh for the Node/Claude Code bundle.
set -euo pipefail

CONTAINERFILE_DIR="${1:?usage: build_sandbox_image.sh <containerfile-dir> <output-tar>}"
OUT="${2:-/tmp/sandbox-image.tar}"
IMAGE_TAG="claude-signal-sandbox:latest"

echo "== building $IMAGE_TAG from $CONTAINERFILE_DIR =="
podman build -t "$IMAGE_TAG" -f "$CONTAINERFILE_DIR/Containerfile" "$CONTAINERFILE_DIR"

echo "== saving to $OUT =="
# docker-archive refuses to write into an existing tar (even one from a prior,
# interrupted run of this same script) - "doesn't support modifying existing
# images" - so clear it first rather than treating that as a real failure.
rm -f "$OUT"
podman save -o "$OUT" "$IMAGE_TAG"
echo "Image written to $OUT"
echo "Next: scp/fetch to the ai instance, then 'podman load -i <file>' there."
