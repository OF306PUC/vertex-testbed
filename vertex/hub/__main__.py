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
from .runner import ARMS, ExperimentRunner


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
    # The four arms of the microgrid comparison. `run_arms` has existed since
    # the family was added and had no way to reach it from a terminal, so the
    # only arm anyone could run was whichever one the manifest was written
    # for.
    ap.add_argument("--arm", metavar="NAME",
                    help=f"run ONE arm instead of the manifest's own: "
                         f"{', '.join(ARMS)}. The manifest is rebuilt with "
                         f"that arm's single field changed and nothing else")
    ap.add_argument("--arms", metavar="A,B,...",
                    help=f"paired comparison: every arm in a randomized order "
                         f"per trial (default {','.join(ARMS)}). Use --trials "
                         f"for more than one")
    ap.add_argument("--trials", type=int, default=1, metavar="N",
                    help="trials for --arms; each runs every arm once, in a "
                         "fresh random order")
    ap.add_argument("--order-seed", type=int, default=0,
                    help="seed for the --arms ordering, so a campaign's "
                         "sequence is reproducible from the record")
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
        # The override does NOT touch radio.adv_interval_ms, so publishing faster
        # than the advertising interval caps BLE delivery at min(1, T_pub/T_adv)
        # and the run measures the transmitter's sampling rate. That is exactly how
        # the first publish-rate sweep was confounded (PLATFORM.md A3.4). Refuse
        # rather than warn: the resulting numbers look like a property of the medium
        # and nothing downstream can tell that they are not.
        adv_s = manifest.radio.adv_interval_ms / 1000.0
        if args.publish_period < adv_s and not args.force:
            ceiling = args.publish_period / adv_s
            print(f"error: --publish-period {args.publish_period:g}s is shorter than "
                  f"the manifest's advertising interval "
                  f"{manifest.radio.adv_interval_ms:g}ms", file=sys.stderr)
            print(f"       BLE delivery would be capped at {ceiling:.3f} regardless "
                  f"of link quality, and the sweep would measure that cap",
                  file=sys.stderr)
            if args.publish_period * 1000 >= 100.0:
                print(f"       use a manifest whose radio block matches this rate "
                      f"(tools/make_manifests.py radio_for), or --force to accept "
                      f"and report the ceiling", file=sys.stderr)
            else:
                print(f"       {args.publish_period*1000:g} ms would be needed to "
                      f"lift the ceiling, below the 100 ms controller floor: it "
                      f"cannot be lifted. --force to accept and report it.",
                      file=sys.stderr)
            return 2
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

        if args.arms:
            arms = [a.strip() for a in args.arms.split(",") if a.strip()]
            unknown = [a for a in arms if a not in ARMS]
            if unknown:
                print(f"error: unknown arm(s) {unknown}; known: {list(ARMS)}",
                      file=sys.stderr)
                return 2
            total = len(arms) * args.trials
            print(f"{args.trials} trial(s) x {len(arms)} arm(s) = {total} runs "
                  f"of {args.duration:g}s, order randomized within each trial "
                  f"(seed {args.order_seed}); run-index {args.run_index} held "
                  f"fixed so the arms of a trial share initial conditions")
            reports = await runner.run_arms(
                run_name, args.duration, arms=arms, trials=args.trials,
                settle_between_s=args.settle_between, only=only,
                settle_s=args.settle, order_seed=args.order_seed)
            bad = 0
            for rep in reports:
                print()
                print(rep.summary())
                for note in rep.notes:
                    print(f"  {note}")
                if not rep.ok:
                    bad += 1
            print()
            print(f"{len(reports) - bad}/{len(reports)} runs ok")
            return 0 if bad == 0 else 1

        if args.arm:
            if args.arm not in ARMS:
                print(f"error: unknown arm {args.arm!r}; known: {list(ARMS)}",
                      file=sys.stderr)
                return 2
            runner.set_arm(args.arm)
            run_name = args.run_name or (f"{manifest.name}-{args.run_index}"
                                         f"-{args.arm.replace('+', 'plus')}")
            print(f"arm {args.arm}: one field changed from the manifest, "
                  f"everything else identical")

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
