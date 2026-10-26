# The five-DG microgrid system

The platform's own system under exercise is the scalar `finite_time_adaptive`
law; see PLATFORM.md §2 item 4. The five-DG microgrid benchmark is a **second
system that borrows the platform**, added to answer one reviewer. This file
holds its design decisions, so they live in `docs/` rather than in the comment
blocks of the code that implements them.

---

## 1. Why the assignment carries an allow-list

`AgentAssignment.microgrid` is one dict that crosses the control plane, and it
carries two kinds of value:

- values the controller is **built from** — the `plant`, `virtual` and
  `interface` blocks;
- values the run must **record about itself** — `T_c` is the example. Only the
  LC interface takes it as a parameter, but every arm is judged against it, so
  a log that does not carry it cannot say what deadline the run was held to.

`_microgrid_params` in `vertex/agent/assignment.py` therefore selects keys by
what `ControllerParams` accepts, not by excluding the ones known not to be
parameters.

The failure mode a deny-list produces is worth stating because it is silent
from the hub's side. Under a deny-list, every new value of the second kind
reaches `ControllerParams(**kwargs)` on the agent and raises `TypeError`
inside `configure`. An agent running older code than the hub then refuses the
whole run, while nothing on the agent has changed. An allow-list makes the
unrecognised key inert instead.

This does not rescue an agent already deployed with the old code — nothing
can, short of redeploying it — but it bounds the blast radius of the next
field added.

## 2. Why unknown log columns are an error

`vertex/analysis/units.py` converts between engineering units and the scaled
integers on the wire, and `STATE_COLUMNS` lists what is subject to scaling.
An unrecognised column raises `UnknownColumn` rather than passing through
unconverted.

The alternative fails quietly in a way that survives every downstream check: a
column missing from the list leaves that node's trajectory in scaled integers
while its metadata claims engineering units. The run loads, the plots draw,
and the affected nodes sit a factor of `SCALE_FACTOR` above the rest. Nothing
raises, so the list being incomplete must itself be the error.

## 3. Why the figure layout is resolved in one place

`plotting-tools/dg5_paths.py` is the only module that decides where a five-DG
figure goes:

```
render/dg5/<set>/arms/      figures that put several arms on one axes
render/dg5/<set>/<run>/     figures that draw a single run
```

`<set>` is the run directory — `per-mixed`, `rep-bridge5` — so a figure carries
its uncertainty profile and its agent assignment in its path, and two sets
drawn in one session cannot overwrite each other. `<run>` is trial and arm
only, `t0-LC0`: the set directory has already said `mixed` and `per`.

Four scripts write into this tree. Resolving the destination in one module is
what keeps them from disagreeing, and what keeps any one of them from
hardcoding a path that is right for itself and wrong for the set.

## 4. Why control effort is reconstructed, not logged

Three of the five agents report the applied command; the two whose law runs on
an nRF behind a relay report their states and the adaptive quantity, and the
command is dropped before it reaches the host.

`vertex/analysis/effort.py` rebuilds it. The command is a deterministic
function of quantities that *are* logged, and the reconstruction is local —
the virtual layer's feedforward is a forward difference of the node's own
virtual state, so no neighbour is needed:

```
z+ = (1 - theta) z + theta w / D,  theta = h a_o D / (1 + h a_o D)
  =>  z+ - z = -h a_o eta / (1 + h a_o D) = h g
```

The index conventions were not assumed. They were read off the nodes that log
both the inputs and the answer, by trying every shift in `[-2, 2]` and keeping
the one that reproduces the logged `sigma`, `g` and `mu` exactly. `verify()`
re-runs that check on any run and should be trusted exactly as far as it keeps
passing.

Two things to know when reading those numbers:

- The law's own clock is `t = (k+1) h`, not the host timestamp. The timestamp
  drifts up to 28 ms from the nominal grid, and the LC gain
  `lambda = beta_c / max(T_c - t, delta_c)` turns that into a large error near
  `T_c`. The adaptive interface has no time-dependent term and does not
  notice, which is why this has to be checked on LC.
- A relayed node runs its law in float32 on the nRF; this runs in float64.
  What it reconstructs is the float64 evaluation of the same law on the same
  logged states, which is the quantity the other three nodes report anyway.

## 5. The virtual layer buys convergence with packets, not with gain

The discrete virtual update is a convex combination,

```
z_i+ = (1 - theta) z_i + theta (w_i / D_i),
theta = h a_o,i D_i / (1 + h a_o,i D_i) in [0, 1)
```

and `w_i / D_i` is itself a convex combination of the received values and, at
the pinned node, the reference. A coordinate interval containing them is
therefore preserved for any nonnegative gain. That is what makes the update
safe at a gain the adaptation is free to grow without bound; it is not a proof
of prescribed-time convergence.

The limit is the point. As the gain grows `theta` tends to one and the update
becomes `z_i <- w_i / D_i`, a Jacobi sweep toward the neighbour average. With
the snapshot held between packets the layer performs **one such sweep per
exchange however large the gain**, so the contraction per packet is bounded by
the spectral radius of `D^-1 A` on the graph — `0.9336` on the benchmark's
pinned ring.

Raising the prescribed-time gain therefore cannot make the layer converge in
fewer exchanges. The exchange count is what sets the deadline, which is why
`h_v` is bounded rather than free. See `reviewers-exp/EXCHANGE-LIMIT.md` for
the count.

## 6. Reading the adaptive-interface results

Two properties of `C_AA` and `C_DZ` govern how their numbers must be read.

**The correction is clipped.** Once `vartheta_i >= ||sigma_i||_inf / h`, the
term `omega_il = sgn(sigma_il) min(vartheta_i, |sigma_il| / h)` equals
`sigma_i / h` however much further the gain grows. A drifting gain therefore
need not produce any change in the applied input at all. Any claim that the
dead zone reduces control effort has to come from the measured inputs, not
from the gain trajectory — and on this benchmark it does not: `C_AA` and
`C_DZ` differ by a factor of three in terminal gain and are indistinguishable
in `J_mu`, peak command and total variation.

**`gamma` is an increment per update, not a rate.** An Euler realization of a
continuous rate uses `gamma = h gamma_ct`. The two numbers must not be
identified: reproducing the same continuous behaviour at a different `h`
requires a different `gamma`.

## 7. Why raising `Phi_i` is not enough on its own

The post-deadline parameter rate of the LC interface scales as
`Phi a_zeta^2 (P~^s^2 + Q~^s^2) / lambda_ch`, which is why the source value is
inert here (§8). Raising it alone does not by itself produce identification. While `sigma`
is large, `Gamma int(R^T sigma)` *deposits* an estimate wherever the transient
leaves it, and the post-deadline rate is too slow to correct it. The
`adapt_band` gate exists for that: it holds the update shut until
`||sigma_i||_1` falls below it, so the transient contributes nothing and what
follows is identification rather than deposition.

`adapt_band` and `adapt_after` default to off, which is the source's
behaviour, and the reported runs use them off.

## 8. Declared departures from the source benchmark

The baseline is the prescribed-time controller of Liu and Che. Three values
differ from the source and are declared rather than inherited:

| Value | Source | Here | Why |
|---|---|---|---|
| `beta_c` | 400 | 2 | At `h = 25` ms the Euler update of the error dynamics has multiplier `1 - h beta_c/(T_c - t)`, already `-1` at `k = 0`. Substepping an internal integrator does not fix it while the physical command is held for `h`. |
| `Phi_i` | `3e-3` | `3.0` | The source value leaves the estimate inert at this uncertainty scale; see §7. |
| `alpha_0` | derived 0.1 | 1.0 | The derived value misses the calibration band. |

The gain is additionally saturated below `T_c - t = 2h`, so that
`0 < h lambda <= 1`. That is a check on the homogeneous term, not a stability
result for the sampled adaptive loop.

None of these drives the reported negative result: `C_LC0` fails under
persistent uncertainty because zero input after `T_c` maintains the target
only if `Psi_i(X_i^d, t) S_i = 0`, and `C_LC+` — same regularization, loop kept
closed — converges.
