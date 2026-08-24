#!/usr/bin/env python3
"""Measure the controller's real advertising-interval floor, per advertising type.

The Bluetooth 4.x spec required Advertising_Interval_Min >= 0x00A0 (100 ms) for
ADV_NONCONN_IND and ADV_SCAN_IND. Bluetooth 5.0 removed that restriction and
allows 0x0020 (20 ms). Which rule a controller enforces is a property of its
firmware, not of the host stack, so it has to be measured.

It matters because the advertising interval is a delivery ceiling
(PLATFORM.md 6.3): if the floor is 100 ms, no bridge agent can publish faster
than 10 Hz without capping its own delivery, whatever the manifest says.

Run with the agents stopped -- this needs the user channel:

    bash scripts/agents.sh stop
    sudo .venv/bin/python scripts/adv_floor.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vertex.radio.hci import (ADV_IND, ADV_NONCONN_IND, ADV_SCAN_IND,  # noqa: E402
                              HciError, HciSocket,
                              cmd_le_set_adv_parameters, cmd_reset)

TYPES = [("ADV_NONCONN_IND", ADV_NONCONN_IND),   # what the platform uses
         ("ADV_SCAN_IND", ADV_SCAN_IND),
         ("ADV_IND", ADV_IND)]


def accepts(sock, adv_type: int, units: int) -> bool:
    try:
        sock.command(cmd_le_set_adv_parameters(
            interval_min=units, interval_max=units, adv_type=adv_type))
        return True
    except HciError:
        return False


def main() -> int:
    sock = HciSocket(0).open()
    try:
        sock.command(cmd_reset())
        print(f"  {'type':<18} {'floor':>10}   {'spec 4.x':>9} {'spec 5.0':>9}")
        for name, t in TYPES:
            lo, hi = 0x0020, 0x4000          # 20 ms .. 10.24 s
            if not accepts(sock, t, hi):
                print(f"  {name:<18} {'REJECTS ALL':>10}")
                continue
            while lo < hi:                    # lowest accepted value
                mid = (lo + hi) // 2
                if accepts(sock, t, mid):
                    hi = mid
                else:
                    lo = mid + 1
            print(f"  {name:<18} {lo*0.625:>7.1f} ms   {'100.0':>9} {'20.0':>9}"
                  f"   (0x{lo:04X})")
        print()
        print("  The platform advertises ADV_NONCONN_IND. Its floor is the fastest")
        print("  publish rate a bridge can sustain without capping delivery:")
        print("      max publish rate = 1000 / floor_ms  Hz")
    finally:
        try:
            sock.close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
