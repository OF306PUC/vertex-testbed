"""Publication figures for the five-DG microgrid benchmark.

Five figures, each as .pdf (vector, real LaTeX text), .svg and .png, in
plot_experiments.py's style and at its dimensions:

    dg5_P.svg      physical active power   P_i = kappa_i * x_i^P
    dg5_Q.svg      physical reactive power Q_i = kappa_i * x_i^Q
    dg5_zP.svg     virtual layer, first coordinate,  against the reference
    dg5_zQ.svg     virtual layer, second coordinate, against the reference
    dg5_sum.svg    the fleet total, sum_i P_i and sum_i Q_i

Why those five. The virtual panels show the layer reaching the reference --
one number every node must agree on. The physical panels show what that
means at the plant: `x` is normalised by the DG's rating, so `x_i -> r`
puts each DG at `kappa_i * r`, which is proportional sharing and reads as
five separated levels rather than a consensus. The sum closes it: the
manifest sets `sum_i kappa_i = 1`, so `sum_i P_i -> r^P` exactly, and the
figure shows the fleet meeting total demand rather than five agents agreeing
about a number.

Traces are coloured BY MEDIUM, in tints of that medium's colour, the same
convention as the 30-agent panels -- so a trace says whether that DG's law
ran on an nRF, on a Pi over BLE, or on a socket. At five agents each is also
named, which the 30-agent figures cannot do.

    python3 plotting-tools/plot_dg5.py
    python3 plotting-tools/plot_dg5.py runs/dg5-bridge5-0
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
from palette import (COORD, COORD_DASH, MEDIA_LABEL, MEDIA_ORDER,
                     shades_compact)
from vertex.analysis import load_node

DEFAULT_RUN = ROOT / "runs/dg5-mixed-0"
N = 5
FIG_W, FIG_H, FIG_DPI = 3.5, 2.5, 300

#: One width for every line. Colour already says which agent a trace
#: belongs to, so weight was a second encoding of nothing.
LW = 1.0

#: Compact legend. Five named traces on a 3.5 in axes leave little room, and
#: the legend is an index, not content: it should be readable and small
#: rather than competing with the curves.
LEGEND = dict(fontsize=5.2, framealpha=0.88, handlelength=1.3,
              handletextpad=0.4, labelspacing=0.25, columnspacing=0.9,
              borderpad=0.3, borderaxespad=0.3)

utils_mpl.set_global()


def load(run_dir: Path):
    """Everything the five figures need, from the run and its metadata.

    `kappa` and the reference come from the run's own metadata rather than
    from the manifest on disk: a figure must describe the run it draws, and
    the generator may have moved since.
    """
    t = None
    xP, xQ, zP, zQ, vth, S1, S2, kappa, kind = ([] for _ in range(9))
    true_S = []
    ref, arm = None, None
    for nid in range(1, N + 1):
        n = load_node(run_dir, nid)
        raw = json.loads((run_dir / f"{nid}.meta.json").read_text())
        mg = raw["controller"]["microgrid"]
        kappa.append(float(mg["plant"]["kappa"]))
        # The emulator's true parameter, which the LC estimator is trying to
        # find. It is in the run's own metadata, so the figure can draw the
        # target beside the estimate rather than leaving a reader to look it
        # up in a manifest that may have moved.
        true_S.append((float(mg["plant"]["S1"]), float(mg["plant"]["S2"])))
        kind.append(raw["node_type"])
        arm = mg["interface_kind"]
        # Only the pinned node carries the reference; the others are sent
        # zeros, which is the point of the experiment and not a default.
        if float(mg["virtual"]["b"]) != 0.0:
            ref = (float(mg["virtual"]["rP"]), float(mg["virtual"]["rQ"]))
        d = n.data
        xP.append(np.asarray(d["state_P"], float))
        xQ.append(np.asarray(d["state_Q"], float))
        zP.append(np.asarray(d["vstate_P"], float))
        zQ.append(np.asarray(d["vstate_Q"], float))
        # Only the adaptive interface has one. Under `lc` the interface state
        # is the pair S1, S2, which a relayed node cannot report -- the STATE
        # frame carries the four states and nothing else -- so the figure is
        # skipped rather than drawn from a column that is zero by default.
        vth.append(np.asarray(d["vartheta"], float) if "vartheta" in d else None)
        S1.append(np.asarray(d["S1"], float) if "S1" in d else None)
        S2.append(np.asarray(d["S2"], float) if "S2" in d else None)
        t = np.asarray(d["timestamp"], float)
    if ref is None:
        raise SystemExit(f"{run_dir}: no node carries the reference")
    L = min(map(len, xP + xQ + zP + zQ))
    cut = lambda a: np.vstack([u[:L] for u in a])
    k = np.asarray(kappa)[:, None]
    v = cut(vth) if all(u is not None for u in vth) else None
    S = ((cut(S1), cut(S2)) if all(u is not None for u in S1 + S2) else None)
    return (t[:L], k * cut(xP), k * cut(xQ), cut(zP), cut(zQ), v, S,
            np.asarray(kappa), np.asarray(true_S), kind, arm, ref)


def colours(kind):
    """One tint per agent, within its medium's colour."""
    out = {}
    for m in MEDIA_ORDER:
        ids = [i for i, k in enumerate(kind) if k == m]
        for c, i in zip(shades_compact(m, max(len(ids), 1)), ids):
            out[i] = c
    return out


#: The two prescribed-time deadlines. Drawn on every panel: they are the only
#: structure in the time axis, and a reader checking convergence is checking
#: it against these.
def deadlines(run_dir: Path):
    """`(T_o, T_c)` from the run's own metadata where it has them.

    Runs collected before `T_c` was carried in the assignment recorded it
    only under the `lc` interface, which takes it as a parameter -- so an
    adaptive-arm run has no record of the deadline it was judged against.
    For those, fall back to the manifest the run names, and say so: a figure
    that silently reads a file the run does not point at is a figure that can
    describe a different experiment.
    """
    raw = json.loads((run_dir / "1.meta.json").read_text())
    mg = raw["controller"]["microgrid"]
    T_o = float(mg["virtual"]["T_o"])
    T_c = mg.get("T_c") or mg["interface"].get("T_c")
    if T_c is not None:
        return T_o, float(T_c)
    name = raw.get("manifest_name")
    path = ROOT / "experiments" / f"{name}.yaml"
    if name and path.exists():
        import yaml
        T_c = yaml.safe_load(path.read_text())["controller"]["microgrid"]["T_c"]
        print(f"  T_c not in the run metadata; read {T_c} s from {path.name}")
        return T_o, float(T_c)
    print("  T_c unknown: not in the run and no manifest to fall back to")
    return T_o, 0.0


def mark_deadline(ax, t_s, label, yfrac):
    ax.axvline(t_s, color="0.35", ls=":", lw=1.0, zorder=4)
    ax.annotate(label, xy=(t_s, yfrac), xycoords=("data", "axes fraction"),
                xytext=(5, 0), textcoords="offset points",
                fontsize=8, color="0.35", va="center", ha="left", zorder=6,
                bbox=dict(boxstyle="square,pad=0.15", fc="white", ec="none",
                          alpha=0.85))


def _headroom(lo, hi, legend_loc):
    """Axis limits with room for the legend, on whichever side it sits.

    Five named traces need a five-entry legend, which at this figure size is
    a third of the axes. Opening the limits is better than moving the legend
    out of the frame: these are drawn at 3.5 x 2.5 in for a two-column page,
    and a legend outside the axes eats the width the data needs.
    """
    span = hi - lo if hi > lo else 1.0
    pad = 0.05 * span
    # Asymmetric on purpose. A legend at the top sits over the transient,
    # which is the part of these runs worth reading, so it needs clearing
    # properly; one at the bottom sits over a region the traces have already
    # left, so a smaller opening suffices and the axis does not run off into
    # values the data never takes.
    if legend_loc.startswith("upper"):
        return lo - pad, hi + 0.42 * span
    if legend_loc.startswith("lower"):
        return lo - 0.26 * span, hi + pad
    return lo - pad, hi + pad


def panel(t, series, ylabel, fname, kind, marks, fmt=".1f",
          ref=None, reflabel=None, refs=None, sym="x",
          legend_loc="upper right"):
    """One figure: five traces, coloured by medium and named individually.

    `ref` is one line every agent must reach -- the virtual panels' single
    reference. `refs` is one line PER agent, which is what the physical
    panels need: each DG converges to its own share `kappa_i * r`, so a
    single line would be wrong for four of the five.

    Per-agent references carry no legend entry. Five more entries would
    double the legend to say one thing that the figure already shows by
    every trace landing on its own dashes.
    """
    fig, ax = utils_mpl.get_fig(size=(FIG_W, FIG_H), dpi=FIG_DPI)
    col = colours(kind)
    for i in range(N):
        ax.step(t, series[i], color=col[i], lw=LW, zorder=2,
                label=rf"${sym}_{{{i+1}}}$ ({MEDIA_LABEL[kind[i]]})")
    if refs is not None:
        for i, y in enumerate(refs):
            ax.axhline(y, color=col[i], ls="--", lw=LW, zorder=1,
                       label="_nolegend_")
    if ref is not None:
        ax.axhline(ref, color="k", ls="--", lw=LW, zorder=3, label=reflabel)
    for t_s, lbl, yf in marks:
        mark_deadline(ax, t_s, lbl, yf)
    lo, hi = float(series.min()), float(series.max())
    if ref is not None:
        lo, hi = min(lo, ref), max(hi, ref)
    if refs is not None:
        lo, hi = min(lo, float(min(refs))), max(hi, float(max(refs)))
    lo, hi = _headroom(lo, hi, legend_loc)
    xticks = np.linspace(0, float(t[-1]), 5)
    yticks = np.linspace(lo, hi, 6)
    utils_mpl.set_x_axis(ax, xticks, margin=0.0)
    utils_mpl.set_format(ax.xaxis, xticks, utils_mpl.make_formatter(".0f"))
    utils_mpl.set_y_axis(ax, yticks, margin=0.0)
    utils_mpl.set_format(ax.yaxis, yticks, utils_mpl.make_formatter(fmt))
    ax.set_xlabel("Time [s]")
    ax.set_ylabel(ylabel)
    ax.legend(loc=legend_loc, **LEGEND, ncol=2)
    ax.minorticks_on()
    utils_mpl.set_grid(fig, ax, major=False, minor=False)
    _write(fig, fname)


def sum_panel(t, P, Q, ref, fname, marks):
    """The fleet total against the reference it must meet.

    `sum_i kappa_i = 1` by construction, so the two totals land exactly on
    the reference pair rather than on some scaled version of it -- which is
    what makes this readable as demand being met.
    """
    fig, ax = utils_mpl.get_fig(size=(FIG_W, FIG_H), dpi=FIG_DPI)
    sP, sQ = P.sum(axis=0), Q.sum(axis=0)
    # Both solid. The only dashed lines on this figure are the references,
    # and a dashed trace beside them reads as a second reference.
    ax.step(t, sP, color=COORD["P"], lw=LW, zorder=3, label=r"$\sum_i P_i$")
    ax.step(t, sQ, color=COORD["Q"], lw=LW, zorder=3, label=r"$\sum_i Q_i$")
    ax.axhline(ref[0], color="k", ls="--", lw=LW, zorder=2,
               label=rf"$r^P = {ref[0]:.0f}$, $r^Q = {ref[1]:.0f}$")
    ax.axhline(ref[1], color="k", ls="--", lw=LW, zorder=2)
    for t_s, lbl, yf in marks:
        mark_deadline(ax, t_s, lbl, yf)
    lo = min(sP.min(), sQ.min(), ref[0], ref[1])
    hi = max(sP.max(), sQ.max(), ref[0], ref[1])
    lo, hi = _headroom(lo, hi, "lower right")
    xticks = np.linspace(0, float(t[-1]), 5)
    yticks = np.linspace(lo, hi, 6)
    utils_mpl.set_x_axis(ax, xticks, margin=0.0)
    utils_mpl.set_format(ax.xaxis, xticks, utils_mpl.make_formatter(".0f"))
    utils_mpl.set_y_axis(ax, yticks, margin=0.0)
    utils_mpl.set_format(ax.yaxis, yticks, utils_mpl.make_formatter(".1f"))
    ax.set_xlabel("Time [s]")
    ax.set_ylabel("Fleet total")
    ax.legend(loc="lower right", **LEGEND)
    ax.minorticks_on()
    utils_mpl.set_grid(fig, ax, major=False, minor=False)
    _write(fig, fname)


def _write(fig, fname):
    """PDF for the paper, PNG to look at. No SVG: nothing consumes it."""
    stem = str(fname).replace(".svg", "")
    utils_mpl.save_pdf(fig, stem + ".pdf")
    utils_mpl.save_png(fig, stem + ".png", dpi=FIG_DPI)
    plt.close(fig)
    print(f"  wrote {stem}.pdf + .png")


def main() -> int:
    run = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_RUN
    if not run.is_dir():
        print(f"missing {run}", file=sys.stderr)
        return 2
    t, P, Q, zP, zQ, v, S, kappa, true_S, kind, arm, ref = load(run)
    T_o, T_c = deadlines(run)
    # T_o is the virtual layer's deadline and T_c the physical one, so the
    # virtual panels carry only the first. Drawing both everywhere would put
    # a mark on an axis where nothing is required to happen at it.
    # The deadlines are not drawn for now: they were asked for and then
    # asked off again, and the panels read as trajectories without them.
    # `deadlines()` still resolves them because the metrics need T_c.
    virt_marks = marks = []

    out = dg5_paths.run_dir(run)
    print(f"  {run.name}: kappa {np.round(kappa, 4).tolist()} "
          f"(sum {kappa.sum():.3f}), reference {ref}")

    # sec:reporting item 1 asks for P_i/kappa_i and Q_i/kappa_i, which are
    # the NORMALISED states: every DG converges to the same number, and the
    # figure reads as a consensus. The physical panels below are the same
    # data multiplied by kappa_i, where the same convergence reads as five
    # separated levels -- proportional sharing. Both are wanted and they are
    # not interchangeable, so both are drawn.
    xP, xQ = P / kappa[:, None], Q / kappa[:, None]
    panel(t, xP, r"$P_i/\kappa_i$", out / "dg5_xP.svg", kind, marks,
          fmt=".0f", ref=ref[0], reflabel=rf"$r^P = {ref[0]:.0f}$",
          sym=r"P/\kappa", legend_loc="lower right")
    panel(t, xQ, r"$Q_i/\kappa_i$", out / "dg5_xQ.svg", kind, marks,
          fmt=".0f", ref=ref[1], reflabel=rf"$r^Q = {ref[1]:.0f}$",
          sym=r"Q/\kappa", legend_loc="lower right")
    panel(t, P, "Active power $P_i$", out / "dg5_P.svg", kind, marks,
          refs=kappa * ref[0], sym="P")
    panel(t, Q, "Reactive power $Q_i$", out / "dg5_Q.svg", kind, marks,
          refs=kappa * ref[1], sym="Q")
    panel(t, zP, "Virtual state $z_i^P$", out / "dg5_zP.svg", kind,
          virt_marks,
          fmt=".0f", ref=ref[0], reflabel=rf"$r^P = {ref[0]:.0f}$", sym="z^P",
          legend_loc="lower right")
    panel(t, zQ, "Virtual state $z_i^Q$", out / "dg5_zQ.svg", kind,
          virt_marks,
          fmt=".0f", ref=ref[1], reflabel=rf"$r^Q = {ref[1]:.0f}$", sym="z^Q",
          legend_loc="lower right")
    sum_panel(t, P, Q, ref, out / "dg5_sum.svg", marks)
    # The LC estimator. NO line at the true S_i, deliberately.
    #
    # The Lyapunov argument gives sigma -> 0 and S-tilde BOUNDED; nothing in
    # the source claims parameter convergence, and it would need persistency
    # of excitation, which the design actively removes: the adaptation is
    # driven by Gamma R^T sigma and the prescribed-time gain crushes sigma.
    # The better it tracks, the less it learns, and the estimate settling at
    # a fraction of S_i is the correct behaviour rather than a shortfall.
    # Drawing the true value as a target invites a reader to see a failure
    # that the law never promised to avoid.
    #
    # sec:metrics asks for the NORM ||S_hat_i|| for these arms, so that is
    # drawn too, and separately from vartheta: the same sentence warns
    # against identifying the two.
    if S is not None:
        for c, (Sc, name) in enumerate(zip(S, ("S_1", "S_2"))):
            panel(t, Sc, rf"Estimate $\hat{{{name}}}_i$",
                  out / f"dg5_{name.replace('_', '')}.svg", kind, marks,
                  fmt=".2f", sym=rf"\hat{{{name}}}", legend_loc="lower right")
        panel(t, np.hypot(S[0], S[1]), r"$\|\hat S_i\|_2$",
              out / "dg5_Snorm.svg", kind, marks, fmt=".2f",
              sym=r"\|\hat S\|", legend_loc="lower right")
    if arm == "adaptive" and v is not None:
        panel(t, v, r"Adaptive gain $\vartheta_i$", out / "dg5_vartheta.svg",
              kind, marks, fmt=".2f", sym=r"\vartheta",
              legend_loc="lower right")
    else:
        print(f"  no vartheta figure: the {arm!r} interface has none -- its "
              f"state is (S1, S2), which a relayed node does not report")
    return 0


if __name__ == "__main__":
    sys.exit(main())
