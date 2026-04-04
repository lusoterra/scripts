#!/usr/bin/env bash

set -euo pipefail

if [ "$#" -ne 3 ]; then
    echo "Usage: $0 <source_folder> <destination_folder> <bitrate_kbps>"
    exit 1
fi

SRC="$(realpath "$1")"
DST="$(realpath "$2")"
BITRATE="$3"

export SRC DST BITRATE

process_file() {

    input="$1"
    rel="${input#$SRC/}"

    ext="${input##*.}"
    ext_lower=$(printf '%s' "$ext" | tr '[:upper:]' '[:lower:]')

    case "$ext_lower" in

        flac|wav|aiff|aif)

            output="$DST/${rel%.*}.opus"
            mkdir -p "$(dirname "$output")"

            if [ -f "$output" ]; then
                return
            fi

            echo "ENCODE: $rel"
            opusenc --quiet --bitrate "$BITRATE" --vbr --music "$input" "$output"
            ;;

        mp3|opus|m4a)

            output="$DST/$rel"
            mkdir -p "$(dirname "$output")"

            if [ -f "$output" ]; then
                return
            fi

            echo "COPY: $rel"
            cp -p "$input" "$output"
            ;;

        jpg|jpeg|png|cue|log|txt|pdf)

            output="$DST/$rel"
            mkdir -p "$(dirname "$output")"

            if [ -f "$output" ]; then
                return
            fi

            cp -p "$input" "$output"
            ;;

        *)

            ;;
    esac
}

export -f process_file

find "$SRC" -type f -print0 | \
parallel -0 --bar process_file {}
