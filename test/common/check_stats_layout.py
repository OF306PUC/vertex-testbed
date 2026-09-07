#!/usr/bin/env python3
"""Do the firmware's STATS writer and the host's STATS reader agree?

The STATS frame runs board -> host, which is the opposite direction from every
other payload, so `check_proto_layout.py` cannot see it: that script compares the
host's ENCODERS against the firmware's decoders. Here the firmware encodes and
the host decodes, and nothing checked the pair until this file.

It is worth checking for the same reason the other direction was: the two sides
are a field order and a length written twice, in two languages, and the length
has already drifted once on the other direction (PROTO_CONTROL_LEN, 1 -> 5).

Checks three things:

  1. PROTO_STATS_V1_LEN in proto.h equals STATS_V1_LEN on the host.
  2. The count of stores in control.c's handler matches the host's field count,
     by width: u32 stores against STATS_V1_COUNTERS, u16 against STATS_V1_RADIO.
  3. The field ORDER matches. The handler names its source fields in comments and
     expressions -- `o->foreign`, `u->tx_frames` -- so the sequence of member
     names in the handler is compared against the host's tuples directly.

    python3 test/common/check_stats_layout.py [firmware/nordic]
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from vertex.serial.proto import (STATS_V1, STATS_V1_COUNTERS, STATS_V1_LEN,
                                 STATS_V1_RADIO)


def macro(text: str, name: str) -> int | None:
    """Evaluate a #define whose body is a parenthesised arithmetic expression."""
    m = re.search(rf"#define\s+{name}\s+(.+)", text)
    if not m:
        return None
    body = m.group(1).split("/*")[0].strip()
    body = re.sub(r"\b(\d+)u\b", r"\1", body)          # 8u -> 8
    try:
        return int(eval(body, {"__builtins__": {}}, {}))   # arithmetic only
    except Exception:
        return None


def main(fw: str) -> int:
    src = Path(fw) / "src"
    proto_h = (src / "proto.h").read_text()
    control_c = (src / "control.c").read_text()
    fails: list[str] = []

    # 1. the declared length
    fw_len = macro(proto_h, "PROTO_STATS_V1_LEN")
    fw_ver = macro(proto_h, "PROTO_STATS_V1")
    if fw_len != STATS_V1_LEN:
        fails.append(f"PROTO_STATS_V1_LEN={fw_len} but host STATS_V1_LEN={STATS_V1_LEN}")
    else:
        print(f"  ok   length       {fw_len} bytes on both sides")
    if fw_ver != STATS_V1:
        fails.append(f"PROTO_STATS_V1={fw_ver} but host STATS_V1={STATS_V1}")

    # 2 and 3. the handler's stores, in order
    body = control_c[control_c.index("case PROTO_T_STATS_REQ"):]
    body = body[:body.index("uart_link_send(PROTO_T_STATS")]
    u32 = re.findall(r"proto_st_u32\(&p\[n\],\s*\w+->(\w+)\)", body)
    u16 = re.findall(r"proto_st_u16\(&p\[n\],\s*\w+->(\w+)\)", body)

    for label, got, want in (("counters", u32, list(STATS_V1_COUNTERS)),
                             ("radio", u16, list(STATS_V1_RADIO))):
        if got == want:
            print(f"  ok   {label:<12} {len(got)} fields, same order")
            continue
        fails.append(f"{label}: firmware writes {got}")
        fails.append(f"{label}: host reads     {want}")
        extra, missing = set(got) - set(want), set(want) - set(got)
        if extra:
            fails.append(f"{label}: only in the firmware: {sorted(extra)}")
        if missing:
            fails.append(f"{label}: only on the host:     {sorted(missing)}")

    # the two trailing bytes are written as raw indices, not through a helper
    for name in ("granted_tx_power", "scan_active"):
        if name not in body:
            fails.append(f"the handler never writes {name}")

    for f in fails:
        print(f"  FAIL {f}")
    print(f"  -> {'stats layout matches' if not fails else f'{len(fails)} mismatch(es)'}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else str(ROOT / "firmware/nordic")))
