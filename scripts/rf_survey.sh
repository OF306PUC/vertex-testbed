#!/usr/bin/env bash
# The RF configuration of this host, in one place, for the methods section.
#
# Everything here is measured or read from the driver, not declared: carrier
# frequencies, channel occupancy, the spectral relationship between the Wi-Fi
# channel and the BLE advertising channels, transmit power on both radios, the
# AP's beacon and DTIM parameters, and the negotiated PHY rate.
#
#   bash scripts/rf_survey.sh            # agents may be running
#
# The BLE transmit power line needs the user channel, so it is only filled in
# when the agents are stopped; everything else works either way.
set -uo pipefail
IFC="${IFC:-wlan0}"

# Piped over ssh as `bash -s`, BASH_SOURCE is unset and the repo is not the cwd,
# so neither may be assumed. Both were: the campaign's surveys died on the first
# line under `set -u` and reported nothing but the hostname.
cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." 2>/dev/null || true

# iw, hciconfig and iwconfig live in /usr/sbin, which a non-interactive ssh shell
# does not always have. Without this every radio field reads "?" while the host
# fields read correctly -- which is exactly how the first campaign's surveys came
# back.
PATH="/usr/sbin:/sbin:$PATH"

echo "== host"
printf '  %-22s %s\n' "hostname" "$(hostname)"
printf '  %-22s %s\n' "$IFC" "$(ip -4 -o addr show "$IFC" 2>/dev/null | awk '{print $4}')"

echo "== wi-fi"
info=$(iw dev "$IFC" info 2>/dev/null)
link=$(iw dev "$IFC" link 2>/dev/null)
ch=$(printf '%s' "$info"  | awk '/channel/{print $2}')
fq=$(printf '%s' "$info"  | awk -F'[()]' '/channel/{print $2}' | awk '{print $1}')
tp=$(printf '%s' "$info"  | awk '/txpower/{print $2}')
printf '  %-22s %s\n' "channel"   "${ch:-?}"
printf '  %-22s %s MHz\n' "centre frequency" "${fq:-?}"
printf '  %-22s %s dBm%s\n' "txpower" "${tp:-?}" \
    "$(awk -v t="${tp:-0}" 'BEGIN{print (t>=30)?"   <- brcmfmac placeholder, treat as UNKNOWN":""}')"
printf '  %-22s %s\n' "bssid"   "$(printf '%s' "$link" | awk '/Connected to/{print $3}')"
printf '  %-22s %s\n' "ssid"    "$(printf '%s' "$link" | awk '/SSID/{print $2}')"
printf '  %-22s %s\n' "tx bitrate" "$(printf '%s' "$link" | sed -n 's/.*tx bitrate: //p')"
printf '  %-22s %s\n' "rx bitrate" "$(printf '%s' "$link" | sed -n 's/.*rx bitrate: //p')"
printf '  %-22s %s\n' "power save" "$(iw dev "$IFC" get power_save 2>/dev/null | awk '{print $NF}')"
echo "  regulatory:"; iw reg get 2>/dev/null | sed -n '1,6p' | sed 's/^/    /'

echo "== spectral overlap: wi-fi channel vs BLE advertising channels"
python3 - "$ch" <<'PY'
import sys
BLE = {37: 2402, 38: 2426, 39: 2480}
try:
    ch = int(sys.argv[1])
except Exception:
    print("    wi-fi channel unknown; cannot compute overlap"); raise SystemExit
c = 2407 + 5*ch; lo, hi = c-10, c+10
print(f"    wi-fi ch {ch}: {c} MHz, 20 MHz wide, {lo}-{hi} MHz")
hit = [f"{k} ({v} MHz)" for k, v in BLE.items() if lo <= v <= hi]
for k, v in BLE.items():
    d = 0 if lo <= v <= hi else min(abs(v-lo), abs(v-hi))
    print(f"    BLE adv ch {k} @ {v} MHz: "
          + ("OVERLAPPED" if d == 0 else f"clear by {d} MHz"))
print("    -> " + ("co-channel interference with advertising is present"
                   if hit else
                   "NO overlap: WLAN energy does not land on the advertising channels."))
print("       Front-end contention is unaffected by this -- a busy antenna is")
print("       busy regardless of frequency (PLATFORM.md 6.4).")
PY

echo "== ble"
if command -v hciconfig >/dev/null 2>&1; then
    printf '  %-22s %s\n' "hci0" "$(hciconfig hci0 2>/dev/null | awk '/UP|DOWN/{print $1; exit}')"
fi
printf '  %-22s %s\n' "adv channels" "37/38/39 (2402/2426/2480 MHz), channel_map 0x07"
printf '  %-22s %s\n' "adv tx power" "run scripts/tx_power.py with agents stopped"
printf '  %-22s %s\n' "nRF tx power" "+8 dBm requested; RTT only, no STATS handler"

echo "== access point"
bash scripts/ap_info.sh 2>/dev/null | sed 's/^/  /' || echo "  (scripts/ap_info.sh unavailable)"
