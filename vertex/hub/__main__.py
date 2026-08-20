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
        # One epoch for the whole fleet, decided here.
        epoch = time.time()
        print(f"run {run_name}: {len(only or runner.assignments)} nodes, "
              f"{args.duration:g}s, epoch {epoch:.3f}")
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
