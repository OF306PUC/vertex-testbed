#!/usr/bin/env bash
# Start one iperf3 server per Pi on the hub, and stop them cleanly.
#
# `iperf3 -s` accepts one test at a time. Three Pis aimed at a single port
# therefore load the medium one after another, which is not the experiment:
# PLATFORM.md 8.2 needs all of them on air during the same run. One port each.
#
#   bash scripts/load_servers.sh start 3     # 3 servers, ports 5201..5203
#   bash scripts/load_servers.sh stop
set -uo pipefail
ACTION="${1:-start}"
N="${2:-3}"
BASE=5201

case "$ACTION" in
  start)
    for i in $(seq 0 $((N-1))); do
        p=$((BASE + i))
        if ss -lntu 2>/dev/null | grep -q ":$p "; then
            echo "  port $p already in use -- skipping"
            continue
        fi
        iperf3 -s -p "$p" >"/tmp/iperf3-$p.log" 2>&1 &
        echo "  server on $p (pid $!, log /tmp/iperf3-$p.log)"
    done
    echo "  point one Pi at each port; concurrent load needs concurrent servers"
    ;;
  stop)
    # Matches a bare `iperf3 -s` too, not just the ones this script started: a
    # hand-started server holds its port and makes `start` skip it silently.
    pkill -f 'iperf3 -s' && echo "  stopped" || echo "  none running"
    ;;
  status)
    pgrep -af 'iperf3 -s' || echo "  no iperf3 servers running"
    ss -lntu 2>/dev/null | awk 'NR==1 || /:52[0-9][0-9] /'
    ;;
  *) echo "usage: $0 {start|stop|status} [n]" >&2; exit 2 ;;
esac
