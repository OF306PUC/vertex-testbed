"""Versioned binary state packet, shared by the BLE-advertising and UDP transports.

Layout v1 -- 16 bytes, little-endian throughout::

    off  size  type    field
    0    1     uint8   version (= 1)
    1    1     uint8   flags: bit0 enabled, bit1 disturbance_on
    2    1     uint8   node_id (1..255)
    3    1     uint8   reserved (must be 0; rejected otherwise, so v1.x can use it)
    4    2     uint16  seq        -- wraps mod 2**16
    6    4     int32   vstate     -- scaled by SCALE_FACTOR
    10   6     uint48  tx_time_us -- microseconds since the experiment epoch

BLE advertising budget -- 31 bytes of AD data total::

    complete local name "LABCTRL"    1 + 1 + 7            =  9
    manufacturer data (v1 payload)   1 + 1 + 2 + 16       = 20
                                                            --
                                                            29   (2 spare)

``V0_*`` handles the 6-byte format currently on the air. That format is the nRF
firmware's ``custom_data_type`` struct from ``nordic/src/common.h``::

    uint16 manufacturer;   // stripped by the BLE stack as the company ID
    uint8  netid_enabled;  // 0x7F enabled / 0x70 disabled
    uint8  node;
    int32  vstate;
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

from ..numeric import dequantize, quantize

__all__ = [
    "VERSION", "PAYLOAD_SIZE", "SCALE_FACTOR", "COMPANY_ID",
    "BLE_ADV_BUDGET", "BLE_AD_OVERHEAD",
    "StatePacket", "DecodeError",
    "encode_manufacturer_data", "decode_manufacturer_data",
    "V0_PAYLOAD_SIZE", "V0_FLAG_ENABLED", "V0_FLAG_DISABLED",
    "encode_v0", "decode_v0", "decode_any",
    "LinkMonitor", "LinkStats",
]

VERSION = 1
PAYLOAD_SIZE = 16
SCALE_FACTOR = 1_000_000            # shared with the firmware; see vertex.numeric
COMPANY_ID = 0x0059                 # Nordic Semiconductor, per nordic/src/common.h

#: Neighbour-array size compiled into the nRF firmware (``N_MAX_NEIGHBORS``).
N_MAX_NEIGHBORS_FIRMWARE = 4

BLE_ADV_BUDGET  = 31                 # AD bytes in a legacy-advertising PDU
BLE_AD_OVERHEAD = 9 + 4              # name element (9) + mfr-data header (1+1+2)

_FLAG_ENABLED = 0b0000_0001
_FLAG_DISTURBANCE = 0b0000_0010

_HEAD = struct.Struct("<BBBBHi")    # through vstate: 10 bytes
_STRUCT_SIZE = _HEAD.size           # 10; tx_time_us is a hand-packed uint48

V0_PAYLOAD_SIZE = 6
V0_FLAG_ENABLED = 0x7F
V0_FLAG_DISABLED = 0x70
_V0 = struct.Struct("<BBi")

_MAX_U48 = (1 << 48) - 1
_MAX_U16 = (1 << 16) - 1
_INT32_MIN, _INT32_MAX = -(1 << 31), (1 << 31) - 1


class DecodeError(ValueError):
    """Malformed, truncated, foreign, or unsupported-version packet.
    """


@dataclass(frozen=True, slots=True)
class StatePacket:
    """One agent's broadcast state. ``vstate`` is scaled by ``SCALE_FACTOR``."""

    node_id: int
    vstate: int
    seq: int = 0
    tx_time_us: int = 0
    enabled: bool = True
    disturbance_on: bool = False

    def __post_init__(self) -> None:
        if not 1 <= self.node_id <= 255:
            raise ValueError(f"node_id must be 1..255, got {self.node_id}")
        if not _INT32_MIN <= self.vstate <= _INT32_MAX:
            raise ValueError(
                f"vstate {self.vstate} overflows int32; at SCALE_FACTOR={SCALE_FACTOR} "
                f"the representable virtual state is +-{_INT32_MAX / SCALE_FACTOR:.4f}"
            )
        if not 0 <= self.seq <= _MAX_U16:
            raise ValueError(f"seq must fit uint16, got {self.seq}")
        if not 0 <= self.tx_time_us <= _MAX_U48:
            raise ValueError(f"tx_time_us must fit uint48, got {self.tx_time_us}")

    # scaling boundary: ---------------------------------------------------------
    @classmethod
    def from_state(cls, node_id: int, vstate: float, **kw) -> "StatePacket":
        """Build from a virtual state in engineering units, quantizing for the wire."""
        return cls(node_id=node_id, vstate=quantize(vstate), **kw)

    @property
    def vstate_float(self) -> float:
        """Virtual state in engineering units."""
        return dequantize(self.vstate)

    # v1 codec: -----------------------------------------------------------------
    def encode(self) -> bytes:
        flags = (_FLAG_ENABLED if self.enabled else 0) | (
            _FLAG_DISTURBANCE if self.disturbance_on else 0
        )
        out = _HEAD.pack(VERSION, flags, self.node_id, 0, self.seq, self.vstate)
        out += self.tx_time_us.to_bytes(6, "little")
        return out

    @classmethod
    def decode(cls, data: bytes) -> "StatePacket":
        if len(data) != PAYLOAD_SIZE:
            raise DecodeError(f"expected {PAYLOAD_SIZE} bytes, got {len(data)}")
        version, flags, node_id, reserved, seq, vstate = _HEAD.unpack_from(data, 0)
        if version != VERSION:
            raise DecodeError(f"unsupported version {version} (this build speaks v{VERSION})")
        if reserved != 0:
            raise DecodeError(f"reserved byte must be 0, got {reserved}")
        if node_id == 0:
            raise DecodeError("node_id 0 is reserved")
        return cls(
            node_id=node_id,
            vstate=vstate,
            seq=seq,
            tx_time_us=int.from_bytes(data[10:16], "little"),
            enabled=bool(flags & _FLAG_ENABLED),
            disturbance_on=bool(flags & _FLAG_DISTURBANCE),
        )


# BLE manufacturer-data framing:
def encode_manufacturer_data(pkt: StatePacket) -> bytes:
    """Company ID (LE) + v1 payload, i.e. the AD element *value* for HCI."""
    return COMPANY_ID.to_bytes(2, "little") + pkt.encode()


def decode_manufacturer_data(data: bytes, *, company_id: int = COMPANY_ID) -> StatePacket:
    """Inverse of :func:`encode_manufacturer_data`, with a strict company check."""
    if len(data) < 2:
        raise DecodeError("manufacturer data too short to hold a company ID")
    got = int.from_bytes(data[:2], "little")
    if got != company_id:
        raise DecodeError(f"foreign company ID 0x{got:04X} (want 0x{company_id:04X})")
    return StatePacket.decode(data[2:])


#  v0: the 6-byte format currently on the air (see module docstring)
def encode_v0(node_id: int, vstate: int, enabled: bool) -> bytes:
    """v0 payload: ``[netid_enabled | node | int32 LE vstate]``.
    """
    if not _INT32_MIN <= vstate <= _INT32_MAX:
        raise ValueError(f"vstate {vstate} overflows int32")
    flag = V0_FLAG_ENABLED if enabled else V0_FLAG_DISABLED
    return _V0.pack(flag, node_id, vstate)


def decode_v0(data: bytes) -> StatePacket:
    """Parse a v0 payload. ``seq`` and ``tx_time_us`` do not exist in v0 -> 0.
    """
    if len(data) < V0_PAYLOAD_SIZE:
        raise DecodeError(f"expected >={V0_PAYLOAD_SIZE} bytes, got {len(data)}")
    flag, node_id, vstate = _V0.unpack_from(data, 0)
    if flag not in (V0_FLAG_ENABLED, V0_FLAG_DISABLED):
        raise DecodeError(f"not a v0 packet: flag 0x{flag:02X}")
    if node_id == 0:
        raise DecodeError("node_id 0 is reserved")
    return StatePacket(
        node_id=node_id, vstate=vstate, seq=0, tx_time_us=0,
        enabled=(flag == V0_FLAG_ENABLED),
    )


def decode_any(data: bytes) -> StatePacket:
    """Accept v1 or v0, for a fleet mid-reflash.
    """
    if not data:
        raise DecodeError("empty payload")
    if data[0] == VERSION and len(data) == PAYLOAD_SIZE:
        return StatePacket.decode(data)
    if data[0] in (V0_FLAG_ENABLED, V0_FLAG_DISABLED):
        return decode_v0(data)
    raise DecodeError(f"unrecognised payload: first byte 0x{data[0]:02X}, length {len(data)}")


# per-link quality accounting:
@dataclass
class LinkStats:
    """Per-neighbour reception quality. ``expected`` is inferred from seq gaps."""

    received: int = 0
    expected: int = 0
    lost: int = 0
    duplicates: int = 0
    reordered: int = 0
    resets: int = 0
    delays_us: list[int] = field(default_factory=list)

    @property
    def delivery_ratio(self) -> float:
        """Fraction of expected packets that arrived; 1.0 before any gap is seen."""
        return 1.0 if self.expected == 0 else self.received / self.expected

    @property
    def min_delay_us(self) -> int | None:
        return min(self.delays_us) if self.delays_us else None

    @property
    def median_delay_us(self) -> float | None:
        if not self.delays_us:
            return None
        s = sorted(self.delays_us)
        mid, odd = divmod(len(s), 2)
        return float(s[mid]) if odd else (s[mid - 1] + s[mid]) / 2.0


class LinkMonitor:
    """Accumulates :class:`LinkStats` per neighbour from received packets.
    """

    def __init__(self, *, reorder_window: int = 1024) -> None:
        self.reorder_window = reorder_window
        self.stats: dict[int, LinkStats] = {}
        self._last_seq: dict[int, int] = {}

    def observe(self, pkt: StatePacket, rx_time_us: int | None = None) -> LinkStats:
        st = self.stats.setdefault(pkt.node_id, LinkStats())
        st.received += 1
        if rx_time_us is not None and pkt.tx_time_us:
            st.delays_us.append(rx_time_us - pkt.tx_time_us)

        last = self._last_seq.get(pkt.node_id)
        if last is None:
            st.expected += 1
        else:
            gap = (pkt.seq - last) % (_MAX_U16 + 1)
            if gap == 0:
                st.duplicates += 1
                st.received -= 1          # a duplicate is not a delivery
                return st
            if gap > (_MAX_U16 + 1) - self.reorder_window:
                st.reordered += 1         # arrived out of order; don't move `last`
                st.received -= 1
                return st
            if gap > self.reorder_window:
                st.resets += 1            # peer restarted; resync without a loss burst
                st.expected += 1
            else:
                st.expected += gap
                st.lost += gap - 1
        self._last_seq[pkt.node_id] = pkt.seq
        return st

    def report(self) -> dict[int, LinkStats]:
        return dict(self.stats)
