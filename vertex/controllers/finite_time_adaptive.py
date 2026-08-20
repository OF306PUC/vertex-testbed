"""Finite-time adaptive coordination law.
"""

from __future__ import annotations

import math
from typing import Any, Sequence

from ..numeric import (INV_SCALE_FACTOR, dequantize, is_finite_number, sign,
                       to_finite_float)
from .base import (Controller, ControllerOutput, ControllerParams, register)
from .disturbance import Disturbance

__all__ = ["FiniteTimeAdaptiveController"]


@register
class FiniteTimeAdaptiveController(Controller):
    """Finite-time coordination with an adaptive gain."""

    name = "finite_time_adaptive"

    def __init__(
        self,
        params: ControllerParams,
        *,
        seed: int | None = None,
        uniform=None,
    ) -> None:
        self.params = params
        self.disturbance = Disturbance(params.disturbance, seed=seed, uniform=uniform)
        self._load(params)
        self.reset()

    # config: 
    def _load(self, p: ControllerParams) -> None:
        self.dt = p.dt_s
        self.state0, self.vstate0, self.vartheta0 = p.state, p.vstate, p.vartheta
        self.eta, self.alpha, self.delta = p.eta, p.alpha, p.delta
        self.period_samples = int(p.disturbance.period_samples)

    def set_params(self, params: ControllerParams) -> None:
        """Update parameters mid-run, preserving integrator state.
        """
        self.params = params
        self._load(params)
        self.disturbance = Disturbance(
            params.disturbance,
            seed=self.disturbance.seed,
            uniform=None if self.disturbance.seed is not None else self.disturbance._uniform,
        )
        self.active = 0

    def reset(self) -> None:
        self._state = self.state0
        self._vstate = self.vstate0
        self._vartheta = self.vartheta0
        self.step_count = 0
        self.sigma = 0.0
        self.grad = 0.0
        self.gi = 0.0
        self.active = 0

    # introspection: ------------------------------------------------------------
    @property
    def vstate(self) -> float:
        return self._vstate

    @property
    def state(self) -> float:
        return self._state

    @property
    def vartheta(self) -> float:
        return self._vartheta

    # internals: ----------------------------------------------------------------
    def _sanitize(self) -> None:
        """Force non-finite integrator state back to zero.
        """
        if not math.isfinite(self._state):
            self._state = 0.0
        if not math.isfinite(self._vstate):
            self._vstate = 0.0
        if not math.isfinite(self._vartheta):
            self._vartheta = 0.0

    def _consensus_term(
        self, neighbor_vstates: Sequence[Any], neighbor_enabled: Sequence[Any]
    ) -> float:
        """Sum the neighbour coupling, skipping anything unusable.
        """
        vs = neighbor_vstates if isinstance(neighbor_vstates, (list, tuple)) else ()
        en = neighbor_enabled if isinstance(neighbor_enabled, (list, tuple)) else ()
        total = 0.0

        for j, raw in enumerate(vs):
            if j >= len(en) or not en[j]:
                continue
            vj = to_finite_float(raw)
            if not is_finite_number(vj):
                continue
            diff = self._vstate - vj * INV_SCALE_FACTOR
            if not math.isfinite(diff):
                continue
            # sign(0) == 0, so a neighbour already in agreement contributes nothing
            total += -sign(diff) * math.sqrt(abs(diff))
        return total

    def _emit(self) -> ControllerOutput:
        self._sanitize()
        return ControllerOutput(self._state, self._vstate, self._vartheta)

    # the control law: -----------------------------------------------------------
    def step(
        self, neighbor_vstates: Sequence[Any], neighbor_enabled: Sequence[Any]
    ) -> ControllerOutput:
        """Discrete-time update."""
        self._sanitize()

        nu = self.disturbance(self.step_count * self.dt) * self.dt

        self.gi = self.alpha * self._consensus_term(neighbor_vstates, neighbor_enabled)
        self.sigma = self._state - self._vstate
        self.grad = sign(self.sigma)

        u = self.gi - self._vartheta * self.grad
        dvtheta = 1.0 if abs(self.sigma) > self.delta else 0.0

        self._state = self._state + u + nu
        self._vstate = self._vstate + self.gi
        self._vartheta = self._vartheta + self.eta * dvtheta

        self.step_count = (self.step_count + 1) % self.period_samples
        return self._emit()

