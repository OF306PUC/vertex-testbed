#!/usr/bin/env python3
"""Loopback test A: the Pi advertises, the peer scans and reports over UART.

    sudo hciconfig hci0 down
    sudo python3 run_a.py --count 500 --period 0.2

The UART is the reference: it is reliable and does not use the radio, so any
difference between what the Pi advertised and what came back is attributable to
BLE. The primary assertion is byte equality, not value equality -- the peer does
not parse, so comparing its reported bytes against the AdvData we assembled checks
element structure, company ID, field order and endianness in one comparison.

`--self-test` runs the whole matching path against synthesised reports, with no
port and no adapter. Use it to separate "my script is wrong" from "the radio is
misbehaving" before going near hardware.
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))                # radio.advertiser
sys.path.insert(0, str(_HERE.parent / "common"))   # peer (shared with test B)
sys.path.insert(0, str(_HERE.parents[1]))     # the vertex package

from peer import Peer, PeerError, TimedReport                       # noqa: E402
from radio.advertiser import COMPANY_ID, Advertiser                 # noqa: E402
from vertex.radio import find_manufacturer                          # noqa: E402
from vertex.radio.hci import CommandComplete, ms_to_units           # noqa: E402
from vertex.serial import PeerStats                                 # noqa: E402
from vertex.wire import DecodeError, StatePacket, decode_any        # noqa: E402

MAX_SEQ = 0x10000


# ── accounting ───────────────────────────────────────────────────────────────

@dataclass
class Results:
    transmissions: int = 0
    delivered: int = 0
    byte_mismatches: int = 0
    duplicates: int = 0
    unknown_seq: int = 0
    warmup: int = 0
    foreign: int = 0
    other_node: int = 0
    undecodable: int = 0
    rtt_ms: list[float] = field(default_factory=list)
    peer_delta: PeerStats | None = None
    peer_before: PeerStats | None = None

    @property
    def delivery_ratio(self) -> float:
        return self.delivered / self.transmissions if self.transmissions else 0.0

    @property
    def valid(self) -> bool:
        """Whether the delivery ratio means anything.

        Corruption is a bug, not a radio condition. And a report the peer dropped
        internally is indistinguishable, in this number, from a packet lost over
        the air -- so any internal loss voids the measurement rather than quietly
        deflating it.
        """
        internal = self.peer_delta.lost_internally if self.peer_delta else 0
        return self.byte_mismatches == 0 and internal == 0

    def report(self, *, adv_interval_ms: float) -> str:
        L = [
            "",
            f"transmissions      {self.transmissions}",
            f"delivered          {self.delivered}  ({self.delivery_ratio * 100:.1f}%)",
            f"byte mismatches    {self.byte_mismatches}"
            + ("" if self.byte_mismatches == 0 else "   <-- BUG, not radio loss"),
            f"duplicates         {self.duplicates}",
            f"unknown seq        {self.unknown_seq}",
            f"warm-up adverts    {self.warmup}   (the payload set before enabling)",
            f"foreign adverts    {self.foreign}   (other devices nearby)"
            + ("" if self.foreign < 5000 else
               "  <-- saturates a 115200 UART; see notes"),
            f"other node ids     {self.other_node}",
            f"undecodable        {self.undecodable}",
        ]
        if self.rtt_ms:
            L.append(f"rtt                median {statistics.median(self.rtt_ms):.1f} ms   "
                     f"min {min(self.rtt_ms):.1f}   max {max(self.rtt_ms):.1f}")
            L.append(f"                   (BLE transit + peer scan latency + UART "
                     f"transit + up to one {adv_interval_ms:g} ms adv interval;")
            L.append(f"                    NOT a radio latency -- the two clocks "
                     f"share no origin)")
        if self.peer_delta is not None:
            d = self.peer_delta
            L.append(f"peer internal      queue_dropped {d.queue_dropped}  "
                     f"tx_dropped {d.tx_dropped}"
                     + ("" if d.lost_internally == 0
                        else "   <-- VOIDS the delivery ratio"))
            L.append(f"peer uart          partial_flushes {d.rx_partial_flushes}  "
                     f"full_flushes {d.rx_full_flushes}  "
                     f"crc_errors {d.crc_errors}  timeouts {d.timeouts}")
            # No check on rx_partial_flushes here: this is a DELTA, and the only
            # inbound frames during the measured window are the 'Q' request
            # itself, so the number is ~1 on a healthy link. crc_errors == 0 and
            # timeouts == 0 are the meaningful UART health signals.
        L.append("")
        L.append("VALID" if self.valid else "INVALID -- see the markers above")
        return "\n".join(L)


# ── report matching ──────────────────────────────────────────────────────────

def consume(rep: TimedReport, sent: dict[int, tuple[bytes, float]],
            seen: set[int], node_id: int, res: Results) -> None:
    """Classify one report. Every branch increments exactly one counter."""
    payload = find_manufacturer(rep.report.data, COMPANY_ID)
    if payload is None:
        res.foreign += 1                        # someone else's advertiser
        return
    try:
        pkt = decode_any(payload)
    except DecodeError:
        res.undecodable += 1
        return
    if pkt.node_id != node_id:
        res.other_node += 1
        return
    if pkt.seq == 0:
        res.warmup += 1                         # the payload set before enabling
        return
    if pkt.seq not in sent:
        res.unknown_seq += 1                    # stale, or from a previous run
        return
    if pkt.seq in seen:
        res.duplicates += 1                     # a repeat is not a delivery
        return

    seen.add(pkt.seq)
    ad_sent, t_send = sent[pkt.seq]
    if rep.report.data != ad_sent:
        # Byte equality is the primary result. A mismatch is an encoder,
        # endianness or AD-structure fault -- never a radio condition.
        res.byte_mismatches += 1
        return
    res.delivered += 1
    res.rtt_ms.append((rep.rx_monotonic - t_send) * 1000.0)


# ── the run ──────────────────────────────────────────────────────────────────

def run(args) -> Results:
    res = Results()
    sent: dict[int, tuple[bytes, float]] = {}
    seen: set[int] = set()

    peer = Peer(args.port, args.baud) if not args.self_test else _FakePeer()
    adv = Advertiser(device=args.device, interval_ms=args.adv_interval,
                     channel_map=args.channel_map,
                     sock=_FakeSock(peer) if args.self_test else None)

    with peer:
        peer.set_radio(adv_min=ms_to_units(args.adv_interval),
                       adv_max=ms_to_units(args.adv_interval),
                       scan_interval=ms_to_units(args.scan_interval),
                       scan_window=ms_to_units(args.scan_window),
                       active_scan=False,
                       advertising=False)    # direction A: the peer only scans
        res.peer_before = peer.stats()

        # seq 0 is the warm-up payload; the loop starts at 1.
        adv.open(initial=StatePacket.from_state(args.node, 0.0, seq=0))
        try:
            t0 = time.monotonic()
            for seq in range(1, args.count + 1):
                pkt = StatePacket.from_state(
                    args.node, vstate=seq * 1e-6, seq=seq % MAX_SEQ,
                    tx_time_us=int((time.monotonic() - t0) * 1e6))
                t_send = time.monotonic()
                ad = adv.advertise(pkt)
                sent[pkt.seq] = (ad, t_send)
                res.transmissions += 1

                for rep in peer.reports():
                    consume(rep, sent, seen, args.node, res)
                time.sleep(args.period)

            # Let the last few reports arrive. Without this they count as lost
            # purely because the run ended first.
            time.sleep(3 * args.adv_interval / 1000.0)
            for rep in peer.reports():
                consume(rep, sent, seen, args.node, res)
        finally:
            adv.close()

        after = peer.stats()
        res.peer_delta = after.delta(res.peer_before)

    return res


# ── self-test fakes: exercise the matching path with no hardware ─────────────

class _FakeSock:
    """Turns every advertised payload into a report on the fake peer."""

    def __init__(self, peer: "_FakePeer") -> None:
        self.peer = peer

    def command(self, packet: bytes, *, timeout: float = 2.0,
                tolerate: tuple[int, ...] = ()) -> CommandComplete:
        op = int.from_bytes(packet[1:3], "little")
        if op == 0x2008:
            length = packet[4]
            self.peer.deliver(packet[5:5 + length])
        return CommandComplete(op, 0x00, b"")

    def close(self) -> None:
        pass


class _FakePeer:
    def __init__(self) -> None:
        self._queue: list[TimedReport] = []
        self._n = 0

    def __enter__(self): return self
    def __exit__(self, *exc): pass
    def set_radio(self, **kw): pass

    def stats(self) -> PeerStats:
        self._n += 1
        return PeerStats({k: 0 for k in (
            "reports", "queue_dropped", "oversize", "tx_frames", "tx_dropped",
            "rx_overrun_bytes", "rx_stopped", "rx_partial_flushes",
            "rx_full_flushes", "frames_ok", "crc_errors", "timeouts")}
            | {"rx_partial_flushes": self._n * 10})

    def deliver(self, ad: bytes) -> None:
        from vertex.serial import AdvReport
        self._queue.append(TimedReport(
            AdvReport(0, 0, 0, bytes(6), 0x03, ad), time.monotonic()))

    def reports(self):
        out, self._queue = self._queue, []
        return iter(out)


# ── entry ────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", default="/dev/ttyACM0")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--device", type=int, default=0, help="hciN")
    ap.add_argument("--count", type=int, default=500)
    ap.add_argument("--period", type=float, default=0.2, help="seconds between sends")
    ap.add_argument("--adv-interval", type=float, default=100.0, help="ms")
    ap.add_argument("--scan-interval", type=float, default=100.0, help="ms (peer)")
    ap.add_argument("--scan-window", type=float, default=100.0,
                    help="ms (peer); equal to interval is 100%% duty")
    ap.add_argument("--channel-map", type=lambda s: int(s, 0), default=0x07)
    ap.add_argument("--node", type=int, default=200,
                    help="Pi id; 201 is the peer. Both outside any real experiment")
    ap.add_argument("--self-test", action="store_true",
                    help="run the matching path with no port and no adapter")
    args = ap.parse_args()

    if args.count >= MAX_SEQ:
        ap.error(f"--count must be < {MAX_SEQ}: seq is a uint16 and would wrap")
    if args.scan_window > args.scan_interval:
        ap.error("--scan-window must not exceed --scan-interval")

    print(f"direction A  node={args.node}  adv={args.adv_interval:g} ms  "
          f"scan={args.scan_window:g}/{args.scan_interval:g} ms "
          f"({100 * args.scan_window / args.scan_interval:.0f}% duty)  "
          f"chan_map=0x{args.channel_map:02X}"
          + ("  [SELF-TEST]" if args.self_test else ""))

    try:
        res = run(args)
    except PeerError as exc:
        print(f"\npeer: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130

    print(res.report(adv_interval_ms=args.adv_interval))
    return 0 if res.valid else 1


if __name__ == "__main__":
    raise SystemExit(main())
