#!/usr/bin/env bash
# A compact, diffable REPORT of everything that can differ between two Pis.
#
# Not a "fingerprint": it emits facts, not a hash. Two related things are easy to
# confuse and this file is one of them --
#
#   this script                          a list of key: value facts   -> `diff` it
#   check_interpreter.py --fingerprint   one hash of the module set   -> compare it
#
# The digest appears as one line *inside* this report, which is why the names have
# to stay apart.
#
#   bash scripts/host_report.sh
#   diff <(ssh pi1 'bash -s' < scripts/host_report.sh) \
#        <(ssh pi2 'bash -s' < scripts/host_report.sh)
#
# Why this exists rather than "check /etc/os-release": the OS release is one line
# of several that matter, and the ones below it matter more. The platform's
# headline question is BLE-versus-Wi-Fi contention on the CYW43455 (PLATFORM.md
# §3 A2/A3), so a difference in that chip's FIRMWARE between two hosts is a
# difference in the thing being measured -- and it does not appear in any log.
#
# Output is one `key: value` per line, sorted-ish and stable, so `diff` is the
# intended way to read it across hosts.
set -uo pipefail

kv() { printf '%-22s %s\n' "$1:" "${2:-N/A}"; }

# ── identity ────────────────────────────────────────────────────────────────
kv hostname "$(hostname)"
kv model "$(tr -d '\0' < /proc/device-tree/model 2>/dev/null)"

# ── OS ──────────────────────────────────────────────────────────────────────
# VERSION_CODENAME is the one that decides the python version: bullseye ships
# 3.9, bookworm ships 3.11. requires-python is >= 3.11.
if [ -r /etc/os-release ]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    kv os "${PRETTY_NAME:-?}"
    kv os_codename "${VERSION_CODENAME:-?}"
fi
kv debian_version "$(cat /etc/debian_version 2>/dev/null)"
kv kernel "$(uname -r)"
kv arch "$(uname -m)"
kv libc "$(ldd --version 2>/dev/null | head -1 | awk '{print $NF}')"

# ── interpreters ────────────────────────────────────────────────────────────
kv python_system "$(/usr/bin/python3 -V 2>&1)"
for extra in /usr/local/bin/python3 "$PWD/.venv/bin/python3"; do
    [ -x "$extra" ] && kv "python $extra" "$("$extra" -V 2>&1)"
done
if [ -x "$PWD/.venv/bin/python3" ] && [ -r scripts/check_interpreter.py ]; then
    kv python_modules "$("$PWD/.venv/bin/python3" scripts/check_interpreter.py \
        --fingerprint 2>/dev/null)"
fi

# ── the radio: the part that is actually being measured ─────────────────────
# Firmware blobs rather than dmesg: dmesg is often restricted to root, and the
# on-disk blob is what gets loaded. Hash them so a silent update is visible.
for f in /lib/firmware/brcm/brcmfmac43455-sdio.bin \
         /lib/firmware/brcm/brcmfmac43455-sdio.clm_blob \
         /lib/firmware/brcm/BCM4345C0.hcd; do
    if [ -r "$f" ]; then
        kv "fw $(basename "$f")" "$(md5sum "$f" 2>/dev/null | cut -c1-12) \
$(stat -c%s "$f" 2>/dev/null)B"
    fi
done
kv rpi_firmware "$(vcgencmd version 2>/dev/null | tr '\n' ' ' | cut -c1-70)"
kv wifi_driver "$(basename "$(readlink -f /sys/class/net/wlan0/device/driver 2>/dev/null)" 2>/dev/null)"
kv bluez "$(dpkg-query -W -f='${Version}' bluez 2>/dev/null)"
kv bluetoothd_active "$(systemctl is-active bluetooth 2>/dev/null)"

# ── the two things that silently invalidate a comparison ────────────────────
# The AP, from cached scan results so this stays non-disruptive. Beacon interval
# x DTIM period is the worst-case time a BROADCAST frame waits at the AP -- the
# candidate explanation for a 171 ms median UDP delay on links with a 16 ms
# minimum. See PLATFORM.md 8a-xi and scripts/ap_info.sh.
if [ -n "${VERTEX_SSID:-}" ] && command -v iw >/dev/null 2>&1; then
    kv ap_bssid "$(iw dev "$IFACE" link 2>/dev/null | awk '/Connected to/{print $3}')"
    kv ap_ssid "$(iw dev "$IFACE" link 2>/dev/null | awk -F': ' '/SSID/{print $2}')"
    kv ap_beacon_dtim "$(bash "$(dirname "${BASH_SOURCE[0]}")/ap_info.sh" \
        "$VERTEX_SSID" --dump 2>/dev/null |
        awk -F': +' '/beacon interval|DTIM period/{printf "%s ", $2}')"
fi

kv chrony_ref "$(chronyc tracking 2>/dev/null | awk -F': *' '/Reference ID/{print $2}')"
kv chrony_offset "$(chronyc tracking 2>/dev/null | awk -F': *' '/System time/{print $2}')"
kv wlan_channel "$(iw dev wlan0 info 2>/dev/null | awk '/channel/{print $2, $3, $4}')"
kv wlan_powersave "$(iw dev wlan0 get power_save 2>/dev/null | awk '{print $NF}')"
