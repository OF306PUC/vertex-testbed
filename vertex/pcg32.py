"""PCG32, matching firmware/nordic/src/prng.c bit for bit.

Exists so the C control law and the Python control law can be driven by the
*same* noise sequence. The firmware previously used an unseeded ``rand()``: not
reproducible, and implementation-defined, so no amount of seeding would have made
the two comparable. See ``docs/FIRMWARE_DIVERGENCE.md``.

Not the platform default. ``Disturbance`` still seeds numpy's PCG64 unless a
``uniform`` callable is injected -- this is that callable::

    Disturbance(params, uniform=Pcg32(seed).uniform)

PCG64 is the better generator; PCG32 is the one a Cortex-M4 without a 128-bit
multiply can reproduce. Which one a run uses is an experiment decision, recorded
in ``RunMeta``, not something this module makes.
"""

from __future__ import annotations

__all__ = ["Pcg32"]

_MULT = 6364136223846793005
_MASK64 = (1 << 64) - 1
_MASK32 = (1 << 32) - 1


class Pcg32:
    """O'Neill's minimal xsh-rr 64/32."""

    __slots__ = ("_state", "_inc")

    def __init__(self, state: int, sequence: int = 0) -> None:
        self._state = 0
        self._inc = ((sequence << 1) | 1) & _MASK64
        self.u32()
        self._state = (self._state + (state & _MASK64)) & _MASK64
        self.u32()

    def u32(self) -> int:
        old = self._state
        self._state = (old * _MULT + self._inc) & _MASK64
        xorshifted = (((old >> 18) ^ old) >> 27) & _MASK32
        rot = (old >> 59) & 31
        return ((xorshifted >> rot) | (xorshifted << ((32 - rot) & 31))) & _MASK32

    def uniform(self) -> float:
        """Uniform on [0, 1), 24-bit -- exactly representable in float32."""
        return (self.u32() >> 8) / 16777216.0
