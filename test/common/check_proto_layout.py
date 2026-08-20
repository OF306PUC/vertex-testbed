#!/usr/bin/env python3
"""Verify the C decoder's field offsets match the Python encoder's byte layout.

The host encodes, the firmware decodes, and nothing in either build checks that
they agree. Both offsets and lengths have already drifted once: PROTO_CONTROL_LEN
went from 1 to 5 when the PRNG seed was added, and a one-sided edit there is a
frame the nRF rejects with -EINVAL on the bench, minutes after flashing.

Checks two things per frame type:

  1. PROTO_<TYPE>_LEN in proto.h equals len(encode_<type>(...)).
  2. Every `proto_ld_*(&d[N])` offset in agent.c lands on a field boundary of the
     Python struct, and every boundary is read exactly once.

Point 2 is what catches a field inserted in the middle: the lengths still match
because a field was added on both sides, but every offset after it is stale.

    python3 test/common/check_proto_layout.py firmware/nordic
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from vertex.serial import (encode_algorithm, encode_control, encode_disturbance,
                           encode_network, encode_radio)

# frame -> (macro, encoded payload, expected read offsets in the C decoder)
#
# Offsets are written out rather than derived: the point is to state the layout
# independently of both implementations, so a matching mistake on both sides
# still fails here.
CASES = {
    "NETWORK": (
        "PROTO_NETWORK_MIN_LEN",
        encode_network(enabled=True, node_id=1, neighbors=[]),
        [],                                     # indexed in a loop, not literals
    ),
    "ALGORITHM": (
        "PROTO_ALGORITHM_LEN",
        encode_algorithm(dt_ms=200, clock_ms=1000, state0=1, vstate0=2,
                         vartheta0=3, counter0=4, alpha=5, delta=6, eta=7),
        [0, 4, 8, 12, 16, 20, 24, 28, 32],      # 9 x int32
    ),
    "DISTURBANCE": (
        "PROTO_DISTURBANCE_LEN",
        encode_disturbance(active=True, sine_amplitude=1, frequency=2, phase=3,
                           noise_amplitude=4, noise_offset=5, beta=6, samples=7),
        [1, 5, 9, 13, 17, 21, 25],              # uint8 flag, then 7 x int32
    ),
    "CONTROL": (
        "PROTO_CONTROL_LEN",
        encode_control(trigger=True, seed=0xDEADBEEF),
        [1],                                    # uint8 flag, then uint32 seed
    ),
    "RADIO": (
        "PROTO_RADIO_LEN",
        encode_radio(adv_min=1, adv_max=2, scan_interval=3, scan_window=2),
        [0, 2, 4, 6],                           # 4 x u16, then a flags byte
    ),
}


def macros(header: Path) -> dict[str, int]:
    text = header.read_text()
    out = {}
    for name, val in re.findall(r"#define\s+(PROTO_\w*LEN)\s+(\d+)u?", text):
        out[name] = int(val)
    return out


def read_offsets(src: str, fn: str) -> list[int]:
    """Literal `&d[N]` / `&payload[N]` offsets inside one function."""
    m = re.search(rf"^(?:static )?int {fn}\(.*?^\}}", src, re.M | re.S)
    body = m.group(0) if m else ""
    return sorted(int(n) for n in
                  re.findall(r"proto_ld_\w+\(&(?:d|payload)\[(\d+)\]\)", body))


#: Decoder function per frame type. All in agent.c: control.c dispatches and
#: reports, it does not read fields.
DECODER = {
    "NETWORK": "apply_network",
    "ALGORITHM": "apply_algorithm",
    "DISTURBANCE": "apply_disturbance",
    "CONTROL": "apply_control",
    "RADIO": "agent_parse_radio",
}


def main(fw: str) -> int:
    src_dir = Path(fw) / "src"
    lens = macros(src_dir / "proto.h")
    agent_c = (src_dir / "agent.c").read_text()

    bad = 0
    for name, (macro, payload, offsets) in CASES.items():
        if macro not in lens:
            print(f"  FAIL {name}: {macro} not defined in proto.h")
            bad += 1
            continue
        if lens[macro] != len(payload):
            print(f"  FAIL {name}: {macro}={lens[macro]} but the host encodes "
                  f"{len(payload)} bytes")
            bad += 1
        else:
            print(f"  ok   {name:<12} {macro}={lens[macro]}")

        got = read_offsets(agent_c, DECODER[name])
        if offsets and got != offsets:
            print(f"  FAIL {name}: C reads offsets {got}, layout says {offsets}")
            bad += 1

    print(f"  -> {'layout matches' if not bad else f'{bad} mismatch(es)'}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "firmware/nordic"))
