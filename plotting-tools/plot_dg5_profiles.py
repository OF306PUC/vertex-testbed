r"""The LC interface against the dead zone, in normalised power, per profile.

One file per (uncertainty profile, LC variant, coordinate):

    render/dg5/rep-mixed/arms/pair_LC0-DZ_xP.pdf        P_i/kappa_i
    render/dg5/rep-mixed/arms/pair_LC0-DZ_xQ.pdf        Q_i/kappa_i
    render/dg5/per-mixed/arms/pair_LC0-DZ_xP.pdf
    render/dg5/per-mixed/arms/pair_LC0-DZ_xQ.pdf
    render/dg5/per-mixed/arms/pair_LCplus-DZ_xP.pdf
    render/dg5/per-mixed/arms/pair_LCplus-DZ_xQ.pdf

Separate files, not one grid: the arrangement belongs to the document. The
y limits are nevertheless computed across EVERY panel of one coordinate, so
that any two of them can be tiled without the reader having to check whether
the axes agree -- which is the one thing a grid gave that independent figures
otherwise would not.

Every LC variant a profile has is drawn, including the one whose premise the
profile breaks. An earlier version paired each profile with the variant that
was supposed to work there -- LC0 under replication, LC+ under persistence --
and left out LC0 under persistence on the grounds that it compares a
controller against a condition it does not claim. That is the comparison
worth having: LC0 reaches the reference before T_c and then drifts away from
it, because the terminal policy freezes the estimator while the uncertainty
is still there, and the drift reads as a drift only against a DZ trace that
stays put. The per-run figures show the same trajectories without DZ to
measure them against.

Normalised, where `plot_dg5_compare.py` draws the same comparison in physical
units: there every DG lands on its own level kappa_i * r and the figure reads
as proportional sharing; here all five land on one line and it reads as
consensus. Both are wanted and neither substitutes for the other.

Colour is the agent, as everywhere else in this set; style is the interface.
No per-agent legend -- ten traces per panel would need twenty entries to say
what two line styles already say -- only the reference is named.

    python3 plotting-tools/plot_dg5_profiles.py
    python3 plotting-tools/plot_dg5_profiles.py --trial 3
"""
import argparse
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
from vertex.analysis.load import load_run

#: (profile, run-directory prefix). Each is paired with every LC arm below
#: that it actually has on disk; `rep` was collected without LC+.
PROFILES = [("replication", "runs/rep-mixed/dg5-mixed-rep-0"),
            ("persistent", "runs/per-mixed/dg5-mixed-0")]

#: The arms drawn against DZ, in drawing order.
LC_ARMS = ("LC0", "LCplus")

FIG_W, FIG_H, FIG_DPI = 3.5, 2.5, 300
LW = 1.0
DASHED = (0, (3.0, 1.6))
REF_GREY = "0.45"

utils_mpl.set_global()


def colours(kind):
    out = {}
    for m in MEDIA_ORDER:
        ids = [i for i, k in enumerate(kind) if k == m]
        for c, i in zip(shades_compact(m, max(len(ids), 1)), ids):
            out[i] = c
    return out


def load(run: Path):
    """`(t, x^P, x^Q, kind, reference)` with x normalised, i.e. P_i/kappa_i."""
    r = load_run(run)
    ids = sorted(r.nodes)
    xP, xQ, kind, t, ref = [], [], [], None, None
    for i in ids:
        mg = json.loads((run / f"{i}.meta.json").read_text())
        mg = mg["controller"]["microgrid"]
        if float(mg["virtual"]["b"]) != 0.0:
            ref = (float(mg["virtual"]["rP"]), float(mg["virtual"]["rQ"]))
        d = r.nodes[i].data
        kind.append(r.nodes[i].node_type)
        xP.append(np.asarray(d["state_P"], float))
        xQ.append(np.asarray(d["state_Q"], float))
        t = np.asarray(d["timestamp"], float)
    L = min(map(len, xP + xQ))
    return (t[:L], np.vstack([a[:L] for a in xP]),
            np.vstack([a[:L] for a in xQ]), kind, ref)


def panel(runs, lc, cidx, sym, ylim, out) -> None:
    """One coordinate, one profile: the LC variant dashed over DZ solid."""
    col = colours(runs["DZ"][3])
    fig, ax = utils_mpl.get_fig(size=(FIG_W, FIG_H), dpi=FIG_DPI)
    t_end = 0.0
    for arm, (t, xP, xQ, kind, _ref) in runs.items():
        series = xP if cidx == 0 else xQ
        t_end = max(t_end, float(t[-1]))
        for i in range(len(kind)):
            ax.step(t, series[i], color=col[i], lw=LW,
                    ls="solid" if arm == "DZ" else DASHED,
                    zorder=3 if arm == "DZ" else 2)
    ax.axhline(runs["DZ"][4][cidx], color=REF_GREY, ls="--", lw=LW, zorder=1,
               label=rf"$r^{sym}$")

    lo, hi = ylim
    span = hi - lo if hi > lo else 1.0
    xt = np.linspace(0, t_end, 5)
    yt = np.linspace(lo - 0.05 * span, hi + 0.08 * span, 5)
    utils_mpl.set_x_axis(ax, xt, margin=0.0)
    utils_mpl.set_format(ax.xaxis, xt, utils_mpl.make_formatter(".0f"))
    utils_mpl.set_y_axis(ax, yt, margin=0.0)
    utils_mpl.set_format(ax.yaxis, yt, utils_mpl.make_formatter(".0f"))
    ax.set_xlabel("Time [s]")
    ax.set_ylabel(rf"${sym}_i/\kappa_i$")
    ax.legend(loc="lower right", fontsize=7, framealpha=0.9,
              handlelength=1.6, borderpad=0.3)
    ax.minorticks_on()
    utils_mpl.set_grid(fig, ax, major=False, minor=False)
    utils_mpl.save_pdf(fig, f"{out}.pdf")
    utils_mpl.save_png(fig, f"{out}.png", dpi=FIG_DPI)
    plt.close(fig)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trial", type=int, default=0)
    a = ap.parse_args()

    # Load every run before drawing anything: the y limits span every panel
    # of a coordinate, so none can be drawn until all of them are known.
    pairs, bounds = [], {0: [np.inf, -np.inf], 1: [np.inf, -np.inf]}
    for profile, prefix in PROFILES:
        dz = ROOT / f"{prefix}-t{a.trial}-DZ"
        if not dz.is_dir():
            print(f"  missing {dz}", file=sys.stderr)
            return 2
        dz_data = load(dz)
        for lc in LC_ARMS:
            d = ROOT / f"{prefix}-t{a.trial}-{lc}"
            if not d.is_dir():
                print(f"  {profile}: no {lc} run, skipped")
                continue
            # LC first so `pair_stem` reads `LC0-DZ`, and so DZ is drawn
            # last and its solid traces sit on top.
            runs = {lc: load(d), "DZ": dz_data}
            pairs.append((profile, lc, runs, dz))
            for cidx in (0, 1):
                b = bounds[cidx]
                for t, xP, xQ, _kind, _ref in runs.values():
                    series = xP if cidx == 0 else xQ
                    b[0] = min(b[0], float(series.min()), dz_data[4][cidx])
                    b[1] = max(b[1], float(series.max()), dz_data[4][cidx])

    if not pairs:
        print("no LC/DZ pair found", file=sys.stderr)
        return 2
    for profile, lc, runs, dz in pairs:
        out = dg5_paths.arms_dir(dz)
        for cidx, sym in ((0, "P"), (1, "Q")):
            stem = out / f"pair_{dg5_paths.pair_stem(runs)}_x{sym}"
            panel(runs, lc, cidx, sym, bounds[cidx], stem)
            print(f"  {stem.relative_to(ROOT)}.pdf   "
                  f"{profile}: {lc} dashed, DZ solid")
    return 0


if __name__ == "__main__":
    sys.exit(main())
