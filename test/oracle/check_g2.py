#!/usr/bin/env python3
"""Gate G2: the whole fleet, through the real transport, against the oracle.

`check_fleet.py` pins the composition with the agents handing each other
numbers directly. This one puts the production path between them: the v2
codec, the manufacturer-data framing, the loopback bus, the neighbour table
with its freshness accounting, and the agent's two independent periodic loops
under a virtual clock. Only the medium and the clock are swapped; the agents,
the control loops and the wire are the ones that ship.

What it can find that nothing before it could is a disagreement about *when*
things happen: the oracle exchanges at the start of every fifth tick, while
the agent's publish loop is a separate periodic task that happens to share
some instants with the control loop. An ordering difference there shifts the
exchange schedule by one tick and changes the trajectory.

    python3 test/oracle/check_g2.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _oracle import require                                      # noqa: E402

_SKIP = require(Path(__file__).stem)
if _SKIP is not None:
    raise SystemExit(_SKIP)

from sim.config import (INITIAL_SETS, KAPPA, MU_MAX, N, S0, SETTING_B,        # noqa: E402
                        S_TRUE, X_STAR, InterfaceParams as OAdaptiveParams,
                        RunConfig, VirtualParams as OVirtualParams)
from sim.engine import run_trial                                  # noqa: E402
from sim.impairments import Trace                                 # noqa: E402
from sim.virtual import z0_prescribed                             # noqa: E402
from vertex.sim import SimulatedExperiment                        # noqa: E402
from vertex.topology.loader import load_manifest                  # noqa: E402

ALPHA_MULT, PROFILE, INITIAL = 10.0, "per", "primary"
DURATION = 120.0
#: One LSB of the v2 payload against a virtual state of order 20, the same
#: wire-rounding floor check_fleet.py documents.
TOL = 1e-7

TYPES = {1: "bridge", 2: "wifi", 3: "bridge", 4: "ble", 5: "ble"}


def manifest(topology: str):
    z0, X0 = z0_prescribed(N), INITIAL_SETS[INITIAL]
    s, op = SETTING_B, OVirtualParams(a_o=OVirtualParams.a_o / ALPHA_MULT)
    types = ({i: "bridge" for i in range(1, 6)} if topology == "bridge5"
             else TYPES)
    return load_manifest({
        "name": f"dg5-{topology}", "seed": 20260929,
        "nodes": [{"id": i, "ip": f"10.6.5.{i}", "type": types[i],
                   "publish_period_s": s.h_v,
                   "plant": {"kappa": float(KAPPA[i - 1]),
                             "S": [float(S_TRUE[i - 1, 0]),
                                   float(S_TRUE[i - 1, 1])],
                             "X0": [float(X0[i - 1, 0]), float(X0[i - 1, 1])],
                             "z0": [float(z0[i - 1, 0]), float(z0[i - 1, 1])]}}
                  for i in range(1, 6)],
        "structure": {"generator": "ring",
                      "params": {"k": 1, "ids": [1, 2, 3, 4, 5]}},
        "coordination": {"reference": list(X_STAR), "pinned": {1: 1.0}},
        "controller": {"name": "microgrid_adaptive", "dt_s": s.h,
                       "microgrid": {"interface": "adaptive", "eps": 0.09,
                                     "gamma": 0.0025, "alpha_0": op.alpha_0,
                                     "delta_o": s.delta_o, "mu_max": MU_MAX,
                                     "profile": PROFILE}}})


def oracle():
    op = OVirtualParams(a_o=OVirtualParams.a_o / ALPHA_MULT)
    cfg = RunConfig(condition=S0, profile=PROFILE, initial=INITIAL,
                    setting=SETTING_B, encoding="v2", virtual=op)
    trace = Trace(cfg.condition, cfg.n_steps,
                  cfg.n_steps // cfg.steps_per_exchange + 2, cfg.h, seed=0)
    eps = np.full(N, 0.09)
    return run_trial(cfg, OAdaptiveParams(gamma=0.0025, eps=eps,
                                          mu_max=MU_MAX), trace), cfg


def run_one(topology: str) -> int:
    m = manifest(topology)
    exp = SimulatedExperiment(m, loss=0.0, delay_s=0.0, record_history=True)
    out = asyncio.run(exp.run(duration_s=DURATION))
    r, cfg = oracle()

    dim = 2
    worst, at, which = 0.0, 0, ""
    for nid, agent in sorted(out.agents.items()):
        i = nid - 1
        hist = agent.history
        n = min(len(hist), cfg.n_steps - 1)
        for k in range(n):
            ch = hist[k].channels
            got = (*ch[:dim], *ch[dim:2 * dim])       # x then z
            want = (r.x[k + 1, i, 0], r.x[k + 1, i, 1],
                    r.z[k + 1, i, 0], r.z[k + 1, i, 1])
            for name, a, b in zip(("x_P", "x_Q", "z_P", "z_Q"), got, want):
                d = abs(a - b) / max(1.0, abs(b))
                if d > worst:
                    worst, at, which = d, k, f"{name} at DG {nid}"
    steps = min(len(next(iter(out.agents.values())).history), cfg.n_steps - 1)
    flag = "ok  " if worst <= TOL else "FAIL"
    print(f"  {flag} {topology:<8} {steps} ticks x {N} agents through the wire, "
          f"max relative divergence {worst:.2e}"
          + (f" ({which} at step {at})" if worst else ""))
    print(f"       spread {out.initial_spread():.4f} -> {out.spread():.2e}, "
          f"bus {out.bus_counters}, "
          f"worst link delivery {out.worst_delivery_ratio() * 100:.1f}%")
    return 0 if worst <= TOL else 1


def main() -> int:
    print("gate G2: SimulatedExperiment vs sim.engine.run_trial")
    bad = run_one("bridge5") + run_one("mixed")
    print(f"  -> {'G2 passes' if not bad else 'G2 FAILS'}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
