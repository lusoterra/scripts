#!/bin/bash

set -euo pipefail

usage() {
  cat <<'EOF'
Usage: bwrap-opencode [-w DIR]... [-r DIR]... [PROJECT_DIR] [-- opencode args...]
  -w, --rw DIR   extra directory, read-write
  -r, --ro DIR   extra directory, read-only
  -h, --help     show this help
PROJECT_DIR defaults to the current directory and is always read-write.
EOF
}

die() { echo "bwrap-opencode: $*" >&2; exit 1; }

need_path() {
    [[ -n "${2:-}" ]] || die "$1 needs a path"
    realpath -e -- "$2" 2>/dev/null || die "$1: no such path: $2"
}

RW=(); RO=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--help) usage; exit 0 ;;
        -w|--rw) d="$(need_path "$1" "${2:-}")"; RW+=("$d"); shift 2 ;;
        -r|--ro) d="$(need_path "$1" "${2:-}")"; RO+=("$d"); shift 2 ;;
        --) shift; break ;;
        *) break ;;
    esac
done

PROJECT="$(pwd)"
if [[ $# -gt 0 && -d "$1" ]]; then PROJECT="$(realpath -e -- "$1")"; shift; fi

for d in "$PROJECT" "${RW[@]}"; do
    [[ "$d" == "$HOME" ]] && die "refusing to expose all of \$HOME"
done

mkdir -p ~/.config/opencode ~/.local/share/opencode ~/.local/state/opencode ~/.cache/opencode /tmp/opencode

RESOLV_ARGS=()
RESOLV="$(realpath -e /etc/resolv.conf)"
case "$RESOLV" in
    /etc/*) ;;
    *) RESOLV_ARGS=(--ro-bind "$RESOLV" "$RESOLV") ;;
esac

EXTRA=()
for d in "${RO[@]}"; do EXTRA+=(--ro-bind "$d" "$d"); done
for d in "${RW[@]}"; do EXTRA+=(--bind "$d" "$d"); done

exec bwrap \
    --die-with-parent --unshare-all --share-net --hostname bwrap-opencode \
    --ro-bind /usr /usr \
    --symlink usr/bin /bin --symlink usr/bin /sbin \
    --symlink usr/lib /lib --symlink usr/lib /lib64 \
    --ro-bind /etc /etc \
    "${RESOLV_ARGS[@]}" \
    --proc /proc --dev /dev --tmpfs /tmp \
    --dir /tmp/opencode \
    --bind /tmp/opencode /tmp/opencode \
    --tmpfs "$HOME" \
    --bind ~/.config/zsh           ~/.config/zsh \
    --bind ~/.config/opencode      ~/.config/opencode \
    --bind ~/.local/share/opencode ~/.local/share/opencode \
    --bind ~/.local/state/opencode ~/.local/state/opencode \
    --bind ~/.cache/opencode       ~/.cache/opencode \
    --ro-bind-try ~/.config/git ~/.config/git \
    "${EXTRA[@]}" \
    --bind "$PROJECT" "$PROJECT" \
    --chdir "$PROJECT" \
    --setenv HOME "$HOME" \
    --unsetenv DISPLAY \
    --unsetenv WAYLAND_DISPLAY \
    --unsetenv WAYLAND_SOCKET \
    --unsetenv XAUTHORITY \
    opencode --standalone "$@"
