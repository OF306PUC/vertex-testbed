"""The framed serial link to an nRF: the production half of `test/common/peer.py`.

`proto.py` is the codec; this is the port, the reader thread and the request/reply
matching. A `ble` agent needs it because its control law runs on the nRF, so the
serial link is a device link to a remote compute node rather than a broadcast
medium (see `vertex/agent/relay.py`).

Structurally the same as the loopback harness's `Peer`, which has run on hardware,
including the two corrections that cost a bench session each:

* **The `_awaiting` gate.** Without it a late or unsolicited reply sits in the slot
  and satisfies the *next* request with the *previous* answer, which surfaces as
  decoding one frame type's payload as another's.
* **The idle timeout.** A mid-payload `0x7E` is not treated as a frame start, so a
  truncated frame waits forever *and consumes the frames behind it as payload*
  until the parser is reset.

One thing this adds that the harness does not have: a **STATE path**. The harness
routes only replies and advertising reports, so a `ble` agent driven by it would
configure the nRF, start the run, and log nothing at all.

## Threads

The reader is a thread; `AgentService` is asyncio. STATE frames arrive on the
reader and are handed to the event loop with `call_soon_threadsafe`, so the
callback -- which appends to a `RunLog` -- runs on the loop like every other
writer. Without that the log has two writers and no lock.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable

from .proto import (Frame, FrameParser, FrameType, ProtoError, build_frame,
                    decode_ack)

__all__ = ["LinkError", "LinkRejected", "LinkCounters", "TimedFrame", "SerialLink"]


@dataclass(frozen=True, slots=True)
class TimedFrame:
    """A frame with the instant it was read off the port.

    The stamp is taken in the reader thread, before the frame is handed to the
    event loop. Taking it on the loop instead would fold `call_soon_threadsafe`
    scheduling delay into the measurement and charge it to the board -- and the
    whole point of a host receive time is to separate the two.

    ``rx_time_us`` is on the experiment epoch when a clock was supplied, so it is
    directly comparable with a `wifi` agent's sample times and with the
    `rx_time_us` the radio transports report. ``None`` when no clock was given.
    """

    frame: Frame
    rx_time_us: int | None

#: Frames that answer a request. Anything else arriving is unsolicited.
_REPLY_TYPES = frozenset({FrameType.ACK, FrameType.ERR, FrameType.PONG,
                          FrameType.STATS, FrameType.TXAT})


class LinkError(RuntimeError):
    """The link is closed, or the nRF did not answer."""


class LinkRejected(LinkError):
    """The nRF answered ERR. Carries the frame type it rejected and the code."""

    def __init__(self, frame_type: int, status: int) -> None:
        super().__init__(f"nRF rejected frame 0x{frame_type:02X}: status {status}")
        self.frame_type = frame_type
        self.status = status


@dataclass
class LinkCounters:
    frames_in: int = 0
    frames_out: int = 0
    states: int = 0
    unsolicited: int = 0        # a reply with no request outstanding
    unknown_frames: int = 0
    read_errors: int = 0
    parse_resets: int = 0       # idle timeouts that reset a stalled parser

    def summary(self) -> str:
        return (f"in={self.frames_in} out={self.frames_out} states={self.states} "
                f"unsolicited={self.unsolicited} unknown={self.unknown_frames} "
                f"read_errors={self.read_errors} resets={self.parse_resets}")


class SerialLink:
    """Framed request/reply plus unsolicited STATE, over a serial port.

    Parameters
    ----------
    port, baud:
        Passed to ``serial.Serial``. On a Pi with an nRF52-DK over USB this is
        typically ``/dev/ttyACM0`` at 115200.
    loop:
        Event loop to marshal STATE callbacks onto. Without one the callback runs
        on the reader thread, which is correct only if the callback is itself
        thread-safe.
    io:
        Injected byte source with ``read``/``write``/``close``, for testing
        without a port.
    """

    def __init__(self, port: str = "/dev/ttyACM0", baud: int = 115200, *,
                 loop=None, io=None, clock=None,
                 idle_timeout_s: float = 0.25) -> None:
        self.port = port
        self.baud = baud
        #: Supplies `rx_time_us` for received STATE frames, on the experiment
        #: epoch. Without it reports carry no host-side arrival time and the
        #: board's own clock is the only timeline available.
        self.clock = clock
        self.idle_timeout_s = idle_timeout_s
        self.counters = LinkCounters()
        self.parser = FrameParser()

        self._loop = loop
        self._io = io
        self._owns_io = io is None
        self._on_state: Callable[["TimedFrame"], None] | None = None

        self._reply: Frame | None = None
        self._reply_ready = threading.Event()
        self._awaiting = False
        self._lock = threading.Lock()        # serialises request/reply exchanges
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._last_rx = 0.0

    # ── registration ─────────────────────────────────────────────────────────
    def on_state(self, callback: Callable[["TimedFrame"], None] | None) -> None:
        """Register the handler for unsolicited STATE frames.

        The callback receives a :class:`TimedFrame`, not a bare frame: the arrival
        instant is only knowable here, in the reader thread.

        `BleRelay` registers itself here; without that the nRF's reports are read
        off the port, counted, and dropped.
        """
        self._on_state = callback

    def set_loop(self, loop) -> None:
        """Set the loop STATE callbacks are marshalled onto."""
        self._loop = loop

    # ── lifecycle ────────────────────────────────────────────────────────────
    def open(self) -> "SerialLink":
        if self._io is None:
            import serial                    # imported here so tests need no port
            try:
                self._io = serial.Serial(self.port, self.baud, timeout=0.01)
            except Exception as exc:
                raise LinkError(
                    f"cannot open {self.port} at {self.baud}: {exc}. "
                    f"Check the nRF is attached and the user is in the dialout group"
                ) from None
        self._stop.clear()
        self._last_rx = time.monotonic()
        self._thread = threading.Thread(target=self._run, name="nrf-rx", daemon=True)
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

    def __enter__(self) -> "SerialLink":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def is_open(self) -> bool:
        return self._io is not None and self._thread is not None

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
        """Feed bytes and dispatch. Separated from the loop so tests drive it."""
        now = time.monotonic()
        if data:
            for frame in self.parser.feed(data):
                self.counters.frames_in += 1
                self._dispatch(frame)
            self._last_rx = now
        elif self.parser.in_frame and now - self._last_rx > self.idle_timeout_s:
            self.parser.timeout()
            self.counters.parse_resets += 1

    def _dispatch(self, frame: Frame) -> None:
        if frame.type == FrameType.STATE:
            self.counters.states += 1
            cb = self._on_state
            if cb is None:
                return
            # Stamped here, on the reader thread, at the moment the frame came off
            # the port -- see TimedFrame.
            timed = TimedFrame(frame, self.clock.now_us() if self.clock else None)
            loop = self._loop
            if loop is None:
                cb(timed)
            else:
                # onto the loop: the handler appends to a RunLog, and the loop is
                # where every other writer to it runs.
                try:
                    loop.call_soon_threadsafe(cb, timed)
                except RuntimeError:
                    # Loop already closed -- the run is over; dropping is correct.
                    pass
            return

        if frame.type in _REPLY_TYPES:
            if not self._awaiting or self._reply_ready.is_set():
                self.counters.unsolicited += 1
                return
            self._reply = frame
            self._reply_ready.set()
            return

        self.counters.unknown_frames += 1

    # ── commands ─────────────────────────────────────────────────────────────
    def send(self, frame_type: int, payload: bytes = b"") -> None:
        """Fire and forget. No reply is awaited, so failures surface elsewhere."""
        if self._io is None:
            raise LinkError("link is not open")
        self._io.write(build_frame(frame_type, payload))
        self.counters.frames_out += 1

    def request(self, frame_type: int, payload: bytes = b"",
                *, timeout: float = 1.0) -> Frame:
        """Send and wait for the nRF's reply. Raises `LinkRejected` on ERR."""
        with self._lock:
            self._reply = None
            self._reply_ready.clear()
            self._awaiting = True
            try:
                self.send(frame_type, payload)
                if not self._reply_ready.wait(timeout):
                    raise LinkError(
                        f"no reply to frame 0x{frame_type:02X} within {timeout}s")
                reply = self._reply
            finally:
                self._awaiting = False

        if reply is None:                                       # pragma: no cover
            raise LinkError(f"reply slot empty for frame 0x{frame_type:02X}")
        if reply.type == FrameType.ERR:
            # Raised, not returned: a silently ignored rejection is how a run ends
            # up measuring a configuration the nRF never applied.
            try:
                rejected, status = decode_ack(reply.payload)
            except ProtoError:
                raise LinkError("ERR frame with an undecodable payload") from None
            raise LinkRejected(rejected, status)
        return reply

    def ping(self, *, timeout: float = 1.0) -> int:
        """The nRF's uptime in microseconds. Bounds the clock offset to a UART RTT.

        Worth reading before a run: the CONTROL frame's epoch transfer inherits one
        transit as bias on every timestamp that node emits, and this is what
        measures it. See `firmware/nordic/src/air_wire.h`.
        """
        reply = self.request(FrameType.PING, b"", timeout=timeout)
        if len(reply.payload) != 8:
            raise LinkError(f"PONG payload must be 8 bytes, got {len(reply.payload)}")
        return int.from_bytes(reply.payload, "little")
