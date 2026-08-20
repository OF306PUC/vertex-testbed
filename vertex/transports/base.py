"""The transport seam (best-effort broadcast at a rate): how an agent's state reaches its neighbours.

* :meth:`publish` sends to *all* neighbours at once and returns nothing. There is
  no per-neighbour addressing and no acknowledgement, so nothing here can report
  delivery -- loss is observed at the receiver, from sequence numbers.
* Reception is a **callback**, not a coroutine to await. Packets arrive when they
  arrive, unrelated to the control period, and a control loop must never block on
  one. The callback is expected to be cheap: record and return.
* :meth:`publish` is ``async`` because real radios block; implementations must not
  let a slow send stall the caller's period.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable

from ..wire import StatePacket

__all__ = ["Reception", "ReceiveCallback", "Transport"]


@dataclass(frozen=True, slots=True)
class Reception:
    """A packet as received, with the local arrival time.
    """
    packet: StatePacket
    rx_time_us: int


#: Called by a transport for each received packet. Must be cheap and must not raise.
ReceiveCallback = Callable[[Reception], None]


class Transport(ABC):
    """Best-effort broadcast of agent state."""

    #: Manifest-facing name.
    name: str = "abstract"

    @abstractmethod
    async def start(self, on_receive: ReceiveCallback) -> None:
        """Begin receiving, delivering each packet to ``on_receive``."""

    @abstractmethod
    async def stop(self) -> None:
        """Release radios, sockets and tasks. Must be safe to call twice.
        """

    @abstractmethod
    async def publish(self, packet: StatePacket) -> None:
        """Broadcast ``packet`` to every neighbour. Best-effort; no confirmation."""

    async def __aenter__(self) -> "Transport":
        raise NotImplementedError("start() needs a callback; use start/stop directly")

    def __repr__(self) -> str:      # pragma: no cover
        return f"{type(self).__name__}(name={self.name!r})"
