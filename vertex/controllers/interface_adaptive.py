"""The adaptive physical interface: always-active, and its dead-zone form.

    sigma_i[k] = x~_i[k] - z_i[k]
    omega_il   = sgn(sigma_il) min(vartheta_i, |sigma_il| / h)
    mu_i[k]    = g_i[k] - omega_i[k]
    U_i[k]     = kappa_i B_i^-1 sat_{mu_max}(mu_i[k])

The two controllers differ in one number and nothing else:

    C_AA   vartheta_i[k+1] = vartheta_i[k] + gamma 1{ ||sigma_i||_1 > 0 }
    C_DZ   the same with the indicator 1{ ||sigma_i||_1 > eps_i }

so ``eps = 0`` *is* the always-active ablation rather than a separate class.
That is deliberate: the comparison's whole claim is that the dead zone is the
only difference, and a second implementation would be a second thing that
could differ.

## Two properties worth knowing before reading the results

The correction is **clipped**: once ``vartheta_i >= ||sigma_i||_inf / h`` the
term equals ``sigma_i / h`` however much further the gain grows. A drifting
gain therefore need not produce any change in the applied input, and a claim
that the dead zone reduces control effort has to come from the inputs rather
than from the gain.

``gamma`` is an increment **per update**, not a rate. An Euler realization of
a continuous rate uses ``gamma = h gamma_ct``, and the two numbers must not be
identified: the same continuous behaviour needs a different ``gamma`` at a
different ``h``.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["AdaptiveParams", "AdaptiveInterface"]

#: Trigger forms. ``sigma`` is the measured error of the principal design;
#: ``residual`` is the post-correction form of the companion theorem, kept
#: because the two must never be interchanged inside one drift study.
TRIGGERS = ("sigma", "residual")


def _sign(v: float) -> float:
    """sgn with sgn(0) = 0, so an exactly satisfied coordinate contributes
    nothing rather than a unit kick."""
    return 0.0 if v == 0.0 else (1.0 if v > 0.0 else -1.0)


@dataclass(frozen=True, slots=True)
class AdaptiveParams:
    """One agent's interface. ``eps = 0`` selects the always-active ablation."""

    gamma: float                    # increment per update
    eps: float = 0.0                # dead-zone threshold; 0 -> C_AA
    vartheta0: float = 0.0
    mu_max: float = 10.0
    trigger: str = "sigma"

    def __post_init__(self) -> None:
        if self.gamma < 0:
            raise ValueError(f"gamma must be >= 0, got {self.gamma}")
        if self.eps < 0:
            raise ValueError(f"eps must be >= 0, got {self.eps}")
        if self.trigger not in TRIGGERS:
            raise ValueError(f"unknown trigger {self.trigger!r}, "
                             f"expected one of {TRIGGERS}")

    @property
    def label(self) -> str:
        return "DZ" if self.eps > 0.0 else "AA"


class AdaptiveInterface:
    """The interface for one agent, over two coordinates."""

    __slots__ = ("p", "h", "vartheta", "updates")

    def __init__(self, params: AdaptiveParams, h: float):
        self.p = params
        self.h = float(h)
        self.vartheta = float(params.vartheta0)
        self.updates = 0

    @property
    def label(self) -> str:
        return self.p.label

    def step(self, t_s: float, xP: float, xQ: float, zP: float, zQ: float,
             gP: float, gQ: float):
        """One command, and the adaptation that follows it.

        Returns ``(muP, muQ, mu_appP, mu_appQ, sigmaP, sigmaQ, omegaP, omegaQ,
        saturated, trig, fired)``. ``t_s`` is unused here and taken only so
        every interface presents the same call.
        """
        sP, sQ = xP - zP, xQ - zQ
        lim = self.vartheta
        wP = _sign(sP) * min(lim, abs(sP) / self.h)
        wQ = _sign(sQ) * min(lim, abs(sQ) / self.h)
        muP, muQ = gP - wP, gQ - wQ

        m = self.p.mu_max
        aP = m if muP > m else (-m if muP < -m else muP)
        aQ = m if muQ > m else (-m if muQ < -m else muQ)
        saturated = (aP != muP) or (aQ != muQ)

        if self.p.trigger == "sigma":
            trig = abs(sP) + abs(sQ)
        else:
            trig = abs(sP - self.h * wP) + abs(sQ - self.h * wQ)

        # Form the command first, adapt after: the command for sample k must
        # not see the gain that sample k produced.
        fired = trig > self.p.eps
        if fired:
            self.vartheta += self.p.gamma
            self.updates += 1
        return (muP, muQ, aP, aQ, sP, sQ, wP, wQ, saturated, trig, fired)
