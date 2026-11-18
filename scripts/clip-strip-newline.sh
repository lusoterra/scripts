#!/bin/bash
data=$(cat; printf x)
data=${data%x}

# Nothing to strip -> don't touch the clipboard (also breaks the rewrite loop)
[[ "$data" == *$'\n' ]] || exit 0

# Leave file copies (Dolphin) and images alone, rewriting would destroy them
types=$(wl-paste --list-types 2>/dev/null)
if grep -qiE '^(text/uri-list|x-special/|application/x-kde|image/)' <<<"$types"; then
    exit 0
fi

while [[ "$data" == *$'\n' ]]; do
    data=${data%$'\n'}
done

# Only write back if the clipboard hasn't changed since we read it
current=$(wl-paste --type text 2>/dev/null; printf x)
current=${current%x}
[[ "$current" == "$data"$'\n'* || "$current" == "$data" ]] || exit 0

printf '%s' "$data" | wl-copy
