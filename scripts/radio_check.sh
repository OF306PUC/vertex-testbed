#!/usr/bin/env bash
# Read-only radio diagnosis for one node. Changes nothing.
#
#   on the Pi:    bash radio_check.sh
#   from the hub: ssh control@<ip> 'bash -s' < scripts/radio_check.sh
#   all nodes:    for ip in ...; do echo "== $ip =="; \
#                   ssh control@$ip 'bash -s' < scripts/radio_check.sh; done
#
# Answers: is the onboard WLAN enabled, which physical radio is wlan0, what
# channel are we on, is power save off, and is BLE still there.

set -uo pipefail
ok()   { printf '  \033[32mok\033[0m    %s\n' "$*"; }
bad()  { printf '  \033[31mFIX\033[0m   %s\n' "$*"; }
info() { printf '        %s\n' "$*"; }

CFG=""
for c in /boot/firmware/config.txt /boot/config.txt; do
    [ -f "$c" ] && { CFG="$c"; break; }
done

echo "1. onboard WLAN enabled?"
if [ -z "$CFG" ]; then
    info "no Raspberry Pi config.txt found -- not a Pi, skipping overlay check"
elif grep -qE '^\s*dtoverlay=disable-wifi' "$CFG" 2>/dev/null; then
    bad "dtoverlay=disable-wifi present in $CFG -- onboard WLAN is off"
    info "remove that line and reboot to re-enable it"
else
    ok "no disable-wifi overlay in $CFG"
fi
if command -v rfkill >/dev/null 2>&1; then
    if rfkill list wifi 2>/dev/null | grep -q 'yes'; then
        bad "rfkill is blocking wifi:"; rfkill list wifi | sed 's/^/        /'
        info "clear with: sudo rfkill unblock wifi"
    else
        ok "rfkill is not blocking wifi"
    fi
fi
if lsmod 2>/dev/null | grep -q '^brcmfmac'; then
    ok "brcmfmac module loaded (onboard driver)"
else
    bad "brcmfmac not loaded -- the onboard radio has no driver"
fi

echo "2. which physical radio is each interface?"
if command -v iw >/dev/null 2>&1; then
    for dev in $(iw dev 2>/dev/null | awk '/Interface/{print $2}'); do
        drv=$(basename "$(readlink -f "/sys/class/net/$dev/device/driver" 2>/dev/null)" 2>/dev/null)
        case "$drv" in
            brcmfmac) ok  "$dev -> $drv  (ONBOARD CYW43455 -- what we want)" ;;
            ""|unknown) info "$dev -> driver unknown" ;;
            *)        bad "$dev -> $drv  (external USB dongle -- to be removed)" ;;
        esac
    done
else
    bad "iw not installed: sudo apt install iw"
fi

echo "3. channel (set on the ROUTER, not here)"
for dev in $(iw dev 2>/dev/null | awk '/Interface/{print $2}'); do
    ch=$(iw dev "$dev" info 2>/dev/null | awk '/channel/{print $2; exit}')
    freq=$(iw dev "$dev" info 2>/dev/null | grep -o 'channel [0-9]* ([0-9]* MHz)' | head -1)
    [ -z "${ch:-}" ] && { info "$dev: not associated"; continue; }
    case "$ch" in
        11) ok  "$dev on channel 11 -- clear of BLE adv 37/38/39" ;;
        1)  bad "$dev on channel 1 (2402-2422 MHz) -- collides with BLE adv ch 37 (2402 MHz)" ;;
        6)  bad "$dev on channel 6 (2427-2447 MHz) -- collides with BLE adv ch 38 (2426 MHz)" ;;
        *)  bad "$dev on channel $ch ${freq:+($freq)} -- prefer 11; check overlap with 2402/2426/2480 MHz" ;;
    esac
done

echo "4. power save (adds tens of ms of non-deterministic latency)"
for dev in $(iw dev 2>/dev/null | awk '/Interface/{print $2}'); do
    ps=$(iw dev "$dev" get power_save 2>/dev/null | awk '{print $NF}')
    case "$ps" in
        off) ok  "$dev power_save off" ;;
        on)  bad "$dev power_save ON -- disable it (see below)" ;;
        *)   info "$dev power_save unknown" ;;
    esac
done
if [ -d /etc/NetworkManager/conf.d ]; then
    if grep -rqs 'wifi.powersave *= *2' /etc/NetworkManager/conf.d/; then
        ok "power save disabled persistently via NetworkManager"
    else
        bad "no persistent power-save setting -- it returns on reboot"
    fi
fi

echo "5. BLE controller still present?"
if command -v hciconfig >/dev/null 2>&1 && hciconfig 2>/dev/null | grep -q hci0; then
    ok "hci0 present"
elif [ -d /sys/class/bluetooth/hci0 ]; then
    ok "hci0 present (sysfs)"
else
    bad "no hci0 -- BLE is missing, which the disable-wifi overlay should NOT cause"
fi

echo "6. addressing (needed for the topology manifest)"
for dev in $(iw dev 2>/dev/null | awk '/Interface/{print $2}'); do
    ip -4 -o addr show "$dev" 2>/dev/null | awk '{printf "        %s  %s\n", $2, $4}'
done
info "regulatory domain: $(iw reg get 2>/dev/null | awk '/country/{print $2; exit}')"
