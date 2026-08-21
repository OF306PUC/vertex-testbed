"""One agent: a controller, a transport, and two independent periodic loops.

    control loop   every ``dt_s``              -- read the table, step the law
    publish loop   every ``publish_period_s``  -- broadcast the virtual state
"""

from __future__ import annotations

import asyncio
from typing import Callable
from dataclasses import dataclass, field

from ..clock import Clock
from ..controllers.base import Controller, ControllerOutput
from ..transports.base import Reception, Transport
from ..wire import StatePacket
from .neighbors import NeighborTable

__all__ = ["AgentConfig", "LoopTiming", "StateSample", "Agent"]


@dataclass(frozen=True, slots=True)
class AgentConfig:
    """Everything an agent needs that is not the controller or the transport."""

    node_id: int
    neighbor_ids: tuple[int, ...]
    dt_s: float
    publish_period_s: float
    enabled: bool = True
    #: How long a neighbour's last packet stays usable. Defaults to three publish
    #: periods, so a single lost packet does not eject a neighbour but a genuinely
    #: silent one is dropped promptly.
    max_neighbor_age_s: float | None = None

    def resolved_max_age_s(self) -> float:
        return (self.max_neighbor_age_s if self.max_neighbor_age_s is not None
                else 3.0 * self.publish_period_s)


@dataclass
class LoopTiming:
    """Scheduling error for one loop, in seconds."""

    name: str
    period_s: float
    iterations: int = 0
    late_iterations: int = 0
    max_error_s: float = 0.0
    total_error_s: float = 0.0
    overruns: int = 0

    def record(self, error_s: float, *, threshold_s: float) -> None:
        self.iterations += 1
        self.total_error_s += error_s
        if error_s > self.max_error_s:
            self.max_error_s = error_s
        if error_s > threshold_s:
            self.late_iterations += 1
        if error_s > self.period_s:
            self.overruns += 1          # a whole period was missed

    @property
    def mean_error_s(self) -> float:
        return self.total_error_s / self.iterations if self.iterations else 0.0

    def summary(self) -> str:
        return (f"{self.name}: {self.iterations} iters, "
                f"mean err {self.mean_error_s * 1e3:.3f} ms, "
                f"max {self.max_error_s * 1e3:.3f} ms, "
                f"late {self.late_iterations}, overruns {self.overruns}")


@dataclass(frozen=True, slots=True)
class StateSample:
    """One logged control step."""

    t_s: float
    state: float
    vstate: float
    vartheta: float
    neighbor_vstates: tuple[int, ...]
    neighbor_enabled: tuple[bool, ...]


class Agent:
    """A single coordination agent."""

    def __init__(
        self,
        config: AgentConfig,
        controller: Controller,
        transport: Transport,
        clock: Clock,
        *,
        record_history: bool = True,
    ) -> None:
        self.config = config
        self.controller = controller
        self.transport = transport
        self.clock = clock
        self.neighbors = NeighborTable(
            config.neighbor_ids, max_age_s=config.resolved_max_age_s()
        )
        self.control_timing = LoopTiming("control", config.dt_s)
        self.publish_timing = LoopTiming("publish", config.publish_period_s)
        self.history: list[StateSample] = []
        self._record_history = record_history
        # Called after every control step, if set. Keeps persistence out of the
        # agent while still running inside the control period, so a sample cannot
        # be lost between the step and the record.
        self.on_sample: Callable[[float, ControllerOutput, list[int], list[bool]],
                                 None] | None = None
        self._seq = 0
        self._published = 0
        self._running = False
        self._t0_s: float | None = None

    # lifecycle:
    async def start(self) -> None:
        await self.transport.start(self._on_receive)
        self._t0_s = self.clock.now_s()
        self._running = True

    async def stop(self) -> None:
        self._running = False
        await self.transport.stop()

    @property
    def elapsed_s(self) -> float:
        return 0.0 if self._t0_s is None else self.clock.now_s() - self._t0_s

    def _on_receive(self, reception: Reception) -> None:
        """Transport callback. Cheap, synchronous, and must not raise.
        """
        try:
            self.neighbors.observe(reception)
        except Exception:                                   # pragma: no cover
            pass

    # loops:
    async def run_control_loop(self, *, iterations: int | None = None) -> None:
        """Step the controller every ``dt_s``."""
        await self._run_periodic(
            period_s=self.config.dt_s, timing=self.control_timing,
            body=self._control_step, iterations=iterations,
        )

    async def run_publish_loop(self, *, iterations: int | None = None) -> None:
        """Broadcast the virtual state every ``publish_period_s``."""
        await self._run_periodic(
            period_s=self.config.publish_period_s, timing=self.publish_timing,
            body=self._publish_step, iterations=iterations,
        )

    async def _run_periodic(self, *, period_s, timing, body, iterations) -> None:
        next_deadline = self.clock.now_s() + period_s
        done = 0
        threshold = 0.1 * period_s
        while iterations is None or done < iterations:
            delay = next_deadline - self.clock.now_s()
            # Floor the sleep at a fraction of the period. If a body overruns
            # badly, sleeping a negative amount would spin and starve the other
            # loop; yielding a slice keeps both alive and lets `overruns` report it.
            await self.clock.sleep(max(0.1 * period_s, delay))

            now = self.clock.now_s()
            timing.record(max(0.0, now - next_deadline), threshold_s=threshold)
            body(now)
            done += 1
            # Advance from the previous deadline, not from now, so drift does not
            # accumulate; skip ahead if we fell more than a period behind.
            next_deadline += period_s
            if next_deadline < now:
                missed = int((now - next_deadline) // period_s) + 1
                next_deadline += missed * period_s

    # loop bodies:
    def _control_step(self, now_s: float) -> ControllerOutput:
        now_us = self.clock.now_us()
        vstates, enabled = self.neighbors.snapshot(now_us)
        if self.config.enabled:
            out = self.controller.step(vstates, enabled)
        else:
            # A disabled agent still runs its clock and still publishes, so
            # neighbours see it as present-but-frozen rather than dead. That is
            # what makes a mid-run enable/disable a controlled perturbation
            # instead of an indistinguishable link failure.
            out = ControllerOutput(self.controller.vstate, self.controller.vstate, 0.0)
        t_s = now_s - (self._t0_s or 0.0)
        if self._record_history:
            self.history.append(StateSample(
                t_s=t_s,
                state=out.state, vstate=out.vstate, vartheta=out.vartheta,
                neighbor_vstates=tuple(vstates), neighbor_enabled=tuple(enabled),
            ))
        if self.on_sample is not None:
            # arrivals(), not freshness(): the log wants "a packet arrived since
            # the last sample", which is what the nRF's `fresh` bit means. The
            # staleness test above still drives the controller.
            self.on_sample(t_s, out, vstates, self.neighbors.arrivals())
        return out

    def _publish_step(self, now_s: float) -> None:
        self._seq = (self._seq + 1) % 0x1_0000
        packet = StatePacket.from_state(
            self.config.node_id, self.controller.vstate,
            seq=self._seq, tx_time_us=max(0, self.clock.now_us()),
            enabled=self.config.enabled,
        )
        # Fire-and-forget: publishing must not delay the next control step. A radio
        # that blocks is the transport's problem to bound, not this loop's to wait on.
        asyncio.get_running_loop().create_task(self._publish(packet))

    async def _publish(self, packet: StatePacket) -> None:
        try:
            await self.transport.publish(packet)
            self._published += 1
        except Exception:                                   # pragma: no cover
            pass        # a failed broadcast is loss, which the receiver measures

    # reporting:
    @property
    def published(self) -> int:
        return self._published

    def timing_report(self) -> str:
        return f"{self.control_timing.summary()}\n{self.publish_timing.summary()}"

    def __repr__(self) -> str:      # pragma: no cover
        return (f"Agent(node_id={self.config.node_id}, "
                f"neighbors={self.config.neighbor_ids}, "
                f"z={self.controller.vstate:.6f})")
