# Clock model

## Why this document exists

**Six different things in this project are called "clock".** They are independent,
they interact, and confusing any two of them produces conclusions that look sound.
Naming them apart is most of what this document does.

| # | What | Where it appears | What it actually is |
|---|---|---|---|
| 1 | `clock` | `net.js` topology rows, `coordination_params.clock` | **A period, not a clock.** How often a node exchanges state. Renamed `publish_period_s` in the Python port for exactly this reason |
| 2 | Scheduling clock | Zephyr `k_timer`, `setTimeout`, `WallClock.now_s()` | Monotonic time driving the control and publish loops. Must never step backwards |
| 3 | `time0` / experiment epoch | `coordination_params.time0`, `edge.js`, `StatePacket.tx_time_us` | Shared origin so timestamps from different nodes are comparable |
| 4 | Timestamping clock | chrony-synchronised wall clock, `WallClock.now_us()` | Measures one-way delay and aligns trajectories across nodes. **Infrastructure** |
| 5 | Virtual clock | `VirtualClock` in the simulator | Simulated time that jumps between scheduled wake-ups |
| 6 | **The modelled clock** | `state`, `vstate`, `vartheta` in the event-triggered variant | **The object of study.** The thing the algorithm synchronises |

Numbers 2–5 are how the testbed *runs*. Number 6 is what the testbed is *for*.
Number 1 is a naming accident.

---

## The modelled clock (#6)

In the clock interpretation, each agent's three state variables read as:

| Symbol | Code | Meaning |
|---|---|---|
| *x* | `state` | The **local clock** reading — what this node's own oscillator says |
| *z* | `vstate` | The **virtual clock** — the corrected, agreed-upon time. The only quantity broadcast |
| *θ* | `vartheta` | An **adaptive gain** that grows while the local clock lags its own virtual clock |
| *σ = x − z* | `sigma` | The correction still outstanding on this node |
| *ν(t)* | `disturbance` | Oscillator imperfection — see below |

The update, per control period:

```
v_i   = Σ over enabled neighbours j of  −sign(z − z_j)·√|z − z_j|     coupling
σ     = x − z                                                        local error
u     = α·v_i − θ·sign(σ)                                            correction
x    ← x + u + ν(t)·dt                                               local clock
z    ← z + α·v_i                                                     virtual clock
θ    ← θ + η   while |σ| > δ                                          adaptation
```

Two structural features follow from *x* being a clock rather than an arbitrary state:

**Monotonicity.** A clock reading cannot decrease. The event-triggered variant
therefore clamps all three integrators at zero (`max(·, 0)`), which is why its
docstring says it models a clock and warns against reusing it for a signed state.

**Hysteresis instead of a dead-band.** The discrete variant adapts whenever
|σ| > δ. The clock variant uses two thresholds — engage above `epsilon_on`, release
only below `epsilon_off` — so a clock hovering near its threshold does not switch
its correction on and off every period. The gap between the two *is* the mechanism;
see the divergence note below, because the firmware has them inverted.

### The disturbance models oscillator imperfection

The three components of ν(t) are not arbitrary. Under the clock reading each maps
onto a known property of a real crystal oscillator:

| Component | Parameter | Physical meaning |
|---|---|---|
| Constant | `beta` | **Frequency offset** — this crystal runs fast or slow by a fixed fraction |
| Sinusoid | `sine_amplitude`, `sine_frequency_hz` | **Temperature-driven drift** — the diurnal cycle every oscillator exhibits |
| Uniform noise | `noise_amplitude`, `noise_offset` | **Jitter** — short-term random variation |

`noise_offset` shifts the *uniform draw*, so 0.5 centres the jitter on zero. A
different value biases it, which is a second way to express a frequency offset.

This mapping is the reason the sinusoid matters more here than it would in a
generic disturbance: it is the physically dominant slow term. Which makes the
firmware's time-base defect (below) specifically a clock-model defect.

---

## The separation that matters most: #4 versus #6

**chrony synchronises the real Pi clocks. The algorithm synchronises modelled
clocks. These are different clocks and they do not touch.**

- chrony (#4) is *infrastructure*. It exists so that a timestamp written by node 3
  means the same thing as one written by node 7 — which is what makes one-way delay
  measurable and lets trajectories from different nodes be plotted on one axis.
- The algorithm (#6) operates on `state` and `vstate`, which are numbers in a
  controller. Nothing in the current system writes them back to the operating
  system's clock.

Keeping this straight prevents two mistakes:

1. **Thinking chrony is doing the algorithm's job.** It is not. Disabling chrony
   would degrade *measurement*, not coordination. Convergence of *z* across nodes
   is unaffected by whether the Pis agree on wall-clock time.
2. **The circularity trap.** If this platform is ever used to study *real* clock
   synchronisation — the algorithm actually correcting each Pi's clock — then
   chrony-synchronised timestamps can no longer be used to evaluate it. That would
   be using a synchronisation service to measure a synchronisation algorithm:
   assuming what you set out to prove. Evaluating that experiment needs a reference
   outside both, such as a GPS PPS input or a shared hardware trigger line into
   every board. **This constraint is cheap to satisfy in advance and expensive to
   discover after collecting data.**

---

## Which variant actually runs

This is the surprising part, and worth stating plainly:

| Implementation | Discrete variant | Clock variant |
|---|---|---|
| Firmware (`nordic/src/main.c`) | `discrete_step` — **called** | `update_coordination` — defined, never called |
| Former host code (`raspberry/edge.js`) | `discrete_step` — **called** | `update` — defined, never called |
| Python port | `step` — **called** | `step_continuous` — available, not used by any manifest |

**No current experiment is a clock-synchronisation experiment.** Every run to date
uses the general coordination form, in which *x*, *z* and *θ* are abstract states
with no monotonicity constraint and a single dead-band. The clock model is a
supported mode that has not been exercised.

That is not a problem — but it does mean the clock-modelling code path has never
been validated on hardware, and it is where the divergences below are hiding.

---

## Time in the platform (#2, #3, #5)

Two notions of software time, deliberately separated in `vertex/clock.py`:

```
now_s()   monotonic       scheduling      never steps backwards, so an NTP
                                          correction cannot skip or double a
                                          control period
now_us()  epoch-relative  timestamping    shared origin across nodes, so a
                                          neighbour's timestamp is comparable
```

Using one for both breaks one of them: a monotonic timestamp is meaningless to a
neighbour (no common origin), and a wall-clock scheduler can step backwards.

`tx_time_us` in the wire format is **microseconds since the experiment epoch**, not
since the Unix epoch — absolute Unix microseconds passed 2⁴⁸ in 1978, and the field
is 6 bytes. A per-run epoch leaves ~8.9 years of headroom.

`VirtualClock` (#5) substitutes for both in simulation, jumping between scheduled
wake-ups so 30 agents run 120 modelled seconds in about a second of wall time. The
agent code is identical in both cases; only the clock is swapped.

### One measurement the platform gets for free

Because #4 is synchronised and `tx_time_us` and `rx_time_us` share an epoch,
`LinkMonitor` reports true **one-way** delay rather than round-trip. Negative values
are recorded rather than clamped: a negative minimum is a direct measurement of
residual clock skew between two nodes, and the cheapest available check that chrony
is actually working.

---

## Divergences that specifically affect the clock model

All four are in the firmware, and the last three sit in the clock-modelling path —
so they are **latent**, not currently active. See `FIRMWARE_DIVERGENCE.md` for the
first two in full.

1. **Disturbance time base scaled by 1e-6 where it needs 1e-3.** The sinusoid's
   time argument advances 1000× too slowly. Under the clock reading this is the
   temperature-drift term, i.e. the physically dominant slow component — so BLE
   agents effectively experience no diurnal drift at all, while host-side agents do.
2. **Fixed-point truncation in the integrator.** The firmware stores each step back
   into `int32_t`, re-injecting a one-directional quantisation error every period.
   For a clock, a systematic bias toward zero is a systematic *rate* error.
3. **The clock variant does not clamp at zero in C.** `update_coordination` omits
   the `max(·, 0)` the host implementation applies, so on the firmware a modelled
   clock can run backwards — which is the one thing the clock interpretation forbids.
4. **The hysteresis band is inverted in C.** `coordination_task.c` sets
   `epsilonON = 0.01` and `epsilonOFF = 0.05`; the host sets the reverse. Both C
   comments claim they match the host. With ON below OFF the latch engages above
   0.01 and releases below 0.05, so any error inside that band engages and releases
   on alternate periods: chattering, which is exactly what hysteresis prevents.
   The Python port refuses an inverted band outright, at both the manifest and the
   controller boundary.

---

## Checklist for a clock-synchronisation experiment

If the clock model is to be run for real:

- [ ] Fix firmware divergences 1–4 above, and re-derive whether existing data is comparable.
- [ ] Choose the variant deliberately per manifest; the choice is currently implicit in which function the code calls.
- [ ] Confirm `epsilon_on > epsilon_off` on every implementation, not just the host.
- [ ] Decide the monotonicity contract and apply it consistently across C and Python.
- [ ] **Decide the measurement reference before collecting anything.** If the algorithm corrects real clocks, chrony cannot be the reference — see the circularity trap above.
- [ ] Seed the firmware's `rand()`, which is currently unseeded, so oscillator jitter is reproducible.
