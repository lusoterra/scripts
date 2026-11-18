#!/usr/bin/env bash
# Wrapper around makepkg that records upstream git commits into .aur-overlay.json
# Usage: makepkg-aur [makepkg args...]
# Runs makepkg in the current directory, then records metadata if PKGBUILD exists.
set -euo pipefail

makepkg "$@"

if [[ -f PKGBUILD ]]; then
    python3 "$(dirname "$0")/aur_overlay.py" --record . || true
fi
