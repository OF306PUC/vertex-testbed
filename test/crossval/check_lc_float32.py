"""Is the LC interface fragile in float32, or is the nRF build at fault?

A fleet run had LC+ tracking on three Pi-side nodes and missing the band by
6x on the two whose law runs on an nRF. Everything cheap has been eliminated
by measurement: the law in float64 converges on all five, the board with NO
neighbours reproduces the host over 120 s to 283 LSB, the same board on the
DZ arm converges, and the bridge5 control -- same boards, same graph, worse
delivery -- converges in float64.

What remains are two explanations that point opposite ways:

  (a) the LC interface is numerically fragile in single precision once `g`
      moves. A result, and the reason to prefer the proposed interface.
  (b) the ARM build does something the x86 build does not. A bug, and
      publishing (a) over it would be an error a reviewer finds.

This separates them WITHOUT the nRF. The same firmware C, compiled for x86
in float32, is given neighbours that move the way real ones do -- stepped
every publish period, with packets dropped at the measured rate -- and
compared against the float64 Python on identical inputs. If the C diverges
here, precision is the mechanism and no ARM is involved. If it tracks, (a)
is not established and the difference is in the build.

The existing crossval cannot answer this: it holds neighbours FIXED, so `g`
is constant, and `g` is exactly what the LC interface feeds forward.

    python3 test/crossval/check_lc_float32.py
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import numpy as np                                              # noqa: E402

from vertex.controllers import ControllerParams, create         # noqa: E402
from vertex.controllers.interface_lc import LCParams            # noqa: E402
from vertex.controllers.interface_adaptive import AdaptiveParams  # noqa: E402
from vertex.controllers.plant import PlantParams                # noqa: E402
from vertex.controllers.virtual import VirtualParams            # noqa: E402
from vertex.numeric import quantize                             # noqa: E402
from vertex.serial import (FrameType, encode_algorithm, encode_control,
                           encode_disturbance, encode_network)  # noqa: E402
from vertex.serial.proto import (LAW_MICROGRID_ADAPTIVE,        # noqa: E402
                                 LAW_MICROGRID_LC,
                                 MG_IFACE_FIELDS, MG_PLANT_FIELDS,
                                 MG_VIRTUAL_FIELDS,
                                 encode_microgrid_iface,
                                 encode_microgrid_plant,
                                 encode_microgrid_virtual)

BIN = Path("/tmp/vertex-lcf32")
SRC = ROOT / "firmware/nordic/src"

#: DG5's configuration in dg5-mixed: the node that failed, and the one whose
#: parameters the board-level check already reproduces exactly on fixed input.
DT_MS, PUBLISH_MS = 25, 125
KAPPA, S1, S2 = 5 / 15, 5.0, 5.0
X0_P, X0_Q = 4.0, 2.0
Z0_P, Z0_Q = 5.0, 1.0
R_P, R_Q = 20.0, 25.0
T_O, T_C = 30.0, 40.0
DEGREE = 2
SECONDS = 120.0

#: Measured on the failing links of dg5-mixed-0-LCplus: 86-88% of the
#: sender's sequence numbers reached the receiver. A dropped packet is not a
#: gap in the input -- the previous value is HELD, which is what the firmware
#: and the Python both do -- so loss shows up as a longer hold, not a zero.
DELIVERY = 0.87
SEED = 20260930

#: The band the benchmark is judged against, and a quarter of it for the
#: virtual layer.
DELTA_X = 0.45


def neighbour_trace(steps: int, span: int, rng) -> np.ndarray:
    """Held neighbour values per step, stepped at the publish period.

    The neighbours are two nodes converging to the reference on the same
    schedule this node is -- which is what they were in the fleet -- so the
    input is a ramp that the receiver sees as a staircase with irregular
    treads. It is the staircase that matters: a fixed input makes `g`
    constant and is exactly the regime that already passes.
    """
    out = np.zeros((steps, 2, 2))
    held = np.array([[Z0_P, Z0_Q], [Z0_P, Z0_Q]], dtype=float)
    for k in range(steps):
        if k > 0 and k % span == 0:
            t = k * DT_MS / 1000.0
            # A first-order approach to the reference, the shape the virtual
            # layer actually traces; the exact curve does not matter, only
            # that the value moves between exchanges and sometimes does not.
            fresh = np.array([R_P, R_Q]) + (np.array([Z0_P, Z0_Q])
                                            - np.array([R_P, R_Q])) * np.exp(-t / 6.0)
            for j in range(2):
                if rng.random() < DELIVERY:
                    held[j] = fresh
        out[k] = held
    return out


def mg_fields(lc: bool) -> dict:
    return dict(
        kappa=quantize(KAPPA), S1=quantize(S1), S2=quantize(S2),
        p_max=quantize(10.0), q_max=quantize(12.0),
        T_o=int(T_O * 1000), alpha_0=quantize(1.0), delta_o=PUBLISH_MS,
        kappa_o=quantize(1e-3), xi=quantize(0.025), c_0=quantize(0.1),
        b=0, rP=0, rQ=0, degree=DEGREE, mu_max=quantize(10.0),
        gamma=0 if lc else quantize(0.0025),
        eps=0 if lc else quantize(0.09376),
        vartheta_0=0,
        T_c=int(T_C * 1000) if lc else 0,
        beta_c=quantize(2.0) if lc else 0,
        Phi=quantize(3.0) if lc else 0,
        delta_c=0, adapt_band=-1,
        X0_P=quantize(X0_P), X0_Q=quantize(X0_Q),
        z0_P=quantize(Z0_P), z0_Q=quantize(Z0_Q))


def frames(lc: bool) -> str:
    f = mg_fields(lc)
    out = [
        (FrameType.NETWORK, encode_network(enabled=True, node_id=5,
                                           neighbors=[4, 1])),
        (FrameType.ALGORITHM, encode_algorithm(
            dt_ms=DT_MS, clock_ms=PUBLISH_MS, state0=0, vstate0=0,
            vartheta0=0, counter0=0, gain_ij=0, alpha=quantize(0.5),
            delta=0, eta=0)),
        (FrameType.DISTURBANCE, encode_disturbance(
            active=False, sine_amplitude=0, frequency=0, phase=0,
            noise_amplitude=0, noise_offset=0, beta=0, samples=1_000_000)),
        (FrameType.MG_PLANT, encode_microgrid_plant(
            profile=1, **{k: f[k] for k in MG_PLANT_FIELDS})),
        (FrameType.MG_VIRTUAL, encode_microgrid_virtual(
            **{k: f[k] for k in MG_VIRTUAL_FIELDS})),
        (FrameType.MG_IFACE, encode_microgrid_iface(
            law=LAW_MICROGRID_LC if lc else LAW_MICROGRID_ADAPTIVE,
            terminal=False, **{k: f[k] for k in MG_IFACE_FIELDS})),
        (FrameType.CONTROL, encode_control(trigger=True, seed=1)),
    ]
    return "".join(bytes([t]).hex() + p.hex() + "\n" for t, p in out)


def build() -> None:
    subprocess.run(
        ["gcc", "-O2", "-std=c99", "-Wall", "-Wextra", "-Wno-unused-parameter",
         "-include", str(ROOT / "test/common/zstubs/autoconf_stub.h"),
         "-I", str(ROOT / "test/common/zstubs"), "-I", str(SRC),
         str(ROOT / "test/crossval/harness.c"), str(SRC / "agent.c"),
         str(SRC / "coordination_task.c"), str(SRC / "microgrid_task.c"),
         str(SRC / "prng.c"), "-lm", "-o", str(BIN)], check=True)


def c_sigma(lc: bool, nbr: np.ndarray) -> np.ndarray:
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as fh:
        for row in nbr:
            fh.write(" ".join(f"{quantize(p)},{quantize(q)}" for p, q in row)
                     + "\n")
        path = fh.name
    r = subprocess.run([str(BIN), str(len(nbr)), f"@{path}"],
                       input=frames(lc), text=True, capture_output=True,
                       check=True)
    rows = [[int(x) for x in ln.split(",")[1:]]
            for ln in r.stdout.strip().splitlines()]
    a = np.asarray(rows, float) / 1e6
    return np.abs(a[:, 0] - a[:, 2]) + np.abs(a[:, 1] - a[:, 3])


def py_sigma(lc: bool, nbr: np.ndarray) -> np.ndarray:
    iface = (LCParams(kappa=KAPPA, profile="per", T_c=T_C, beta_c=2.0,
                      Phi=3.0, terminal=False, mu_max=10.0)
             if lc else AdaptiveParams(gamma=0.0025, eps=0.09376, mu_max=10.0))
    c = create("microgrid_lc" if lc else "microgrid_adaptive", ControllerParams(
        dt_s=DT_MS / 1000.0, degree=DEGREE,
        plant=PlantParams(kappa=KAPPA, S1=S1, S2=S2, profile="per"),
        virtual=VirtualParams(T_o=T_O, alpha_0=1.0, delta_o=PUBLISH_MS / 1000.0,
                              kappa_o=1e-3, xi=0.025, c_0=0.1,
                              b=0.0, rP=0.0, rQ=0.0),
        interface=iface,
        state0_P=X0_P, state0_Q=X0_Q, vstate0_P=Z0_P, vstate0_Q=Z0_Q))
    out = []
    for row in nbr:
        # SCALED INTEGERS, which is what a controller's `step` takes -- the
        # agent hands it `NeighborTable.snapshot()` and that is the wire's
        # fixed point, not engineering units. Dividing back by 1e6 here fed
        # the Python a different input from the C and made the comparison
        # meaningless while still printing a plausible number.
        flat = [quantize(v) for pair in row for v in pair]
        c.step(flat, [True] * DEGREE)
        out.append(abs(c.plant.xP - c.virtual.zP)
                   + abs(c.plant.xQ - c.virtual.zQ))
    return np.asarray(out)


def main() -> int:
    build()
    steps = int(SECONDS * 1000 / DT_MS)
    span = PUBLISH_MS // DT_MS
    rng = np.random.default_rng(SEED)
    nbr = neighbour_trace(steps, span, rng)
    held = int(np.sum(np.any(np.diff(nbr[::span, 0, 0]) != 0, keepdims=True)))

    print(f"the LC interface in float32, on neighbours that move")
    print(f"  {steps} steps at {DT_MS} ms, exchanges every {PUBLISH_MS} ms, "
          f"delivery {DELIVERY:.0%}, seed {SEED}")
    print(f"  DG5's configuration: kappa={KAPPA:.4f}, S=({S1},{S2}), "
          f"unpinned, degree {DEGREE}\n")

    bad = 0
    for lc, label in ((False, "C_DZ "), (True, "C_LC+")):
        cs, ps = c_sigma(lc, nbr), py_sigma(lc, nbr)
        k = int(T_C * 1000 / DT_MS)
        c_end, p_end = cs[k:].max(), ps[k:].max()
        gap = np.abs(cs - ps).max()
        verdict = ("float32 DIVERGES" if c_end > DELTA_X >= p_end
                   else "both in band" if max(c_end, p_end) <= DELTA_X
                   else "both out of band")
        print(f"  {label}  worst |sigma| after T_c:  C(float32) {c_end:8.4f}   "
              f"py(float64) {p_end:8.4f}   max|C-py| {gap:.4f}   -> {verdict}")
        if c_end > DELTA_X >= p_end:
            bad += 1
    print()
    if bad:
        print("  -> precision is the mechanism: the same C in float32 misses "
              "the band on x86, with no nRF involved")
    else:
        print("  -> float32 alone does NOT reproduce the fleet divergence; "
              "the difference is elsewhere, and 'LC is numerically fragile' "
              "is not established by this")
    return 0


if __name__ == "__main__":
    sys.exit(main())
