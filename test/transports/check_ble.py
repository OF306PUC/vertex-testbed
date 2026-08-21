#!/usr/bin/env python3
"""Exercise BleTransport against a fake HCI socket -- no adapter needed.

Four things are checked, each one a failure that has already happened once on
this platform or would be invisible if it happened:

1. **AD framing.** The bytes handed to LE Set Advertising Data parse back as
   separate elements. Wrapping a complete AD inside one manufacturer element fit
   in 31 bytes, errored nowhere, and made loopback test B report 0% delivered.
2. **Setup order.** Parameters are set while the corresponding function is
   disabled, and scanning is enabled last. A refused `set scan parameters` leaves
   the previous window in force and the run measures a configuration nobody chose.
3. **The pump does not eat reports.** An advertising report queued behind a
   publish's command completion must still reach the callback. This is the reason
   the transport has a pump instead of calling `HciSocket.command()`.
4. **Self-filtering.** Our own advertisement, reported back by the controller,
   must not be delivered as a neighbour's.
5. **v0 is accepted.** The nRF's broadcaster still puts the 6-byte v0 payload on
   air, so a `bridge` agent in a room with `ble` agents hears both formats.
   Rejecting v0 would count every nRF advertisement as undecodable, which looks
   identical to the nRFs being switched off.

    python3 test/transports/check_ble.py
"""
from __future__ import annotations

import asyncio
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import time

from vertex.clock import WallClock
from vertex.radio import build_ad, element, parse_ad
from vertex.radio.hci import HCI_EVENT_PKT, ms_to_units
from vertex.transports.ble import BleTransport
from vertex.wire import StatePacket, encode_v0
from vertex.wire.codec import COMPANY_ID

OP_ADV_PARAMS, OP_ADV_DATA, OP_ADV_ENABLE = 0x2006, 0x2008, 0x200A
OP_SCAN_PARAMS, OP_SCAN_ENABLE = 0x200B, 0x200C
OP_RESET = 0x0C03


def command_complete(op: int, status: int = 0) -> bytes:
    return bytes([HCI_EVENT_PKT, 0x0E, 0x04, 0x01]) + op.to_bytes(2, "little") \
        + bytes([status])


def adv_report(ad: bytes, addr: bytes = b"\x01\x02\x03\x04\x05\x06",
               rssi: int = -60) -> bytes:
    body = bytes([0x02, 0x01, 0x03, 0x00]) + addr + bytes([len(ad)]) + ad \
        + rssi.to_bytes(1, "little", signed=True)
    return bytes([HCI_EVENT_PKT, 0x3E, len(body)]) + body


class FakeSock:
    """The slice of HciSocket the transport uses, backed by a datagram socketpair.

    A real fd, not a queue: the transport arms `loop.add_reader` on `fileno`, so
    the readiness path is the one that runs in production.

    SOCK_DGRAM, not a pipe. An HCI user-channel socket is SOCK_RAW and therefore
    packet-oriented -- one `recv` returns exactly one event. A pipe is a byte
    stream, so a report and a command completion written back to back arrive in a
    single read, and the transport looks like it drops the second. That is an
    artefact of the fake, not of the code under test, and modelling the boundary
    correctly is the difference between this script proving something and
    inventing a bug.
    """

    def __init__(self) -> None:
        self._r, self._w = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
        self._r.setblocking(False)
        self.sent: list[int] = []           # opcodes, in order
        self.adv_data: list[bytes] = []     # the AD of each Set Adv Data
        self.state = {"adv": False, "scan": False}
        self.violations: list[str] = []
        self.closed = False

    @property
    def fileno(self) -> int:
        return self._r.fileno()

    def _note(self, packet: bytes) -> int:
        op = int.from_bytes(packet[1:3], "little")
        self.sent.append(op)
        if op == OP_ADV_DATA:
            n = packet[4]                   # significant length
            self.adv_data.append(packet[5:5 + n])
        elif op == OP_ADV_ENABLE:
            self.state["adv"] = packet[4] == 1
        elif op == OP_SCAN_ENABLE:
            self.state["scan"] = packet[4] == 1
        elif op == OP_ADV_PARAMS and self.state["adv"]:
            self.violations.append("adv parameters set while advertising")
        elif op == OP_SCAN_PARAMS and self.state["scan"]:
            self.violations.append("scan parameters set while scanning")
        return op

    def command(self, packet: bytes, *, timeout: float = 2.0,
                tolerate: tuple[int, ...] = ()):
        # Synchronous setup path: answer immediately, nothing to queue.
        from vertex.radio.hci import CommandComplete
        return CommandComplete(self._note(packet), 0, b"")

    def send(self, packet: bytes) -> None:
        # Asynchronous path: the completion goes on the pipe, like a controller.
        op = self._note(packet)
        self._w.send(command_complete(op))

    def push(self, event: bytes) -> None:
        self._w.send(event)

    def recv(self, size: int = 1024) -> bytes:
        return self._r.recv(size)

    def close(self) -> None:
        self.closed = True


async def main() -> int:
    sock = FakeSock()
    received: list = []
    t = BleTransport(node_id=1, clock=WallClock(time.time()), adv_interval_ms=100.0,
                     scan_interval_ms=100.0, scan_window_ms=30.0, sock=sock)
    await t.start(received.append)

    fails = []

    # 2. setup order
    order = sock.sent
    print(f"  setup opcodes: {[hex(o) for o in order]}")
    for v in sock.violations:
        fails.append(f"setup order: {v}")
    if order[-1] != OP_SCAN_ENABLE:
        fails.append(f"scanning was not enabled last (last was {order[-1]:#x})")
    if not sock.state["adv"] or not sock.state["scan"]:
        fails.append("advertising or scanning not enabled after start()")

    # 1. AD framing
    pkt = StatePacket(node_id=1, vstate=22_300_000, seq=7, tx_time_us=123456)
    await t.publish(pkt)
    ad = sock.adv_data[-1]
    elems = list(parse_ad(ad, strict=True))
    types = [e.type for e in elems]
    print(f"  AD {len(ad)} B, elements {[hex(x) for x in types]}")
    # Manufacturer element only. The flags element was dropped so this AD matches
    # the nRF's byte for byte -- PLATFORM.md 8b.A3.
    if types != [0xFF]:
        fails.append(f"AD elements are {types}, expected [manufacturer] only")
    else:
        mfg = elems[0].value
        if int.from_bytes(mfg[:2], "little") != COMPANY_ID:
            fails.append("company id is not first in the manufacturer element")
        elif mfg[2:] != pkt.encode():
            fails.append("manufacturer payload does not round-trip")
    if len(ad) > 31:
        fails.append(f"AD is {len(ad)} bytes, over the 31-byte budget")

    # 3. a report queued behind a publish's completion still arrives
    neighbour = StatePacket(node_id=2, vstate=21_000_000, seq=3)
    sock.push(adv_report(t._ad(neighbour)))
    await t.publish(pkt)                     # its completion sits behind the report
    for _ in range(20):                      # let the pump drain
        await asyncio.sleep(0)
    if not received:
        fails.append("report queued behind a command completion was lost")
    else:
        got = received[0].packet
        if (got.node_id, got.vstate, got.seq) != (2, 21_000_000, 3):
            fails.append(f"delivered packet is wrong: {got}")

    # 4. self-filtering
    before = len(received)
    sock.push(adv_report(t._ad(pkt)))        # our own node_id
    for _ in range(20):
        await asyncio.sleep(0)
    if len(received) != before:
        fails.append("our own advertisement was delivered as a neighbour's")
    if t.stats.self_filtered != 1:
        fails.append(f"self_filtered is {t.stats.self_filtered}, expected 1")

    # 5. a v0 advertisement from an nRF
    before = len(received)
    v0 = build_ad(element(0xFF, COMPANY_ID.to_bytes(2, "little")
                          + encode_v0(node_id=5, vstate=19_500_000, enabled=True)))
    sock.push(adv_report(v0))
    for _ in range(20):
        await asyncio.sleep(0)
    if len(received) != before + 1:
        fails.append("a v0 advertisement from an nRF was not delivered")
    elif (received[-1].packet.node_id, received[-1].packet.vstate) != (5, 19_500_000):
        fails.append(f"v0 decoded wrong: {received[-1].packet}")

    await t.stop()
    if sock.state["adv"] or sock.state["scan"]:
        fails.append("stop() left the radio enabled")

    print(f"  stats: {t.stats.summary()}")
    p = t.parameters()
    print(f"  params: scan {p['scan_window_ms']}/{p['scan_interval_ms']} ms = "
          f"{p['scan_window_units']}/{p['scan_interval_units']} units, "
          f"duty {p['scan_duty_cycle']:.2f}")
    if p["scan_window_units"] != ms_to_units(30.0):
        fails.append("parameters() misreports the programmed units")

    for f in fails:
        print(f"  FAIL {f}")
    print(f"  -> {'ok' if not fails else f'{len(fails)} failure(s)'}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
