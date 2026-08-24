#!/usr/bin/env bash
# Corrected publish-rate sweep: four points, ceiling pinned at 1.0.
#
# NOT `--publish-period`: that overrides the publish rate without touching
# radio.adv_interval_ms, which caps BLE delivery at min(1, T_pub/T_adv) and makes
# the sweep measure the transmitter's sampling rate instead of the medium. That is
# how the first sweep was confounded -- docs/PLATFORM.md A3.4. One manifest per
# point, each carrying a radio block that matches its rate.
set -euo pipefail

BASE="${BASE:-sweep2}"          # run-name prefix; existing sweep-* stays intact
DURATION="${DURATION:-120}"
REPEATS="${REPEATS:-10}"
POINTS="${POINTS:-p400 p200 p080 p040}"
LOG="${LOG:-sweep-$(date +%Y%m%d-%H%M%S).log}"

echo "sweep: $POINTS  x${REPEATS} @ ${DURATION}s  -> runs/${BASE}-*  log=$LOG"
est=0; for _ in $POINTS; do est=$((est + REPEATS * (DURATION + 5))); done
echo "estimated $((est / 60)) min"

for pt in $POINTS; do
  m="experiments/sweep-${pt}.yaml"
  [ -f "$m" ] || { echo "missing $m -- run tools/make_manifests.py --hosts ..." >&2; exit 1; }
  echo "=== $pt ==="
  # `|| true`: one failing point must not abort the remaining points. The first
  # sweep lost p040 entirely because pipefail propagated p080's failure and set -e
  # killed the loop -- 20 wasted minutes and a gap in the middle of the sweep.
  if ! python3 -m vertex.hub run "$m" \
      --duration "$DURATION" --repeat "$REPEATS" \
      --run-name "${BASE}-${pt}" --settle-between 5 2>&1 | tee -a "$LOG"; then
    echo "!! $pt FAILED -- continuing to the remaining points" | tee -a "$LOG"
    failed="${failed:-} $pt"
  fi
done
[ -n "${failed:-}" ] && echo "points that failed:${failed}"
echo "done. compare with: python3 tools/compare_runs.py runs/${BASE}-p040-r* --out results/${BASE}-p040"
