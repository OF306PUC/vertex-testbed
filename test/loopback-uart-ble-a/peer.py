"""Serial driver for the nRF test peer.

Frames are defined and tested in `vertex.serial.proto`, and cross-checked against
the firmware's C parser. This adds only the port, the read loop and dispatch.

The transport is injectable so the whole driver is testable without a port: pass
any object with read/write/close.
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Iterator, Protocol

from vertex.serial import (AdvReport, Frame, FrameParser, FrameType, PeerStats,
                           build_frame, decode_ack, decode_adv_report, decode_pong,
                           decode_stats, encode_radio)

__all__ = ["Peer", "PeerError", "PeerRejected", "SerialIO", "PeerCounters",
           "TimedReport"]


class SerialIO(Protocol):
    def read(self, n: int) -> bytes: ...
    def write(self, data: bytes) -> int: ...
    def close(self) -> None: ...


class PeerError(RuntimeError):
    """Peer unreachable, or no reply within the timeout."""


class PeerRejected(PeerError):
    """The peer returned an ERR frame."""

    def __init__(self, frame_type: int, status: int) -> None:
        self.frame_type = frame_type
        self.status = status
        super().__init__(f"peer rejected frame 0x{frame_type:02X} with status {status}")


@dataclass(frozen=True, slots=True)
class TimedReport:
    """An advertising report with the instant it reached us."""

    report: AdvReport
    rx_monotonic: float


@dataclass
class PeerCounters:
    """What the driver itself saw. Distinct from the peer's own counters."""

    frames_in: int = 0
    reports: int = 0
    unknown_frames: int = 0
    unsolicited: int = 0        # a reply with nothing waiting for it
    read_errors: int = 0

    def summary(self) -> str:
        return (f"frames={self.frames_in} reports={self.reports} "
                f"unknown={self.unknown_frames} unsolicited={self.unsolicited} "
                f"read_errors={self.read_errors}")


#: Frame types that answer a request, as opposed to arriving unprompted.
_REPLY_TYPES = frozenset({FrameType.ACK, FrameType.ERR, FrameType.PONG,
                          FrameType.STATS})


class Peer:
    def __init__(self, port: str = "/dev/ttyACM0", baud: int = 115200,
                 *, idle_timeout_s: float = 0.1, io: SerialIO | None = None) -> None:
        self.port = port
        self.baud = baud
        self.idle_timeout_s = idle_timeout_s
        self.counters = PeerCounters()
        self.parser = FrameParser()

        self._io = io
        self._owns_io = io is None
        self._reports: queue.Queue[TimedReport] = queue.Queue()
        self._reply: Frame | None = None
        self._reply_ready = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._last_rx = 0.0

    # ── lifecycle ────────────────────────────────────────────────────────────
    def open(self) -> "Peer":
        if self._io is None:
            import serial                    # imported here so tests need no port
            self._io = serial.Serial(self.port, self.baud, timeout=0.01)
        self._stop.clear()
        self._last_rx = time.monotonic()
        self._thread = threading.Thread(target=self._run, name="peer-rx", daemon=True)
        self._thread.start()
        return self

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self._io is not None and self._owns_io:
            try:
                self._io.close()
            except Exception:
                pass
            self._io = None

    def __enter__(self) -> "Peer":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()

    # ── read loop ────────────────────────────────────────────────────────────
    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                data = self._io.read(4096) if self._io is not None else b""
            except Exception:
                self.counters.read_errors += 1
                time.sleep(0.05)
                continue
            self.pump(data)

    def pump(self, data: bytes) -> None:
        """Feed bytes and dispatch. Separated from the loop so tests drive it.

        The idle branch is required, not defensive: a mid-payload 0x7E is not
        treated as a frame start, so a truncated frame waits forever *and eats the
        following frames as payload* until the parser is reset.
        """
        now = time.monotonic()
        if data:
            for frame in self.parser.feed(data):
                self.counters.frames_in += 1
                self._dispatch(frame, now)
            self._last_rx = now
        elif self.parser.in_frame and now - self._last_rx > self.idle_timeout_s:
            self.parser.timeout()

    def _dispatch(self, frame: Frame, now: float) -> None:
        if frame.type == FrameType.ADV_REPORT:
            try:
                report = decode_adv_report(frame.payload)
            except Exception:
                self.counters.unknown_frames += 1
                return
            self.counters.reports += 1
            # Timestamp here, before queueing: taken after, it measures our own
            # scheduler rather than the link.
            self._reports.put(TimedReport(report, now))
            return

        if frame.type in _REPLY_TYPES:
            if self._reply_ready.is_set():
                self.counters.unsolicited += 1
            self._reply = frame
            self._reply_ready.set()
            return

        self.counters.unknown_frames += 1

    # ── commands ─────────────────────────────────────────────────────────────
    def send(self, frame_type: int, payload: bytes = b"") -> None:
        if self._io is None:
            raise PeerError("peer is not open")
        self._io.write(build_frame(frame_type, payload))

    def request(self, frame_type: int, payload: bytes = b"",
                *, timeout: float = 1.0) -> Frame:
        """Send and wait for the peer's reply. Raises on ERR."""
        with self._lock:
            self._reply = None
            self._reply_ready.clear()
            self.send(frame_type, payload)
            if not self._reply_ready.wait(timeout):
                raise PeerError(
                    f"no reply to frame 0x{frame_type:02X} within {timeout}s")
            reply = self._reply
            assert reply is not None

        if reply.type == FrameType.ERR:
            # Raise rather than return: a silently ignored rejection is how you
            # end up sweeping a scan window the peer never applied.
            raise PeerRejected(*decode_ack(reply.payload))
        return reply

    def set_radio(self, *, adv_min: int, adv_max: int, scan_interval: int,
                  scan_window: int, active_scan: bool = False,
                  advertising: bool = False) -> None:
        """Set the peer's radio parameters, in 0.625 ms units.

        `advertising=False` for direction A: the peer only scans. Leaving its
        advertiser on would put its own packets into your reports.
        """
        self.request(FrameType.RADIO, encode_radio(
            adv_min=adv_min, adv_max=adv_max, scan_interval=scan_interval,
            scan_window=scan_window, active_scan=active_scan,
            advertising=advertising))

    def ping(self) -> int:
        """Peer uptime in microseconds. Bounds the clock offset to a UART RTT."""
        return decode_pong(self.request(FrameType.PING).payload)

    def stats(self) -> PeerStats:
        """The peer's own counters. Read before and after a run and subtract."""
        return decode_stats(self.request(FrameType.STATS_REQ).payload)

    # ── reports ──────────────────────────────────────────────────────────────
    def reports(self) -> Iterator[TimedReport]:
        """Drain everything received so far. Non-blocking."""
        while True:
            try:
                yield self._reports.get_nowait()
            except queue.Empty:
                return

    @property
    def pending_reports(self) -> int:
        return self._reports.qsize()
