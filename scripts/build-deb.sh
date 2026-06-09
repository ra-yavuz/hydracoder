#!/usr/bin/env bash
# Build the hydracoder .deb without debhelper. Mirrors the lillycoder template.
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
VERSION=$(sed -nE '1 s/^[^(]*\(([^)]+)\).*/\1/p' "$ROOT/debian/changelog")
[ -n "$VERSION" ] || { echo "could not parse version from debian/changelog" >&2; exit 1; }

PKG_DIR="$ROOT/dist/hydracoder_${VERSION}_all"
DEB_OUT="$ROOT/dist/hydracoder_${VERSION}_all.deb"

rm -rf "$PKG_DIR" "$DEB_OUT"
mkdir -p "$PKG_DIR/DEBIAN" \
         "$PKG_DIR/usr/bin" \
         "$PKG_DIR/usr/lib/hydracoder" \
         "$PKG_DIR/usr/share/doc/hydracoder"

# Bin shim
install -m 0755 "$ROOT/bin/hydracoder" "$PKG_DIR/usr/bin/hydracoder"

# Library: copy the hydracoder package (incl. web/ assets).
cp -r "$ROOT/lib/hydracoder" "$PKG_DIR/usr/lib/hydracoder/"

# Strip pyc/__pycache__ from the payload.
find "$PKG_DIR/usr/lib/hydracoder" -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
find "$PKG_DIR/usr/lib/hydracoder" -name '*.pyc' -delete 2>/dev/null || true

# Docs
install -m 0644 "$ROOT/README.md" "$PKG_DIR/usr/share/doc/hydracoder/README.md"
install -m 0644 "$ROOT/LICENSE"   "$PKG_DIR/usr/share/doc/hydracoder/copyright"

# Maintainer scripts
install -m 0755 "$ROOT/debian/postinst" "$PKG_DIR/DEBIAN/postinst"
install -m 0755 "$ROOT/debian/postrm"   "$PKG_DIR/DEBIAN/postrm"

cat > "$PKG_DIR/DEBIAN/control" <<EOF
Package: hydracoder
Version: ${VERSION}
Section: utils
Priority: optional
Architecture: all
Depends: python3 (>= 3.10), python3-websockets, lillycoder, hydra-llm
Maintainer: Ramazan Yavuz <yavuzramazan1994@gmail.com>
Homepage: https://github.com/ra-yavuz/hydracoder
Description: local-AI development orchestrator with a web UI
 hydracoder turns a project goal into a task graph and drives local models to
 build it: a planner decomposes the goal, a scheduler routes each task to a
 right-sized local model, a reviewer checks each result, and an append-only
 journal makes a crash or a full context window recoverable. It runs entirely
 on local models via hydra-llm, using lillycoder as the per-task agent loop.
 A dark web UI shows each model as a live terminal and a persistent chat box
 lets you steer the run in plain language.
 .
 Built so development can continue on local hardware when hosted AI becomes
 too expensive to use.
 .
 DISCLAIMER: provided AS IS, WITHOUT WARRANTY OF ANY KIND. hydracoder runs
 local models that read, write, and delete files in a workspace and run shell
 commands on your machine. You alone are responsible for any damage to your
 data, hardware, or system. By installing you accept all risk. See
 /usr/share/doc/hydracoder/README.md for the full disclaimer.
EOF

: > "$PKG_DIR/DEBIAN/conffiles"

dpkg-deb --build --root-owner-group "$PKG_DIR" "$DEB_OUT"
echo
echo "Built: $DEB_OUT"
ls -la "$DEB_OUT"
