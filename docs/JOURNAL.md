# VERTEX lab journal

The dated record of hardware bring-up and every experiment run on the new platform,
in the order it happened. Kept verbatim: the reasoning that led to a conclusion is
often worth more than the conclusion, and a result later overturned still needs to
be findable.

**This is the raw notebook.** For current-state design decisions and the
consolidated findings read `PLATFORM.md`; for how to run the thing, `RUNBOOK.md`.
Where they disagree, `PLATFORM.md` is current and this file is history.

---

## 8a. Loopback test results (2026-08-20, hardware)

**Direction A — Pi transmit.** 493/500 delivered, **0 byte mismatches**. The Python
encoder, AD element structure, company id and little-endian field order agree with
the C capture byte for byte. Measurement voided by UART saturation, not radio loss:
the peer relayed ~217 reports/s over 115200 (92% of line rate), 96% of them other
devices' beacons, and dropped 269 of its own. Fixable with a peer-side company-id
filter; not on the critical path.

**Direction B — Pi receive, scan-window sweep.**

| duty | delivered | ratio | reports | foreign | ours |
|---|---|---|---|---|---|
| 100% | 452/500 | 90.4% | 17903 | 17203 | 700 |
| 50%  | 328/500 | 65.6% | 13175 | 12692 | 483 |
| 25%  | 182/500 | 36.4% |  6787 |  6521 | 266 |
| 10%  |  73/500 | 14.6% |  2635 |  2537 |  98 |

**The scan window is under our control** -- 75.8 points of movement, the parameter
BlueZ's D-Bus API never exposed (§3 A1). Ratio tracks duty between the one- and
two-chance models, as expected at 100 ms advertising against a 200 ms payload
period. Foreign reports scale with duty independently, confirming the window gates
reception rather than something else in the chain. 0 byte mismatches over 1035
deliveries; 1528 across both directions.

### The consequence for the platform

Delivery varies **6x** with the scan window, and the production firmware hardcodes
it (`BT_GAP_SCAN_FAST_INTERVAL`/`WINDOW` in `observer.c`, and the `BT_LE_ADV_NCONN`
defaults in `broadcaster.c`). So every BLE agent's neighbour visibility -- which
determines whether consensus converges at all -- is set by a Zephyr default nobody
chose and recorded in no run's metadata.

**Radio parameters are therefore a first-class experimental variable, not a
deployment detail.** They belong in the manifest and in `RunMeta.environment`.

---

## 8a-bis. First hardware bring-up (2026-08-20)

One board flashed and run with `test/nrf/check_board.py`. Every stage passed,
including the air stage.

| | |
|---|---|
| PING RTT | 10.3 ms -- so UARTE hardware byte counting is active, and this bounds the CONTROL epoch bias |
| Rejection | short ALGORITHM -> `ERR` / `AGENT_ERR_LEN` |
| Report cadence | **500.006 ms** per interval against `clock = 500` |
| Steps per report | 2.55 against `dt = 200`, `clock = 500` (2.50 expected) |
| Air | 30 advertisements in 4 s, decoded as **v1** by the host codec |
| `vstate` air vs serial | 22.300000 vs 22.300000, delta 0 |
| Epoch transfer | `tx_time_us - epoch_us` = 9.956 s, matching the trigger-to-scan interval |

Two results that read like faults and are not:

* **`vstate` and `vartheta` never moved.** Correct with one board. `v_i()` sums
  only over `neighbors_enabled[j]`, and the report shows `enabled=[False]` because
  no neighbour was ever heard -- so the coupling term is exactly zero and `vstate`
  cannot change. And `|sigma| = 0.002066 < delta = 0.01`, so `dvtheta = 0` and the
  adaptive gain stays at zero. `state` did move, by 0.002066, which is the
  disturbance integrating on its own.
* **`seq = 48` after ~11 reports.** The scan runs *after* the collection window,
  so the board had been stepping for ~10 s at `dt = 200 ms`. 48 is right.

**One real defect, now fixed: the run began 442 ms and 555 ms after the trigger**,
on two consecutive runs of the same command. The run loops are semaphore-driven and
the network thread only noticed `running` when its 1000 ms idle poll next fired, so
the first control step landed anywhere in that window.

This does not cancel out. `time_us` and `epoch_us` are latched when CONTROL
*arrives*, so `t_us` is on a shared origin -- but the first step is not. Two nodes
reporting the same `t_us` would be at different step counts, which misaligns
precisely the trajectories a convergence measurement compares. Across nine nodes
that is up to a second of unmeasured skew sitting underneath the hub's own
trigger spread, which the hub does report.

Fixed with `control_set_trigger_hook()`: `control.c` calls it on every accepted
CONTROL frame and `main.c` registers a hook that gives the network semaphore, so
the loops react immediately and the idle poll is a safety net rather than the
mechanism. Verify on the next run: `t_us` of the first report should be a few ms,
not a few hundred.

---

## 8a-ter. Two timelines in the log (2026-08-21)

Prompted by asking whether the nRF's reports were timestamped. They were -- with
`t_us`, run-relative to that board's CONTROL arrival -- but three things were wrong
with relying on it, and the fix was chosen to be the smallest of the three.

The row schema is now **v4**: `timestamp`, `device_timestamp`, `state`, `vstate`,
`vartheta`, then the neighbour pairs. `timestamp` is this host's clock, and it is
the axis to plot against; `device_timestamp` is whatever computed the sample.
Identical for `wifi` and `bridge`, whose controllers run in the agent process; for
`ble` the difference is the serial transit plus scheduling, measured at ~120 ms in
the loopback harness and now visible on hardware.

This closes the comparability problem **without touching firmware**: the host clock
is chrony-synchronised and epoch-shared, so a `ble` agent's samples land on the same
axis as a `wifi` agent's. The firmware's own two bases -- run-relative `t_us` in
STATE, epoch-relative `tx_time_us` on the air -- are left as they are, and remain
worth reconciling later.

The arrival stamp is taken in `SerialLink`'s reader thread rather than on the event
loop, so `call_soon_threadsafe` delay is not charged to the board.

Two bugs fell out of doing it:

* **The collector renamed rows and made them unreadable.** It wrote every rows
  artefact as `<node>.rows`, but `recover_rows` dispatches on the suffix -- `.bin`
  vs `.csv` vs `.jsonl` -- so a collected binary run decoded as text and raised
  `UnicodeDecodeError`. The agent had been sending its real filename all along and
  `ControlClient.fetch` was dropping it; there is now `fetch_named`, and the hub
  keeps the agent's name.
* **`normalize_run` passed the new column through by luck**, via a fall-through
  branch rather than by name. Named explicitly now: seconds are not a scaled state,
  and a column that survives conversion by accident survives it only until someone
  adds a branch above it.

`test/hub/check_fleet.py` asserts both directions -- that a `ble` node's columns
differ, and that a local agent's are bit-identical -- so a regression that quietly
reuses the board's clock for both fails rather than looking plausible.

---

## 8a-quater. Trigger hook confirmed, and the stamp sharpened (2026-08-21)

The hook works. The evidence is `counter`, not the wall clock:

| | first report `t_us` | `counter` there | first step at |
|---|---|---|---|
| before the hook | 441 986 µs | 0 | ~+442 ms |
| after | 502 319 µs | 3 | **~0 ms** |

`counter` is the number of steps actually taken, so it cannot be confused by when
the *reporting* timer happened to fire. Three steps already done by the first
report means the dynamics timer started at the trigger; zero steps by 442 ms means
it had not started at all. Report cadence measured 500.000 ms exactly over 11
intervals.

Two of my own bugs surfaced doing this, both in the measuring apparatus rather than
the thing measured -- worth recording because both made a working system look
broken:

* **`check_board.py` cleared its report buffer *after* the trigger**, discarding
  the very sample that shows the hook working. It reported "first report 516 ms"
  while the board had in fact already reported within milliseconds. The script now
  keeps that sample and derives trigger-to-first-*step* from `counter`, which is
  the honest reading, and fails if it exceeds 100 ms.
* **`on_state` began delivering a `TimedFrame` and `check_board.py` still expected
  a bare frame**, so every STATE decode raised `'TimedFrame' object has no
  attribute 'payload'` and the run reported "nothing received" -- while the link
  counters said `states=21`. The counters were right. `BleRelay` had been updated;
  the script had not.

**The arrival stamp was being quantised by pyserial.** `device_timestamp -
timestamp` measured -14.9 .. -6.6 ms, and that decomposes cleanly:

    STATE frame 36 B at 115200 8N1  = 3.1 ms transit
    read timeout 10 ms              = 0..10 ms of rounding
                                      -------------------
    predicted 3.1 .. 13.1 ms; observed 6.6 .. 14.9 ms, spread 8.3 ms

The spread *is* the poll granularity, and none of it is real. `SerialLink`'s reader
now blocks in `select` on the port's descriptor with `timeout=0` on the Serial
itself, so it wakes when bytes arrive rather than when a timer expires. What should
remain is the transit plus the board's assembly delay, and a spread far below one
control period. Anything left after that is the board's crystal against the Pi's
clock, which is the quantity `CLOCK_MODEL.md` predicts will dominate a long run.

Raising the UART baud would cut the transit proportionally (3.1 ms at 115200,
0.4 ms at 921600). Not done: it needs a devicetree change on the board, and the
transit is a near-constant offset rather than jitter.

---

## 8a-quinquies. First two-host run: the law works, UDP did not (2026-08-21)

`n6-ring`, 30 s, 6/6 nodes, 662 samples, trigger spread 32 ms. Two results and two
defects.

**The coordination law runs on hardware.** Every BLE link delivered and `vstate`
converged:

```
t= 0s  1:  6.896  2:  5.213  21: 21.807  22: 29.495   spread 24.28
t=29s  1: 14.339  2: 13.923  21: 14.495  22: 19.269   spread  4.93
```

`fresh` 0.97-1.00 on nRF-to-nRF (1-2) and on both nRF-advertises/Pi-scans hops
(1-22, 2-21). The C law, the Python law, the epoch transfer, the STATE path and the
v1 air format all work together.

**Every UDP link delivered nothing.** 3/3 BLE, 0/3 UDP, both directions, so it was
the medium and not a node. Nodes 11 and 12 never moved because they heard nothing.

Cause: **`broadcast_address()` assumed a /24 and the lab network is a /22.**

```
kernel:  10.6.5.2/22 brd 10.6.7.255
we sent:                 10.6.5.255
```

On a /22, `10.6.5.255` is an ordinary host address that nobody holds. Every
`sendto` succeeded, every socket stayed healthy, and delivery was zero -- the
failure mode is completely silent. `scripts/udp_check.py` isolated it in one run on
each host; its "not even our own datagrams came back" line is what ruled out AP
client isolation and pointed at the address.

Fixed by asking the kernel instead of deriving: `interface_broadcast()` uses
`SIOCGIFBRDADDR` via ioctl (Linux-specific, stdlib-only, no new dependency on ten
Pis). `AgentService` now takes the interface name for exactly this, and records
`udp_broadcast`, `udp_broadcast_source` and `prefixlen` in the run's environment --
so a future wrong address is visible in the data rather than only in a delivery
ratio. `broadcast_address()` survives as a named fallback with its assumption
stated. Preflight prints the kernel's value and flags when a /24 guess would have
differed.

Note the diagnostic gap this exposed: the no-intra-host-edge rule means **every**
UDP link in `n6-ring` is cross-host, so there was no local UDP link to compare
against and nothing to separate "the transport is broken" from "the network does
not carry broadcast". `udp_check.py` exists to fill that gap from outside.

**A `ble` agent's host timestamps were on the wrong origin.** `timestamp` ran
85.285 -> 115.285 while `device_timestamp` ran 0.002 -> 30.003 and the `wifi` nodes
ran 0.202 -> 30.002. `_start` reset `self.clock` and `self.relay.clock` but not
`self.link.clock`, and the link is what stamps `rx_time_us` -- so the relay's host
timeline was anchored at process launch, offset by that agent's uptime.
`BleRelay.start()` now pushes the run's clock to the link before any report can
arrive.

The lesson is about the check, not the code. `check_fleet.py` asserted that a `ble`
node's two time columns **differ**. They did; one was simply wrong. It now asserts
the origin too -- the first row's `timestamp` must be near zero for every agent type
-- and the harness idles 1.5 s before triggering so that a launch-anchored clock is
distinguishable from a run-anchored one. Verified to catch the regression.

For the smoke run's data, `device_timestamp` is the usable column on the `ble`
nodes: run-relative and correct, on the same origin as the others' `timestamp` to
within the 32 ms trigger spread.

**One asymmetry to decide before collecting comparable data.** `ble` logged 31 rows,
`wifi` and `bridge` logged 150: the relay reports at `clock` (1 Hz here) while local
agents log every control step (5 Hz at `dt = 0.2`). The BLE path's trajectory is 5x
coarser, across exactly the axis being compared. Setting `publish_period_s = dt_s`
matches them.

---

## 8a-sexies. First fully working run (2026-08-21)

`n6-ring`, 30 s, 6/6 nodes, trigger spread 27 ms. Every link delivering, every node
converging. The first result the testbed has produced.

```
udp_broadcast          10.6.7.255
udp_broadcast_source   kernel/wlan0
prefixlen              22
interface_used         wlan0
```

**All twelve directed links carried traffic:**

```
 22(bridge h2) ->  1(ble    h4)  BLE  fresh 0.65   <-- the one outlier
  2(ble    h2) ->  1(ble    h4)  BLE  fresh 1.00
 21(bridge h4) ->  2(ble    h2)  BLE  fresh 0.97
  1(ble    h4) -> 22(bridge h2)  BLE  fresh 1.00
  2(ble    h2) -> 21(bridge h4)  BLE  fresh 1.00
 11/12/21/22 UDP, both directions        fresh 0.97
```

**Convergence, all six agents:**

```
t= 0s  spread 24.283
t=15s  spread 10.323
t=30s  spread  4.359    still falling
```

What that jointly demonstrates, on hardware, for the first time: the C control law
on two nRFs and the Python law on four Pi agents driving one coordination problem;
BLE nRF-to-nRF, BLE nRF-to-Pi in both directions, and UDP between hosts; the epoch
transfer; the STATE relay path; v1 on the air; and the hub configuring, triggering,
stopping and collecting six agents across two machines.

## 8a-septies. After the reflash: the collapse is gone, and `fresh` meant two things

`n6-fast`, 120 s, reflashed. Both firmware fixes took:

```
all six nodes  3002 rows  25.0 Hz     (was 601 rows / 5 Hz on ble)
every link     last packet at t=120.3s  (was: two links dead from t=104 and t=111)
spread         24.378 -> 0.001 by t=15 s, flat for the remaining 105 s
```

**The link collapse was the un-reflashed firmware.** The old code called
`broadcaster_update()` every `dt` -- 25 times a second -- while scanning
continuously. After 80-100 s of that, both nRFs stopped receiving from their
bridges. At the new 5 Hz publish rate neither does. So sustained 25 Hz
`bt_le_adv_update_data` degrades the Zephyr BT stack's own scan-report delivery;
worth remembering as a ceiling on how fast an nRF can be asked to re-advertise.

**But the freshness figures inverted**, and that turned out to be a defect in the
measurement, not the radio:

```
into an nRF      0.315 - 0.345
into a Pi agent  0.995 - 0.998
```

The same column name meant two different things:

| | definition | window | result on a healthy 5 Hz link |
|---|---|---|---|
| Pi agent | value younger than `max_neighbor_age_s` = `3 * publish_period_s` | 600 ms | ~1.00 |
| nRF | a packet arrived since the last report | 40 ms | ceiling 0.2 |

A **staleness** test against a **arrival** test. Neither is wrong; they answer
different questions, and the platform's headline comparison runs straight across
the boundary. It stayed hidden while the nRF reported at 1 Hz -- both windows were
then longer than the publish period, so both read ~1.0 -- and surfaced the moment
the nRF began reporting five times faster than anyone published.

Fixed on the host side, because an arrival flag is strictly more informative:
staleness can be derived from arrivals, not the reverse. `NeighborTable` now has
both, and they have different jobs:

* `freshness()` -- the staleness test, still what the **controller** reads. A
  neighbour whose value is 200 ms old must not drop out of the coupling term just
  because nothing arrived in the last 40 ms control period.
* `arrivals()` -- read-and-clear, what the **log** records. Identical semantics to
  the nRF's `fresh` mask.

Note what nearly went wrong in the diagnosis: a "definition-independent" metric of
rising edges per second gave 7.9-8.6 /s for the nRF receivers and 0.02 /s for the Pi
receivers. That is not a radio difference either -- a staleness flag sits at 1 and
therefore has almost no rising edges. The metric was measuring the second definition
rather than escaping both.

### Symmetry between the two implementations: three rates, all backwards

The 0.65 link led to this. `main.c` had **three** rate asymmetries against a Pi
agent, all in its thread layout, and all in the wrong direction:

| | Pi agent | nRF (before) | consequence |
|---|---|---|---|
| neighbour data absorbed | on packet arrival | at `clock` (1 Hz) | the law took `clock/dt` steps on values up to 1 s stale |
| state published | every `publish_period_s` | every `dt` | `clock/dt` times more airtime, and that many more chances past a receiver's duplicate filter |
| sample logged | every `dt` | every `clock` | the `ble` trajectory `clock/dt` times coarser than the `wifi` one in the same run |

**No wire change was needed.** `dt` and `clock` were both already being sent; they
were wired to the wrong things. Now:

* `dynamics_thread` (`dt`): drain the observer queue, then step. **No I/O** -- this
  is the thread whose period the experiment depends on, and an HCI round trip or a
  UART write in it is jitter in the control period.
* `network_thread` (`dt`): snapshot and `report_state()` every tick; publish every
  `clock/dt`-th tick.

One trap in doing it: **`tx_seq` must increment per publish, never per step.** A
sequence number advancing per step while only every Nth packet goes out reads at
the receiver as `(N-1)/N` of the traffic lost -- it would have manufactured 80%
packet loss out of nothing.

```
dt=200ms clock=1000ms -> step  5.0 Hz  report  5.0 Hz  publish 1.0 Hz
dt= 40ms clock= 200ms -> step 25.0 Hz  report 25.0 Hz  publish 5.0 Hz
```

### `n6-fast`: the symmetric-rate manifest

`experiments/n6-fast.yaml`. Same topology and forced ordering as `n6-ring`, 25 Hz
dynamics, 5 Hz publish, gains rescaled so the two are comparable.

```
dt_s   0.04     alpha 0.004   (0.02 / 5)    per-second coupling 0.1000 /s
                eta   4e-07   (2e-6 / 5)    n6-ring: 0.02/0.2 = 0.1000 /s
                delta 0.01    unchanged -- a dead-band, not a rate
publish_period_s 0.2  -> step & report 25 Hz, publish every 5 ticks = 5 Hz
```

Verified rather than argued: the real C law run at both configurations produces the
same continuous-time trajectory, converging as `dt` shrinks.

```
t(s)   n6-ring vstate   n6-fast vstate    diff
   0       22.293928       22.298784   +0.004856
  19       22.042884       22.044270   +0.001386
```

Which fields scale and which do not:

| field | scales with dt? | why |
|---|---|---|
| `alpha`, `eta` | **yes, /5** | per-step gains; `vstate += alpha*v_i` has no dt, so the per-second gain is `alpha/dt` |
| `delta` | no | a threshold on `|sigma|` in state units |
| `beta`, `sine_amplitude` | no | the step adds `disturbance * dt`, so their per-second contribution is dt-invariant |
| `noise_amplitude` | **yes, x sqrt(5)** | independent draws accumulate as a random walk, std proportional to `amp*sqrt(dt)`; 2.5e-3 -> 5.5902e-3 |
| `period_samples` | **yes, x5** | the disturbance repeats every `samples*dt`; 1000 would cycle every 40 s and repeat three times inside a 120 s run. 3000 gives exactly 120 s |

### The 10 Hz sine: requested, and worth knowing about

At `dt = 0.04` a 10 Hz sine advances **0.4 cycles per step = 2/5**, so the
evaluated sequence repeats every **5 steps -- exactly the publish period**:

```
f= 10.0 Hz  0.40 cycles/step = 2/5   -> repeats every  5 steps (0.20 s)
f=  9.0 Hz  0.36 cycles/step = 9/25  -> repeats every 25 steps (1.00 s)
f= 11.0 Hz  0.44 cycles/step = 11/25 -> repeats every 25 steps (1.00 s)
```

Two consequences. Every publish samples the same phase of the disturbance, and the
sine's net contribution to the state between publishes is exactly zero -- it sums to
zero over its 5-step repeat. So it ripples *within* a window, visible in the 25 Hz
log, but does not perturb the inter-agent dynamics, which is presumably the point of
having it.

It is also only 2.5 samples per cycle. That is inside the 12.5 Hz Nyquist limit, so
the sequence is well-defined, but it does not resemble a smooth sine.

9 Hz or 11 Hz repeats every 25 steps and avoids the coincidence. Shipped at 10 Hz
as asked -- `sine_frequency_hz` is a one-field change in `CONTROLLER_FAST`.

### Choosing dt and the publish period

**25 Hz dynamics with a 0.2 s publish period is a good choice, but alpha and eta
must be rescaled with it.**

The discrete law does not multiply the coupling by `dt`:

```c
gi = alpha * v_i(a);          /* no dt */
vstate_f = z + gi;
vartheta_f = vartheta + eta * dvtheta;   /* no dt */
nu = disturbance(a) * dt;     /* the disturbance IS dt-scaled */
```

So `alpha` is a **per-step** gain and the per-second gain is `alpha/dt`:

```
dt=0.2   alpha=0.02   -> 0.100 /s
dt=0.04  alpha=0.02   -> 0.500 /s     <- 5x faster: a different experiment
dt=0.04  alpha=0.004  -> 0.100 /s     <- same dynamics, finer sampling
```

Dropping `dt` from 0.2 to 0.04 without touching `alpha` makes the coupling five
times faster per second. That is not a finer sample of the same experiment; it is a
different one. Divide `alpha` and `eta` by the same factor as `dt` to keep the
continuous-time behaviour, which is what the validator's "both absorb the step
size" warning is about.

A side benefit: the finite-time law overshoots once `|sigma| < alpha^2`, so a
smaller `alpha` also lowers the chatter floor -- `4.0e-4` at `alpha=0.02` against
`1.6e-5` at `0.004`.

Everything else about 25 Hz / 0.2 s checks out:

```
advertisements per published value  2      (adv interval 100 ms) -- want >= 2
STATE at 25 Hz, 4 neighbours        1275 B/s of 11520  = 11% of the UART
a 120 s run                         3000 rows/node = 164 KB
```

One thing to change with `dt`: the disturbance repeats every `samples * dt`, so
`samples=1000` goes from a 200 s cycle to a 40 s one. For a 120 s run use
`samples=3000` to keep the disturbance from repeating three times inside it.

### The one anomaly worth chasing

`22 -> 1` at **0.65** while every other link is 0.97-1.00. Both are bridge-to-nRF
BLE hops, and its mirror `21 -> 2` is 0.97, so it is not purely structural.

The likely mechanism is the **publish-rate asymmetry**, and this is the first
measurement that makes it visible. An nRF re-advertises on every control step
(`dt` = 200 ms, 5 Hz) because `main.c`'s dynamics thread calls
`broadcaster_update()` after each `discrete_step()`. A Pi agent publishes at
`publish_period_s` (1 Hz here). Combined with the nRF's scanner running
`BT_LE_SCAN_OPT_FILTER_DUPLICATE` (§8b.A0), a receiver gets one un-suppressed
report per *distinct payload*: five chances per second from an nRF source, one from
a Pi source. Losing one of five is invisible; losing one of one is a 1.00 -> 0.00
step for that window.

That would make every Pi-to-nRF link fragile in a way no Pi-to-Pi or nRF-to-anything
link is -- and it sits exactly on the axis being compared. Three things to separate,
in order of cost:

1. Set `publish_period_s = dt_s` so both sources publish at the same rate. Also
   fixes the 5x sampling-resolution gap between `ble` (31 rows) and `wifi`/`bridge`
   (150 rows) in the same run.
2. Turn off duplicate filtering on the nRF's scanner (§8b.A0) and re-measure.
3. Only then look at RF: `21 -> 2` at 0.97 versus `22 -> 1` at 0.65 across
   nominally identical hops may still be geometry, and the recorded RSSI is the
   thing to check.

Do not read 0.65 as a per-link loss figure yet. It is a *freshness* figure over
1 s windows, and with a 1 Hz publisher the window boundary alone can produce it.

### Second two-host run: timestamps fixed, UDP still dead -- and the log said why

Re-run of `n6-ring`, 30 s, trigger spread 19 ms.

**The timestamp origin is fixed.** All six nodes now start at ~0.2 s:

```
node  1 ble    timestamp   0.227.. 30.227   device   0.002.. 30.002
node 11 wifi   timestamp   0.201.. 30.001   device   0.201.. 30.001
```

**UDP still delivered nothing**, and the recorded environment identified the cause
without any guessing:

```
udp_broadcast          10.6.5.255
udp_broadcast_source   assumed /24        <- the kernel was never asked
prefixlen              (key absent)
```

`prefixlen` is written only when an interface is known, so its absence proved
`AgentService` had received `interface=None` -- the /24 fallback was taken before
the ioctl was ever tried. `vertex/agent/__main__.py` was still not passing
`interface=args.interface`: that edit had been in a batch that aborted on an
earlier anchor miss, so it never applied while the rest of the batch did.

Two changes, one for the bug and one for the class of bug:

* `__main__.py` passes the interface.
* **It no longer has to.** `interface_for_ip()` derives the interface from
  `host_ip`, so `_broadcast_target()` reaches the kernel even when the caller
  omits it. A required argument that can be forgotten will be, and the penalty
  here is an entire run: the fallback works, nothing complains, and every datagram
  goes to an address nobody holds.

```
with interface given : ('10.6.3.255', 'kernel/enp2s0')
with only host_ip    : ('10.6.3.255', 'kernel/enp2s0')
with neither         : ('255.255.255.255', 'limited broadcast')
```

`interface_used` is now recorded alongside `prefixlen`, so "which interface did
this actually use" is answerable from the data.

**The recorded environment paid for itself immediately.** Two runs earlier there
was no `udp_broadcast_source` field and the same failure would have needed another
round of hardware bisection. Recording *how* a value was determined, not just the
value, is what made a silent fallback visible.

**On the process failure.** This is the third silent `str.replace` miss in this
work, and the second to reach hardware. Batched edits now apply per file and report
`applied/total` per file, so one miss cannot mask the rest -- and the verification
asserts *behaviour* (`_broadcast_target()` returns the kernel's address) rather than
that the text changed.

---

## 8a-octies. Freshness is a proxy; the real per-link figure was never recorded

Same manifest, host-side `arrivals()` fix in place, so both agent types now log the
same *definition*. Convergence unchanged: spread 24.378 -> 0.001 by t=15 s, held for
105 s. But the columns still are not comparable, and the transport counters -- added
one run earlier for exactly this -- showed why.

```
freshness column      into an nRF 0.33-0.34   into a Pi (UDP) 0.133-0.137
UdpTransport counters sent 600  received 2367  self_filtered 600  delivered 1767
```

**UDP delivery is 98%, not the 14% the freshness column suggests.** Two separate
effects, both in the measurement:

*Extra receivers.* Node 11's neighbours sent 600 each = 1200, yet 1767 were
delivered. The third sender is node **21, on the same host**: subnet broadcast
reaches every socket on the subnet, so a bridge's UDP half lands in its host-mate's
socket too. 3 x 600 = 1800 expected, 1767 seen = **98.2%**. The extras are then
dropped by `NeighborTable` as not-a-neighbour, so they never reached the rows.

*Arrival bunching.* The flag is a boolean per sampling window, and the gap pattern
gives it away:

```
12->11 UDP  gaps of 7-8 samples (91%)  = 280-320 ms between flags
            but node 12 publishes every 200 ms (5 samples)
            589 arrivals / 407 flags = 1.45 per window -> 7.2 samples. Exact match.
```

Two datagrams land inside one 40 ms window about 31% of the time, and a boolean can
only record one.

*And the rates still differ by medium.* On BLE the gaps are 2-3 samples =
80-120 ms, which is the **100 ms advertising interval**, not the 200 ms publish
period. Each published value is advertised about twice over its life and the
duplicate filter is not suppressing the repeat, so a BLE receiver flags ~8.2/s
against 5 publishes/s while a UDP receiver flags ~3.4/s. Same definition, different
rate: a BLE link repeats each value, a UDP link sends it once.

So freshness cannot be compared across media at all. It is useful for one thing --
"was this link alive at time t", which is what caught the collapse -- and nothing
more.

**The comparable metric already existed and was not recorded.** `LinkStats` infers
`expected` from **sequence-number gaps**, which counts published values rather than
transmissions: a re-advertised value scores a `duplicate` rather than a second
delivery, and a receiver sampling faster than the sender publishes cannot undercount
it. `link_stats()` was surfaced in `status` and nowhere else, so it vanished when a
run ended. Now written into the run's environment beside the transport counters:

```json
"links": {"12": {"received": 40, "expected": 40, "lost": 0, "duplicates": 0,
                 "reordered": 0, "delivery_ratio": 1.0, "median_delay_us": 1542.0}}
```

That is the number to compare BLE against UDP with, and the next run will carry it.

## 8a-nonies. The first real per-link measurement (2026-08-21)

`n6-fast`, 120 s, schema v5, with `link_stats` recorded. Delivery from
**sequence-number gaps**, so it counts published values rather than transmissions:

```
    link  medium   recv    exp  lost    dup   ratio
  12->11  UDP       587    599    12      0  0.9800
  22->11  UDP       583    599    16      0  0.9733
  11->12  UDP       580    599    19      0  0.9683
  21->12  UDP       584    599    15      0  0.9750
   2->21  BLE       524    600    76    273  0.8733
   1->22  BLE       524    600    76    266  0.8733
```

**BLE 87.3%, UDP 96.8-98.0%** -- a 10-point gap, and the first number the platform
has produced that answers the question it was built for. The duplicate counts
confirm the earlier inference from the freshness gaps: 524 + 273 = 797 packets for
600 published values, so each value reaches the receiver about 1.33 times. That is
the advertising interval being shorter than the publish period, counted correctly as
duplicates rather than as deliveries.

**The delays were nonsense**, and it was the same bug as the relay's link clock --
fixed in one place and not as a class:

```
  12->11  -10462 ms      11->12  +10814 ms      1->22  +20863 ms
```

Objects are built at `configure` and the epoch arrives at `start`, so every holder
of a clock reference keeps the launch-time one. `BleRelay.link` had been fixed;
`Agent`, `UdpTransport`, `BleTransport` and `MultiTransport`'s members had not. So
`tx_time_us` and `rx_time_us` sat on two different origins and each "delay" was
really the gap between two agents' start times -- in the right units, off by four
orders of magnitude.

`AgentService._rebind_clock()` now enumerates every holder in one place, so a new
one is a line there rather than another wrong delay figure. And `check_fleet.py`
fails any median delay above 1 s, which caught a **third** instance immediately: the
harness's own `LoopbackBus` stamps `rx_time_us` and held a clock made before the
epoch existed, manufacturing the exact offset the check looks for. Delays now read
+0.121 to +0.158 ms on loopback.

## 8a-decies. A1/A3 closed; the new baseline, and a 172 ms UDP delay

Both ADs now carry the manufacturer element and nothing else -- 20 bytes,
`ADV_NONCONN_IND`, 864 us per advertising event on both sides. The nRF's name
element (9 bytes, read by nothing) and its scan-response data (which promoted the
PDU to `ADV_SCAN_IND`) are gone, and the Pi's flags element went with them so the
two arms emit byte-identical PDUs. 11 spare AD bytes, up from 2.

```
    link  med  recv  exp  lost  dup   ratio   delay ms
  12->11  UDP   594  600     6    0  0.9900     171.96
  11->12  UDP   588  600    12    0  0.9800     172.59
   2->21  BLE   528  600    72  289  0.8800     109.19
   1->22  BLE   539  600    61  294  0.8983     108.47
```

**A1/A3 did not materially change delivery**, which was the expectation and is a
useful result: BLE 88.0-89.8% against 87.3% before, UDP 98.0-99.0% against
96.8-98.0% -- both inside run-to-run variation. So the ~11% BLE loss is *not*
explained by AD size or scannability, and the candidate list narrows to the
duplicate-per-value effect, the scan window, and genuine coexistence. Duplicates
held at 289-294, as predicted: the 100 ms advertising interval against a 200 ms
publish period is untouched.

**The delays are real now** -- the clock rebinding worked, and no figure is in
seconds. BLE at 108-109 ms is *correct and expected*: a published value waits for
the next advertising event, and the advertising interval is 100 ms. That is the
floor, measured properly for the first time.

**UDP at ~172 ms is not.** A LAN one-way delay should be about 1 ms. Two things
rule out the previous class of bug:

* all six UDP links agree to within 2.9 ms, and
* both directions are **positive and equal** (12->11 +172.0, 11->12 +172.6).
  Clock skew is antisymmetric -- it reads +x one way and -x the other, as the
  -10462/+10814 pair did. This is a real, symmetric latency.

Prime suspect is **WLAN power save**: a sleeping station's traffic is buffered
until the next beacon, which produces exactly this scale. `scripts/host_report.sh`
already prints `wlan_powersave`; `iw dev wlan0 set power_save off` on both hosts is
the test. Note the run's own `radio` block records the BLE parameters but nothing
about the WLAN side -- worth adding, since a 172 ms transport delay is a first-order
property of the experiment.

I should also have been recording **`min_delay_us`**, not only the median. The
minimum is the propagation floor -- immune to queueing, so it separates "this link
is slow" from "this link queues" -- and a *negative* minimum is a direct measurement
of residual clock skew, the cheapest available check that chrony is working. Now
recorded, along with the sample count.

## 8a-undecies. It is not power save: UDP broadcast is being held at the AP

`power_save off` recorded in the run's own metadata, and the delay did not move:

```
power_save off   wlan_channel 11   wlan_freq_mhz 2462   wlan_txpower_dbm 31.0
wlan_type managed

    link  med   ratio  dup  min ms  median ms    n
  12->11  UDP  0.9816    0   20.80     175.36  588
  11->12  UDP  0.9850    0   16.50     171.39  590
   2->21  BLE  0.9000  314    6.49     107.55  854
   1->22  BLE  0.8915  265    4.93     104.39  799
```

`min_delay_us` is what settled it. **The minimum is 8-21x below the median on every
link**, so the 172 ms is a distribution, not a link property -- and each medium has
its own explanation.

**BLE is understood and correct.** A published value waits 0-100 ms for the next
advertising event, and about 35% of samples are the *second* advertisement of the
same value roughly 100 ms later (265-314 duplicates of ~800 samples). A mixture of
U(0,100) and U(100,200) has its median above 100 ms. min 5 ms, median 105 ms,
consistent.

**UDP is not.** `duplicates = 0` and 588-595 samples against 600 published, so every
datagram arrives exactly once -- there is no duplication to shift the median. A
16 ms minimum with a 171 ms median means the datagrams are being **held**.

`wlan_type = managed`: both Pis are stations on an access point, so every packet
goes Pi -> AP -> Pi. **Broadcast and multicast frames through an AP are buffered
until the next DTIM beacon whenever any associated station is dozing** -- not just
ours. A 100 ms beacon with DTIM 1-3 gives 100-300 ms of buffering, mean wait
50-150 ms. That is why `power_save off` on our own Pis changed nothing: the
buffering is at the AP, driven by other clients.

If that holds it is a first-order finding rather than a nuisance. The platform's
Wi-Fi transport is **subnet broadcast** (chosen in the §10 log to avoid IGMP
snooping and get one-frame-reaches-all airtime), and on infrastructure WLAN that
choice buys a DTIM-scale latency penalty that unicast does not pay. With a 200 ms
publish period, Wi-Fi neighbour data is nearly a full period stale while BLE is half
that -- **the opposite of the ordering anyone would assume**, and squarely the kind
of transport asymmetry this testbed exists to find.

Three tests, in order of cost:

1. **Read the AP's beacon interval and DTIM period** -- `iw dev wlan0 scan` on the
   associated BSS. If DTIM x beacon is near 170 ms, that is the answer.
2. **Send unicast instead of broadcast.** Unicast is not DTIM-buffered.
   `UdpTransport` already takes `send_to`, so one datagram per neighbour is a small
   change and a decisive experiment. It costs airtime proportional to degree, which
   is itself the traffic knob the reviewer's question needs.
3. **Remove the AP** -- IBSS or a direct link. Cleanest, most disruptive.

Percentiles are now recorded per link (`p10/p25/p50/p75/p90/p99`) because the shape
is the diagnosis: a hard mode at a multiple of the beacon interval is DTIM
buffering, a smooth heavy tail is contention. min and median alone cannot separate
them.

## 8a-duodecies. Confirmed: the AP's DTIM cycle, to 0.8%

`scripts/ap_info.sh oficina_v2` on both hosts, identically:

```
BSSID  04:d9:f5:b2:ba:80   freq 2462   signal -28 dBm
beacon interval  100 TUs = 102.4 ms
DTIM period      3          ->  307.2 ms worst, 153.6 ms mean
```

A broadcast frame generated at a uniform point in the DTIM cycle waits `U(0, W)`
with `W = 307.2 ms`, so its median wait is `W/2 = 153.6 ms` and its minimum tends to
zero. The measured delay should therefore be that plus a fixed base hop cost, and
**`median - min` should equal `W/2` on every link**:

```
     link     min   median  median-min   vs W/2
   12->11   20.80   175.36      154.56    +0.96
   22->11   20.70   173.13      152.43    -1.17
   11->12   16.50   171.39      154.89    +1.29
   21->12   17.71   171.66      153.95    +0.35
```

Four independent links, all within 1.3 ms -- **0.8% error** against a prediction
derived from nothing but the AP's beacon interval and DTIM period. That is the
mechanism, not a hypothesis about it.

The residue, 16.5-20.8 ms, is the base cost: Pi -> AP -> Pi plus the receiver's
event-loop scheduling. It is the part unicast would keep.

### The trade-off, now quantified on both axes

|  | delivery | median delay | min delay |
|---|---|---|---|
| BLE | 89-90% | 104-108 ms | 5-6 ms |
| UDP broadcast | 98-99% | 171-175 ms | 16-21 ms |

BLE is ~9 points worse on delivery and ~65 ms **better** on latency. Neither
dominates, and each number has a mechanism behind it: BLE's latency is the
advertising interval, UDP's is the AP's DTIM cycle. This is the first result the
platform has produced that is a *finding* rather than a check.

Note what it does to the reviewer's question. The Wi-Fi path's latency penalty is
not a coexistence effect at all -- it is a consequence of choosing **subnet
broadcast on infrastructure WLAN**, and it is present at 2.6% duty with no
contention to speak of. Any claim about degradation under heavier traffic has to be
made *on top of* a 153.6 ms structural offset that has nothing to do with traffic.

### The next experiment writes itself, with a falsifiable prediction

Switch `UdpTransport` from subnet broadcast to unicast-per-neighbour:

```
broadcast: median ~171 ms, distribution roughly uniform from 18 to 325 ms
unicast  : median ~18 ms   (the DTIM term goes, the base hop stays)
```

If unicast comes back near 170 ms, DTIM is not the mechanism and this section is
wrong. If it comes back near 18 ms, the platform has a transport it can run
latency-sensitive experiments on -- and unicast airtime scales with **degree**,
which turns G1 vs G2 into a genuine traffic experiment, since broadcast airtime is
degree-independent.

## 8a-terdecies. Ten repeats: the first measurement with error bars

`n6-fast`, 10 x 120 s, `--run-index 0` held fixed so the initial conditions and
every node's disturbance stream are bit-identical and the network realisation is the
only variable. The replicate check passed on all ten.

```
convergence to spread < 0.01:   13.70 +- 0.17 s   (n=10)

     link  med   delivery            delay ms          rssi dBm
   22->1   BLE   >=0.9359 +-0.0533        --          -40.1 +- 1.7
    2->1   BLE   >=0.9635 +-0.0060        --          -33.2 +- 0.5
    1->2   BLE   >=0.9636 +-0.0087        --          -33.6 +- 0.4
   21->2   BLE   >=0.9325 +-0.0802        --          -43.5 +- 1.4
  12->11   UDP     0.9867 +-0.0082   169.90 +- 9.90       --
  22->11   UDP     0.9843 +-0.0075   169.32 +- 9.07       --
  21->12   UDP     0.9820 +-0.0093   170.77 +- 9.27       --
  11->12   UDP     0.9838 +-0.0080   171.80 +- 9.91       --
   2->21   BLE     0.8235 +-0.0220   105.84 +- 2.03   -38.6 +- 1.3
  12->21   UDP     0.9867 +-0.0082   169.94 +- 9.91       --
  11->22   UDP     0.9838 +-0.0080   171.86 +- 9.91       --
   1->22   BLE     0.8415 +-0.0195   107.38 +- 2.15   -37.3 +- 1.5
```

**Convergence time is 13.70 +- 0.17 s -- a 1.2% spread.** With the initial
conditions and disturbance held identical, that sd *is* the network's effect on
convergence, and at these loss rates it is small. That is the number the two-factor
design was built to produce.

**The transport asymmetry is now resolved well beyond its error bars.**

| | delivery | median delay |
|---|---|---|
| nRF -> Pi (BLE) | 0.824-0.842 +- 0.02 | 106 +- 2 ms |
| Pi -> nRF (BLE) | >=0.933-0.964 +- 0.01-0.08 | -- |
| UDP | 0.982-0.987 +- 0.008 | 170 +- 10 ms |

UDP delivers ~15 points better than nRF -> Pi BLE with sd ~0.01 on both, so the gap
is ~15 sd. And UDP's delay is ~64 ms *worse* with sd ~10 ms, so that gap is ~6 sd.
Both differences are real; neither transport dominates. Note the UDP delay sd of
~10 ms against a DTIM-derived mean -- the buffering is the mean, the sd is where in
the cycle each frame lands.

Also note the two directions of BLE differ by ~12 points on the same physical links,
and the weak direction is nRF -> Pi, where RSSI is -33 to -43 dBm. Signal is not the
constraint. The Pi's scanner has duplicate filtering **off**, so it receives roughly
two advertisements per published value and still misses ~17%; the nRF has it **on**,
sees about one per value, and misses ~4%. That points at the Pi's HCI receive path.

### Two bugs the repeat set exposed that a single run could not

**The nRF did not clear its neighbour `seq` and `rssi` on trigger.** The first two
samples of every run carried the *previous* run's values -- seq 600 before it reset
to 1. `apply_control` re-latched `neighbor_vstates` and not these two. Cross-run
state leakage is invisible in a single run and fatal to a replicate set, which is
exactly what a repeat set is for. Fixed in `agent.c`; needs a reflash.

**`link_delivery()` was destroyed by one stale sample.** It took `first` and `last`
of the seq series, so a leading 600 gave `span = (600-600) % 65536 + 1 = 1` against
586 distinct values -- a delivery ratio of **586**. Where the run ended at 599
instead, `span` became 65536 and the ratio 0.0089. Both appeared in the same set, and
the aggregate reported `253.95 +- 247.15`, which is what made it obvious.

The analysis now starts the window at the series minimum, reports `stale_prefix`,
and refuses to divide when the series is not monotonic rather than returning a ratio
above one. That recovers this dataset without a reflash: the four relay links read
0.933-0.964.

Worth stating the general shape, since it has now happened twice: **a statistic
computed from endpoints is destroyed by one bad sample, and an aggregate over
repeats is what surfaces it.** The single run before this reported 0.9583 for the
same link and looked entirely reasonable.

### Multi-run: repeats hold the configuration, not the initial conditions

`--repeat N` runs the same configuration N times as `<base>-r0..rN-1`, and
**deliberately does not advance the run index**. That index seeds both the initial
conditions and every node's disturbance stream, so holding it fixed makes those
bit-identical across the set and leaves the network realisation -- which packets
arrive, and when -- as the only thing that varies. That is what isolates the
transport's effect on the control law.

Varying the initial conditions is a *separate* dimension: several `--run-index`
values, each with its own set of repeats, gives the two-factor design. Folding both
into one loop would confound them, which is why `--repeat` does not touch the index.

```
python3 -m vertex.hub run experiments/n6-fast.yaml --duration 120 --repeat 10
python3 tools/compare_runs.py runs/n6-fast-0-r*
```

Each repeat still gets its **own epoch** -- a wall-clock origin is not part of the
configuration, and sharing one would put every run's timestamps on the first run's
origin. `--settle-between` (default 5 s) lets neighbour tables and radios quiesce,
so run *k+1* does not begin with values from run *k* still inside a freshness
window.

`tools/compare_runs.py` reports every figure as mean +- sd with n, because a single
run cannot resolve an effect smaller than the run-to-run spread and on this platform
that spread is not small: BLE delivery has ranged over 6.2 points across nominally
identical runs while UDP moved 2.2.

**It verifies the set is a set before computing anything.** Every node's initial
`vstate` is compared across the runs, and a mismatch is reported as
`NOT REPLICATES` with the node and both values. Averaging runs that differ in their
initial conditions silently mixes two sources of variance, and the resulting sd
would look like network variability. Verified by perturbing one run's first sample:
the check names it.

Two harness faults surfaced immediately, both of a class the product had already
fixed:

* **the fake nRF never reset its step counter**, so repeat *r1* began where *r0*
  stopped and the replicate check failed on the `ble` nodes. Real firmware
  re-latches state, vstate, counter and `tx_seq` in `apply_control`; the fake did
  not, and was unfaithful in exactly the way that breaks a replicate set.
* **the harness's shared bus held a clock from the first run's epoch**, so repeats
  *r1* and *r2* reported delays of ~3.5 s -- the same stale-clock class as
  `_rebind_clock`, in the one clock holder outside `AgentService`'s reach.
  `run_repeated` now takes a `before_each(epoch)` hook for precisely that, and the
  harness re-anchors the bus per run.

### Only 8 of 12 links were being measured -- and the missing 4 were the risky ones

`links` in a run's metadata comes from `agent.neighbors.link_stats()`, and a `ble`
agent has no local `Agent` -- the law and the neighbour table live on the nRF. So
every link *terminating at a relay* had no delivery statistics at all:

```
  22->1 BLE  NO      2->1 BLE  NO      1->2 BLE  NO      21->2 BLE  NO
  8 others   yes
```

All four are BLE, and all four are the **Pi -> nRF** direction. So every BLE figure
quoted before this -- 89-90% delivery, 104-108 ms -- was nRF -> Pi only. The other
direction was unmeasured, and it is the one that had already misbehaved: 0.65 and
0.72 in the freshness era, then both links collapsing at t=104 s and t=111 s.

A per-link measurement that silently covers 8 of 12 is worse than none, because the
eight look complete.

**Fixed by logging the sender's `seq` per neighbour per row, schema v6.** Both
values needed were already captured on both paths and discarded at the last
boundary: `observer.h` held `seq[]` and `rssi[]` and `report.c` did not send them;
`hci.py` parsed `rssi` and `Reception` had no field for it.

```
STATE neighbour record   [vstate:4][flags:1] -> [vstate:4][seq:2][rssi:1][flags:1]
row columns per neighbour  2 -> 4   (vstate, rx_, seq_, rssi_)
4 neighbours at 25 Hz      51 -> 63 B frames = 13.7% of the UART, was 10.2%
```

Logging `seq` rather than a delivery ratio is deliberate: **loss, duplicates,
reordering and per-window ratios are all derivable offline, at any window size
chosen after the fact.** `NodeRun.link_delivery()` does it, and it works for every
link including those terminating at a relay -- which the end-of-run aggregate
structurally cannot. `rssi` is the discriminator between "the signal got worse"
(interference) and "packets were dropped elsewhere" (load), which is the distinction
the review question turns on.

`report.h` now defines `STATE_NEIGHBOUR_BYTES` and the Python struct is checked
against it, so the two cannot drift silently.

Three things the harness caught while doing this, each of which would have reached
the bench:

* the fake nRF still packed the 2-field neighbour record, so its report thread died
  and both relays logged nothing -- surfaced as "rows file decoded to nothing";
* a relay has no `Transport` by design, so nothing a `ble` node sends reaches the
  loopback bus and every link *sourced* at one was unmeasurable. The harness now
  advertises on each relay's behalf, which is what the board does on hardware;
* stamping `tx_time_us` as a constant in that advertiser made every delay the
  elapsed time -- caught by the 1 s delay guard added one run earlier, at +1.057 s.

`check_fleet.py` now fails if any declared link lacks a seq-derived delivery figure.
Verified by disabling the `seq` column: 12 failures.

## PTA cannot schedule someone else's radio (2026-08-21)

*(2026-08-21, JI's hypothesis, supported by the 10-run set.)*

The nRF52 is a single-protocol radio. It has no Packet Traffic Arbitration, and does
not need any: nothing else on that chip competes for the antenna. The CYW43455 does
have PTA, because WLAN and BLE share one front-end there.

**But PTA can only arbitrate transmissions the chip itself originates.** The Pi can
schedule its own BLE TX into gaps around its own WLAN activity. It cannot schedule
the nRF's. So a packet from the nRF arrives whenever the nRF's advertising timer
says, with no knowledge of what the Pi's radio is doing -- and the direction where
the Pi is *receiving* is the one with no coordination available to it.

That predicts the asymmetry, and the sign is right: nRF -> Pi is the weak direction.

**The RSSI distributions localise it to the Pi's receive path.** Geometry is fixed
and the transmitter is the same board in each pair, so any spread difference is a
receiver property:

| case | p5..p95 | span | min | delivery |
|---|---|---|---|---|
| nRF -> nRF, neither end shares a front-end | -38..-31 | **7 dB** | -41 | 0.964 |
| Pi -> nRF, receiver is the nRF | -44..-37 | **7 dB** | -50 | 0.936 |
| nRF -> Pi, receiver is the CYW43455 | -59..-31 | **28 dB** | -91 | 0.824 |
| nRF -> Pi, receiver is the CYW43455 | -58..-32 | **26 dB** | -85 | 0.842 |

Four times the spread and a tail 40 dB below the median, on exactly the two links
where the shared front-end is doing the receiving. The span tracks *which end
receives*, not distance, channel or transmitter.

Note what this rules out. A weak-signal explanation would show a *lower* median, and
it does not -- the medians are within a few dB across all four. What changes is the
variance, which is the signature of a receiver whose sensitivity is being modulated
by something other than the incoming signal. The CYW43455's coexistence design
(shared LNA, joint AGC -- the datasheet passage that motivated dropping the USB
dongle in the first place, §3 A2) gives that a concrete mechanism: an AGC set for
WLAN levels is not set for a -35 dBm BLE advertisement.

It also rules out the simpler reading of pure TX blanking. Blanking loses packets the
receiver never hears at all, leaving the ones it does hear looking normal. A 40 dB
low tail means the Pi *is* hearing marginal packets, so its sensitivity is varying
rather than its receiver being switched off.

#### The falsifiable prediction, and why it is the experiment the review asked for

Load the Pi's WLAN and watch the two directions separately:

* **nRF -> Pi** delivery degrades, and its RSSI spread widens further.
* **Pi -> nRF** delivery stays flat, because PTA is scheduling that direction.
* the **asymmetry between them grows with load**.

If both directions degrade equally, this section is wrong and the cause is shared
airtime rather than receiver arbitration. If neither degrades, the effect is not
coexistence at all.

That is a *directional* prediction, which is worth much more than "performance
degrades under heavier traffic". The review asked for quantitative evidence that
degradation under G2 is attributable to communication traffic and coexistence. A
monotonic decline in aggregate delivery cannot separate those two. A decline in one
direction only, on the link whose receiver shares a front-end, with a widening RSSI
spread, distinguishes coexistence from congestion by construction.

Note also that the platform's *own* Wi-Fi transport is the load source, so this is
not a hypothetical interaction: at 0.52% duty the asymmetry is already 12 points.
`iperf3` between the hosts at stepped rates is the controlled version.

#### It also reframes the G1/G2 comparison

Degree does not change broadcast airtime (C2.2), so G1 vs G2 cannot vary traffic on
the medium. But it does change how many *accepted* packets each receiver processes,
and under this mechanism the receiver is where the damage happens. So G1 vs G2 may
still show an effect -- attributable to receive-side load, not to airtime. Reporting
it as the latter would be the same error as attributing the 153.6 ms UDP delay to
congestion.

## The publish-rate sweep: the advertising interval is a rate limiter (2026-08-24)

*(2026-08-24. Four publish periods x 10 repeats x 120 s, 6 agents, identical
initial conditions across all 40 runs.)*

Intended as an airtime sweep. It is not one, and the reason is the single most
important measurement on this platform so far.

**The advertising interval does not change when the publish period does.**
`broadcaster_update()` rewrites the AD payload; `bt_le_adv_start()` keeps
advertising at `adv_interval_ms` = 100 ms regardless. So a receiver can observe at
most 10 distinct values per second per advertiser, no matter how fast the publisher
produces them. Publishing faster than that overwrites values before they are ever
transmitted. That is not packet loss -- it is **undersampling at the transmitter**,
and it is indistinguishable from loss in any delivery statistic that counts distinct
sequence numbers.

The ceiling is `min(1, T_pub / T_adv)`, and it predicts the raw numbers directly:

| publish | ceiling | nRF->nRF observed | obs/ceiling |
|---|---|---|---|
| 2.5 Hz | 1.000 | 0.9973 | 0.997 |
| 5.0 Hz | 1.000 | 0.9619 | 0.962 |
| 12.5 Hz | 0.800 | 0.6546 | **0.818** |
| 25.0 Hz | 0.400 | 0.3305 | **0.826** |

0.818 and 0.826 -- the two capped points normalise to the same number. The apparent
collapse from 0.997 to 0.331 is the cap, not the medium.

UDP over the same sweep: 0.9865 -> 0.9783. Flat, because every publish is its own
datagram and no such ceiling exists.

**Consequence: no delivery ratio on this platform is interpretable without stating
`T_pub / T_adv` alongside it.** Any comparison across publish rates that does not
normalise by the ceiling is measuring the advertising interval.

#### What this does and does not do to A3.3

It **refutes the level explanation**. `nRF -> nRF` involves no CYW43455 at either
end, and it degrades identically to `Pi -> nRF` (0.3305 vs 0.3327, agreeing to <0.02
at every point). If the falling delivery were coexistence, the link with no shared
front-end at either end would have been immune. It was not.

It **supports the asymmetry claim**, and more strongly than the RSSI data did.
Normalise all three categories by the ceiling and the two directions that do not
involve a Pi receiver land on top of each other, while `nRF -> Pi` sits below both
at every point and the gap widens with rate:

| publish | nRF->Pi | Pi->nRF | nRF->nRF | gap |
|---|---|---|---|---|
| 2.5 Hz | 0.968 | 0.997 | 0.997 | 0.03 |
| 5.0 Hz | 0.843 | 0.878* | 0.962 | 0.12 |
| 12.5 Hz | 0.619 | 0.828 | 0.818 | 0.20 |
| 25.0 Hz | **0.602** | 0.832 | 0.826 | **0.23** |

\* includes one collapsed link, see below.

So the two effects separate cleanly: **the level is the advertising cap, the
asymmetry is the Pi's receive path.** A3.3's mechanism survives; A3.3's framing of
falling delivery as evidence for it does not, and the WLAN-load experiment there is
still the test that decides it.

#### Anomaly: one run in forty

`sweep-p200-r1` converged at 103.80 s against 13.79 +- 0.17 s for the other nine at
that point, with one link at 0.107 while its own worst-case peers sat at 0.79-0.88.
A single link collapsing, not a gradual degradation -- the same signature as
`n6-fast-0` r6/r9. Three occurrences now; still undiagnosed. It inflates p200's
`Pi -> nRF` mean and is the whole of its +-0.259.

#### Fix before re-running

Scale `adv_interval_ms` with the publish period so the ceiling stays at 1.0, and the
sweep will measure airtime instead of the transmitter's sampling rate.

## The corrected sweep: an HCI socket per run exhausts the adapter (2026-08-24)

Re-ran the publish-rate sweep with the advertising interval pinned to the publish
period. **p400 and p200 completed clean, 10 repeats each. p080 lost both bridge
agents on all 10 runs. p040 never ran.**

```
FAIL 21 bridge 10.6.5.2:3003  samples=0 files=2 -- start refused
     HciError: cannot take hci0 on the user channel ([Errno 16] Device or
     resource busy). The adapter is up, or another socket still holds it.
```

The error text had predicted this failure mode in advance, including the fix.

**Cause.** `AgentService._build()` calls `_make_transport()` on every run, which
constructed a fresh `BleTransport`, which opened its own HCI user channel. So a
sweep opens one socket per repeat. `close()` does not release `hci0` immediately,
and after p400 and p200 had consumed 20 opens the next one failed with EBUSY. Once
it failed it stayed failed for every remaining repeat of that point, because
nothing retried the bind.

Note what this means for the two good points: **p400 and p200 are valid**. The
failure is not rate-dependent, it is cumulative, and those two simply came first.
Had the sweep run in the opposite order, p040 and p080 would have been the
survivors.

**Fix.** One socket per *process*, opened lazily by `AgentService._hci_socket()`
and closed only at shutdown. `BleTransport` already had the seam -- an injected
`sock=` sets `_owns_sock = False`, so `stop()` leaves it open and `start()` skips
`HCI_Reset`. Reopening was never necessary: advertising and scan parameters are
settable on a live socket once the corresponding function is disabled, which
`start()` does regardless. `HCI_Reset` now happens once per process, on first open,
which is what clears a previous process's leftovers.

Regression test at `test/transports/check_hci_reuse.py`: ten transport builds must
produce exactly one open. It is a counting test rather than a behavioural one
because reproducing the live failure needs ~20 real opens -- nothing short of a
full sweep triggers it, which is precisely why it reached hardware.

**Third bug, in `scripts/agents.sh`.** Recovery was blocked by the stop path:
`stop()` sent TERM and deleted the pidfile in the same breath, so a process slower
to exit than that became an orphan — still holding `hci0` on the user channel, no
longer tracked by `status` or reachable by `stop`. `hciconfig hci0 down` then
returned EBUSY too, because a user-channel socket owns the device exclusively and
the kernel refuses hciconfig while it is held. Two failure modes pointing at the
same adapter with no indication of who held it. `stop()` now waits for exit and
escalates to KILL, keeps the pidfile if the process survives, and `agents.sh hci`
reports the holder.

**Second bug, in `scripts/sweep.sh`.** `set -o pipefail` propagated p080's failure
through the `tee`, and `set -e` then killed the loop, so p040 never ran at all. One
failing point should cost one point. Now isolated, with the failed points named in
the summary.

---

## The advertising floor: 100 ms, not 20 (2026-08-24)

Re-ran p080 and p040 after the HCI socket fix. Different failure, both bridges,
all 10 runs:

```
FAIL 21 bridge -- start refused
     HciError: opcode 0x2006: invalid HCI command parameters
```

`0x2006` is `LE_Set_Advertising_Parameters`. The only thing that changed between
p200 (worked) and p080 (failed) is the interval: 80 ms = 128 units, 40 ms = 64.

**The CYW43455 enforces the Bluetooth 4.x floor.** 4.x required
`Advertising_Interval_Min >= 0x00A0` (100 ms) for `ADV_NONCONN_IND` and
`ADV_SCAN_IND`; 5.0 removed it. `RadioSpec` had `ge=20.0` with a comment citing the
5.0 rule — the host was permissive and the controller was not, so a manifest that
validated cleanly failed at `start` on every repeat.

`scripts/adv_floor.py` binary-searches the real floor per advertising type, so the
next controller is measured rather than assumed.

**Consequences.**

* `RadioSpec.adv_interval_ms` floor raised to 100 ms — a validation error now,
  which costs one message instead of ten runs.
* `radio_for()` **clamps** instead of raising, so the same interval still reaches
  both agent classes and both sit on the same ceiling. Clamping one side only
  would recreate the JS platform's defect.
* p080 and p040 are capped at 0.80 and 0.40 and can never be otherwise on this
  hardware. They remain worth running — normalised by the ceiling, `obs/ceiling`
  was consistent to 0.008 across the confounded sweep — but they measure the
  medium *and* the cap, and must be reported that way.
* The sweep's honest range is therefore 2.5–10 Hz uncapped, or 2.5–25 Hz with two
  points normalised. Not the clean airtime sweep intended.

Fourth bug from one sweep, and the second in a validator I wrote: the guard I added
to prevent the confound advised setting the interval to a value the radio refuses.
It now names the floor and says the ceiling cannot be lifted. It also fired on
`wifi` nodes, which have no radio.

---

---

## Appendix: superseded plans and closed items

Kept because they record what was believed before the hardware answered, and what
has since been finished. None of it is current — `PLATFORM.md` is.

### A7. Measurement plan — decide the dongle question with data

Do not decide this by reading datasheets, including this document. Instrument and
measure. Prerequisite: **sequence numbers in the payload** (see C4).

Matrix — for each cell, record BLE per-link packet delivery ratio, BLE one-way
delay (chrony gives us ~30–100 µs, so this is measurable), and Wi-Fi RTT/jitter:

| | Wi-Fi idle | Wi-Fi light (UDP agent) | Wi-Fi loaded (`iperf3`) |
|---|---|---|---|
| BLE scan off | — | | |
| BLE scan 50% duty | | | |
| BLE scan 100% duty | | | |

Run each row twice: **onboard CYW43455 only** (the chosen configuration) vs.
**TP-Link + onboard-BLE** split (the retired one, as a baseline). Then sweep
`btc_params` if the onboard case underperforms.

Control for A3.1's three mechanisms separately or the result is uninterpretable:
fix the WLAN channel at 11 throughout (removes mechanism 2), log per-node Wi-Fi TX
airtime alongside BLE delivery ratio (isolates mechanism 1), and only then sweep
`btc_params` (mechanism 3). Also record BLE scan window/interval per row — once A5
lands this is a controlled variable rather than an unknown, which is what makes the
table interpretable at all (A3.3).

The output is a coexistence characterization table for the Pi 4 — itself a
publishable platform contribution.

**Note the changed purpose.** The dongle question is now *decided* (§10): onboard
radio for both. A7 is therefore no longer a decision input but **validation and
tuning** — quantify what the integrated configuration actually delivers, and give
`btc_params` sweeps a metric to optimize. The retired dongle configuration stays
in the matrix as a baseline, and stays documented as a fallback in case the
numbers come back bad.

**Migration back to onboard Wi-Fi** — this is now the forward path, not a
rollback. Per Pi:

1. Remove `dtoverlay=disable-wifi` from `/boot/firmware/config.txt`; reboot.
2. Reconnect `wlan0` to the LAN via `nmcli`; unplug the TP-Link dongle.
3. `iw dev wlan0 set power_save off` (make it persistent — a NetworkManager
   `wifi.powersave=2` connection setting, not a one-shot command).
4. Verify: `iw dev` shows `wlan0` only; `ethtool -i wlan0 | grep driver` shows
   `brcmfmac`; `hciconfig -a` still shows `hci0`.
5. Update IPs in the topology manifest — `wlan0`'s address replaces the dongle's.

README §3 (the dongle procedure) should be rewritten as an appendix: *"if
integrated coexistence proves insufficient, here is the split-radio fallback,"*
with A7's numbers as the reason it wasn't needed. Keep it — it's a real result
about the hardware, and other people on Pi 4s will hit the same question.

Side effect: with only `wlan0` present, known bug §7.2 (non-deterministic
interface selection) stops biting — but still fix it, and pin to `wlan0`
explicitly, since the intent should be in the code rather than in the absence of
a second interface.

---

### 8c. Road to a first run: 3 Pis, 9 agents

Three Raspberry Pis, each hosting `ble` + `wifi` + `bridge`. The smallest
configuration that exercises every path the platform compares, and the target that
orders the remaining work.

Everything in §8a/§8b is about the *node*. This section is about the fact that
nothing yet **starts** one.

### Status

**Items 1-5 are done.** `bash test/check_all.sh` now ends with a `fleet end to end`
stage that drives the whole host-side path -- hub, control plane, nine
`AgentService` instances, controllers and relay, logs, fetch, files on disk -- on
one machine with faked radios. Nine of nine nodes, 360 samples, one epoch.

What shipped:

| | |
|---|---|
| `vertex/agent/__main__.py` | `python3 -m vertex.agent --type {ble,wifi,bridge}` |
| `vertex/serial/link.py` | `SerialLink`: port, reader thread, request/reply, **STATE path** |
| `vertex/hub/runner.py` | `ExperimentRunner`: configure, trigger, wait, stop, collect |
| `vertex/hub/__main__.py` | `python3 -m vertex.hub {status,run} <manifest>` |
| `vertex/transports/multi.py` | `MultiTransport`: the bridge's two media |
| `experiments/n9-ring.yaml` | the bring-up manifest |
| `test/hub/check_fleet.py` | the end-to-end check |

Four defects were found doing it, three of which would have cost a bench session:

**A `bridge` had only one medium.** `Agent` holds one `Transport` and the factory
gave `bridge` a `BleTransport`, so a `wifi` and a `ble` agent -- which share no
medium -- had no path between them at all. `make_manifests.py`'s own comment says
bridges "join the BLE and Wi-Fi subnets" and `n30-clusters` gives node 21 both a
BLE and a Wi-Fi neighbour, so the intent was always both. Fixed at the Transport
seam (`MultiTransport`) rather than by teaching `Agent` about lists, so `Agent` is
unchanged. A bridge now transmits every packet twice, which is intended and is not
airtime-comparable with a single-medium agent -- that belongs in the analysis.

**Three of the four n30 manifests declared links that cannot carry a packet.** The
generators walk ids numerically, so `ring`/`line` over 1..30 puts a `ble` agent
next to a `wifi` agent at the 10/11 boundary. The validator now rejects that
outright -- it is an error, not a warning, because the run *looks* healthy: both
agents start, both publish, and the link reports 0% delivery, indistinguishable
from a radio fault. The fix is `BAND_ORDER`, a relabelling that puts five bridges
at each boundary; λ₂ is bit-identical before and after (0.0219, 0.2166), because a
relabelled ring is the same graph. `n30-clusters` was always clean -- its edges
were hand-declared with bridges at the boundaries.

The validator also now warns on **intra-host links**: a local UDP broadcast is
delivered by the kernel and never reaches the radio, and two BLE radios centimetres
apart are not a link under test, so such a link reports ~100% delivery and ~0 delay
and flatters any average it lands in. `n9-ring`'s ordering avoids them entirely,
which is why its edges are written out rather than generated.

**`AgentService.shutdown()` hung whenever a hub was connected.** From Python 3.12
`Server.wait_closed()` waits for every live handler, and the hub holds its
connections open for the whole experiment. So a SIGTERM to an agent mid-run would
have needed a SIGKILL behind it. `ControlServer` now tracks its writers and closes
them, with a bounded drain.

**The epoch had nowhere to come from.** An agent's `WallClock` was fixed at launch,
so nine agents meant nine origins and a one-way delay measuring process launch
order. The epoch is per-run and the hub owns it, so it now travels with `start` --
the same argument as the nRF's seed travelling in CONTROL rather than ALGORITHM --
and lands in `RunMeta.environment.epoch_unix_s`. The check asserts all nine agree.

### Blocking — nothing runs without these

**1. There is no agent process.** `AgentService` is complete and has no caller: no
`__main__.py`, no console script, nothing anywhere constructs it. Needs to pick a
`node_type`, resolve `host_ip` with `resolve_local_ip()`, open the serial port when
the type is `ble`, and serve until signalled.

**2. There is no production serial link.** `vertex/serial/` is codec only —
`proto.py` and nothing else. `BleRelay` needs an object with
`request(type, payload, timeout)`. `test/common/peer.py` has a good one (pyserial,
reader thread, the `_awaiting` gate that fixed the TXAT race) but it is
test-harness code, and it has no STATE path.

**3. STATE frames never reach the relay.** `BleRelay.handle_frame` has **no caller
anywhere**, and `on_state` appears only in the docstring that claims `peer.py`
satisfies the contract — which it does not: `peer.py` routes ACK/ERR/PONG/STATS/
TXAT and ADV_REPORT, and STATE is not among them. A `ble` agent would therefore
configure the nRF, start it, and log **zero samples**.

Worth recording how this survived: the end-to-end check that exercised relay mode
called `handle_frame` directly. The harness supplied the wiring it was meant to be
testing, so the gap was invisible from a passing run. Items 2 and 3 are one change,
because they are one path.

**4. There is no hub.** `ControlClient` is complete and strictly single-node;
nothing instantiates it. The fan-out is small because the pieces exist:
`assignments_for(manifest, run_index)` returns exactly the `configure` payload
keyed by node id, and `CONTROL_PORTS` resolves host and type to a port.
`HUB_PORT = 3000` is declared and unused.

**5. There is no 9-node manifest.** All four in `experiments/` are 30 nodes over
ten hardcoded addresses; `tools/make_manifests.py` pins `HOSTS` and the 1/11/21
band offsets.

### The largest unknown is not on that list

**The coordination firmware has never been through `west build`.**
`test/check_all.sh` is `gcc -fsyntax-only` against stub headers plus host-linked
cross-checks. That catches undeclared identifiers, offset drift and codec
disagreement; it cannot catch a Kconfig conflict, a devicetree problem or a link
error. Items 1-4 are all host-side, so building and flashing one board is the
cheapest de-risking available and it parallelises with all of them.

One Kconfig problem has already been found by reading rather than building:
`firmware/nordic/prj.conf` was missing `CONFIG_UART_0_NRF_HW_ASYNC` and its timer,
which both loopback peers have carried since direction A. Without hardware byte
counting the UARTE driver cannot know how many bytes sit in the DMA buffer before
it fills, so the RX idle timeout never fires and a short frame is never delivered.
Every configuration frame is short: the board would have accepted nothing and
looked like a dead cable. The same file also had `CONFIG_LOG=n`, compiling every
`LOG_INF` in the firmware to nothing, and left `CONFIG_UART_CONSOLE` at its default
of on — writing console text into the binary stream on uart0.

### Wanted, not blocking

* `vertex/analysis/` is `units.py` only — no loaders, metrics or multi-node
  aggregation. The run reader is `runlog.read_run_file`.
* No systemd units and no provisioning. `scripts/radio_check.sh` is single-node
  read-only diagnosis.
* README is two lines and describes no procedure.
* §8b.E: `pyproject.toml` `testpaths` still names two deleted directories, so
  `pytest` collects nothing and says so quietly.

### Closed

**1. `ble` relay mode** — `vertex/agent/relay.py` plus `AgentService.is_relay`. No
local controller and no Transport for a `ble` agent: the nRF owns the law, the
radio and the timing. `assignment_to_frames()` is the single place engineering
units become scaled int32, and reports are logged `units="scaled_int"` rather than
converted, with `vertex.analysis.units` normalising on read.

**2. The STATE frame** — `[t_us:8][state:4][vstate:4][vartheta:4][counter:4][n:1]`
then per neighbour `[vstate:4][flags:1]`, flags bit0 = enabled, bit1 = fresh.
Scaled int32 throughout. `fresh` is cleared on every report, so it means "heard
since the last report" and per-link delivery ratio for a `ble` agent stays
derivable from its log alone — which matters because the nRF's own advertisements
are still v0 and carry no sequence number.

**3. Binary serial protocol in the firmware** — replaces `serial.c`'s ASCII
`n`/`a`/`p`/`t`: same four jobs, framed with a CRC, no 64-byte split, no 50 ms
inter-command gap. Split three ways, matching the loopback peers:

* `proto.h/.c` — the **envelope** only: SOF, type, length, CRC. Nothing about what
  a payload means, because the envelope is shared by every message on the link.
* `agent.h/.c` — the **payload formats** (documented in the header, beside the
  decoder) and `struct agent`. No Zephyr dependency, so it compiles and runs on
  the host — which is what lets the cross-check drive the real decoder.
* `control.c` — the **dispatcher**: pick a handler, turn its result into an ACK or
  an ERR, answer a PING. The agent arrives through uart_link's `ctx`, so the
  dispatcher holds no state of its own.

RADIO decodes through `agent_parse_radio()` but is applied by whoever owns the
radio — the observer on the nRF, `ble_scan`/`ble_adv` on the peers. One validator,
three effects; previously each of the three had its own partial bounds check.

`test/common/check_proto_layout.py` checks that the host encodes what each
firmware decodes, by length *and* by field offset, across **all three** firmwares.
Both failure modes are demonstrated by reintroducing them: a one-sided
`PROTO_CONTROL_LEN` edit, and an offset that drifts while the length still
matches. Checking only one firmware is how `PROTO_CONTROL_LEN` came to be 5 on
the host and on the nRF while both peers stayed at 1.

**4. The firmware divergences** — closed and now guarded by
`test/crossval/compare.py`, which links the real `agent.c` + `coordination_task.c`
+ `prng.c` against the Python controller and reports the residual. Configuration
enters as encoded frames built by the host's own encoders, so the decoders are on
the checked path too. Currently max 4.5e-5 over 400 steps,
which is the float32 floor. See `docs/FIRMWARE_DIVERGENCE.md` for the measured
size of each old divergence. `rand()` is gone in favour of PCG32, mirrored in
`vertex/pcg32.py`, seeded per node per run from the CONTROL frame.

**5. Direction B harness** — ran. Delivery moved 90.4% -> 14.6% across the scan
window, 75.8 points, which is the parameter BlueZ never exposed. See §8a.

**6. `transports/ble.py`** — `BleTransport`, the `bridge` agent's path. One HCI
user-channel socket carries commands, completions and advertising reports
together, so the transport installs a single reader (`_pump`) and routes command
completions to whoever awaits that opcode. `HciSocket.command()` blocks and
*discards* intervening reports, which is fine during setup and ruinous during a
run: `publish()` issues a command every control period and each one would eat the
reports queued behind it, indistinguishably from radio loss.
`test/transports/check_ble.py` covers AD framing, setup ordering, that pump
behaviour, self-filtering and v0 acceptance, against a datagram socketpair.

**7. Radio parameters in the manifest** — `RadioSpec` in the manifest, carried on
`AgentAssignment.radio`, into `RunMeta.environment["radio"]` with the requested
milliseconds, the programmed 0.625 ms units, the duty cycle, `radio_source`
(`manifest` or `launch-override`) and `applied_on` (`nrf52` / `pi-hci` / `none`).
Recorded for `wifi` agents too, so all three types in one experiment carry the
same environment block.

**8. Firmware, in order** — all five: binary protocol, STATE frame with the
`fresh` bit, settable scan parameters (`observer_set_scan_params`), the two
divergences plus a seeded PRNG, and `update_coordination()`/epsilon removed.

**9. The radio and wiring modules follow.** `common.h` / `broadcaster` /
`observer` / `main` refactored onto `struct agent`, with four defects fixed that
the restructure exposed rather than introduced:

* **The log snapshot was not a snapshot.** `coordination_params` held its
  neighbour arrays as **pointers**, so `memcpy(&log_data_copy, &coordination, ...)`
  copied the pointers: the snapshot aliased live memory and reported vstates could
  change under `report_state()` mid-frame. `struct agent` holds them inline.
* **STATE was reported on the wrong condition.** Only when a queue message arrived
  *and* every neighbour had been heard at least once. So a node with one silent
  neighbour reported nothing for a whole run, and the surviving timestamps were
  event-driven rather than periodic — making "nothing arrived" and "nothing was
  sent" indistinguishable in the very log the delivery ratio comes from. Now every
  `clock` tick, unconditionally; the `fresh` bits already say which links
  delivered, but only if the window closes on schedule.
* **The scan callback wrote the agent with no lock**, from the Bluetooth RX
  thread, while main's two threads used `coordination_mutex` on the same fields.
  The observer is now read-only on the agent: availability travels as a cumulative
  `heard` bitmask in the queue message and `main.c` applies it under the mutex it
  already holds. `report.c`'s `fresh_mask` is `atomic_t` for the same reason —
  marked from one thread, read-and-cleared from another.
* **The advertising interval was unreachable.** `broadcaster.h` defined
  `MIN_ADV_INTERVAL`/`MAX_ADV_INTERVAL` as 1280 ms; both were unused and the code
  ran Zephyr's 100–150 ms default. The RADIO frame's `adv_min`/`adv_max` were
  validated and then dropped on the floor. Now `broadcaster_set_adv_params()`
  stores them and `broadcaster_init()` applies them, so half of item A closes: the
  advertising interval reaches the nRF. `channel_map` still does not.

Also: `custom_data_type` is the manufacturer AD element's value, memcpy'd onto the
air, so its memory layout *is* the wire format — previously unpacked and
unasserted, working only because the field order happens to need no padding on
this target. Now packed, with a size assertion whose diagnostic names the problem,
and `test/crossval/check_v0.py` compiles the real header and hands the real
struct's bytes to the host's `decode_any()`.

### 4. Workstream B — Python Migration

**Decision: yes.** Reasons, in order of weight:

1. **Radio access** (A4, A5). Raw HCI sockets, netlink, `pyroute2`, `bumble`,
   `dbus-fast` — all stdlib or one pip install. This alone justifies it.
2. **One language for controller, simulator, and analysis.** The control law
   currently exists twice (`raspberry/algo.js` and `nordic/src/coordination_task.c`)
   and analysis lives separately in `scripts/organize_data.py`. In Python the same
   `Controller.step()` runs on hardware, in the fast-forward simulator, and in the
   notebook that produces the figures.
3. **Ecosystem:** `networkx` (topology generation *and validation* — `net.js`
   notes the average-consensus law assumes strongly connected + balanced, and
   nothing checks it), `numpy`/`scipy`, `python-control`, `cvxpy` for distributed
   optimization agents, `matplotlib` for the SVGs already in `docs/plots/`.
4. **asyncio maps 1:1** onto the current event-driven structure.

**Honest caveat:** Python GC adds jitter. At `dt = 200 ms` this is three orders
of magnitude of headroom — irrelevant. If we later want `dt ≤ 5 ms` on the Pi:
`uvloop`, preallocate arrays, and/or push the fast loop into the nRF (which is
already what the BLE agent does).

### Stack mapping

| Now | Python |
|---|---|
| `express` + `socket.io` | **FastAPI** + `python-socketio` |
| `axios` | raw UDP (preferred, see A3) / `httpx` |
| `serialport` | `pyserial-asyncio` |
| `node-ble` + `bleadv.sh` | raw HCI socket / `bumble` |
| `child_process.fork` IPC | `asyncio` + `multiprocessing`, or separate processes over a unix socket |
| `pm2` / `ecosystem.config.js` | templated `systemd` units (`vertex-agent@ble.service`) |
| `seedrandom` | `numpy.random.default_rng(seed)` |

### Phased plan — never leaves a broken testbed

1. **Config first.** Replace the `TOPOLOGY` block in `net.js` (lines ~178–365,
   plus the large commented-out topology library) with YAML manifests + `pydantic`
   models. Both codebases read the same file. Zero risk.
2. **Port the controller with golden fixtures.** `algo.js` → numpy `Controller`,
   asserted to bit-parity against existing JSON logs, step for step. This is the
   safety net for everything after.
3. **Port the agent** (`edge.js`) with asyncio + a `Transport` ABC. Python and JS
   agents can run in the *same* experiment — they only need to agree on the wire
   format — so migrate one node at a time.
4. **Port the hub** with FastAPI + `python-socketio`. That library is
   protocol-compatible with socket.io v4, so **`raspberry/public/*.html` needs
   zero changes.**
5. **Then** replace `bleadv.sh` with the HCI layer (A5).
6. Firmware stays C. Only the parameter schema is shared.

---
