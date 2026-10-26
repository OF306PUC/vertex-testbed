"""
summary_grid.py – Per-metric summary figures for the paper.

Three figures, one per metric:
    Figure 1 – MSE          (z & x side-by-side bars)
    Figure 2 – T_conv       (bars, worst-agent line annotated by index)
    Figure 3 – Packet loss  (bars)

Each figure has one subplot per topology arranged in a row.

Edit TOPOLOGIES and CONFIG to match your experiment.
"""

# %%
import numpy as np
import matplotlib.pyplot as plt
from matplotlib.patches import Patch
import utils_mpl

# =========================================================
# CONFIG
# =========================================================

TOPOLOGIES = [
    ("dring",    30),
    ("ring",     30),
    ("clusters", 30),
]

Ts         = 1       # nominal sampling period [s]
T_MAX      = 2000    # hard time-axis cap [s]
N_TAIL     = 100     # tail samples used for steady-state MSE
DELTA_BAND = 0.01    # convergence threshold δ

COLOR_Z    = "#2166ac"   # MSE(z) bars
COLOR_X    = "#d6604d"   # MSE(x) bars

# Communication-type colors (used in T_conv and packet-loss figures)
TYPE_COLORS = {
    "ble":    "#4393c3",   # bluish
    "wifi":   "#4d9a6f",   # green
    "bridge": "#d4860b",   # amber
}

# Agent-to-type mapping (1-based).  All 30-node topologies share the same
# partition: BLE=1..10, WiFi=11..20, Bridge=21..30 (from net.js).
def agent_type(agent_id: int) -> str:
    if agent_id <= 10:
        return "ble"
    elif agent_id <= 20:
        return "wifi"
    return "bridge"

TOPO_LABELS = {
    "dring":    r"$\mathcal{G}_1$",
    "ring":     r"$\mathcal{G}_2$",
    "clusters": r"$\mathcal{G}_3(t)$",
}

RENDER_DIR = "render"

# Figure dimensions for SVG → PDF export.
# Width should match the intended print width (inches):
#   IEEE two-column full-width : 7.16
#   Elsevier two-column full   : 7.00
#   Single column              : 3.50
# DPI = 72 is correct for SVG: vectors are resolution-independent and
# converters (Inkscape, rsvg-convert) use the viewport size, not DPI,
# to determine the PDF page size.
FIG_W  = 7.00   # IEEEtran journal, letterpaper \textwidth
FIG_H  = 5.0   # total figure height for the 3-row grid [in] — tune this in LaTeX
FIG_DPI = 72

# =========================================================
# GLOBAL STYLE
# =========================================================

utils_mpl.set_global()

# =========================================================
# HELPER: load CSV data and compute all metrics
# =========================================================

def load_topology(topology: str, num_agents: int) -> dict:
    csv_dir = f"{topology}-{num_agents}nodes-csv"

    t, x, z = [], [], []
    for i in range(num_agents):
        csv_data = np.loadtxt(
            f"{csv_dir}/node_{i + 1}.csv", delimiter=",", skiprows=1
        )
        t.append(csv_data[:, 0])
        x.append(csv_data[:, 1])
        z.append(csv_data[:, 2])

    maxt  = min(np.max([np.max(ti) for ti in t]), T_MAX)
    mint  = np.min([np.min(ti) for ti in t])
    z_avg = np.mean([z[i][0] for i in range(num_agents)])

    def tail_by_time(values, timestamps):
        mask = timestamps.astype(int) >= int(maxt) - N_TAIL
        return values[mask]

    # ── MSE ───────────────────────────────────────────────
    mse_z_per = np.array(
        [np.mean((tail_by_time(z[i], t[i]) - z_avg) ** 2) for i in range(num_agents)]
    )
    mse_x_per = np.array(
        [np.mean((tail_by_time(x[i], t[i]) - z_avg) ** 2) for i in range(num_agents)]
    )

    # ── Activation time (for switched agents that start constant) ─
    # Detected as the last timestamp before the state first changes.
    ACT_THRESHOLD = 1e-4   # min |Δx| considered a real state change
    act_time_per = []
    for i in range(num_agents):
        dx   = np.abs(np.diff(x[i]))
        idxs = np.where(dx > ACT_THRESHOLD)[0]
        if len(idxs) == 0 or idxs[0] == 0:
            act_time_per.append(mint)          # active from the start
        else:
            act_time_per.append(t[i][idxs[0] - 1])   # last still-constant sample
    act_time_per = np.array(act_time_per)

    # ── Convergence time ──────────────────────────────────
    abs_error = [np.abs(x[i] - z[i]) for i in range(num_agents)]
    conv_time_per = []
    for i in range(num_agents):
        inside     = abs_error[i] <= DELTA_BAND
        violations = np.where(~inside)[0]
        if len(violations) == 0:
            conv_time_per.append(mint)
        else:
            idx = int(violations[-1])
            conv_time_per.append(t[i][idx] if idx < len(t[i]) else maxt)
    conv_time_per = np.array(conv_time_per)

    # ── Packet loss ───────────────────────────────────────
    gap_count = []
    for i in range(num_agents):
        ti_m = t[i][t[i] <= maxt]
        gaps = int(np.sum(np.diff(ti_m) > 2 * Ts)) if len(ti_m) > 1 else 0
        gap_count.append(gaps)
    gap_count = np.array(gap_count)
    loss_pct  = gap_count / (maxt / Ts) * 100.0

    return {
        "maxt":          maxt,
        "mse_z_per":     mse_z_per,
        "mse_x_per":     mse_x_per,
        "conv_time_per": conv_time_per,
        "act_time_per":  act_time_per,
        "loss_pct":      loss_pct,
        "num_agents":    num_agents,
    }

# =========================================================
# LOAD ALL TOPOLOGIES
# =========================================================

data = {topo: load_topology(topo, n) for topo, n in TOPOLOGIES}

# =========================================================
# SINGLE 3×3 FIGURE  (rows: MSE | T_conv | loss,  cols: topologies)
# =========================================================

# %%
N_TOPO = len(TOPOLOGIES)
ROW_MSE   = 0
ROW_TCONV = 1
ROW_LOSS  = 2

fig, axes = plt.subplots(
    3, N_TOPO,
    figsize=(FIG_W, FIG_H), dpi=FIG_DPI,
    constrained_layout=True,
)

for col, (topo, _) in enumerate(TOPOLOGIES):
    d         = data[topo]
    n         = d["num_agents"]
    agent_idx = np.arange(1, n + 1)
    xtick_ag  = np.arange(5, n + 1, 5)
    xbounds   = np.array([1, n])

    # ── ROW 0: MSE ────────────────────────────────────────
    ax = axes[ROW_MSE, col]

    mse_z_per = d["mse_z_per"]
    mse_x_per = d["mse_x_per"]
    bw  = 0.42
    mag = max(mse_z_per.max(), mse_x_per.max(), 1e-30)
    dec = max(0, -int(np.floor(np.log10(mag))) + 1)
    fmt = f".{dec}f"

    ax.bar(agent_idx - bw/2, mse_z_per, width=bw, color=COLOR_Z, zorder=2, label=r"$z_i$")
    ax.bar(agent_idx + bw/2, mse_x_per, width=bw, color=COLOR_X, zorder=2, label=r"$x_i$")

    ytk = np.linspace(0, mag * 1.1, 4)
    utils_mpl.set_x_axis(ax, xbounds, margin=0.02)
    utils_mpl.set_format(ax.xaxis, xtick_ag, utils_mpl.make_formatter(".0f"))
    utils_mpl.set_y_axis(ax, ytk, margin=0.0)
    utils_mpl.set_format(ax.yaxis, ytk, utils_mpl.make_formatter(fmt))
    ax.set_title(TOPO_LABELS[topo], fontsize=10)
    if col == 0:
        ax.set_ylabel("MSE", fontsize=9)
    ax.tick_params(labelsize=8)
    ax.minorticks_on()
    utils_mpl.set_grid(fig, ax, major=False, minor=True)
    ax.legend(fontsize=7, loc="upper right")

    # ── ROW 1: T_conv ─────────────────────────────────────
    ax = axes[ROW_TCONV, col]

    conv_time_per = d["conv_time_per"]
    act_time_per  = d["act_time_per"]
    maxt          = d["maxt"]
    worst_idx     = int(np.argmax(conv_time_per))
    type_colors   = [TYPE_COLORS[agent_type(i)] for i in agent_idx]

    ACT_MIN  = 10.0
    inactive = act_time_per - act_time_per.min()
    late     = inactive > ACT_MIN

    base_h = np.where(late, act_time_per, conv_time_per)
    base_c = ["0.78" if late[i] else type_colors[i] for i in range(n)]
    ax.bar(agent_idx, base_h, width=0.7, color=base_c, zorder=2)
    top_h = np.where(late, conv_time_per - act_time_per, 0)
    ax.bar(agent_idx, top_h, width=0.7, bottom=np.where(late, act_time_per, 0),
           color=type_colors, zorder=3)
    ax.axhline(conv_time_per[worst_idx], color="k", linestyle="-.", lw=1.0, zorder=3)

    ytk = np.linspace(0, maxt, 5)
    utils_mpl.set_x_axis(ax, xbounds, margin=0.02)
    utils_mpl.set_format(ax.xaxis, xtick_ag, utils_mpl.make_formatter(".0f"))
    utils_mpl.set_y_axis(ax, ytk, margin=0.0)
    utils_mpl.set_format(ax.yaxis, ytk, utils_mpl.make_formatter(".0f"))
    if col == 0:
        ax.set_ylabel(r"$T_{\rm conv}$ [s]", fontsize=9)
    ax.tick_params(labelsize=8)
    ax.minorticks_on()
    utils_mpl.set_grid(fig, ax, major=False, minor=True)

    legend_els = [
        plt.Line2D([0], [0], color="k", linestyle="-.", lw=1.0,
                   label=rf"$T_{{\rm conv}}(x_{{{worst_idx + 1}}})$"),
    ]
    if late.any():
        t_sw = float(np.mean(act_time_per[late]))
        ax.axhline(t_sw, color="0.35", linestyle="--", lw=1.0, zorder=4)
        legend_els.append(Patch(facecolor="0.78", label="node disabled"))
    ax.legend(handles=legend_els, fontsize=7, loc="upper right")

    # ── ROW 2: Packet loss ────────────────────────────────
    ax = axes[ROW_LOSS, col]

    loss_pct    = d["loss_pct"]
    type_colors = [TYPE_COLORS[agent_type(i)] for i in agent_idx]

    ax.bar(agent_idx, loss_pct, width=0.7, color=type_colors, zorder=2)

    ytk = np.linspace(0, max(loss_pct.max() * 1.1, 0.1), 4)
    utils_mpl.set_x_axis(ax, xbounds, margin=0.02)
    utils_mpl.set_format(ax.xaxis, xtick_ag, utils_mpl.make_formatter(".0f"))
    utils_mpl.set_y_axis(ax, ytk, margin=0.0)
    utils_mpl.set_format(ax.yaxis, ytk, utils_mpl.make_formatter(".2f"))
    ax.set_xlabel("Agent", fontsize=9)
    if col == 0:
        ax.set_ylabel(r"Packet loss [\%]", fontsize=9)
        legend_els = [
            Patch(facecolor=TYPE_COLORS["ble"],    label="BLE"),
            Patch(facecolor=TYPE_COLORS["wifi"],   label="WiFi"),
            Patch(facecolor=TYPE_COLORS["bridge"], label="Bridge"),
        ]
        ax.legend(handles=legend_els, fontsize=7, loc="upper right")
    ax.tick_params(labelsize=8)
    ax.minorticks_on()
    utils_mpl.set_grid(fig, ax, major=False, minor=True)

utils_mpl.save_svg(fig, f"{RENDER_DIR}/summary.svg")
plt.show()
