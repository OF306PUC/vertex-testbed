#!/usr/bin/env python3
"""Cross-validate the firmware's on-air codec against the host's.

The nRF and the Pi now both speak v1, so the same bytes have to mean the same
thing on both sides. Nothing in either build checks that: the firmware serialises
field by field in C, the host with `struct.pack`, and a disagreement shows up as a
neighbour whose vstate reads plausibly and is wrong.

`test/crossval/air_wire_harness.c` links the real `air_wire.c` unmodified, so what is
compared is the code that gets flashed.

Four directions, each of which has to hold:

  1. firmware encode -> host decode
  2. host encode -> firmware decode
  3. host v0 encode -> firmware decode  (a half-reflashed bench must degrade to
     missing timestamps, not to a blackout)
  4. the firmware rejects what the host rejects: foreign company, nonzero
     reserved byte, node id 0, and lengths belonging to no version

    python3 test/crossval/check_air_wire.py
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from vertex.wire import DecodeError, StatePacket, decode_any, encode_v0
from vertex.wire.codec import (COMPANY_ID, PAYLOAD_SIZE, V0_FLAG_DISABLED,
                               V0_FLAG_ENABLED, decode_manufacturer_data,
                               encode_manufacturer_data)

BIN = Path("/tmp") / "vertex-air-wire-harness"

AIR_WIRE_OK, AIR_WIRE_ERR_LEN, AIR_WIRE_ERR_COMPANY, AIR_WIRE_ERR_FORMAT = 0, -1, -2, -3

# node, enabled, disturbance_on, seq, vstate, tx_time_us
CASES = [
    (1,   True,  False, 0,      22_300_000,              0),
    (2,   True,  True,  1,     -12_500_000,        123_456),
    (255, False, False, 65535,  2147483647, (1 << 48) - 1),   # every field at max
    (7,   False, True,  40000, -2147483648,  1_700_000_000),
]


def build() -> bool:
    proc = subprocess.run(
        ["gcc", "-O2", "-std=c99", "-Wall", "-Wextra",
         "-I", str(ROOT / "firmware/nordic/src"),
         str(ROOT / "test/crossval/air_wire_harness.c"),
         str(ROOT / "firmware/nordic/src/air_wire.c"), "-o", str(BIN)],
        capture_output=True, text=True)
    if proc.returncode != 0:
        for line in proc.stderr.strip().splitlines():
            if "error" in line:
                print(f"  FAIL {line.strip()}")
        return False
    return True


def fw_encode(node, enabled, dist, seq, vstate, tx) -> bytes:
    out = subprocess.run(
        [str(BIN), "enc", str(node), str(int(enabled)), str(int(dist)),
         str(seq), str(vstate), str(tx)],
        check=True, capture_output=True, text=True).stdout.strip()
    return bytes.fromhex(out)


def fw_decode(raw: bytes):
    """(rc, fields) -- fields is None when rc is negative."""
    out = subprocess.run([str(BIN), "dec", raw.hex()],
                         check=True, capture_output=True, text=True).stdout.strip()
    parts = out.split(",")
    if len(parts) == 1:
        return int(parts[0]), None
    rc, node, en, dist, seq, vstate, tx, has = (int(x) for x in parts)
    return rc, (node, bool(en), bool(dist), seq, vstate, tx, bool(has))


def main() -> int:
    if not build():
        print("  -> air_wire.c does not compile; codec unverifiable")
        return 1
    fails = []

    # 1. firmware encode -> host decode
    for node, en, dist, seq, vstate, tx in CASES:
        raw = fw_encode(node, en, dist, seq, vstate, tx)
        if len(raw) != 2 + PAYLOAD_SIZE:
            fails.append(f"firmware emitted {len(raw)} bytes, expected {2 + PAYLOAD_SIZE}")
            continue
        try:
            pkt = decode_manufacturer_data(raw)
        except DecodeError as exc:
            fails.append(f"host refused the firmware's v1 bytes {raw.hex()}: {exc}")
            continue
        got = (pkt.node_id, pkt.enabled, pkt.disturbance_on, pkt.seq,
               pkt.vstate, pkt.tx_time_us)
        want = (node, en, dist, seq, vstate, tx)
        ok = got == want
        print(f"  {'ok  ' if ok else 'FAIL'} fw->host  {raw.hex()}")
        if not ok:
            fails.append(f"fw->host decoded {got}, expected {want}")

    # 2. host encode -> firmware decode. Byte-identical encoders would make this
    #    redundant, so assert that too: it is the strongest form of "agree".
    for node, en, dist, seq, vstate, tx in CASES:
        host_raw = encode_manufacturer_data(StatePacket(
            node_id=node, vstate=vstate, seq=seq, tx_time_us=tx,
            enabled=en, disturbance_on=dist))
        fw_raw = fw_encode(node, en, dist, seq, vstate, tx)
        if host_raw != fw_raw:
            fails.append(f"encoders differ for node {node}: host {host_raw.hex()} "
                         f"vs firmware {fw_raw.hex()}")
        rc, fields = fw_decode(host_raw)
        want = (node, en, dist, seq, vstate, tx, True)
        ok = rc == AIR_WIRE_OK and fields == want
        print(f"  {'ok  ' if ok else 'FAIL'} host->fw  {host_raw.hex()}")
        if not ok:
            fails.append(f"host->fw rc={rc} fields={fields}, expected {want}")

    # 3. v0 on receive
    for netid, node, vstate in ((V0_FLAG_ENABLED, 3, 21_000_000),
                                (V0_FLAG_DISABLED, 4, -500_000)):
        raw = COMPANY_ID.to_bytes(2, "little") + encode_v0(
            node_id=node, vstate=vstate, enabled=netid == V0_FLAG_ENABLED)
        rc, fields = fw_decode(raw)
        want = (node, netid == V0_FLAG_ENABLED, False, 0, vstate, 0, False)
        ok = rc == AIR_WIRE_OK and fields == want
        print(f"  {'ok  ' if ok else 'FAIL'} v0->fw    {raw.hex()}  "
              f"has_seq_and_time={fields[6] if fields else '-'}")
        if not ok:
            fails.append(f"v0->fw rc={rc} fields={fields}, expected {want}")

    # 4. the firmware refuses what the host refuses
    good = fw_encode(1, True, False, 5, 1_000_000, 42)
    rejects = [
        ("foreign company", b"\x11\x22" + good[2:],              AIR_WIRE_ERR_COMPANY),
        ("nonzero reserved", good[:5] + b"\x01" + good[6:],      AIR_WIRE_ERR_FORMAT),
        ("node id 0",       good[:4] + b"\x00" + good[5:],       AIR_WIRE_ERR_FORMAT),
        ("truncated",       good[:-1],                           AIR_WIRE_ERR_LEN),
        ("overlong",        good + b"\x00",                      AIR_WIRE_ERR_LEN),
        ("company only",    good[:2],                            AIR_WIRE_ERR_LEN),
    ]
    for name, raw, want_rc in rejects:
        rc, fields = fw_decode(raw)
        ok = rc == want_rc and fields is None
        print(f"  {'ok  ' if ok else 'FAIL'} reject {name:<16} rc={rc}")
        if not ok:
            fails.append(f"{name}: rc={rc}, expected {want_rc}")
        # And the host must agree it is not a valid v1 packet.
        if name in ("nonzero reserved", "node id 0"):
            try:
                decode_manufacturer_data(raw)
                fails.append(f"host ACCEPTED {name}, firmware rejected it")
            except DecodeError:
                pass

    for f in fails:
        print(f"  FAIL {f}")
    print(f"  -> {'firmware and host agree on v1' if not fails else f'{len(fails)} mismatch(es)'}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
