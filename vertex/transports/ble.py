"""BLE broadcast transport -- the `bridge` agent's path.

The `bridge` agent runs the control law on the Pi and publishes it over the Pi's
own radio, so this is the transport that makes BLE and Wi-Fi comparable with the
*same* controller on both sides. `ble` agents do not use this module at all: their
law and their radio both live on the nRF (see `vertex.agent.relay`).

## One socket, two directions

The user channel is exclusive: one socket carries commands, command completions
and advertising reports together. `HciSocket.command()` blocks reading until its
completion arrives and **discards** everything in between -- fine during setup,
ruinous during a run, because `publish()` issues a command every control period
and each one would silently eat the advertising reports queued behind it. Those
losses would be indistinguishable from radio loss, which is the number this
platform exists to measure.

So once the run starts there is exactly one reader: `_pump`, installed with
`loop.add_reader`. Advertising reports go to the receive callback; command
completions are routed to whoever is awaiting that opcode. Setup runs
synchronously *before* the pump is installed and before scanning is enabled, so
nothing can be in flight to lose.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from ..clock import Clock
from ..radio import (AD_FLAGS, AD_MANUFACTURER, MAX_AD_LEN, build_ad, element,
                     find_manufacturer)
from ..radio.hci import (EVT_COMMAND_COMPLETE, EVT_COMMAND_STATUS, EVT_LE_META,
                         ADV_NONCONN_IND, CHANNELS_ALL, HciError, HciSocket,
                         HciStatus, cmd_le_set_adv_data, cmd_le_set_adv_enable,
                         cmd_le_set_adv_parameters, cmd_le_set_scan_enable,
                         cmd_le_set_scan_parameters, cmd_reset, ms_to_units,
                         parse_adv_reports, parse_command_complete,
                         parse_command_status, parse_event)
from ..wire import DecodeError, StatePacket, decode_any
from ..wire.codec import COMPANY_ID
from .base import Reception, ReceiveCallback, Transport

__all__ = ["BleStats", "BleTransport"]

#: LE Limited/General Discoverable is wrong for a non-connectable beacon; 0x04 is
#: "BR/EDR not supported" alone, which is what this is.
FLAGS_LE_ONLY = bytes([0x04])


@dataclass
class BleStats:
    """What the radio did. Every rejection is counted, none are silent."""

    sent: int = 0
    send_errors: int = 0
    send_timeouts: int = 0
    events: int = 0
    reports: int = 0
    foreign: int = 0            # a different company id, or no manufacturer element
    self_filtered: int = 0
    undecodable: int = 0
    delivered: int = 0
    other_events: int = 0
    last_error: str | None = None

    def summary(self) -> str:
        return (f"sent={self.sent} (errors {self.send_errors}, "
                f"timeouts {self.send_timeouts}), reports={self.reports} -> "
                f"delivered={self.delivered}, own={self.self_filtered}, "
                f"foreign={self.foreign}, undecodable={self.undecodable}")


class BleTransport(Transport):
    """Best-effort state broadcast over BLE advertising.

    Parameters
    ----------
    node_id:
        This agent's id, used to discard our own advertisements. Filtering by id
        rather than by address matters here for the same reason as on UDP, plus
        one more: with privacy off the controller still reports our own packets
        when another Pi relays them.
    clock:
        Supplies ``now_us`` for the receive timestamp, on the experiment epoch.
    adv_interval_ms / scan_interval_ms / scan_window_ms:
        The parameters BlueZ never exposed. Recorded in ``RunMeta.environment``
        because a 6x effect that cannot be reconstructed afterwards must travel
        with the data.
    channel_map:
        Bitmask over advertising channels 37/38/39 (2402/2426/2480 MHz). The
        default uses all three. Restricting it is how a run avoids the WLAN
        channel in use.
    sock:
        Injected for testing. When ``None`` a user-channel socket is opened on
        ``device``, which needs the adapter down and ``CAP_NET_ADMIN``.
    """

    name = "ble"

    def __init__(
        self,
        node_id: int,
        clock: Clock,
        *,
        device: int = 0,
        adv_interval_ms: float = 100.0,
        scan_interval_ms: float = 100.0,
        scan_window_ms: float = 100.0,
        channel_map: int = CHANNELS_ALL,
        passive_scan: bool = True,
        company_id: int = COMPANY_ID,
        command_timeout: float = 0.25,
        sock=None,
    ) -> None:
        self.node_id = node_id
        self.clock = clock
        self.device = device
        self.adv_interval_ms = adv_interval_ms
        self.scan_interval_ms = scan_interval_ms
        self.scan_window_ms = scan_window_ms
        self.channel_map = channel_map
        self.passive_scan = passive_scan
        self.company_id = company_id
        self.command_timeout = command_timeout
        self.stats = BleStats()

        self._sock = sock
        self._owns_sock = False
        self._on_receive: ReceiveCallback | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._pumping = False
        self._advertising = False
        self._scanning = False

    # the parameters, as recorded:
    def parameters(self) -> dict:
        """Radio configuration, for ``RunMeta.environment``.
        """
        return {
            "transport": "ble",
            "device": self.device,
            "adv_interval_ms": self.adv_interval_ms,
            "adv_interval_units": ms_to_units(self.adv_interval_ms),
            "scan_interval_ms": self.scan_interval_ms,
            "scan_interval_units": ms_to_units(self.scan_interval_ms),
            "scan_window_ms": self.scan_window_ms,
            "scan_window_units": ms_to_units(self.scan_window_ms),
            "scan_duty_cycle": self.scan_window_ms / self.scan_interval_ms,
            "channel_map": self.channel_map,
            "scan_type": "passive" if self.passive_scan else "active",
            "company_id": self.company_id,
        }

    # lifecycle:
    async def start(self, on_receive: ReceiveCallback) -> None:
        if self._pumping:
            return
        if self.scan_window_ms > self.scan_interval_ms:
            raise HciError(f"scan window {self.scan_window_ms} ms exceeds "
                           f"interval {self.scan_interval_ms} ms")
        self._on_receive = on_receive

        if self._sock is None:
            self._sock = HciSocket(self.device).open()
            self._owns_sock = True
            # Only on a socket we opened: an injected one may already be configured.
            self._sock.command(cmd_reset())

        adv_units = ms_to_units(self.adv_interval_ms)
        self._sock.command(cmd_le_set_adv_enable(False),
                           tolerate=(HciStatus.COMMAND_DISALLOWED,))
        self._sock.command(cmd_le_set_adv_parameters(
            interval_min=adv_units, interval_max=adv_units,
            adv_type=ADV_NONCONN_IND, channel_map=self.channel_map))
        # Something valid must be advertised before enabling, or the controller
        # radiates whatever was left in its buffer from the previous run.
        self._sock.command(cmd_le_set_adv_data(self._ad(self._idle_packet())))
        self._sock.command(cmd_le_set_adv_enable(True))
        self._advertising = True

        # Tolerating 0x0C on the *disable* only: the CYW43455 rejects disabling
        # something already disabled as an invalid state transition rather than
        # treating it as a no-op. The set below is not tolerated -- a refused
        # `set scan parameters` leaves the previous window in force, and the run
        # then measures a configuration nobody chose.
        self._sock.command(cmd_le_set_scan_enable(False),
                           tolerate=(HciStatus.COMMAND_DISALLOWED,))
        self._sock.command(cmd_le_set_scan_parameters(
            interval=ms_to_units(self.scan_interval_ms),
            window=ms_to_units(self.scan_window_ms),
            scan_type=0x00 if self.passive_scan else 0x01))

        # Reader first, then scanning: the reverse order leaves a window in which
        # reports arrive with nobody to route them.
        loop = asyncio.get_running_loop()
        loop.add_reader(self._sock.fileno, self._pump)
        self._pumping = True

        # Duplicate filtering OFF. A suppressed duplicate is indistinguishable
        # from a lost packet, which is the number being measured.
        self._sock.command(cmd_le_set_scan_enable(True, filter_duplicates=False))
        self._scanning = True

    async def stop(self) -> None:
        """Safe to call twice, and safe to call on a half-started transport."""
        if self._pumping and self._sock is not None:
            try:
                asyncio.get_running_loop().remove_reader(self._sock.fileno)
            except (RuntimeError, HciError):
                pass
            self._pumping = False

        for fut in self._pending.values():
            if not fut.done():
                fut.cancel()
        self._pending.clear()

        if self._sock is not None:
            for enabled, off in ((self._scanning, cmd_le_set_scan_enable(False)),
                                 (self._advertising, cmd_le_set_adv_enable(False))):
                if not enabled:
                    continue
                try:
                    self._sock.command(off, tolerate=(HciStatus.COMMAND_DISALLOWED,))
                except Exception:
                    # Teardown is best-effort: the run's data is already written,
                    # and raising here would mask whatever ended the run.
                    pass
            self._scanning = self._advertising = False
            if self._owns_sock:
                self._sock.close()
                self._sock = None
                self._owns_sock = False
        self._on_receive = None

    # send:
    def _idle_packet(self) -> StatePacket:
        """Placeholder AD for the gap between enabling and the first publish."""
        return StatePacket(node_id=self.node_id, vstate=0, seq=0, enabled=False)

    def _ad(self, packet: StatePacket) -> bytes:
        """Complete AdvData for one packet.
        """
        # Manufacturer element ONLY -- no flags. Flags is optional for
        # non-connectable undirected advertising, and dropping it makes this AD
        # byte-for-byte the same size and composition as the nRF's, which now sends
        # no name either. Before: nRF 29 bytes, this 23; the 6-byte difference was
        # 144 us of TX airtime per advertising event separating the two arms of the
        # comparison. See PLATFORM.md 8b.A3.
        return build_ad(
            element(AD_MANUFACTURER,
                    self.company_id.to_bytes(2, "little") + packet.encode()),
            limit=MAX_AD_LEN,
        )

    async def publish(self, packet: StatePacket) -> None:
        """Replace the advertising data. Best-effort; failures are counted.

        A send failure is loss. It is counted rather than raised for the same
        reason as on UDP: the publish loop must keep its period, and the receiver
        is what measures delivery anyway.
        """
        if self._sock is None or not self._pumping:
            raise RuntimeError("publish() before start()")
        cmd = cmd_le_set_adv_data(self._ad(packet))
        try:
            await self._command(cmd)
            self.stats.sent += 1
        except asyncio.TimeoutError:
            self.stats.send_timeouts += 1
            self.stats.last_error = "no command complete for LE Set Adv Data"
        except Exception as exc:
            self.stats.send_errors += 1
            self.stats.last_error = repr(exc)

    async def _command(self, packet: bytes) -> None:
        """Issue a command while the pump owns the socket.

        The completion arrives on the pump, not here, so this registers a future
        keyed by opcode and waits for the pump to resolve it.
        """
        op = int.from_bytes(packet[1:3], "little")
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        # One outstanding command per opcode. publish() is the only caller in a
        # run and it is serialised by the control period, so a collision here
        # means two publishers on one radio -- worth failing loudly.
        if op in self._pending and not self._pending[op].done():
            raise HciError(f"opcode 0x{op:04X} is already outstanding")
        self._pending[op] = fut
        try:
            self._sock.send(packet)
            await asyncio.wait_for(fut, self.command_timeout)
        finally:
            self._pending.pop(op, None)

    # receive:
    def _pump(self) -> None:
        """Read whatever is ready and route it. Never raises.

        Installed with ``add_reader``, so an exception escaping here would kill
        the event loop's reader and the run would go quiet without failing.
        """
        try:
            packet = self._sock.recv(1024)
        except (OSError, HciError):
            return
        self.stats.events += 1
        try:
            event = parse_event(packet)
        except HciError:
            self.stats.other_events += 1
            return

        if event.code == EVT_LE_META:
            self._consume_reports(event.params)
            return

        if event.code == EVT_COMMAND_COMPLETE:
            try:
                cc = parse_command_complete(event.params)
            except HciError:
                return
            self._settle(cc.opcode, None if cc.ok else HciError(cc.describe()))
            return

        if event.code == EVT_COMMAND_STATUS:
            try:
                cs = parse_command_status(event.params)
            except HciError:
                return
            if not cs.ok:
                self._settle(cs.opcode,
                             HciError(f"opcode 0x{cs.opcode:04X}: status "
                                      f"0x{cs.status:02X}"))
            return

        self.stats.other_events += 1

    def _settle(self, op: int, error: Exception | None) -> None:
        fut = self._pending.get(op)
        if fut is None or fut.done():
            return
        if error is None:
            fut.set_result(None)
        else:
            fut.set_exception(error)

    def _consume_reports(self, params: bytes) -> None:
        rx_time_us = self.clock.now_us()
        for report in parse_adv_reports(params):
            self.stats.reports += 1
            # Filter before decoding:
            payload = find_manufacturer(report.data, self.company_id)
            if payload is None:
                self.stats.foreign += 1
                continue
            try:
                # decode_any, not the v1 decoder. Both sides speak v1 now.
                packet = decode_any(payload)
            except DecodeError:
                self.stats.undecodable += 1
                continue
            except Exception as exc:                        # pragma: no cover
                self.stats.undecodable += 1
                self.stats.last_error = repr(exc)
                continue

            if packet.node_id == self.node_id:
                self.stats.self_filtered += 1
                continue

            cb = self._on_receive
            if cb is None:
                continue
            try:
                cb(Reception(packet=packet, rx_time_us=rx_time_us))
                self.stats.delivered += 1
            except Exception as exc:                        # pragma: no cover
                self.stats.last_error = repr(exc)
