# Running the 30-agent campaign

Fifty replicates of **six arms** — G₁, G₂ and G₃(t) at 30 agents, at **two
rates** — driven by `scripts/campaign.sh`. About **30 hours** and **12 GB**.

---

## What is being run

| topology | degree | λ₂ | note |
|---|---|---|---|
| G₁ directed ring | in-degree 1 | 0.0219 | sparsest strongly connected, slowest |
| G₂ ring, degree 4 | 4 | 0.2166 | at the firmware's neighbour limit |
| G₃(t) clustered | 2–4 | 0.1392 | cut-points 21, 30 enabled at **t = 60 s** |

Each at both rates, so six manifests:

| rate | manifests | dt | publish | k | keeps |
|---|---|---|---|---|---|
| 40 Hz / 8 Hz | `n30-{dring,ring4,clusters}-40hz` | 0.025 | 125 ms | 1.00 | the faster control update |
| 25 Hz / 5 Hz | `n30-{dring,ring4,clusters}-25hz` | 0.04 | 200 ms | 1.60 | the advertising redundancy |

**Both rates are collected because they buy different things and the difference
is the finding.** `k = T_pub/T_adv` is the number of advertising events carrying
one published value, and measured delivery tracks it monotonically — ble→ble
0.639 at k = 0.91, 0.694 at k = 1.00, 0.947 at k = 1.60 — while the steady-state
error moves the other way, 0.0018 at 125 ms against 0.0051–0.0067 at 200 ms. See
PLATFORM.md §5.5a.

All six share scanning **20/19 ms (95% duty)**, Wi-Fi transmit power
**12 dBm** (`tx_power_dbm`, new 2026-09-11: previously unset, reading back
as the untrusted `brcmfmac` placeholder, which left the medium comparison
with an uncontrolled term), advertising **100–113.6 ms** at 40 Hz and
**100–150 ms** at 25 Hz (the max is the interval the Pi's radio actually
uses, so it is sized to keep k > 1 on both radios; see PLATFORM 5.5b),
ν₀ = **0.05** with the sinusoid at 70% and 2 Hz, seed 20260818, and gains
`gain_ij = 0.5`, `eta = 2.5e-3` — identical across the rates, because the
recursion is `x += dt*(u+nu)` and the gains are rates.

**Duration is per arm, not uniform within a rate** (changed 2026-09-11):

| arm | 40 Hz | 25 Hz | converges at | margin |
|---|---|---|---|---|
| G1 dring | **420 s** | **480 s** | 230 s / 259 s (sd 26 / 33) | 7.2 / 6.7 sd |
| G2 ring4 | 300 s | 360 s | 59 s / 66 s | 241 / 24 sd |
| G3 clusters | 300 s | 360 s | 190 s / 199 s | 85 / 16 sd |

G1 was previously uniform with the others at 300/360 s, which left it only 2.7
and 3.1 standard deviations of margin: a slow realisation nearly ran out of run
before converging. The other two arms converge four times faster and already had
far more margin than they needed, so lengthening them would have added 8 hours
to a 60-cycle campaign for nothing. Convergence time is measured from t = 0 and
does not depend on run length, so the arms remain directly comparable; what
differs is the length of the steady-state window, and G1's is now the longest
rather than the shortest. The rates differ from
each other because 25 Hz publishes at 5 Hz against 40 Hz's 8 Hz, and fewer
updates per second means a longer wall clock to the same place.

Note that ϑ takes roughly 130 s to latch in simulation, so a steady-state window
should start no earlier than ~150 s. For G₃ the merge at t = 60 s leaves 240 s
(40 Hz) or 300 s (25 Hz) of merged evolution.

**Run index is held fixed at 0.** Initial conditions and every node's disturbance
stream are then identical across all 50 replicates, so the network realisation is
the only thing that varies. That is what makes them replicates.

---

## Why cycles rather than batches

The campaign runs **one of each of the six arms per cycle**, fifty cycles, and
rotates the order within the cycle.

Batching would put all 50 G₂ runs in one four-hour window and all 50 G₁ runs in a
different one, making time of day a between-topology confound. That is not a
theoretical worry here: a bridge decodes roughly 13,500 foreign advertisements
against 2,100 of ours in a two-minute run, so ambient 2.4 GHz activity dominates
the receivers and it varies with building occupancy across a 30-hour campaign.

Interleaving makes every topology sample the same conditions. Rotating the order
within each cycle additionally prevents any topology from always being the first
run after an idle gap.

---

## Before you start

**1. Set up key-based ssh from the hub to all ten Pis.** The RF survey runs over
ssh and cannot prompt for a password. Your `~/.ssh/config` already defines the
aliases and per-host users; what is missing is the key:

```bash
for pi in pi1 pi2 pi4 pi5 pi7 pi8 pi9 pi10 pi12 pi13; do ssh-copy-id "$pi"; done
for pi in pi1 pi2 pi4 pi5 pi7 pi8 pi9 pi10 pi12 pi13; do
  ssh -o BatchMode=yes "$pi" hostname || echo "$pi STILL NEEDS A KEY"
done
```

Without this the campaign still runs, but every RF survey file will read
`ssh to <pi> failed`.

**2. Fix the two suspect nodes.** Node 9's nRF on `10.6.5.12` refused frame
`0x4E` during a previous configure, and bridge 21's links to nodes 9 and 10 were
the two worst in the fleet at 0.10 and 0.12 delivery. Reflash those boards and
confirm before committing 30 hours:

```bash
ssh pi12 'bash scripts/agents.sh stop'
ssh pi12 'python3 test/nrf/check_board.py'      # must PING, configure and report
ssh pi12 'bash scripts/agents.sh start'
```

**3. Start the agents on every Pi and confirm all 30 answer.**

```bash
for pi in pi1 pi2 pi4 pi5 pi7 pi8 pi9 pi10 pi12 pi13; do
  ssh "$pi" 'cd vertex-testbed && bash scripts/agents.sh preflight && bash scripts/agents.sh start'
done
python3 -m vertex.hub status experiments/n30-ring4-40hz.yaml
```

Every line must read `ok`. Configure is an all-or-nothing barrier, so one
unreachable node aborts a run.

**4. Regenerate the manifests if the host list changed.**

```bash
python3 tools/make_manifests.py --campaign     # --campaign, or it writes only the test set
git diff experiments/                          # nothing unexpected should move
```

**5. Dry run.** Prints the plan and the rotation without touching the fleet.

```bash
bash scripts/campaign.sh --dry-run
```

---

## Running it

```bash
bash scripts/campaign.sh                    # 50 cycles, the default
CYCLES=20 bash scripts/campaign.sh          # shorter
tail -f runs/c<date>/campaign.log           # from another terminal
```

Run it under `tmux` or `nohup` — 30 hours outlives an ssh session.

```bash
tmux new -s campaign 'bash scripts/campaign.sh'
```

### Knobs

| variable | default | what it does |
|---|---|---|
| `CYCLES` | 50 | replicates per arm |
| `RUN_INDEX` | 0 | initial-condition substream; keep fixed for replicates |
| `SETTLE` | 10 | seconds between runs |
| `RETRIES` | 1 | extra attempts per failed run |
| `TIMEOUT` | 15 | per control-plane command |
| `RF` | 1 | capture the RF survey each cycle |
| `OUT` | `runs` | output root |
| `CAMPAIGN` | `c<date>-<time>` | campaign directory name |

### If it stops

It is **resumable**. A run whose directory already exists is skipped, so
relaunching with the same `CAMPAIGN` continues where it left off:

```bash
CAMPAIGN=c20260904-1730 bash scripts/campaign.sh
```

A failed run is retried once, then recorded as failed and the campaign
continues. It does not abort on one bad run.

---

## What lands on disk

```
runs/<campaign>/
  campaign.log                        every action, timestamped
  rf/cycle-00/pi1.txt ...             ambient conditions per cycle, per host
  n30-dring-40hz-c00/                 30 x .bin + 30 x .meta.json + run.json
  n30-ring4-40hz-c00/
  n30-clusters-40hz-c00/
  n30-dring-25hz-c00/
  n30-ring4-25hz-c00/
  n30-clusters-25hz-c00/
  ...
```

| arm | rate | run | per node | per run | × 50 |
|---|---|---|---|---|---|
| dring-40hz (deg 1) | 40 Hz | 300 s | 0.86 MB | 26 MB | 1.3 GB |
| ring4-40hz (deg 4) | 40 Hz | 300 s | 2.02 MB | 60 MB | 3.0 GB |
| clusters-40hz (deg ~3) | 40 Hz | 300 s | 1.63 MB | 49 MB | 2.4 GB |
| dring-25hz (deg 1) | 25 Hz | 360 s | 0.65 MB | 19 MB | 1.0 GB |
| ring4-25hz (deg 4) | 25 Hz | 360 s | 1.51 MB | 45 MB | 2.3 GB |
| clusters-25hz (deg ~3) | 25 Hz | 360 s | 1.22 MB | 37 MB | 1.8 GB |

Row width is `5 + 4·degree` columns of 8 bytes, so degree and sample rate both
drive the size. **~11.8 GB total.**

---

## Checks during and after

**Per cycle**, the log line for each run is `ok` or `FAIL`. A `FAIL` that repeats
on the same node across cycles is a node problem, not bad luck — stop and fix it
rather than collecting 50 degraded replicates.

**For G₃ specifically**, confirm the merge actually happened. Scheduled events
were silently failing until recently, and the symptom was three clusters that
each converged separately and never merged:

```bash
python3 -c "
import json,glob
for f in sorted(glob.glob('runs/<campaign>/n30-clusters-*hz-c*/run.json')):
    print(f.split('/')[-2], json.load(open(f)).get('notes'))"
```

Every line must read `applied {'enabled': True} to [21, 30]`. Anything with
`MISSED` or `FAILED` is a run where the graph never merged.

**After the campaign**, aggregate each topology:

```bash
for a in dring ring4 clusters; do
  for r in 40hz 25hz; do
    python3 tools/compare_runs.py "runs/<campaign>/n30-$a-$r-c*"
  done
done
```

---

## Known limitations of this design

**Eight of G₃'s edges are intra-host**, including all four links that join the
cut-points: `(1,21)` and `(11,21)` on `10.6.5.1`, `(10,30)` and `(20,30)` on
`10.6.5.13`. Those barely use the radio, so the merge transient G₃ exists to
measure happens over links that are almost loopback. Changing `CLUSTER_EDGES` so
21 attaches to nodes 2 and 12 and 30 to nodes 9 and 19 would fix it, at the cost
of altering the published G₃ edge set. Decide before the campaign, not after.

**The bridge → nRF blackouts are a lottery, and the campaign is what will
measure them.** Across single runs of identical configurations the fraction of
link-time dark has ranged 7.2% to 39.6%, and which pairs go dark changes between
runs. So no single-run comparison of dither settings means anything, and the
50 replicates are the point: they turn a 5x spread into a distribution. The
100–150 ms range is in place in all six arms; whether it helps is a campaign
result, not an assumption.

**The disturbance is five times the dead-band, and that is fine.** ν₀ = 0.05
against δ = 0.01. Theorem 2's condition holds with room to spare, h < ε/ν₀ = 0.2 s
against dt of 0.025 and 0.04, and simulation shows the coordination error is
insensitive to ν₀: the disturbance enters the physical state x and never the
virtual state z, so the z-spread the campaign compares is identical at ν₀ = 0.015
and 0.05 (0.001526 in both). What does respond is the adaptive gain ϑ, which
climbs proportionally, and the paper's MSE(xᵢ), which is an x-quantity. Compare
z-spread across the ν₀ boundary freely; compare MSE only within it.

**Do not raise eta to compensate.** It is an integrator on ϑ that stops only when
|σ| ≤ δ, so a larger value overshoots: at ν₀ = 0.05, eta of 1e-2, 5e-2 and 2e-1
drove ϑ to 2.6, 14.4 and 59.7 against a disturbance of 0.05 and never entered the
band at all, where the configured 2.5e-3 entered it at 129 s with ϑ = 0.47. The
current value is the best of those tried.

**Fifty per arm is 300 runs, 30 hours and 12 GB.** Measured run-to-run spread is
a few points on delivery, so 20 replicates already resolve differences of a point
or two; `CYCLES=20` finishes in about 12 hours. The script resumes, so running 20
first and extending is strictly better than committing to 50 up front.
