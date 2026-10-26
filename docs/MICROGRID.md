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

## 5. Declared departures from the source benchmark

The baseline is the prescribed-time controller of Liu and Che. Three values
differ from the source and are declared rather than inherited:

| Value | Source | Here | Why |
|---|---|---|---|
| `beta_c` | 400 | 2 | At `h = 25` ms the Euler update of the error dynamics has multiplier `1 - h beta_c/(T_c - t)`, already `-1` at `k = 0`. Substepping an internal integrator does not fix it while the physical command is held for `h`. |
| `Phi_i` | `3e-3` | `3.0` | The post-deadline parameter rate scales with `Phi a_zeta^2`; at `a_zeta ~ 1e-3` the source value gives a time constant far longer than a trial, leaving the estimate inert. |
| `alpha_0` | derived 0.1 | 1.0 | The derived value misses the calibration band. |

The gain is additionally saturated below `T_c - t = 2h`, so that
`0 < h lambda <= 1`. That is a check on the homogeneous term, not a stability
result for the sampled adaptive loop.

None of these drives the reported negative result: `C_LC0` fails under
persistent uncertainty because zero input after `T_c` maintains the target
only if `Psi_i(X_i^d, t) S_i = 0`, and `C_LC+` — same regularization, loop kept
closed — converges.
