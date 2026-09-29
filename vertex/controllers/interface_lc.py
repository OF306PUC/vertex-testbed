"""The literature baseline: a sampled prescribed-time adaptive interface.

    sigma_i[k]   = x~_i[k] - z_i[k]
    A_i[k]       = a_zeta(t_k) / kappa_i
    R_hat_i[k]   = A_i [[P~^s, Q~^s], [Q~^s, -P~^s]]
    lambda_ch[k] = beta_c / max(T_c - t_k, delta_c)
    mu_i[k]      = g_i[k] - lambda_ch sigma_i[k] - R_hat_i[k] S_hat_i[k]
    S_hat_i[k+1] = S_hat_i[k] + h Gamma_i R_hat_i[k]^T sigma_i[k]

``R_hat`` is symmetric, so the adaptation's transpose is free. Both commands
use the **old** ``S_hat[k]``; the update is committed afterwards.

Two variants, differing only past the deadline:

    C_LC0   the source's terminal policy: mu = 0 and S_hat frozen for t >= T_c
    C_LC+   the active branch for all k

Neither inherits exact prescribed-time convergence. LC0 documents the source's
policy; LC+ tests whether it is what fails under persistent uncertainty.

## The regressor is known, the parameter is not

``Psi_i`` and ``a_zeta(t)`` are known and evaluated here; ``S_i`` is the
emulator's alone and this module never sees it. ``S_hat`` starts at zero,
which is what keeps the estimate from being handed the answer.

## The adaptation gates

``Phi_i`` at the source's value leaves the estimate inert: the post-deadline
parameter rate is ``Phi a_zeta^2 (P~^s^2 + Q~^s^2) / lambda_ch``, and with
``a_zeta ~ 1e-3`` that is a time constant far longer than a trial. Raising it
alone does not fix that, because the transient then *deposits* an estimate
rather than converging to one: while sigma is large, ``Gamma int(R^T sigma)``
places S_hat wherever the transient leaves it and the post-deadline rate is
too slow to correct it.

``adapt_band`` holds the update shut until ``||sigma_i||_1`` falls below it,
so the transient contributes nothing and what follows is identification
rather than deposition. Note the inequality runs opposite to the dead zone of
the adaptive interface, and for a different reason: a robustness gain must
grow while the error is large, a parameter estimate must only read a residual
that is informative about the parameter.

Both default to off, which is the document's behaviour.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .plant import a_zeta, smooth_saturation

__all__ = ["LCParams", "LCInterface"]


@dataclass(frozen=True, slots=True)
class LCParams:
    """One agent's baseline interface."""

    kappa: float
    profile: str = "per"            # the known time profile of the regressor
    T_c: float = 40.0
    beta_c: float = 2.0
    Phi: float = 3e-3               # the source's value; see the module note
    terminal: bool = True           # True -> C_LC0, False -> C_LC+
    mu_max: float = 10.0
    p_max: float = 10.0
    q_max: float = 12.0
    #: None means the document's delta_c = 2h, resolved when h is known.
    delta_c: float | None = None
    #: Hold the parameter update until ``||sigma||_1`` is at or below this.
    #: None is the document's behaviour, which adapts from k = 0.
    adapt_band: float | None = None
    #: Hold it until this time. An alternative to ``adapt_band`` that
    #: hardcodes a deadline; kept for comparison, not recommended.
    adapt_after: float | None = None

    def __post_init__(self) -> None:
        if self.kappa <= 0:
            raise ValueError(f"kappa must be > 0, got {self.kappa}")
        if self.beta_c <= 0:
            raise ValueError(f"beta_c must be > 0, got {self.beta_c}")

    @property
    def label(self) -> str:
        return "LC0" if self.terminal else "LC+"

    def gamma_S(self) -> float:
        """``Gamma_i = kappa_i^2 Phi_i``.

        The ``kappa_i^2`` is what preserves the physical-coordinate design,
        and it is doing real work: it cancels exactly against
        ``A_i^2 = a_zeta^2 / kappa_i^2`` in the closed-loop parameter rate, so
        one ``Phi`` serves agents whose ratings differ five to one. A flat
        ``Gamma`` overshoots at the small-kappa end while the large end is
        still learning.
        """
        return self.Phi * self.kappa * self.kappa


class LCInterface:
    """The baseline interface for one agent, over two coordinates."""

    __slots__ = ("p", "h", "delta_c", "gamma_S", "S1", "S2", "updates")

    def __init__(self, params: LCParams, h: float):
        self.p = params
        self.h = float(h)
        self.delta_c = params.delta_c if params.delta_c is not None else 2.0 * h
        self.gamma_S = params.gamma_S()
        self.S1 = 0.0                   # S_hat[0] = 0: never given the answer
        self.S2 = 0.0
        self.updates = 0

    @property
    def label(self) -> str:
        return self.p.label

    @property
    def vartheta(self) -> float:
        """No sliding gain in this design. NaN rather than 0, because zero is
        a value the comparison could read and this is an absence."""
        return math.nan

    def lam(self, t_s: float) -> float:
        return self.p.beta_c / max(self.p.T_c - t_s, self.delta_c)

    def regressor(self, t_s: float, xP: float, xQ: float
                  ) -> tuple[float, float, float]:
        """``(A_i, P~^s, Q~^s)``, evaluated at the measured state."""
        p = self.p
        ps, qs = smooth_saturation(p.kappa * xP, p.kappa * xQ, p.p_max, p.q_max)
        return a_zeta(p.profile, t_s) / p.kappa, ps, qs

    def step(self, t_s: float, xP: float, xQ: float, zP: float, zQ: float,
             gP: float, gQ: float):
        """One command, then the parameter update.

        Returns the same shape the adaptive interface does, with ``omega``
        holding whatever was subtracted from the feedforward so that
        ``mu = g - omega`` reads as an identity on every arm. Under the
        terminal policy the command is zero, so ``omega`` is ``g``.
        """
        sP, sQ = xP - zP, xQ - zQ
        A, ps, qs = self.regressor(t_s, xP, xQ)

        terminal = self.p.terminal and t_s >= self.p.T_c
        if terminal:
            muP = muQ = 0.0
            wP, wQ = gP, gQ
        else:
            lam = self.lam(t_s)
            wP = lam * sP + A * (ps * self.S1 + qs * self.S2)
            wQ = lam * sQ + A * (qs * self.S1 - ps * self.S2)
            muP, muQ = gP - wP, gQ - wQ

        m = self.p.mu_max
        aP = m if muP > m else (-m if muP < -m else muP)
        aQ = m if muQ > m else (-m if muQ < -m else muQ)
        saturated = (aP != muP) or (aQ != muQ)
        trig = abs(sP) + abs(sQ)

        fired = False
        if not terminal:
            gate = True
            if self.p.adapt_after is not None and t_s < self.p.adapt_after:
                gate = False
            if self.p.adapt_band is not None and trig > self.p.adapt_band:
                gate = False
            if gate:
                step = self.h * self.gamma_S * A
                self.S1 += step * (ps * sP + qs * sQ)
                self.S2 += step * (qs * sP - ps * sQ)
                self.updates += 1
                fired = True
        return (muP, muQ, aP, aQ, sP, sQ, wP, wQ, saturated, trig, fired)
