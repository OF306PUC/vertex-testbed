"""In-process broadcast bus, for simulation and tests.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Callable

import numpy as np

from ..clock import Clock
from ..wire import StatePacket, decode_any, encode_manufacturer_data
from ..wire.codec import decode_manufacturer_data
from .base import Reception, ReceiveCallback, Transport

__all__ = ["LoopbackBus", "LoopbackTransport"]


@dataclass
class LoopbackBus:
    """Shared medium. Create one per simulation and hand it to every transport.

    ``loss`` is the probability an individual *delivery* is dropped, evaluated per
    receiver -- not per send. 
    """

    clock: Clock
    loss: float = 0.0
    delay_s: float = 0.0
    serialise: bool = True
    seed: int = 0
    _subscribers: dict[int, ReceiveCallback] = field(default_factory=dict, repr=False)
    _rng: np.random.Generator | None = field(default=None, repr=False)
    _sent: int = 0
    _delivered: int = 0
    _dropped: int = 0

    def __post_init__(self) -> None:
        if not 0.0 <= self.loss <= 1.0:
            raise ValueError(f"loss must be in [0, 1], got {self.loss}")
        if self.delay_s < 0:
            raise ValueError(f"delay_s must be >= 0, got {self.delay_s}")
        self._rng = np.random.default_rng(self.seed)

    def subscribe(self, node_id: int, callback: ReceiveCallback) -> None:
        if node_id in self._subscribers:
            raise ValueError(f"node {node_id} is already attached to this bus")
        self._subscribers[node_id] = callback

    def unsubscribe(self, node_id: int) -> None:
        self._subscribers.pop(node_id, None)

    async def broadcast(self, sender_id: int, packet: StatePacket) -> None:
        """Deliver to every subscriber except the sender."""
        self._sent += 1
        wire = encode_manufacturer_data(packet) if self.serialise else None

        for node_id, callback in list(self._subscribers.items()):
            if node_id == sender_id:
                continue                      # an agent does not hear itself
            if self.loss and float(self._rng.random()) < self.loss:
                self._dropped += 1
                continue
            if self.delay_s:
                asyncio.get_running_loop().create_task(
                    self._deliver_later(callback, wire, packet)
                )
            else:
                self._deliver(callback, wire, packet)

    async def _deliver_later(self, callback, wire, packet) -> None:
        await self.clock.sleep(self.delay_s)
        self._deliver(callback, wire, packet)

    def _deliver(self, callback: ReceiveCallback, wire: bytes | None,
                 packet: StatePacket) -> None:
        # Round-trip through the codec so simulation exercises the real format.
        delivered = decode_manufacturer_data(wire) if wire is not None else packet
        callback(Reception(packet=delivered, rx_time_us=self.clock.now_us()))
        self._delivered += 1

    @property
    def counters(self) -> dict[str, int]:
        return {"sent": self._sent, "delivered": self._delivered, "dropped": self._dropped}


class LoopbackTransport(Transport):
    """One agent's attachment to a :class:`LoopbackBus`."""

    name = "loopback"

    def __init__(self, bus: LoopbackBus, node_id: int) -> None:
        self.bus = bus
        self.node_id = node_id
        self._started = False

    async def start(self, on_receive: ReceiveCallback) -> None:
        if self._started:
            return
        self.bus.subscribe(self.node_id, on_receive)
        self._started = True

    async def stop(self) -> None:
        self.bus.unsubscribe(self.node_id)
        self._started = False

    async def publish(self, packet: StatePacket) -> None:
        if not self._started:
            raise RuntimeError("publish() before start()")
        await self.bus.broadcast(self.node_id, packet)
