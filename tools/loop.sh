#!/usr/bin/env bash
# Keep the container agent working. Same contract as agent_loop.bat: one turn at a time, sleep,
# repeat, and a lock older than 40 minutes counts as a crash so a dead turn never wedges the loop.
#   AGENT_CMD=pi bash tools/loop.sh
set -uo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
lock="$root/state/agent/lock-container"
prompt="$root/state/agent/prompt-container.txt"
cmd="${AGENT_CMD:-pi}"
args="${AGENT_ARGS:---auto}"

while true; do
  if [ -f "$lock" ]; then
    age=$(( ( $(date +%s) - $(stat -c %Y "$lock") ) / 60 ))
    if [ "$age" -lt 40 ]; then sleep 60; continue; fi
    echo "breaking a $age minute lock"
  fi
  date -u +"%Y-%m-%dT%H:%M:%SZ loop turn" > "$lock"
  python3 "$root/tools/brief.py" --for container > "$prompt"
  cd "$root" || exit 1
  # shellcheck disable=SC2086
  $cmd $args < "$prompt"
  rm -f "$lock"
  sleep 45
done
