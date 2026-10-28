#!/bin/sh

# If GPG_TTY is set and standard input is a terminal, use curses (terminal) pinentry
if [ -n "$GPG_TTY" ]; then
    exec pinentry-tty "$@"
else
    # Otherwise, fall back to your preferred GUI pinentry (e.g., pinentry-qt, pinentry-gnome3, or pinentry-gtk-2)
    exec pinentry-qt "$@"
fi
