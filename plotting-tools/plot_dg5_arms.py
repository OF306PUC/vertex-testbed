"""The arm comparison: the figures the five-DG benchmark exists to produce.

One run per arm of the same manifest, drawn on shared axes:

    arms_Ez.svg      E_z(t), the virtual layer, against its gate
    arms_Ex.svg      E_x(t), physical tracking, against its band
    arms_split.svg   the same tracking error split by WHERE the law ran

Arms are coloured from `palette.CONTROLLER`, which shares no hue with the
medium palette for exactly this reason: a per-agent panel colours by medium
and this colours by arm, and both appear in the same document.

The third figure is not decoration. `arms_Ex` reports the worst node, which
a fleet-wide maximum cannot distinguish from a fleet that is uniformly worse.
Splitting the same quantity by where the law ran is the platform's own
question -- single precision on an nRF against double precision on a host --
and it is the only figure here that can answer it.

    python3 plotting-tools/plot_dg5_arms.py
    python3 plotting-tools/plot_dg5_arms.py runs/dg5-mixed-0-AA runs/...
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
from palette import (CONTROLLER, CONTROLLER_DASH, CONTROLLER_LW,
                     CONTROLLER_Z)
from plot_dg5 import deadlines
from vertex.analysis.effort import applied_command
from vertex.analysis.load import load_run

#: Run directory per arm. The manifest's own arm has no suffix, which is what
#: `--arm` omits when the run name is not overridden.
DEFAULT = {"DZ": ROOT / "runs/dg5-mixed-0",
           "AA": ROOT / "runs/dg5-mixed-0-AA",
           "LC+": ROOT / "runs/dg5-mixed-0-LCplus"}
FIG_W, FIG_H, FIG_DPI = 3.5, 2.5, 300

#: sec:metrics. E_z is gated at delta_x/4; E_x must enter the band delta_x.
DELTA_X = 0.45
E_Z_GATE = DELTA_X / 4

utils_mpl.set_global()

#: Where the law ran, for the split figure. `ble` is the nRF in float32;
#: `wifi` and `bridge` are both the Pi in float64, and the split is on that
#: boundary rather than on the three media, because the question here is the
#: arithmetic and not the radio.
ON_NRF, ON_PI = "nRF (float32)", "Pi (float64)"


def load(run_dir: Path):
    """`(t, E_z, E_x, per-node |sigma|, where each node ran, T_o, T_c)`."""
    r = load_run(run_dir)
    ids = sorted(r.nodes)
    # Shared with plot_dg5: the run's metadata where it has T_c, else the
    # manifest it names. It must be T_c and not T_o. Measured at T_o the
    # pinned node is still some 28 away and converging -- which is what the
    # interval between the two deadlines is for -- so E_x read there reports
    # 28.6 for an arm that settles at 0.005, and reports it silently.
    T_o, T_c = deadlines(run_dir)
    ref = None
    for i in ids:
        v = json.loads((run_dir / f"{i}.meta.json").read_text())
        v = v["controller"]["microgrid"]["virtual"]
        if float(v["b"]) != 0.0:
            ref = (float(v["rP"]), float(v["rQ"]))

    cols, where, t, gain, mu, kap = [], {}, None, [], [], []
    for i in ids:
        d = r.nodes[i].data
        # The interface's own state, whatever this arm calls it: the adaptive
        # gain under AA/DZ, the first estimator component under LC. They are
        # not the same quantity and the figure says so in its label -- what
        # they share is being the thing the interface accumulates, which is
        # where these arms differ and E_x does not.
        gain.append(np.asarray(d["vartheta" if "vartheta" in d else "S1"],
                               float))
        # Rebuilt where the node does not report it. Taking only the nodes
        # that log `muapp` summed three agents of five on the heterogeneous
        # assignment and drew the result as the fleet's total variation,
        # which is a partial sum presented as a complete one. There is no
        # partial case left: every node either reports the command or has it
        # reconstructed from its own states.
        mu.append(applied_command(run_dir, i))
        t = np.asarray(d["timestamp"], float)
        cols.append([np.asarray(d[k], float) for k in
                     ("state_P", "state_Q", "vstate_P", "vstate_Q")])
        kap.append(float(json.loads((run_dir / f"{i}.meta.json").read_text())
                         ["controller"]["microgrid"]["plant"]["kappa"]))
        where[i] = ON_NRF if r.nodes[i].node_type == "ble" else ON_PI
    L = min(len(c[0]) for c in cols)
    t = t[:L]
    Ez = np.max([np.abs(c[2][:L] - ref[0]) + np.abs(c[3][:L] - ref[1])
                 for c in cols], axis=0)
    sig = {i: np.abs(c[0][:L] - c[2][:L]) + np.abs(c[1][:L] - c[3][:L])
           for i, c in zip(ids, cols)}
    Ex = np.max(np.vstack(list(sig.values())), axis=0)
    G = np.mean(np.vstack([g[:L] for g in gain]), axis=0)
    Es = np.max([np.abs(c[0][:L] - c[2][:L]) + np.abs(c[1][:L] - c[3][:L])
                 for c in cols], axis=0)
    # Cumulative total variation of the APPLIED command, eq:control_tv. The
    # question sec:reporting asks is whether a drifting gain changes what the
    # plant actually receives, and only the applied -- post-saturation --
    # command answers it.
    # eq:control_tv sums the per-agent variation, not the variation of the
    # sum: two agents moving oppositely cancel in the second and must not.
    # Cropped to L first, because the nodes do not log the same row count.
    # `applied_command` has already dropped each node's first row, so these
    # are one shorter than the state columns and crop to L - 1.
    Lm = L - 1
    TV = np.cumsum(sum(np.abs(np.diff(m[:Lm], axis=0)).sum(1) for m in mu))
    # The fleet's peak demand at each instant, the worst node and not the
    # mean: `arms_Ex` reports the worst node for the same reason, and an
    # interface that is cheap on four agents and saturating on the fifth has
    # not been cheap.
    MU = np.max([np.hypot(m[:Lm, 0], m[:Lm, 1]) for m in mu], axis=0)
    # The physical powers: P_i = kappa_i * x_i^P, summed over the fleet.
    # sum_i kappa_i = 1 by construction, so the total lands on the reference
    # itself and the four arms are directly comparable against one line.
    k = np.asarray(kap)
    sP = np.sum([k[j] * c[0][:L] for j, c in enumerate(cols)], axis=0)
    sQ = np.sum([k[j] * c[1][:L] for j, c in enumerate(cols)], axis=0)
    return (t, Ez, Ex, sig, where, T_o, (float(T_c) if T_c else None), G,
            Es, TV, sP, sQ, ref, MU)


def mark(ax, t_s, label, yfrac):
    ax.axvline(t_s, color="0.35", ls=":", lw=1.0, zorder=4)
    ax.annotate(label, xy=(t_s, yfrac), xycoords=("data", "axes fraction"),
                xytext=(5, 0), textcoords="offset points", fontsize=8,
                color="0.35", va="center", ha="left", zorder=6,
                bbox=dict(boxstyle="square,pad=0.15", fc="white", ec="none",
                          alpha=0.85))


def finish(fig, ax, t_end, ylabel, marks, stem, out, legend="upper right"):
    for t_s, lbl, yf in marks:
        mark(ax, t_s, lbl, yf)
    xt = np.linspace(0, t_end, 5)
    utils_mpl.set_x_axis(ax, xt, margin=0.0)
    utils_mpl.set_format(ax.xaxis, xt, utils_mpl.make_formatter(".0f"))
    ax.set_xlabel("Time [s]")
    ax.set_ylabel(ylabel)
    ax.legend(loc=legend, fontsize=6.2, framealpha=0.88, handlelength=1.8,
              handletextpad=0.5, labelspacing=0.3, borderpad=0.3)
    ax.minorticks_on()
    utils_mpl.set_grid(fig, ax, major=True, minor=False)
    utils_mpl.save_pdf(fig, str(out / f"{stem}.pdf"))
    utils_mpl.save_png(fig, str(out / f"{stem}.png"), dpi=FIG_DPI)
    plt.close(fig)
    print(f"  wrote {out / stem}.pdf + .png")


#: One width for every line. The palette varies it deliberately -- thick
#: underneath, thin on top, so a coincident pair still reads as two curves,
#: and AA and DZ do coincide over most of the tracking record. Uniform
#: weight gives that job entirely to `CONTROLLER_DASH`: the two remain
#: separable because their dash patterns differ, not because one is drawn
#: fatter. If a coincident pair ever stops being readable, this is the line
#: to revisit.
LW = 1.0


def arm_style(arm):
    return dict(color=CONTROLLER.get(arm, "0.3"),
                ls=CONTROLLER_DASH.get(arm, "solid"),
                lw=LW,
                zorder=CONTROLLER_Z.get(arm, 2))


def main() -> int:
    runs = DEFAULT
    if len(sys.argv) > 1:
        runs = {}
        for a in sys.argv[1:]:
            p = Path(a)
            arm = json.loads((p / "1.meta.json").read_text())["run_name"]
            runs[arm.rsplit("-", 1)[-1].replace("plus", "+")] = p
    data = {}
    for arm, d in runs.items():
        if not d.is_dir():
            print(f"  skip {arm}: {d} missing")
            continue
        data[arm] = load(d)
    if not data:
        print("no runs to draw", file=sys.stderr)
        return 2

    out = dg5_paths.arms_dir(next(iter(runs.values())))
    t_end = max(float(v[0][-1]) for v in data.values())
    T_o = next(iter(data.values()))[5]
    T_c = next(iter(data.values()))[6] or None

    # --- the virtual layer, against its gate ------------------------------
    fig, ax = utils_mpl.get_fig(size=(FIG_W, FIG_H), dpi=FIG_DPI)
    for arm, (t, Ez, *_) in data.items():
        ax.semilogy(t, np.maximum(Ez, 1e-6), label=arm, **arm_style(arm))
    ax.axhline(E_Z_GATE, color="0.55", ls="--", lw=LW, zorder=5,
               label=rf"gate $\delta_x/4 = {E_Z_GATE:.4f}$")
    finish(fig, ax, t_end, r"$E_z(t)$", [], "arms_Ez", out)

    # --- physical tracking, against its band ------------------------------
    fig, ax = utils_mpl.get_fig(size=(FIG_W, FIG_H), dpi=FIG_DPI)
    for arm, (t, _Ez, Ex, *_) in data.items():
        ax.semilogy(t, np.maximum(Ex, 1e-6), label=arm, **arm_style(arm))
    ax.axhline(DELTA_X, color="0.55", ls="--", lw=LW, zorder=5,
               label=rf"band $\delta_x = {DELTA_X}$")
    marks = []
    finish(fig, ax, t_end, r"$E_x(t)$", marks, "arms_Ex", out)

    # --- the same error, split by where the law ran -----------------------
    #
    # Solid for the nRF and dotted for the Pi, inside each arm's own colour:
    # the pair belongs to one arm, and a reader needs to see which arm splits
    # rather than which of ten traces is which.
    fig, ax = utils_mpl.get_fig(size=(FIG_W, FIG_H), dpi=FIG_DPI)
    for arm, (t, _z, _x, sig, where, *_r) in data.items():
        st = arm_style(arm)
        for tag, dash in ((ON_NRF, "solid"), (ON_PI, (0, (1.2, 1.4)))):
            ids = [i for i, w in where.items() if w == tag]
            if not ids:
                continue
            worst = np.max(np.vstack([sig[i] for i in ids]), axis=0)
            ax.semilogy(t, np.maximum(worst, 1e-6), color=st["color"],
                        ls=dash, lw=LW, zorder=st["zorder"],
                        label=f"{arm}, {tag}")
    ax.axhline(DELTA_X, color="0.55", ls="--", lw=LW, zorder=5)
    finish(fig, ax, t_end, r"worst $\|\sigma_i\|_1$", marks,
           "arms_split", out, legend="center right")

    # --- E_sigma, the third of the error triple sec:metrics separates ------
    #
    # E_z and E_x alone cannot tell a virtual-layer limitation from an
    # interface one; E_sigma = max_i ||x_i - z_i|| is the term that does, and
    # sec:reporting asks for all three on one logarithmic axis.
    fig, ax = utils_mpl.get_fig(size=(FIG_W, FIG_H), dpi=FIG_DPI)
    for arm, (t, *_rest) in data.items():
        ax.semilogy(t, np.maximum(_rest[7], 1e-6), label=arm, **arm_style(arm))
    ax.axhline(DELTA_X, color="0.55", ls="--", lw=LW, zorder=5,
               label=rf"$\delta_x = {DELTA_X}$")
    finish(fig, ax, t_end, r"$E_\sigma(t)$", marks, "arms_Esigma", out)

    # --- does a drifting gain change the applied command? ------------------
    #
    # sec:reporting asks this directly. AA's gain ramps without bound and
    # DZ's stops, so if the answer were yes the two total-variation curves
    # would separate. Drawn together on one axes for that reason: the claim
    # is about a difference, and a difference is read from coincidence or
    # its absence, not from two numbers in a table.
    fig, ax = utils_mpl.get_fig(size=(FIG_W, FIG_H), dpi=FIG_DPI)
    for arm, (t, *_rest) in data.items():
        ax.plot(t[1:len(_rest[8]) + 1], _rest[8], label=arm, **arm_style(arm))
    finish(fig, ax, t_end, r"cumulative $\mathrm{TV}_\mu$", marks,
           "arms_tv", out, legend="upper left")

    # --- the applied command itself ---------------------------------------
    #
    # Every interface on one axes, which the AA-against-DZ pair figure cannot
    # do: that one spends its style dimension on two arms and its colour on
    # the five agents, and a third arm has nowhere to go. Here colour is the
    # arm and the fleet is reduced to its worst node, so three or four
    # interfaces compare directly and the saturation ceiling is one line.
    fig, ax = utils_mpl.get_fig(size=(FIG_W, FIG_H), dpi=FIG_DPI)
    for arm, (t, *_rest) in data.items():
        ax.plot(t[1:len(_rest[12]) + 1], _rest[12], label=arm, **arm_style(arm))
    finish(fig, ax, t_end,
           r"$\max_i \|\mu_i^{\mathrm{app}}\|_2$", marks,
           "arms_mu", out, legend="upper right")

    # --- what each interface family accumulates ----------------------------
    #
    # TWO figures, not one. These arms carry different quantities: AA and DZ
    # accumulate the sliding gain vartheta, LC0 and LC+ a parameter estimate
    # S_hat. sec:metrics says to report both and then, in the same sentence,
    # "do not identify them with the sliding gain" -- which is exactly what a
    # shared axes labelled `interface state` invited. They are no longer
    # drawn together.
    for fam, keep, ylab, stem in (
            ("adaptive", ("AA", "DZ"), r"$\vartheta_i$ (fleet mean)",
             "arms_vartheta"),
            ("lc", ("LC0", "LC+"), r"$\hat S_{i,1}$ (fleet mean)",
             "arms_Shat")):
        sel = {a: v for a, v in data.items() if a in keep}
        if not sel:
            continue
        fig, ax = utils_mpl.get_fig(size=(FIG_W, FIG_H), dpi=FIG_DPI)
        for arm, (t, *_rest) in sel.items():
            ax.step(t, _rest[6], label=arm, **arm_style(arm))
        finish(fig, ax, t_end, ylab, marks, stem, out, legend="upper left")

    # --- the physical response, and the comparison between arms ------------
    #
    # Per-agent trajectories are in the per-run panels; here the question is
    # which arm delivers the demand, so the fleet total is the quantity and
    # the four arms share one axes against one reference line.
    for idx, (name, sym) in enumerate(((10, r"\sum_i P_i"), (11, r"\sum_i Q_i"))):
        fig, ax = utils_mpl.get_fig(size=(FIG_W, FIG_H), dpi=FIG_DPI)
        for arm, (t, *_rest) in data.items():
            ax.step(t, _rest[name - 1], label=arm, **arm_style(arm))
        r0 = next(iter(data.values()))[12][idx]
        ax.axhline(r0, color="0.55", ls="--", lw=LW, zorder=5,
                   label=rf"$r^{{{'P' if idx == 0 else 'Q'}}} = {r0:.0f}$")
        finish(fig, ax, t_end, rf"${sym}$", marks,
               f"arms_sum{'P' if idx == 0 else 'Q'}", out,
               legend="lower right")

    print()
    print(f"  {'arm':<6}{'worst E_z after T_o':>22}{'worst E_x after T_c':>22}")
    for arm, (t, Ez, Ex, *_rest) in data.items():
        # Every sample from the deadline, not a subsample of them: a maximum
        # taken on a coarse grid steps over exactly the excursions it exists
        # to report.
        i_o = int(np.searchsorted(t, T_o))
        i_c = int(np.searchsorted(t, T_c)) if T_c else None
        if i_c is None:
            print(f"  {arm:<6} no T_c: E_x window undefined")
            continue
        g = "PASS" if Ez[i_o:].max() <= E_Z_GATE else "FAIL"
        b = "PASS" if Ex[i_c:].max() <= DELTA_X else "FAIL"
        print(f"  {arm:<6}{Ez[i_o:].max():>16.5f} {g:<5}"
              f"{Ex[i_c:].max():>16.5f} {b:<5}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
