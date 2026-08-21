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
# Resolved to an ABSOLUTE path, not left as a bare name.
#
# `sudo -E` preserves the environment but NOT the PATH lookup: Debian's sudoers
# sets `secure_path`, which overrides PATH for resolving the command. So a bare
# `python3` under sudo finds /usr/bin/python3 while the unprivileged agents find
# the venv's -- the bridge would then run a different interpreter, with different
# packages, from its two siblings. Silent, and a miserable thing to diagnose.
PY=${VERTEX_PY:-python3}
PY=$(command -v "$PY" 2>/dev/null || echo "$PY")

mkdir -p "$RUNDIR"

pidfile() { echo "$RUNDIR/$1.pid"; }
logfile() { echo "$RUNDIR/$1.log"; }

# Does the interpreter already carry CAP_NET_ADMIN?
has_cap() {
    command -v getcap >/dev/null 2>&1 &&
        getcap "$(readlink -f "$(command -v $PY)")" 2>/dev/null | grep -q net_admin
}

# Only `bridge` needs privileges -- it binds the HCI user channel, which is
# exclusive and root-only. `ble` needs the serial port (dialout) and `wifi` needs
# nothing. Elevating all three would be simpler and wrong: it makes every run log
# root-owned and hands the radio to processes that never touch it.
needs_sudo() {
    [ "$1" = "bridge" ] && [ "$(id -u)" -ne 0 ] && ! has_cap
}

privilege_note() {
    if [ "$(id -u)" -eq 0 ]; then echo "running as root"
    elif has_cap; then echo "cap_net_admin on $PY"
    elif command -v sudo >/dev/null 2>&1; then echo "sudo, for the bridge only"
    else echo "NONE -- bridge cannot bind the HCI user channel"
    fi
}

alive() {
    local pf pid; pf=$(pidfile "$1")
    [ -f "$pf" ] || return 1
    pid=$(cat "$pf")
    # /proc rather than `kill -0`: signalling a root-owned process from an
    # unprivileged shell fails with EPERM, which is indistinguishable from "gone".
    [ -d "/proc/$pid" ]
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

    echo "== python"
    printf '  %-22s %s\n' "interpreter" "$PY"
    if [ -n "${VIRTUAL_ENV:-}" ]; then
        printf '  %-22s %s\n' "venv" "$VIRTUAL_ENV"
        case "$PY" in
        "$VIRTUAL_ENV"/*) ;;
        *) printf '  %-22s WARN a venv is active but %s is outside it\n' "" "$PY" ;;
        esac
    fi
    # StrEnum is 3.11+, and vertex/net.py imports it at module scope, so an older
    # interpreter fails at import with a traceback rather than a clear message.
    # pyproject declares >=3.11; check it here so the reason is one line.
    local pyver
    pyver=$($PY -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])' 2>/dev/null)
    if $PY -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
        printf '  %-22s %s\n' "$PY" "$pyver"
    else
        printf '  %-22s FAIL %s -- vertex needs >= 3.11 (enum.StrEnum). Raspberry Pi OS\n' "$PY" "${pyver:-unknown}"
        printf '  %-22s      Bullseye ships 3.9; Bookworm ships 3.11.\n' ""
        bad=1
    fi

    # The version alone is not enough: the agent needs numpy, pydantic, networkx
    # and pyyaml, and a fresh venv has none of them. Importing the agent module is
    # the check that matches what `start` will actually do -- checking the version
    # and then failing on ModuleNotFoundError three times is not a preflight.
    local imp
    imp=$($PY -c 'import vertex.agent.__main__' 2>&1) \
        && printf '  %-22s vertex.agent imports\n' "dependencies" \
        || {
            printf '  %-22s FAIL %s\n' "dependencies" "$(echo "$imp" | tail -1)"
            printf '  %-22s      fix: %s -m pip install -e .\n' "" "$PY"
            bad=1
        }

    echo "== clock (the shared epoch is only shared if these are)"
    if command -v chronyc >/dev/null 2>&1; then
        local off ref
        off=$(chronyc tracking 2>/dev/null | awk -F': *' '/System time/{print $2}')
        ref=$(chronyc tracking 2>/dev/null | awk -F': *' '/Reference ID/{print $2}')
        if [ -z "$off" ]; then
            printf '  %-22s FAIL chronyc present but not tracking\n' "chrony"; bad=1
        elif [ "${ref%% *}" = "00000000" ]; then
            # The trap this check exists for. With no source, chrony reports the
            # offset against the LOCAL clock, so it reads as a few nanoseconds and
            # looks perfect -- while the host is aligned with nothing. Two nodes in
            # that state share an epoch in name only, and every cross-node delay
            # is measuring their clock offset instead.
            printf '  %-22s FAIL no source (ref 00000000). Offset reads %s and\n' "chrony" "$off"
            printf '  %-22s      means nothing -- it is against its own clock.\n' ""
            printf '  %-22s      Check: chronyc sources -v ; chronyc makestep\n' ""
            bad=1
        else
            printf '  %-22s %s (ref %s)\n' "chrony offset" "$off" "$ref"
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
                UP)   printf '  %-22s FAIL UP -- bluetoothd holds it; the user channel\n' "hci0"
                      printf '  %-22s      is exclusive. Now:  sudo hciconfig hci0 down\n' ""
                      printf '  %-22s      Permanently (it comes back on every boot):\n' ""
                      printf '  %-22s        sudo systemctl disable --now bluetooth\n' ""
                      bad=1 ;;
                *)    printf '  %-22s FAIL no hci0\n' "hci0"; bad=1 ;;
                esac
            else
                printf '  %-22s ?    hciconfig missing; cannot check\n' "hci0"
            fi
            # Only this agent needs elevation, and there are three ways to get
            # it -- report which one will actually be used rather than failing on
            # the absence of one particular route.
            local note; note=$(privilege_note)
            case "$note" in
            NONE*) printf '  %-22s FAIL %s\n' "privileges" "$note"
                   printf '  %-22s      fix: sudo setcap cap_net_admin,cap_net_raw+eip \\\n' ""
                   printf '  %-22s              $(readlink -f $(command -v %s))\n' "" "$PY"
                   bad=1 ;;
            *)     printf '  %-22s %s\n' "privileges" "$note" ;;
            esac
            ;;
        wifi)
            # UdpTransport broadcasts to this; a wrong prefix means neighbours
            # never hear each other while both agents look healthy.
            local bcast
            bcast=$($PY -c "
import sys
from vertex.net import broadcast_address, resolve_local_ip, InterfaceError
try:
    print(broadcast_address(resolve_local_ip('$IFACE')))
except InterfaceError as exc:
    sys.exit(str(exc))
" 2>&1) \
                && printf '  %-22s %s (assumes /24)\n' "UDP broadcast to" "$bcast" \
                || printf '  %-22s ?    %s\n' "UDP broadcast to" "$bcast"
            ;;
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

        local pre=()
        if needs_sudo "$t"; then
            # -E keeps the environment, so VERTEX_* and PYTHONPATH survive.
            pre=(sudo -E)
        fi
        nohup "${pre[@]}" $PY -u -m vertex.agent "${args[@]}" \
            >"$(logfile "$t")" 2>&1 &
        echo $! >"$(pidfile "$t")"
        echo "  started $t (pid $!)${pre:+ [sudo]} -> $(logfile "$t")"
    done
    sleep 1
    status
}

stop() {
    for t in $TYPES; do
        local pf pid; pf=$(pidfile "$t")
        if alive "$t"; then
            pid=$(cat "$pf")
            # A sudo'd bridge cannot be signalled by the unprivileged user; sudo
            # forwards TERM to its child, so signal sudo itself.
            kill -TERM "$pid" 2>/dev/null ||
                sudo kill -TERM "$pid" 2>/dev/null
            echo "  stopping $t (pid $pid)"
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
