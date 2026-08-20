"""Numeric conventions: fixed-point quantization and the rounding rule.
"""
from __future__ import annotations

import math
from typing import Any

__all__ = ["SCALE_FACTOR", "INV_SCALE_FACTOR", "NAN", "round_half_up", "sign",
           "quantize", "dequantize", "to_finite_float", "is_finite_number"]

#: Fixed-point scale shared by the wire format, the logs, and the firmware.
SCALE_FACTOR = 1_000_000
INV_SCALE_FACTOR = 1e-6

NAN = float("nan")


def round_half_up(x: float) -> float:
    """Round to the nearest integer, ties away from zero toward +infinity.

    >>> round_half_up(2.5), round_half_up(-2.5), round_half_up(3.5)
    (3.0, -2.0, 4.0)
    >>> round_half_up(-0.5)
    -0.0

    Implemented by comparing the *fractional part*. The tempting
    ``floor(x + 0.5)`` is wrong twice over, and both failures hit the same input,
    ``x = 0.49999999999999994`` (the double immediately below 0.5):

    1. ``x + 0.5`` rounds up to exactly ``1.0``, so ``floor`` yields 1 where the
       nearest integer is 0.
    2. The obvious guard, rejecting a result when ``r - x > 0.5``, also fails --
       ``1 - 0.49999999999999994`` rounds to exactly ``0.5``, so the subtraction
       destroys the very information being tested.

    ``x - floor(x)`` is exact for every finite double, so this comparison cannot
    misfire.

    >>> round_half_up(0.49999999999999994)
    0.0
    """
    if math.isnan(x):
        return NAN
    if math.isinf(x):
        return x
    f = math.floor(x)
    r = f + 1 if (x - f) >= 0.5 else f
    # Preserve signed zero, so a negative value rounding to zero still reports
    # its sign to anything downstream that inspects it.
    if r == 0 and (x < 0 or (x == 0 and math.copysign(1.0, x) < 0)):
        return -0.0
    return float(r)


def sign(x: float) -> float:
    """Signum with ``sign(0) == 0``, preserving the sign of zero.
    
    >>> sign(5.0), sign(-5.0), sign(0.0)
    (1.0, -1.0, 0.0)
    """
    if math.isnan(x):
        return NAN
    if x > 0:
        return 1.0
    if x < 0:
        return -1.0
    return x


def quantize(value: float, scale: int = SCALE_FACTOR) -> int:
    """Engineering units -> scaled integer, for the wire and the logs."""
    return int(round_half_up(value * scale))


def dequantize(value: int | float, scale: int = SCALE_FACTOR) -> float:
    """Scaled integer -> engineering units."""
    return float(value) / scale


def is_finite_number(v: Any) -> bool:
    """True only for a real, finite ``int``/``float``. Booleans are excluded."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return False
    return math.isfinite(v)


def to_finite_float(v: Any, default: float = NAN) -> float:
    """Best-effort conversion of untrusted input to a finite float.

    >>> to_finite_float("2.5"), to_finite_float(3)
    (2.5, 3.0)
    >>> import math; math.isnan(to_finite_float("")), math.isnan(to_finite_float(None))
    (True, True)
    """
    if isinstance(v, bool) or v is None:
        return default
    if isinstance(v, (int, float)):
        return float(v) if math.isfinite(v) else default
    if isinstance(v, str):
        try:
            f = float(v.strip())
        except ValueError:
            return default
        return f if math.isfinite(f) else default
    return default
