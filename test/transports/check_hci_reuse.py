#!/usr/bin/env python3
"""One HCI socket per process, not one per run.

`AgentService._build()` constructs a fresh `BleTransport` on every run. If that
transport also opens its own HCI user channel, a parameter sweep opens one socket
per repeat -- and the kernel does not release `hci0` instantly on close, so a few
points in, `open()` fails with EBUSY and every remaining repeat of that point
records nothing.

Measured on hardware: `sweep2-p080` lost both bridge agents on all 10 runs, after
p400 and p200 had already consumed 20 opens. The two good points came first, which
is why this looks like a rate-dependent failure and is not one.

This is a counting test rather than a behavioural one because the failure needs
~20 real opens to appear: nothing short of the full sweep reproduces it live.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from vertex.agent.service import AgentService     # noqa: E402
from vertex.topology.models import AgentType      # noqa: E402

opens = 0


class FakeSock:
    def __init__(self, *a, **k): pass
    def open(self):
        global opens
        opens += 1
        return self
    def command(self, *a, **k): return b""
    def close(self): pass
    @property
    def fileno(self): return -1


def main() -> int:
    import vertex.radio.hci as hci
    real = hci.HciSocket
    hci.HciSocket = FakeSock
    try:
        svc = AgentService.__new__(AgentService)
        svc.agent = None
        svc._hci = None
        svc.clock = None
        svc.node_type = AgentType.BRIDGE
        svc.radio = {"adv_interval_ms": 100.0, "scan_interval_ms": 100.0,
                     "scan_window_ms": 100.0}
        svc.assignment = None

        # Ten runs of the same agent, as `--repeat 10` does.
        socks = [svc._ble_transport(21)._sock for _ in range(10)]
    finally:
        hci.HciSocket = real

    if opens != 1:
        print(f"FAIL: {opens} HCI opens across 10 runs, expected 1")
        print("      a sweep would exhaust the adapter -- see the docstring")
        return 1
    if len({id(s) for s in socks}) != 1:
        print("FAIL: transports did not share one socket object")
        return 1
    print(f"  1 HCI open across 10 transport builds, socket shared")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
