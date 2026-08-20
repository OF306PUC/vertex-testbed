"""UDP broadcast transport -- the Wi-Fi path.

Each agent broadcasts a 16-byte datagram at its publish rate and listens for its
neighbours' broadcasts. There is no addressing, no acknowledgement and no
retransmission, which is the entire point: it makes the Wi-Fi path *structurally
the same* as the BLE path.

**Several sockets must share the port.** Those same three agents all bind
``STATE_PORT``. ``SO_REUSEADDR`` plus ``SO_REUSEPORT`` allows that, and broadcast
datagrams are delivered to *every* matching socket. 
"""

from __future__ import annotations

import asyncio
import socket
from dataclasses import dataclass, field

from ..clock import Clock
from ..net import STATE_PORT
from ..wire import DecodeError, StatePacket, decode_any, encode_manufacturer_data
from ..wire.codec import COMPANY_ID, decode_manufacturer_data
from .base import Reception, ReceiveCallback, Transport

__all__ = ["UdpStats", "UdpTransport"]


@dataclass
class UdpStats:
    """What the socket saw. Rejections are counted, never silent.
    """

    sent: int = 0
    send_errors: int = 0
    received: int = 0
    self_filtered: int = 0
    undecodable: int = 0
    delivered: int = 0
    last_error: str | None = None

    def summary(self) -> str:
        return (f"sent={self.sent} (errors {self.send_errors}), "
                f"received={self.received} -> delivered={self.delivered}, "
                f"own={self.self_filtered}, undecodable={self.undecodable}")


class _Protocol(asyncio.DatagramProtocol):
    """Bridges asyncio's datagram callbacks to the transport."""

    def __init__(self, owner: "UdpTransport") -> None:
        self._owner = owner

    def datagram_received(self, data: bytes, addr) -> None:
        self._owner._handle(data, addr)

    def error_received(self, exc: Exception) -> None:
        # ICMP port-unreachable and friends. Expected on a broadcast medium where
        # not every host is listening; recording beats logging on every packet.
        self._owner.stats.last_error = repr(exc)

    def connection_lost(self, exc: Exception | None) -> None:
        pass


class UdpTransport(Transport):
    """Best-effort state broadcast over UDP.

    Parameters
    ----------
    node_id:
        This agent's id. Used to discard our own broadcasts.
    clock:
        Supplies ``now_us`` for the receive timestamp, on the experiment epoch.
    send_to:
        ``(host, port)`` to broadcast to -- normally the subnet broadcast address
        from :func:`vertex.net.broadcast_address`. Tests pass a unicast address.
    bind_host / bind_port:
        Where to listen. Defaults to every interface on ``STATE_PORT``.
    """

    name = "udp"

    def __init__(
        self,
        node_id: int,
        clock: Clock,
        *,
        send_to: tuple[str, int],
        bind_host: str = "0.0.0.0",
        bind_port: int = STATE_PORT,
        broadcast: bool = True,
        reuse_port: bool = True,
    ) -> None:
        self.node_id = node_id
        self.clock = clock
        self.send_to = send_to
        self.bind_host = bind_host
        self.bind_port = bind_port
        self.broadcast = broadcast
        self.reuse_port = reuse_port
        self.stats = UdpStats()
        self._transport: asyncio.DatagramTransport | None = None
        self._on_receive: ReceiveCallback | None = None

    # lifecycle:
    async def start(self, on_receive: ReceiveCallback) -> None:
        if self._transport is not None:
            return
        self._on_receive = on_receive

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if self.reuse_port and hasattr(socket, "SO_REUSEPORT"):
                # Lets the three agents on one host share STATE_PORT; broadcast is
                # delivered to all such sockets.
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            if self.broadcast:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.setblocking(False)
            sock.bind((self.bind_host, self.bind_port))
        except OSError:
            sock.close()
            raise

        loop = asyncio.get_running_loop()
        self._transport, _ = await loop.create_datagram_endpoint(
            lambda: _Protocol(self), sock=sock
        )

    async def stop(self) -> None:
        if self._transport is not None:
            self._transport.close()
            self._transport = None
        self._on_receive = None

    @property
    def bound_port(self) -> int:
        """The port actually bound -- differs from ``bind_port`` when 0 was given."""
        if self._transport is None:
            return self.bind_port
        return int(self._transport.get_extra_info("sockname")[1])

    # send:
    async def publish(self, packet: StatePacket) -> None:
        if self._transport is None:
            raise RuntimeError("publish() before start()")
        payload = encode_manufacturer_data(packet)
        try:
            self._transport.sendto(payload, self.send_to)
            self.stats.sent += 1
        except OSError as exc:
            # A send failure is loss. Report it as a counter rather than raising:
            # the publish loop must keep its period, and the receiver is the thing
            # that measures delivery anyway.
            self.stats.send_errors += 1
            self.stats.last_error = repr(exc)

    # receive:
    def _handle(self, data: bytes, addr) -> None:
        """Decode one datagram. Never raises: this runs on the event loop's
        datagram callback, and an exception here would tear down the receiver."""
        self.stats.received += 1
        rx_time_us = self.clock.now_us()
        try:
            packet = self._decode(data)
        except DecodeError:
            self.stats.undecodable += 1
            return
        except Exception as exc:                            # pragma: no cover
            self.stats.undecodable += 1
            self.stats.last_error = repr(exc)
            return

        if packet.node_id == self.node_id:
            # Our own broadcast, echoed back. Filter by id and not by source
            # address: sibling agents on this host share our address.
            self.stats.self_filtered += 1
            return

        cb = self._on_receive
        if cb is None:
            return
        try:
            cb(Reception(packet=packet, rx_time_us=rx_time_us))
            self.stats.delivered += 1
        except Exception as exc:                            # pragma: no cover
            self.stats.last_error = repr(exc)

    @staticmethod
    def _decode(data: bytes) -> StatePacket:
        """Accept the company-prefixed framing, or a bare payload.

        The same framing is used as on BLE -- a 2-byte company id then the payload --
        so one encoder serves both transports and a captured datagram is directly
        comparable with a captured advertisement. A bare payload is also accepted so
        diagnostic tooling can send the 16 bytes alone.
        """
        if len(data) >= 2 and int.from_bytes(data[:2], "little") == COMPANY_ID:
            return decode_manufacturer_data(data)
        return decode_any(data)
