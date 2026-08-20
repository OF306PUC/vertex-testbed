"""Time sources: wall-clock for hardware, virtual for simulation.
"""

from __future__ import annotations

import asyncio
import heapq
import itertools
import time
from typing import Protocol, runtime_checkable

__all__ = ["Clock", "WallClock", "VirtualClock"]


@runtime_checkable
class Clock(Protocol):
    """Time and delay, injectable so the agent is testable without waiting."""

    def now_s(self) -> float:
        """Monotonic seconds, for scheduling."""

    def now_us(self) -> int:
        """Microseconds since the experiment epoch, for packet timestamps."""

    async def sleep(self, seconds: float) -> None:
        """Yield for ``seconds``."""


class WallClock:
    """Real time. ``now_us`` is anchored to an explicit experiment epoch.
    """

    __slots__ = ("epoch_unix_s",)

    def __init__(self, epoch_unix_s: float) -> None:
        self.epoch_unix_s = float(epoch_unix_s)

    def now_s(self) -> float:
        return time.monotonic()

    def now_us(self) -> int:
        # time.time(), not monotonic: this value is compared against timestamps
        # produced on other machines, and only the synchronised wall clock has a
        # shared origin. Susceptible to step corrections, which is exactly why it
        # is not used for scheduling.
        return int(round((time.time() - self.epoch_unix_s) * 1e6))

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(max(0.0, seconds))

    def __repr__(self) -> str:      # pragma: no cover
        return f"WallClock(epoch_unix_s={self.epoch_unix_s!r})"


class VirtualClock:
    """Simulated time that jumps to the next scheduled wake-up.

    ``sleep`` parks the caller on a heap keyed by virtual deadline; :meth:`advance`
    moves time to the earliest deadline and wakes everything due at it.
    """

    __slots__ = ("_t", "_waiters", "_counter")

    def __init__(self, start_s: float = 0.0) -> None:
        self._t = float(start_s)
        self._waiters: list[tuple[float, int, asyncio.Future]] = []
        self._counter = itertools.count()

    # Clock protocol: 
    def now_s(self) -> float:
        return self._t

    def now_us(self) -> int:
        return int(round(self._t * 1e6))

    async def sleep(self, seconds: float) -> None:
        if seconds <= 0:
            await asyncio.sleep(0)      # still yield, so a zero delay cannot spin
            return
        fut = asyncio.get_running_loop().create_future()
        heapq.heappush(self._waiters, (self._t + seconds, next(self._counter), fut))
        await fut

    # driving: 
    @property
    def pending(self) -> int:
        """How many sleepers are parked."""
        return len(self._waiters)

    @property
    def next_deadline_s(self) -> float | None:
        return self._waiters[0][0] if self._waiters else None

    async def settle(self, expected_sleepers: int, *, max_spins: int = 10_000) -> bool:
        """Yield until ``expected_sleepers`` are parked.
        """
        for _ in range(max_spins):
            if len(self._waiters) >= expected_sleepers:
                return True
            await asyncio.sleep(0)
        return False

    def advance(self) -> float | None:
        """Jump to the earliest deadline and wake everything due at it.
        """
        if not self._waiters:
            return None
        deadline = self._waiters[0][0]
        self._t = deadline
        while self._waiters and self._waiters[0][0] <= deadline:
            _, _, fut = heapq.heappop(self._waiters)
            if not fut.done():
                fut.set_result(None)
        return self._t

    def cancel_all(self) -> None:
        """Wake every sleeper so tasks can unwind at shutdown."""
        while self._waiters:
            _, _, fut = heapq.heappop(self._waiters)
            if not fut.done():
                fut.set_result(None)

    def __repr__(self) -> str:      # pragma: no cover
        return f"VirtualClock(t={self._t:.6f}, pending={len(self._waiters)})"
