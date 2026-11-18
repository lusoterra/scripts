#!/bin/sh

mkdir -p "$HOME/.local/share/applications/Steam" && \
    cp -r "$HOME/.var/app/com.valvesoftware.Steam/Desktop/"* \
        "$HOME/.local/share/applications/Steam/"

for file in "$HOME/.local/share/applications/Steam/"*.desktop; do
    if [ -f "$file" ] && grep -q "Exec=steam steam://rungameid/" "$file"; then
        echo "Fixing: $file"
        sed -i 's|Exec=steam steam://rungameid/|Exec=/usr/bin/flatpak run com.valvesoftware.Steam steam://rungameid/|g' "$file"
        chmod +x "$file"
    fi
done

command -v kbuildsycoca6 >/dev/null 2>&1 && kbuildsycoca6
