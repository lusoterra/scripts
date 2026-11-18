#!/usr/bin/env python3

import sys

def ms_to_timestamp(ms):
    total_seconds = ms // 1000
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60

    if hours > 0:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    else:
        return f"{minutes}:{seconds:02d}"

def parse_ffmetadata(file_path):
    chapters = []
    current = {}

    with open(file_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()

            if line.startswith("START="):
                current["start"] = int(line.split("=")[1])

            elif line.startswith("title="):
                current["title"] = line.split("=", 1)[1]

            elif line == "[CHAPTER]":
                if "start" in current and "title" in current:
                    chapters.append((current["start"], current["title"]))
                current = {}

        # append last chapter if needed
        if "start" in current and "title" in current:
            chapters.append((current["start"], current["title"]))

    return chapters

def main():
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} chapters.ffmeta")
        sys.exit(1)

    chapters = parse_ffmetadata(sys.argv[1])

    for start_ms, title in chapters:
        print(f"{ms_to_timestamp(start_ms)} {title}")

if __name__ == "__main__":
    main()
