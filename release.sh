#!/usr/bin/env bash
#
# release.sh — cut a release tag for one component, using the versioning tool
# itself to compute the next version from git tags.
#
#   ./release.sh <component> [major|minor|alpha|beta|release]
#
# Examples:
#   ./release.sh secretvault major     # first release -> secretvault-1.0
#   ./release.sh queuing minor         # next minor    -> queuing-1.1
#   ./release.sh queuing alpha         # 1.1 -> queuing-1.2a1 (then alpha again: 1.2a2)
#   ./release.sh queuing beta          # 1.2a2 -> queuing-1.2b1
#   ./release.sh queuing release       # promote the pre-release -> queuing-1.2
#
# The component scopes the tag namespace (`<component>-<X.x>`), so modules
# release independently without shadowing each other (queuing-1.0 vs
# qworkers-1.0). With no prior tag a major bump bootstraps to 1.0, a minor
# bump to 0.1. The tag is created on the current HEAD and pushed to origin.
set -euo pipefail

usage() { echo "usage: $0 <component> [major|minor|alpha|beta|release]   (default: minor)" >&2; exit 2; }

COMPONENT="${1:-}"; [ -n "$COMPONENT" ] || usage
BUMP="${2:-minor}"
case "$BUMP" in
  major) FLAG=--increment-major ;;
  minor) FLAG=--increment-minor ;;
  alpha|beta|release) FLAG="--increment $BUMP" ;;
  *)     usage ;;
esac

REPO_ROOT="$(git rev-parse --show-toplevel)"
# The self-contained PEP 723 CLI — `python3 <file>` needs no PYTHONPATH/install.
VERSIONING="$REPO_ROOT/versioning/__main__.py"

NEXT="$(python3 "$VERSIONING" --repo-root "$REPO_ROOT" --component "$COMPONENT" $FLAG)"
echo "==> cutting $NEXT"
git -C "$REPO_ROOT" tag -a "$NEXT" -m "release $NEXT"
git -C "$REPO_ROOT" push origin "$NEXT"
echo "==> tagged and pushed $NEXT"
python3 "$VERSIONING" --repo-root "$REPO_ROOT" --component "$COMPONENT" -v
