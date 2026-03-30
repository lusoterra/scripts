#!/bin/sh

set -euo pipefail

if [ $UID -ne 0 ]; then
    echo "Run this script with root"
    exit 1
fi

if [ "$#" -ne 1 ]; then
    echo "Usage example: $0 AS15169"
    exit 1
fi

ASN="$1"

if [[ ! "$ASN" =~ ^AS ]]; then
    ASN="AS$ASN"
fi

echo "Fetching prefixes for $ASN..."

API_URL="https://stat.ripe.net/data/announced-prefixes/data.json?resource=${ASN}"

PREFIXES=$(curl -s "$API_URL" | jq -r '.data.prefixes[].prefix')

if [ -z "$PREFIXES" ]; then
    echo "No prefixes found for $ASN"
    exit 1
fi

echo "Adding UFW rules..."

for prefix in $PREFIXES; do
    ufw allow from "$prefix"
done

echo
ufw reload

echo "All prefixes from $ASN are now whitelisted."
