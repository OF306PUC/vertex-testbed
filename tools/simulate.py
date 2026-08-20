#!/usr/bin/env python3
"""Run every manifest in simulation and print a convergence report.

    python3 tools/simulate.py [--duration 120] [--loss 0.0] [--publish 0.2]

The point of this script: a topology can be validated, simulated and judged
without touching hardware, in about a second per experiment. Use it to sanity-check
a manifest or a controller change before deploying anything.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vertex.sim import simulate                    # noqa: E402
from vertex.topology import check, load_manifest_file  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--duration", type=float, default=120.0, help="virtual seconds")
    ap.add_argument("--loss", type=float, default=0.0, help="per-receiver drop rate")
    ap.add_argument("--delay", type=float, default=0.0, help="channel delay, seconds")
    ap.add_argument("--publish", type=float, default=None,
                    help="override publish period, seconds")
    ap.add_argument("--only", default=None, help="run one manifest by stem")
    args = ap.parse_args()

    paths = sorted((ROOT / "experiments").glob("*.yaml"))
    if args.only:
        paths = [p for p in paths if p.stem == args.only]
        if not paths:
            print(f"no manifest named {args.only!r}", file=sys.stderr)
            return 2

    failures = 0
    for path in paths:
        m = load_manifest_file(path)
        if args.publish is not None:
            for n in m.nodes:
                object.__setattr__(n, "publish_period_s", args.publish)

        rep = check(m)
        if not rep.ok:
            print(f"{m.name}: INVALID\n{rep.summary()}\n")
            failures += 1
            continue

        t = time.perf_counter()
        out = await simulate(m, duration_s=args.duration, loss=args.loss,
                             delay_s=args.delay)
        wall = time.perf_counter() - t

        print(out.summary())
        print(f"  declared lambda_2={rep.algebraic_connectivity}, "
              f"wall {wall:.2f}s for {args.duration:g} virtual seconds")
        for w in rep.warnings:
            print(f"  warning {w[:100]}{'...' if len(w) > 100 else ''}")
        print()
        if not out.run.ok:
            failures += 1
    return failures


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
