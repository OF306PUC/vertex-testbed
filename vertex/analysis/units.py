"""Unit systems for logged state, and normalisation between them.

Two representations of the same quantity exist in this platform, and a single
experiment contains both:

``engineering``
    Plain floats -- a virtual state of ``22.3`` is ``22.3``. What the control law,
    the manifests and the analysis work in.
``scaled_int``
    Integers scaled by :data:`~vertex.numeric.SCALE_FACTOR`, so ``22.3`` is
    ``22300000``. What the radio payload carries and what the microcontroller
    computes in, because it has no reason to pay for floating point.
"""

from __future__ import annotations

from typing import Any, Iterable, Literal

from ..numeric import SCALE_FACTOR, dequantize, quantize

__all__ = ["ENGINEERING", "SCALED_INT", "UNIT_SYSTEMS", "LEGACY_UNITS",
           "UnitSystem", "UnitMismatch", "detect_units", "convert_value",
           "normalize_run", "assert_consistent_units", "STATE_COLUMNS"]

ENGINEERING = "engineering"
SCALED_INT = "scaled_int"
UNIT_SYSTEMS = (ENGINEERING, SCALED_INT)


LEGACY_UNITS = SCALED_INT

UnitSystem = Literal["engineering", "scaled_int"]

#: Columns holding a state quantity, and therefore subject to scaling. Timestamps
#: are seconds and freshness flags are booleans; converting either would be wrong.
STATE_COLUMNS = ("state", "vstate", "vartheta")


class UnitMismatch(ValueError):
    """Runs in different unit systems were combined without normalising."""


def detect_units(payload: dict[str, Any]) -> str:
    """Read the unit system from a run payload, defaulting to legacy.
    """
    meta = payload.get("meta") or {}
    declared = meta.get("units")
    if declared in UNIT_SYSTEMS:
        return declared
    return LEGACY_UNITS


def convert_value(value: float | int | None, frm: str, to: str) -> float | int | None:
    """Convert one state quantity between unit systems."""
    if value is None or frm == to:
        return value
    if frm == SCALED_INT and to == ENGINEERING:
        return dequantize(value)
    if frm == ENGINEERING and to == SCALED_INT:
        scaled = quantize(value)
        if not -(1 << 31) <= scaled <= (1 << 31) - 1:
            raise ValueError(
                f"{value} does not fit int32 at scale {SCALE_FACTOR}: the "
                f"representable range is +-{((1 << 31) - 1) / SCALE_FACTOR:.6f}. "
                "Refusing to clamp -- a clamped trajectory is indistinguishable "
                "from one that genuinely plateaued."
            )
        return scaled
    raise ValueError(f"unknown conversion {frm!r} -> {to!r}")


def normalize_run(payload: dict[str, Any], *, to: str = ENGINEERING) -> dict[str, Any]:
    """Return a copy of a run payload with state columns in ``to``.
    """
    if to not in UNIT_SYSTEMS:
        raise ValueError(f"unknown unit system {to!r}; choose from {UNIT_SYSTEMS}")
    frm = detect_units(payload)
    out = dict(payload)
    meta = dict(out.get("meta") or {})
    meta["units"] = to
    meta["units_converted_from"] = frm
    out["meta"] = meta

    data = out.get("data") or {}
    if frm == to:
        return out

    converted: dict[str, Any] = {}
    for name, column in data.items():
        # Both time columns and the freshness flags are named explicitly rather
        # than left to the fall-through: seconds are not a scaled state, and a
        # column that survives conversion by accident survives it only until
        # someone adds a branch above.
        if name in ("timestamp", "device_timestamp") or name.startswith("rx_"):
            converted[name] = column
        elif name in STATE_COLUMNS or name.isdigit():
            converted[name] = [convert_value(v, frm, to) for v in column]
        else:
            converted[name] = column
    out["data"] = converted
    return out


def assert_consistent_units(payloads: Iterable[dict[str, Any]]) -> str:
    """Check that every run shares one unit system, and return it.
    """
    seen: dict[str, list[Any]] = {}
    for p in payloads:
        u = detect_units(p)
        node = (p.get("meta") or {}).get("node_id", "?")
        seen.setdefault(u, []).append(node)
    if not seen:
        return ENGINEERING
    if len(seen) > 1:
        detail = "; ".join(f"{u}: nodes {sorted(map(str, n))}" for u, n in seen.items())
        raise UnitMismatch(
            f"runs use more than one unit system ({detail}). Normalise with "
            "normalize_run() before combining -- plotting them together would "
            f"separate the groups by a factor of {SCALE_FACTOR}."
        )
    return next(iter(seen))
