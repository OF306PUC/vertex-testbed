#!/usr/bin/env python3
"""Derive a LoopbackBus link profile from measured hardware runs.

Thirty heterogeneous agents cannot run on three Raspberry Pis: the HCI user
channel is exclusive, so a host carries at most one `bridge`, and the firmware
runs one agent per nRF52. Simulation is the only route to that scale -- but a
simulation with one global loss figure would misrepresent a network whose
measured per-class delivery spans 0.61 to 0.98.

This reads real runs and emits the per-class loss and delay to hand to
`LoopbackBus`, so the 30-agent simulation is calibrated against hardware rather
than assumed.

    python3 tools/link_profile.py runs/n950-r*  --exclude n950-r1
"""
from __future__ import annotations

import argparse
import json
import statistics as st
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from vertex.analysis import load_run          # noqa: E402


def profile(run_dirs: list[Path]) -> dict:
    deliv: dict[tuple[str, str], list[float]] = {}
    delay: dict[tuple[str, str], list[float]] = {}
    for d in run_dirs:
        r = load_run(d)
        for dst in r.ids:
            n = r.nodes[dst]
            for src in n.neighbours:
                key = (r.nodes[src].node_type, n.node_type)
                dd = n.link_delivery(src) or {}
                v = dd.get("exact_delivery_ratio")
                v = v if v is not None else dd.get("delivery_ratio")
                if v is not None:
                    deliv.setdefault(key, []).append(v)
                agg = ((n.meta.get("environment") or {}).get("links")
                       or {}).get(str(src)) or {}
                if agg.get("median_delay_us"):
                    delay.setdefault(key, []).append(agg["median_delay_us"] / 1e6)

    out = {"runs": [d.name for d in run_dirs], "loss": {}, "delay_s": {}, "n": {}}
    for key, vs in sorted(deliv.items()):
        k = f"{key[0]}->{key[1]}"
        out["loss"][k] = round(1.0 - st.fmean(vs), 4)
        out["n"][k] = len(vs)
    for key, vs in sorted(delay.items()):
        out["delay_s"][f"{key[0]}->{key[1]}"] = round(st.fmean(vs), 4)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("runs", nargs="+", type=Path)
    ap.add_argument("--exclude", nargs="*", default=[],
                    help="run directory names to drop, e.g. a collapsed link")
    ap.add_argument("--out", type=Path, default=None)
    a = ap.parse_args(argv)
    dirs = [d for d in a.runs if d.is_dir() and d.name not in a.exclude]
    if not dirs:
        print("no run directories", file=sys.stderr)
        return 2
    pr = profile(dirs)
    text = json.dumps(pr, indent=2)
    print(text)
    if a.out:
        a.out.write_text(text)
        print(f"\n  wrote {a.out}", file=sys.stderr)
    print("\n  delay is only measured where a Pi receives; pairs whose receiver is",
          file=sys.stderr)
    print("  an nRF have no entry and fall back to the bus scalar.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
