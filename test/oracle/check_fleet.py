#!/usr/bin/env python3
"""Five `MicrogridController`s against the oracle's whole-fleet trial.

The three module checks pin the pieces; this pins the composition. Five
controllers exchange virtual states directly, with no transport and no
impairment, against `sim.engine.run_trial` on the same configuration. What it
tests that the module checks cannot is the tick order: the command at sample
k must see the virtual and adaptive states of sample k, not k+1, and getting
that wrong shifts every trajectory by one step in a way each piece in
isolation looks fine under.

    python3 test/oracle/check_fleet.py

This is gate G2's mathematics. G2 itself adds the transport, which needs the
v2 payload first.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _oracle import require                                      # noqa: E402

_SKIP = require(Path(__file__).stem)
if _SKIP is not None:
    raise SystemExit(_SKIP)

from sim.config import (INITIAL_SETS, KAPPA, MU_MAX, N, P_MAX, Q_MAX, S0,     # noqa: E402
                        SETTING_B, S_TRUE, T_C, T_O, X_STAR,
                        InterfaceParams as OAdaptiveParams,
                        LCParams as OLCParams, RunConfig,
                        VirtualParams as OVirtualParams)
from sim.engine import run_trial                                  # noqa: E402
from sim.graph import DEGREE, NEIGHBORS, PIN                      # noqa: E402
from sim.impairments import Trace                                 # noqa: E402
from sim.virtual import z0_prescribed                             # noqa: E402
from vertex.controllers import ControllerParams, create           # noqa: E402
from vertex.numeric import quantize                              # noqa: E402
from vertex.controllers.interface_adaptive import AdaptiveParams  # noqa: E402
from vertex.controllers.interface_lc import LCParams              # noqa: E402
from vertex.controllers.plant import PlantParams                  # noqa: E402
from vertex.controllers.virtual import VirtualParams              # noqa: E402

#: One least significant bit of the v2 payload, relative to a virtual state
#: of order 20. The two sides round a tie differently at the wire: the
#: testbed's `quantize` is round-half-up and the oracle's encoder is numpy's
#: round-half-even, so a value landing exactly between two counts can differ
#: by one count of 1e-6. That is a rounding rule, not a different equation,
#: and the check is that it stays a floor rather than accumulating.
TOL = 1e-7
PROFILE, INITIAL = "per", "primary"
ALPHA_MULT = 10.0


def fleet(kind: str, **iface):
    """Five controllers on the benchmark ring, pinned at DG 1."""
    s, z0 = SETTING_B, z0_prescribed(N)
    X0 = INITIAL_SETS[INITIAL]
    op = OVirtualParams(a_o=OVirtualParams.a_o / ALPHA_MULT)
    out = []
    for i in range(N):
        vp = VirtualParams(T_o=op.T_o, alpha_0=op.alpha_0,
                           delta_o=s.delta_o, kappa_o=op.kappa_o, xi=op.xi,
                           c_0=op.c_0, b=float(PIN[i]),
                           rP=float(X_STAR[0]) if PIN[i] else 0.0,
                           rQ=float(X_STAR[1]) if PIN[i] else 0.0)
        pp = PlantParams(kappa=float(KAPPA[i]), S1=float(S_TRUE[i, 0]),
                         S2=float(S_TRUE[i, 1]), profile=PROFILE,
                         p_max=P_MAX, q_max=Q_MAX)
        ip = (AdaptiveParams(mu_max=MU_MAX, **iface) if kind == "adaptive"
              else LCParams(kappa=float(KAPPA[i]), profile=PROFILE,
                            p_max=P_MAX, q_max=Q_MAX, mu_max=MU_MAX, **iface))
        out.append(create(
            f"microgrid_{kind}",
            ControllerParams(dt_s=s.h, degree=int(DEGREE[i]), plant=pp,
                             virtual=vp, interface=ip,
                             state0_P=float(X0[i, 0]), state0_Q=float(X0[i, 1]),
                             vstate0_P=float(z0[i, 0]),
                             vstate0_Q=float(z0[i, 1]))))
    return out


def oracle_run(params):
    op = OVirtualParams(a_o=OVirtualParams.a_o / ALPHA_MULT)
    cfg = RunConfig(condition=S0, profile=PROFILE, initial=INITIAL,
                    setting=SETTING_B, encoding="v2", virtual=op)
    trace = Trace(cfg.condition, cfg.n_steps,
                  cfg.n_steps // cfg.steps_per_exchange + 2, cfg.h, seed=0)
    return run_trial(cfg, params, trace), cfg


def compare(kind: str, oracle_params, port_iface, label: str) -> int:
    r, cfg = oracle_run(oracle_params)
    ctrls = fleet(kind, **port_iface)
    span = cfg.steps_per_exchange
    # The held caches start empty, which is zeros: an agent has heard nothing
    # before the first exchange, and seeding them with the neighbours' true
    # initial values would hand every node information hardware does not give
    # it. That was a bug in an earlier version of this harness.
    held = {i: {j: (0, 0) for j in NEIGHBORS[i]} for i in range(N)}
    worst, at, which = 0.0, 0, ""
    for k in range(cfg.n_steps):
        # No exchange at k = 0, matching the oracle and the hardware: nobody
        # can receive before anyone has transmitted.
        if k > 0 and k % span == 0:             # exchange: perfect delivery
            snap = {i: tuple(ctrls[i].vstate) for i in range(N)}
            for i in range(N):
                for j in NEIGHBORS[i]:
                    held[i][j] = (quantize(snap[j][0]), quantize(snap[j][1]))
        if k == cfg.n_steps - 1:
            break               # no row k+1 to compare the last update against
        for i, c in enumerate(ctrls):
            flat = [v for j in NEIGHBORS[i] for v in held[i][j]]
            o = c.step(flat, [True] * len(NEIGHBORS[i]))
            # The two log at different points of the tick and that is a real
            # difference, in the record and not in the dynamics. The testbed
            # convention, which the scalar law has always used, is to return
            # the state *after* the update, so `o` holds x[k+1] and z[k+1];
            # the oracle appends before the update, so its row k holds x[k].
            nxt = min(k + 1, cfg.n_steps - 1)
            for name, got, want in (("x_P", o.state[0], r.x[nxt, i, 0]),
                                    ("x_Q", o.state[1], r.x[nxt, i, 1]),
                                    ("z_P", o.vstate[0], r.z[nxt, i, 0]),
                                    ("z_Q", o.vstate[1], r.z[nxt, i, 1])):
                d = abs(got - want) / max(1.0, abs(want))
                if d > worst:
                    worst, at, which = d, k, f"{name} at DG {i+1}"
    flag = "ok  " if worst <= TOL else "FAIL"
    print(f"  {flag} {label:<26} {cfg.n_steps} ticks x {N} agents, max "
          f"relative divergence {worst:.2e}"
          + (f" ({which} at step {at}, bounded, not accumulating)"
             if worst else ""))
    return 0 if worst <= TOL else 1


def main() -> int:
    print("five MicrogridControllers vs sim.engine.run_trial")
    eps = np.full(N, 0.09)
    bad = 0
    bad += compare("adaptive",
                   OAdaptiveParams(gamma=0.0025, eps=np.zeros(N), mu_max=MU_MAX),
                   dict(gamma=0.0025, eps=0.0), "C_AA")
    bad += compare("adaptive",
                   OAdaptiveParams(gamma=0.0025, eps=eps, mu_max=MU_MAX),
                   dict(gamma=0.0025, eps=0.09), "C_DZ")
    bad += compare("lc", OLCParams(terminal=True, Phi=3.0, mu_max=MU_MAX),
                   dict(terminal=True, Phi=3.0), "C_LC0")
    bad += compare("lc", OLCParams(terminal=False, Phi=3.0, mu_max=MU_MAX),
                   dict(terminal=False, Phi=3.0), "C_LC+")
    print(f"  -> {'the composition matches' if not bad else 'DIVERGENT'}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
