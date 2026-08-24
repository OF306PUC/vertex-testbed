"""Run an experiment across the fleet.

    python3 -m vertex.hub status experiments/n9-ring.yaml
    python3 -m vertex.hub run    experiments/n9-ring.yaml --duration 120
    python3 -m vertex.hub run    experiments/n9-ring.yaml --only 1,11,21

`status` first, always: it is the cheapest way to find a Pi that did not come up,
and finding that out after a 26-minute run is expensive.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

from ..topology import check, load_manifest_file
from .runner import ExperimentRunner


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="python3 -m vertex.hub")
    ap.add_argument("action", choices=["status", "run"])
    ap.add_argument("manifest", type=Path)
    ap.add_argument("--duration", type=float, default=60.0, help="run seconds")
    ap.add_argument("--run-name", default=None,
                    help="default: <manifest>-<run-index>")
    ap.add_argument("--run-index", type=int, default=0,
                    help="selects the initial-condition substream; a different "
                         "index is a different run of the same experiment")
    ap.add_argument("--out-dir", type=Path, default=Path("runs"))
    ap.add_argument("--only", default=None,
                    help="comma-separated node ids, for bringing up a subset")
    ap.add_argument("--settle", type=float, default=0.0,
                    help="seconds between configuring and triggering")
    ap.add_argument("--repeat", type=int, default=1, metavar="N",
                    help="run the SAME configuration N times, named <base>-r0..rN-1. "
                         "The run index is held fixed, so initial conditions and "
                         "disturbance streams are identical across the set and the "
                         "network realisation is the only variable. To vary initial "
                         "conditions instead, use several --run-index values.")
    ap.add_argument("--publish-period", type=float, default=None, metavar="S",
                    help="override the manifest's publish_period_s for this run. "
                         "MUST be an integer multiple of dt_s: the nRF derives its "
                         "publish interval by integer division of clock/dt, while a "
                         "Pi agent sleeps the period exactly, so a non-multiple "
                         "makes the two publish at different rates and silently "
                         "reintroduces the asymmetry the rate rewiring removed.")
    ap.add_argument("--settle-between", type=float, default=5.0, metavar="S",
                    help="seconds between repeats, so neighbour tables and radios "
                         "quiesce before the next run (default 5)")
    ap.add_argument("--timeout", type=float, default=10.0,
                    help="per-command control-plane timeout")
    ap.add_argument("--force", action="store_true",
                    help="run even if the manifest fails validation")
    return ap.parse_args(argv)


async def main_async(args: argparse.Namespace) -> int:
    manifest = load_manifest_file(args.manifest)
    rep = check(manifest)
    for w in rep.warnings:
        print(f"warning: {w}")
    if not rep.ok:
        for e in rep.errors:
            print(f"error: {e}", file=sys.stderr)
        if not args.force:
            print("refusing to run an invalid manifest (--force to override)",
                  file=sys.stderr)
            return 2

    only = ([int(x) for x in args.only.split(",")] if args.only else None)
    runner = ExperimentRunner(manifest, out_dir=args.out_dir,
                              timeout=args.timeout, run_index=args.run_index)

    if args.publish_period is not None:
        dt = manifest.controller.dt_s
        ratio = args.publish_period / dt
        if abs(ratio - round(ratio)) > 1e-9:
            print(f"error: --publish-period {args.publish_period} is not a multiple "
                  f"of dt_s={dt}", file=sys.stderr)
            print(f"       the nRF would publish every {int(ratio)} ticks = "
                  f"{int(ratio)*dt:g}s while a Pi agent publishes every "
                  f"{args.publish_period:g}s", file=sys.stderr)
            mult = [round(k * dt, 6) for k in (1, 2, 3, 5, 10, 25)]
            print(f"       multiples of dt: {mult}", file=sys.stderr)
            return 2
        for nid, a in runner.assignments.items():
            runner.assignments[nid] = a.model_copy(
                update={"publish_period_s": args.publish_period})
        # The assignment is dumped into RunMeta.controller, so the override travels
        # with the data and a swept run is self-describing.
        print(f"publish period overridden: {args.publish_period:g}s "
              f"({1/args.publish_period:g} Hz), {round(ratio)} ticks of dt={dt:g}s")
    try:
        if args.action == "status":
            for nid, st in (await runner.status(only)).items():
                host, port = runner.endpoint(nid)
                if "error" in st:
                    print(f"  FAIL {nid:>3} {host}:{port}  {st['error']}")
                else:
                    print(f"  ok   {nid:>3} {host}:{port}  "
                          f"type={st.get('node_type')} configured={st.get('configured')} "
                          f"running={st.get('running')} samples={st.get('samples')}")
            return 0

        run_name = args.run_name or f"{manifest.name}-{args.run_index}"
        n_nodes = len(only or runner.assignments)

        if args.repeat > 1:
            print(f"{args.repeat} repeats of {run_name}: {n_nodes} nodes, "
                  f"{args.duration:g}s each, run-index {args.run_index} held fixed "
                  f"so only the network varies")
            reports = await runner.run_repeated(
                run_name, args.duration, repeats=args.repeat,
                settle_between_s=args.settle_between, only=only,
                settle_s=args.settle)
            bad = 0
            for rep in reports:
                print()
                print(rep.summary())
                if not rep.ok:
                    bad += 1
            print()
            print(f"{len(reports) - bad}/{len(reports)} runs ok"
                  + (f"; collected under {reports[0].out_dir.parent}"
                     if reports and reports[0].out_dir else ""))
            print(f"aggregate with: python3 tools/compare_runs.py "
                  f"runs/{run_name}-r'*'")
            return 0 if bad == 0 else 1

        # One epoch for the whole fleet, decided here.
        epoch = time.time()
        print(f"run {run_name}: {n_nodes} nodes, {args.duration:g}s, "
              f"epoch {epoch:.3f}")
        report = await runner.run(run_name, args.duration, epoch_unix_s=epoch,
                                  only=only, settle_s=args.settle)
        print(report.summary())
        if report.out_dir:
            print(f"collected into {report.out_dir}")
        return 0 if report.ok else 1
    finally:
        await runner.close()


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(main_async(parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
