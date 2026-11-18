#!/usr/bin/env bash
# Download the latest Serein flatpak (incl. nightly/pre-releases) to /tmp,
# install it, then clean up. Temporary until Serein is on Flathub.
set -euo pipefail

repo="ViceVerse-cz/Serein"
api="https://api.github.com/repos/$repo/releases?per_page=1"

tmp=$(mktemp -d /tmp/serein.XXXXXX)
trap 'rm -rf "$tmp"' EXIT
file="$tmp/serein-latest.flatpak"

# /releases/latest skips pre-releases, so ask the list endpoint for the newest one
url=$(curl -fsSL "$api" \
  | grep -oE '"browser_download_url": *"[^"]*Linux-x86_64\.flatpak"' \
  | head -n1 \
  | sed -E 's/.*"(https[^"]*)"/\1/')

[ -n "$url" ] || { echo "Could not find a Linux-x86_64 flatpak in the latest release" >&2; exit 1; }
echo "Downloading $url"

if command -v aria2c >/dev/null 2>&1; then
  aria2c -x4 -s4 --allow-overwrite=true -d "$tmp" -o "$(basename "$file")" "$url" \
    || curl -fL --retry 3 -o "$file" "$url"
else
  curl -fL --retry 3 -o "$file" "$url"
fi

sudo flatpak install -y "$file"
echo "Done, temp file removed."
