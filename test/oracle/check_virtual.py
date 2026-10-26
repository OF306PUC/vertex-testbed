#!/usr/bin/env python3
"""Cross-validate `vertex.controllers.virtual` against the reviewers-exp oracle.

The common virtual layer exists twice for the same reason the plant does: the
oracle evaluates all five observers at once in numpy, the port evaluates one
in plain floats, and only the arity is allowed to differ.

Three things are checked, in increasing strength: the regularized schedule,
one algebraic step from an arbitrary state, and a full run with the neighbour
snapshot refreshed on the exchange schedule.

    python3 test/oracle/check_virtual.py
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

from sim.config import (DIM, N, SETTING_B, X_STAR,                # noqa: E402
                        VirtualParams as OracleParams, RateSetting)
from sim.graph import DEGREE, NEIGHBORS, PIN                      # noqa: E402
from sim.virtual import VirtualLayer as OracleLayer, z0_prescribed  # noqa: E402
from vertex.controllers.virtual import (VirtualLayer,             # noqa: E402
                                        VirtualParams)

TOL = 1e-12
ALPHA_0 = 1.0                       # the candidate operating point
SETTING = SETTING_B
TIMES = (0.0, 0.3, 7.5, 29.999, 30.0, 40.0, 119.975)


def both(alpha_0: float = ALPHA_0, setting: RateSetting = SETTING):
    """The oracle and the five ports, from one parameter set."""
    a_o = 1.0 / (alpha_0 * OracleParams().T_o)
    op = OracleParams(a_o=a_o)
    assert abs(op.alpha_0 - alpha_0) < 1e-15
    oracle = OracleLayer(op, setting, np.zeros((N, DIM)))
    z0 = z0_prescribed(N)
    ports = [VirtualLayer(
        VirtualParams(T_o=op.T_o, alpha_0=alpha_0, delta_o=setting.delta_o,
                      kappa_o=op.kappa_o, xi=op.xi, c_0=op.c_0,
                      b=float(PIN[i]),
                      rP=float(X_STAR[0]) if PIN[i] else 0.0,
                      rQ=float(X_STAR[1]) if PIN[i] else 0.0),
        degree=int(DEGREE[i]), zP=z0[i, 0], zQ=z0[i, 1]) for i in range(N)]
    return oracle, ports


def rel(a, b) -> float:
    return abs(a - b) / max(1.0, abs(b))


def check_schedule() -> int:
    """alpha, f_o, rho_o, and the convex weight, at both settings."""
    bad, worst = 0, 0.0
    for setting in (SETTING_B, RateSetting("A", 0.2, 1.0)):
        oracle, ports = both(setting=setting)
        for t in TIMES:
            fo_ref, rho_ref = oracle.coefficients(t)
            for pl in ports:
                fo, rho = pl.coefficients(t)
                worst = max(worst, rel(pl.alpha(t), oracle.alpha(t)),
                            rel(fo, fo_ref), rel(rho, rho_ref))
        th_ref = oracle.theta(0.0)
        for i, pl in enumerate(ports):
            worst = max(worst, rel(pl.theta(0.0, setting.h), th_ref[i]))
    if worst > TOL:
        print(f"  FAIL schedule differs by {worst:.3e}")
        bad = 1
    else:
        print(f"  ok   alpha, f_o, rho_o and theta agree to {worst:.2e} "
              f"at both settings")
    return bad


def check_one_step() -> int:
    """One algebraic update from an arbitrary z, c and neighbour snapshot."""
    rng = np.random.default_rng(5)
    worst, bad = 0.0, 0
    for trial in range(8):
        oracle, ports = both()
        z = rng.uniform(-30.0, 40.0, size=(N, DIM))
        c = rng.uniform(0.0, 50.0, size=N)
        oracle.z, oracle.c = z.copy(), c.copy()
        z_hat = rng.uniform(0.0, 32.0, size=(N, N, DIM))
        oracle.hold(z_hat)
        for i, pl in enumerate(ports):
            pl.zP, pl.zQ, pl.c = z[i, 0], z[i, 1], c[i]
            pl.hold([tuple(z_hat[i, j]) for j in NEIGHBORS[i]])
            worst = max(worst, rel(pl.wP, oracle.w[i, 0]),
                        rel(pl.wQ, oracle.w[i, 1]))
        t = 3.0 + 2.0 * trial
        zp_r, cp_r, g_r, eta_r = oracle.candidate(t)
        for i, pl in enumerate(ports):
            zp0, zp1, cp, gP, gQ, eP, eQ = pl.candidate(t, SETTING.h)
            worst = max(worst, rel(zp0, zp_r[i, 0]), rel(zp1, zp_r[i, 1]),
                        rel(cp, cp_r[i]), rel(gP, g_r[i, 0]),
                        rel(gQ, g_r[i, 1]), rel(eP, eta_r[i, 0]),
                        rel(eQ, eta_r[i, 1]))
    if worst > TOL:
        print(f"  FAIL one step differs by {worst:.3e}")
        bad = 1
    else:
        print(f"  ok   w, eta, z+, c+ and g agree to {worst:.2e} "
              f"over 8 random states")
    return bad


def check_run() -> int:
    """A full 120 s run, snapshot refreshed on the exchange schedule."""
    oracle, ports = both()
    h, span = SETTING.h, SETTING.steps_per_exchange
    steps = int(round(120.0 / h))
    worst, at = 0.0, 0
    for k in range(steps):
        t = k * h
        if k % span == 0:
            # perfect delivery: every node sees every neighbour's current z
            z_now = np.asarray(oracle.z, dtype=float)
            oracle.hold(np.repeat(z_now[None], N, axis=0))
            for i, pl in enumerate(ports):
                pl.hold([(ports[j].zP, ports[j].zQ) for j in NEIGHBORS[i]])
        zp_r, cp_r, _g, _e = oracle.candidate(t)
        cand = [pl.candidate(t, h) for pl in ports]
        oracle.commit(zp_r, cp_r)
        for pl, cd in zip(ports, cand):
            pl.commit(cd[0], cd[1], cd[2])
        for i, pl in enumerate(ports):
            d = max(rel(pl.zP, oracle.z[i, 0]), rel(pl.zQ, oracle.z[i, 1]),
                    rel(pl.c, oracle.c[i]))
            if d > worst:
                worst, at = d, k
    ez = max(abs(ports[i].zP - X_STAR[0]) + abs(ports[i].zQ - X_STAR[1])
             for i in range(N))
    flag = "ok  " if worst <= TOL else "FAIL"
    print(f"  {flag} {steps} ticks, exchange every {span}: max relative "
          f"divergence {worst:.2e} at step {at}")
    print(f"       final E_z = {ez:.3e} (the layer reached the reference)")
    return 0 if worst <= TOL and ez < 1e-3 else 1


def main() -> int:
    print("vertex.controllers.virtual vs reviewers-exp/sim/virtual.py")
    bad = check_schedule() + check_one_step() + check_run()
    print(f"  -> {'one observer' if not bad else 'DIVERGENT'}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
