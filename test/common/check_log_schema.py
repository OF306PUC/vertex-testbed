#!/usr/bin/env python3
"""The log schema a node declares must match what that node actually writes.

A run declares its columns once, at start, from the controller registry. The
writer then supplies a row per sample. Nothing checked that the two agreed,
and they did not: a `ble` node running the microgrid law declared 19 state
columns and 2 timing columns, because that is what `MicrogridAdaptive`
declares, while `_record_report` supplied the three values the nRF's STATE
frame carries and no timing at all.

`RunLog.append` did raise. But it raises inside an event-loop callback, where
the exception is handled by the loop and seen by nobody -- so all 4800
samples were dropped, the file kept its header, and the hub reported the node
ok. Two nodes of five logged nothing and the run looked successful.

This is the cheap check that closes it: build the real RunLog for every
controller, on both paths, and write the row that path actually produces.

    python3 test/common/check_log_schema.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from vertex.agent.runlog import RunLog, RunMeta          # noqa: E402
from vertex.agent.service import (_controller_columns,   # noqa: E402
                                  _timing_columns)
from vertex.controllers.base import REGISTRY             # noqa: E402

#: What each path hands to `RunLog.append`, as (channels, timing) counts.
#:
#: The relay's are not a choice: they are what the nRF's STATE frame carries.
#: Three under the scalar law, five under a microgrid one, because the frame
#: grows a tail with the second coordinate when the board runs it.
RELAY_TIMING = 0


def relay_channels(name: str) -> int:
    if name == "microgrid_lc":
        return 6        # four states plus the estimator pair
    return 5 if name.startswith("microgrid") else 3


def one(name: str, relay: bool, tmp: Path) -> str | None:
    cols = _controller_columns(name, relay=relay)
    timing = _timing_columns(name, relay=relay)
    cls = REGISTRY[name]
    n_ch = relay_channels(name) if relay else len(cls.column_names())
    n_ti = RELAY_TIMING if relay else (2 if cls.logs_timing else 0)

    dim = 2 if name.startswith("microgrid") else 1
    log = RunLog(tmp, RunMeta(
        run_name=f"schema-{name}-{'relay' if relay else 'local'}",
        node_id=1, node_type="ble" if relay else "bridge",
        manifest_name="schema", seed=0, run_index=0,
        dt_s=0.025, publish_period_s=0.125, neighbors=[2],
        state_columns=cols, timing_columns=timing, dim=dim,
        controller={}, units="scaled_int" if relay else "engineering",
        environment={}))
    log.start()
    try:
        # One neighbour, `dim` values for it: the flat shape the controller
        # is handed. Writing one value for a two-coordinate law is what the
        # log used to do, and it silently stored the wrong neighbour's.
        log.append(0.0, (0.0,) * n_ch, [0.0] * dim, [False],
                   device_t_s=0.0, neighbor_seq=[0], neighbor_rssi=[0],
                   timing=(0.0,) * n_ti)
        names = log.column_names()
        want = ([str(2)] if dim == 1 else ["2_P", "2_Q"])
        if [n for n in names if n.startswith("2")][:dim] != want:
            return f"neighbour columns are {names}, expected {want} among them"
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"
    finally:
        log.close()
    return None


def check_forward_compatible() -> int:
    """An assignment field the agent does not know must be inert.

    The `microgrid` block carries both controller parameters and things the
    run records about itself. When `T_c` was added for the second kind, every
    agent older than the hub answered `configure refused -- TypeError:
    ControllerParams.__init__() got an unexpected keyword argument 'T_c'`,
    and a five-node run died with nothing changed on the agents' side.

    Selecting keys by what ControllerParams accepts makes the next such field
    ignorable. This asserts that, because the failure mode is a whole run and
    the cause looks like a deployment problem rather than a schema one.
    """
    import yaml
    from vertex.agent.assignment import AgentAssignment, assignment_for
    from vertex.topology.loader import load_manifest

    path = ROOT / "experiments/dg5-mixed.yaml"
    if not path.exists():
        print("  --   forward compat   no dg5 manifest to build from")
        return 0
    m = load_manifest(yaml.safe_load(path.read_text()))
    d = assignment_for(m, 4).model_dump()
    d["microgrid"]["a_field_from_a_newer_hub"] = 123
    try:
        AgentAssignment(**d).to_controller_params()
    except Exception as exc:
        print(f"  FAIL forward compat   an unknown microgrid key is fatal: "
              f"{type(exc).__name__}: {exc}")
        return 1
    print("  ok   forward compat   an unknown microgrid key is ignored")
    return 0


def main() -> int:
    bad = 0
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        for name in sorted(REGISTRY):
            for relay, path in ((False, "local"), (True, "relay")):
                err = one(name, relay, tmp)
                cols = len(_controller_columns(name, relay=relay))
                ti = len(_timing_columns(name, relay=relay))
                if err is None:
                    print(f"  ok   {name:<22} {path:<6} "
                          f"{cols:>2} state + {ti} timing")
                else:
                    print(f"  FAIL {name:<22} {path:<6} {err}")
                    bad += 1
    bad += check_forward_compatible()
    print(f"  -> {'schemas agree' if not bad else f'{bad} mismatch(es)'}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
