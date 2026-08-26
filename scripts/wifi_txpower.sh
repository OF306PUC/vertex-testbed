#!/usr/bin/env bash
# Is Wi-Fi transmit power actually settable on this host, or only reported?
#
# `iw` shows "txpower 31.00 dBm" on many Raspberry Pis. That is not a measurement:
# brcmfmac reports a fixed placeholder when it does not expose real TX power, and
# 31 dBm (~1.3 W) exceeds both the CYW43455's capability and every 2.4 GHz
# regulatory limit. Before treating TX power as a controlled variable, establish
# whether the driver honours a change at all.
#
#   bash scripts/wifi_txpower.sh            # report only
#   bash scripts/wifi_txpower.sh 20         # try to set 20 dBm, then verify
set -uo pipefail
IFC="${IFC:-wlan0}"
WANT="${1:-}"

say() { printf '%s\n' "$*"; }

say "== interface"
iw dev "$IFC" info 2>/dev/null | sed 's/^/  /' || { say "  no $IFC"; exit 1; }

say "== regulatory domain (this is the real cap, not what iw reports as txpower)"
iw reg get 2>/dev/null | sed -n '1,12p' | sed 's/^/  /'

cur=$(iw dev "$IFC" info 2>/dev/null | awk '/txpower/{print $2}')
say "== reported txpower: ${cur:-unknown} dBm"
case "$cur" in
  31.00) say "  -> the brcmfmac placeholder. Treat as UNKNOWN, not as 31 dBm." ;;
esac

[ -z "$WANT" ] && { say "== pass a dBm value to test whether setting works"; exit 0; }

mbm=$((WANT * 100))
say "== attempting: iw dev $IFC set txpower fixed $mbm   (${WANT} dBm)"
if sudo iw dev "$IFC" set txpower fixed "$mbm" 2>&1 | sed 's/^/  /'; then
    say "  command accepted"
else
    say "  command REFUSED -- driver does not support setting txpower"
fi

new=$(iw dev "$IFC" info 2>/dev/null | awk '/txpower/{print $2}')
say "== readback: ${new:-unknown} dBm"
if [ "$new" = "$cur" ]; then
    say "  UNCHANGED -- accepted but ignored, or not supported."
    say "  Wi-Fi TX power is NOT a controllable variable on this host."
else
    say "  changed ${cur} -> ${new}. TX power IS controllable; record it per run"
    say "  and note that changing it invalidates comparison with existing data."
fi
say "== restore with: sudo iw dev $IFC set txpower auto"
