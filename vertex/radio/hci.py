"""Raw HCI over an exclusive user channel.

`HCI_CHANNEL_USER`, not `HCI_CHANNEL_RAW`. RAW leaves BlueZ managing the
controller, so the daemon can overwrite advertising and scanning parameters
underneath you. USER takes the device exclusively and requires it to be down
first (`hciconfig hci0 down`). Coexistence is unaffected: arbitration lives inside
the combo chip, not the host stack.

Packet framing on the socket:

    command  0x01 | opcode(2 LE) | plen(1) | params
    event    0x04 | event(1)     | plen(1) | params

Everything above the socket is a pure function over bytes, so command encoding
and report parsing are tested without hardware.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import errno
import os
import socket
import struct
from dataclasses import dataclass
from typing import Iterator

__all__ = [
    "AF_BLUETOOTH", "BTPROTO_HCI", "HCI_CHANNEL_USER", "HCI_CHANNEL_RAW",
    "HCI_COMMAND_PKT", "HCI_EVENT_PKT", "OGF_HOST_CTL", "OGF_LE_CTL",
    "OCF", "opcode", "HciError", "HciStatus", "STATUS_NAMES",
    "cmd_reset", "cmd_le_set_adv_parameters", "cmd_le_set_adv_data",
    "cmd_le_set_adv_enable", "cmd_le_set_scan_parameters", "cmd_le_set_scan_enable",
    "ms_to_units", "units_to_ms",
    "Event", "CommandComplete", "CommandStatus", "AdvReport",
    "parse_event", "parse_adv_reports", "HciSocket", "sockaddr_hci",
    "ADV_NONCONN_IND", "ADV_IND", "SCAN_PASSIVE", "SCAN_ACTIVE", "CHANNELS_ALL",
]

AF_BLUETOOTH = 31
BTPROTO_HCI = 1
HCI_CHANNEL_RAW = 0
HCI_CHANNEL_USER = 1

HCI_COMMAND_PKT = 0x01
HCI_ACLDATA_PKT = 0x02
HCI_EVENT_PKT = 0x04

OGF_HOST_CTL = 0x03
OGF_LE_CTL = 0x08


def opcode(ogf: int, ocf: int) -> int:
    """Combination of Opcode Gorup Field (OGF) shifted 
    by 10 bits with and Opcode Command Field (OCP):  
    
    (OGF << 10) | OCF. Little-endian on the wire."""
    return (ogf << 10) | ocf


class OCF:
    RESET = 0x0003
    LE_SET_ADV_PARAMETERS = 0x0006
    LE_SET_ADV_DATA = 0x0008
    LE_SET_ADV_ENABLE = 0x000A
    LE_SET_SCAN_PARAMETERS = 0x000B
    LE_SET_SCAN_ENABLE = 0x000C


OP_RESET = opcode(OGF_HOST_CTL, OCF.RESET)
OP_LE_SET_ADV_PARAMETERS = opcode(OGF_LE_CTL, OCF.LE_SET_ADV_PARAMETERS)
OP_LE_SET_ADV_DATA = opcode(OGF_LE_CTL, OCF.LE_SET_ADV_DATA)
OP_LE_SET_ADV_ENABLE = opcode(OGF_LE_CTL, OCF.LE_SET_ADV_ENABLE)
OP_LE_SET_SCAN_PARAMETERS = opcode(OGF_LE_CTL, OCF.LE_SET_SCAN_PARAMETERS)
OP_LE_SET_SCAN_ENABLE = opcode(OGF_LE_CTL, OCF.LE_SET_SCAN_ENABLE)

# advertising types
ADV_IND = 0x00
ADV_NONCONN_IND = 0x03          # broadcast only; we never accept connections
ADV_SCAN_IND = 0x06

SCAN_PASSIVE = 0x00
SCAN_ACTIVE = 0x01              # transmits scan requests: costs airtime

CHANNELS_ALL = 0x07             # 37 | 38 | 39
CHANNEL_37, CHANNEL_38, CHANNEL_39 = 0x01, 0x02, 0x04

#: Intervals and windows are in 0.625 ms units.
UNIT_US = 625


def ms_to_units(ms: float) -> int:
    """Milliseconds to 0.625 ms units, as the controller expects."""
    units = round(ms * 1000 / UNIT_US)
    if not 0x0004 <= units <= 0x4000:
        raise HciError(
            f"{ms} ms is {units} units, outside the 0x0004..0x4000 range "
            f"({0x0004 * UNIT_US / 1000:.2f}..{0x4000 * UNIT_US / 1000:.1f} ms)")
    return units


def units_to_ms(units: int) -> float:
    return units * UNIT_US / 1000.0


class HciError(RuntimeError):
    """Command refused, adapter unavailable, or a malformed packet."""


class HciStatus:
    SUCCESS = 0x00
    UNKNOWN_COMMAND = 0x01
    COMMAND_DISALLOWED = 0x0C
    INVALID_PARAMETERS = 0x12


STATUS_NAMES = {
    0x00: "success",
    0x01: "unknown command",
    0x0C: "command disallowed (is the function still enabled?)",
    0x11: "unsupported feature or parameter value",
    0x12: "invalid HCI command parameters",
    0x1A: "unsupported remote feature",
}


# ── command builders ─────────────────────────────────────────────────────────

def _cmd(op: int, params: bytes = b"") -> bytes:
    if len(params) > 255:
        raise HciError(f"parameter block of {len(params)} exceeds 255")
    return bytes([HCI_COMMAND_PKT]) + struct.pack("<H", op) + bytes([len(params)]) + params


def cmd_reset() -> bytes:
    return _cmd(OP_RESET)


def cmd_le_set_adv_parameters(*, interval_min: int, interval_max: int,
                              adv_type: int = ADV_NONCONN_IND,
                              own_addr_type: int = 0x00,
                              channel_map: int = CHANNELS_ALL,
                              filter_policy: int = 0x00) -> bytes:
    """15 bytes. Only settable while advertising is disabled."""
    if interval_min > interval_max:
        raise HciError(f"interval_min ({interval_min}) exceeds max ({interval_max})")
    if not 0x01 <= channel_map <= 0x07:
        raise HciError(f"channel_map must select at least one channel, got {channel_map:#x}")
    return _cmd(OP_LE_SET_ADV_PARAMETERS, struct.pack(
        "<HHBBB", interval_min, interval_max, adv_type, own_addr_type, 0x00)
        + b"\x00" * 6 + bytes([channel_map, filter_policy]))


def cmd_le_set_adv_data(ad: bytes) -> bytes:
    """32 bytes of parameters: significant length, then a fixed 31-byte field.

    The field is always 31 bytes, zero-padded. Sending a short parameter block is
    a common cause of `invalid HCI command parameters`.
    """
    if len(ad) > 31:
        raise HciError(f"advertising data is {len(ad)} bytes, limit is 31")
    return _cmd(OP_LE_SET_ADV_DATA, bytes([len(ad)]) + ad + b"\x00" * (31 - len(ad)))


def cmd_le_set_adv_enable(enable: bool) -> bytes:
    return _cmd(OP_LE_SET_ADV_ENABLE, bytes([0x01 if enable else 0x00]))


def cmd_le_set_scan_parameters(*, interval: int, window: int,
                               scan_type: int = SCAN_PASSIVE,
                               own_addr_type: int = 0x00,
                               filter_policy: int = 0x00) -> bytes:
    """7 bytes. Window must not exceed interval; equal means 100% duty."""
    if window > interval:
        raise HciError(f"scan window ({window}) must be <= interval ({interval})")
    return _cmd(OP_LE_SET_SCAN_PARAMETERS, struct.pack(
        "<BHHBB", scan_type, interval, window, own_addr_type, filter_policy))


def cmd_le_set_scan_enable(enable: bool, *, filter_duplicates: bool = False) -> bytes:
    """Duplicate filtering defaults off: a suppressed duplicate is
    indistinguishable from a lost packet, which makes loss unmeasurable."""
    return _cmd(OP_LE_SET_SCAN_ENABLE,
                bytes([0x01 if enable else 0x00, 0x01 if filter_duplicates else 0x00]))


# ── event parsing ────────────────────────────────────────────────────────────

EVT_COMMAND_COMPLETE = 0x0E
EVT_COMMAND_STATUS = 0x0F
EVT_LE_META = 0x3E
SUBEVT_ADV_REPORT = 0x02


@dataclass(frozen=True, slots=True)
class Event:
    code: int
    params: bytes


@dataclass(frozen=True, slots=True)
class CommandComplete:
    opcode: int
    status: int
    params: bytes

    @property
    def ok(self) -> bool:
        return self.status == HciStatus.SUCCESS

    def describe(self) -> str:
        return (f"opcode 0x{self.opcode:04X}: "
                f"{STATUS_NAMES.get(self.status, f'status 0x{self.status:02X}')}")


@dataclass(frozen=True, slots=True)
class CommandStatus:
    opcode: int
    status: int

    @property
    def ok(self) -> bool:
        return self.status == HciStatus.SUCCESS


@dataclass(frozen=True, slots=True)
class AdvReport:
    event_type: int
    addr_type: int
    addr: bytes                 # little-endian, as on the wire
    data: bytes
    rssi: int

    @property
    def addr_str(self) -> str:
        return ":".join(f"{b:02X}" for b in reversed(self.addr))


def parse_event(packet: bytes) -> Event:
    """Split an event packet into code and parameters."""
    if len(packet) < 3:
        raise HciError(f"event packet of {len(packet)} bytes is truncated")
    if packet[0] != HCI_EVENT_PKT:
        raise HciError(f"not an event packet: first byte 0x{packet[0]:02X}")
    code, plen = packet[1], packet[2]
    params = packet[3:3 + plen]
    if len(params) != plen:
        raise HciError(f"event declares {plen} parameter bytes, carries {len(params)}")
    return Event(code, params)


def parse_command_complete(params: bytes) -> CommandComplete:
    if len(params) < 4:
        raise HciError("command complete is truncated")
    op = struct.unpack_from("<H", params, 1)[0]
    return CommandComplete(op, params[3], params[4:])


def parse_command_status(params: bytes) -> CommandStatus:
    if len(params) < 4:
        raise HciError("command status is truncated")
    return CommandStatus(struct.unpack_from("<H", params, 2)[0], params[0])


def parse_adv_reports(params: bytes) -> Iterator[AdvReport]:
    """Walk an LE Advertising Report subevent.

    `num_reports` can exceed 1: the controller batches reports, each with its own
    variable-length data, so they must be walked sequentially. A parser that
    assumes one report per event silently drops traffic under load -- which looks
    exactly like radio loss.
    """
    if not params or params[0] != SUBEVT_ADV_REPORT:
        return
    num = params[1] if len(params) > 1 else 0
    off = 2
    for _ in range(num):
        if off + 9 > len(params):
            return                          # truncated: yield what parsed
        event_type = params[off]
        addr_type = params[off + 1]
        addr = params[off + 2:off + 8]
        dlen = params[off + 8]
        if off + 9 + dlen + 1 > len(params):
            return
        data = params[off + 9:off + 9 + dlen]
        rssi = struct.unpack_from("<b", params, off + 9 + dlen)[0]
        yield AdvReport(event_type, addr_type, addr, data, rssi)
        off += 9 + dlen + 1


# socket: 

class _SockaddrHci(ctypes.Structure):
    """struct sockaddr_hci -- family, device, channel. Six bytes."""

    _fields_ = [("hci_family", ctypes.c_ushort),
                ("hci_dev", ctypes.c_ushort),
                ("hci_channel", ctypes.c_ushort)]


def sockaddr_hci(device: int, channel: int = HCI_CHANNEL_USER) -> bytes:
    """Pack a sockaddr_hci. Exposed so the layout is testable without an adapter."""
    return bytes(_SockaddrHci(AF_BLUETOOTH, device, channel))


def _bind_user_channel(fd: int, device: int) -> None:
    """bind(2) with a hand-packed sockaddr_hci.

    Python's socket module accepts only ``(device,)`` for BTPROTO_HCI -- there is
    no way to pass a channel, and the two-tuple form fails with "wrong format".
    Since the user channel is the entire point (RAW leaves BlueZ managing the
    controller), the bind has to go through libc directly.
    """
    libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
    addr = _SockaddrHci(AF_BLUETOOTH, device, HCI_CHANNEL_USER)
    if libc.bind(fd, ctypes.byref(addr), ctypes.sizeof(addr)) != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))

def _bind_hint(device: int, exc: OSError) -> str:
    """Turn a bind errno into the specific thing to do about it.

    EBUSY and EPERM need opposite fixes, and the generic message sent people to
    `hciconfig down` when the real cause was a socket the previous sweep point had
    not finished releasing.
    """
    base = f"cannot take hci{device} on the user channel ({exc})"
    if exc.errno == errno.EBUSY:
        return (f"{base}.\n"
                f"  The adapter is up, or another socket still holds it.\n"
                f"    sudo hciconfig hci{device} down\n"
                f"  If this happened partway through a parameter sweep, the cause is\n"
                f"  rebinding per measurement: the kernel does not release the device\n"
                f"  instantly. Bind once and change parameters instead -- scan and\n"
                f"  advertising parameters are settable after disabling the function,\n"
                f"  with no need to reopen the socket.")
    if exc.errno in (errno.EPERM, errno.EACCES):
        return (f"{base}.\n"
                f"  The user channel needs CAP_NET_ADMIN. Either run as root, or:\n"
                f"    sudo setcap cap_net_admin,cap_net_raw+eip $(readlink -f $(which python3))")
    if exc.errno == errno.ENODEV:
        return f"{base}.\n  No such adapter. Check `hciconfig -a`."
    return (f"{base}.\n"
            f"  Expected: adapter down, and CAP_NET_ADMIN.\n"
            f"    sudo hciconfig hci{device} down")


class HciSocket:
    """Exclusive HCI access to one adapter.

    The adapter must be down first, or the bind fails with EBUSY:

        sudo hciconfig hci0 down
    """

    def __init__(self, device: int = 0) -> None:
        self.device = device
        self._sock: socket.socket | None = None

    def open(self) -> "HciSocket":
        s = socket.socket(AF_BLUETOOTH, socket.SOCK_RAW, BTPROTO_HCI)
        try:
            _bind_user_channel(s.fileno(), self.device)
        except OSError as exc:
            s.close()
            raise HciError(_bind_hint(self.device, exc)) from None
        self._sock = s
        return self

    def close(self) -> None:
        if self._sock is not None:
            self._sock.close()
            self._sock = None

    def __enter__(self) -> "HciSocket":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def fileno(self) -> int:
        if self._sock is None:
            raise HciError("socket is not open")
        return self._sock.fileno()

    def send(self, packet: bytes) -> None:
        if self._sock is None:
            raise HciError("socket is not open")
        self._sock.send(packet)

    def recv(self, size: int = 1024) -> bytes:
        if self._sock is None:
            raise HciError("socket is not open")
        return self._sock.recv(size)

    def command(self, packet: bytes, *, timeout: float = 2.0) -> CommandComplete:
        """Send a command and wait for its completion, raising on refusal.

        Checking every setup command matters: a refused `set scan parameters`
        leaves the previous window in force, and the run then measures a
        configuration nobody chose.
        """
        if self._sock is None:
            raise HciError("socket is not open")
        want = struct.unpack_from("<H", packet, 1)[0]
        self._sock.send(packet)
        self._sock.settimeout(timeout)
        while True:
            try:
                evt = parse_event(self._sock.recv(1024))
            except socket.timeout:
                raise HciError(f"no response to opcode 0x{want:04X} in {timeout}s") from None
            if evt.code == EVT_COMMAND_COMPLETE:
                cc = parse_command_complete(evt.params)
                if cc.opcode != want:
                    continue
                if not cc.ok:
                    raise HciError(cc.describe())
                return cc
            if evt.code == EVT_COMMAND_STATUS:
                cs = parse_command_status(evt.params)
                if cs.opcode == want and not cs.ok:
                    raise HciError(
                        f"opcode 0x{want:04X}: "
                        f"{STATUS_NAMES.get(cs.status, f'status 0x{cs.status:02X}')}")
