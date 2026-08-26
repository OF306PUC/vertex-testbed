#!/usr/bin/env bash
# Generate a measured Wi-Fi load on this Pi, for the coexistence experiment.
#
# PLATFORM.md 8.2: the claim is that the Pi's BLE *receive* path degrades under
# WLAN activity while its BLE *transmit* path does not, because PTA can schedule
# the chip's own transmissions but not an external advertiser's. That predicts a
# DIRECTIONAL effect -- nRF->Pi falls, Pi->nRF holds -- which is what
# distinguishes coexistence from ordinary congestion.
#
# Load must be generated ON the Pi, not at it: the mechanism is the Pi's own
# radio competing with its own BLE receiver, so the traffic has to originate here.
#
#   # on the hub laptop, ONE SERVER PER PI -- see the PORT note below:
#   for p in 5201 5202 5203; do iperf3 -s -p $p & done
#
#   # on each Pi, just before triggering the run (one port each):
#   bash scripts/wlan_load.sh 10.6.0.39 5 130 5201    # pi1
#   bash scripts/wlan_load.sh 10.6.0.39 5 130 5202    # pi2
#   bash scripts/wlan_load.sh 10.6.0.39 5 130 5203    # pi4
#
# Then start the experiment from the hub. Runs slightly longer than the run so
# the load covers the whole measurement.
set -uo pipefail

SERVER="${1:?usage: wlan_load.sh <server-ip> <mbps> <seconds> [port]}"
MBPS="${2:?}"
SECS="${3:-130}"
# One PORT PER PI, and this matters: `iperf3 -s` serves one test at a time, so
# three Pis pointed at one port load the medium SEQUENTIALLY -- observed as
# "test #1 ... test #2 ... test #3", 130 s each, one Pi on air at a time. The
# experiment needs them concurrent, so each Pi gets its own server.
PORT="${4:-5201}"
OUT="${OUT:-/tmp/wlan_load-$(date +%H%M%S).json}"

command -v iperf3 >/dev/null 2>&1 || {
    echo "iperf3 not installed:  sudo apt-get install -y iperf3" >&2; exit 1; }

echo "load: ${MBPS} Mbit/s UDP to ${SERVER}:${PORT} for ${SECS}s  -> $OUT"
# UDP, not TCP: TCP adapts its rate to loss, so the offered load would not be the
# controlled variable -- it would fall exactly when BLE contention rose, which is
# the interaction being measured.
iperf3 -c "$SERVER" -p "$PORT" -u -b "${MBPS}M" -t "$SECS" -J >"$OUT" 2>/dev/null &
pid=$!
echo "  iperf3 pid $pid; it exits on its own after ${SECS}s"
echo "  stop early with: kill $pid"
wait $pid
python3 - "$OUT" <<'PY'
import json, sys
try:
    d = json.load(open(sys.argv[1]))
    s = d.get("end", {}).get("sum", {})
    print(f"  achieved {s.get('bits_per_second',0)/1e6:.2f} Mbit/s, "
          f"{s.get('lost_percent', 0):.2f}% lost, {s.get('seconds',0):.0f}s")
    print(f"  RECORD THIS with the run: offered {sys.argv[1]}")
except Exception as exc:
    print(f"  could not parse {sys.argv[1]}: {exc}")
PY
