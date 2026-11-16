#!/usr/bin/env python3
"""Scan XDG / Flatpak user directories for leftovers from uninstalled packages.

This script NEVER deletes anything. It only reports directories that look like
they belong to packages that are no longer installed, so you can review and
remove them yourself.

Respects the standard XDG environment variables (XDG_CONFIG_HOME,
XDG_DATA_HOME, XDG_CACHE_HOME, XDG_STATE_HOME) and a few common overrides.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


# ---------------------------------------------------------------------------
# XDG / base directory resolution (honours environment overrides)
# ---------------------------------------------------------------------------

HOME = Path.home()


def _env_path(var: str, default: Path) -> Path:
    value = os.environ.get(var)
    if value:
        return Path(value).expanduser().resolve(strict=False)
    return default


CONFIG_HOME = _env_path("XDG_CONFIG_HOME", HOME / ".config")
DATA_HOME = _env_path("XDG_DATA_HOME", HOME / ".local" / "share")
CACHE_HOME = _env_path("XDG_CACHE_HOME", HOME / ".cache")
STATE_HOME = _env_path("XDG_STATE_HOME", HOME / ".local" / "state")
# Flatpak per-app data is not strictly XDG, but honour an override anyway.
VAR_APP_HOME = _env_path("VAR_APP_HOME", HOME / ".var" / "app")

# Extra roots the user can add (comma separated) via env or --extra.
EXTRA_ROOTS_ENV = os.environ.get("XDG_SWEEP_EXTRA_ROOTS", "")


# ---------------------------------------------------------------------------
# Alias table + system directory set (adapted from xdg-sweep)
# ---------------------------------------------------------------------------

KNOWN_ALIASES: dict[str, set[str]] = {
    "bravesoftware": {"brave-bin", "brave-browser", "brave"},
    "chromium": {"chromium", "chromium-browser"},
    "google-chrome": {"google-chrome", "google-chrome-stable"},
    "microsoft-edge": {"microsoft-edge-stable-bin", "microsoft-edge-stable"},
    "vivaldi": {"vivaldi", "vivaldi-stable"},
    "code": {"visual-studio-code-bin", "code", "code-oss"},
    "code - oss": {"code", "code-oss", "visual-studio-code-bin"},
    "code - insiders": {"visual-studio-code-insiders-bin"},
    "vscodium": {"vscodium", "vscodium-bin"},
    "sublime-text-3": {"sublime-text-4", "sublime-text-3", "sublime-text"},
    "sublime-text": {"sublime-text-4", "sublime-text-3", "sublime-text"},
    "jetbrains": {"intellij-idea-community-edition", "pycharm-community-edition",
                  "clion", "goland", "webstorm", "rider"},
    "discord": {"discord", "discord-canary", "discord-ptb", "discord-development"},
    "signal": {"signal-desktop", "signal-desktop-beta"},
    "slack": {"slack-desktop", "slack-electron"},
    "telegram": {"telegram-desktop"},
    "telegramdesktop": {"telegram-desktop"},
    "element": {"element-desktop", "element-desktop-git"},
    "spotify": {"spotify", "spotify-launcher"},
    "vlc": {"vlc"},
    "alacritty": {"alacritty"},
    "kitty": {"kitty"},
    "wezterm": {"wezterm", "wezterm-git"},
    "steam": {"steam", "steam-native-runtime"},
    "lutris": {"lutris"},
    "heroic": {"heroic-games-launcher-bin", "heroic-games-launcher"},
    "mangohud": {"mangohud", "lib32-mangohud"},
    "gamescope": {"gamescope"},
    "xfce4": {"xfce4-session", "xfce4-panel", "xfce4-settings", "xfconf"},
    "cinnamon": {"cinnamon"},
    "mate": {"mate-session-manager"},
    "obs-studio": {"obs-studio"},
    "keepassxc": {"keepassxc"},
    "libreoffice": {"libreoffice-fresh", "libreoffice-still"},
    "gtk-3.0": {"gtk3"},
    "gtk-4.0": {"gtk4"},
    "npm": {"npm", "nodejs"},
    "yarn": {"yarn"},
    "rustup": {"rustup"},
    "cargo": {"rust", "rustup"},
    "go": {"go"},
    "pip": {"python-pip", "python"},
    "hypr": {"hyprland", "hyprland-git"},
    "waybar": {"waybar"},
    "sway": {"sway", "swayfx"},
    "wofi": {"wofi"},
    "rofi": {"rofi", "rofi-wayland"},
    "dunst": {"dunst"},
    "mako": {"mako"},
    "firefox": {"firefox", "firefox-developer-edition", "firefox-nightly"},
    "mozilla": {"firefox", "thunderbird", "seamonkey"},
    "thunderbird": {"thunderbird"},
    "google": {"google-chrome", "google-chrome-stable"},
    "brave": {"brave", "brave-bin", "brave-browser"},
    "chrome": {"google-chrome", "google-chrome-stable", "chromium"},
}

SYSTEM_DIRS: set[str] = {
    "dconf", "dbus", "fontconfig", "glib-2.0", "gnome-session", "gtk-2.0",
    "gtk-3.0", "gtk-4.0", "ibus", "icc", "mime", "menus", "recently-used.xbel",
    "systemd", "user-dirs.dirs", "user-dirs.locale", "xdg-desktop-portal",
    "pulse", "pipewire", "wireplumber", "at-spi", "dbus-1", "gnome", "kde",
    "plasma", "kdedefaults", "qtproject.conf", "trolltech.conf", "xsettingsd",
    "user-places.xbel", "trash", "Trash", "icons", "themes", "fonts",
    "applications", "desktop-directories", "color-schemes", "baloo",
    "kactivitymanagerd", "kwalletd", "evolution", "gvfs-metadata", "keyrings",
    "gnome-shell", "xorg", "wayland", "session", "autostart", "locale", "doc",
    "mesa_shader_cache", "mesa_shader_cache_db", "mesa", "event-sound-cache",
    "thumbnails", "icon-cache", "sessions", "bash_history", "bash_profile",
    "bash_logout", "bashrc", "profile", "xauthority", "xprofile", "xinitrc",
    "xsession", "zshrc", "zsh_history", "zprofile", "zcompdump", "zshenv",
    "inputrc", "lesshst", "wget-hsts", "python_history", "node_repl_history",
    "viminfo", "venv",
    "pulse-cookie",
    "ssh", "gnupg", "pki",
    "config", "data", "cache", "state", "tmp", "log", "logs", "run",
}


def is_system_dir(name: str) -> bool:
    lower = name.lower()
    if lower in {s.lower() for s in SYSTEM_DIRS}:
        return True
    if lower.startswith("zcompdump"):
        return True
    # zsh completion dumps
    if lower.startswith(".zcompdump"):
        return True
    return False


def lookup_alias(name: str) -> set[str] | None:
    return KNOWN_ALIASES.get(name.lower())


# ---------------------------------------------------------------------------
# Installed package discovery
# ---------------------------------------------------------------------------

def get_pacman_packages() -> set[str]:
    try:
        r = subprocess.run(["pacman", "-Qq"], capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            return {ln.strip().lower() for ln in r.stdout.splitlines() if ln.strip()}
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return set()


def get_flatpak_apps() -> set[str]:
    apps: set[str] = set()
    try:
        r = subprocess.run(["flatpak", "list", "--app", "--columns=application"],
                           capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            for ln in r.stdout.splitlines():
                ln = ln.strip()
                if ln:
                    apps.add(ln.lower())
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    # Also try user/system explicitly in case default missed anything.
    for scope in ("--user", "--system"):
        try:
            r = subprocess.run(["flatpak", "list", scope, "--app", "--columns=application"],
                               capture_output=True, text=True, timeout=30)
            if r.returncode == 0:
                for ln in r.stdout.splitlines():
                    ln = ln.strip()
                    if ln:
                        apps.add(ln.lower())
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
    return apps


def parse_desktop_files() -> dict[str, str]:
    mapping: dict[str, str] = {}
    d = Path("/usr/share/applications")
    if not d.is_dir():
        return mapping
    for f in d.glob("*.desktop"):
        try:
            content = f.read_text(errors="replace")
        except OSError:
            continue
        stem = f.stem.lower()
        for line in content.splitlines():
            if line.startswith("Exec="):
                cmd = line[5:].strip().split()
                if cmd:
                    binary = Path(cmd[0]).name.lower()
                    if binary:
                        mapping[stem] = binary
                break
    return mapping


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

def _normalize(name: str) -> str:
    n = name.lower()
    if "." in n and n.count(".") >= 2:
        n = n.rsplit(".", 1)[-1]
    n = re.sub(r"[-_.]", "", n)
    return n


def _try_match(dirname: str, packages: set[str], flatpaks: set[str],
               desktop_map: dict[str, str]) -> tuple[str | None, str | None]:
    lower = dirname.lower()

    # 1. Exact pacman package
    if lower in packages:
        return lower, "exact"

    # 2. Alias table
    alias_pkgs = lookup_alias(dirname)
    if alias_pkgs:
        for pkg in alias_pkgs:
            if pkg.lower() in packages:
                return pkg, "alias"

    # 3. Flatpak app id
    if lower in flatpaks:
        return lower, "flatpak"
    for app_id in flatpaks:
        parts = app_id.split(".")
        if lower in parts:
            return app_id, "flatpak"

    # 4. Desktop file mapping
    if lower in desktop_map:
        binary = desktop_map[lower]
        if binary in packages:
            return binary, "desktop"

    # 5. Fuzzy normalized equality
    normalized = _normalize(dirname)
    if normalized:
        for pkg in packages:
            if _normalize(pkg) == normalized:
                return pkg, "fuzzy"

    # 6. Prefix / suffix
    if len(lower) >= 3:
        for pkg in packages:
            if pkg.startswith(lower) or pkg.endswith(lower):
                return pkg, "fuzzy"
            if lower.startswith(pkg) or lower.endswith(pkg):
                return pkg, "fuzzy"

    # 7. Flatpak partial: entry may be last segment of an app id
    for app_id in flatpaks:
        if app_id.rsplit(".", 1)[-1] == lower:
            return app_id, "flatpak"

    return None, None


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------

@dataclass
class DirEntry:
    path: Path
    root_category: str
    name: str
    size_bytes: int = 0
    is_dotfile: bool = False


def _dir_size(path: Path) -> int:
    total = 0
    try:
        for dirpath, _dirnames, filenames in os.walk(path, followlinks=False):
            for f in filenames:
                fp = os.path.join(dirpath, f)
                try:
                    total += os.lstat(fp).st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


def _scan_root(root: Path, category: str, compute_sizes: bool) -> list[DirEntry]:
    entries: list[DirEntry] = []
    if not root.is_dir():
        return entries
    try:
        children = sorted(root.iterdir(), key=lambda p: p.name)
    except (PermissionError, OSError):
        return entries
    for child in children:
        if not child.is_dir() and not child.is_file():
            continue
        # Skip symlinks at top level to avoid escaping.
        if child.is_symlink():
            continue
        size = 0
        if compute_sizes:
            if child.is_dir():
                size = _dir_size(child)
            else:
                try:
                    size = child.stat().st_size
                except OSError:
                    size = 0
        entries.append(DirEntry(path=child, root_category=category, name=child.name))
        entries[-1].size_bytes = size
    return entries


def _scan_dotfiles(compute_sizes: bool) -> list[DirEntry]:
    entries: list[DirEntry] = []
    skip = {".config", ".local", ".cache", ".var", ".git", ".ssh", ".gnupg",
            ".pki", ".dbus", ".gvfs"}
    try:
        children = sorted(HOME.iterdir(), key=lambda p: p.name)
    except (PermissionError, OSError):
        return entries
    for child in children:
        if not child.name.startswith("."):
            continue
        if child.name in skip:
            continue
        if not (child.is_dir() or child.is_file()):
            continue
        if child.is_symlink():
            continue
        size = 0
        if compute_sizes:
            if child.is_dir():
                size = _dir_size(child)
            else:
                try:
                    size = child.stat().st_size
                except OSError:
                    size = 0
        clean = child.name.lstrip(".")
        e = DirEntry(path=child, root_category="dotfile", name=clean, is_dotfile=True)
        e.size_bytes = size
        entries.append(e)
    return entries


def scan_all(compute_sizes: bool, extra_roots: list[Path]) -> list[DirEntry]:
    entries: list[DirEntry] = []
    # Map root -> category
    roots = [
        (CONFIG_HOME, "config"),
        (DATA_HOME, "data"),
        (CACHE_HOME, "cache"),
        (STATE_HOME, "state"),
        (VAR_APP_HOME, "varapp"),
    ]
    for root, cat in roots:
        entries.extend(_scan_root(root, cat, compute_sizes))
    entries.extend(_scan_dotfiles(compute_sizes))
    for i, r in enumerate(extra_roots):
        entries.extend(_scan_root(r, f"extra{i}", compute_sizes))
    return entries


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

from enum import Enum


class Status(Enum):
    MATCHED = "matched"
    ORPHANED = "orphaned"
    SYSTEM = "system"
    WHITELISTED = "whitelisted"


@dataclass
class MatchResult:
    entry: DirEntry
    status: Status
    matched_package: str | None = None
    match_method: str | None = None


def classify(entries: list[DirEntry], packages: set[str], flatpaks: set[str],
             desktop_map: dict[str, str], whitelist: set[str]) -> list[MatchResult]:
    results: list[MatchResult] = []
    for entry in entries:
        lower = entry.name.lower()
        if lower in whitelist:
            results.append(MatchResult(entry, Status.WHITELISTED))
            continue
        # System dirs apply to all categories except varapp where app ids rarely match.
        if is_system_dir(entry.name):
            results.append(MatchResult(entry, Status.SYSTEM))
            continue
        # For varapp, do not fuzzy-match generic system-ish names; still try flatpak exact.
        pkg, method = _try_match(entry.name, packages, flatpaks, desktop_map)
        if pkg:
            results.append(MatchResult(entry, Status.MATCHED, pkg, method))
        else:
            if entry.is_dotfile:
                pkg2, method2 = _try_match(entry.path.name.lstrip("."), packages, flatpaks, desktop_map)
                if pkg2:
                    results.append(MatchResult(entry, Status.MATCHED, pkg2, method2))
                    continue
            results.append(MatchResult(entry, Status.ORPHANED))
    return results


def load_whitelist() -> set[str]:
    keep = HOME / ".config" / "xdg-sweep" / "keep.txt"
    # Also support our own keep file.
    alt = HOME / ".config" / "scan-orphaned" / "keep.txt"
    out: set[str] = set()
    for f in (keep, alt):
        if f.is_file():
            try:
                for line in f.read_text().splitlines():
                    line = line.strip()
                    if line and not line.startswith("#"):
                        out.add(line.lower())
            except OSError:
                pass
    return out


def _human_size(nbytes: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(nbytes) < 1024:
            return f"{nbytes:.1f} {unit}"
        nbytes /= 1024
    return f"{nbytes:.1f} PiB"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Report orphaned XDG/Flatpak user directories.")
    parser.add_argument("--no-sizes", action="store_true", help="Skip size calculation (faster).")
    parser.add_argument("--all", action="store_true", help="Show matched/system/whitelisted too.")
    parser.add_argument("--show-matched", action="store_true", help="Include matched (installed) dirs.")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of a table.")
    parser.add_argument("--min-size", type=str, default=None,
                        help="Only show entries at least this big (e.g. 100M, 1G).")
    parser.add_argument("--extra", type=str, default=None,
                        help="Comma-separated extra directories to scan.")
    args = parser.parse_args(argv)

    compute_sizes = not args.no_sizes

    extra_roots: list[Path] = []
    if EXTRA_ROOTS_ENV:
        for part in EXTRA_ROOTS_ENV.split(","):
            part = part.strip()
            if part:
                extra_roots.append(Path(part).expanduser())
    if args.extra:
        for part in args.extra.split(","):
            part = part.strip()
            if part:
                extra_roots.append(Path(part).expanduser())

    print("Resolving installed packages...", file=sys.stderr)
    packages = get_pacman_packages()
    flatpaks = get_flatpak_apps()
    desktop_map = parse_desktop_files()
    whitelist = load_whitelist()

    print(f"  pacman packages: {len(packages)}", file=sys.stderr)
    print(f"  flatpak apps:    {len(flatpaks)}", file=sys.stderr)
    if not packages:
        print("  WARNING: no pacman packages found (pacman missing or DB unavailable). "
              "Results will over-report orphans.", file=sys.stderr)
    print("Scanning roots:", file=sys.stderr)
    for label, root in [("config", CONFIG_HOME), ("data", DATA_HOME),
                        ("cache", CACHE_HOME), ("state", STATE_HOME),
                        ("var/app", VAR_APP_HOME)]:
        print(f"  {label}: {root} {'(exists)' if root.is_dir() else '(missing)'}", file=sys.stderr)
    if extra_roots:
        for r in extra_roots:
            print(f"  extra: {r}", file=sys.stderr)

    entries = scan_all(compute_sizes, extra_roots)
    results = classify(entries, packages, flatpaks, desktop_map, whitelist)

    # min-size filter
    def parse_size(s: str) -> int:
        s = s.strip().upper()
        mult = 1
        if s.endswith("K"):
            mult = 1024; s = s[:-1]
        elif s.endswith("M"):
            mult = 1024**2; s = s[:-1]
        elif s.endswith("G"):
            mult = 1024**3; s = s[:-1]
        elif s.endswith("T"):
            mult = 1024**4; s = s[:-1]
        try:
            return int(float(s) * mult)
        except ValueError:
            return 0

    min_bytes = parse_size(args.min_size) if args.min_size else 0

    # Determine which to display
    def show(r: MatchResult) -> bool:
        if r.entry.size_bytes < min_bytes:
            return False
        if args.json:
            return True  # filter later
        if args.all:
            return True
        if r.status == Status.ORPHANED:
            return True
        if args.show_matched and r.status == Status.MATCHED:
            return True
        return False

    display = [r for r in results if show(r)]

    # Summary
    counts = {s: 0 for s in Status}
    for r in results:
        counts[r.status] += 1
    orphan_size = sum(r.entry.size_bytes for r in results if r.status == Status.ORPHANED)

    if args.json:
        payload = {
            "roots": {
                "config": str(CONFIG_HOME),
                "data": str(DATA_HOME),
                "cache": str(CACHE_HOME),
                "state": str(STATE_HOME),
                "var_app": str(VAR_APP_HOME),
            },
            "counts": {s.value: counts[s] for s in Status},
            "orphaned_size_bytes": orphan_size,
            "entries": [
                {
                    "path": str(r.entry.path),
                    "category": r.entry.root_category,
                    "name": r.entry.name,
                    "status": r.status.value,
                    "matched_package": r.matched_package,
                    "match_method": r.match_method,
                    "size_bytes": r.entry.size_bytes,
                }
                for r in results
                if r.status == Status.ORPHANED or args.all or args.show_matched
            ],
        }
        print(json.dumps(payload, indent=2))
        return 0

    # Table output
    print()
    print("NOTE: nothing is removed by this script. Review each path before deleting.")
    print()
    # Header
    print(f"{'STATUS':<12} {'CATEGORY':<9} {'SIZE':>10}  PATH")
    print("-" * 90)
    for r in sorted(display, key=lambda x: -x.entry.size_bytes):
        size_str = _human_size(r.entry.size_bytes) if compute_sizes else "-"
        print(f"{r.status.value:<12} {r.entry.root_category:<9} {size_str:>10}  {r.entry.path}")

    print()
    print("Summary:")
    print(f"  Matched (installed):  {counts[Status.MATCHED]}")
    print(f"  Orphaned:             {counts[Status.ORPHANED]}")
    print(f"  System:               {counts[Status.SYSTEM]}")
    print(f"  Whitelisted:          {counts[Status.WHITELISTED]}")
    print(f"  Total orphan size:    {_human_size(orphan_size)}")
    print()
    print("Tip: add directory names to ~/.config/scan-orphaned/keep.txt to whitelist them.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
