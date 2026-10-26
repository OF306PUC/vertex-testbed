"""Two interfaces, one axes: the per-agent physical response compared.

The per-run panels show one interface at a time, which answers "did this
arm work" and not "how do they differ". Here both are drawn together, one
figure per coordinate:

    dg5cmp_P.pdf    P_i(t) under DZ and under LC
    dg5cmp_Q.pdf    Q_i(t) under DZ and under LC

Colour is the AGENT and line style is the INTERFACE. That way DG3 is the
same amber in both, so a reader follows one generator across the two laws
instead of decoding ten independent traces; and solid-vs-dotted separates
the laws without spending a second colour dimension the medium palette has
already claimed.

Per-generator targets kappa_i * r are drawn in grey and carry no legend
entry: five more rows would say what every trace landing on its own line
already says.

    python3 plotting-tools/plot_dg5_compare.py DZ_RUN LC_RUN
    python3 plotting-tools/plot_dg5_compare.py            # the defaults below
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
from vertex.analysis.load import load_run

DEFAULT = {"DZ": ROOT / "runs/dg5-bridge5-0-t0-DZ",
           "LC+": ROOT / "runs/dg5-bridge5-0-LCplus"}
N = 5
FIG_W, FIG_H, FIG_DPI = 3.5, 2.5, 300

#: Solid is the proposed interface; every other arm is dotted. One law is
#: the subject of the comparison and the rest are what it is compared to,
#: and the figure should say which is which without reading the legend.
STYLE = {"DZ": "solid"}
DOTTED = (0, (1.4, 1.4))

def colours(kind):
    """One tint per agent, within its own MEDIUM's colour.

    Hardcoding the bridge ramp was right while every agent was a bridge and
    wrong the moment one was not: on `mixed` it painted two nRF nodes and a
    Wi-Fi node amber, and the figure then said nothing about where each law
    ran -- which is the whole reason the medium palette exists.
    """
    out = {}
    for m in MEDIA_ORDER:
        ids = [i for i, k in enumerate(kind) if k == m]
        for c, i in zip(shades_compact(m, max(len(ids), 1)), ids):
            out[i] = c
    return out


#: The target lines. Grey rather than the agent's own colour: they are the
#: same quantity for both interfaces, so colouring them by agent would add
#: a third meaning to a hue that already carries two.
REF_GREY = "0.55"

#: One width for every line on the figure. Weight is not carrying meaning
#: here -- colour says the agent and the dash says the interface -- so
#: varying it only made some traces look more important than others.
LW = 1.0

utils_mpl.set_global()


def load(run_dir: Path):
    r = load_run(run_dir)
    ids = sorted(r.nodes)
    kap, P, Q, t, kind = [], [], [], None, []
    ref = None
    for i in ids:
        mg = json.loads((run_dir / f"{i}.meta.json").read_text())
        mg = mg["controller"]["microgrid"]
        kap.append(float(mg["plant"]["kappa"]))
        kind.append(r.nodes[i].node_type)
        if float(mg["virtual"]["b"]) != 0.0:
            ref = (float(mg["virtual"]["rP"]), float(mg["virtual"]["rQ"]))
        d = r.nodes[i].data
        P.append(np.asarray(d["state_P"], float))
        Q.append(np.asarray(d["state_Q"], float))
        t = np.asarray(d["timestamp"], float)
    L = min(map(len, P + Q))
    k = np.asarray(kap)[:, None]
    return (t[:L], k * np.vstack([a[:L] for a in P]),
            k * np.vstack([a[:L] for a in Q]), np.asarray(kap), ref, kind)


def panel(runs, coord, ylabel, fname):
    idx = 0 if coord == "P" else 1
    fig, ax = utils_mpl.get_fig(size=(FIG_W, FIG_H), dpi=FIG_DPI)
    col = colours(next(iter(runs.values()))[5])
    ref, kap, t_end = None, None, 0.0
    lo, hi = np.inf, -np.inf

    for arm, (t, P, Q, kap, ref, kind) in runs.items():
        series = P if idx == 0 else Q
        t_end = max(t_end, float(t[-1]))
        for i in range(N):
            ax.step(t, series[i], color=col[i], ls=STYLE.get(arm, DOTTED),
                    lw=LW, zorder=3 if arm in STYLE else 2,
                    label=arm if i == 0 else None)
            lo, hi = min(lo, series[i].min()), max(hi, series[i].max())

    for y in kap * ref[idx]:
        ax.axhline(y, color=REF_GREY, ls="--", lw=LW, zorder=1,
                   label="_nolegend_")
        lo, hi = min(lo, y), max(hi, y)

    span = hi - lo if hi > lo else 1.0
    lo, hi = lo - 0.05 * span, hi + 0.30 * span
    xt = np.linspace(0, t_end, 5)
    yt = np.linspace(lo, hi, 6)
    utils_mpl.set_x_axis(ax, xt, margin=0.0)
    utils_mpl.set_format(ax.xaxis, xt, utils_mpl.make_formatter(".0f"))
    utils_mpl.set_y_axis(ax, yt, margin=0.0)
    utils_mpl.set_format(ax.yaxis, yt, utils_mpl.make_formatter(".1f"))
    ax.set_xlabel("Time [s]")
    ax.set_ylabel(ylabel)
    ax.legend(loc="upper right", fontsize=6.5, ncol=len(runs), framealpha=0.9,
              handlelength=1.8, handletextpad=0.5, labelspacing=0.25,
              columnspacing=1.0, borderpad=0.3)
    ax.minorticks_on()
    utils_mpl.set_grid(fig, ax, major=False, minor=False)
    utils_mpl.save_pdf(fig, str(fname) + ".pdf")
    utils_mpl.save_png(fig, str(fname) + ".png", dpi=FIG_DPI)
    plt.close(fig)
    print(f"  wrote {fname}.pdf + .png")


def main() -> int:
    spec = dict(DEFAULT)
    if len(sys.argv) > 2:
        spec = {}
        for p in sys.argv[1:]:
            d = Path(p)
            name = json.loads((d / "1.meta.json").read_text())["run_name"]
            spec[name.rsplit("-", 1)[-1].replace("plus", "+")] = d
    runs = {}
    for arm, d in spec.items():
        if not d.is_dir():
            print(f"  skip {arm}: {d} missing", file=sys.stderr)
            continue
        runs[arm] = load(d)
    if not runs:
        return 2
    # The proposed interface is drawn last so its solid traces sit on top.
    runs = dict(sorted(runs.items(), key=lambda kv: kv[0] in STYLE))
    out = dg5_paths.arms_dir(next(iter(spec.values())))
    print(f"  interfaces: {', '.join(runs)}")
    stem = dg5_paths.pair_stem(runs)
    panel(runs, "P", "Active power $P_i$", out / f"pair_{stem}_P")
    panel(runs, "Q", "Reactive power $Q_i$", out / f"pair_{stem}_Q")
    return 0


if __name__ == "__main__":
    sys.exit(main())
