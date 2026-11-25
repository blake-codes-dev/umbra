#!/usr/bin/env bash
# Set the live bisect flags the payload reads. Written to a file the container can also edit, so a
# bisect needs no message round trip. Words are matched as substrings of the whole file.
# usage: flags.sh [nohook] [nosmoke] [nostub] [none]
set -e
file="$(cd "$(dirname "$0")/../.." && pwd)/state/live-flags.txt"
if [ "${1:-}" = "none" ] || [ $# -eq 0 ]; then
  : > "$file"
  echo "flags cleared"
else
  printf '%s\n' "$@" > "$file"
  echo "flags: $*"
fi
