#!/bin/sh

# Get list of all sink names
sinks=($(pactl list short sinks | awk '{print $2}'))
# Get the current default sink name
current=$(pactl get-default-sink)

# Find current and switch to next
for i in "${!sinks[@]}"; do
    if [ "${sinks[$i]}" = "$current" ]; then
        next_index=$(( (i + 1) % ${#sinks[@]} ))
        pactl set-default-sink "${sinks[$next_index]}"
        break
    fi
done
