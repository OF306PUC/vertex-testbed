#!/usr/bin/env bash
# What the access point is, and the two numbers that set broadcast latency.
#
#   sudo bash scripts/ap_info.sh                       # your SSID from the env
#   sudo bash scripts/ap_info.sh oficina_v2
#   bash scripts/ap_info.sh oficina_v2 --dump          # cached, no disruption
#
# Why this and not `iw dev wlan0 link`: the numbers that matter live in the
# BEACON, not in the association state, so they only appear in a scan.
#
#   beacon interval   in TUs, 1 TU = 1024 us. Usually 100 TUs = 102.4 ms.
#   DTIM period       beacons between delivery of BUFFERED BROADCAST/MULTICAST.
#
# Their product is the worst-case time a broadcast frame waits at the AP whenever
# any associated station is power-saving -- including stations that are not ours.
# That is the candidate explanation for a 171 ms median UDP delay measured on links
# with a 16 ms minimum, on Pis whose own power_save is off. See PLATFORM.md 8a-xi.
#
# A live scan briefly interrupts traffic on the interface: do not run it during an
# experiment. `--dump` reads the cached results instead and needs no root.
set -uo pipefail

SSID=${1:-${VERTEX_SSID:-}}
IFACE=${VERTEX_IFACE:-wlan0}
MODE=scan
[ "${2:-}" = "--dump" ] && MODE="scan dump"
[ "${1:-}" = "--dump" ] && { MODE="scan dump"; SSID=${VERTEX_SSID:-}; }

if [ -z "$SSID" ]; then
    echo "usage: $0 <ssid> [--dump]      (or set VERTEX_SSID)" >&2
    exit 2
fi
command -v iw >/dev/null 2>&1 || { echo "iw is not installed" >&2; exit 2; }

# RS on "\nBSS " so each record is one BSS, then keep the one with our SSID.
# Matching on the SSID line specifically, not anywhere in the record: an SSID can
# appear inside another BSS's IEs.
# shellcheck disable=SC2086
iw dev "$IFACE" $MODE 2>/dev/null | awk -v want="$SSID" '
    BEGIN { RS = "\nBSS "; FS = "\n" }
    {
        rec = $0; has = 0
        n = split(rec, L, "\n")
        for (i = 1; i <= n; i++) if (L[i] ~ /^[ \t]*SSID:[ \t]*/) {
            s = L[i]; sub(/^[ \t]*SSID:[ \t]*/, "", s)
            if (s == want) has = 1
        }
        if (!has) next
        bssid = rec; sub(/\(on .*/, "", bssid); gsub(/^BSS |[ \t\n].*$/, "", bssid)
        assoc = (rec ~ /-- associated/) ? "  <-- this host is associated" : ""
        printf "  %-18s %s%s\n", "BSSID:", bssid, assoc
        beacon = ""; dtim = ""
        for (i = 1; i <= n; i++) {
            l = L[i]; gsub(/^[ \t]+/, "", l)
            if (l ~ /^SSID:/)            printf "  %-18s %s\n", "SSID:", substr(l, 7)
            if (l ~ /^freq:/)            printf "  %-18s %s\n", "freq:", substr(l, 7)
            if (l ~ /^signal:/)          printf "  %-18s %s\n", "signal:", substr(l, 9)
            if (l ~ /^beacon interval:/) { beacon = l; sub(/[^0-9]*/, "", beacon)
                                           sub(/[^0-9].*/, "", beacon) }
            if (l ~ /^TIM:/)             { dtim = l
                                           sub(/.*DTIM Period[ \t]*/, "", dtim)
                                           sub(/[^0-9].*/, "", dtim) }
        }
        if (beacon != "") {
            ms = beacon * 1024 / 1000
            printf "  %-18s %s TUs = %.1f ms\n", "beacon interval:", beacon, ms
            if (dtim != "") {
                printf "  %-18s %s\n", "DTIM period:", dtim
                worst = ms * dtim
                printf "  %-18s %.1f ms worst, %.1f ms mean\n",
                       "broadcast buffer:", worst, worst / 2
                printf "\n  A broadcast frame waits up to %.0f ms at this AP whenever any\n", worst
                printf "  associated station is power-saving. Compare with the measured\n"
                printf "  UDP median delay: if they agree, that is the mechanism.\n"
            } else {
                printf "  %-18s not in the beacon IEs (try a live scan, not --dump)\n", "DTIM period:"
            }
        }
        found = 1
    }
    END { if (!found) print "  no BSS with that SSID in the scan results" }
'
