#!/bin/bash

birth=1772144945 # $(stat -c %W /)

if [ "$birth" -le 0 ]; then
    echo "Filesystem creation time unavailable."
    return 1
fi

birth_date=$(date -u -d "@$birth" +%F)
today=$(date -u +%F)

years=$(( $(date -u -d "$today" +%Y) - $(date -u -d "$birth_date" +%Y) ))
months=$(( $(date -u -d "$today" +%-m) - $(date -u -d "$birth_date" +%-m) ))
days=$(( $(date -u -d "$today" +%-d) - $(date -u -d "$birth_date" +%-d) ))

if (( days < 0 )); then
    months=$((months - 1))
    prev_month_days=$(date -u -d "$(date -u -d "$today" +%Y-%m-01) -1 day" +%-d)
    days=$((days + prev_month_days))
fi

if (( months < 0 )); then
    years=$((years - 1))
    months=$((months + 12))
fi

# Build output parts dynamically
out=""
[ "$years" -gt 0 ] && out="${years} years"

if [ "$months" -gt 0 ]; then
    [ -n "$out" ] && out="${out}, "
    out="${out}${months} months"
fi

if [ "$days" -gt 0 ]; then
    [ -n "$out" ] && out="${out}, "
    out="${out}${days} days"
fi

# Fallback if everything is 0 (e.g., today is the installation day)
if [ -z "$out" ]; then
    out="0 days"
fi

echo "$out"

