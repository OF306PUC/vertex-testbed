#!/usr/bin/env bash
# Everything that can be checked without hardware.
#
# The hardware harnesses under test/loopback-uart-ble-{a,b} are not run here:
# they need an nRF on a UART and a Bluetooth adapter with the interface down.
#
#   bash test/check_all.sh
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

fail=0
run() {
    echo "== $1"
    shift
    "$@" || fail=$((fail + 1))
    echo
}

# Would `west build` accept the C? Catches the two classes of error that reached
# the bench: an edit whose declaration never landed, and a missing #include.
run "firmware syntax: nordic"          bash test/common/syntax_check.sh firmware/nordic
run "firmware syntax: loopback a"      bash test/common/syntax_check.sh test/loopback-uart-ble-a/firmware
run "firmware syntax: loopback b"      bash test/common/syntax_check.sh test/loopback-uart-ble-b/firmware
run "firmware symbols"                 python3 test/common/check_firmware_symbols.py firmware/nordic

# Does the host encode what each firmware decodes? All three, not just nordic:
# PROTO_CONTROL_LEN grew on the host and on nordic while both peers stayed at the
# old length, and checking one firmware could not see it.
run "serial layout: nordic"            python3 test/common/check_proto_layout.py firmware/nordic
run "serial layout: loopback a"        python3 test/common/check_proto_layout.py test/loopback-uart-ble-a/firmware
run "serial layout: loopback b"        python3 test/common/check_proto_layout.py test/loopback-uart-ble-b/firmware

# Does the firmware's on-air struct match the host's v0 decoder?
run "on-air v1 codec"                 python3 test/crossval/check_air_wire.py

# Are the C and Python control laws the same dynamical system?
run "control law C vs Python"          python3 test/crossval/compare.py

# Does the BLE transport behave against a fake controller?
run "ble transport"                    python3 test/transports/check_ble.py

# Does the whole host-side path work: hub -> agents -> logs -> collected files?
run "fleet end to end"                 python3 test/hub/check_fleet.py

if [ "$fail" -gt 0 ]; then
    echo "$fail check(s) failed"
    exit 1
fi
echo "all checks passed"
