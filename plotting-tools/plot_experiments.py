"""Publication figures for the three campaign topologies.

Ten figures, each as .pdf (for the paper: vector, real LaTeX text), .svg and
.png, in plot.py's style and at its dimensions:
  <topology>_x.svg, _z.svg, _vartheta.svg   for dring, ring4, clusters
  disagreement.svg                          all three on one axes

The run per topology is chosen by tools, not by eye: see pick.py. Ranking is on
absence of dead links, then how close the consensus lands to the mean of the
initial conditions, then delivery. Convergence SPEED is deliberately not a
criterion -- in a directed cycle a degraded link makes the fleet converge faster
and to one node's initial condition instead of the average, so ranking on speed
selects the broken runs.

    python3 plotting-tools/plot_experiments.py
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
from palette import MEDIA, MEDIA_LABEL, MEDIA_ORDER, TOPOLOGY, TOPOLOGY_LABEL, shades
from core import compute_disagreement_scalar
# One definition of the presentation window, shared with the tool that ranks the
# runs: a run is chosen on exactly the span it is drawn on.
from pick import TMAX
from vertex.analysis import load_node

CAMPAIGN = ROOT / "runs/n30-regimeB"
# python3 plotting-tools/pick.py --json results/pick-40hz.json
RUNS = {"dring":    "n30-dring-40hz-c35",
        "ring4":    "n30-ring4-40hz-c27",
        "clusters": "n30-clusters-40hz-c37"}
LABEL = TOPOLOGY_LABEL
N = 30
FIG_W, FIG_H, FIG_DPI = 3.5, 2.5, 300
MERGE_T = 60.0                       # G3 cut-points enabled here

utils_mpl.set_global()

# Agents are coloured by the medium they run on, in tints of that medium's
# colour, so a trace says which subnetwork it belongs to. The previous `turbo`
# cycle varied hue across node index, which no reader decodes, and happened to
# assign BLE the blues and bridge the reds -- contradicting the medium palette
# used by every other figure.
AGENT_COLOR = {}
for _m in MEDIA_ORDER:
    _ids = [i for i in range(1, N + 1)
            if (_m == "ble" and i <= 10) or (_m == "wifi" and 10 < i <= 20)
            or (_m == "bridge" and i > 20)]
    for _c, _i in zip(shades(_m, len(_ids)), _ids):
        AGENT_COLOR[_i] = _c


def load(run_dir: Path, tmax: float | None = None):
    """(t, x, z, vartheta) as (N, n_t) arrays on a common length.

    `tmax` crops the run to the presentation window before anything is derived
    from it, so the disagreement and the axis limits describe the span drawn.
    """
    t = x = None
    xs, zs, vs = [], [], []
    for nid in range(1, N + 1):
        n = load_node(run_dir, nid)
        xs.append(np.asarray(n.state)); zs.append(np.asarray(n.vstate))
        vs.append(np.asarray(n.vartheta)); t = np.asarray(n.t)
    L = min(map(len, xs))
    if tmax is not None:
        L = min(L, int(np.searchsorted(t, tmax, side="right")))
    return (t[:L], np.vstack([a[:L] for a in xs]),
            np.vstack([a[:L] for a in zs]), np.vstack([a[:L] for a in vs]))


#: Agents named in the clusters legends: the two cut-points, disabled until the
#: merge at t = 60 s. They are the only agents whose behaviour is a function of
#: the event, so they are the only ones worth naming out of thirty traces.
CUTPOINTS = (20, 29)          # 0-based, i.e. agents 21 and 30


def mark_merge_line(ax, yfrac=0.07):
    """Dotted line at the merge, with an arrow naming when it happens.

    An arrow rather than a floating word: this is the only event in a run and
    a reader needs the time, not a label. Always the same side and the same
    direction, on every figure, so the set reads as one family. It sits just
    above the time axis, which is clear on the per-topology panels but is where
    G1's plateau settles on the log-scale summaries, so the text carries an
    opaque box and is drawn above the traces rather than being moved.
    """
    ax.axvline(MERGE_T, color="0.35", ls=":", lw=1.0, zorder=4)
    ax.annotate(r"$t = 60$ s", xy=(MERGE_T, yfrac),
                xycoords=("data", "axes fraction"),
                xytext=(26, 0), textcoords="offset points",
                fontsize=8, color="0.35", va="center", ha="left", zorder=6,
                bbox=dict(boxstyle="square,pad=0.15", fc="white", ec="none",
                          alpha=0.85),
                arrowprops=dict(arrowstyle="->", color="0.35", lw=0.8,
                                shrinkA=0, shrinkB=1))


def panel(t, series, ylabel, fname, fmt=".0f", ref=None, reflabel=None,
          refstyle="k--", mark_merge=False, sym=None, legend_loc="upper right"):
    fig, ax = utils_mpl.get_fig(size=(FIG_W, FIG_H), dpi=FIG_DPI)
    named = CUTPOINTS if (sym and mark_merge) else ()
    for i in range(N):
        lbl = f"${sym}_{{{i+1}}}$" if i in named else None
        ax.step(t, series[i], color=AGENT_COLOR[i + 1],
                lw=1.2 if i in named else 0.9,
                zorder=3 if i in named else 2, label=lbl)
    if ref is not None:
        ax.step(t, ref, refstyle, lw=1.5, label=reflabel)
    if mark_merge:
        mark_merge_line(ax)
    lo, hi = float(series.min()), float(series.max())
    pad = 0.05 * (hi - lo if hi > lo else 1.0)
    xticks = np.linspace(0, float(t[-1]), 5)
    yticks = np.linspace(lo - pad, hi + pad, 6)
    utils_mpl.set_x_axis(ax, xticks, margin=0.0)
    utils_mpl.set_format(ax.xaxis, xticks, utils_mpl.make_formatter(".0f"))
    utils_mpl.set_y_axis(ax, yticks, margin=0.0)
    utils_mpl.set_format(ax.yaxis, yticks, utils_mpl.make_formatter(fmt))
    ax.set_xlabel("Time [s]")
    ax.set_ylabel(ylabel)
    if reflabel or named:
        ax.legend(loc=legend_loc, fontsize=8)
    ax.minorticks_on()
    utils_mpl.set_grid(fig, ax, major=False, minor=False)
    utils_mpl.save_svg(fig, str(fname))
    utils_mpl.save_pdf(fig, str(fname).replace(".svg", ".pdf"))
    utils_mpl.save_png(fig, str(fname).replace(".svg", ".png"), dpi=FIG_DPI)
    plt.close(fig)
    print(f"  wrote {fname}")


KINDS = {}                 # topology -> {node id: medium}, from the run metadata


def main() -> int:
    data = {}
    for topo, run in RUNS.items():
        d = CAMPAIGN / run
        if not d.is_dir():
            print(f"missing {d}", file=sys.stderr); return 2
        data[topo] = load(d, TMAX.get(topo))
        KINDS[topo] = {json.loads(m.read_text())["node_id"]:
                       json.loads(m.read_text())["node_type"]
                       for m in d.glob("*.meta.json")}
        out = ROOT / "plotting-tools/render" / topo
        out.mkdir(parents=True, exist_ok=True)
        t, x, z, v = data[topo]
        zbar = float(z[:, 0].mean())
        merge = topo == "clusters"
        panel(t, x, "States", out / f"{topo}_x.svg", ".0f",
              ref=z[0], reflabel=r"$z_{1}$ (ref.)", mark_merge=merge, sym="x")
        panel(t, z, "Virtual States", out / f"{topo}_z.svg", ".0f",
              ref=zbar * np.ones_like(t), reflabel=rf"$\bar{{z}} = {zbar:.2f}$",
              refstyle="k-.", mark_merge=merge, sym="z")
        panel(t, v, "Adaptive Gains", out / f"{topo}_vartheta.svg", ".3f",
              mark_merge=merge, sym=r"\vartheta", legend_loc="upper left")

    # ---- summary figures: the three topologies on one axes ------------------
    #
    # Topology-coloured, from palette.TOPOLOGY, which shares no hue with the
    # medium palette precisely so these can sit beside the per-agent panels
    # without the same three colours carrying two meanings.
    out = ROOT / "plotting-tools/render"
    xt = np.linspace(0, max(float(v[0][-1]) for v in data.values()), 5)

    def save(fig, stem):
        utils_mpl.save_svg(fig, str(out / f"{stem}.svg"))
        utils_mpl.save_png(fig, str(out / f"{stem}.png"), dpi=300)
        plt.close(fig)
        print(f"  wrote {out / stem}.svg + .png")

    # disagreement: || (I - 11^T/N) x(t) ||
    fig, ax = utils_mpl.get_fig(size=(FIG_W, FIG_H), dpi=FIG_DPI)
    for topo, (t_, x, z, v) in data.items():
        ax.semilogy(t_, compute_disagreement_scalar(x), color=TOPOLOGY[topo],
                    lw=1.1, label=LABEL[topo])
    mark_merge_line(ax)
    utils_mpl.set_x_axis(ax, xt, margin=0.0)
    utils_mpl.set_format(ax.xaxis, xt, utils_mpl.make_formatter(".0f"))
    ax.set_xlabel("Time [s]")
    ax.set_ylabel(r"$\|(I-\frac{1}{N}\mathbf{1}\mathbf{1}^{\top})x(t)\|$")
    ax.legend(loc="upper right", fontsize=8)
    ax.minorticks_on()
    utils_mpl.set_grid(fig, ax, major=True, minor=False)
    save(fig, "disagreement")

    # distance of the fleet mean from the target
    fig, ax = utils_mpl.get_fig(size=(FIG_W, FIG_H), dpi=FIG_DPI)
    for topo, (t_, x, z, v) in data.items():
        zbar = float(z[:, 0].mean())
        ax.semilogy(t_, np.linalg.norm(x - zbar, axis=0), color=TOPOLOGY[topo],
                    lw=1.1, label=LABEL[topo])
    mark_merge_line(ax)
    utils_mpl.set_x_axis(ax, xt, margin=0.0)
    utils_mpl.set_format(ax.xaxis, xt, utils_mpl.make_formatter(".0f"))
    ax.set_xlabel("Time [s]")
    ax.set_ylabel(r"$\|x(t)-\bar{z}\mathbf{1}\|$")
    ax.legend(loc="upper right", fontsize=8)
    ax.minorticks_on()
    utils_mpl.set_grid(fig, ax, major=True, minor=False)
    save(fig, "consensus_error")

    # signed offset, on a linear axis. The norm above cannot show sign, and the
    # sign is what separates the two rings: they settle at nearly equal distance
    # from zbar, one above and one below, so on a log-norm plot they overlap and
    # look like the same result.
    fig, ax = utils_mpl.get_fig(size=(FIG_W, FIG_H), dpi=FIG_DPI)
    for topo, (t_, x, z, v) in data.items():
        zbar = float(z[:, 0].mean())
        ax.plot(t_, x.mean(axis=0) - zbar, color=TOPOLOGY[topo], lw=1.2,
                label=LABEL[topo])
    ax.axhline(0.0, color="0.35", ls="-", lw=0.9)
    mark_merge_line(ax)
    utils_mpl.set_x_axis(ax, xt, margin=0.0)
    utils_mpl.set_format(ax.xaxis, xt, utils_mpl.make_formatter(".0f"))
    ax.set_xlabel("Time [s]")
    ax.set_ylabel(r"$\overline{x}(t)-\bar{z}$")
    ax.legend(loc="lower right", fontsize=8)
    ax.minorticks_on()
    utils_mpl.set_grid(fig, ax, major=True, minor=False)
    save(fig, "consensus_offset_signed")

    # the decomposition, as two panels rather than six overlaid curves:
    #   MSE(x_i - zbar) = offset^2 + spread^2
    # One axes needed solid and dotted lines for six series and was unreadable.
    # Side by side, the two-order-of-magnitude gap between the panels is the
    # message, and each panel keeps one colour per topology.
    fig, ax = plt.subplots(1, 2, figsize=(FIG_W * 2.05, FIG_H), dpi=FIG_DPI,
                           sharex=True)
    for topo, (t_, x, z, v) in data.items():
        zbar = float(z[:, 0].mean())
        ax[0].semilogy(t_, np.abs(x.mean(axis=0) - zbar), color=TOPOLOGY[topo],
                       lw=1.2, label=LABEL[topo])
        ax[1].semilogy(t_, np.sqrt(((x - x.mean(axis=0)) ** 2).mean(axis=0)),
                       color=TOPOLOGY[topo], lw=1.2)
    for k, title in enumerate((r"offset  $|\bar{x}(t)-\bar{z}|$",
                               r"spread  RMS$(x_i-\bar{x})$")):
        ax[k].axvline(MERGE_T, color="0.35", ls=":", lw=1.0)
        ax[k].set_xlabel("Time [s]")
        ax[k].set_title(title, fontsize=10)
        ax[k].minorticks_on()
        ax[k].grid(alpha=.3, which="both")
    ax[0].legend(loc="upper right", fontsize=8)
    ax[0].set_xlim(0, xt[-1])
    fig.tight_layout()
    fig.savefig(out / "mse_decomposition.svg", bbox_inches="tight")
    fig.savefig(out / "mse_decomposition.png", bbox_inches="tight", dpi=300)
    plt.close(fig)
    print(f"  wrote {out}/mse_decomposition.svg + .png")
    return 0


if __name__ == "__main__":
    sys.exit(main())
