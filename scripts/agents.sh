#!/usr/bin/env bash
# Start, stop and inspect this host's three agents.
#
#   bash scripts/agents.sh preflight        # check before you commit to a run
#   bash scripts/agents.sh start
#   bash scripts/agents.sh status
#   bash scripts/agents.sh logs bridge
#   bash scripts/agents.sh stop
#
# Six agents over two hosts is six terminals otherwise. This is the stopgap; the
# durable answer is templated systemd units (PLATFORM.md D6), which also survive a
# reboot and hand their logs to journald.
#
# Deliberately does NOT pass --epoch. The epoch is per-run and the hub sends it
# with the trigger, so every node in a run shares one origin. An epoch fixed at
# launch would give each agent its own.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

TYPES=${VERTEX_TYPES:-"ble wifi bridge"}
SERIAL=${VERTEX_SERIAL:-/dev/ttyACM0}
IFACE=${VERTEX_IFACE:-wlan0}
DATA=${VERTEX_DATA:-data}
RUNDIR=${VERTEX_RUNDIR:-/tmp/vertex-agents}
PY=${VERTEX_PY:-python3}

mkdir -p "$RUNDIR"

pidfile() { echo "$RUNDIR/$1.pid"; }
logfile() { echo "$RUNDIR/$1.log"; }

alive() {
    local pf; pf=$(pidfile "$1")
    [ -f "$pf" ] && kill -0 "$(cat "$pf")" 2>/dev/null
}

preflight() {
    local bad=0
    echo "== host"
    printf '  %-22s %s\n' "hostname" "$(hostname)"
    local ip
    # One line on failure, not a traceback: this is a checklist, and a stack trace
    # in the middle of it hides the other items.
    ip=$($PY -c "
import sys
from vertex.net import resolve_local_ip, InterfaceError
try:
    print(resolve_local_ip('$IFACE'))
except InterfaceError as exc:
    sys.exit(str(exc))
" 2>&1) \
        && printf '  %-22s %s\n' "$IFACE" "$ip" \
        || { printf '  %-22s FAIL %s\n' "$IFACE" "$ip"; bad=1; }

    echo "== clock (the shared epoch is only shared if these are)"
    if command -v chronyc >/dev/null 2>&1; then
        local off src
        off=$(chronyc tracking 2>/dev/null | awk -F': *' '/System time/{print $2}')
        src=$(chronyc tracking 2>/dev/null | awk -F': *' '/Reference ID/{print $2}')
        if [ -n "$off" ]; then
            printf '  %-22s %s (ref %s)\n' "chrony offset" "$off" "${src:-?}"
        else
            printf '  %-22s FAIL chronyc present but not tracking\n' "chrony"; bad=1
        fi
    else
        printf '  %-22s FAIL not installed -- timestamps will not be comparable\n' "chrony"
        bad=1
    fi

    for t in $TYPES; do
        echo "== $t"
        case "$t" in
        ble)
            if [ -e "$SERIAL" ]; then
                printf '  %-22s %s\n' "nRF port" "$SERIAL"
                [ -r "$SERIAL" ] && [ -w "$SERIAL" ] \
                    || { printf '  %-22s FAIL not readable/writable (dialout group?)\n' "permissions"; bad=1; }
            else
                printf '  %-22s FAIL %s missing -- is the nRF attached?\n' "nRF port" "$SERIAL"; bad=1
            fi
            ;;
        bridge)
            # The HCI user channel is exclusive: BlueZ must not hold the adapter.
            if command -v hciconfig >/dev/null 2>&1; then
                local st
                st=$(hciconfig hci0 2>/dev/null | awk '/UP|DOWN/{print $1; exit}')
                case "$st" in
                DOWN) printf '  %-22s DOWN (correct)\n' "hci0" ;;
                UP)   printf '  %-22s FAIL UP -- run: sudo hciconfig hci0 down\n' "hci0"; bad=1 ;;
                *)    printf '  %-22s FAIL no hci0\n' "hci0"; bad=1 ;;
                esac
            else
                printf '  %-22s ?    hciconfig missing; cannot check\n' "hci0"
            fi
            if [ "$(id -u)" -ne 0 ]; then
                if command -v getcap >/dev/null 2>&1 &&
                   getcap "$(readlink -f "$(command -v $PY)")" 2>/dev/null | grep -q net_admin; then
                    printf '  %-22s cap_net_admin on %s\n' "privileges" "$PY"
                else
                    printf '  %-22s FAIL needs root or cap_net_admin on %s\n' "privileges" "$PY"; bad=1
                fi
            else
                printf '  %-22s root\n' "privileges"
            fi
            ;;
        wifi)
            printf '  %-22s nothing beyond the LAN\n' "requires" ;;
        esac
    done

    echo
    [ "$bad" -eq 0 ] && echo "preflight clean" || echo "preflight found problems (see FAIL above)"
    return "$bad"
}

start() {
    for t in $TYPES; do
        if alive "$t"; then
            echo "  $t already running (pid $(cat "$(pidfile "$t")"))"
            continue
        fi
        local args=(--type "$t" --data-dir "$DATA" --interface "$IFACE")
        [ "$t" = "ble" ] && args+=(--serial "$SERIAL")
        nohup $PY -u -m vertex.agent "${args[@]}" >"$(logfile "$t")" 2>&1 &
        echo $! >"$(pidfile "$t")"
        echo "  started $t (pid $!) -> $(logfile "$t")"
    done
    sleep 1
    status
}

stop() {
    for t in $TYPES; do
        local pf; pf=$(pidfile "$t")
        if alive "$t"; then
            kill -TERM "$(cat "$pf")" 2>/dev/null
            echo "  stopping $t (pid $(cat "$pf"))"
        fi
        rm -f "$pf"
    done
}

status() {
    for t in $TYPES; do
        if alive "$t"; then
            printf '  %-8s up   pid %-8s %s\n' "$t" "$(cat "$(pidfile "$t")")" \
                "$(tail -n1 "$(logfile "$t")" 2>/dev/null | cut -c1-90)"
        else
            printf '  %-8s DOWN      %s\n' "$t" \
                "$(tail -n1 "$(logfile "$t")" 2>/dev/null | cut -c1-90)"
        fi
    done
}

case "${1:-status}" in
    preflight) preflight ;;
    start)     start ;;
    stop)      stop ;;
    restart)   stop; sleep 1; start ;;
    status)    status ;;
    logs)      tail -f "$(logfile "${2:?which agent: ble|wifi|bridge}")" ;;
    *) echo "usage: $0 {preflight|start|stop|restart|status|logs <type>}" >&2; exit 2 ;;
esac
