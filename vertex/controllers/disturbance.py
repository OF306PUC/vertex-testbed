"""Exogenous disturbance signal.
"""

from __future__ import annotations

import math
from typing import Callable

import numpy as np

from .base import DisturbanceParams

__all__ = ["Disturbance"]


class Disturbance:
    """``nu(t) = noise_amplitude*(U - noise_offset) + beta + sine_amplitude*sin(...)``

    Parameters
    ----------
    params:
        Signal definition, in engineering units.
    seed:
        Seeds a PCG64 stream. Mutually exclusive with ``uniform``.
    uniform:
        Zero-argument callable returning a float in [0, 1). For injecting a
        specific stream -- reference comparisons, or replaying a recorded run.
    """

    __slots__ = ("params", "seed", "_uniform", "enabled", "noise_amplitude",
                 "noise_offset", "beta", "sine_amplitude", "sine_frequency_hz",
                 "sine_phase_s")

    def __init__(
        self,
        params: DisturbanceParams,
        *,
        seed: int | None = None,
        uniform: Callable[[], float] | None = None,
    ) -> None:
        if (seed is None) == (uniform is None):
            raise ValueError(
                "supply exactly one of `seed` or `uniform`; an unseeded "
                "disturbance is not reproducible"
            )
        self.params = params
        self.seed = seed
        self._uniform = uniform if uniform is not None else np.random.default_rng(seed).random

        self.enabled = bool(params.enabled)
        self.noise_amplitude = params.noise_amplitude
        self.noise_offset = params.noise_offset
        self.beta = params.beta
        self.sine_amplitude = params.sine_amplitude
        self.sine_frequency_hz = params.sine_frequency_hz
        self.sine_phase_s = params.sine_phase_s

    def __call__(self, t_s: float) -> float:
        """Disturbance value at time ``t_s`` seconds; 0.0 when disabled.
        """
        if not self.enabled:
            return 0.0
        noise = self.noise_amplitude * (self._uniform() - self.noise_offset)
        sinusoid = self.sine_amplitude * math.sin(
            2.0 * math.pi * self.sine_frequency_hz * (t_s - self.sine_phase_s)
        )
        return noise + self.beta + sinusoid
