"""Pi-side BLE transmit for loopback test A.

Wraps `vertex.radio` into the two operations the test needs: configure the
advertiser once, then replace the payload per transmission.

The socket is injectable, so the command sequence is verified without an adapter.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from vertex.radio import (AD_MANUFACTURER, AD_NAME_COMPLETE, build_ad, element,
                          manufacturer_value)
from vertex.radio.hci import (ADV_NONCONN_IND, CHANNELS_ALL, CommandComplete,
                              HciError, HciSocket, HciStatus, cmd_le_set_adv_data,
                              cmd_le_set_adv_enable, cmd_le_set_adv_parameters,
                              cmd_reset, ms_to_units)
from vertex.wire import StatePacket

__all__ = ["Advertiser", "CommandSink"]

COMPANY_ID = 0x0059


class CommandSink(Protocol):
    """The slice of HciSocket this module uses."""

    def command(self, packet: bytes, *, timeout: float = 2.0,
                tolerate: tuple[int, ...] = ()) -> CommandComplete: ...
    def close(self) -> None: ...


@dataclass
class Advertiser:
    device: int = 0
    interval_ms: float = 100.0
    channel_map: int = CHANNELS_ALL
    name: bytes = b"LABCTRL"
    company_id: int = COMPANY_ID
    sock: CommandSink | None = None          # injected for tests
    _owns_sock: bool = field(default=False, init=False)
    _enabled: bool = field(default=False, init=False)
    transmissions: int = field(default=0, init=False)

    def open(self, initial: StatePacket | None = None) -> "Advertiser":
        """Reset, set parameters, set an initial payload, enable.

        This order is required: parameters are only settable while advertising is
        disabled, or the controller answers 0x0C command disallowed.

        Every step goes through `command()`, which raises on a non-zero status. A
        refused `set adv parameters` would leave the previous interval in force and
        the run would measure a configuration nobody chose.
        """
        if self.sock is None:
            self.sock = HciSocket(self.device).open()
            self._owns_sock = True

        units = ms_to_units(self.interval_ms)
        self.sock.command(cmd_reset())
        # A previous run that died without disabling could leave advertising on,
        # and parameters are only settable while it is off. Tolerate 0x0C for the
        # same reason as the scanner's pre-disable.
        self.sock.command(cmd_le_set_adv_enable(False),
                          tolerate=(HciStatus.COMMAND_DISALLOWED,))
        self.sock.command(cmd_le_set_adv_parameters(
            interval_min=units, interval_max=units,
            adv_type=ADV_NONCONN_IND, channel_map=self.channel_map))
        # A payload must exist before enabling, or the first advertising events
        # carry whatever the controller had left over. It does go on the air, so
        # the caller should supply one it can attribute -- same node id, seq 0.
        self.sock.command(cmd_le_set_adv_data(self.build(
            initial if initial is not None else StatePacket.from_state(1, 0.0, seq=0))))
        self.sock.command(cmd_le_set_adv_enable(True))
        self._enabled = True
        return self

    def build(self, packet: StatePacket) -> bytes:
        """AdvData for one packet: name element, then manufacturer element."""
        return build_ad(
            element(AD_NAME_COMPLETE, self.name),
            element(AD_MANUFACTURER,
                    manufacturer_value(self.company_id, packet.encode())))

    def advertise(self, packet: StatePacket) -> bytes:
        """Replace the payload. Returns the exact AdvData put on the air.

        Only 0x2008 -- advertising must stop for a *parameter* change, not a data
        change. Stopping and restarting would reset the advertising cadence on
        every packet and make an interval sweep meaningless.

        The return value is what the caller compares reports against. Re-deriving
        it at comparison time would compare the encoder with itself, and a broken
        encoder would pass.
        """
        if self.sock is None or not self._enabled:
            raise HciError("advertise() before open()")
        ad = self.build(packet)
        self.sock.command(cmd_le_set_adv_data(ad))
        self.transmissions += 1
        return ad

    def close(self) -> None:
        """Disable advertising, then release the socket. Safe to call twice."""
        if self.sock is not None and self._enabled:
            try:
                self.sock.command(cmd_le_set_adv_enable(False))
            except Exception:
                pass
            self._enabled = False
        if self.sock is not None and self._owns_sock:
            self.sock.close()
            self.sock = None

    def __enter__(self) -> "Advertiser":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()
