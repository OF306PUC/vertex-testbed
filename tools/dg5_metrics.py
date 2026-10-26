#!/usr/bin/env python3
"""sec:metrics for the five-DG benchmark, aggregated over paired trials.

Every scalar the document defines, per run, then the median and interquartile
range per arm and a paired interval for each difference between arms.

Paired means paired: the arms of one trial share a run index, so they share
initial conditions and every node's disturbance stream, and the difference is
taken within a trial before being summarised. Summarising each arm first and
subtracting the summaries throws that away.

    python3 tools/dg5_metrics.py                       # runs/dg5-bridge5-0-t*
    python3 tools/dg5_metrics.py 'runs/dg5-mixed-0-t*'
    python3 tools/dg5_metrics.py --baseline LC+        # differences against LC+
"""
from __future__ import annotations

import argparse
import glob
import json
import re
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from vertex.analysis.effort import applied_command
from vertex.analysis.load import load_run                       # noqa: E402

DELTA_X = 0.45
DEFAULT = "runs/dg5-bridge5-0-t*"

#: name -> (label, format). Order is the order reported.
METRICS = [
    ("Ez_f", "E_z final", "9.4f"), ("Ez_m", "E_z max", "9.3f"),
    ("Es_f", "E_sigma final", "9.4f"), ("Es_m", "E_sigma max", "9.3f"),
    ("Ex_f", "E_x final", "9.4f"), ("Ex_m", "E_x max", "9.3f"),
    ("Ec_f", "E_c final", "9.4f"),
    ("T", "T_delta_x [s]", "9.2f"),
    ("J", "J_mu", "9.1f"), ("pk", "mu_pk", "9.3f"), ("TV", "TV_mu", "9.1f"),
    ("J_tr", "J_mu pre-T_c", "9.1f"),
    ("J_ss", "J_mu post-T_c", "9.1f"), ("J_ss_frac", "J_ss / J_mu", "9.3f"),
    ("g_end", "gain at T_end", "9.3f"), ("g_max", "gain max", "9.3f"),
    ("slope", "gain slope", "9.2e"), ("ups", "adaptation updates", "9.0f"),
    ("sat", "saturation [%]", "9.3f"), ("cpu", "CPU/update [us]", "9.1f"),
    ("rh_p99", "realized h p99 [ms]", "9.2f"),
    ("aoi", "mean AoI [ms]", "9.1f"), ("rho", "rho", "9.3f"),
    # docs/METRICS.md
    ("t0", "t_0 window start [s]", "9.2f"),
    ("tband", "T_band [s]", "9.2f"),
    ("tconv", "max_i T_conv,i [s]", "9.2f"),
    ("mse", "max_i MSE(x_i)", "9.3e"),
    ("mse_med", "median_i MSE(x_i)", "9.3e"),
    ("mse_avg", "mean_i MSE(x_i)", "9.3e"),
    ("pcomp", "min_i P_complete", "9.3f"),
]


def entrance(E, t, band) -> float:
    """eq:settling_time: persistent entrance, not first crossing."""
    bad = np.nonzero(E > band)[0]
    if bad.size == 0:
        return float(t[0])
    return float("inf") if bad[-1] + 1 >= len(t) else float(t[bad[-1] + 1])


def one_run(D: Path) -> dict:
    r = load_run(D)
    ids = sorted(r.nodes)
    meta = json.loads((D / f"{ids[0]}.meta.json").read_text())
    mg = meta["controller"]["microgrid"]
    ref = None
    for i in ids:
        v = json.loads((D / f"{i}.meta.json").read_text())
        v = v["controller"]["microgrid"]["virtual"]
        if float(v["b"]) != 0.0:
            ref = np.array([float(v["rP"]), float(v["rQ"])])
    if ref is None:
        raise SystemExit(f"{D}: no pinned node, so X_Sigma^d is not defined")
    T_c = float(mg.get("T_c") or 40.0)

    d = {i: r.nodes[i].data for i in ids}
    L = min(len(d[i]["timestamp"]) for i in ids)
    t = np.asarray(d[ids[0]]["timestamp"], float)[:L]
    h = float(np.median(np.diff(t)))
    col = lambda i, k: np.asarray(d[i][k], float)[:L]
    x = {i: np.stack([col(i, "state_P"), col(i, "state_Q")], 1) for i in ids}
    z = {i: np.stack([col(i, "vstate_P"), col(i, "vstate_Q")], 1) for i in ids}
    n1 = lambda a: np.abs(a).sum(1)

    # Every node, not just the first. A `mixed` run has relay nodes whose
    # column set is strictly smaller -- no `updates`, no `saturated`, no
    # `cpu_us`, no `realized_h` -- and checking ids[0], a Pi-side node, said
    # the whole fleet had them. A fleet-wide quantity is reported only when
    # every node reports it; summing the subset that happens to log it would
    # put a 3-node total in a column labelled for 5.
    have = lambda k: all(k in d[i] for i in ids)
    out = {}
    Ez = np.max([n1(z[i] - ref) for i in ids], 0)
    Es = np.max([n1(x[i] - z[i]) for i in ids], 0)
    Ex = np.max([n1(x[i] - ref) for i in ids], 0)
    Ec = np.max([n1(x[i] - x[j]) for i in ids for j in ids if j > i], 0)
    out |= dict(Ez_f=Ez[-1], Ez_m=Ez.max(), Es_f=Es[-1], Es_m=Es.max(),
                Ex_f=Ex[-1], Ex_m=Ex.max(), Ec_f=Ec[-1],
                T=entrance(Ex, t, DELTA_X), T_c=T_c)

    # The applied command, taken from the log where a node reports it and
    # rebuilt from that node's own states where it does not -- see
    # vertex/analysis/effort.py, which reproduces the logged command to 7e-14
    # on every node of every run that carries both. A relayed node drops the
    # command before the host sees it, which made the whole heterogeneous
    # campaign report NaN for effort; it was never missing data, only
    # unrecorded, and a partial sum over the three nodes that happened to log
    # it would have been worse than either.
    mu = {i: applied_command(D, i) for i in ids}
    out["J"] = h * sum(float((mu[i] ** 2).sum()) for i in ids)
    out["pk"] = max(float(np.abs(mu[i]).max()) for i in ids)
    out["TV"] = sum(float(np.abs(np.diff(mu[i], axis=0)).sum()) for i in ids)

    # Effort spent AFTER the prescribed deadline. By T_c the fleet is already
    # at the reference, so nothing past it is spent reaching the band: under
    # the persistent profile it is spent rejecting a disturbance that does not
    # vanish, and under the replication profile there is nothing left to
    # reject. That makes this the part of the effort attributable to the
    # persistent uncertainty, which total J_mu cannot show because the
    # transient dominates it.
    #
    # The window opens at T_c, the declared deadline, and not at the measured
    # t_0: t_0 differs per arm, which would price each arm's maintenance over
    # a different interval, and it is infinite for LC0 -- the one arm whose
    # terminal policy this quantity is meant to expose, since zero input past
    # T_c costs nothing and is exactly why it drifts.
    #
    # Built per node from that node's own clock: `applied_command` returns one
    # command per interval, so it is one sample shorter than the row count,
    # and the two Pi-side nodes of a mixed run carry a different sample count
    # from the three relayed ones.
    # Split at T_c, so the two halves partition J_mu exactly: what it cost to
    # REACH the band, and what it costs to HOLD it against a disturbance that
    # does not stop. Reporting only the total hides which of the two an arm
    # spends its effort on, and that is the whole difference between them.
    J_tr = J_ss = 0.0
    for i in ids:
        ti = np.asarray(d[i]["timestamp"], float)[:len(mu[i])]
        J_tr += h * float((mu[i][ti < T_c] ** 2).sum())
        J_ss += h * float((mu[i][ti >= T_c] ** 2).sum())
    out["J_tr"] = J_tr
    out["J_ss"] = J_ss
    out["J_ss_frac"] = J_ss / out["J"] if out["J"] else float("nan")

    gk = "vartheta" if have("vartheta") else "S1"
    post = t >= T_c + 10.0
    tt = t[post] - t[post].mean()
    if not have(gk):
        out |= dict(gk="--", g_end=np.nan, g_max=np.nan, slope=np.nan)
        gains = []
    else:
        gains = [col(i, gk) for i in ids]
        out["gk"] = gk
    if gains:
        out["g_end"] = float(np.mean([g[-1] for g in gains]))
        out["g_max"] = float(np.max([g.max() for g in gains]))
        out["slope"] = float(np.mean([np.sum(tt * (g[post] - g[post].mean()))
                                      / np.sum(tt ** 2) for g in gains]))
    # `vartheta` is the adaptive gain of the AA/DZ interfaces; `S1` is the LC
    # interfaces' parameter estimate. Different quantities, different units,
    # different targets -- and the single "max. adaptive" row this replaces
    # put them side by side as if one could be read against the other.
    out["g_max_vth"] = out["g_max"] if out.get("gk") == "vartheta" else np.nan
    out["ups"] = (float(sum(col(i, "updates")[-1] for i in ids))
                  if have("updates") else np.nan)
    for key, src, f in (("sat", "saturated", lambda a: 100 * a.mean()),
                        ("cpu", "cpu_us", np.median),
                        ("rh_p99", "realized_h",
                         lambda a: 1e3 * np.percentile(a, 99))):
        out[key] = (float(np.mean([f(col(i, src)) for i in ids]))
                    if have(src) else np.nan)

    aoi, rho = [], []
    span = max(1, int(round(0.125 / h)))
    for i in ids:
        for j in r.nodes[i].neighbours:
            fr = col(i, f"rx_{j}") > 0
            idx = np.arange(L)
            last = np.maximum.accumulate(np.where(fr, idx, -1))
            seen = last >= 0
            aoi.append(float(np.mean((idx[seen] - last[seen]) * h)))
            w = fr[:L // span * span].reshape(-1, span).any(1)
            rho.append(float(w.mean()))
    out["aoi"] = float(np.mean(aoi)) * 1e3
    out["rho"] = float(np.mean(rho))

    # ---- docs/METRICS.md -------------------------------------------------
    #
    # The steady-state window is t_0 = max(T_band, max_i T_conv,i), and both
    # conditions are required: an agent tracking its own virtual state says
    # nothing about whether that state is near the target yet, and opening on
    # the physical condition alone integrates the whole transient, which
    # measures convergence speed rather than residual accuracy.
    #
    # METRICS.md calls the first of those `T_c`. That name is taken here --
    # the benchmark's prescribed deadline is also T_c, and it is 40 s by
    # declaration rather than by measurement -- so it is `T_band` below.
    # Conflating them would put a measured quantity in a column a reader
    # expects to be a setting.
    spread = np.max([n1(z[i] - z[j]) for i in ids for j in ids if j > i], axis=0)
    out["tband"] = entrance(spread, t, DELTA_X)
    # T_conv,i: the LAST instant x_i is outside the band about z_i, plus one
    # step. A settling time, not a first crossing, so a brief re-excursion
    # cannot let an agent count as converged before it stayed converged.
    tconv = [entrance(n1(x[i] - z[i]), t, DELTA_X) for i in ids]
    out["tconv"] = float(max(tconv))
    t0 = max(out["tband"], out["tconv"])
    out["t0"] = t0

    if not np.isfinite(t0) or t0 >= float(t[-1]):
        # No steady state inside the run: an MSE over an empty window would
        # be a number, and a misleading one.
        out |= dict(mse=np.nan, mse_med=np.nan, mse_avg=np.nan)
    else:
        w = t >= t0
        # Against the reference, which is what the sharing objective asks the
        # fleet to reach. METRICS.md measures against the consensus average
        # because its experiment has no exogenous target; this one does.
        per_agent = [float(np.mean(np.sum((x[i][w] - ref) ** 2, axis=1)))
                     for i in ids]
        out["mse"] = float(max(per_agent))
        out["mse_med"] = float(np.median(per_agent))
        # Averaged over the agents, so that under --mean the reported cell is
        # the mean over agents AND over realizations: the typical residual of
        # a typical agent. The max is the worst agent of a typical run, which
        # is a bound and answers a different question; both are reported
        # because a max far above this mean means one agent carries the error.
        out["mse_avg"] = float(np.mean(per_agent))

    # P_complete: the fraction of publication windows in which EVERY declared
    # neighbour delivered. Counted jointly rather than derived from rho, so
    # rho^d stays a prediction to test instead of a restatement.
    comp = []
    for i in ids:
        nb = list(r.nodes[i].neighbours)
        if not nb:
            continue
        fresh = [col(i, f"rx_{j}")[:L // span * span].reshape(-1, span).any(1)
                 for j in nb]
        comp.append(float(np.all(np.vstack(fresh), axis=0).mean()))
    out["pcomp"] = float(min(comp)) if comp else np.nan
    return out


def paired_interval(diffs: np.ndarray, conf: float = 0.95):
    """Paired t interval on the within-trial differences.

    A t interval and not a rank test, for a reason worth stating: the
    two-sided Wilcoxon signed-rank statistic on n paired trials cannot
    return p below 2/2^n, so at n = 5 the smallest attainable p is 0.0625
    and no nonparametric claim at the 5% level is possible however large
    the effect. The interval below is parametric; with n this small its
    width is doing most of the talking, which is the honest state of it.
    """
    d = diffs[np.isfinite(diffs)]
    n = d.size
    if n < 2:
        return float(d.mean()) if n else np.nan, np.nan, np.nan, n
    # 97.5th percentile of t, small-n table; falls back to the normal value.
    T = {2: 12.706, 3: 4.303, 4: 3.182, 5: 2.776, 6: 2.571, 7: 2.447,
         8: 2.365, 9: 2.306, 10: 2.262}.get(n, 1.96)
    m, se = float(d.mean()), float(d.std(ddof=1) / np.sqrt(n))
    return m, m - T * se, m + T * se, n



#: The arm order a table uses, whichever subset of them a campaign has.
ARM_ORDER = ["LC0", "LC+", "AA", "DZ"]

#: How each arm is named in the paper. The run directories carry the short
#: tags; a table that prints those makes the reader translate.
ARM_TEX = {"LC0": r"$\mathcal{C}_\mathrm{LC}$",
           "LC+": r"$\mathcal{C}_\mathrm{LC+}$",
           "AA": r"$\mathcal{C}_\mathrm{AA}$",
           "DZ": r"$\mathcal{C}_\mathrm{DZ}$"}


def _head(arms):
    return "metric & " + " & ".join(ARM_TEX.get(a, a) for a in arms) + r" \\"


#: Set by --no-iqr. The cells then carry the median alone.
_NO_IQR = False


#: Report the MEAN of each metric over the trials instead of the median, as a
#: single number. No bracket comes with it: an interquartile range describes a
#: median, and pairing one with a mean would label a dispersion that does not
#: belong to the reported centre. A run whose settling time is infinite makes
#: the mean infinite, which is the honest propagation -- the median would hide
#: one diverging trial among nine that converged, and here it must not.
_MEAN = False

#: With a caption, the tool writes the whole float rather than a bare tabular.
#: The sentence naming the statistic is appended by the tool, not typed into
#: the caption: which statistic the cells carry is a property of how they were
#: computed, and a caption maintained by hand beside a regenerated table is the
#: one place the two can disagree without anyone noticing.
_CAPTION = None
_LABEL = "tab:dg5"


def _centre(v):
    """(value, bracket) for one metric's values over the trials."""
    if _MEAN:
        return float(np.mean(v)), None
    fin = v[np.isfinite(v)]
    return (float(np.median(v)),
            None if (_NO_IQR or not fin.size) else np.percentile(fin, [25, 75]))


def _summary_cells(data, arm, trials, key, fmt):
    """One cell: median over trials, with the interquartile range."""
    if arm not in data:
        return "--"
    v = np.array([data[arm][t].get(key, np.nan) for t in trials
                  if t in data[arm]], float)
    v = v[np.isfinite(v) | np.isinf(v)]
    if v.size == 0:
        return "--"
    if v.size == 1:
        return _cell(v[0], fmt)
    mid, iqr = _centre(v)
    return _cell(mid, fmt, iqr)


def write_grouped(outdir: Path, groups) -> None:
    """One summary over several campaigns, each campaign a block of rows.

    `groups` is `[(label, data, trials), ...]`. The columns are the union of
    the arms, in `ARM_ORDER`, so a campaign that lacks one leaves a column of
    dashes under its own label rather than changing the table's shape --- the
    comparison across blocks is the point, and it only reads if the columns
    line up.

    Rows grouped by campaign rather than columns by arm: the metric set is
    seven deep and the arms are four wide, so blocking the rows keeps the
    table inside one column of a two-column page, where crossing arm with
    profile would need eight columns and a landscape float.
    """
    outdir.mkdir(parents=True, exist_ok=True)
    arms = [a for a in ARM_ORDER
            if any(a in d for _, d, _ in groups)]
    L = [r"% generated by tools/dg5_metrics.py -- do not edit",
         r"\begin{tabular}{l" + "r" * len(arms) + "}", r"\toprule",
         _head(arms)]
    for n, (label, data, trials) in enumerate(groups):
        L.append(r"\midrule" if n == 0 else r"\addlinespace\midrule")
        L.append(r"\multicolumn{" + str(len(arms) + 1)
                 + r"}{l}{\itshape " + label + r"} \\")
        for key in SUMMARY:
            tex_label, fmt = TEX[key]
            cells = [_summary_cells(data, a, trials, key, fmt) for a in arms]
            if any(c != "--" for c in cells):
                L.append(f"{tex_label} & " + " & ".join(cells) + r" \\")
    L += [r"\bottomrule", r"\end{tabular}"]
    stat = (f"mean over {len(groups[0][2])} paired trials" if _MEAN
            else f"median over {len(groups[0][2])} paired trials")
    (outdir / "dg5_summary.tex").write_text(_wrap(L, stat) + "\n")
    print(f"  wrote {outdir / 'dg5_summary.tex'}  "
          f"({len(groups)} group(s), arms {', '.join(arms)})")
    for label, data, _ in groups:
        gaps = [a for a in arms if a not in data]
        if gaps:
            print(f"    {label}: no runs for {', '.join(gaps)}; column dashed")


def load_campaign(pattern: str, want=None):
    """`(data, trials)` for one glob of run directories."""
    runs = {}
    for p in sorted(glob.glob(pattern)):
        d = Path(p)
        m = re.search(r"-t(\d+)-(.+)$", d.name)
        if d.is_dir() and m:
            runs.setdefault(m.group(2).replace("plus", "+"), {})[int(m.group(1))] = d
    if want:
        runs = {k: v for k, v in runs.items() if k in want}
    data = {arm: {tr: one_run(d) for tr, d in sorted(ts.items())}
            for arm, ts in runs.items()}
    trials = sorted(set().union(*(set(v) for v in data.values()))) if data else []
    return data, trials


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("pattern", nargs="?", default=DEFAULT)
    ap.add_argument("--arms", default=None, metavar="A,B,...",
                    help="report only these arms, in this order; the rest "
                         "are read and dropped")
    ap.add_argument("--baseline", default="DZ",
                    help="arm the differences are taken against")
    ap.add_argument("--group", action="append", default=None,
                    metavar="LABEL=GLOB",
                    help="repeatable: write ONE summary covering several "
                         "campaigns, each as a block of rows. Requires "
                         "--latex and ignores the positional pattern.")
    ap.add_argument("--metrics", default=None, metavar="K,K,...",
                    help="override SUMMARY: the metric keys to tabulate, in "
                         "this order. A shorter table for a section that does "
                         "not argue from every row.")
    ap.add_argument("--caption", default=None, metavar="TEXT",
                    help="write a complete table float with this caption "
                         "instead of a bare tabular. The sentence naming the "
                         "statistic is appended automatically.")
    ap.add_argument("--label", default="tab:dg5", metavar="KEY",
                    help="label of the generated float; needs --caption")
    ap.add_argument("--mean", action="store_true",
                    help="report the mean over trials, as a single number, "
                         "instead of the median with [q1, q3]")
    ap.add_argument("--no-iqr", action="store_true",
                    help="medians only, no [q1, q3]. Narrow enough for one "
                         "column, at the cost of the dispersion.")
    ap.add_argument("--latex", metavar="DIR", default=None,
                    help="also write booktabs tables into DIR: the summary "
                         "sec:reporting item 6 asks for, and the paired "
                         "differences")
    a = ap.parse_args()

    if a.no_iqr:
        global _NO_IQR
        _NO_IQR = True
    if a.mean:
        global _MEAN
        _MEAN = True
    if a.caption:
        global _CAPTION, _LABEL
        _CAPTION, _LABEL = a.caption, a.label

    if a.metrics:
        global SUMMARY
        want = [k.strip() for k in a.metrics.split(",")]
        bad = [k for k in want if k not in TEX]
        if bad:
            print(f"unknown metric key(s) {bad}; have {sorted(TEX)}",
                  file=sys.stderr)
            return 2
        SUMMARY = want

    if a.group:
        if not a.latex:
            print("--group needs --latex DIR", file=sys.stderr)
            return 2
        want = [x.strip() for x in a.arms.split(",")] if a.arms else None
        groups = []
        for spec in a.group:
            if "=" not in spec:
                print(f"--group wants LABEL=GLOB, got {spec!r}", file=sys.stderr)
                return 2
            label, pattern = spec.split("=", 1)
            d, tr = load_campaign(pattern, want)
            if not d:
                print(f"no runs matched {pattern!r}", file=sys.stderr)
                return 2
            print(f"  {label}: {len(tr)} trial(s), arms {', '.join(d)}")
            groups.append((label, d, tr))
        print()
        write_grouped(Path(a.latex), groups)
        return 0

    runs = {}
    for p in sorted(glob.glob(a.pattern)):
        d = Path(p)
        m = re.search(r"-t(\d+)-(.+)$", d.name)
        if not (d.is_dir() and m):
            continue
        runs.setdefault(m.group(2).replace("plus", "+"), {})[int(m.group(1))] = d
    if not runs:
        print(f"no runs matched {a.pattern!r}; expected <base>-t<N>-<ARM>/",
              file=sys.stderr)
        return 2

    want = [x.strip() for x in a.arms.split(",")] if a.arms else None
    if want:
        missing = [x for x in want if x not in runs]
        if missing:
            print(f"no runs for {missing}; have {sorted(runs)}", file=sys.stderr)
            return 2
        runs = {k: runs[k] for k in want}
    data = {arm: {tr: one_run(d) for tr, d in sorted(ts.items())}
            for arm, ts in runs.items()}
    arms = list(data)
    trials = sorted(set().union(*(set(v) for v in data.values())))
    print(f"  {len(arms)} arm(s) x {len(trials)} trial(s): "
          f"{', '.join(arms)}   trials {trials}\n")

    W = 22
    print(f"  {'metric':<22}" + "".join(f"{arm:>{W}}" for arm in arms))
    print("  " + "-" * (22 + W * len(arms)))
    for key, label, fmt in METRICS:
        cells = []
        for arm in arms:
            v = np.array([data[arm][tr].get(key, np.nan) for tr in trials
                          if tr in data[arm]], float)
            v = v[np.isfinite(v)]
            if v.size == 0:
                cells.append(f"{'--':>{W}}")
            elif v.size == 1:
                cells.append(f"{format(v[0], fmt):>{W}}")
            else:
                q1, q3 = np.percentile(v, [25, 75])
                cells.append(f"{format(np.median(v), fmt)}"
                             f" [{format(q1, fmt).strip()},"
                             f"{format(q3, fmt).strip()}]".rjust(W))
        print(f"  {label:<22}" + "".join(cells))
    print("\n  median [q1, q3] over trials" if len(trials) > 1
          else "\n  single trial: no median or IQR, and no interval below")

    if len(trials) < 2 or a.baseline not in data:
        if a.latex:
            write_latex(Path(a.latex), data, arms, trials, a.baseline)
        return 0
    print(f"\n  paired differences against {a.baseline}, 95% t interval "
          f"on the within-trial difference\n")
    print(f"  {'metric':<22}{'arm':<6}{'mean diff':>12}{'95% CI':>26}{'n':>4}")
    print("  " + "-" * 68)
    for key, label, fmt in METRICS:
        for arm in arms:
            if arm == a.baseline:
                continue
            dif = np.array([data[arm][tr].get(key, np.nan)
                            - data[a.baseline][tr].get(key, np.nan)
                            for tr in trials
                            if tr in data[arm] and tr in data[a.baseline]],
                           float)
            m, lo, hi, n = paired_interval(dif)
            if not np.isfinite(m):
                continue
            ci = ("--" if not np.isfinite(lo)
                  else f"[{lo:+.3g}, {hi:+.3g}]")
            star = "" if (not np.isfinite(lo) or lo <= 0 <= hi) else "  *"
            print(f"  {label:<22}{arm:<6}{m:>+12.4g}{ci:>26}{n:>4}{star}")
    print("\n  * the interval excludes zero")
    if a.latex:
        print()
        write_latex(Path(a.latex), data, arms, trials, a.baseline)
    return 0


#: sec:reporting item 6, in its order. A subset of METRICS, because the
#: summary table is what a reader checks and the full sweep is what an
#: author checks.
#: Control effort is in it because `vertex.analysis.effort` rebuilds the
#: applied command on the nodes that do not report it. Update counts,
#: saturation and computation time are left out as author's diagnostics;
#: delivery and completeness are implementation conditions and belong with
#: the platform table, not among the control metrics.
#: `Ec_f` is out: measured over the 40 runs of the persistent campaign it
#: correlates with `Ex_f` at r = +1.0000, ratio 0.77, so it is the same row
#: twice. `pk` is out: it is 10.000 in every run of every arm, the saturation
#: limit, so it has no variance to report -- but it belongs in the caption,
#: because it is why `TV` cannot separate the arms. `TV` is in despite
#: separating nothing: at r = -0.02 against `J` it is an independent axis, and
#: an interval that contains zero is a result rather than a reason to omit.
SUMMARY = ["T", "Ex_f", "mse", "mse_avg",
           "J", "J_tr", "J_ss", "TV", "pk"]

TEX = {"T": (r"$T_{\delta_x}$ [s]", "{:.2f}"),
       "Ex_f": (r"$E_x(T_{\mathrm{end}})$", "{:.4f}"),
       "J": (r"$J_\mu$", "{:.1f}"),
       "J_tr": (r"$J_\mu(t< T_c)$", "{:.1f}"),
       "J_ss": (r"$J_\mu(t\geq T_c)$", "{:.2f}"),
       "J_ss_frac": (r"$J_\mu(t\geq T_c)/J_\mu$", "{:.3f}"),
       "TV": (r"$\operatorname{TV}_\mu$", "{:.1f}"),
       "g_max": ("max.\\ adaptive", "{:.3f}"),
       "g_max_vth": (r"$\max_i \vartheta_i$", "{:.3f}"),
       "g_end": ("final adaptive", "{:.3f}"),
       "ups": ("updates", "{:.0f}"),
       "sat": (r"saturation [\%]", "{:.3f}"),
       "cpu": (r"CPU/update [$\mu$s]", "{:.1f}"),
       "Ez_f": (r"$E_z(T_{\mathrm{end}})$", "{:.4f}"),
       "Es_f": (r"$E_\sigma(T_{\mathrm{end}})$", "{:.4f}"),
       "Ec_f": (r"$E_c(T_{\mathrm{end}})$", "{:.4f}"),
       "slope": (r"$s_\vartheta$ [1/s]", "{:.2e}"),
       "pk": (r"$\mu_{\mathrm{pk}}$", "{:.3f}"),
       "rho": (r"$\rho$", "{:.3f}"),
       "aoi": ("mean AoI [ms]", "{:.1f}"),
       "mse": (r"$\max_i \mathrm{MSE}(x_i)$", "{:.2e}"),
       "mse_med": (r"$\mathrm{median}_i\,\mathrm{MSE}(x_i)$", "{:.2e}"),
       "mse_avg": (r"$\mathrm{mean}_i\,\mathrm{MSE}(x_i)$", "{:.2e}"),
       "t0": (r"$t_0$ [s]", "{:.2f}"),
       "tband": (r"$T_{\mathrm{band}}$ [s]", "{:.2f}"),
       "tconv": (r"$\max_i T_{\mathrm{conv},i}$ [s]", "{:.2f}"),
       "pcomp": (r"$\min_i P_{\mathrm{complete}}$", "{:.3f}")}


def _cell(v, fmt, iqr=None):
    # Infinity is tested before finiteness, because the two mean different
    # things here: a settling time that never arrives is a result, not a gap
    # in the record. `entrance` returns inf when the band is left and not
    # re-entered, and the table must show that rather than a dash.
    if v == float("inf"):
        return r"$\infty$"
    if not np.isfinite(v):
        return "--"
    out = fmt.format(v)
    if iqr is not None and np.isfinite(iqr[0]):
        out += r" \tiny{[" + fmt.format(iqr[0]) + ", " + fmt.format(iqr[1]) + "]}"
    return out


def _wrap(L: list[str], stat: str) -> str:
    """The tabular, inside a float with its caption, when one was asked for."""
    head, body = L[0], L[1:]
    if not _CAPTION:
        return "\n".join([head] + body)
    cap = _CAPTION.rstrip()
    if not cap.endswith("."):
        cap += "."
    cap += f" Every cell is the {stat}."
    return "\n".join([head,
                      r"\begin{table}[t]", r"  \centering",
                      "  \\caption{" + cap + "}",
                      "  \\label{" + _LABEL + "}",
                      r"  \footnotesize", r"  \setlength{\tabcolsep}{5pt}"]
                     + ["  " + x for x in body]
                     + [r"\end{table}"])


def write_latex(outdir: Path, data, arms, trials, baseline) -> None:
    """Two tables: the summary, and the paired differences.

    Written rather than pasted from the terminal, because a number
    transcribed by hand is a number that can disagree with the run that
    produced it, and every figure in this set is regenerated from the runs
    on every change.
    """
    outdir.mkdir(parents=True, exist_ok=True)
    n = len(trials)
    plural = "trials" if n != 1 else "trial"
    stat = (f"mean over {n} paired {plural}" if _MEAN else
            (f"median over {n} paired {plural}" if n > 1 else "single trial"))
    L = [r"% generated by tools/dg5_metrics.py -- do not edit"]
    # Without a caption the statistic has nowhere else to go, so it is stated
    # in a comment; with one, the caption already says it.
    L += [] if _CAPTION else [f"% cells are the {stat}"]
    L += [
         r"\begin{tabular}{l" + "r" * len(arms) + "}", r"\toprule",
         _head(arms), r"\midrule"]
    dropped = []
    for key in SUMMARY:
        label, fmt = TEX[key]
        cells = []
        for arm in arms:
            v = np.array([data[arm][t].get(key, np.nan) for t in trials
                          if t in data[arm]], float)
            v = v[np.isfinite(v) | np.isinf(v)]
            if v.size == 0:
                cells.append("--")
            elif v.size == 1:
                cells.append(_cell(v[0], fmt))
            else:
                mid, iqr = _centre(v)
                cells.append(_cell(mid, fmt, iqr))
        # A row no arm could supply is left out rather than printed as a
        # line of dashes. The heterogeneous campaign cannot measure control
        # effort at all -- two of its five nodes relay a law running on a
        # separate processor and report states, not the applied command --
        # and a table should not spend a row saying nothing four times.
        if any(c != "--" for c in cells):
            L.append(f"{label} & " + " & ".join(cells) + r" \\")
        else:
            dropped.append(label)
    L += [r"\bottomrule", r"\end{tabular}"]
    (outdir / "dg5_summary.tex").write_text(_wrap(L, stat) + "\n")
    print(f"  wrote {outdir / 'dg5_summary.tex'}  ({stat})"
          + ("  [with caption]" if _CAPTION else ""))
    if dropped:
        print(f"    no arm reports {', '.join(dropped)}; row(s) omitted")

    if n < 2 or baseline not in data:
        print("  no difference table: it needs at least two paired trials")
        return
    L = [r"% generated by tools/dg5_metrics.py -- do not edit",
         r"\begin{tabular}{llrr}", r"\toprule",
         r"metric & arm & mean diff. & 95\% CI \\", r"\midrule"]
    for key in SUMMARY:
        label, fmt = TEX[key]
        for arm in arms:
            if arm == baseline:
                continue
            dif = np.array([data[arm][t].get(key, np.nan)
                            - data[baseline][t].get(key, np.nan)
                            for t in trials
                            if t in data[arm] and t in data[baseline]], float)
            m, lo, hi, k = paired_interval(dif)
            if not np.isfinite(m):
                continue
            ci = "--" if not np.isfinite(lo) else f"$[{lo:+.3g}, {hi:+.3g}]$"
            mark = "" if (not np.isfinite(lo) or lo <= 0 <= hi) else r"$^{*}$"
            L.append(f"{label} & {arm} & ${m:+.4g}${mark} & {ci} " + r"\\")
    L += [r"\bottomrule", r"\end{tabular}"]
    (outdir / "dg5_paired.tex").write_text("\n".join(L) + "\n")
    print(f"  wrote {outdir / 'dg5_paired.tex'}  "
          f"(paired within trial, n = {n}; $^*$ excludes zero)")


if __name__ == "__main__":
    sys.exit(main())
