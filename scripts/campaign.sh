#!/usr/bin/env bash
# Run the three 30-agent topologies N times each, one of each per cycle.
#
#   bash scripts/campaign.sh                 # 50 cycles, 240 s runs
#   CYCLES=20 bash scripts/campaign.sh       # shorter campaign
#   bash scripts/campaign.sh --dry-run       # print the plan, run nothing
#
# INTERLEAVED, not batched, and that is the point. Fifty consecutive runs of one
# topology occupy one four-hour window of office 2.4 GHz activity while another
# topology occupies a different one, which makes time of day a between-topology
# confound. Ambient traffic dominates these receivers -- a bridge decodes ~13500
# foreign advertisements against ~2100 of ours in a 120 s run -- so that confound
# is not small. One of each per cycle makes every topology sample the same
# conditions, and time becomes a covariate rather than a bias.
#
# The order WITHIN a cycle rotates, so no topology is permanently first. The
# first run after an idle gap is not like the third.
#
# Resumable: a cycle whose run directory already exists is skipped, so a campaign
# interrupted at cycle 31 continues from 31 rather than starting over.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

CYCLES="${CYCLES:-50}"
DURATION="${DURATION:-240}"
RUN_INDEX="${RUN_INDEX:-0}"          # held fixed: same initial conditions every cycle
OUT="${OUT:-runs}"
SETTLE="${SETTLE:-10}"               # between runs, so radios and tables quiesce
RETRIES="${RETRIES:-1}"              # extra attempts per failed run
TIMEOUT="${TIMEOUT:-15}"             # per control-plane command
PIS="${PIS:-pi1 pi2 pi4 pi5 pi7 pi8 pi9 pi10 pi12 pi13}"
RF="${RF:-1}"                        # capture rf_survey each cycle
CAMPAIGN="${CAMPAIGN:-c$(date +%Y%m%d-%H%M)}"

# manifest:duration. Six arms: two rates x three topologies. dring gets longer
# because lambda_2 = 0.022 makes it the slow one -- it was still descending at
# 300 s in simulation while ring4 settled by 240.
ARMS=(
  "n30-dring-40hz:360"    "n30-ring4-40hz:240"    "n30-clusters-40hz:240"
  "n30-dring-25hz:360"    "n30-ring4-25hz:240"    "n30-clusters-25hz:240"
)
LOG="$OUT/$CAMPAIGN/campaign.log"
DRY=0; [ "${1:-}" = "--dry-run" ] && DRY=1

mkdir -p "$OUT/$CAMPAIGN/rf"
say() { printf '%s  %s\n' "$(date +%H:%M:%S)" "$*" | tee -a "$LOG"; }

# ── the plan, before committing hours to it ─────────────────────────────────
total=$(( CYCLES * ${#ARMS[@]} ))
secs=0
for arm in "${ARMS[@]}"; do secs=$(( secs + CYCLES * (${arm##*:} + SETTLE + 25) )); done
eta=$(printf '%dh%02dm' $((secs/3600)) $(((secs%3600)/60)))
say "campaign $CAMPAIGN: $CYCLES cycles x ${#ARMS[@]} arms = $total runs"
say "  run-index $RUN_INDEX held fixed, ~$eta estimated"
for arm in "${ARMS[@]}"; do say "    ${arm%%:*}  ${arm##*:}s"; done
say "  output $OUT/$CAMPAIGN, rf survey: $([ "$RF" = 1 ] && echo on || echo off)"

# ── preflight: every node answers before anything long starts ───────────────
preflight() {
    local bad=0
    for arm in "${ARMS[@]}"; do
        local m="experiments/${arm%%:*}.yaml"
        [ -f "$m" ] || { say "MISSING manifest $m"; bad=1; }
    done
    say "checking all 30 agents respond"
    if ! python3 -m vertex.hub status "experiments/${ARMS[0]%%:*}.yaml" \
            --timeout "$TIMEOUT" 2>&1 | tee -a "$LOG" | grep -q "^  ok"; then
        say "FAIL: no agent answered; start them with scripts/agents.sh start"
        bad=1
    fi
    local down
    down=$(python3 -m vertex.hub status "experiments/${ARMS[0]%%:*}.yaml" \
           --timeout "$TIMEOUT" 2>/dev/null | grep -c "^  FAIL" || true)
    [ "$down" -gt 0 ] && { say "WARNING: $down agent(s) not answering"; }
    return $bad
}

# ── ambient conditions, once per cycle ──────────────────────────────────────
# Read-only and needs no sudo. The BLE transmit-power line stays blank while the
# agents hold the HCI user channel; everything else is filled in either way.
rf_survey() {
    local cyc=$1 dir="$OUT/$CAMPAIGN/rf/cycle-$(printf %02d "$cyc")"
    mkdir -p "$dir"
    for pi in $PIS; do
        timeout 30 ssh -o BatchMode=yes -o ConnectTimeout=5 "$pi" \
            'bash -s' < scripts/rf_survey.sh > "$dir/$pi.txt" 2>&1 \
            || echo "ssh to $pi failed (key auth not set up?)" > "$dir/$pi.txt"
    done
    local ok
    ok=$(grep -Lc "ssh to .* failed" "$dir"/*.txt 2>/dev/null | wc -l)
    say "  rf survey: $ok/$(echo $PIS | wc -w) hosts -> $dir"
}

# ── one run, with retries ───────────────────────────────────────────────────
one_run() {
    local base=$1 dur=$2 cyc=$3
    local name; name=$(printf '%s-c%02d' "$base" "$cyc")
    local manifest="experiments/${base}.yaml"

    if [ -d "$OUT/$CAMPAIGN/$name" ]; then
        say "  skip $name (already collected)"; return 0
    fi
    for attempt in $(seq 0 "$RETRIES"); do
        [ "$attempt" -gt 0 ] && say "  retry $attempt for $name"
        if [ "$DRY" = 1 ]; then
            say "  DRY  $name  ($manifest, ${dur}s)"; return 0
        fi
        if python3 -m vertex.hub run "$manifest" \
                --duration "$dur" --run-index "$RUN_INDEX" \
                --run-name "$name" --out-dir "$OUT/$CAMPAIGN" \
                --timeout "$TIMEOUT" >>"$LOG" 2>&1; then
            say "  ok   $name"
            return 0
        fi
        say "  FAIL $name (attempt $((attempt+1)))"
        sleep "$SETTLE"
    done
    return 1
}

# ── the campaign ────────────────────────────────────────────────────────────
[ "$DRY" = 1 ] || preflight || { say "preflight failed; nothing run"; exit 1; }

failed=0; done_runs=0
for ((c=0; c<CYCLES; c++)); do
    say "cycle $c/$((CYCLES-1))"
    [ "$RF" = 1 ] && [ "$DRY" = 0 ] && rf_survey "$c"

    # rotate the order so no arm is permanently first
    n=${#ARMS[@]}
    for k in $(seq 0 $((n - 1))); do
        arm=${ARMS[$(( (k + c) % n ))]}
        one_run "${arm%%:*}" "${arm##*:}" "$c" \
            && done_runs=$((done_runs+1)) || failed=$((failed+1))
        [ "$DRY" = 1 ] || sleep "$SETTLE"
    done
done

say "campaign $CAMPAIGN finished: $done_runs ok, $failed failed"
say "aggregate with:"
for arm in "${ARMS[@]}"; do
    say "  python3 tools/compare_runs.py '$OUT/$CAMPAIGN/${arm%%:*}-c*'"
done
[ "$failed" -eq 0 ]
