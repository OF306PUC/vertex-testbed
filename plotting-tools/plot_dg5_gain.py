"""sec:reporting items 3 and 4: the adaptive gain, and what it does to the input.

    arms_theta.pdf   vartheta_i per AGENT for AA and DZ, with T_c marked
    arms_mu.pdf      the APPLIED command per agent, same pair
    arms_tv.pdf      cumulative TV of the applied command (plot_dg5_arms.py)

Item 4 poses a question rather than asking for a picture: does the gain drift
actually change the clipped control action? AA's gain ramps without bound and
DZ's stops, so if the answer were yes these two figures would separate where
the gain figure does. Drawn as a pair on purpose -- the claim is about a
difference, and a difference is read from coincidence or its absence.

Colour is the AGENT, style is the ARM, as on the interface comparison. T_c is
drawn here and nowhere else in this figure set: item 3 asks for it by name,
and it is the instant the two arms would diverge if they were going to.

The applied command is logged only where the law runs on its own host, so
these are `bridge5` figures. A relayed node reports four states and the
interface state, not mu.

    python3 plotting-tools/plot_dg5_gain.py AA_RUN DZ_RUN
"""
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "plotting-tools"))

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

import utils_mpl
import dg5_paths
from palette import MEDIA_ORDER, shades_compact
from vertex.analysis.effort import applied_command
from vertex.analysis.load import load_run

DEFAULT = {"AA": ROOT / "runs/per-bridge5/dg5-bridge5-0-t0-AA",
           "DZ": ROOT / "runs/per-bridge5/dg5-bridge5-0-t0-DZ"}
FIG_W, FIG_H, FIG_DPI = 3.5, 2.5, 300
LW = 1.0
STYLE = {"DZ": "solid"}
DOTTED = (0, (1.4, 1.4))
MARK_GREY = "0.35"


utils_mpl.set_global()


def colours(kind):
    out = {}
    for m in MEDIA_ORDER:
        ids = [i for i, k in enumerate(kind) if k == m]
        for c, i in zip(shades_compact(m, max(len(ids), 1)), ids):
            out[i] = c
    return out


def load(run_dir: Path):
    r = load_run(run_dir)
    ids = sorted(r.nodes)
    mg = json.loads((run_dir / f"{ids[0]}.meta.json").read_text())
    mg = mg["controller"]["microgrid"]
    T_c = float(mg.get("T_c") or mg["interface"].get("T_c") or 40.0)
    kind, th, mu, t = [], [], [], None
    for i in ids:
        d = r.nodes[i].data
        kind.append(r.nodes[i].node_type)
        t = np.asarray(d["timestamp"], float)
        th.append(np.asarray(d["vartheta"], float) if "vartheta" in d else None)
        # Rebuilt where the node does not report it, so the heterogeneous
        # assignment gets this figure too. `applied_command` drops the first
        # row -- it has no predecessor -- so the series is padded back to the
        # log's length at the front rather than silently shifting by a tick.
        a = applied_command(run_dir, i)
        mu.append(np.concatenate([[np.nan], np.hypot(a[:, 0], a[:, 1])]))
    L = min(len(a) for a in th if a is not None)
    cut = lambda a: (np.vstack([u[:L] for u in a])
                     if all(u is not None for u in a) else None)
    return t[:L], cut(th), cut(mu), kind, T_c


def panel(runs, pick, ylabel, stem, out, T_c, legend="upper left", log=False):
    fig, ax = utils_mpl.get_fig(size=(FIG_W, FIG_H), dpi=FIG_DPI)
    col = colours(next(iter(runs.values()))[3])
    t_end, lo, hi = 0.0, np.inf, -np.inf
    drawn = False
    for arm, (t, th, mu, kind, _tc) in runs.items():
        series = th if pick == "theta" else mu
        if series is None:
            continue
        drawn = True
        t_end = max(t_end, float(t[-1]))
        for i in range(len(kind)):
            (ax.semilogy if log else ax.step)(
                t, np.maximum(series[i], 1e-6) if log else series[i],
                color=col[i], ls=STYLE.get(arm, DOTTED), lw=LW,
                zorder=3 if arm in STYLE else 2,
                label=arm if i == 0 else None)
            lo, hi = min(lo, series[i].min()), max(hi, series[i].max())
    if not drawn:
        plt.close(fig)
        print(f"  skip {stem}: no node logs it (relayed nodes do not report mu)")
        return
    ax.axvline(T_c, color=MARK_GREY, ls=":", lw=LW, zorder=4)
    ax.annotate(r"$T_c$", xy=(T_c, 0.92), xycoords=("data", "axes fraction"),
                xytext=(5, 0), textcoords="offset points", fontsize=8,
                color=MARK_GREY, va="center", ha="left", zorder=6,
                bbox=dict(boxstyle="square,pad=0.15", fc="white", ec="none",
                          alpha=0.85))
    xt = np.linspace(0, t_end, 5)
    utils_mpl.set_x_axis(ax, xt, margin=0.0)
    utils_mpl.set_format(ax.xaxis, xt, utils_mpl.make_formatter(".0f"))
    if not log:
        span = hi - lo if hi > lo else 1.0
        yt = np.linspace(lo - 0.05 * span, hi + 0.30 * span, 6)
        utils_mpl.set_y_axis(ax, yt, margin=0.0)
        utils_mpl.set_format(ax.yaxis, yt, utils_mpl.make_formatter(".2f"))
    ax.set_xlabel("Time [s]")
    ax.set_ylabel(ylabel)
    ax.legend(loc=legend, fontsize=6.5, ncol=len(runs), framealpha=0.9,
              handlelength=1.8, handletextpad=0.5, labelspacing=0.25,
              columnspacing=1.0, borderpad=0.3)
    ax.minorticks_on()
    utils_mpl.set_grid(fig, ax, major=log, minor=False)
    utils_mpl.save_pdf(fig, str(out / f"{stem}.pdf"))
    utils_mpl.save_png(fig, str(out / f"{stem}.png"), dpi=FIG_DPI)
    plt.close(fig)
    print(f"  wrote {out / stem}.pdf + .png")


def main() -> int:
    spec = dict(DEFAULT)
    if len(sys.argv) > 2:
        spec = {}
        for p in sys.argv[1:]:
            d = Path(p)
            name = json.loads((d / "1.meta.json").read_text())["run_name"]
            spec[name.rsplit("-", 1)[-1].replace("plus", "+")] = d
    runs = {a: load(d) for a, d in spec.items() if d.is_dir()}
    if not runs:
        return 2
    runs = dict(sorted(runs.items(), key=lambda kv: kv[0] in STYLE))
    out = dg5_paths.arms_dir(next(iter(spec.values())))
    T_c = next(iter(runs.values()))[4]
    print(f"  arms: {', '.join(runs)}   T_c = {T_c:g} s")
    stem = dg5_paths.pair_stem(runs)
    panel(runs, "theta", r"$\vartheta_i$", f"pair_{stem}_theta", out, T_c)
    panel(runs, "mu", r"$\|\mu_i^{\mathrm{app}}\|_2$", f"pair_{stem}_mu",
          out, T_c, legend="upper right", log=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
