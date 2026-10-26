"""Presentation figures: wider panels, fewer things per slide.

    python3 plotting-tools/plot_slides.py [--topo clusters] [--run c30]

Five figures from one run, plus a schematic of BLE advertising and scanning
produced by plot_ble_timing.py.

  slide_x.png            states, full run beside a steady-state zoom
  slide_z.png            virtual states, same treatment
  slide_vartheta.png     adaptive gains
  slide_disagreement.png spread about the fleet mean, per medium, log scale
  slide_consensus.png    distance of the fleet mean from the target, log scale

Agents are coloured by medium throughout, in tints of that medium's colour, so
the same trace means the same thing on every slide. The two log-scale figures
are split by medium rather than drawn as one fleet curve, because the media
differ by an order of magnitude in delivery and a single curve would hide which
subnetwork is responsible for what.
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
from palette import MEDIA, MEDIA_LABEL, MEDIA_ORDER, shades
from vertex.analysis import load_node

CAMPAIGN = ROOT / "runs/n30-regimeB"
N = 30
DELTA = 0.01
MERGE_T = 60.0
# Same style as the paper figures, at roughly twice the size: a slide is read
# from across a room, so the type has to grow with the canvas rather than the
# canvas alone. utils_mpl.set_global gives the IEEE serif/tick/grid settings.
FIG_W, FIG_H, DPI = 9.0, 3.6, 300      # PNG at print dpi: slide software
                                       # rasterises SVG unpredictably
ZOOM_FRAC = 0.12                          # tail of the run shown in the zoom
TOPOLOGIES = ("dring", "ring4", "clusters")

utils_mpl.set_global(fontsize=15.0)


def load(run_dir):
    kinds, xs, zs, vs, t = {}, [], [], [], None
    for nid in range(1, N + 1):
        j = json.loads((run_dir / f"{nid}.meta.json").read_text())
        kinds[nid] = j["node_type"]
        n = load_node(run_dir, nid)
        xs.append(np.asarray(n.state)); zs.append(np.asarray(n.vstate))
        vs.append(np.asarray(n.vartheta)); t = np.asarray(n.t)
    L = min(map(len, xs))
    return (t[:L], np.vstack([a[:L] for a in xs]), np.vstack([a[:L] for a in zs]),
            np.vstack([a[:L] for a in vs]), kinds)


def agent_colours(kinds):
    out = {}
    for m in MEDIA_ORDER:
        ids = [i for i in sorted(kinds) if kinds[i] == m]
        for c, i in zip(shades(m, len(ids)), ids):
            out[i] = c
    return out


def two_panel(t, S, colours, ylabel, fname, merge, zoom_band=None, ylab_zoom=None):
    """Full run on the left, steady-state tail on the right."""
    fig, ax = plt.subplots(1, 2, figsize=(FIG_W, FIG_H), dpi=DPI,
                           gridspec_kw={"width_ratios": [2, 1]})
    for a_ in ax:
        a_.minorticks_on()
    t0 = t[-1] * (1 - ZOOM_FRAC)
    m = t >= t0
    for i in range(N):
        ax[0].plot(t, S[i], color=colours[i + 1], lw=0.9)
        ax[1].plot(t[m], S[i][m], color=colours[i + 1], lw=0.9)
    if merge:
        ax[0].axvline(MERGE_T, color="0.4", ls=":", lw=1.0)
        ax[0].annotate("$t=60$ s", xy=(MERGE_T, 0.04),
                       xycoords=("data", "axes fraction"),
                       xytext=(6, 0), textcoords="offset points", fontsize=9,
                       color="0.4", va="center",
                       arrowprops=dict(arrowstyle="->", color="0.4", lw=0.8))
    if zoom_band is not None:
        ax[1].axhspan(zoom_band - DELTA, zoom_band + DELTA, color="0.85",
                      zorder=0, lw=0)
        ax[1].axhline(zoom_band, color="k", ls="-.", lw=1.2)
    for a_, lab in ((ax[0], "full run"), (ax[1], "steady state")):
        a_.set_xlabel("Time [s]")
        a_.grid(alpha=.3)
        a_.set_title(lab, fontsize=13)
    ax[0].set_ylabel(ylabel)
    ax[1].set_ylabel(ylab_zoom or "")
    # one legend for the three media, not thirty agents
    for m_ in MEDIA_ORDER:
        ax[0].plot([], [], color=MEDIA[m_], lw=2, label=MEDIA_LABEL[m_])
    ax[0].legend(loc="best", fontsize=12, ncol=3)
    fig.tight_layout()
    fig.savefig(fname, bbox_inches="tight", dpi=DPI)
    plt.close(fig)
    print(f"  wrote {fname}")


def one_panel(t, S, colours, ylabel, fname, merge):
    """Single axes, no zoom: for a quantity whose steady state is the flat part.

    vartheta latches and then holds, so a steady-state inset would show a set of
    horizontal lines and nothing else. The interesting behaviour is entirely in
    the climb.
    """
    fig, ax = plt.subplots(figsize=(FIG_W * 0.68, FIG_H), dpi=DPI)
    ax.minorticks_on()
    for i in range(N):
        ax.plot(t, S[i], color=colours[i + 1], lw=0.9)
    if merge:
        ax.axvline(MERGE_T, color="0.4", ls=":", lw=1.0)
        ax.annotate("$t=60$ s", xy=(MERGE_T, 0.06),
                    xycoords=("data", "axes fraction"),
                    xytext=(6, 0), textcoords="offset points", fontsize=12,
                    color="0.4", va="center",
                    arrowprops=dict(arrowstyle="->", color="0.4", lw=0.8))
    for m_ in MEDIA_ORDER:
        ax.plot([], [], color=MEDIA[m_], lw=2, label=MEDIA_LABEL[m_])
    ax.set_xlabel("Time [s]")
    ax.set_ylabel(ylabel)
    ax.legend(loc="best", fontsize=12, ncol=3)
    ax.grid(alpha=.3)
    fig.tight_layout()
    fig.savefig(fname, bbox_inches="tight", dpi=DPI)
    plt.close(fig)
    print(f"  wrote {fname}")


def log_panel(t, series_by_medium, ylabel, fname, merge, floor_label=None):
    fig, ax = plt.subplots(figsize=(FIG_W * 0.62, FIG_H), dpi=DPI)
    ax.minorticks_on()
    for m_, y in series_by_medium.items():
        ax.semilogy(t, y, color=MEDIA[m_], lw=1.3, label=MEDIA_LABEL[m_])
    if merge:
        ax.axvline(MERGE_T, color="0.4", ls=":", lw=1.0)
        ax.annotate("$t=60$ s", xy=(MERGE_T, 0.06),
                    xycoords=("data", "axes fraction"),
                    xytext=(6, 0), textcoords="offset points", fontsize=9,
                    color="0.4", va="center",
                    arrowprops=dict(arrowstyle="->", color="0.4", lw=0.8))
    if floor_label:
        ax.axhline(DELTA, color="0.4", ls="--", lw=1.0)
        ax.annotate(floor_label, xy=(0.98, DELTA), xycoords=("axes fraction", "data"),
                    xytext=(0, 5), textcoords="offset points",
                    fontsize=12, color="0.4", ha="right")
    ax.set_xlabel("Time [s]")
    ax.set_ylabel(ylabel)
    ax.legend(loc="upper right", fontsize=12)
    ax.grid(alpha=.3, which="both")
    fig.tight_layout()
    fig.savefig(fname, bbox_inches="tight", dpi=DPI)
    plt.close(fig)
    print(f"  wrote {fname}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--topo", action="append", default=[],
                    help="repeatable; defaults to all three")
    ap.add_argument("--rate", default="40hz")
    ap.add_argument("--run", default="c30")
    a = ap.parse_args()
    for topo in (a.topo or TOPOLOGIES):
        rc = one(topo, a.rate, a.run)
        if rc:
            return rc
    return 0


def one(topo, rate, runid) -> int:
    run = CAMPAIGN / f"n30-{topo}-{rate}-{runid}"
    if not run.is_dir():
        print(f"no such run: {run}", file=sys.stderr)
        return 2
    out = ROOT / "plotting-tools/render/slides" / topo
    out.mkdir(parents=True, exist_ok=True)
    merge = topo == "clusters"

    t, X, Z, V, kinds = load(run)
    col = agent_colours(kinds)
    zbar = float(Z[:, 0].mean())

    two_panel(t, X, col, "States $x_i$", out / f"{topo}_x.png", merge,
              zoom_band=zbar, ylab_zoom=r"$x_i$ about $\bar{z}$")
    two_panel(t, Z, col, "Virtual states $z_i$", out / f"{topo}_z.png", merge,
              zoom_band=zbar, ylab_zoom=r"$z_i$ about $\bar{z}$")
    one_panel(t, V, col, r"Adaptive gains $\vartheta_i$",
              out / f"{topo}_vartheta.png", merge)

    # disagreement and consensus error, per medium
    idx = {m_: [i - 1 for i in sorted(kinds) if kinds[i] == m_] for m_ in MEDIA_ORDER}
    xbar = X.mean(axis=0)
    disag = {m_: np.sqrt(((X[ii] - xbar) ** 2).mean(axis=0)) for m_, ii in idx.items()}
    cons = {m_: np.abs(X[ii].mean(axis=0) - zbar) for m_, ii in idx.items()}
    log_panel(t, disag, r"RMS spread about $\bar{x}(t)$",
              out / f"{topo}_disagreement.png", merge, floor_label=r"$\delta$")
    log_panel(t, cons, r"$|\,\overline{x}_m(t)-\bar{z}\,|$",
              out / f"{topo}_consensus.png", merge)
    return 0


if __name__ == "__main__":
    sys.exit(main())
