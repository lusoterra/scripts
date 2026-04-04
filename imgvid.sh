#!/bin/sh

ffmpeg -loop 1 -i "$1" -i "$2" \
  -c:v libx264 -tune stillimage \
  -c:a copy -shortest -vf "crop=trunc(iw/2)*2:trunc(ih/2)*2" \
  "$3"
