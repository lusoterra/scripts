#!/usr/bin/env bash
#
# whitelist-ip-range-on-port
#
# Looks up the IP prefixes announced by a given ASN, then configures ufw so
# that a given port only accepts traffic from those prefixes, and denies
# everything else on that port.
#
# Usage:
#   whitelist-ip-range-on-port --asn 13335 --port 443
#   whitelist-ip-range-on-port --asn AS15169 --port 22 --ipv6 --dry-run
#
# Requires: curl, jq, ufw, root privileges (unless --dry-run)

set -euo pipefail

usage() {
  cat <<EOF
Usage: $(basename "$0") --asn ASN --port PORT [options]

Required:
  --asn ASN        AS number, e.g. 15169 or AS15169
  --port PORT      Port (or port range like 6000:7000) to restrict, e.g. 22

Options:
  --proto PROTO    tcp|udp (default: tcp)
  --ipv6           Also fetch and whitelist IPv6 prefixes (default: IPv4 only)
  --dry-run        Print the ufw commands instead of running them
  --no-reload      Don't reload ufw at the end
  -h, --help       Show this help

Behavior:
  - Prefixes are fetched from the RIPEstat public API (no key required),
    which aggregates global BGP announcement data, not just RIPE-region ASNs.
  - Rules created by this script are tagged with a ufw comment
    (asn<ASN>-port<PORT>-<PROTO>). Re-running with the same ASN/port/proto
    replaces the previous rule set instead of duplicating it.
  - Allow rules for the ASN's prefixes are added, followed by a deny rule
    for the same port from anywhere. ufw applies the first matching rule,
    so this only works correctly if no OTHER pre-existing rule for the same
    port appears earlier in 'ufw status numbered'. The script warns you if
    it finds any.

Example:
  $(basename "$0") --asn 13335 --port 443
EOF
}

ASN=""
PORT=""
PROTO="tcp"
INCLUDE_V6=0
DRY_RUN=0
RELOAD=1

while [[ $# -gt 0 ]]; do
  case "$1" in
    --asn) ASN="${2:-}"; shift 2 ;;
    --port) PORT="${2:-}"; shift 2 ;;
    --proto) PROTO="${2:-}"; shift 2 ;;
    --ipv6) INCLUDE_V6=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    --no-reload) RELOAD=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage; exit 1 ;;
  esac
done

if [[ -z "$ASN" || -z "$PORT" ]]; then
  echo "Error: --asn and --port are required" >&2
  usage
  exit 1
fi

ASN="${ASN^^}"
ASN="${ASN#AS}"

if ! [[ "$ASN" =~ ^[0-9]+$ ]]; then
  echo "Error: ASN must be numeric (with or without 'AS' prefix)" >&2
  exit 1
fi

if ! [[ "$PORT" =~ ^[0-9]+(:[0-9]+)?$ ]]; then
  echo "Error: PORT must be a number or a range like 6000:7000" >&2
  exit 1
fi

if [[ "$PROTO" != "tcp" && "$PROTO" != "udp" ]]; then
  echo "Error: --proto must be 'tcp' or 'udp'" >&2
  exit 1
fi

if [[ "$EUID" -ne 0 && "$DRY_RUN" -eq 0 ]]; then
  echo "Error: this needs root (sudo) to run ufw. Use --dry-run to preview without root." >&2
  exit 1
fi

for bin in curl jq ufw; do
  command -v "$bin" >/dev/null 2>&1 || { echo "Error: '$bin' is required but not found" >&2; exit 1; }
done

TAG="asn${ASN}-port${PORT}-${PROTO}"

run() {
  if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "+ $*"
  else
    eval "$@"
  fi
}

echo "Fetching announced prefixes for AS${ASN}..." >&2

fetch_prefixes() {
  local af="$1" # v4 or v6
  curl -fsS "https://stat.ripe.net/data/announced-prefixes/data.json?resource=AS${ASN}" \
    | jq -r --arg af "$af" '
        .data.prefixes[]?.prefix
        | select(
            ($af == "v4" and (contains(":") | not)) or
            ($af == "v6" and (contains(":")))
          )
      '
}

mapfile -t V4_PREFIXES < <(fetch_prefixes v4)

V6_PREFIXES=()
if [[ "$INCLUDE_V6" -eq 1 ]]; then
  mapfile -t V6_PREFIXES < <(fetch_prefixes v6)
fi

if [[ "${#V4_PREFIXES[@]}" -eq 0 && "${#V6_PREFIXES[@]}" -eq 0 ]]; then
  echo "Error: no prefixes found for AS${ASN}. Aborting without touching the firewall." >&2
  exit 1
fi

echo "Found ${#V4_PREFIXES[@]} IPv4 prefix(es)$( [[ $INCLUDE_V6 -eq 1 ]] && echo " and ${#V6_PREFIXES[@]} IPv6 prefix(es)" )." >&2

# Warn about pre-existing rules on this port that aren't ours, since they'd
# take precedence if positioned earlier in ufw's rule list.
if command -v ufw >/dev/null 2>&1; then
  EXISTING="$(ufw status numbered 2>/dev/null | grep -E "[[:space:]]${PORT}(/${PROTO})?[[:space:]]" | grep -vF "# $TAG" || true)"
  if [[ -n "$EXISTING" ]]; then
    echo "Warning: found existing ufw rule(s) on port $PORT not managed by this script:" >&2
    echo "$EXISTING" >&2
    echo "These may take precedence over the new rules. Review/remove them with 'ufw delete <num>' if needed." >&2
  fi
fi

if [[ "$PORT" == "22" || "$PORT" == "22:22" ]]; then
  echo "Warning: you are restricting port 22 (SSH). Make sure your own IP falls within AS${ASN}'s ranges," >&2
  echo "or you risk locking yourself out. Keep your current session open until you've verified access." >&2
fi

# Remove any rules previously created by this script for this exact tag.
echo "Removing old rules tagged '$TAG' (if any)..." >&2
while true; do
  LINE="$(ufw status numbered 2>/dev/null | grep -F "# $TAG" | head -n1 || true)"
  [[ -z "$LINE" ]] && break
  NUM="$(echo "$LINE" | sed -E 's/^\[[[:space:]]*([0-9]+)\].*/\1/')"
  [[ -z "$NUM" ]] && break
  run "ufw --force delete $NUM"
done

# Add allow rules for each prefix.
for prefix in "${V4_PREFIXES[@]}"; do
  run "ufw allow proto $PROTO from $prefix to any port $PORT comment '$TAG'"
done
for prefix in "${V6_PREFIXES[@]}"; do
  run "ufw allow proto $PROTO from $prefix to any port $PORT comment '$TAG'"
done

# Deny everything else on this port. Added last so allow rules above match first.
run "ufw deny proto $PROTO from any to any port $PORT comment '$TAG'"

if [[ "$RELOAD" -eq 1 ]]; then
  run "ufw reload"
fi

echo "Done. Port ${PORT}/${PROTO} is now restricted to AS${ASN}'s announced ranges." >&2
[[ "$DRY_RUN" -eq 1 ]] && echo "(dry run — no changes were made)" >&2

exit 0
