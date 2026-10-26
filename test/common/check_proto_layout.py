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

from vertex.serial import (FrameType, encode_algorithm, encode_control,
                           encode_disturbance, encode_network, encode_radio)
from vertex.serial.proto import (STATE_NEIGHBOUR, STATE_TAIL,
                                 STATE_TAIL_LC, build_frame)
from vertex.serial.proto import (LAW_MICROGRID_ADAPTIVE, MG_IFACE_FIELDS,
                                 MG_PLANT_FIELDS, MG_VIRTUAL_FIELDS,
                                 encode_microgrid_iface,
                                 encode_microgrid_plant,
                                 encode_microgrid_virtual)

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
                         vartheta0=3, counter0=4, gain_ij=5, delta=6, eta=7,
                         alpha=8),
        [0, 4, 8, 12, 16, 20, 24, 28, 32, 36],  # 10 x int32
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
    # One value per field, distinct and positive, so kappa > 0 and b != 0
    # satisfy the encoders' own guards without special-casing.
    "MG_PLANT": (
        "PROTO_MG_PLANT_LEN",
        encode_microgrid_plant(profile=1,
                               **{f: i + 1 for i, f in enumerate(MG_PLANT_FIELDS)}),
        [1 + 4 * i for i in range(len(MG_PLANT_FIELDS))],       # u8, then int32
    ),
    "MG_VIRTUAL": (
        "PROTO_MG_VIRTUAL_LEN",
        encode_microgrid_virtual(
            **{f: i + 1 for i, f in enumerate(MG_VIRTUAL_FIELDS)}),
        [4 * i for i in range(len(MG_VIRTUAL_FIELDS))],
    ),
    "MG_IFACE": (
        "PROTO_MG_IFACE_LEN",
        encode_microgrid_iface(law=LAW_MICROGRID_ADAPTIVE, terminal=True,
                               **{f: i + 1 for i, f in enumerate(MG_IFACE_FIELDS)}),
        [2 + 4 * i for i in range(len(MG_IFACE_FIELDS))],       # 2 x u8 first
    ),
}

#: Frames a firmware may legitimately not implement. Only the coordination
#: firmware runs the microgrid laws; the two loopback benches speak the same
#: protocol for everything else and have no business carrying this one.
#:
#: Absent is fine, half-present is not: a macro without a decoder, or a decoder
#: without a macro, is the state this check exists to catch. Anything not
#: listed here is mandatory and its absence is still a failure.
OPTIONAL = {"MG_PLANT", "MG_VIRTUAL", "MG_IFACE"}


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
    "MG_PLANT": "apply_mg_plant",
    "MG_VIRTUAL": "apply_mg_virtual",
    "MG_IFACE": "apply_mg_iface",
}


def has_decoder(src: str, fn: str) -> bool:
    return re.search(rf"^(?:static )?int {fn}\(", src, re.M) is not None


#: The largest frame the host-to-board link will carry, in bytes on the wire.
#:
#: MEASURED, not specified: bisected on an nRF52840-DK over its J-Link VCOM,
#: a 64-byte frame is answered and a 65-byte one is not, 3/3 repeatable. It
#: is the USB CDC bulk packet size, and the loss is silent -- no counter on
#: the board moves, so the host sees a timeout and not a rejection.
#:
#: Directional. STATS is 93 bytes board-to-host and arrives intact.
WIRE_LIMIT = 64


def check_sizes() -> int:
    """No host encoder may build a frame the link cannot carry.

    This is the check that was missing. The microgrid block shipped as one
    121-byte frame -- nearly triple ALGORITHM, the largest that had ever
    existed -- and every offline check passed, because none of them puts a
    byte on a wire. It cost four hardware runs to find, and the first three
    blamed the control law.
    """
    bad = 0
    for name, (_macro, payload, _offsets) in CASES.items():
        n = len(build_frame(FrameType[name], payload))
        if n <= WIRE_LIMIT:
            print(f"  ok   size         {name:<11} {n:>3} B on the wire")
        else:
            print(f"  FAIL size         {name}: {n} B on the wire exceeds the "
                  f"measured {WIRE_LIMIT} B limit; it will be lost silently, "
                  f"not rejected")
            bad += 1
    return bad


def check_state_layout(src_dir: Path) -> int:
    """The board-to-host STATE frame, whose widths live in report.h.

    Not in CASES: STATE goes the other way, so it has no decoder in agent.c
    and no host encoder to compare lengths against -- but the same drift is
    possible, and the microgrid tail is a fresh chance for it.
    """
    report_h = src_dir / "report.h"
    if not report_h.exists():
        print("  --   state        no report.h in this firmware")
        return 0
    text = report_h.read_text()
    want = {"STATE_NEIGHBOUR_BYTES": STATE_NEIGHBOUR.size,
            "STATE_TAIL_BYTES": STATE_TAIL.size,
            "STATE_TAIL_LC_BYTES": STATE_TAIL_LC.size}
    bad = 0
    for macro, size in want.items():
        m = re.search(rf"#define\s+{macro}\s+(\d+)u?", text)
        if m is None:
            print(f"  FAIL state        {macro} not defined in report.h")
            bad += 1
        elif int(m.group(1)) != size:
            print(f"  FAIL state        {macro}={m.group(1)} but the host "
                  f"struct packs {size} bytes")
            bad += 1
        else:
            print(f"  ok   state        {macro}={size}")
    return bad


def dispatched(control_c: str) -> set[str]:
    """Frame types `control_on_frame` routes, by PROTO_T_ name."""
    m = re.search(r"^void control_on_frame\(.*?^\}", control_c, re.M | re.S)
    return set(re.findall(r"case (PROTO_T_\w+):", m.group(0) if m else ""))


def parseable(proto_c: str) -> set[str]:
    """Frame types `max_len_for` admits. The parser drops anything else at the
    type byte, before the dispatcher is ever reached."""
    m = re.search(r"max_len_for\(uint8_t type\).*?^\}", proto_c, re.M | re.S)
    return set(re.findall(r"case (PROTO_T_\w+):", m.group(0) if m else ""))


def check_dispatch(src_dir: Path, agent_c: str) -> int:
    """Every frame agent.c can decode must be routed to it by control.c.

    Three files have to agree for a frame to work: proto.h names the type,
    agent.c decodes the fields, control.c decides what reaches agent.c. The
    first two were checked above and the third was not, so MICROGRID shipped
    with a type, a decoder, a length macro and a 115-byte encoder on the host
    -- and a board that answered ERR, because the switch never mentioned it.

    The crossval harness cannot catch this: it calls `agent_apply_frame`
    directly, which is exactly the call control.c was failing to make.

    FOUR files, not three. proto.c's `max_len_for` is a second, independent
    list of admissible types, and the parser drops anything missing from it at
    the *type byte* -- before control.c is reached, and without an ERR, so the
    host sees a timeout rather than a rejection. MICROGRID was missing from
    both lists at once, and the parser is the one that ran first.
    """
    control = src_dir / "control.c"
    if not control.exists():
        print("  --   dispatch     no control.c in this firmware")
        return 0
    routed = dispatched(control.read_text())
    known = parseable((src_dir / "proto.c").read_text())
    bad = 0
    for name, fn in sorted(DECODER.items()):
        if not has_decoder(agent_c, fn):
            continue                    # the firmware does not speak it
        token = f"PROTO_T_{name}"
        missing = [w for w, s in (("proto.c max_len_for()", known),
                                  ("control.c control_on_frame()", routed))
                   if token not in s]
        if not missing:
            print(f"  ok   dispatch     {name:<11} parser + dispatcher -> {fn}()")
        else:
            print(f"  FAIL dispatch     {name}: agent.c has {fn}() but "
                  f"{' and '.join(missing)} never mentions {token}")
            bad += 1
    return bad


def main(fw: str) -> int:
    src_dir = Path(fw) / "src"
    lens = macros(src_dir / "proto.h")
    agent_c = (src_dir / "agent.c").read_text()

    bad = 0
    for name, (macro, payload, offsets) in CASES.items():
        declared, decodes = macro in lens, has_decoder(agent_c, DECODER[name])
        if name in OPTIONAL and not declared and not decodes:
            print(f"  --   {name:<12} not implemented by this firmware")
            continue
        if declared != decodes:
            print(f"  FAIL {name}: half implemented -- {macro} is "
                  f"{'defined' if declared else 'missing'} but "
                  f"{DECODER[name]}() is "
                  f"{'present' if decodes else 'absent'}")
            bad += 1
            continue
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

    bad += check_dispatch(src_dir, agent_c)
    bad += check_sizes()
    bad += check_state_layout(src_dir)

    print(f"  -> {'layout matches' if not bad else f'{bad} mismatch(es)'}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "firmware/nordic"))
