#!/usr/bin/env python3
"""Loopback test B: the peer advertises on command, the Pi scans over raw HCI.

    sudo hciconfig hci0 down
    sudo python3 run_b.py --count 500 --period 0.2 --scan-window 100

This is the direction that validates the Pi's SCANNING path -- the interval and
window BlueZ's D-Bus API never exposed. Direction A could not: its --scan-window
configured the peer's scanner, which was never in doubt.

    --sweep 100,50,25,10
runs the same test at several duty cycles. **Delivery ratio must fall as the
window shrinks.** If it does not move, the parameter is not taking effect and the
whole HCI path has bought nothing over BlueZ. That single result is the point.

`--self-test` runs the matching path with no port and no adapter.
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))                      # radio.scanner
sys.path.insert(0, str(_HERE.parent / "common"))    # peer (shared with test A)
sys.path.insert(0, str(_HERE.parents[1]))           # the vertex package

from peer import Peer, PeerError                                  # noqa: E402
from radio.scanner import COMPANY_ID, Scanner, Seen               # noqa: E402
from vertex.radio import (AD_MANUFACTURER, AD_NAME_COMPLETE,      # noqa: E402
                          build_ad, element, manufacturer_value)
from vertex.radio.hci import CommandComplete, ms_to_units         # noqa: E402
from vertex.serial import PeerStats                               # noqa: E402
from vertex.wire import DecodeError, StatePacket, decode_any      # noqa: E402

MAX_SEQ = 0x10000


def build_advdata(node: int, seq: int, *, name: bytes = b"LABCTRL") -> bytes:
    pkt = StatePacket.from_state(node, vstate=seq * 1e-6, seq=seq % MAX_SEQ,
                                tx_time_us=seq * 1000)
    return build_ad(element(AD_NAME_COMPLETE, name),
                    element(AD_MANUFACTURER,
                            manufacturer_value(COMPANY_ID, pkt.encode())))


# ── accounting ───────────────────────────────────────────────────────────────

@dataclass
class Results:
    window_ms: float = 0.0
    interval_ms: float = 0.0
    commanded: int = 0
    delivered: int = 0
    byte_mismatches: int = 0
    duplicates: int = 0
    unknown_seq: int = 0
    other_node: int = 0
    undecodable: int = 0
    latency_ms: list[float] = field(default_factory=list)
    scan_foreign: int = 0
    scan_reports: int = 0
    peer_delta: PeerStats | None = None

    @property
    def duty(self) -> float:
        return self.window_ms / self.interval_ms if self.interval_ms else 0.0

    @property
    def delivery_ratio(self) -> float:
        return self.delivered / self.commanded if self.commanded else 0.0

    @property
    def valid(self) -> bool:
        internal = self.peer_delta.lost_internally if self.peer_delta else 0
        return self.byte_mismatches == 0 and internal == 0

    def line(self) -> str:
        return (f"  window {self.window_ms:6.1f} ms ({self.duty * 100:5.1f}% duty)"
                f"   delivered {self.delivered:4d}/{self.commanded:<4d}"
                f" = {self.delivery_ratio * 100:5.1f}%"
                f"   scanner: {self.scan_reports:6d} reports, "
                f"{self.scan_foreign:6d} foreign, {self.scan_reports - self.scan_foreign:5d} ours"
                f"   mism {self.byte_mismatches}"
                + ("" if self.valid else "  INVALID"))

    def detail(self) -> str:
        L = [
            f"commanded          {self.commanded}",
            f"delivered          {self.delivered}  ({self.delivery_ratio * 100:.1f}%)",
            f"byte mismatches    {self.byte_mismatches}"
            + ("" if self.byte_mismatches == 0 else "   <-- BUG, not radio loss"),
            f"duplicates         {self.duplicates}",
            f"unknown seq        {self.unknown_seq}",
            f"other node ids     {self.other_node}",
            f"undecodable        {self.undecodable}",
            f"scanner            reports {self.scan_reports}  "
            f"foreign {self.scan_foreign}   (filtered on the Pi, costs nothing)",
        ]
        if self.latency_ms:
            L.append(f"latency            median {statistics.median(self.latency_ms):.1f} ms  "
                     f"min {min(self.latency_ms):.1f}  max {max(self.latency_ms):.1f}")
            L.append("                   (UART command + peer TX + BLE transit + up to "
                     "one advertising interval)")
        if self.peer_delta is not None:
            d = self.peer_delta
            L.append(f"peer internal      tx_dropped {d.tx_dropped}  "
                     f"queue_dropped {d.queue_dropped}"
                     + ("" if d.lost_internally == 0 else "   <-- VOIDS the ratio"))
            L.append(f"peer uart          crc_errors {d.crc_errors}  "
                     f"timeouts {d.timeouts}  frames_ok {d.frames_ok}")
        L.append("VALID" if self.valid else "INVALID -- see the markers above")
        return "\n".join(L)


def consume(seen: Seen, sent: dict[int, tuple[bytes, float]], done: set[int],
            node: int, res: Results) -> None:
    try:
        pkt = decode_any(seen.payload)
    except DecodeError:
        res.undecodable += 1
        return
    if pkt.node_id != node:
        res.other_node += 1
        return
    if pkt.seq not in sent:
        res.unknown_seq += 1
        return
    if pkt.seq in done:
        res.duplicates += 1                     # a repeat is not a delivery
        return

    done.add(pkt.seq)
    ad_sent, t_cmd = sent[pkt.seq]
    if seen.ad != ad_sent:
        # Byte equality is the primary result in both directions.
        res.byte_mismatches += 1
        return
    res.delivered += 1
    res.latency_ms.append((seen.rx_monotonic - t_cmd) * 1000.0)


# ── one measurement ──────────────────────────────────────────────────────────

def measure(args, window_ms: float, peer, scanner_sock) -> Results:
    res = Results(window_ms=window_ms, interval_ms=args.scan_interval)
    sent: dict[int, tuple[bytes, float]] = {}
    done: set[int] = set()

    scanner = Scanner(device=args.device, interval_ms=args.scan_interval,
                      window_ms=window_ms, passive=not args.active_scan,
                      sock=scanner_sock)

    peer.set_radio(adv_min=ms_to_units(args.adv_interval),
                   adv_max=ms_to_units(args.adv_interval),
                   scan_interval=ms_to_units(args.scan_interval),
                   scan_window=ms_to_units(window_ms),
                   active_scan=False,
                   advertising=True)         # direction B: the peer transmits
    before = peer.stats()

    with scanner:
        for seq in range(1, args.count + 1):
            ad = build_advdata(args.node, seq)
            t_cmd = time.monotonic()
            peer.advertise(ad)               # returns (seq, peer uptime_us)
            sent[seq % MAX_SEQ] = (ad, t_cmd)
            res.commanded += 1

            for s in scanner.drain(timeout=args.period):
                consume(s, sent, done, args.node, res)

        # Let the last advertisements be seen. Without this they count as lost
        # purely because the run ended first.
        deadline = time.monotonic() + 3 * args.adv_interval / 1000.0
        while time.monotonic() < deadline:
            for s in scanner.drain(timeout=0.05):
                consume(s, sent, done, args.node, res)

    res.scan_reports = scanner.counters.reports
    res.scan_foreign = scanner.counters.foreign
    res.peer_delta = peer.stats().delta(before)
    return res


# ── self-test fakes ──────────────────────────────────────────────────────────

class _FakeScanSock:
    """Echoes whatever the fake peer was told to advertise, as an HCI event."""

    def __init__(self, peer: "_FakePeerB") -> None:
        self.peer = peer

    def command(self, packet, *, timeout=2.0, tolerate=()) -> CommandComplete:
        return CommandComplete(int.from_bytes(packet[1:3], "little"), 0x00, b"")

    @property
    def fileno(self) -> int:
        return self.peer.read_fd

    def recv(self, size: int = 1024) -> bytes:
        return self.peer.next_event()

    def close(self) -> None:
        pass


class _FakePeerB:
    def __init__(self) -> None:
        import os
        self.read_fd, self._write_fd = os.pipe()
        self._pending: list[bytes] = []
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
            | {"frames_ok": self._n * 5})

    def advertise(self, ad: bytes, *, timeout: float = 1.0) -> tuple[int, int]:
        import os, struct
        params = bytes([0x02, 0x01, 0x03, 0x01]) + bytes(6) + bytes([len(ad)]) \
            + ad + struct.pack("<b", -40)
        self._pending.append(bytes([0x04, 0x3E, len(params)]) + params)
        os.write(self._write_fd, b"\x00")
        return (0, 0)

    def next_event(self) -> bytes:
        import os
        os.read(self.read_fd, 1)
        return self._pending.pop(0) if self._pending else b""


# ── entry ────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", default="/dev/ttyACM0")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--device", type=int, default=0, help="hciN")
    ap.add_argument("--count", type=int, default=500)
    ap.add_argument("--period", type=float, default=0.2)
    ap.add_argument("--adv-interval", type=float, default=100.0,
                    help="ms, on the peer")
    ap.add_argument("--scan-interval", type=float, default=100.0, help="ms, on the Pi")
    ap.add_argument("--scan-window", type=float, default=100.0,
                    help="ms, on the Pi; equal to interval is 100%% duty")
    ap.add_argument("--sweep", default="",
                    help="comma-separated windows in ms, e.g. 100,50,25,10")
    ap.add_argument("--active-scan", action="store_true",
                    help="transmit scan requests (costs airtime)")
    ap.add_argument("--node", type=int, default=201)
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()

    if args.count >= MAX_SEQ:
        ap.error(f"--count must be < {MAX_SEQ}: seq is a uint16")

    windows = ([float(w) for w in args.sweep.split(",")] if args.sweep
               else [args.scan_window])
    for w in windows:
        if w > args.scan_interval:
            ap.error(f"window {w} exceeds --scan-interval {args.scan_interval}")

    print(f"direction B  node={args.node}  peer adv={args.adv_interval:g} ms  "
          f"Pi scan interval={args.scan_interval:g} ms"
          + ("  [SELF-TEST]" if args.self_test else ""))

    peer = _FakePeerB() if args.self_test else Peer(args.port, args.baud)
    out: list[Results] = []
    sock = None
    try:
        with peer:
            # Bind the user channel ONCE for the whole sweep. Rebinding per point
            # races the kernel's teardown of the previous socket: the second bind
            # returns EBUSY because the adapter is not released instantly.
            if args.self_test:
                sock = _FakeScanSock(peer)
            else:
                from vertex.radio.hci import HciSocket, cmd_reset
                sock = HciSocket(args.device).open()
                sock.command(cmd_reset())
            try:
                for w in windows:
                    out.append(measure(args, w, peer, sock))
                    print(out[-1].line())
            finally:
                # Leave the peer quiet. Otherwise it keeps advertising after the
                # run and pollutes the next one -- and the air generally.
                try:
                    peer.set_radio(adv_min=ms_to_units(args.adv_interval),
                                   adv_max=ms_to_units(args.adv_interval),
                                   scan_interval=ms_to_units(args.scan_interval),
                                   scan_window=ms_to_units(args.scan_interval),
                                   advertising=False)
                except Exception:
                    pass
    except PeerError as exc:
        print(f"\npeer: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    finally:
        if sock is not None and not args.self_test:
            sock.close()

    if len(out) == 1:
        print()
        print(out[0].detail())
    else:
        print()
        # The result the whole HCI path exists to produce.
        ratios = [r.delivery_ratio for r in out]
        widest, narrowest = ratios[0], ratios[-1]
        moved = widest - narrowest
        ours = sum(r.scan_reports - r.scan_foreign for r in out)
        reports = sum(r.scan_reports for r in out)

        print(f"delivery ratio {widest * 100:.1f}% at {out[0].duty * 100:.0f}% duty "
              f"-> {narrowest * 100:.1f}% at {out[-1].duty * 100:.0f}% duty  "
              f"(moved {moved * 100:.1f} points)")

        # A flat curve at zero says nothing about the scan window: the pipeline
        # broke upstream of it. Diagnose in order along the chain.
        if all(r.delivered == 0 for r in out):
            print("  ^^ NOTHING was received. This is not a scan-window result --")
            print("     the window cannot be measured until something arrives.")
            if reports == 0:
                print("     scanner saw 0 reports of any kind: the Pi is not scanning.")
                print("       - check every Command Complete status in Scanner.open()")
                print("       - `hciconfig hci0` should show the adapter DOWN "
                      "(user channel)")
            elif ours == 0:
                print(f"     scanner saw {reports} reports but 0 with our company id.")
                print("       - the peer is transmitting something, but not what we sent")
                print("       - most likely the peer re-frames the AdvData instead of")
                print("         advertising it verbatim; capture one and compare bytes")
            else:
                print(f"     scanner matched {ours} of ours, yet none were counted as")
                print("     delivered: check unknown-seq and other-node in --sweep '' mode")
        elif moved < 0.10:
            print("  ^^ the scan window is NOT taking effect. Delivery should fall")
            print("     roughly with duty cycle; a flat curve at a NON-zero ratio means")
            print("     the parameter never reached the controller, and the HCI path has")
            print("     bought nothing over BlueZ. Check every Command Complete status.")
        else:
            print("  ^^ the scan window is taking effect -- the parameter BlueZ")
            print("     never exposed is now measurably under our control.")

    return 0 if all(r.valid for r in out) else 1


if __name__ == "__main__":
    raise SystemExit(main())
