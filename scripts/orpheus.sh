#!/bin/sh

(
    cd "$HOME/.local/share/OrpheusDL"
    ./venv/bin/python "$HOME/.local/share/OrpheusDL/orpheus.py" "$@"
)
