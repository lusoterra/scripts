#!/bin/sh
P="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/.flatpak/cz.viceverse.serein/xdg-run"
if ! flatpak ps --columns=application | grep -qx cz.viceverse.serein; then
  find "$P" -maxdepth 1 -name 'discord-ipc-*' ! -type l -delete 2>/dev/null
fi
exec flatpak run cz.viceverse.serein "$@"
