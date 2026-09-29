#!/usr/bin/env python3
"""The scalar coordination law must produce the same numbers it always has.

`vertex/controllers/` grew a second family for the microgrid benchmark, and
that widened `ControllerOutput` from three floats to sequences plus a declared
channel list. The container changed; the arithmetic must not. 596 collected
runs and every published figure depend on the scalar law being the same
dynamical system it was, so this pins it.

`scalar_law_golden.json` was captured from the pre-refactor implementation and
holds the scaled integers of a 2000-step trajectory with the disturbance on, a
mixed enabled/disabled neighbour set, and a fixed seed. Equality is **exact**,
not within a tolerance: nothing in a container change may move a least
significant bit, so any difference at all is a packaging error rather than
numerical drift, and that is much easier to diagnose.

    python3 test/common/check_scalar_law_unchanged.py

The companion check is `test/crossval/compare.py`, which pins the same law
against the C the nRF runs. This one pins it against its own past.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))

from vertex.controllers.base import (ControllerParams,          # noqa: E402
                                     DisturbanceParams)
from vertex.controllers.finite_time_adaptive import (            # noqa: E402
    FiniteTimeAdaptiveController)

GOLDEN = HERE / "scalar_law_golden.json"


def main() -> int:
    g = json.loads(GOLDEN.read_text())
    pp, dd = g["params"], g["disturbance"]
    params = ControllerParams(
        dt_s=pp["dt_s"], state=pp["state"], vstate=pp["vstate"],
        vartheta=pp["vartheta"], eta=pp["eta"], gain_ij=pp["gain_ij"],
        alpha=pp["alpha"], delta=pp["delta"],
        disturbance=DisturbanceParams(**dd))
    ctrl = FiniteTimeAdaptiveController(params, seed=pp["seed"])

    cols = FiniteTimeAdaptiveController.column_names()
    if cols != g["columns"]:
        print(f"FAIL columns changed: {cols} != {g['columns']}")
        return 1

    nb, en = g["neighbors"], g["enabled"]
    bad, first = 0, None
    for k, want in enumerate(g["rows"]):
        got = list(ctrl.step(nb, en).scaled())
        if got != want:
            bad += 1
            if first is None:
                first = (k, got, want)
    print(f"  {len(g['rows'])} steps, dt={pp['dt_s']} s, disturbance on, "
          f"seed={pp['seed']}, {len(nb)} neighbours ({sum(en)} enabled)")
    print(f"  columns {cols}")
    if bad:
        k, got, want = first
        print(f"  FAIL {bad} step(s) differ; first at {k}: {got} != {want}")
        return 1
    print(f"  ok   every scaled value identical to the pre-refactor law")
    print("  -> the container changed, the arithmetic did not")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
