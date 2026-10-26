"""Age-of-information figures: the sawtooth, and its distribution.

    python3 plotting-tools/plot_aoi.py

Two figures, because the mean alone is misleading and a single number cannot
show why.

  aoi_sawtooth.svg  A few seconds of age(t) on one link per medium. This is the
                    metric itself: age drops to zero when a value arrives and
                    climbs at the control rate until the next one, so the teeth
                    are the publication interval and the tall teeth are losses.
                    It makes the definition legible in a way a table cannot.

  aoi_cdf.svg       The distribution of age over a whole run, per medium, in
                    both regimes. The mean hides the tail that actually hurts
                    the control law: a link can have a respectable mean age and
                    still leave the coupling term stale for half a second.
                    Plotted as a CDF so the median and the p95 are both read off
                    directly, and with the publication period marked, since an
                    age below T_pub means the cache was refreshed every window.
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
from palette import MEDIA, MEDIA_LABEL, MEDIA_ORDER
from vertex.analysis import load_node

CAMPAIGN_B = ROOT / "runs/n30-regimeB"
CAMPAIGN_A = ROOT / "runs/n30-regimeA"
ARM = "n30-ring4-40hz"          # degree 4, the densest neighbourhood
RUN_B, RUN_A = f"{ARM}-c30", f"{ARM}-c05"
T_PUB = 0.125
FIG_W, FIG_H, FIG_DPI = 3.5, 2.5, 300
MEDIA_STYLE = {k: (MEDIA[k], MEDIA_LABEL[k]) for k in MEDIA_ORDER}

utils_mpl.set_global()


def ages_by_medium(run_dir: Path):
    """{medium: 1-D array of cached-value ages, in seconds, over all its links}."""
    kinds, out = {}, {}
    for m in run_dir.glob("*.meta.json"):
        j = json.loads(m.read_text())
        kinds[j["node_id"]] = (j["node_type"], j["dt_s"])
    per_medium = {k: [] for k in MEDIA_STYLE}
    example = {}
    for nid, (kind, dt) in sorted(kinds.items()):
        if kind not in per_medium:
            continue
        try:
            n = load_node(run_dir, nid)
        except Exception:
            continue
        for s in n.neighbours:
            f = np.asarray(n.data[f"rx_{s}"], dtype=bool)
            idx = np.arange(len(f))
            # index of the most recent arrival at or before each sample
            last = np.maximum.accumulate(np.where(f, idx, -1))
            age = np.where(last >= 0, (idx - last) * dt, np.nan)
            per_medium[kind].append(age)
            if kind not in example:
                example[kind] = (np.asarray(n.t), age)
    for k, v in per_medium.items():
        out[k] = np.concatenate(v) if v else np.array([])
    return out, example


def main() -> int:
    outdir = ROOT / "plotting-tools/render"
    outdir.mkdir(parents=True, exist_ok=True)
    ages_b, ex_b = ages_by_medium(CAMPAIGN_B / RUN_B)
    ages_a, _ = ages_by_medium(CAMPAIGN_A / RUN_A)

    # ---- 1. the sawtooth ---------------------------------------------------
    fig, ax = utils_mpl.get_fig(size=(FIG_W, FIG_H), dpi=FIG_DPI)
    t0, t1 = 200.0, 206.0
    for kind in ("ble", "wifi"):
        if kind not in ex_b:
            continue
        t, age = ex_b[kind]
        m = (t >= t0) & (t <= t1)
        c, lab = MEDIA_STYLE[kind]
        ax.plot(t[m] - t0, age[m] * 1000, color=c, lw=1.0, label=lab)
    ax.axhline(T_PUB * 1000, color="0.35", ls=":", lw=1.0)
    ax.annotate(r"$T_\mathrm{pub}$", xy=(0.02, T_PUB * 1000),
                xytext=(2, 3), textcoords="offset points",
                fontsize=8, color="0.35")
    xt = np.linspace(0, t1 - t0, 4)
    utils_mpl.set_x_axis(ax, xt, margin=0.0)
    utils_mpl.set_format(ax.xaxis, xt, utils_mpl.make_formatter(".0f"))
    ax.set_xlabel("Time [s]")
    ax.set_ylabel("Age of cached value [ms]")
    ax.legend(loc="upper right", fontsize=8)
    ax.minorticks_on()
    utils_mpl.set_grid(fig, ax, major=False, minor=False)
    utils_mpl.save_svg(fig, str(outdir / "aoi_sawtooth.svg"))
    utils_mpl.save_pdf(fig, str(outdir / "aoi_sawtooth.pdf"))
    utils_mpl.save_png(fig, str(outdir / "aoi_sawtooth.png"), dpi=FIG_DPI)
    plt.close(fig)
    print(f"  wrote {outdir/'aoi_sawtooth.svg'}")

    # ---- 2. the distribution ----------------------------------------------
    fig, ax = utils_mpl.get_fig(size=(FIG_W, FIG_H), dpi=FIG_DPI)
    for kind, (c, lab) in MEDIA_STYLE.items():
        for ages, ls, tag in ((ages_b[kind], "-", ""), (ages_a[kind], "--", " (A)")):
            a = ages[~np.isnan(ages)]
            if not a.size:
                continue
            xs = np.sort(a) * 1000
            ys = np.arange(1, len(xs) + 1) / len(xs)
            step = max(1, len(xs) // 3000)
            ax.plot(xs[::step], ys[::step], color=c, ls=ls, lw=1.1,
                    label=f"{lab}{tag}")
    ax.axvline(T_PUB * 1000, color="0.35", ls=":", lw=1.0)
    ax.annotate(r"$T_\mathrm{pub}$", xy=(T_PUB * 1000, 0.05),
                xytext=(3, 0), textcoords="offset points",
                fontsize=8, color="0.35", va="center")
    ax.set_xlim(0, 600)
    ax.set_ylim(0, 1.0)
    xt = np.linspace(0, 600, 5)
    yt = np.linspace(0, 1, 6)
    utils_mpl.set_x_axis(ax, xt, margin=0.0)
    utils_mpl.set_format(ax.xaxis, xt, utils_mpl.make_formatter(".0f"))
    utils_mpl.set_y_axis(ax, yt, margin=0.0)
    utils_mpl.set_format(ax.yaxis, yt, utils_mpl.make_formatter(".1f"))
    ax.set_xlabel("Age of cached value [ms]")
    ax.set_ylabel("CDF")
    ax.legend(loc="lower right", fontsize=7, ncol=2)
    ax.minorticks_on()
    utils_mpl.set_grid(fig, ax, major=True, minor=False)
    utils_mpl.save_svg(fig, str(outdir / "aoi_cdf.svg"))
    utils_mpl.save_pdf(fig, str(outdir / "aoi_cdf.pdf"))
    utils_mpl.save_png(fig, str(outdir / "aoi_cdf.png"), dpi=FIG_DPI)
    plt.close(fig)
    print(f"  wrote {outdir/'aoi_cdf.svg'}")

    print("\n  median / p95 age in ms, solid = regime B, dashed = regime A")
    for kind in MEDIA_STYLE:
        for ages, tag in ((ages_b[kind], "B"), (ages_a[kind], "A")):
            a = ages[~np.isnan(ages)] * 1000
            if a.size:
                print(f"    {kind:7} {tag}  median {np.median(a):6.1f}   "
                      f"p95 {np.percentile(a,95):7.1f}   max {a.max():7.1f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
