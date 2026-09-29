#!/usr/bin/env python3
"""Per-agent tables for the paper, one row per agent, mean +- sd across runs.

    python3 tools/per_agent_table.py results/table/regimeB.json [--arm ring4-40hz]
    python3 tools/per_agent_table.py results/table/regimeB.json --latex > tab.tex

Every entry is a statistic over the repeated trials of one arm, so the sd is
run-to-run variability for that agent, not variability across agents. That is
the quantity a reader needs to judge whether a difference between two agents is
real: initial conditions and disturbance streams are identical across trials,
so everything the sd captures comes from the network.

  MSE        mean (x_i - zbar)^2 over the steady-state window
  T_conv     settling of x_i onto its own z_i, in seconds
  p          per-link delivery, averaged over the agent's incoming links
  P_comp     fraction of publication intervals with ALL d_i neighbours fresh

MSE is near-identical down each column, because zbar is a single fleet-wide
value that every agent converges to. It is tabulated anyway, by request, and
also summarised per arm after the tables; read it as an arm-level quantity that
happens to be printed on every row, not as a per-agent property.

The probabilities and the discrepancy are tabulated in units of 1e-3, declared
in the column header, so each entry is a three-digit integer with a two-digit
dispersion rather than a leading "0." and four decimals on every row.

The degree is carried on every table, including the regular graphs where it
repeats one value. It is what makes P_comp readable: the same p_i gives a very
different P_comp at d = 1 and d = 4, and the reader needs both numbers in view
to see that.
"""
from __future__ import annotations

import argparse
import json
import math
import statistics as st
import sys
from collections import defaultdict
from pathlib import Path

# (header, cache key, unit shown in the header, multiplier, format, width)
COLS = [("MSE", "mse", "1e-3", 1e3, "{:.0f}", 13),
        ("T_conv", "t_conv", "s", 1.0, "{:.1f}", 15),
        ("p", "rho", "1e-3", 1e3, "{:.0f}", 13),
        ("P_comp", "p_complete", "1e-3", 1e3, "{:.0f}", 13)]

# `p_i` here is the per-link delivery probability: the fraction of publication
# intervals in which that neighbour's value arrived, averaged over the agent's
# incoming links. NOTE this is not the same p as in docs/METRICS.md, which uses
# p for the per-ADVERTISING-EVENT capture probability (0.663) and rho for this
# one (0.743). The two differ by k = T_pub/T_adv. Whichever symbol the paper
# adopts, it has to be used consistently in both places.
HEAD = {"MSE": r"$\mathrm{MSE}(x_i)$", "T_conv": r"$T_{\mathrm{conv},i}$",
        "p": r"$p_i$", "P_comp": r"$P_{\mathrm{comp},i}$"}
UNIT_TEX = {"s": "s", "ms": "ms", "1e-3": r"\times 10^{-3}"}

#: Printed once per block instead of repeated on all thirty rows.
MEDIUM_LABEL = {"ble": "BLE agents", "wifi": "Wi-Fi agents",
                "bridge": "Bridge agents"}


def clean(v):
    return [x for x in v if x is not None
            and not (isinstance(x, float) and math.isnan(x))]


def cell(v, fmt, sep=" +- ", no_sd=False):
    """mean +- sd, or a dash when the metric is missing for that agent."""
    if not v:
        return "--"
    m = st.mean(v)
    if no_sd:
        return fmt.format(m)
    s = st.pstdev(v) if len(v) > 1 else 0.0
    return fmt.format(m) + sep + fmt.format(s)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cache", type=Path)
    ap.add_argument("--arm", action="append", default=[])
    ap.add_argument("--latex", action="store_true")
    ap.add_argument("--no-sd", action="store_true",
                    help="report the mean alone, without the standard deviation")
    a = ap.parse_args()

    D = json.loads(a.cache.read_text())["metrics"]
    agg = defaultdict(lambda: defaultdict(list))
    meta = {}
    for name, per in D.items():
        arm = name.rsplit("-c", 1)[0].replace("n30-", "")
        if a.arm and arm not in a.arm:
            continue
        for nid, rec in per.items():
            key = (arm, int(nid))
            meta[key] = (rec.get("kind"), rec.get("deg"))
            for _, k, _, mul, _, _ in COLS:
                if rec.get(k) is not None:
                    agg[key][k].append(rec[k] * mul)

    for arm in sorted({k[0] for k in agg}):
        rows = sorted(k for k in agg if k[0] == arm)
        n_runs = max(len(agg[k].get("rho", [])) for k in rows)
        show_deg = True        # requested on every table, regular or not
        ncol = 1 + int(show_deg) + len(COLS)

        if a.latex:
            print(f"% {arm}, mean $\\pm$ sd over {n_runs} trials")
            print("\\begin{tabular}{" + "r" * ncol + "}")
            print("\\toprule")
            head = ["agent"] + (["$d$"] if show_deg else [])
            for name_, _, unit, _, _, _ in COLS:
                head.append(f"{HEAD[name_]} [{UNIT_TEX[unit]}]")
            print(" & ".join(head) + r" \\")
            print("\\midrule")
            last = None
            for key in rows:
                kind, deg = meta[key]
                if kind != last:
                    if last is not None:
                        print("\\midrule")
                    print(f"\\multicolumn{{{ncol}}}{{l}}"
                          f"{{\\textit{{{MEDIUM_LABEL.get(kind, kind)}}}}} \\\\")
                    last = kind
                cells = [str(key[1])] + ([str(deg)] if show_deg else [])
                for _, k, _, _, fmt, _ in COLS:
                    cells.append(cell(clean(agg[key][k]), fmt,
                                      sep=r" $\pm$ ", no_sd=a.no_sd))
                print(" & ".join(cells) + r" \\")
            print("\\bottomrule\n\\end{tabular}\n")
            continue

        print("=" * 78)
        print(f"{arm}   mean +- sd over {n_runs} trials")
        print("=" * 78)
        hdr = f"{'agent':>5}" + (f"{'d':>3}" if show_deg else "")
        for name_, _, unit, _, _, w in COLS:
            hdr += f"{name_ + ' [' + unit + ']':>{w}}"
        print(hdr)
        last = None
        for key in rows:
            kind, deg = meta[key]
            if kind != last:
                print(f"  -- {MEDIUM_LABEL.get(kind, kind)} " + "-" * 50)
                last = kind
            line = f"{key[1]:>5}" + (f"{deg:>3}" if show_deg else "")
            for _, k, _, _, fmt, w in COLS:
                line += f"{cell(clean(agg[key][k]), fmt, no_sd=a.no_sd):>{w}}"
            print(line)
        print()

    if not a.latex:
        print("=" * 62)
        print("arm-level MSE(x_i - zbar) [1e-3], mean +- sd over trials")
        print("  (near-uniform across agents: zbar is one fleet-wide value)")
        print("=" * 62)
        for arm in sorted({k[0] for k in agg}):
            v = [x for key in agg if key[0] == arm
                 for x in clean(agg[key].get("mse", []))]
            if v:
                print(f"  {arm:18}{st.mean(v):9.4f} +- {st.pstdev(v):<9.4f}"
                      f"n={len(v)} agent-runs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
