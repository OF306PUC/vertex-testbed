#!/usr/bin/env python3
"""Cross-validate the firmware's control law against the host's, numerically.

The platform's headline comparison is BLE agents versus Wi-Fi agents. The law
runs in C on the nRF for the first and in Python on the Pi for the second, so any
systematic difference between the two implementations is an alternative
explanation for whatever the experiment measures. This is the check that the two
are now the same dynamical system.

`test/crossval/harness.c` links the real firmware sources -- agent.c,
coordination_task.c and prng.c, unmodified -- against the stub Zephyr headers, so
what runs here is the code that gets flashed, not a transcription of it.

Configuration reaches the firmware as *encoded frames* built by the host's own
encoders, so agent.c's decoders are on the path too: a field offset that drifts
apart from `vertex/serial/proto.py` fails here rather than on the bench.

The residual is expected to be nonzero: the firmware integrates in float32 where
the host uses float64. That is finding 2(c) in docs/FIRMWARE_DIVERGENCE.md and it
is a precision floor, not a structural difference. What this script asserts is
that the residual stays at that floor -- growing like accumulated rounding, not
like a different equation.

    python3 test/crossval/compare.py
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from vertex.controllers.base import ControllerParams, DisturbanceParams
from vertex.controllers.finite_time_adaptive import FiniteTimeAdaptiveController
from vertex.numeric import quantize
from vertex.pcg32 import Pcg32
from vertex.serial import (FrameType, encode_algorithm, encode_control,
                           encode_disturbance, encode_network)

BIN = Path("/tmp") / "vertex-xval"
SRC = [ROOT / "test/crossval/harness.c",
       ROOT / "firmware/nordic/src/agent.c",
       ROOT / "firmware/nordic/src/coordination_task.c",
       ROOT / "firmware/nordic/src/prng.c"]

# Must mirror harness.c's disturbance block exactly.
DIST = DisturbanceParams(enabled=True, noise_amplitude=0.1, noise_offset=0.5,
                         beta=0.02, sine_amplitude=0.5, sine_frequency_hz=2.0,
                         sine_phase_s=0.25, period_samples=1000)

STEPS = 400
DT_MS = 200
STATE0, VSTATE0, VARTHETA0 = 22.3, 22.3, 0.0
ALPHA, DELTA, ETA = 0.02, 0.01, 2e-6
SEED, NODE = 12345, 3
NEIGHBOURS = [21.0, 23.0]

# One LSB is 1e-6 in engineering units, so this is a 5e-5 tolerance on a state of
# ~22 -- about 2 ppm, the order of the float32 quantum at that magnitude.
TOL_LSB = 50


def build() -> None:
    cmd = ["gcc", "-O2", "-std=c99", "-Wall", "-Wextra", "-Wno-unused-parameter",
           "-include", str(ROOT / "test/common/zstubs/autoconf_stub.h"),
           "-I", str(ROOT / "test/common/zstubs"),
           "-I", str(ROOT / "firmware/nordic/src"),
           *map(str, SRC), "-lm", "-o", str(BIN)]
    subprocess.run(cmd, check=True)


def frames() -> str:
    """The configuration, as the host would send it. One hex line per frame."""
    out = [
        (FrameType.NETWORK, encode_network(enabled=True, node_id=NODE,
                                           neighbors=list(range(1, len(NEIGHBOURS) + 1)))),
        (FrameType.ALGORITHM, encode_algorithm(
            dt_ms=DT_MS, clock_ms=1000,
            state0=quantize(STATE0), vstate0=quantize(VSTATE0),
            vartheta0=quantize(VARTHETA0), counter0=0,
            alpha=quantize(ALPHA), delta=quantize(DELTA), eta=quantize(ETA))),
        (FrameType.DISTURBANCE, encode_disturbance(
            active=DIST.enabled,
            sine_amplitude=quantize(DIST.sine_amplitude),
            frequency=quantize(DIST.sine_frequency_hz),
            phase=quantize(DIST.sine_phase_s),
            noise_amplitude=quantize(DIST.noise_amplitude),
            noise_offset=quantize(DIST.noise_offset),
            beta=quantize(DIST.beta),
            samples=DIST.period_samples)),
        # Last: the trigger latches initial conditions and seeds the PRNG.
        (FrameType.CONTROL, encode_control(trigger=True, seed=SEED)),
    ]
    return "".join(f"{int(t):02X}{p.hex()}\n" for t, p in out)


def run_c() -> list[tuple[int, int, int]]:
    args = [str(BIN), str(STEPS), *[str(quantize(v)) for v in NEIGHBOURS]]
    proc = subprocess.run(args, input=frames(), check=False,
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise SystemExit(f"harness failed ({proc.returncode}): {proc.stderr.strip()}")
    rows = []
    for line in proc.stdout.strip().splitlines():
        _, x, z, th, _counter = line.split(",")
        rows.append((int(x), int(z), int(th)))
    return rows


def run_python() -> list[tuple[int, int, int]]:
    params = ControllerParams(dt_s=DT_MS / 1000.0, state=STATE0, vstate=VSTATE0,
                              vartheta=VARTHETA0, eta=ETA, alpha=ALPHA,
                              delta=DELTA, disturbance=DIST)
    # The same stream the firmware draws: PCG32 seeded (seed, node).
    ctrl = FiniteTimeAdaptiveController(params, uniform=Pcg32(SEED, NODE).uniform)
    nb = [quantize(v) for v in NEIGHBOURS]
    en = [True] * len(nb)
    return [ctrl.step(nb, en).scaled() for _ in range(STEPS)]


def main() -> int:
    build()
    c_rows, py_rows = run_c(), run_python()
    if len(c_rows) != len(py_rows):
        print(f"FAIL: {len(c_rows)} C steps vs {len(py_rows)} Python steps")
        return 1

    names = ("state", "vstate", "vartheta")
    worst = [0, 0, 0]
    worst_at = [0, 0, 0]
    for k, (c, p) in enumerate(zip(c_rows, py_rows)):
        for i in range(3):
            d = abs(c[i] - p[i])
            if d > worst[i]:
                worst[i], worst_at[i] = d, k
    print(f"  {STEPS} steps, dt={DT_MS} ms, disturbance on, PCG32(seed={SEED}, seq={NODE})")
    print(f"  config crossed the real encoders and agent.c's decoders "
          f"({len(frames().splitlines())} frames)")
    print(f"  step 0   C={c_rows[0]}  py={py_rows[0]}")
    print(f"  step {STEPS-1} C={c_rows[-1]}  py={py_rows[-1]}")
    bad = 0
    for i, name in enumerate(names):
        flag = "ok  " if worst[i] <= TOL_LSB else "FAIL"
        if worst[i] > TOL_LSB:
            bad += 1
        print(f"  {flag} {name:<9} max |C - py| = {worst[i]} LSB "
              f"({worst[i]/1e6:.2e}) at step {worst_at[i]}")
    print(f"  -> {'agree within float32 precision' if not bad else 'DIVERGENT'}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
