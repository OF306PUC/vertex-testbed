#!/usr/bin/env python3
"""Cross-validate the two physical interfaces against the reviewers-exp oracle.

Four arms, two modules: `interface_adaptive` is C_AA when eps = 0 and C_DZ
when it is positive, `interface_lc` is C_LC0 when the terminal policy is on
and C_LC+ when it is not. The oracle drives all five agents at once in numpy;
the port drives one in plain floats.

Driven with the same measured state, virtual state and feedforward at every
step, so the adaptive states evolve under identical inputs and any divergence
compounds rather than cancelling.

    python3 test/oracle/check_interfaces.py
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

from sim.config import (KAPPA, MU_MAX, N, P_MAX, Q_MAX,           # noqa: E402
                        InterfaceParams as OracleAdaptiveParams,
                        LCParams as OracleLCParams)
from sim.interface import (AdaptiveInterface as OracleAdaptive,   # noqa: E402
                           LCInterface as OracleLC)
from vertex.controllers.interface_adaptive import (               # noqa: E402
    AdaptiveInterface, AdaptiveParams)
from vertex.controllers.interface_lc import LCInterface, LCParams  # noqa: E402

TOL = 1e-12
H, STEPS = 0.025, 1200
PROFILE = "per"
FIELDS = ("muP", "muQ", "appP", "appQ", "sigP", "sigQ", "omP", "omQ",
          "sat", "trig", "fired")


def rel(a, b) -> float:
    a, b = float(a), float(b)
    return abs(a - b) / max(1.0, abs(b))


def drive(rng):
    """The same (x, z, g) sequence for both sides."""
    x = rng.uniform(-5.0, 45.0, size=(STEPS, N, 2))
    z = rng.uniform(0.0, 30.0, size=(STEPS, N, 2))
    g = rng.uniform(-8.0, 8.0, size=(STEPS, N, 2))
    return x, z, g


def compare(label, oracle, ports, x, z, g, extra=None) -> tuple[int, float]:
    worst, at, which = 0.0, 0, ""
    for k in range(STEPS):
        t = k * H
        ref = oracle.step(t, x[k], z[k], g[k])
        got = [pl.step(t, x[k, i, 0], x[k, i, 1], z[k, i, 0], z[k, i, 1],
                       g[k, i, 0], g[k, i, 1]) for i, pl in enumerate(ports)]
        for i, row in enumerate(got):
            cols = (ref.mu[i, 0], ref.mu[i, 1], ref.mu_app[i, 0],
                    ref.mu_app[i, 1], ref.sigma_hat[i, 0], ref.sigma_hat[i, 1],
                    ref.omega[i, 0], ref.omega[i, 1], float(ref.saturated[i]),
                    ref.trig[i], float(ref.fired[i]))
            for name, a, b in zip(FIELDS, row, cols):
                d = rel(float(a), float(b))
                if d > worst:
                    worst, at, which = d, k, f"{name} at DG {i+1}"
        if extra:
            worst_x, name_x = extra(oracle, ports)
            if worst_x > worst:
                worst, at, which = worst_x, k, name_x
    flag = "ok  " if worst <= TOL else "FAIL"
    print(f"  {flag} {label:<28} {STEPS} steps, max relative divergence "
          f"{worst:.2e}" + (f" ({which} at step {at})" if worst else ""))
    return (0 if worst <= TOL else 1), worst


def adaptive_state(oracle, ports):
    w = max(rel(pl.vartheta, oracle.vartheta[i]) for i, pl in enumerate(ports))
    return w, "vartheta"


def lc_state(oracle, ports):
    w = 0.0
    for i, pl in enumerate(ports):
        w = max(w, rel(pl.S1, oracle.S_hat[i, 0]), rel(pl.S2, oracle.S_hat[i, 1]))
    return w, "S_hat"


def check_adaptive(eps_scale: float, label: str) -> int:
    rng = np.random.default_rng(17)
    x, z, g = drive(rng)
    gamma, eps = 0.0025, eps_scale * np.full(N, 0.09)
    oracle = OracleAdaptive(
        OracleAdaptiveParams(gamma=gamma, eps=eps, mu_max=MU_MAX), H)
    ports = [AdaptiveInterface(
        AdaptiveParams(gamma=gamma, eps=float(eps[i]), mu_max=MU_MAX), H)
        for i in range(N)]
    if oracle.label != ports[0].label:
        print(f"  FAIL label {oracle.label} != {ports[0].label}")
        return 1
    bad, _ = compare(label, oracle, ports, x, z, g, adaptive_state)
    return bad


def check_lc(terminal: bool, Phi: float, band, label: str) -> int:
    rng = np.random.default_rng(23)
    x, z, g = drive(rng)
    kw = dict(terminal=terminal, Phi=Phi, mu_max=MU_MAX, adapt_band=band)
    oracle = OracleLC(OracleLCParams(**kw), H, PROFILE)
    ports = [LCInterface(LCParams(kappa=float(KAPPA[i]), profile=PROFILE,
                                  p_max=P_MAX, q_max=Q_MAX, **kw), H)
             for i in range(N)]
    if oracle.label != ports[0].label:
        print(f"  FAIL label {oracle.label} != {ports[0].label}")
        return 1
    bad, _ = compare(label, oracle, ports, x, z, g, lc_state)
    return bad


def check_terminal_policy() -> int:
    """C_LC0 must freeze S_hat at T_c, not merely zero the command.

    eq. (47) of the source sets both ``U_i = 0`` and ``S_hat_dot = 0`` for
    ``t >= T_c``. Zeroing only the command would leave the estimate drifting
    on an error the controller is no longer acting on, and the post-deadline
    drift study would then be measuring an adaptation that cannot affect
    anything. Checked on both sides, and against C_LC+, which must keep
    moving or the contrast is vacuous.
    """
    h, T_c = 0.025, 40.0
    bad = 0
    for terminal, must_freeze in ((True, True), (False, False)):
        port = LCInterface(LCParams(kappa=1 / 3, profile=PROFILE, T_c=T_c,
                                    terminal=terminal, Phi=3.0,
                                    mu_max=MU_MAX), h)
        oracle = OracleLC(OracleLCParams(terminal=terminal, Phi=3.0,
                                         T_c=T_c, mu_max=MU_MAX), h, PROFILE)
        x = np.full((N, 2), [20.5, 25.3])
        z = np.full((N, 2), [20.0, 25.0])
        g = np.full((N, 2), [0.1, -0.1])
        at_tc = None
        for k in range(int(2 * T_c / h)):
            t = k * h
            port.step(t, x[0, 0], x[0, 1], z[0, 0], z[0, 1], g[0, 0], g[0, 1])
            out = oracle.step(t, x, z, g)
            if abs(t - T_c) < 1e-9:
                at_tc = (port.S1, port.S2, oracle.S_hat.copy(),
                         float(port.updates))
            if terminal and t >= T_c and bool(out.fired.any()):
                print(f"  FAIL {port.label} adapted at t = {t} >= T_c")
                bad = 1
                break
        froze = (port.S1, port.S2) == at_tc[:2]
        froze_o = bool(np.array_equal(oracle.S_hat, at_tc[2]))
        if froze is not must_freeze or froze_o is not must_freeze:
            print(f"  FAIL {port.label}: frozen={froze} (oracle {froze_o}), "
                  f"expected {must_freeze}")
            bad = 1
        else:
            verb = "frozen at T_c" if must_freeze else "still adapting past T_c"
            print(f"  ok   {port.label + ',':<6} S_hat {verb:<26} "
                  f"({at_tc[0]:.4e}, {at_tc[1]:.4e}) -> "
                  f"({port.S1:.4e}, {port.S2:.4e})")
    return bad


def main() -> int:
    print("vertex.controllers.interface_* vs reviewers-exp/sim/interface.py")
    bad = 0
    bad += check_adaptive(0.0, "C_AA  (eps = 0)")
    bad += check_adaptive(1.0, "C_DZ  (eps_i calibrated)")
    bad += check_lc(True, 3e-3, None, "C_LC0 (as written)")
    bad += check_lc(False, 3e-3, None, "C_LC+ (as written)")
    bad += check_lc(False, 3e6, 0.02, "C_LC+ (gated, Phi = 3e6)")
    bad += check_terminal_policy()
    print(f"  -> {'four arms, one implementation each' if not bad else 'DIVERGENT'}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
