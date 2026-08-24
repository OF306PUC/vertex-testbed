#!/usr/bin/env python3
"""Aggregate a set of repeated runs into one measurement with error bars.

    python3 tools/compare_runs.py runs/n6-fast-0-r*
    python3 tools/compare_runs.py runs/n6-fast-0-r* --out results --label baseline

A single run cannot resolve an effect smaller than the run-to-run spread, and on
this platform that spread is not small: BLE delivery has ranged over 6.2 points
across nominally identical runs while UDP moved 2.2. Every number this prints is
therefore mean +- sd over the set, with n, and the sd is the thing that says
whether a difference between two configurations means anything.

## It checks the runs are actually replicates first

The set is only a set if the runs share a configuration. Holding `--run-index`
fixed makes the initial conditions and every node's disturbance stream identical,
so the network realisation is the only variable. If someone passes a different
index, the runs are still runs but they are no longer replicates, and averaging
them silently mixes two sources of variance. So the initial `vstate` of every node
is compared across the set and a mismatch is reported before any statistic.
"""
from __future__ import annotations

import argparse
import statistics
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vertex.analysis import Run, load_run  # noqa: E402

COLOUR = {"BLE": "#c0392b", "UDP": "#2471a3"}


def medium(run: Run, src: int, dst: int) -> str:
    kinds = (run.nodes[src].node_type, run.nodes[dst].node_type)
    return "BLE" if "ble" in kinds else "UDP"


def mean_sd(xs: list[float]) -> tuple[float, float, int]:
    xs = [x for x in xs if x is not None]
    if not xs:
        return (float("nan"), float("nan"), 0)
    return (statistics.fmean(xs),
            statistics.stdev(xs) if len(xs) > 1 else 0.0,
            len(xs))


def convergence_time(run: Run, threshold: float) -> float | None:
    """First time the spread drops below `threshold` and stays below it.

    'And stays' matters: the spread can dip through a threshold in passing while
    agents are still crossing over one another, and a first-crossing time would
    report that transient as convergence.
    """
    ts = [i * run.duration_s / 600 for i in range(601)]
    spreads = [run.spread(t) for t in ts]
    last_above = None
    for t, sp in zip(ts, spreads):
        if sp >= threshold:
            last_above = t
    if last_above is None:
        return 0.0
    if last_above >= ts[-1]:
        return None                     # never converged within the run
    return last_above


def check_replicates(runs: list[Run]) -> list[str]:
    """Are these the same configuration? Returns complaints."""
    out = []
    base = runs[0]
    for r in runs[1:]:
        if sorted(r.ids) != sorted(base.ids):
            out.append(f"{r.name}: node set differs from {base.name}")
            continue
        for nid in base.ids:
            a, b = base.nodes[nid].vstate[0], r.nodes[nid].vstate[0]
            if abs(a - b) > 1e-6:
                out.append(f"{r.name}: node {nid} starts at {b:.6f}, "
                           f"{base.name} starts at {a:.6f} -- not a replicate "
                           f"(different --run-index?)")
        if r.manifest != base.manifest:
            out.append(f"{r.name}: manifest {r.manifest!r} != {base.manifest!r}")
    return out


def link_table(runs: list[Run]) -> list[tuple]:
    """(src, dst, medium, delivery m/sd/n, delay m/sd/n, rssi m/sd/n, exact?)"""
    base = runs[0]
    rows = []
    for dst in base.ids:
        for src in base.nodes[dst].neighbours:
            deliv, delay, rssi = [], [], []
            exact_used = True
            for r in runs:
                node = r.nodes.get(dst)
                if node is None:
                    continue
                d = node.link_delivery(src)
                ex = d.get("exact_delivery_ratio") if d else None
                if ex is None:
                    exact_used = False
                    ex = d.get("delivery_ratio") if d else None
                deliv.append(ex)
                agg = ((node.meta.get("environment") or {}).get("links")
                       or {}).get(str(src)) or {}
                md = agg.get("median_delay_us")
                delay.append(md / 1000 if md is not None else None)
                rs = [x for x in node.neighbour_rssi(src) if x]
                rssi.append(statistics.fmean(rs) if rs else None)
            rows.append((src, dst, medium(base, src, dst),
                         mean_sd(deliv), mean_sd(delay), mean_sd(rssi),
                         exact_used))
    return rows


def summarise(runs: list[Run], threshold: float) -> str:
    base = runs[0]
    L = [f"{len(runs)} runs of {base.manifest}, {base.duration_s:g}s each",
         f"  {', '.join(r.name for r in runs)}", ""]

    problems = check_replicates(runs)
    if problems:
        L += ["  NOT REPLICATES -- statistics below mix more than one variable:"]
        L += [f"    {p}" for p in problems] + [""]
    else:
        L += ["  replicate check: initial conditions identical across the set, so"
              " the network is the only variable", ""]

    ct = [convergence_time(r, threshold) for r in runs]
    got = [c for c in ct if c is not None]
    m, sd, n = mean_sd(got)
    L += [f"  convergence to spread < {threshold:g}:"]
    if n:
        L += [f"    {m:.2f} +- {sd:.2f} s   (n={n}"
              + (f", {len(ct)-n} never converged)" if len(ct) - n else ")")]
    else:
        L += ["    never reached within any run"]

    L += ["", f"  {'link':>9} {'med':<4} {'delivery':>16} {'delay ms':>16} "
              f"{'rssi dBm':>14}  n"]
    for src, dst, med, (dm, ds, dn), (lm, ls, ln), (rm, rs, rn), exact in \
            link_table(runs):
        tag = "" if exact else " >="
        L.append(f"  {f'{src}->{dst}':>9} {med:<4} "
                 f"{tag}{dm:>7.4f} +-{ds:>6.4f} "
                 f"{lm:>9.2f} +-{ls:>5.2f} "
                 f"{rm:>8.1f} +-{rs:>4.1f}  {dn}")

    L += ["", "  '>=' marks a link with no exact reception count -- the receiver is",
          "  a relay, so the figure is the sampled-seq LOWER BOUND.",
          "",
          "  The sd is the point: a difference between two configurations is only",
          "  real if it exceeds it."]
    return "\n".join(L)


def plot(runs: list[Run], out: Path, threshold: float, label: str) -> list[Path]:
    made = []
    rows = link_table(runs)

    fig, ax = plt.subplots(1, 2, figsize=(13, 5))
    labels = [f"{s}→{d}" for s, d, *_ in rows]
    cols = [COLOUR[m] for _, _, m, *_ in rows]
    x = range(len(rows))

    ax[0].bar(x, [r[3][0] for r in rows], yerr=[r[3][1] for r in rows],
              color=cols, capsize=3)
    ax[0].set_xticks(list(x)); ax[0].set_xticklabels(labels, rotation=45,
                                                     ha="right", fontsize=7)
    ax[0].set_ylabel("delivery ratio")
    ax[0].set_ylim(0, 1.05)
    ax[0].set_title(f"delivery, mean ± sd over {len(runs)} runs")
    ax[0].grid(alpha=0.25, axis="y")

    ax[1].bar(x, [r[4][0] for r in rows], yerr=[r[4][1] for r in rows],
              color=cols, capsize=3)
    ax[1].set_xticks(list(x)); ax[1].set_xticklabels(labels, rotation=45,
                                                     ha="right", fontsize=7)
    ax[1].set_ylabel("median one-way delay (ms)")
    ax[1].set_title("delay, mean ± sd  (red BLE, blue UDP)")
    ax[1].grid(alpha=0.25, axis="y")

    fig.tight_layout()
    p = out / "links_aggregate.png"; fig.savefig(p, dpi=130); plt.close(fig)
    made.append(p)

    # Every run's spread on one axis: the envelope is what a single run cannot show.
    fig, ax = plt.subplots(figsize=(11, 5))
    for r in runs:
        ts = [i * r.duration_s / 400 for i in range(401)]
        ax.semilogy(ts, [max(r.spread(t), 1e-9) for t in ts], linewidth=0.9,
                    alpha=0.75, label=r.name)
    ax.axhline(threshold, color="#888", linestyle=":", linewidth=1.0)
    ax.set_xlabel("t (s)"); ax.set_ylabel("spread  max−min  (log)")
    ax.set_title(f"{label or runs[0].manifest}: convergence across "
                 f"{len(runs)} runs, identical initial conditions")
    ax.legend(fontsize=7, ncol=2); ax.grid(alpha=0.25, which="both")
    fig.tight_layout()
    p = out / "convergence_aggregate.png"; fig.savefig(p, dpi=130); plt.close(fig)
    made.append(p)
    return made


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dirs", nargs="+", type=Path)
    ap.add_argument("--out", type=Path, default=Path("results"))
    ap.add_argument("--label", default="", help="name for the output directory")
    ap.add_argument("--threshold", type=float, default=0.01,
                    help="spread below which the fleet counts as converged")
    args = ap.parse_args()

    dirs = [d for d in args.run_dirs if d.is_dir()]
    if len(dirs) < 2:
        print(f"need at least two run directories, got {len(dirs)}", file=sys.stderr)
        return 2
    runs = [load_run(d) for d in sorted(dirs)]

    out = args.out / (args.label or f"{runs[0].manifest}-aggregate")
    out.mkdir(parents=True, exist_ok=True)

    text = summarise(runs, args.threshold)
    (out / "summary.txt").write_text(text + "\n", encoding="utf-8")
    print(text)
    print()
    for p in plot(runs, out, args.threshold, args.label) + [out / "summary.txt"]:
        print(f"  wrote {p}")
    return 1 if check_replicates(runs) else 0


if __name__ == "__main__":
    sys.exit(main())
