# Adding a controller family

The platform runs one system under exercise — the scalar `finite_time_adaptive`
law — and the five-DG microgrid benchmark borrows it (PLATFORM.md §2 item 4,
MICROGRID.md). This file is the seam between the two: what a controller family
declares, what the platform does with each declaration, and what a family may
not touch.

It exists so that the next family is an **addition** rather than a renovation.
It is written from the microgrid family as the worked example, which means it
also records the three places where the seam does not yet reach — those are the
edits a second system had to make in shared code, and they are the ones a split
has to resolve.

---

## 1. What a family declares

Subclass `Controller` in `vertex/controllers/base.py` and set what differs from
the default. Everything here has a default that reproduces the scalar law, so a
family declares only its departures.

| Attribute | Default | What it means |
|---|---|---|
| `name` | `"abstract"` | the registry key a manifest selects |
| `dim` | `1` | components per state and per virtual state |
| `coords` | `()` | suffixes when `dim > 1`, so a column reads `state_P` and not `state_0` |
| `logs_timing` | `False` | whether the agent records realized period and computation time beside the channels |
| `publishes_in_tick` | `False` | whether the packet leaves from inside the control step, after the commit |
| `holds_stale_neighbours` | `False` | whether an overdue neighbour keeps contributing its last value |
| `channels()` | `()` | the extra logged channels, in `ControllerOutput.extra` order, one name per scalar |
| `relay_columns()` | `column_names()` | the subset that survives a relayed node, when the family reports less than it computes |

`column_names()` composes the first four into the log schema and need not be
overridden.

Each default is the scalar law's behaviour, and each has a reason to be the
default rather than a reason to be arbitrary — `base.py` states them per
attribute. Two are worth repeating because they are not preference:

- **`publishes_in_tick`** must be `True` for a law whose gains depend on
  absolute time. With two independent loops the publish instant ties with a
  control instant every period and the tie-break is not stable, so the exchange
  lands a tick early or late. Measured on the virtual clock, completed-step
  counts at successive publishes ran 4, 10, 14, 19 — increments of 6, 4, 5
  rather than 5.
- **`holds_stale_neighbours`** must be `True` for a Laplacian disagreement,
  which is defined over a fixed edge set. Dropping an overdue term changes the
  graph; holding it only ages the information.

## 2. What the platform does with the declarations

The agent reads them and never branches on the family:

```
vertex/agent/agent.py   publishes_in_tick, logs_timing, dim, holds_stale_neighbours
vertex/agent/runlog.py  dim, to size the record and name the neighbour columns
```

`runlog.record_width(n_neighbors, n_state, n_timing, dim=...)` takes `dim` as a
parameter rather than inspecting the controller, so the log format is a function
of the declaration and of nothing else.

## 3. What a family may not touch

- **The scalar law's bytes.** `test/common/check_scalar_law_unchanged.py`
  asserts that every scaled value of `finite_time_adaptive` is identical to the
  pre-refactor law, step by step. A change that moves them is a defect
  regardless of what it was for.
- **The wire format of an existing frame.** A new family adds frame types; it
  does not widen one in use. The microgrid configuration is three frames
  (`MG_PLANT`, `MG_VIRTUAL`, `MG_IFACE`) because one 115-byte frame exceeded the
  64-byte USB CDC ceiling and was dropped silently.
- **The defaults above.** Changing a default changes every family that did not
  declare, including the scalar one.

## 4. Where the seam does not reach

Three things a family needed were not declared by the family. Two are now
closed and recorded here with what the fix was; the third is open.

### 4.1 Which columns cross a relay link — *closed*

A node whose law runs on an nRF reports through its host, and only part of the
state crosses that link. The family declares the subset:

```python
@classmethod
def relay_columns(cls) -> list[str]:
    return cls.column_names()        # the default: everything crosses
```

`vertex/agent/service.py::_controller_columns` asks the registry for it
instead of selecting between literals by family name. Getting it wrong is not
loud: a relayed node that declares more columns than it writes produces a log
with zero samples, because the mismatch raises inside an event-loop callback.

### 4.2 Which columns are stored scaled — *closed, and it was the same gap*

`vertex/analysis/units.py` converts between engineering units and the integers
on the wire, and `STATE_COLUMNS` lists what is subject to conversion. That list
turned out to be **exactly** the union of the three relay column sets, and not
by coincidence: a value arrives as a scaled integer precisely when it crossed
the STATE frame from an nRF. It is now derived from `relay_columns()` across
the registry rather than restated.

A column missing from the set raises `UnknownColumn` rather than passing
through unconverted, because the alternative fails quietly: it would leave that
node's trajectory in scaled integers while its metadata claims engineering
units (MICROGRID.md §2).

**Still open here.** `normalize_run` raises on a node that logs the full column
set — `g_P`, `mu_P`, `sigma_P`, `c`, `theta`, `saturated`, `trig`, `watchdog`,
`updates` and the timing columns are in neither `STATE_COLUMNS` nor
`PASSTHROUGH_COLUMNS`. Those columns only ever exist host-side, in engineering
units, so nothing has needed the conversion and the dg5 analysis reads through
`load_run` without it. Closing it means classifying each as scaled or
passthrough, which is a judgement per column rather than a derivation.

### 4.3 How parameters are carried

`AgentAssignment` in `vertex/agent/assignment.py` carries a `microgrid: dict`
field by name, and both `to_controller_params()` and
`vertex/topology/loader.py::controller_params_for` branch on whether it is
populated:

```
if self.microgrid:   return ControllerParams(dt_s=..., **_microgrid_params(...))
if c.microgrid is not None:  return ControllerParams(dt_s=..., **microgrid_blocks(...))
```

A third family would add a third field and a third branch in both places. This
is the largest of the three gaps and the one with no obvious default: the scalar
law's parameters are flat, the microgrid family's are three nested dataclasses,
and `ControllerParams` currently holds the union.

**What the seam would need:** the family owning the manifest→assignment→params
path for its own block, with `ControllerParams` carrying an opaque payload the
family unpacks.

## 5. The firmware side

The nRF runs the same two families, and the arrangement there is different
from the Python side on purpose.

**Both laws are compiled into every image, and the manifest chooses at run
time.** `enum control_law` selects, `agent.h` carries the parameters and
variables of both families in one `struct agent`, and `discrete_step` in
`coordination_task.c` dispatches on `params.law`:

```c
if (a->params.law != LAW_FINITE_TIME_ADAPTIVE) { microgrid_step(a); return; }
```

Plant, agent and control-law state therefore share one struct rather than
being separated per family. That is a deliberate choice and not an oversight:
a board holds one agent, the selection is a single enum compared once per
tick, and keeping the layout flat is what lets the host and the board agree on
a byte offset without a negotiation. The microgrid members cost 168 B of the
376 B struct — 45 % — which on a part with 256 KB of RAM is not a reason to do
anything differently.

### 5.1 What keeps C and Python consistent

The firmware is not a second implementation that happens to agree. Seven
checks in `test/check_all.sh` hold it to the host, and they are what makes a
firmware edit safe to make:

| Check | What it binds |
|---|---|
| `firmware symbols` | every `PROTO_T_*` and function a firmware uses is declared |
| `serial layout: nordic` | the C decoder's field offsets against the Python encoder's byte layout |
| `stats layout: nordic` | the firmware's STATS writer against the host's reader, field for field |
| `on-air v1 codec` | the advertisement encoding, both directions |
| `scalar law unchanged` | every scaled value of the scalar law, against a golden reference |
| `control law C vs Python` | the scalar law numerically, 400 steps, to within float32 precision |
| `microgrid law C vs Python` | the microgrid law numerically, 3000 steps, all four arms |

`scalar law unchanged` is the one that matters most when touching anything
shared: it asserts byte-for-byte equality against a stored reference, so a
change that perturbs the scalar law is caught even if it leaves the microgrid
family working.

### 5.2 If the inclusion is ever made conditional

Not planned. Recorded because the map is the expensive part.

A `CONFIG_VERTEX_MICROGRID` symbol would need a `Kconfig` in
`firmware/nordic/` — there is none today, and `prj.conf` carries only upstream
Zephyr symbols — plus `target_sources_ifdef` in `CMakeLists.txt`. The guards
would fall at eight points across six shared files:

| File | What is guarded |
|---|---|
| `agent.h` | the two struct members, `microgrid` and `mg` |
| `agent.c` | three frame-decode branches, two `microgrid_reset()` calls |
| `coordination_task.c` | the `discrete_step` dispatch |
| `proto.h`, `proto.c` | the three `MG_*` frame types, lengths and parser table |
| `control.c` | the routing of those frames |
| `report.c` | the second coordinate in the STATE tail |

Eight `#ifdef` in six shared files is its own kind of disorder, and plausibly
worse than one file compiled unconditionally. The arrangement that would
avoid both is the one the Python side already has: a dispatch table of
function pointers that each law registers, so including or excluding the file
is the whole change and `agent.h` stops naming a family. That is a firmware
refactor, not a build flag.

What is not known: the flash cost. The microgrid path calls `tanhf`, `expf`
and `sinf`, which may pull libm routines a scalar-only image would not carry.
Measuring it needs a Zephyr build of both configurations.

## 6. What this means for the split

PLATFORM.md §2 item 4 leaves open how far "apart" the microgrid work goes.
What this file establishes is the prior question — which edits to shared code
were *necessary* rather than convenient:

- §1–§3 of this file show that logging shape and tick behaviour were already
  extensible. The microgrid family declared `dim`, `coords`, `channels()`,
  `publishes_in_tick` and `holds_stale_neighbours`, and no shared module names
  it for any of them.
- §4 shows the three that were not. Two are now closed by moving the
  declaration onto the controller class, which is a change to the seam and not
  a directory move; they turned out to be one gap with two consumers. The
  third, parameter carriage, is the one with no obvious default.
- §5 shows that the firmware is outside this question. Both laws are compiled
  in and selected at run time, the shared struct is intended, and seven checks
  hold the C to the Python. A split of the Python side does not require the
  firmware to move, and conditional compilation there is a separate decision
  with its own map in §5.2.

So the seam can be closed without choosing the split, and closing it first is
what makes the split a relocation rather than a redesign.
