"""The common virtual layer: one prescribed-time observer, one node.

    eta_i = D_i z_i - w_i,        D_i = sum_j a_ij + b_i
                                  w_i = sum_j a_ij z~_ij + b_i r
    z_i+  = (z_i + h a_o,i w_i) / (1 + h a_o,i D_i)
    g_i   = -a_o,i eta_i / (1 + h a_o,i D_i)
    c_i+  = c_i + h rho_o xi_i f_o^2 ||eta_i||_2^2

with ``a_o,i = c_i f_o(t)``, ``f_o = alpha + kappa_o``, ``rho_o = 2 alpha`` and
``alpha(t) = alpha_0 T_o / max(T_o - t, delta_o)``.

Only the pinned node has ``b_i > 0``, and only it evaluates the term carrying
the reference. Everyone else reaches it through the graph.

## Why there is no solver here

An earlier version of the specification integrated this subsystem with RK4
over 200 internal steps per tick. The revision replaced that with the single
semi-implicit step above, applied to the local ``z_i`` term only while the
neighbour snapshot and ``a_o,i`` are held. One reciprocal serves both
coordinates and the gain update needs a sum of two squares, no square root.

The form that matters is the convex one,

    z_i+ = (1 - theta) z_i + theta (w_i / D_i),
    theta = h a_o,i D_i / (1 + h a_o,i D_i) in [0, 1)

because ``w_i / D_i`` is itself a convex combination of the received values
and, at the pinned node, the reference. A coordinate interval containing them
is therefore preserved for any nonnegative gain, which is what makes the
update safe at a gain the adaptation is free to grow without bound. It is not
a proof of prescribed-time convergence.

## What it costs, and what it cannot buy

As the gain grows ``theta`` tends to one and the update becomes
``z_i <- w_i / D_i``: a Jacobi sweep toward the neighbour average. With the
snapshot held between packets the layer performs one such sweep per exchange
however large the gain, so the contraction per *packet* is bounded by the
spectral radius of ``D^-1 A`` on the graph, 0.9336 on the benchmark's pinned
ring. Convergence is bought with packets, not with gain. See
``reviewers-exp/EXCHANGE-LIMIT.md``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

__all__ = ["VirtualParams", "VirtualLayer"]


@dataclass(frozen=True, slots=True)
class VirtualParams:
    """One node's share of the common layer.

    ``alpha_0`` is given directly rather than through the source's
    ``1/(a_o T_o)``. The document derives 0.1 that way; the value actually
    used is a declared modification, because at 0.1 the observer reaches only
    85 % of its relaxation by ``T_o`` and misses the calibration band. Stating
    it as a number keeps the manifest honest about which it is.

    ``delta_o`` is the regularization width. The document ties it to the
    exchange period, which was written when that period was 1 s; it is a
    parameter here so the tie can be reported rather than assumed.
    """

    T_o: float = 30.0               # the observer's prescribed deadline
    alpha_0: float = 1.0            # the document derives 0.1; see above
    delta_o: float = 0.125          # regularization width, the document's h_v
    kappa_o: float = 1e-3
    xi: float = 0.025
    c_0: float = 0.1
    #: Pinning coefficient. Nonzero at the one node that receives the
    #: reference, zero everywhere else.
    b: float = 0.0
    #: The reference, meaningful only where ``b > 0``. A node with ``b = 0``
    #: must not be configured with it: not knowing it is the point.
    rP: float = 0.0
    rQ: float = 0.0

    def __post_init__(self) -> None:
        if self.T_o <= 0 or self.delta_o <= 0:
            raise ValueError("T_o and delta_o must be > 0")
        if self.b < 0:
            raise ValueError(f"pinning coefficient must be >= 0, got {self.b}")
        if self.b == 0.0 and (self.rP or self.rQ):
            raise ValueError(
                "an unpinned node was given the reference; b = 0 means the "
                "node does not know it and must reach it through the graph")


class VirtualLayer:
    """The observer for one node. Degrees and weights come from the graph."""

    __slots__ = ("p", "degree", "D", "zP", "zQ", "c", "wP", "wQ")

    def __init__(self, params: VirtualParams, degree: int,
                 zP: float = 0.0, zQ: float = 0.0):
        self.p = params
        self.degree = int(degree)           # sum_j a_ij, unit weights
        self.D = float(degree) + params.b
        if self.D <= 0:
            raise ValueError("D_i = degree + b must be > 0; an isolated, "
                             "unpinned node has no way to reach the reference")
        self.zP, self.zQ = float(zP), float(zQ)
        self.c = float(params.c_0)
        # Before the first packet a node has heard nobody, so the only term it
        # can form is its own pinned reference. An unpinned node then has
        # w = 0 and eta = D z, which decays it toward zero until a neighbour
        # is heard; that is the same behaviour an empty receive cache gives.
        self.wP = params.b * params.rP
        self.wQ = params.b * params.rQ

    # --- the held snapshot ---------------------------------------------------
    def hold(self, neighbours: Iterable[Sequence[float]]) -> None:
        """Freeze ``w_i = sum_j a_ij z~_ij + b_i r`` for this tick.

        ``neighbours`` yields the decoded, held ``(z_P, z_Q)`` of each
        neighbour, in any order. Unit edge weights, so the sum is plain. A
        neighbour whose packet was lost contributes its last value and not
        nothing: dropping the term would change the graph, holding it does
        not.
        """
        sP = sQ = 0.0
        n = 0
        for zP, zQ in neighbours:
            sP += zP
            sQ += zQ
            n += 1
        if n != self.degree:
            raise ValueError(f"expected {self.degree} neighbour values, got {n}")
        self.wP = sP + self.p.b * self.p.rP
        self.wQ = sQ + self.p.b * self.p.rQ

    # --- the regularized schedule -------------------------------------------
    def alpha(self, t_s: float) -> float:
        return self.p.alpha_0 * self.p.T_o / max(self.p.T_o - t_s, self.p.delta_o)

    def coefficients(self, t_s: float) -> tuple[float, float]:
        """``(f_o, rho_o)`` at time ``t``."""
        a = self.alpha(t_s)
        return a + self.p.kappa_o, 2.0 * a

    def theta(self, t_s: float, h: float) -> float:
        """The convex weight, for diagnostics. Always in [0, 1)."""
        f_o, _ = self.coefficients(t_s)
        x = h * self.c * f_o * self.D
        return x / (1.0 + x)

    # --- one local step ------------------------------------------------------
    def candidate(self, t_s: float, h: float):
        """``(zP+, zQ+, c+, gP, gQ, etaP, etaQ)``, computed but not committed.

        The interface is evaluated on the old ``z_i``, so nothing is written
        back here; the caller commits after the command has been formed.
        """
        f_o, rho_o = self.coefficients(t_s)
        a_o = self.c * f_o
        den = 1.0 + h * a_o * self.D
        etaP = self.D * self.zP - self.wP
        etaQ = self.D * self.zQ - self.wQ
        k = -a_o / den
        gP, gQ = k * etaP, k * etaQ
        zP_plus = self.zP + h * gP
        zQ_plus = self.zQ + h * gQ
        c_plus = self.c + h * rho_o * self.p.xi * f_o * f_o * (
            etaP * etaP + etaQ * etaQ)
        return zP_plus, zQ_plus, c_plus, gP, gQ, etaP, etaQ

    def commit(self, zP: float, zQ: float, c: float) -> None:
        self.zP, self.zQ, self.c = zP, zQ, c

    def packet(self) -> tuple[float, float]:
        """What crosses the network: the virtual state, and nothing else.

        The bound estimator was removed exactly and the observer gain ``c_i``
        is local, so there is no third component to send.
        """
        return self.zP, self.zQ
