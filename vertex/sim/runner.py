"""Driving a :class:`~vertex.clock.VirtualClock` to completion.

The loop is: let every task park on a sleep, then jump time to the earliest
deadline. Two details are load-bearing.

**Never advance time while a task is runnable.** Doing so executes that task in the
*next* time step, silently reordering the simulation and producing results that
depend on event-loop scheduling rather than on the model.

**Shut down by cancelling tasks, not by waking them.** Waking a sleeper just lets
it loop round and sleep again, with nobody left to advance the clock -- the driver
then waits forever. Cancellation propagates ``CancelledError`` through the parked
future so each loop unwinds through its own ``finally``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Sequence

from ..clock import VirtualClock

__all__ = ["SimResult", "drive_virtual_clock"]


def _other_live_tasks() -> set[asyncio.Task]:
    """Every unfinished task on the loop except the driver itself.

    Counting *all* loop tasks rather than only the ones the driver started is
    essential, because tasks appear dynamically during a run: the agent publishes
    fire-and-forget, and a delaying medium schedules each delivery as its own task.
    A driver that only knew about its own tasks could satisfy its parked-count
    target while such a task was still runnable, advance time past it, and deliver
    the packet at the wrong virtual instant -- or, with a delay configured, never.
    """
    current = asyncio.current_task()
    return {t for t in asyncio.all_tasks() if t is not current and not t.done()}


async def _park_all(
    clock: VirtualClock, tasks: list[asyncio.Task], max_spins: int
) -> tuple[list[asyncio.Task], bool]:
    """Yield until every still-running task on the loop is parked on the clock.

    Liveness is recomputed on every spin, and that matters twice over: a task that
    *finishes* never parks, so a count taken up front could never be reached and a
    completed run would be misreported as a stall; and new tasks may appear mid-spin.
    """
    for _ in range(max_spins):
        primary = [t for t in tasks if not t.done()]
        if not primary:
            return primary, True
        if clock.pending >= len(_other_live_tasks()):
            return primary, True
        await asyncio.sleep(0)
    return [t for t in tasks if not t.done()], False


@dataclass
class SimResult:
    """Outcome of a simulated run."""

    end_time_s: float
    steps: int
    completed: bool = False
    stalled: bool = False
    errors: list[BaseException] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors and not self.stalled

    def summary(self) -> str:
        state = ("completed" if self.completed else
                 "stalled" if self.stalled else "stopped at horizon")
        out = f"{state}: t={self.end_time_s:.3f}s after {self.steps} clock steps"
        if self.errors:
            out += f"; {len(self.errors)} task error(s): {self.errors[0]!r}"
        return out


async def drive_virtual_clock(
    clock: VirtualClock,
    factories: Sequence[Callable[[], Awaitable[None]]],
    *,
    until_s: float,
    max_steps: int = 2_000_000,
    spin_limit: int = 1_000,
) -> SimResult:
    """Run ``factories`` as concurrent tasks under ``clock`` until ``until_s``.

    ``factories`` are zero-argument callables returning coroutines, rather than
    coroutines themselves, so nothing starts running before the driver is ready to
    control the clock.

    ``max_steps`` bounds the driver against a task that sleeps for zero time in a
    loop -- that would advance the clock by nothing and spin forever, which is a
    modelling bug worth reporting rather than hanging on. ``spin_limit`` bounds how
    long we wait for tasks to park before declaring a stall; exceeding it means a
    task is awaiting something the clock does not control.
    """
    tasks = [asyncio.create_task(f()) for f in factories]
    result = SimResult(end_time_s=clock.now_s(), steps=0)

    try:
        while result.steps < max_steps:
            # Wait for every live task to park. If they do not, some task is
            # busy-looping without awaiting the clock, and advancing now would
            # corrupt the ordering.
            alive, parked = await _park_all(clock, tasks, spin_limit)
            if not alive:
                result.completed = True
                break
            if not parked:
                result.stalled = True
                break

            nxt = clock.next_deadline_s
            if nxt is None:
                result.stalled = True       # alive but nothing scheduled: deadlock
                break
            if nxt > until_s:
                break                       # horizon reached; not an error

            clock.advance()
            result.steps += 1
        else:
            result.stalled = True           # hit max_steps
    finally:
        for t in tasks:
            t.cancel()
        outcomes = await asyncio.gather(*tasks, return_exceptions=True)
        result.errors = [o for o in outcomes
                         if isinstance(o, BaseException)
                         and not isinstance(o, asyncio.CancelledError)]
        result.end_time_s = clock.now_s()

    return result
