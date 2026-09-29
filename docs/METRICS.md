# Table metrics: how each is computed, and where the headroom is

Computed by `tools/table_metrics.py`, tabulated per agent by
`tools/per_agent_table.py`, over `runs/n30-regimeB`.

## The steady-state window

```
t_0 = max( T_c , max_i T_conv,i )        t_1 = end of run
```

`T_c` is the first instant the virtual states fall inside a band,
`max_i z_i - min_i z_i <= delta`. It is a band rather than exact agreement
because packet loss at the receiving stage leaves a residual disagreement that
does not vanish. `T_conv,i` is the settling of agent *i*'s physical state onto
its own virtual state. Both conditions are required: an agent tracking its own
reference says nothing about whether that reference is near `zbar` yet, and
opening the window on the physical condition alone integrates the whole
consensus transient, which measures convergence speed rather than residual
accuracy and reverses the ordering between a sparse and a dense graph.

Measured `t_0`: 162 s (G1, 40 Hz), 78 s (G2), 175 s (G3). For G2 the two
conditions coincide; for G1 and G3 the virtual condition is the binding one.

## Each metric

| metric | definition |
|---|---|
| `MSE(x_i)` | mean of `(x_i(k) - zbar)^2` over samples in `[t_0, t_1]`, with `zbar = mean_i z_i(0)` |
| `T_conv,i` | last sample at which `\|x_i - z_i\| > delta`, plus one step. A settling time, not a first crossing, so a brief re-excursion under disturbance cannot let an agent count as converged before it stayed converged |
| `rho_i` | samples are binned into publication windows of `T_pub/dt` samples; per neighbour, the fraction of windows containing at least one fresh arrival; averaged over the agent's incoming links |
| `P_complete(i)` | fraction of windows in which **all** `d` neighbours delivered. Counted jointly over the neighbourhood, not derived from `rho`, so `rho^d` remains a prediction to test rather than a restatement |
| `AoI` | per sample, `(k - index of last fresh arrival) * dt`; averaged over samples and over links. Staleness at the receiver, not end-to-end age: BLE links carry no usable one-way delay, so including transit would apply to UDP links only and the two columns would not be comparable |
| `dv_i` | at ~400 probes through the run, the coordination input computed from the agent's cached neighbour values against the one it would have computed from those neighbours' true values at the same instant, reconstructed from their own logs on the shared epoch. Reported as an RMS |

Binning by publication window rather than by sample is deliberate: the `rx_`
flag under-counts when two arrivals land inside one sampling period, whereas
"did at least one arrive in this window" does not depend on that.

### Known inconsistency

`MSE` is computed over `[t_0, t_1]`. `rho`, `P_complete`, `AoI` and `dv` are
currently computed over the **whole run**, transient included. In one table
that mixes two intervals, and it biases the network columns by arm: G1's
transient is 39% of its run against G2's 26%, so G1 carries proportionally more
pre-convergence data in every network column. Either restrict all six to
`[t_0, t_1]` or state the difference.

## Paired comparisons across matched cycles

The campaign runs one instance of each arm per cycle and rotates the order
within the cycle, so cycle *N* of G1 and cycle *N* of G2 ran minutes apart
under the same ambient conditions. A **paired** comparison takes the difference
within each cycle, `MSE_G2(c) - MSE_G1(c)`, rather than comparing two means.
Whatever the two arms share in a cycle -- access-point state, ambient 2.4 GHz
traffic, building occupancy -- cancels.

This matters because the run-to-run spread is not small: `MSE` is
`0.1087 +- 0.0617` on G2, a coefficient of variation of 57%. Comparing two
means with error bars that wide can hide an effect that pairing resolves,
because much of the scatter is common to both arms within a cycle rather than
independent between them.

## The consensus lands off target, reproducibly, and the sign depends on the graph

`MSE(x_i - zbar)` decomposes exactly into

    MSE = (offset)^2 + (spread among agents)

and at steady state the second term is ~4e-4 RMS while the first is ~0.3 for
the rings. The agents agree with each other roughly 700 times more closely than
the value they agree on sits from `zbar`. The metric named for deviation is
therefore almost entirely a measure of where the consensus landed, not of how
well the fleet coordinated.

Measured over 20 runs per arm:

| arm | signed offset | norm | sign |
|---|---|---|---|
| dring-40hz | **+0.2856** +- 0.065 | 1.565 | positive in **20/20** |
| ring4-40hz | **-0.3346** +- 0.102 | 1.833 | negative in **20/20** |
| clusters-40hz | +0.0110 +- 0.058 | 0.060 | 12/20, no consistent sign |

Three things follow.

**The two rings are not the same result.** They settle at almost equal distance
from `zbar`, one above and one below. A figure plotting the norm
`|| x - zbar*1 ||` cannot show that: both appear as a plateau near 1.6 to 1.8
and look identical. `consensus_offset_signed` plots the signed quantity on a
linear axis for exactly this reason, and the two separate immediately.

**The clustered graph is not better coordinated.** Its internal agreement,
~4e-4, is indistinguishable from the rings'. It simply has no systematic
displacement, so its offset averages to nothing and its MSE is 25x smaller.
Reporting MSE per arm without this note invites the reading that `G3`
coordinates an order of magnitude better than `G2`, which the disagreement
measurement contradicts.

**The bias itself is unexplained and worth explaining.** On a balanced graph
the average is conserved, so the consensus should land on `zbar`. A
displacement that is reproducible across 20 runs, and whose sign flips between
two nominally balanced topologies, means the invariant is being broken by
something structural. Asymmetric packet loss making the effective edge weights
unbalanced is the obvious candidate, and it has not been verified here. It is
the first thing a reviewer will ask about.

## Why the platform has headroom, by design

Delivery on a broadcast medium is governed by

```
k = T_pub / T_adv        rho = 1 - (1 - p)^k
```

where `k` is the number of advertising events that carry one published value
and `p` is the probability of capturing a single event. Advertising and
publication run on independent timers, so a value is radiated `k` times before
the controller overwrites it, and each transmission is an independent chance
for the neighbour to hear it. Both terms are manifest parameters
(`adv_interval_ms` / `adv_interval_max_ms`, and `publish_period_s`), so the
headroom below is reachable by configuration alone.

### Why a value fails to arrive, and why more events fix it

An advertising event puts one PDU on each of the three primary channels in
about 1.6 ms. A scanner listens on **one** channel per scan window and advances
at every interval, so exactly one of those three PDUs is ever a candidate: the
one on the channel the receiver happens to be parked on. The other two are
inaudible by construction. That is not a loss. The three-channel sweep is
redundancy against a scanner parked elsewhere, and it is completely effective
-- it guarantees one candidate per event, whatever the scanner is doing.

The candidate is then lost in one of two ways:

| | per event |
|---|---|
| it lands in the gap between scan windows, receiver asleep | 0.081 |
| it collides with another transmission on that channel | 0.279 |
| **captured** | **0.663** |

so per-event capture is `p = 0.663`, derived from the measured `rho = 0.743` at
`k = 1.25`. Contention dominates by more than three to one, on a channel where
about 45% of the decoded traffic belongs to equipment outside the experiment.

Neither mechanism has any memory from one event to the next. The scan phase
drifts against the advertising phase, and a collision is a fresh draw against
whatever else is transmitting. **That is why raising `k` works**: every extra
advertising event carrying the same published value is an independent redraw
against both mechanisms at once, and the failure probability compounds as
`(1-p)^k`. Raising `T_pub` buys those extra events for free, since the radio
keeps advertising at its own cadence while the controller publishes less often.

The effect on a degree-4 neighbourhood is steep, because `P_complete` compounds
the improvement a second time:

| `k` | `rho` | `P_complete(d=4)` |
|---|---|---|
| 1.00 | 0.663 | 0.193 |
| 1.25 (current, 40 Hz) | 0.743 | 0.305 |
| 2.00 (current, 25 Hz) | 0.886 | 0.617 |
| 2.50 | 0.934 | 0.761 |

Going from `k = 1.25` to `k = 2.00` doubles the complete-neighbourhood rate
while the per-link probability rises by only a fifth. That asymmetry is the
whole argument for treating `k` as the design variable rather than `rho`.

Taking `p = 0.663` from the measured `rho = 0.743` at 40 Hz:

| configuration | `k` | `rho` | `P_complete(d=4)` |
|---|---|---|---|
| 40 Hz, adv 100 ms (current) | 1.25 | 0.743 | 0.305 |
| 25 Hz, adv 100 ms (current) | 2.00 | 0.886 | 0.617 |
| 40 Hz, adv 50 ms | 2.50 | 0.934 | 0.761 |
| 40 Hz, adv 20 ms | 6.25 | 0.999 | 0.996 |

The model is checked against a configuration it was not fitted to: at 25 Hz it
predicts `rho = 0.886` and `P_complete = 0.617`, and the measured values are
`0.857` and `0.527`. It is therefore optimistic by roughly three points on
`rho`, and the extrapolations above should be read as upper bounds.

**Two ways to raise `k`, and the platform exposes both.** Shortening the
advertising interval raises it directly: 100 ms is the floor for legacy
advertising on this controller (measured, `scripts/adv_floor.py`), and BLE 5
extended advertising reaches 20 ms, which the table above puts at
`P_complete = 0.996` at degree 4. Lengthening the publication period raises it
too, and that path is already demonstrated rather than predicted: moving from
125 ms to 200 ms took `rho` from 0.743 to 0.857 and `P_complete(d=4)` from
0.305 to 0.527, at no cost in radio configuration.

**The same argument applies to Wi-Fi, with a different quantum.** There is no
advertising interval on the UDP path, but when the access point buffers
broadcast frames it releases them on beacon boundaries, giving an effective
delivery quantum of 250 to 300 ms. Against `T_pub = 125 ms` that yields
`rho = 0.52`; a publication period longer than the quantum puts at least one
release inside every window and returns `rho` to near unity. The mechanism is
the same -- the published value must be radiated at least once before it is
overwritten -- and so is the remedy.

**What the extrapolation does not include.** Halving the advertising interval
doubles the offered load, and `p` is itself set by contention: roughly 29% of
the measured per-link loss is attributable to collisions, on a channel where
45% of the decoded traffic is not ours. So `p` will fall as `T_adv` shortens,
and the gain will be less than the table implies. Raising `T_pub` has no such
cost, which makes it the cheaper of the two levers, paid for in control
bandwidth rather than in airtime.
