"""Pi-side BLE scanning for loopback test B.

The half that matters: scan interval and window are the parameters BlueZ's D-Bus
API never exposed, and this is where they become measurable.

Reads advertising reports off the HCI socket, filters to our company ID, and
counts everything it rejects. Filtering happens before any decoding work: a busy
room produces ~200 reports/s of which ~4% are ours, and direction A was voided by
relaying the other 96%. Here they cost a byte comparison each.

The socket is injectable, so parsing and filtering are tested without an adapter.
"""

from __future__ import annotations

import select
import time
from dataclasses import dataclass, field
from typing import Iterator, Protocol

from vertex.radio import find_manufacturer
from vertex.radio.hci import (CommandComplete, HciError, HciSocket,
                              cmd_le_set_scan_enable, cmd_le_set_scan_parameters,
                              cmd_reset, parse_adv_reports, parse_event)

__all__ = ["Scanner", "ScanSink", "Seen", "ScanCounters"]

COMPANY_ID = 0x0059


class ScanSink(Protocol):
    """The slice of HciSocket this module uses."""

    def command(self, packet: bytes, *, timeout: float = 2.0) -> CommandComplete: ...
    def recv(self, size: int = 1024) -> bytes: ...
    def close(self) -> None: ...
    @property
    def fileno(self) -> int: ...


@dataclass(frozen=True, slots=True)
class Seen:
    """One advertisement of ours, with the instant it was read off the socket."""

    payload: bytes          # manufacturer value after the company ID
    ad: bytes               # the complete AdvData, for byte-equality checks
    rssi: int
    addr: bytes
    rx_monotonic: float


@dataclass
class ScanCounters:
    events: int = 0
    reports: int = 0
    ours: int = 0
    foreign: int = 0        # a different company, or no manufacturer element
    malformed: int = 0
    other_events: int = 0   # command completes and the like, arriving mid-scan

    def summary(self) -> str:
        return (f"events={self.events} reports={self.reports} ours={self.ours} "
                f"foreign={self.foreign} malformed={self.malformed}")


@dataclass
class Scanner:
    device: int = 0
    interval_ms: float = 100.0
    window_ms: float = 100.0
    company_id: int = COMPANY_ID
    passive: bool = True
    sock: ScanSink | None = None
    counters: ScanCounters = field(default_factory=ScanCounters, init=False)
    _owns_sock: bool = field(default=False, init=False)
    _enabled: bool = field(default=False, init=False)

    def open(self) -> "Scanner":
        """Reset, set parameters, enable.

        Scan parameters are only settable while scanning is disabled, or the
        controller answers 0x0C command disallowed. Every step goes through
        `command()`, which raises on a non-zero status -- a refused `set scan
        parameters` would leave the previous window in force and the sweep would
        measure a configuration nobody chose. That is the one failure this whole
        test exists to rule out.
        """
        from vertex.radio.hci import ms_to_units

        if self.sock is None:
            self.sock = HciSocket(self.device).open()
            self._owns_sock = True

        interval = ms_to_units(self.interval_ms)
        window = ms_to_units(self.window_ms)
        if window > interval:
            raise HciError(
                f"window {self.window_ms} ms exceeds interval {self.interval_ms} ms")

        self.sock.command(cmd_reset())
        self.sock.command(cmd_le_set_scan_parameters(
            interval=interval, window=window,
            scan_type=0x00 if self.passive else 0x01))
        # Duplicate filtering OFF: a suppressed duplicate is indistinguishable
        # from a lost packet, which is exactly the number being measured.
        self.sock.command(cmd_le_set_scan_enable(True, filter_duplicates=False))
        self._enabled = True
        return self

    @property
    def duty_cycle(self) -> float:
        return self.window_ms / self.interval_ms

    def drain(self, timeout: float = 0.0) -> Iterator[Seen]:
        """Read whatever is waiting and yield our advertisements.

        Non-blocking by default. `select` rather than a blocking recv so the
        caller keeps control of its own period.
        """
        if self.sock is None or not self._enabled:
            raise HciError("drain() before open()")

        deadline = time.monotonic() + timeout
        while True:
            remaining = max(0.0, deadline - time.monotonic())
            ready, _, _ = select.select([self.sock.fileno], [], [], remaining)
            if not ready:
                return
            try:
                packet = self.sock.recv(1024)
            except OSError:
                return
            yield from self._consume(packet)
            if timeout == 0.0 and time.monotonic() >= deadline:
                return

    def _consume(self, packet: bytes) -> Iterator[Seen]:
        now = time.monotonic()
        self.counters.events += 1
        try:
            event = parse_event(packet)
        except HciError:
            self.counters.malformed += 1
            return
        if event.code != 0x3E:
            self.counters.other_events += 1
            return

        # parse_adv_reports is a generator because num_reports can exceed 1: the
        # controller batches, each report carrying its own variable-length data.
        # Assuming one per event silently drops traffic under load, which looks
        # exactly like radio loss.
        for report in parse_adv_reports(event.params):
            self.counters.reports += 1
            payload = find_manufacturer(report.data, self.company_id)
            if payload is None:
                self.counters.foreign += 1
                continue
            self.counters.ours += 1
            yield Seen(payload, report.data, report.rssi, report.addr, now)

    def close(self) -> None:
        if self.sock is not None and self._enabled:
            try:
                self.sock.command(cmd_le_set_scan_enable(False))
            except Exception:
                pass
            self._enabled = False
        if self.sock is not None and self._owns_sock:
            self.sock.close()
            self.sock = None

    def __enter__(self) -> "Scanner":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()
