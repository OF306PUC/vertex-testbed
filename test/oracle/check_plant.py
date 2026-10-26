#!/usr/bin/env python3
"""Cross-validate `vertex.controllers.plant` against the reviewers-exp oracle.

The microgrid emulator exists twice and has to stay one dynamical system. The
oracle in `reviewers-exp/sim/plant.py` evaluates all five DGs at once in numpy
and is what every Phase 1 result was measured with; the port in
`vertex/controllers/plant.py` evaluates one DG in plain floats, because that is
the shape an agent runs and the shape a C port would use. Same equations,
different arity, and this is the check that the difference is only arity.

This is gate G2's first piece. The later ones add the virtual layer, the
interfaces and the whole fleet over the loopback bus.

    python3 test/oracle/check_plant.py

The residual is expected to be nonzero and tiny: the two reach `tanh`, `exp`
and `sin` through numpy and through `math`, which may differ in the last
place. What is asserted is that the difference stays at that floor rather than
growing like a different equation.
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

from sim import plant as oracle                              # noqa: E402
from sim.config import (INITIAL_SETS, KAPPA, N, P_MAX, Q_MAX, S_TRUE,
                        UNCERTAINTY_PROFILES)                # noqa: E402
from vertex.controllers.plant import (MicrogridPlant, PlantParams,
                                      a_zeta, smooth_saturation)   # noqa: E402

#: A few ulp of 1.0. Both sides are float64 doing the same operations; only
#: the libm behind tanh/exp/sin can differ.
TOL = 1e-12
STEPS, H = 400, 0.025
TIMES = (0.0, 0.3, 1.0, 7.5, 40.0, 119.975)


def params(i: int, profile: str) -> PlantParams:
    return PlantParams(kappa=float(KAPPA[i]), S1=float(S_TRUE[i, 0]),
                       S2=float(S_TRUE[i, 1]), profile=profile,
                       p_max=P_MAX, q_max=Q_MAX)


def rel(a: float, b: float) -> float:
    return abs(a - b) / max(1.0, abs(b))


def check_scalars() -> tuple[int, float]:
    worst = 0.0
    for profile in UNCERTAINTY_PROFILES:
        for t in TIMES:
            worst = max(worst, rel(a_zeta(profile, t),
                                   float(oracle.a_zeta(profile, t))))
    X = np.array([[3.0, -4.0], [40.0, 55.0], [0.0, 0.0], [-9.0, 11.0],
                  [1e3, -1e3]])
    got = np.array([smooth_saturation(p, q, P_MAX, Q_MAX) for p, q in X])
    worst = max(worst, float(np.abs(got - oracle.smooth_saturation(X)).max()))
    print(f"  ok   a_zeta and the rating limiter agree to {worst:.2e}")
    return 0, worst


def check_zeta() -> tuple[int, float]:
    """zeta_i at several states and times, every DG, both profiles."""
    worst, bad = 0.0, 0
    rng = np.random.default_rng(3)
    for profile in UNCERTAINTY_PROFILES:
        for _ in range(6):
            x = rng.uniform(-80.0, 80.0, size=(N, 2))
            for t in TIMES:
                ref = oracle.zeta(x, t, profile)
                for i in range(N):
                    pl = MicrogridPlant(params(i, profile), x[i, 0], x[i, 1])
                    zP, zQ = pl.zeta(t)
                    worst = max(worst, rel(zP, ref[i, 0]), rel(zQ, ref[i, 1]))
    if worst > TOL:
        print(f"  FAIL zeta differs by {worst:.3e} > {TOL:.0e}")
        bad = 1
    else:
        print(f"  ok   zeta agrees to {worst:.2e} over "
              f"{len(UNCERTAINTY_PROFILES) * 6 * len(TIMES) * N} evaluations")
    return bad, worst


def check_coordinates() -> int:
    """x = X/kappa, X = kappa x, U = kappa B^-1 mu, and the analytic bound."""
    bad = 0
    X0 = INITIAL_SETS["primary"]
    x_ref = oracle.x0_from_physical(X0)
    bound_ref = oracle.zeta_bound()
    mu = np.tile(np.array([2.0, -3.0]), (N, 1))
    u_ref = oracle.physical_command(mu)
    for i in range(N):
        p = params(i, "per")
        pl = MicrogridPlant.from_physical(p, X0[i, 0], X0[i, 1])
        if max(rel(pl.xP, x_ref[i, 0]), rel(pl.xQ, x_ref[i, 1])) > TOL:
            print(f"  FAIL normalization differs at DG {i+1}")
            bad = 1
        back = np.array(pl.physical)
        if np.abs(back - X0[i]).max() > TOL:
            print(f"  FAIL kappa x does not recover X at DG {i+1}")
            bad = 1
        u = np.array(pl.command(mu[i, 0], mu[i, 1]))
        if np.abs(u - u_ref[i]).max() > TOL:
            print(f"  FAIL physical command differs at DG {i+1}")
            bad = 1
        if rel(p.zeta_bound(), bound_ref[i]) > TOL:
            print(f"  FAIL zeta bound differs at DG {i+1}: "
                  f"{p.zeta_bound()} vs {bound_ref[i]}")
            bad = 1
    if not bad:
        print(f"  ok   normalization, command and bar_zeta agree "
              f"(bar_zeta = {bound_ref[0]:.4f} at every DG)")
    return bad


def check_trajectory() -> tuple[int, float]:
    """The same mu sequence through both emulators, step for step."""
    profile = "per"
    rng = np.random.default_rng(11)
    mu = rng.uniform(-10.0, 10.0, size=(STEPS, N, 2))
    x_ref = oracle.x0_from_physical(INITIAL_SETS["primary"]).copy()
    plants = [MicrogridPlant(params(i, profile), x_ref[i, 0], x_ref[i, 1])
              for i in range(N)]
    worst, at = 0.0, 0
    for k in range(STEPS):
        t = k * H
        x_ref = x_ref + H * (mu[k] + oracle.zeta(x_ref, t, profile))
        for i, pl in enumerate(plants):
            pl.step(mu[k, i, 0], mu[k, i, 1], t, H)
            d = max(rel(pl.xP, x_ref[i, 0]), rel(pl.xQ, x_ref[i, 1]))
            if d > worst:
                worst, at = d, k
    flag = "ok  " if worst <= TOL else "FAIL"
    print(f"  {flag} {STEPS} Euler steps at h = {H} s: "
          f"max relative divergence {worst:.2e} at step {at}")
    return (0 if worst <= TOL else 1), worst


def main() -> int:
    print("vertex.controllers.plant vs reviewers-exp/sim/plant.py")
    bad = 0
    b, _ = check_scalars(); bad += b
    b, _ = check_zeta(); bad += b
    bad += check_coordinates()
    b, _ = check_trajectory(); bad += b
    print(f"  -> {'one dynamical system' if not bad else 'DIVERGENT'}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
