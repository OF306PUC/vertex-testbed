"""The neighbour table: what the control loop reads instead of the network.

Under a push model, packets arrive whenever they arrive and the control loop runs
on its own period. Something has to bridge those two rates, and this is it: the
table is written by the transport callback and read synchronously by the control
loop, which therefore never awaits the network.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..transports.base import Reception
from ..wire import LinkMonitor, LinkStats

__all__ = ["NeighborRecord", "NeighborTable"]


@dataclass(slots=True)
class NeighborRecord:
    """Latest known state of one neighbour."""

    vstate: int                 # scaled, as received
    seq: int
    tx_time_us: int
    rx_time_us: int
    sender_enabled: bool
    updates: int = 0

    def age_us(self, now_us: int) -> int:
        return now_us - self.rx_time_us


class NeighborTable:
    """Latest state per neighbour, with freshness accounting."""

    __slots__ = ("neighbor_ids", "max_age_us", "monitor", "_records", "_arrived",
                 "_ignored", "_first_seen_us")

    def __init__(
        self,
        neighbor_ids: list[int] | tuple[int, ...],
        *,
        max_age_s: float,
        monitor: LinkMonitor | None = None,
    ) -> None:
        if max_age_s <= 0:
            raise ValueError(f"max_age_s must be > 0, got {max_age_s}")
        self.neighbor_ids = tuple(neighbor_ids)
        self.max_age_us = int(round(max_age_s * 1e6))
        self.monitor = monitor if monitor is not None else LinkMonitor()
        self._records: dict[int, NeighborRecord] = {}
        #: Neighbours heard since the last `arrivals()` call. Set on receipt,
        #: cleared on read -- the same "since the last report" semantics the nRF's
        #: `fresh` bit has, so the two agent types' logged columns mean one thing.
        self._arrived: set[int] = set()
        self._ignored = 0
        self._first_seen_us: dict[int, int] = {}

    # write side: called from the transport callback
    def observe(self, reception: Reception) -> bool:
        """Record a packet. Returns False if it was not from a declared neighbour 
           (enforced topology due information broadcast).
        """
        pkt = reception.packet
        if pkt.node_id not in self.neighbor_ids:
            self._ignored += 1
            return False

        self._arrived.add(pkt.node_id)
        self._first_seen_us.setdefault(pkt.node_id, reception.rx_time_us)
        self.monitor.observe(pkt, rx_time_us=reception.rx_time_us)

        prev = self._records.get(pkt.node_id)
        self._records[pkt.node_id] = NeighborRecord(
            vstate=pkt.vstate, seq=pkt.seq, tx_time_us=pkt.tx_time_us,
            rx_time_us=reception.rx_time_us, sender_enabled=pkt.enabled,
            updates=(prev.updates + 1) if prev else 1,
        )
        return True

    # read side: called from the control loop, synchronously 
    def snapshot(self, now_us: int) -> tuple[list[int], list[bool]]:
        """``(vstates, enabled)`` aligned to ``neighbor_ids``.
        """
        vstates: list[int] = []
        enabled: list[bool] = []
        for nid in self.neighbor_ids:
            rec = self._records.get(nid)
            if rec is None:
                vstates.append(0)
                enabled.append(False)
                continue
            fresh = rec.age_us(now_us) <= self.max_age_us
            vstates.append(rec.vstate)
            enabled.append(bool(fresh and rec.sender_enabled))
        return vstates, enabled

    # introspection:
    def freshness(self, now_us: int) -> list[bool]:
        # Distinct from snapshot()'s `enabled`, which also folds in whether the
        # sender declared itself enabled. Logging needs them separated: a stale
        # link and a disabled neighbour look identical to the controller but mean
        # opposite things when reading results.
        out = []
        for nid in self.neighbor_ids:
            rec = self._records.get(nid)
            out.append(bool(rec is not None and rec.age_us(now_us) <= self.max_age_us))
        return out

    def arrivals(self) -> list[bool]:
        """Which neighbours were heard since the last call. Read-and-clear.

        This is what the LOG records, and it is deliberately not `freshness()`.
        The two answer different questions and both are needed:

            freshness()  is the value younger than max_neighbor_age_s?
                         A staleness test -- what the CONTROLLER needs, because a
                         neighbour whose value is 200 ms old must not be dropped
                         from the coupling term just because nothing arrived in the
                         last control period.
            arrivals()   did a packet arrive since the last sample?
                         An arrival test -- what the LOG needs, and exactly what
                         the nRF's `fresh` bit already means.

        Logging the staleness test made the same column mean two different things
        on the two agent types. It only became visible once the nRF reported five
        times faster than anyone published: the relay's column read 0.32 (an
        arrival flag against a 40 ms window) while a Pi agent's read 0.996 (a
        staleness flag against a 600 ms window), and the two were being compared
        as though they measured the same thing.
        """
        out = [nid in self._arrived for nid in self.neighbor_ids]
        self._arrived.clear()
        return out

    def fresh_count(self, now_us: int) -> int:
        return sum(self.snapshot(now_us)[1])

    @property
    def heard(self) -> tuple[int, ...]:
        return tuple(nid for nid in self.neighbor_ids if nid in self._records)

    @property
    def missing(self) -> tuple[int, ...]:
        """Declared neighbours never heard from -- the first thing to check on a
        run that will not converge."""
        return tuple(nid for nid in self.neighbor_ids if nid not in self._records)

    @property
    def ignored(self) -> int:
        return self._ignored

    def records(self) -> dict[int, NeighborRecord]:
        return dict(self._records)

    def link_stats(self) -> dict[int, LinkStats]:
        return self.monitor.report()

    def __repr__(self) -> str:      # pragma: no cover
        return (f"NeighborTable(ids={self.neighbor_ids}, heard={self.heard}, "
                f"missing={self.missing}, ignored={self._ignored})")
