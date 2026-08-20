"""The `ble` agent: a relay to a controller running on the nRF.

Unlike `wifi` and `bridge`, a BLE agent runs no control law on the Pi.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

from ..numeric import quantize
from ..serial import (FrameType, StateReport, build_frame, decode_ack,
                      decode_state, encode_algorithm, encode_control,
                      encode_disturbance, encode_network, encode_radio)
from .assignment import AgentAssignment

__all__ = ["RelayError", "RelayCounters", "assignment_to_frames",
           "radio_frame", "BleRelay"]


class RelayError(RuntimeError):
    """The nRF refused a configuration frame, or stopped reporting."""


@dataclass
class RelayCounters:
    reports: int = 0
    malformed: int = 0
    rejected: int = 0
    frames_sent: int = 0

    def summary(self) -> str:
        return (f"reports={self.reports} malformed={self.malformed} "
                f"rejected={self.rejected} sent={self.frames_sent}")


# engineering units -> the nRF's fixed point:

def assignment_to_frames(a: AgentAssignment, *,
                         trigger: bool | None = None,
                         epoch_us: int = 0) -> list[tuple[int, bytes]]:
    """Translate one assignment into the frames the nRF expects.

    Order matters: network, then algorithm, then disturbance, and only then the
    trigger -- the trigger latches initial conditions, seeds the nRF's PRNG and
    hands it this host's clock reading, so it must arrive last.
    """
    d = a.disturbance
    frames: list[tuple[int, bytes]] = [
        (FrameType.NETWORK, encode_network(enabled=a.enabled, node_id=a.node_id,
                                           neighbors=list(a.neighbors))),
        (FrameType.ALGORITHM, encode_algorithm(
            dt_ms=int(round(a.dt_s * 1000)),
            clock_ms=int(round(a.publish_period_s * 1000)),
            state0=quantize(a.state), vstate0=quantize(a.vstate),
            vartheta0=quantize(a.vartheta), counter0=0,
            alpha=quantize(a.alpha), delta=quantize(a.delta),
            eta=quantize(a.eta))),
        (FrameType.DISTURBANCE, encode_disturbance(
            active=bool(d.get("enabled", False)),
            sine_amplitude=quantize(float(d.get("sine_amplitude", 0.0))),
            frequency=quantize(float(d.get("sine_frequency_hz", 0.0))),
            phase=quantize(float(d.get("sine_phase_s", 0.0))),
            noise_amplitude=quantize(float(d.get("noise_amplitude", 0.0))),
            noise_offset=quantize(float(d.get("noise_offset", 0.5))),
            beta=quantize(float(d.get("beta", 0.0))),
            samples=int(d.get("period_samples", 1000)))),
    ]
    if trigger is not None:
        frames.append((FrameType.CONTROL, encode_control(
            trigger=trigger, seed=a.disturbance_seed & 0xFFFFFFFF,
            epoch_us=epoch_us)))
    return frames


def radio_frame(*, adv_interval_ms: float, scan_interval_ms: float,
                scan_window_ms: float, advertising: bool = True,
                active_scan: bool = False) -> tuple[int, bytes]:
    """Radio parameters for the nRF, in 0.625 ms units.
    """
    def units(ms: float) -> int:
        u = int(round(ms * 1000 / 625))
        if not 0x0004 <= u <= 0x4000:
            raise RelayError(f"{ms} ms is {u} units, outside 0x0004..0x4000")
        return u

    if scan_window_ms > scan_interval_ms:
        raise RelayError(
            f"scan window {scan_window_ms} ms exceeds interval {scan_interval_ms} ms")
    return (FrameType.RADIO, encode_radio(
        adv_min=units(adv_interval_ms), adv_max=units(adv_interval_ms),
        scan_interval=units(scan_interval_ms), scan_window=units(scan_window_ms),
        active_scan=active_scan, advertising=advertising))


# the relay:

class BleRelay:
    """Drives the controller on the nRF over a framed serial link.

    `link` is anything with `request(type, payload, timeout) -> Frame` and
    `on_state(callback)` -- `vertex.serial.SerialLink`. Injected rather than
    constructed so the relay is testable without a port.

    The relay registers itself for STATE frames in `__init__`. That registration
    is the entire reporting path, and it was missing: `handle_frame` had no caller
    anywhere and `on_state` appeared only in this docstring. A `ble` agent would
    configure the nRF, start the run and log **zero samples**, while `status()`
    reported `running: true`. The end-to-end check did not catch it because the
    check called `handle_frame` itself -- supplying the wiring it was meant to be
    testing.

    `test/common/peer.py` does NOT satisfy this contract: it routes replies and
    advertising reports only, with no STATE path. It is a loopback harness, not a
    stand-in for the link.
    """

    def __init__(self, link, *, on_report: Callable[[StateReport], None] | None = None,
                 timeout: float = 1.0, clock=None) -> None:
        self.link = link
        #: Supplies the epoch reading sent with the trigger, so the nRF can stamp
        #: `tx_time_us` on the fleet's epoch instead of its own uptime. Without a
        #: clock the nRF sends zero timestamps, and the host skips delay
        #: accounting rather than recording a wrong figure.
        self.clock = clock
        self.timeout = timeout
        self.counters = RelayCounters()
        self.on_report = on_report
        self.assignment: AgentAssignment | None = None
        self.last: StateReport | None = None
        self._t0: float | None = None

        # Subscribed here, not in start(): the nRF logs on boot and on reset, so a
        # STATE frame can arrive before the trigger, and it should be counted
        # rather than dropped by a callback nobody registered.
        register = getattr(link, "on_state", None)
        if register is None:
            raise RelayError(
                f"{type(link).__name__} has no on_state(); a relay cannot receive "
                f"the nRF's reports through it -- use vertex.serial.SerialLink"
            )
        register(self.handle_frame)

    # configuration:
    def _send(self, frame_type: int, payload: bytes) -> None:
        try:
            self.link.request(frame_type, payload, timeout=self.timeout)
        except Exception as exc:
            self.counters.rejected += 1
            raise RelayError(
                f"nRF refused frame 0x{frame_type:02X}: {exc}") from None
        self.counters.frames_sent += 1

    def configure(self, assignment: AgentAssignment, *,
                  radio: tuple[int, bytes] | None = None) -> None:
        """Push an assignment. Does NOT start the run -- call `start()`."""
        for frame_type, payload in assignment_to_frames(assignment):
            self._send(frame_type, payload)
        if radio is not None:
            self._send(*radio)
        self.assignment = assignment

    def start(self) -> None:
        """Trigger the run. Latches initial conditions on the nRF."""
        if self.assignment is None:
            raise RelayError("configure() before start()")
        self._t0 = time.monotonic()
        # Read as late as possible: everything between this line and the nRF
        # latching the frame is bias on every timestamp that node emits.
        epoch_us = max(0, self.clock.now_us()) if self.clock is not None else 0
        # The same substream the host controllers use, so a `ble` agent and a
        # `wifi` agent with the same manifest seed see the same noise sequence.
        self._send(FrameType.CONTROL, encode_control(
            trigger=True, seed=self.assignment.disturbance_seed & 0xFFFFFFFF,
            epoch_us=epoch_us))

    def stop(self) -> None:
        self._send(FrameType.CONTROL, encode_control(trigger=False))

    # reporting:
    def handle_frame(self, frame) -> StateReport | None:
        """Feed an inbound frame. Returns the report if it was one.

        A malformed report is counted and dropped, never raised: the nRF logs on
        boot and reset.
        """
        if frame.type != FrameType.STATE:
            return None
        try:
            report = decode_state(frame.payload)
        except Exception:
            self.counters.malformed += 1
            return None
        self.counters.reports += 1
        self.last = report
        if self.on_report is not None:
            self.on_report(report)
        return report

    @property
    def elapsed_s(self) -> float:
        return 0.0 if self._t0 is None else time.monotonic() - self._t0

    def status(self) -> dict:
        r = self.last
        return {
            "mode": "relay",
            "node_id": self.assignment.node_id if self.assignment else None,
            "configured": self.assignment is not None,
            "running": self._t0 is not None,
            "reports": self.counters.reports,
            "malformed": self.counters.malformed,
            "elapsed_s": round(self.elapsed_s, 3),
            # Scaled int32, as reported.
            "last": None if r is None else {
                "t_us": r.t_us, "state": r.state, "vstate": r.vstate,
                "vartheta": r.vartheta, "counter": r.counter,
                "neighbor_vstates": list(r.neighbor_vstates),
                "neighbor_fresh": list(r.neighbor_fresh),
            },
        }
