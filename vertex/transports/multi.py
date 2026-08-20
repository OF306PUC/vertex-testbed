"""Several media behind one Transport -- what makes a `bridge` a bridge.

`ble` agents have a BLE radio and `wifi` agents have a socket, so **they share no
medium and can never exchange state**. A `ble`-to-`wifi` edge in a manifest is a
link that cannot carry a packet, however well the graph validates.

The `bridge` agent is what joins them: it publishes on both media and listens on
both. Without that, a manifest like `n30-clusters` -- where node 21 has both a BLE
and a Wi-Fi neighbour -- is silently unrunnable, and `Agent` holds exactly one
`Transport`, so there was nowhere for the second one to go.

Composing at the Transport seam rather than teaching `Agent` about lists keeps the
change to one class: `start`/`stop`/`publish` fan out, and receptions from every
member arrive at one callback. `Agent` is unchanged and does not know.

## What this costs, deliberately

A bridge transmits **every** packet twice, once per medium. That is roughly double
the TX airtime of a `ble` or `wifi` agent at the same publish period, and on the
CYW43455 the two transmissions contend for one antenna. It is not an inefficiency
to be optimised away: joining two media means being heard on both, and the airtime
is what §3 A2/A3 measures. It does mean a bridge is not airtime-comparable with the
single-medium agents, which belongs in the analysis rather than in this class.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from ..wire import StatePacket
from .base import Reception, ReceiveCallback, Transport

__all__ = ["MultiStats", "MultiTransport"]


@dataclass
class MultiStats:
    """Per-medium send accounting, keyed by the member's ``name``."""

    published: int = 0
    partial: int = 0                    # at least one medium refused
    failed: int = 0                     # every medium refused
    per_medium_errors: dict[str, int] = field(default_factory=dict)
    last_error: str | None = None

    def summary(self) -> str:
        errs = ", ".join(f"{k}={v}" for k, v in sorted(self.per_medium_errors.items()))
        return (f"published={self.published} partial={self.partial} "
                f"failed={self.failed}" + (f" errors[{errs}]" if errs else ""))


class MultiTransport(Transport):
    """Publish on every member medium; receive from all of them.

    Parameters
    ----------
    members:
        The transports to fan out over, in publish order. Two or more; a single
        medium does not need this wrapper and wrapping it only adds a layer to
        read through when something goes wrong.
    """

    name = "multi"

    def __init__(self, members: Sequence[Transport]) -> None:
        if len(members) < 2:
            raise ValueError(
                f"MultiTransport needs at least two media, got {len(members)}; "
                f"use the single transport directly"
            )
        names = [m.name for m in members]
        if len(set(names)) != len(names):
            # Two members with one name make the per-medium counters
            # unattributable, and two of the same medium is a configuration
            # mistake rather than a topology.
            raise ValueError(f"duplicate medium names {names}")
        self.members = list(members)
        self.stats = MultiStats()
        self._started: list[Transport] = []

    @property
    def media(self) -> list[str]:
        return [m.name for m in self.members]

    def __repr__(self) -> str:                                  # pragma: no cover
        return f"MultiTransport({'+'.join(self.media)})"

    # lifecycle:
    async def start(self, on_receive: ReceiveCallback) -> None:
        """Start every member, unwinding if one fails.

        A half-started bridge is worse than a stopped one: it would publish on one
        medium and log a delivery ratio for neighbours it cannot hear, which reads
        as radio loss. So a failure here stops whatever did start and raises.
        """
        for m in self.members:
            try:
                await m.start(on_receive)
            except Exception:
                for started in reversed(self._started):
                    try:
                        await started.stop()
                    except Exception:
                        pass
                self._started.clear()
                raise
            self._started.append(m)

    async def stop(self) -> None:
        """Stop every member. Safe to call twice, and stops the rest if one raises."""
        errors = []
        for m in reversed(self._started):
            try:
                await m.stop()
            except Exception as exc:
                errors.append(exc)
        self._started.clear()
        if errors:
            # Reported after the others are down: teardown must not leave a radio
            # running because an earlier one complained.
            self.stats.last_error = repr(errors[0])

    # send:
    async def publish(self, packet: StatePacket) -> None:
        """Send on every medium.

        Concurrently, and never raising. One slow medium must not delay the other
        past the publish period, and one refusing must not stop the other from
        being heard -- a partial send is degraded, not failed. Both outcomes are
        counted; the receiver is what measures delivery.
        """
        if not self._started:
            raise RuntimeError("publish() before start()")

        results = await asyncio.gather(
            *(m.publish(packet) for m in self._started),
            return_exceptions=True,
        )
        failures = 0
        for m, r in zip(self._started, results):
            if isinstance(r, BaseException):
                failures += 1
                self.stats.per_medium_errors[m.name] = (
                    self.stats.per_medium_errors.get(m.name, 0) + 1)
                self.stats.last_error = f"{m.name}: {r!r}"
        if failures == 0:
            self.stats.published += 1
        elif failures < len(self._started):
            self.stats.partial += 1
        else:
            self.stats.failed += 1

    # introspection:
    def member_stats(self) -> dict[str, object]:
        """Each member's own counters, for the run's status output."""
        out: dict[str, object] = {}
        for m in self.members:
            st = getattr(m, "stats", None)
            if st is not None:
                out[m.name] = st.summary() if hasattr(st, "summary") else repr(st)
        return out
