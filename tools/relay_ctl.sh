#!/usr/bin/env bash
# Control for the relay bridge. Kept separate from the daemon so start/stop never has to guess a
# pid by command-name matching (which also matches the shell that runs the match).
#
#   bash tools/relay_ctl.sh start|stop|status|logs
set -uo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
pidfile="$root/state/agent/relay.pid"
logfile="$root/state/agent/relay.log"
python="/lsiopy/bin/python3"
[ -x "$python" ] || python="python3"

running() { [ -f "$pidfile" ] && kill -0 "$(cat "$pidfile")" 2>/dev/null; }

case "${1:-status}" in
  start)
    if running; then echo "already up (pid $(cat "$pidfile"))"; exit 0; fi
    cd "$root"
    nohup "$python" -u tools/relay_daemon.py >> "$logfile" 2>&1 &
    echo $! > "$pidfile"
    sleep 3
    if running; then echo "up (pid $(cat "$pidfile"))"; else echo "failed, see $logfile"; tail -5 "$logfile"; fi
    ;;
  stop)
    if running; then kill "$(cat "$pidfile")"; sleep 1; echo "stopped"; else echo "not running"; fi
    rm -f "$pidfile"
    ;;
  status)
    if running; then echo "up (pid $(cat "$pidfile"))"; else echo "down"; fi
    grep -E "gateway ready|relay up" "$logfile" 2>/dev/null | tail -2
    ;;
  logs) tail -f "$logfile" ;;
  *) echo "usage: $0 start|stop|status|logs"; exit 1 ;;
esac
