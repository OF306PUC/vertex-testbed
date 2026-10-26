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

Three things a family needs are not declared by the family. They are maintained
in shared modules, keyed by the family's name, and they are what a second system
had to edit rather than extend.

### 4.1 Which columns cross a relay link

A node whose law runs on an nRF reports through its host, and only part of the
state crosses that link. The subset is a literal in
`vertex/agent/service.py`, selected by name:

```
RELAY_COLUMNS              = ("state", "vstate", "vartheta")
RELAY_COLUMNS_MICROGRID    = (...)
RELAY_COLUMNS_MICROGRID_LC = (...)

if name == "microgrid_lc": ...
return list(RELAY_COLUMNS_MICROGRID if name.startswith("microgrid") else ...)
```

A family cannot express "these of my columns cross the relay". Getting it wrong
is not loud: a relayed node that declares more columns than it writes produces a
log with zero samples, because the mismatch raises inside an event-loop callback.

**What the seam would need:** a `relay_columns()` classmethod defaulting to
`column_names()`.

### 4.2 Which columns are stored scaled

`vertex/analysis/units.py` holds `STATE_COLUMNS`, the columns subject to
conversion between engineering units and the integers on the wire. Every entry
is derivable from the registry — but as a *subset*: 9 names out of the 23 that
`column_names()` yields across the registered families. The rule that selects
the subset is not written anywhere a family can state it.

The failure mode is silent and is why `UnknownColumn` is raised rather than
passing through: a column missing from the list leaves that node's trajectory in
scaled integers while its metadata claims engineering units, and nothing
downstream notices (MICROGRID.md §2).

**What the seam would need:** a `scaled_columns()` classmethod, or a flag per
channel, so the list is derived rather than maintained.

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

## 5. What this means for the split

PLATFORM.md §2 item 4 leaves open how far "apart" the microgrid work goes. The
three gaps above are the answer to the prior question — which edits to shared
code were *necessary* rather than convenient:

- §1–§3 of this file show that logging shape and tick behaviour were already
  extensible. The microgrid family declared `dim`, `coords`, `channels()`,
  `publishes_in_tick` and `holds_stale_neighbours`, and no shared module names
  it for any of them.
- §4 shows the three that were not. All three are *lists or branches keyed by
  family name*, and all three could be closed by moving the declaration onto the
  controller class — a change to the seam, not a directory move.

So the seam can be closed without choosing the split, and closing it first is
what makes the split a relocation rather than a redesign.
