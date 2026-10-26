#!/usr/bin/env python3
"""Cross-validate the microgrid law in C against the Python it mirrors.

`compare.py` does this for the scalar law. This is its counterpart for the
second family, and it closes the last blind link in the chain:

    reviewers-exp/sim  <->  vertex/controllers   (test/oracle/*)
    vertex/controllers <->  firmware/nordic      (here)

The C is the real firmware source, linked unchanged against the stub Zephyr
headers, and its configuration arrives as **encoded frames** built by the
host's own encoder, so agent.c's `apply_microgrid` is on the path. A field
that drifts from vertex/serial/proto.py fails here rather than on the bench.

Both laws are exercised, because they take different branches through
`microgrid_step` and a mistake in one would not show in the other.

The residual is expected to be nonzero: the firmware integrates in float32
where the host uses float64. That is a precision floor, and the assertion is
that it stays one -- growing like accumulated rounding rather than like a
different equation.

    python3 test/crossval/check_microgrid.py
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from vertex.controllers import ControllerParams, create              # noqa: E402
from vertex.controllers.interface_adaptive import AdaptiveParams     # noqa: E402
from vertex.controllers.interface_lc import LCParams                 # noqa: E402
from vertex.controllers.plant import PlantParams                     # noqa: E402
from vertex.controllers.virtual import VirtualParams                 # noqa: E402
from vertex.numeric import quantize                                  # noqa: E402
from vertex.serial import (FrameType, encode_algorithm, encode_control,
                           encode_disturbance, encode_network)       # noqa: E402
from vertex.serial.proto import (MG_IFACE_FIELDS, MG_PLANT_FIELDS,   # noqa: E402
                                 MG_VIRTUAL_FIELDS,
                                 LAW_MICROGRID_ADAPTIVE,
                                 LAW_MICROGRID_LC,
                                 encode_microgrid_iface,
                                 encode_microgrid_plant,
                                 encode_microgrid_virtual)

BIN = Path("/tmp/vertex-mgxval")
SRC = ROOT / "firmware/nordic/src"
#: 75 s at 25 ms, which must be past T_c = 40 s. At 1500 steps the run ended
#: at 37.5 s, LC0's terminal branch never ran, and C_LC0 and C_LC+ produced
#: identical rows: the check passed while the one behaviour that separates
#: them was untested.
STEPS = 3000
DT_MS = 25
KAPPA, S1, S2 = 5 / 15, 5.0, 5.0
DEGREE = 2
NEIGHBOURS = ((quantize(3.0), quantize(4.0)), (quantize(6.0), quantize(2.0)))

#: Least significant bits of the 1e6 fixed point. float32 carries about seven
#: decimal digits, so a state of order 20 holds roughly 1e-5, which is 10 LSB;
#: 1500 steps of accumulation puts the observed worst case an order above
#: that. A structural difference would be orders more.
TOL_LSB = 2_000


def build() -> None:
    cmd = ["gcc", "-O2", "-std=c99", "-Wall", "-Wextra", "-Wno-unused-parameter",
           "-include", str(ROOT / "test/common/zstubs/autoconf_stub.h"),
           "-I", str(ROOT / "test/common/zstubs"), "-I", str(SRC),
           str(ROOT / "test/crossval/harness.c"),
           str(SRC / "agent.c"), str(SRC / "coordination_task.c"),
           str(SRC / "microgrid_task.c"), str(SRC / "prng.c"),
           "-lm", "-o", str(BIN)]
    subprocess.run(cmd, check=True)


def mg_fields(lc: bool) -> dict:
    """One configuration, in the units the wire uses."""
    return dict(
        kappa=quantize(KAPPA), S1=quantize(S1), S2=quantize(S2),
        p_max=quantize(10.0), q_max=quantize(12.0),
        T_o=30_000, alpha_0=quantize(1.0), delta_o=125,
        kappa_o=quantize(1e-3), xi=quantize(0.025), c_0=quantize(0.1),
        b=quantize(1.0), rP=quantize(20.0), rQ=quantize(25.0),
        degree=DEGREE,
        mu_max=quantize(10.0),
        gamma=0 if lc else quantize(0.0025),
        eps=0 if lc else quantize(0.09),
        vartheta_0=0,
        T_c=40_000 if lc else 0,
        beta_c=quantize(2.0) if lc else 0,
        Phi=quantize(3.0) if lc else 0,
        delta_c=0,
        adapt_band=-1,
        X0_P=quantize(4.0), X0_Q=quantize(2.0),
        z0_P=quantize(5.0), z0_Q=quantize(1.0),
    )


def frames(lc: bool, terminal: bool, samples: int = 100_000) -> str:
    f = mg_fields(lc)
    out = [
        (FrameType.NETWORK, encode_network(enabled=True, node_id=5,
                                           neighbors=[4, 1])),
        (FrameType.ALGORITHM, encode_algorithm(
            dt_ms=DT_MS, clock_ms=125, state0=0, vstate0=0, vartheta0=0,
            counter0=0, gain_ij=0, alpha=quantize(0.5), delta=0, eta=0)),
        # `samples` is the SCALAR law's disturbance period and the microgrid
        # family never reads it -- but the firmware once wrapped its own
        # clock by it, so this value decided whether the law's time ran
        # forward or reset mid-run. Both checks here used a period far longer
        # than the run, which is exactly why neither caught it; the run below
        # uses the manifest's own default, which does wrap.
        (FrameType.DISTURBANCE, encode_disturbance(
            active=False, sine_amplitude=0, frequency=0, phase=0,
            noise_amplitude=0, noise_offset=0, beta=0, samples=samples)),
        # Three frames, in order. MG_IFACE carries `law` and agent.c refuses
        # it until the other two have landed, so this sequence is also the
        # test that the commit rule holds -- reorder it and the run fails.
        (FrameType.MG_PLANT, encode_microgrid_plant(
            profile=1, **{k: f[k] for k in MG_PLANT_FIELDS})),
        (FrameType.MG_VIRTUAL, encode_microgrid_virtual(
            **{k: f[k] for k in MG_VIRTUAL_FIELDS})),
        (FrameType.MG_IFACE, encode_microgrid_iface(
            law=LAW_MICROGRID_LC if lc else LAW_MICROGRID_ADAPTIVE,
            terminal=terminal, **{k: f[k] for k in MG_IFACE_FIELDS})),
        (FrameType.CONTROL, encode_control(trigger=True, seed=1)),
    ]
    return "".join(bytes([t]).hex() + p.hex() + "\n" for t, p in out)


def c_rows(lc: bool, terminal: bool, samples: int = 100_000) -> list[tuple[int, ...]]:
    args = [str(BIN), str(STEPS)] + [f"{p},{q}" for p, q in NEIGHBOURS]
    r = subprocess.run(args, input=frames(lc, terminal, samples), text=True,
                       capture_output=True, check=True)
    return [tuple(int(x) for x in ln.split(",")[1:])
            for ln in r.stdout.strip().splitlines()]


def py_rows(lc: bool, terminal: bool) -> list[tuple[int, ...]]:
    f = mg_fields(lc)
    iface = (LCParams(kappa=KAPPA, profile="per", T_c=40.0, beta_c=2.0,
                      Phi=3.0, terminal=terminal, mu_max=10.0)
             if lc else
             AdaptiveParams(gamma=0.0025, eps=0.09, mu_max=10.0))
    c = create("microgrid_lc" if lc else "microgrid_adaptive", ControllerParams(
        dt_s=DT_MS / 1000.0, degree=DEGREE,
        plant=PlantParams(kappa=KAPPA, S1=S1, S2=S2, profile="per"),
        virtual=VirtualParams(T_o=30.0, alpha_0=1.0, delta_o=0.125,
                              kappa_o=1e-3, xi=0.025, c_0=0.1,
                              b=1.0, rP=20.0, rQ=25.0),
        interface=iface,
        state0_P=4.0, state0_Q=2.0, vstate0_P=5.0, vstate0_Q=1.0))
    flat = [v for pair in NEIGHBOURS for v in pair]
    out = []
    for _ in range(STEPS):
        c.step(flat, [True] * DEGREE)
        v, it = c.virtual, c.interface
        out.append((quantize(c.plant.xP), quantize(c.plant.xQ),
                    quantize(v.zP), quantize(v.zQ), quantize(v.c),
                    quantize(getattr(it, "vartheta", float("nan")))
                    if not lc else 0,
                    quantize(getattr(it, "S1", 0.0)),
                    quantize(getattr(it, "S2", 0.0)),
                    0))
    return out


NAMES = ("xP", "xQ", "zP", "zQ", "c", "vartheta", "S1", "S2", "watchdog")


def one(lc: bool, terminal: bool, label: str, samples: int = 100_000) -> int:
    c, p = c_rows(lc, terminal, samples), py_rows(lc, terminal)
    if len(c) != len(p):
        print(f"  FAIL {label}: {len(c)} C steps vs {len(p)} Python")
        return 1
    # vartheta is NaN on the LC side and 0 on the C side: an absence, not a
    # value, and comparing it would compare two spellings of "not here".
    skip = {5, 8} if lc else {6, 7, 8}
    worst = [0] * len(NAMES)
    for a, b in zip(c, p):
        for i in range(len(NAMES)):
            if i in skip:
                continue
            worst[i] = max(worst[i], abs(a[i] - b[i]))
    bad = [NAMES[i] for i in range(len(NAMES)) if worst[i] > TOL_LSB]
    detail = " ".join(f"{NAMES[i]}={worst[i]}" for i in range(len(NAMES))
                      if i not in skip)
    print(f"  {'ok  ' if not bad else 'FAIL'} {label:<28} max |C - py| LSB: "
          f"{detail}")
    return 1 if bad else 0


#: agent.h's AGENT_ERR_RANGE.
ERR_RANGE = -2


def check_commit_rule() -> int:
    """MG_IFACE alone must not change the law.

    The frames were split because the transport forced it, but committing
    them as a set is a choice: `law` is what takes the board out of the
    scalar family, and a board running a law whose plant or observer never
    arrived would integrate garbage while reporting normally. This asserts
    the refusal rather than trusting the ordering to hold.
    """
    f = mg_fields(False)
    out = [
        (FrameType.NETWORK, encode_network(enabled=True, node_id=5,
                                           neighbors=[4, 1])),
        (FrameType.ALGORITHM, encode_algorithm(
            dt_ms=DT_MS, clock_ms=125, state0=0, vstate0=0, vartheta0=0,
            counter0=0, gain_ij=0, alpha=quantize(0.5), delta=0, eta=0)),
        (FrameType.MG_IFACE, encode_microgrid_iface(
            law=LAW_MICROGRID_ADAPTIVE, terminal=False,
            **{k: f[k] for k in MG_IFACE_FIELDS})),
    ]
    stdin = "".join(bytes([t]).hex() + p.hex() + "\n" for t, p in out)
    r = subprocess.run([str(BIN), "1"], input=stdin, text=True,
                       capture_output=True)
    want = f"frame 0x{int(FrameType.MG_IFACE):02X} rejected: {ERR_RANGE}"
    if want in r.stderr:
        print(f"  ok   MG_IFACE alone is refused ({ERR_RANGE}), so a half "
              f"configuration cannot select a law")
        return 0
    print(f"  FAIL MG_IFACE alone was accepted; expected {want!r}, "
          f"got {r.stderr.strip()!r}")
    return 1


def main() -> int:
    build()
    print(f"microgrid law, C vs Python: {STEPS} steps at dt = {DT_MS} ms, "
          f"config through the real encoders")
    bad = (one(False, False, "C_DZ  (adaptive, eps > 0)")
           + one(True, False, "C_LC+ (no terminal policy)")
           + one(True, True, "C_LC0 (terminal policy)")
           + one(True, False, "C_LC+ (samples wraps at 25 s)", samples=1000)
           + check_commit_rule())
    print(f"  -> {'agree within float32 precision' if not bad else 'DIVERGENT'}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
