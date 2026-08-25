#!/usr/bin/env python3
"""Export a vertex run to the `node_<id>.csv` layout the FTRAC plot tools read.

`tools-python/{plot,sim_analysis,summary_grid}.py` were written against the JS
platform, which wrote one CSV per agent with columns [t, x, z, vartheta]. This
platform writes `<id>.bin` plus `<id>.meta.json`, and its column order is

    timestamp, device_timestamp, state, vstate, vartheta, <per-neighbour...>

so every column after the first is offset by one. Pointed at new data unchanged,
those tools read `device_timestamp` as x, `state` as z and `vstate` as vartheta --
silently, and plausibly, because device_timestamp is monotonic and state does
converge. This writes the layout they expect instead.

    python3 tools/export_csv.py runs/n650-r0 --out export/n650-r0

Node ids are the platform's real ids (1,2 = ble, 11,12 = wifi, 21,22 = bridge),
not 1..N: `summary_grid.agent_type()` depends on that banding. The consumer loop
must therefore iterate the ids in `index.json` rather than `range(NUM_AGENTS)`.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vertex.analysis import load_run  # noqa: E402


def export(run_dir: Path, out_dir: Path, *, relative_time: bool = True) -> dict:
    run = load_run(run_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    t0 = min(run.nodes[i].t[0] for i in run.ids if len(run.nodes[i].t))
    index = {"run": run_dir.name, "ids": [], "types": {}, "t0": t0}

    for nid in run.ids:
        n = run.nodes[nid]
        if not len(n.t):
            print(f"  skip node {nid}: no samples", file=sys.stderr)
            continue
        path = out_dir / f"node_{nid}.csv"
        with path.open("w", encoding="utf-8") as fh:
            fh.write("t,x,z,vartheta\n")
            for k in range(len(n.t)):
                t = n.t[k] - t0 if relative_time else n.t[k]
                fh.write(f"{t:.6f},{n.state[k]:.9g},{n.vstate[k]:.9g},"
                         f"{n.vartheta[k]:.9g}\n")
        index["ids"].append(nid)
        index["types"][str(nid)] = n.node_type
        print(f"  wrote {path}  ({len(n.t)} rows)")

    (out_dir / "index.json").write_text(json.dumps(index, indent=2))
    return index


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("run_dir", type=Path)
    ap.add_argument("--out", type=Path, default=None,
                    help="output directory (default: export/<run name>)")
    ap.add_argument("--absolute-time", action="store_true",
                    help="keep the run's absolute timestamps instead of "
                         "starting at 0; the plot tools assume t starts near 0")
    a = ap.parse_args(argv)
    out = a.out or Path("export") / a.run_dir.name
    idx = export(a.run_dir, out, relative_time=not a.absolute_time)
    print(f"\n  ids: {idx['ids']}")
    print(f"  set CSV_DIR = \"{out}\" and iterate these ids, not range(N)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
