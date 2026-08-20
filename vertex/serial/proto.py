"""Framed binary protocol to the nRF test peer.

The Python half of ``firmware/nordic-testpeer/src/proto.[ch]``. The two are
cross-checked byte for byte by ``tests/test_peer_proto_conformance.py``, which
compiles the C and compares its output against this module -- because "two
implementations of one protocol" is a standing invitation to a disagreement that
only shows up over a UART at 3am.

Frame layout, little-endian throughout::

    +------+------+--------+--------------+--------+
    | SOF  | TYPE | LEN:2  | PAYLOAD[LEN] | CRC:2  |
    | 0x7E |      |        |              |        |
    +------+------+--------+--------------+--------+
                  \\______________________/
                    CRC-16/CCITT-FALSE over TYPE, LEN, PAYLOAD

A dedicated start-of-frame byte plus a CRC, rather than using the type byte as the
sync marker: payloads are binary and contain every byte value, so a corrupted
stream would otherwise resynchronise *inside* a payload and accept garbage as
configuration.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Iterator

__all__ = [
    "SOF", "MAX_PAYLOAD", "OVERHEAD", "MAX_FRAME", "FrameType", "Frame",
    "ProtoError", "crc16", "build_frame", "FrameParser", "ParserStats",
    "encode_network", "encode_algorithm", "encode_disturbance", "encode_control",
    "encode_adv_tx", "encode_radio", "encode_ping", "encode_stats_req",
    "AdvReport", "PeerStats", "decode_adv_report", "decode_pong",
    "decode_txat", "StateReport", "encode_state", "decode_state",
    "STATE_FLAG_ENABLED", "STATE_FLAG_FRESH",
    "decode_ack", "decode_stats", "MAX_NEIGHBORS", "MAX_AD_LEN",
]

SOF = 0x7E
MAX_PAYLOAD = 256
OVERHEAD = 6                 # SOF + TYPE + LEN(2) + CRC(2)
MAX_FRAME = MAX_PAYLOAD + OVERHEAD
MAX_NEIGHBORS = 16
MAX_AD_LEN = 31


class FrameType(IntEnum):
    """Must match the ``PROTO_T_*`` defines in ``proto.h``."""

    # Pi -> peer
    NETWORK = 0x4E          # 'N'
    ALGORITHM = 0x41        # 'A'  -- 'A', not 0x61
    DISTURBANCE = 0x44      # 'D'
    CONTROL = 0x53          # 'S'
    ADV_TX = 0x54           # 'T'
    RADIO = 0x52            # 'R'
    PING = 0x50             # 'P'
    STATS_REQ = 0x51        # 'Q'
    # peer -> Pi
    TXAT = 0x74             # 't' -- payload reached the controller
    ADV_REPORT = 0x72       # 'r'
    STATE = 0x78            # 'x'
    ACK = 0x6B              # 'k'
    ERR = 0x65              # 'e'
    PONG = 0x70             # 'p'
    STATS = 0x71            # 'q'


#: Payload length the peer accepts, per type. ``None`` means variable.
PAYLOAD_LEN: dict[int, int | None] = {
    FrameType.NETWORK: None,        # 2..2+MAX_NEIGHBORS
    FrameType.ALGORITHM: 36,
    FrameType.DISTURBANCE: 29,
    FrameType.CONTROL: 11,
    FrameType.RADIO: 9,
    FrameType.PING: 0,
    FrameType.STATS_REQ: 0,
    FrameType.ADV_TX: None,
}


class ProtoError(ValueError):
    """Malformed frame or out-of-range field."""


@dataclass(frozen=True, slots=True)
class Frame:
    type: int
    payload: bytes

    @property
    def name(self) -> str:
        try:
            return FrameType(self.type).name
        except ValueError:
            return f"0x{self.type:02X}"


def crc16(data: bytes) -> int:
    """CRC-16/CCITT-FALSE: poly 0x1021, init 0xFFFF, no reflection, no final xor.

    Check value for ``b"123456789"`` is 0x29B1.
    """
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def build_frame(frame_type: int, payload: bytes = b"") -> bytes:
    """Assemble one frame."""
    if len(payload) > MAX_PAYLOAD:
        raise ProtoError(f"payload of {len(payload)} exceeds {MAX_PAYLOAD}")
    head = bytes([frame_type]) + struct.pack("<H", len(payload))
    return bytes([SOF]) + head + payload + struct.pack("<H", crc16(head + payload))


# parser:
@dataclass
class ParserStats:
    frames_ok: int = 0
    crc_errors: int = 0
    len_errors: int = 0
    resyncs: int = 0
    timeouts: int = 0
    bytes_in: int = 0


class FrameParser:
    """Byte-stream frame parser. Mirrors ``struct proto_parser``.

    Stream-oriented rather than chunk-oriented, so it does not care how the
    serial layer fragments its reads -- which is exactly why the firmware can use
    a 1 ms DMA idle timeout without risk of splitting a frame.
    """

    __slots__ = ("_state", "_type", "_expected", "_buf", "_crc_rx", "stats")

    _WAIT_SOF, _WAIT_TYPE, _WAIT_LEN_LO, _WAIT_LEN_HI = 0, 1, 2, 3
    _WAIT_PAYLOAD, _WAIT_CRC_LO, _WAIT_CRC_HI = 4, 5, 6

    def __init__(self) -> None:
        self.stats = ParserStats()
        self.reset()

    def reset(self) -> None:
        self._state = self._WAIT_SOF
        self._type = 0
        self._expected = 0
        self._buf = bytearray()
        self._crc_rx = 0

    @property
    def in_frame(self) -> bool:
        """True while a frame is partially received."""
        return self._state != self._WAIT_SOF

    def timeout(self) -> None:
        """Abandon a partial frame after the link has been idle.

        Needed for the same reason as on the firmware: a mid-payload ``0x7E`` is
        deliberately not treated as a frame start, so a truncated frame would
        otherwise wait forever *and consume the following frames as payload*.
        """
        if self.in_frame:
            self.stats.timeouts += 1
            self.reset()

    def feed(self, data: bytes) -> Iterator[Frame]:
        """Consume bytes, yielding each complete, CRC-valid frame."""
        for byte in data:
            self.stats.bytes_in += 1
            frame = self._step(byte)
            if frame is not None:
                yield frame

    def _step(self, byte: int) -> Frame | None:
        st = self._state

        if st == self._WAIT_SOF:
            if byte == SOF:
                self._state = self._WAIT_TYPE
            # else: inter-frame noise, dropped without counting -- the port may
            # legitimately be opened mid-stream.

        elif st == self._WAIT_TYPE:
            if byte in PAYLOAD_LEN or byte in tuple(FrameType):
                self._type = byte
                self._state = self._WAIT_LEN_LO
            else:
                self.stats.resyncs += 1
                self._state = self._WAIT_SOF

        elif st == self._WAIT_LEN_LO:
            self._expected = byte
            self._state = self._WAIT_LEN_HI

        elif st == self._WAIT_LEN_HI:
            self._expected |= byte << 8
            if self._expected > MAX_PAYLOAD:
                self.stats.len_errors += 1
                self.stats.resyncs += 1
                self._state = self._WAIT_SOF
            else:
                self._buf = bytearray()
                self._state = (self._WAIT_CRC_LO if self._expected == 0
                               else self._WAIT_PAYLOAD)

        elif st == self._WAIT_PAYLOAD:
            self._buf.append(byte)
            if len(self._buf) >= self._expected:
                self._state = self._WAIT_CRC_LO

        elif st == self._WAIT_CRC_LO:
            self._crc_rx = byte
            self._state = self._WAIT_CRC_HI

        elif st == self._WAIT_CRC_HI:
            self._crc_rx |= byte << 8
            head = bytes([self._type]) + struct.pack("<H", self._expected)
            self._state = self._WAIT_SOF
            if crc16(head + bytes(self._buf)) == self._crc_rx:
                self.stats.frames_ok += 1
                return Frame(self._type, bytes(self._buf))
            self.stats.crc_errors += 1
            self.stats.resyncs += 1

        return None


# outbound payloads:
def encode_network(*, enabled: bool, node_id: int, neighbors: list[int]) -> bytes:
    if not 1 <= node_id <= 255:
        raise ProtoError(f"node_id must be 1..255, got {node_id}")
    if len(neighbors) > MAX_NEIGHBORS:
        raise ProtoError(f"{len(neighbors)} neighbours exceeds {MAX_NEIGHBORS}")
    return bytes([1 if enabled else 0, node_id, *neighbors])


def encode_algorithm(*, dt_ms: int, clock_ms: int, state0: int, vstate0: int,
                     vartheta0: int, counter0: int, alpha: int, delta: int,
                     eta: int) -> bytes:
    """Nine int32s. Order matches ``apply_algorithm()`` in agent.c exactly."""
    if dt_ms <= 0 or clock_ms <= 0:
        raise ProtoError("dt_ms and clock_ms must be > 0; the peer rejects zero")
    return struct.pack("<9i", dt_ms, clock_ms, state0, vstate0, vartheta0,
                       counter0, alpha, delta, eta)


def encode_disturbance(*, active: bool, sine_amplitude: int, frequency: int,
                       phase: int, noise_amplitude: int, noise_offset: int,
                       beta: int, samples: int) -> bytes:
    if samples <= 0:
        raise ProtoError("samples must be > 0; the counter wraps modulo it")
    return bytes([1 if active else 0]) + struct.pack(
        "<7i", sine_amplitude, frequency, phase, noise_amplitude, noise_offset,
        beta, samples)


def encode_control(*, trigger: bool, seed: int = 0, epoch_us: int = 0) -> bytes:
    """Start/stop the run, carrying the node's PRNG seed and the clock offset.

    The frame's transit is a systematic bias on every timestamp the node emits
    afterwards: the stamp is taken here and latched on arrival. Bounded by a PING
    round trip, ~1.5 ms for this payload at 115200 baud.
    """
    if not 0 <= seed <= 0xFFFFFFFF:
        raise ProtoError(f"seed must fit in uint32, got {seed}")
    if not 0 <= epoch_us <= (1 << 48) - 1:
        raise ProtoError(f"epoch_us must fit in uint48, got {epoch_us}")
    return (bytes([1 if trigger else 0])
            + struct.pack("<I", seed)
            + epoch_us.to_bytes(6, "little"))


def encode_adv_tx(ad: bytes) -> bytes:
    """Complete AD data, advertised verbatim by the peer."""
    if len(ad) > MAX_AD_LEN:
        raise ProtoError(f"AD of {len(ad)} bytes exceeds the {MAX_AD_LEN}-byte limit")
    return bytes(ad)


def encode_radio(*, adv_min: int, adv_max: int, scan_interval: int,
                 scan_window: int, active_scan: bool = False,
                 advertising: bool = True) -> bytes:
    """Radio parameters, in 0.625 ms units.

    ``scan_window <= scan_interval`` is enforced here as well as on the peer:
    the peer's rejection arrives as an ``ERR`` frame several milliseconds later,
    by which time the caller has moved on.
    """
    if scan_window > scan_interval:
        raise ProtoError(
            f"scan_window ({scan_window}) must be <= scan_interval ({scan_interval})")
    if scan_interval == 0:
        raise ProtoError("scan_interval must be non-zero")
    flags = (0x01 if active_scan else 0) | (0x02 if advertising else 0)
    return struct.pack("<HHHHB", adv_min, adv_max, scan_interval, scan_window, flags)


def encode_ping() -> bytes:
    return b""


def encode_stats_req() -> bytes:
    return b""


# inbound payloads:
@dataclass(frozen=True, slots=True)
class AdvReport:
    """One advertising report, exactly as the peer captured it."""

    timestamp_us: int
    rssi: int
    addr_type: int
    addr: bytes
    adv_type: int
    data: bytes

    @property
    def addr_str(self) -> str:
        """Colon-separated, most significant first -- the wire order is reversed."""
        return ":".join(f"{b:02X}" for b in reversed(self.addr))


def decode_adv_report(payload: bytes) -> AdvReport:
    """``[timestamp_us:8][rssi:1][addr_type:1][addr:6][adv_type:1][len:1][data]``"""
    if len(payload) < 18:
        raise ProtoError(f"advertising report truncated: {len(payload)} bytes")
    ts, rssi, addr_type = struct.unpack_from("<QbB", payload, 0)
    addr = payload[10:16]
    adv_type, length = payload[16], payload[17]
    data = payload[18:18 + length]
    if len(data) != length:
        raise ProtoError(f"report declares {length} AD bytes, carries {len(data)}")
    return AdvReport(ts, rssi, addr_type, addr, adv_type, data)


#: STATE payload, little-endian. Every scaled field is int32 in units of 1e-6 --
#: uniformly, including the disturbance frequency. A struct with some fields
#: scaled and some not is the class of bug this platform keeps paying for.
#:
#:   [t_us:8][state:4][vstate:4][vartheta:4][counter:4][n:1]
#:   then per neighbour: [vstate:4][flags:1]   flags bit0=enabled bit1=fresh
STATE_HEADER = struct.Struct("<QiiiiB")
STATE_NEIGHBOUR = struct.Struct("<iB")

STATE_FLAG_ENABLED = 0x01
STATE_FLAG_FRESH = 0x02


@dataclass(frozen=True, slots=True)
class StateReport:
    """One control step, as the microcontroller computed it.
    """

    t_us: int
    state: int
    vstate: int
    vartheta: int
    counter: int
    neighbor_vstates: tuple[int, ...]
    neighbor_enabled: tuple[bool, ...]
    neighbor_fresh: tuple[bool, ...]

    @property
    def t_s(self) -> float:
        return self.t_us / 1e6


def encode_state(r: StateReport) -> bytes:
    """Build a STATE payload. Mirrors the firmware; used to exercise the decoder."""
    n = len(r.neighbor_vstates)
    out = STATE_HEADER.pack(r.t_us, r.state, r.vstate, r.vartheta, r.counter, n)
    for i in range(n):
        flags = ((STATE_FLAG_ENABLED if r.neighbor_enabled[i] else 0)
                 | (STATE_FLAG_FRESH if r.neighbor_fresh[i] else 0))
        out += STATE_NEIGHBOUR.pack(r.neighbor_vstates[i], flags)
    return out


def decode_state(payload: bytes) -> StateReport:
    """Parse a STATE payload."""
    if len(payload) < STATE_HEADER.size:
        raise ProtoError(f"STATE payload of {len(payload)} bytes is truncated")
    t_us, state, vstate, vartheta, counter, n = STATE_HEADER.unpack_from(payload, 0)

    want = STATE_HEADER.size + n * STATE_NEIGHBOUR.size
    if len(payload) != want:
        raise ProtoError(
            f"STATE declares {n} neighbour(s) so should be {want} bytes, "
            f"got {len(payload)}")
    if n > MAX_NEIGHBORS:
        raise ProtoError(f"STATE declares {n} neighbours, limit is {MAX_NEIGHBORS}")

    vstates, enabled, fresh = [], [], []
    for i in range(n):
        v, flags = STATE_NEIGHBOUR.unpack_from(payload, STATE_HEADER.size
                                               + i * STATE_NEIGHBOUR.size)
        vstates.append(v)
        enabled.append(bool(flags & STATE_FLAG_ENABLED))
        fresh.append(bool(flags & STATE_FLAG_FRESH))

    return StateReport(t_us, state, vstate, vartheta, counter,
                       tuple(vstates), tuple(enabled), tuple(fresh))


def decode_txat(payload: bytes) -> tuple[int, int]:
    """``(seq, uptime_us)`` -- when a commanded payload reached the controller."""
    if len(payload) != 10:
        raise ProtoError(f"TXAT payload must be 10 bytes, got {len(payload)}")
    return struct.unpack("<HQ", payload)


def decode_pong(payload: bytes) -> int:
    if len(payload) != 8:
        raise ProtoError(f"PONG payload must be 8 bytes, got {len(payload)}")
    return struct.unpack("<Q", payload)[0]


def decode_ack(payload: bytes) -> tuple[int, int]:
    """``(frame_type, status)``. Status is signed; negative is a rejection."""
    if len(payload) != 2:
        raise ProtoError(f"ACK/ERR payload must be 2 bytes, got {len(payload)}")
    return payload[0], struct.unpack("<b", payload[1:2])[0]


#: Counter order must match the ``PROTO_T_STATS_REQ`` handler in main.c.
STATS_FIELDS = (
    "reports", "queue_dropped", "oversize",
    "tx_frames", "tx_dropped", "rx_overrun_bytes", "rx_stopped",
    "rx_partial_flushes", "rx_full_flushes",
    "frames_ok", "crc_errors", "timeouts",
)


@dataclass(frozen=True)
class PeerStats:
    """The peer's counters. Read before and after a run and subtract.
    """

    values: dict[str, int] = field(default_factory=dict)

    def __getattr__(self, name: str) -> int:
        try:
            return self.values[name]
        except KeyError:
            raise AttributeError(name) from None

    def delta(self, earlier: "PeerStats") -> "PeerStats":
        return PeerStats({k: self.values[k] - earlier.values.get(k, 0)
                          for k in self.values})

    @property
    def lost_internally(self) -> int:
        """Reports the peer dropped itself -- never radio loss."""
        return self.values.get("queue_dropped", 0) + self.values.get("tx_dropped", 0)


def decode_stats(payload: bytes) -> PeerStats:
    n = len(STATS_FIELDS)
    if len(payload) != 4 * n:
        raise ProtoError(f"stats payload must be {4 * n} bytes, got {len(payload)}")
    return PeerStats(dict(zip(STATS_FIELDS, struct.unpack(f"<{n}I", payload))))
