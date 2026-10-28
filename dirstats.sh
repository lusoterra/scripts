#!/usr/bin/env bash
set -euo pipefail

if [[ $# -gt 1 || "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    cat <<'HELP'
Usage: dirstats [DIRECTORY]

Analyze files recursively and report counts and disk usage by extension.

Options:
  -h, --help    Show this help message

Examples:
  dirstats
  dirstats /mnt/data
  dirstats ~/Documents
HELP
    exit 0
fi

DIR="${1:-.}"

if [[ ! -d "$DIR" ]]; then
    printf 'Error: directory not found: %s\n' "$DIR" >&2
    exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
    echo "Error: Python 3 is required. Install it with: sudo pacman -S python" >&2
    exit 1
fi

python3 - "$DIR" <<'PY'
import os
import sys
from collections import defaultdict

root = os.path.abspath(sys.argv[1])
stats = defaultdict(lambda: [0, 0])
errors = 0

def onerror(error):
    global errors
    errors += 1

for base, dirs, files in os.walk(root, onerror=onerror):
    for filename in files:
        path = os.path.join(base, filename)

        # Count dotfiles without an extension separately.
        if filename.startswith(".") and filename.count(".") == 1:
            ext = "[no extension]"
        else:
            ext = os.path.splitext(filename)[1].lower() or "[no extension]"

        try:
            size = os.stat(path, follow_symlinks=False).st_size
        except OSError:
            errors += 1
            continue

        stats[ext][0] += 1
        stats[ext][1] += size

total_files = sum(count for count, size in stats.values())
total_bytes = sum(size for count, size in stats.values())

def human_size(n):
    value = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if value < 1024 or unit == "PiB":
            return f"{value:.2f} {unit}"
        value /= 1024

print(f"\nDirectory: {root}")
print(f"Files:     {total_files:,}")
print(f"Total size: {human_size(total_bytes)}")
print()
print(f"{'EXTENSION':<20} {'FILES':>12} {'SIZE':>12} {'SHARE':>9}")
print("-" * 57)

for ext, (count, size) in sorted(
    stats.items(), key=lambda item: item[1][1], reverse=True
):
    percent = size / total_bytes * 100 if total_bytes else 0
    print(f"{ext:<20} {count:>12,} {human_size(size):>12} {percent:>8.2f}%")

print("-" * 57)
percent_total = "100.00%" if total_bytes else "0.00%"
print(f"{'TOTAL':<20} {total_files:>12,} {human_size(total_bytes):>12} {percent_total:>9}")

if errors:
    print(f"\nWarning: {errors} file(s) or directory entries could not be read.")
PY
