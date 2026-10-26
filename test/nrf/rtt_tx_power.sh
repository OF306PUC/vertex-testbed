#!/usr/bin/env bash
# Show the nRF's BLE transmit power, and the rest of its radio setup, from RTT.
#
# NOT over the serial port. `prj.conf` sets CONFIG_UART_CONSOLE=n and
# CONFIG_LOG_BACKEND_UART=n, so logging goes to RTT over SWD and the UART carries
# only the framed binary protocol -- a serial monitor shows you 0x7E frames, not
# text. This attaches to RTT instead, which is non-intrusive: the agent can keep
# running while it captures.
#
# The line you want is emitted by broadcaster_init(), which runs at the RUN
# TRIGGER, not at boot. So: start this, then start a run, then read.
#
#   bash test/nrf/rtt_tx_power.sh                 # capture until Ctrl-C
#   bash test/nrf/rtt_tx_power.sh 60              # capture for 60 s
#
# What to look for:
#   "Tx power: 8 dBm"                  requested and granted (nRF52840)
#   "Tx power 8 dBm requested, 4 ..."  granted less than asked (nRF52832)
#   no line at all                     the vendor command failed; see "Set Tx power err"
set -uo pipefail
SECS="${1:-0}"
DEV="${DEV:-NRF52840_XXAA}"
OUT="${OUT:-/tmp/rtt-$(date +%H%M%S).log}"

pick() { command -v "$1" >/dev/null 2>&1 && echo "$1"; }
LOGGER=$(pick JLinkRTTLogger || true)
EXE=$(pick JLinkExe || true)
NRFUTIL=$(pick nrfutil || true)

if [ -z "$LOGGER" ] && [ -z "$EXE" ] && [ -z "$NRFUTIL" ]; then
    cat >&2 <<'MSG'
No RTT tool found. Install one of:
  - SEGGER J-Link pack  -> JLinkRTTLogger, JLinkExe   (the DK has an onboard J-Link)
  - nrfutil             -> nrfutil device x-rtt-read
Alternatively read it without RTT at all: the value is deterministic for a given
board. broadcaster.h requests TX_POWER_LEVEL_BLE = +8 dBm; an nRF52840 grants +8,
an nRF52832 caps at +4. The log only confirms which happened.
MSG
    exit 1
fi

echo "capturing RTT from $DEV -> $OUT"
echo "  start a run now; the Tx power line is emitted at the trigger."
if [ -n "$LOGGER" ]; then
    # channel 0 is the default log channel
    JLinkRTTLogger -Device "$DEV" -If SWD -Speed 4000 -RTTChannel 0 "$OUT" &
else
    JLinkExe -Device "$DEV" -If SWD -Speed 4000 -AutoConnect 1 \
             -RTTTelnetPort 19021 >/dev/null 2>&1 &
    sleep 2
    (command -v JLinkRTTClient >/dev/null 2>&1 && JLinkRTTClient >"$OUT" 2>&1) &
fi
pid=$!
trap 'kill $pid 2>/dev/null; echo; echo "-- stopped"' INT TERM

if [ "$SECS" -gt 0 ] 2>/dev/null; then sleep "$SECS"; kill $pid 2>/dev/null
else echo "  Ctrl-C when done"; wait $pid 2>/dev/null || true; fi

echo
echo "== radio lines captured =="
grep -aE "Tx power|adv interval|Scanning started|Advertising started|Set Tx power err" \
     "$OUT" 2>/dev/null | sed 's/^/  /' || echo "  none -- was a run triggered while capturing?"
echo
echo "full log: $OUT"
