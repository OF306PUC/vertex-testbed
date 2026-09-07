# Running the 30-agent campaign

Fifty replicates each of G₁, G₂ and G₃(t) at 30 agents, 50 Hz dynamics, 10 Hz
publish. About **11 hours** and **5 GB**. Driven by `scripts/campaign.sh`.

---

## What is being run

| topology | manifest | degree | λ₂ | note |
|---|---|---|---|---|
| G₁ directed ring | `n30-dring-50hz` | in-degree 1 | 0.0219 | sparsest strongly connected |
| G₂ ring, degree 4 | `n30-ring4-50hz` | 4 | 0.2166 | at the firmware's neighbour limit |
| G₃(t) clustered | `n30-clusters-50hz` | 2–4 | 0.1392 | cut-points 21, 30 enabled at **t = 60 s** |

All three share `dt_s = 0.02`, `publish_period_s = 0.1`, `CONTROLLER_50HZ`,
seed 20260818, advertising 100–120 ms, scanning 20/18 ms (90% duty). Topology is
the only variable.

**240 s per run.** From the pilot runs, G₂ reaches its noise floor by ~150 s and
G₁ by ~225 s; G₃ needs 60 s of separation plus the merge transient. 240 s covers
all three with margin and keeps them a uniform length.

**Run index is held fixed at 0.** Initial conditions and every node's disturbance
stream are then identical across all 50 replicates, so the network realisation is
the only thing that varies. That is what makes them replicates.

---

## Why cycles rather than batches

The campaign runs **one of each topology per cycle**, fifty cycles, and rotates
the order within the cycle.

Batching would put all 50 G₂ runs in one four-hour window and all 50 G₁ runs in a
different one, making time of day a between-topology confound. That is not a
theoretical worry here: a bridge decodes roughly 13,500 foreign advertisements
against 2,100 of ours in a two-minute run, so ambient 2.4 GHz activity dominates
the receivers and it varies with building occupancy across an 11-hour campaign.

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
confirm before committing 11 hours:

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
python3 -m vertex.hub status experiments/n30-ring4-50hz.yaml
```

Every line must read `ok`. Configure is an all-or-nothing barrier, so one
unreachable node aborts a run.

**4. Regenerate the manifests if the host list changed.**

```bash
python3 tools/make_manifests.py
git diff experiments/          # nothing unexpected should move
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

Run it under `tmux` or `nohup` — 11 hours outlives an ssh session.

```bash
tmux new -s campaign 'bash scripts/campaign.sh'
```

### Knobs

| variable | default | what it does |
|---|---|---|
| `CYCLES` | 50 | replicates per topology |
| `DURATION` | 240 | seconds per run |
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
  n30-dring-50hz-c00/                 30 x .bin + 30 x .meta.json + run.json
  n30-ring4-50hz-c00/
  n30-clusters-50hz-c00/
  ...
```

| topology | per node | per run | × 50 |
|---|---|---|---|
| dring (degree 1) | 0.86 MB | 26 MB | 1.3 GB |
| ring4 (degree 4) | 2.02 MB | 61 MB | 3.0 GB |
| clusters (degree ~3) | 1.63 MB | 49 MB | 2.4 GB |

Row width is `5 + 4·degree` columns of 8 bytes, so degree drives the size.

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
for f in sorted(glob.glob('runs/<campaign>/n30-clusters-50hz-c*/run.json')):
    print(f.split('/')[-2], json.load(open(f)).get('notes'))"
```

Every line must read `applied {'enabled': True} to [21, 30]`. Anything with
`MISSED` or `FAILED` is a run where the graph never merged.

**After the campaign**, aggregate each topology:

```bash
python3 tools/compare_runs.py 'runs/<campaign>/n30-dring-50hz-c*'
python3 tools/compare_runs.py 'runs/<campaign>/n30-ring4-50hz-c*'
python3 tools/compare_runs.py 'runs/<campaign>/n30-clusters-50hz-c*'
```

---

## Known limitations of this design

**Eight of G₃'s edges are intra-host**, including all four links that join the
cut-points: `(1,21)` and `(11,21)` on `10.6.5.1`, `(10,30)` and `(20,30)` on
`10.6.5.13`. Those barely use the radio, so the merge transient G₃ exists to
measure happens over links that are almost loopback. Changing `CLUSTER_EDGES` so
21 attaches to nodes 2 and 12 and 30 to nodes 9 and 19 would fix it, at the cost
of altering the published G₃ edge set. Decide before the campaign, not after.

**The bridge → nRF class still shows blackouts** at 30 agents despite the
100–120 ms advertising dither: in the pilot, `21→9` was dark for 82% of the run
and `21→10` for 62%, while every nRF→nRF link had zero gaps over one second. The
campaign will measure this rather than avoid it, but it means the G₂ delivery
figures carry a known degraded class. A wider dither (`adv_interval_max_ms: 150`)
is the untested candidate fix.

**Fifty is generous.** Measured run-to-run spread is a few points on delivery, so
20 replicates already resolve differences of a point or two. `CYCLES=20` finishes
in about 4.5 hours and exposes the campaign to far less drift. Consider running
20 first, looking at the spread, and extending only if the effect you care about
is inside the noise.
