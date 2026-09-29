"""The nonlinear DG emulator of the microgrid benchmark.

    Xdot_i = B_i U_i + Psi_i(X_i, t) S_i,          B_i = 108 I_2
    Psi_i  = a_zeta(t) [[P^s, Q^s], [Q^s, -P^s]]
    P^s    = P_max tanh(P / P_max),   Q^s = Q_max tanh(Q / Q_max)

In the normalized coordinates ``x_i = X_i / kappa_i`` and
``U_i = kappa_i B_i^-1 mu_i`` this is exactly the matched form the coordination
theory uses,

    xdot_i = mu_i + zeta_i(x_i, t),   zeta_i = kappa_i^-1 Psi_i(kappa_i x_i, t) S_i

so the emulator integrates ``x`` and recovers ``X = kappa x`` for logging.

## One node, two coordinates, plain floats

Each agent emulates only its own generator, so this works on two floats rather
than on an (N, 2) array. That is not a micro-optimisation: it is the shape the
firmware would use if this law is ever ported to C, and keeping the two in the
same shape is what makes them checkable against each other, the way
``coordination_task.c`` and ``finite_time_adaptive.py`` are today.

## S_i does not leave this module

``S_i`` is the unknown the LC interface estimates and the AA/DZ interfaces
never see. It is held here, in the emulator, and nothing constructs an
interface with a reference to a plant. That is the whole of the information
barrier: not a convention, but the absence of a reference.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

__all__ = ["PlantParams", "MicrogridPlant", "a_zeta", "smooth_saturation",
           "PROFILES"]

#: The two uncertainty profiles of the benchmark. ``rep`` reproduces the time
#: profile of the source and has effectively vanished by t = 2 s; ``per`` keeps
#: the matched uncertainty alive through the post-convergence interval, which
#: is what makes the terminal-policy question answerable at all.
PROFILES = ("rep", "per")


def a_zeta(profile: str, t_s: float) -> float:
    """The scalar uncertainty profile."""
    if profile == "rep":
        return 1e-3 * math.exp(-5.0 * t_s)
    if profile == "per":
        return 1e-3 * (0.75 + 0.25 * math.sin(0.1 * t_s))
    raise ValueError(f"unknown uncertainty profile {profile!r}, "
                     f"expected one of {PROFILES}")


def smooth_saturation(p: float, q: float, p_max: float, q_max: float
                      ) -> tuple[float, float]:
    """``(P^s, Q^s)``: the rating limiter that bounds the regressor globally.

    A smooth limiter rather than a clip, so the regressor stays differentiable
    and its bound holds without assuming anything about the trajectory after
    the fact.
    """
    return p_max * math.tanh(p / p_max), q_max * math.tanh(q / q_max)


@dataclass(frozen=True, slots=True)
class PlantParams:
    """One generator's data. ``S1``/``S2`` are the emulator's alone."""

    kappa: float                    # kappa_i = i / 15 on the benchmark
    S1: float                       # the unknown parameter, emulator only
    S2: float
    profile: str = "per"
    p_max: float = 10.0             # kW
    q_max: float = 12.0             # kvar
    b: float = 108.0                # B_i = b I_2

    def __post_init__(self) -> None:
        if self.kappa <= 0:
            raise ValueError(f"kappa must be > 0, got {self.kappa}")
        if self.profile not in PROFILES:
            raise ValueError(f"unknown profile {self.profile!r}")

    def zeta_bound(self) -> float:
        """``bar_zeta_i`` of the benchmark's explicit bound, in the 1-norm.

        Used only to check that the plant satisfies the coordination theory's
        hypothesis. It is never given to an adaptive law.
        """
        return (1e-3 / self.kappa) * (self.p_max + self.q_max) * (
            abs(self.S1) + abs(self.S2))


class MicrogridPlant:
    """The emulator for one DG, integrating the normalized state."""

    __slots__ = ("p", "xP", "xQ")

    def __init__(self, params: PlantParams, xP: float = 0.0, xQ: float = 0.0):
        self.p = params
        self.xP, self.xQ = float(xP), float(xQ)

    # --- coordinates ---------------------------------------------------------
    @classmethod
    def from_physical(cls, params: PlantParams, P: float, Q: float
                      ) -> "MicrogridPlant":
        return cls(params, P / params.kappa, Q / params.kappa)

    @property
    def physical(self) -> tuple[float, float]:
        """``X_i = kappa_i x_i``, in kW and kvar."""
        return self.p.kappa * self.xP, self.p.kappa * self.xQ

    def command(self, muP: float, muQ: float) -> tuple[float, float]:
        """``U_i = kappa_i B_i^-1 mu_i``, the physical command."""
        k = self.p.kappa / self.p.b
        return k * muP, k * muQ

    def rating_utilisation(self) -> tuple[float, float]:
        """``|X_i| / [P,Q]_max``. Above 1 means outside the nameplate."""
        P, Q = self.physical
        return abs(P) / self.p.p_max, abs(Q) / self.p.q_max

    # --- dynamics ------------------------------------------------------------
    def zeta(self, t_s: float) -> tuple[float, float]:
        """The matched uncertainty in normalized coordinates, at the current x.

        ``Psi_i`` is evaluated at the rating-limited physical powers, so this
        is bounded by :meth:`PlantParams.zeta_bound` whatever x does.
        """
        p = self.p
        ps, qs = smooth_saturation(p.kappa * self.xP, p.kappa * self.xQ,
                                   p.p_max, p.q_max)
        a = a_zeta(p.profile, t_s) / p.kappa
        return a * (ps * p.S1 + qs * p.S2), a * (qs * p.S1 - ps * p.S2)

    def step(self, muP: float, muQ: float, t_s: float, h: float
             ) -> tuple[float, float]:
        """One forward-Euler update, ``x[k+1] = x[k] + h (mu[k] + zeta[k])``.

        Euler and not something better on purpose: the sampled plant is part
        of the experiment's specification, and the coordination result is
        stated for exactly this update. ``mu`` is the *applied* command, after
        saturation.
        """
        zP, zQ = self.zeta(t_s)
        self.xP += h * (muP + zP)
        self.xQ += h * (muQ + zQ)
        return self.xP, self.xQ
