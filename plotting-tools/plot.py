"""
plot_results.py – Publication-quality plots for FTRAC experimental results.

Supports two data sources (controlled by USE_CSV):
  - CSV files:  one file per agent, columns [t, x, z, vartheta]
  - Simulation: pass numpy arrays directly from simulate_discrete()

Edit the CONFIG section below to match your experiment.
"""

# %%
import os
import numpy as np
import matplotlib.pyplot as plt
import utils_mpl

# =========================================================
# CONFIG  ← edit here
# =========================================================

TOPOLOGY     = "clusters"           # used for subdirectory and file names
NUM_AGENTS   = 30
DISCRETE_EXP = True             # True  → single threshold δ (discrete)
                                 # False → two thresholds ε₁, ε₂ (continuous)

# Time window for full plots (seconds)
T_MAX = 2000

# Zoom window for detail plots
ZOOM_TMIN = 800
ZOOM_TMAX = 1200
if TOPOLOGY == "clusters":
    ZOOM_TMIN = 1600
    ZOOM_TMAX = 2000

# Zoom window for error detail plot
ERR_ZOOM_TMIN = 0
ERR_ZOOM_TMAX = 1000
if TOPOLOGY == "clusters":
    ERR_ZOOM_TMIN = 1000
    ERR_ZOOM_TMAX = 1600
ERR_ZOOM_MAX  = 0.05

# Hysteresis thresholds
EPSILON_1 = 0.01   # δ  (discrete) or ε_off (continuous)
EPSILON_2 = 0.05   # ε̄  (continuous only)

# Reference agent (1-based)
REF_AGENT = 1

# Output directory
RENDER_DIR = f"render/{TOPOLOGY}"

# Figure dimensions for SVG → PDF export.
# Each figure is placed as \begin{subfigure}{0.49\linewidth} inside a
# figure* environment, so the rendered width is 0.49 × 7.16 in ≈ 3.5 in.
# Generate at that exact size so font/line weights look correct in the paper.
FIG_W      = 3.5    # 0.49 × IEEEtran \textwidth (7.16 in) [in]
FIG_H      = 2.5    # height for main plots [in]
FIG_H_ZOOM = 2.1    # height for zoom / detail plots [in]
FIG_DPI    = 300

# ---- Data source ----
USE_CSV  = True
CSV_DIR  = f"{TOPOLOGY}-{NUM_AGENTS}nodes-csv"

# If USE_CSV is False, assign simulation arrays here (shape: n_agents × n_points):
#   t_arr       : 1-D time vector
#   x_arr       : (n_agents, n_points)
#   z_arr       : (n_agents, n_points)
#   vartheta_arr: (n_agents, n_points)
t_arr        = None
x_arr        = None
z_arr        = None
vartheta_arr = None

# Color map for agent traces
COLOR_MAP = "turbo"

# =========================================================
# GLOBAL STYLE
# =========================================================

utils_mpl.set_global()

try:
    sampled = [plt.colormaps[COLOR_MAP](v) for v in np.linspace(0.0, 1.0, NUM_AGENTS)]
    plt.rcParams["axes.prop_cycle"] = plt.cycler(color=sampled)
except Exception:
    cmap = plt.cm.get_cmap(COLOR_MAP, NUM_AGENTS)
    plt.rcParams["axes.prop_cycle"] = plt.cycler(color=[cmap(i) for i in range(NUM_AGENTS)])

os.makedirs(RENDER_DIR, exist_ok=True)

# =========================================================
# LOAD / PREPARE DATA
# =========================================================

if USE_CSV:
    t         = []
    x         = []
    z         = []
    vartheta  = []

    max_t = []
    min_t = []
    max_x = []
    min_x = []
    max_z = []
    min_z = []
    max_vartheta = []
    min_vartheta = []

    for i in range(NUM_AGENTS):
        node_id  = i + 1
        csv_data = np.loadtxt(f"{CSV_DIR}/node_{node_id}.csv", delimiter=",", skiprows=1)
        t.append(csv_data[:, 0])
        x.append(csv_data[:, 1])
        z.append(csv_data[:, 2])
        vartheta.append(csv_data[:, 3])

        max_t.append(np.max(csv_data[:, 0]))
        min_t.append(np.min(csv_data[:, 0]))
        max_x.append(np.max(csv_data[:, 1]))
        min_x.append(np.min(csv_data[:, 1]))
        max_z.append(np.max(csv_data[:, 2]))
        min_z.append(np.min(csv_data[:, 2]))
        max_vartheta.append(np.max(csv_data[:, 3]))
        min_vartheta.append(np.min(csv_data[:, 3]))

else:
    # Build per-agent lists from simulation arrays
    assert t_arr is not None, "Assign t_arr, x_arr, z_arr, vartheta_arr before USE_CSV=False"
    t        = [t_arr        for _ in range(NUM_AGENTS)]
    x        = [x_arr[i]    for i in range(NUM_AGENTS)]
    z        = [z_arr[i]    for i in range(NUM_AGENTS)]
    vartheta = [vartheta_arr[i] for i in range(NUM_AGENTS)]

    max_t = [np.max(t_arr)]  * NUM_AGENTS
    min_t = [np.min(t_arr)]  * NUM_AGENTS
    max_x = [np.max(r) for r in x]
    min_x = [np.min(r) for r in x]
    max_z = [np.max(r) for r in z]
    min_z = [np.min(r) for r in z]
    max_vartheta = [np.max(r) for r in vartheta]
    min_vartheta = [np.min(r) for r in vartheta]

# ---- Derived bounds ----
maxt = min(np.max(max_t), T_MAX)
mint = np.min(min_t)
maxx = np.max(max_x)
minx = np.min(min_x)
maxz = np.max(max_z)
minz = np.min(min_z)
maxvartheta = np.max(max_vartheta)
minvartheta = np.min(min_vartheta)

z_avg = np.mean([z[i][0] for i in range(NUM_AGENTS)])

ref = REF_AGENT - 1   # 0-based index

xticks          = np.linspace(0, maxt, 5)
yticks_x        = np.linspace(minx * 1.5, maxx * 1.05, 6)
yticks_z        = np.linspace(minz * 1.5, maxz * 1.05, 6)
yticks_vartheta = np.linspace(minvartheta if minvartheta <= 0 else 0, maxvartheta * 1.05, 6)

# ---- Absolute tracking error ----
abs_error = [np.abs(x[i] - z[i]) for i in range(NUM_AGENTS)]
min_abs_error = np.min([np.min(e) for e in abs_error])
max_abs_error = np.max([np.max(e) for e in abs_error])

# Zoom tick arrays (reused across several panels)
xticks_zoom  = np.linspace(ZOOM_TMIN, ZOOM_TMAX, 3)
if TOPOLOGY == "dring": 
    yticks_zoom_x = np.linspace(z_avg - 0.5 * EPSILON_1, z_avg + 2 * EPSILON_1, 3)
    yticks_zoom_z = np.linspace(z_avg - 0.5 * EPSILON_1, z_avg + 1.5 * EPSILON_1, 3)
if TOPOLOGY == "ring":
    yticks_zoom_x  = np.linspace(z_avg - 200 * EPSILON_1, z_avg + 40 * EPSILON_1, 3)
    yticks_zoom_z = np.linspace(z_avg - 160 * EPSILON_1, z_avg + 40 * EPSILON_1, 3)
if TOPOLOGY == "clusters":
    yticks_zoom_x  = np.linspace(z_avg - 25 * EPSILON_1, z_avg - 20 * EPSILON_1, 3)
    yticks_zoom_z = np.linspace(z_avg - 25 * EPSILON_1, z_avg + 5 * EPSILON_1, 3)


# =========================================================
# HELPER: is this a clusters topology?
# =========================================================
def _is_clusters() -> bool:
    return "clusters" in TOPOLOGY


# =========================================================
# PLOT 1 – States  x_i
# =========================================================

# %% States
fig, ax = utils_mpl.get_fig(size=(FIG_W, FIG_H), dpi=FIG_DPI)

for i in range(NUM_AGENTS):
    lbl = f"$x_{{{i+1}}}$" if (_is_clusters() and i in (20, 29)) else None
    ax.step(t[i], x[i], lw=1.0, label=lbl)

ax.step(t[ref], z[ref], "k--", lw=1.5, label=f"$z_{{{REF_AGENT}}}$ (ref.)")

utils_mpl.set_x_axis(ax, xticks, margin=0.0)
utils_mpl.set_format(ax.xaxis, xticks, utils_mpl.make_formatter(".0f"))
utils_mpl.set_y_axis(ax, yticks_x, margin=0.0)
utils_mpl.set_format(ax.yaxis, yticks_x, utils_mpl.make_formatter(".0f"))

ax.set_xlabel("Time [s]")
ax.set_ylabel("States")
ax.legend(loc="upper right", fontsize=10)
ax.minorticks_on()
utils_mpl.set_grid(fig, ax, major=False, minor=False)

utils_mpl.save_svg(fig, f"{RENDER_DIR}/{TOPOLOGY}_x.svg")
plt.show()


# =========================================================
# PLOT 2 – States zoom
# =========================================================

# % States zoom
fig, ax = utils_mpl.get_fig(size=(FIG_W, FIG_H_ZOOM), dpi=FIG_DPI)

for i in range(NUM_AGENTS):
    ax.step(t[i], x[i], lw=0.75)
ax.step(t[ref], z[ref], "k--", lw=1.75)

utils_mpl.set_x_axis(ax, xticks_zoom, margin=0.0)
utils_mpl.set_format(ax.xaxis, xticks_zoom, utils_mpl.make_formatter(".0f"))
utils_mpl.set_y_axis(ax, yticks_zoom_x, margin=0.0)
utils_mpl.set_format(ax.yaxis, yticks_zoom_x, utils_mpl.make_formatter(".2f"))  
utils_mpl.remove_axes(ax)

ax.minorticks_on()
utils_mpl.set_grid(fig, ax, major=False, minor=False)

utils_mpl.save_svg(fig, f"{RENDER_DIR}/{TOPOLOGY}_states_zoomed.svg")
plt.show()

# =========================================================
# PLOT 3 – Virtual states  z_i
# =========================================================

# % Virtual states
fig, ax = utils_mpl.get_fig(size=(FIG_W, FIG_H), dpi=FIG_DPI)

for i in range(NUM_AGENTS):
    lbl = f"$z_{{{i+1}}}$" if (_is_clusters() and i in (20, 29)) else None
    ax.step(t[i], z[i], lw=1.0, label=lbl)

ax.step(t[0], z_avg * np.ones_like(t[0]), "k-.", lw=1.5,
        label=f"$\\bar{{z}} = {z_avg:.2f}$")

utils_mpl.set_x_axis(ax, xticks, margin=0.0)
utils_mpl.set_format(ax.xaxis, xticks, utils_mpl.make_formatter(".0f"))
utils_mpl.set_y_axis(ax, yticks_z, margin=0.0)
utils_mpl.set_format(ax.yaxis, yticks_z, utils_mpl.make_formatter(".0f"))

ax.set_xlabel("Time [s]")
ax.set_ylabel("Virtual States")
ax.legend(loc="upper right", fontsize=10)
ax.minorticks_on()
utils_mpl.set_grid(fig, ax, major=False, minor=False)

utils_mpl.save_svg(fig, f"{RENDER_DIR}/{TOPOLOGY}_z.svg")
plt.show()

# =========================================================
# PLOT 4 – Virtual states zoom
# =========================================================

# % Virtual states zoom
fig, ax = utils_mpl.get_fig(size=(FIG_W, FIG_H_ZOOM), dpi=FIG_DPI)

for i in range(NUM_AGENTS):
    if TOPOLOGY == "clusters": 
        ax.step(t[i], z[i], lw=1.25)
    else:
        ax.step(t[i], z[i], lw=0.75)
ax.step(t[0], z_avg * np.ones_like(t[0]), "k-.", lw=1.75)

utils_mpl.set_x_axis(ax, xticks_zoom, margin=0.0)
utils_mpl.set_format(ax.xaxis, xticks_zoom, utils_mpl.make_formatter(".0f"))
utils_mpl.set_y_axis(ax, yticks_zoom_z, margin=0.0)
utils_mpl.set_format(ax.yaxis, yticks_zoom_z, utils_mpl.make_formatter(".2f"))
utils_mpl.remove_axes(ax)

ax.minorticks_on()
utils_mpl.set_grid(fig, ax, major=False, minor=False)

utils_mpl.save_svg(fig, f"{RENDER_DIR}/{TOPOLOGY}_vstates_zoomed.svg")
plt.show()

# =========================================================
# PLOT 5 – Adaptive gains  ϑ_i
# =========================================================

# % Adaptive gains
fig, ax = utils_mpl.get_fig(size=(FIG_W, FIG_H), dpi=FIG_DPI)

for i in range(NUM_AGENTS):
    lbl = f"$\\vartheta_{{{i+1}}}$" if (_is_clusters() and i in (20, 29)) else None
    ax.step(t[i], vartheta[i], lw=1.0, label=lbl)

utils_mpl.set_x_axis(ax, xticks, margin=0.0)
utils_mpl.set_format(ax.xaxis, xticks, utils_mpl.make_formatter(".0f"))
utils_mpl.set_y_axis(ax, yticks_vartheta, margin=0.0)
utils_mpl.set_format(ax.yaxis, yticks_vartheta, utils_mpl.make_formatter(".4f"))

ax.set_xlabel("Time [s]")
ax.set_ylabel("Adaptive Gains")
if _is_clusters():
    ax.legend(loc="upper left", fontsize=10)

ax.minorticks_on()
utils_mpl.set_grid(fig, ax, major=False, minor=False)

utils_mpl.save_svg(fig, f"{RENDER_DIR}/{TOPOLOGY}_vartheta.svg")
plt.show()

# =========================================================
# PLOT 6 – Absolute tracking error  |σ_i|  (with zoom inset)
# =========================================================
# %%
xticks_err   = np.linspace(mint, maxt, 5)
yticks_error = np.linspace(-0.01, max_abs_error * 1.005, 5)

xticks_err_zoom = np.linspace(ERR_ZOOM_TMIN, ERR_ZOOM_TMAX, 3)
yticks_err_zoom = np.linspace(0.0, ERR_ZOOM_MAX, 3)

fig, ax = utils_mpl.get_fig(size=(FIG_W, FIG_H), dpi=FIG_DPI)

# ── Main plot ──────────────────────────────────────────────
for i in range(NUM_AGENTS):
    lbl = f"$|\\sigma_{{{i+1}}}|$" if (_is_clusters() and i in (20, 29)) else None
    ax.step(t[i], abs_error[i], lw=1.0, label=lbl)

if DISCRETE_EXP:
    ax.step(t[ref], EPSILON_1 * np.ones_like(t[ref]), "r-.", lw=1.25, label=r"$\delta$")
else:
    ax.step(t[ref], EPSILON_1 * np.ones_like(t[ref]), "k--", lw=1.25, label=r"$\varepsilon_1$")
    ax.step(t[ref], EPSILON_2 * np.ones_like(t[ref]), "r-.", lw=1.25, label=r"$\varepsilon_2$")

utils_mpl.set_x_axis(ax, xticks_err, margin=0.0)
utils_mpl.set_format(ax.xaxis, xticks_err, utils_mpl.make_formatter(".0f"))
utils_mpl.set_y_axis(ax, yticks_error, margin=0.0)
utils_mpl.set_format(ax.yaxis, yticks_error, utils_mpl.make_formatter(".2f"))

ax.set_xlabel("Time [s]")
ax.set_ylabel("Abs. tracking error")
ax.legend(loc="upper right", fontsize=10)
ax.minorticks_on()
utils_mpl.set_grid(fig, ax, major=False, minor=False)

# ── Zoom inset ─────────────────────────────────────────────
# [left, bottom, width, height] in axes-fraction coordinates
# Adjust these four values to reposition / resize the inset
ax_inset = ax.inset_axes([0.70, 0.40, 0.27, 0.20])

for i in range(NUM_AGENTS):
    ax_inset.step(t[i], abs_error[i], lw=0.75)

if DISCRETE_EXP:
    ax_inset.step(t[ref], EPSILON_1 * np.ones_like(t[ref]), "r-.", lw=1.5)
else:
    ax_inset.step(t[ref], EPSILON_1 * np.ones_like(t[ref]), "k--", lw=1.5)
    ax_inset.step(t[ref], EPSILON_2 * np.ones_like(t[ref]), "r-.", lw=1.5)

utils_mpl.set_x_axis(ax_inset, xticks_err_zoom, margin=0.0)
utils_mpl.set_format(ax_inset.xaxis, xticks_err_zoom, utils_mpl.make_formatter(".0f"))
utils_mpl.set_y_axis(ax_inset, yticks_err_zoom, margin=0.0)
utils_mpl.set_format(ax_inset.yaxis, yticks_err_zoom, utils_mpl.make_formatter(".2f"))
#utils_mpl.remove_axes(ax_inset)

ax_inset.minorticks_on()
utils_mpl.set_grid(fig, ax_inset, major=False, minor=True)
ax_inset.tick_params(labelsize=7)

# ── Connector lines from inset to the zoomed region ────────
# ax.indicate_inset_zoom(ax_inset, edgecolor="gray", alpha=0.6)
from matplotlib.patches import ConnectionPatch

xlim = ax_inset.get_xlim()
ylim = ax_inset.get_ylim()
zoom_center = ((xlim[0]+xlim[1])/2, (ylim[0]+ylim[1])/2)

con = ConnectionPatch(
    xyA=(0.5, 0.0),      coordsA=ax_inset.transAxes,  # bottom-center of inset
    xyB=zoom_center,     coordsB=ax.transData,          # center of zoom box
    arrowstyle="->", mutation_scale=8,
    color="gray", lw=0.8, linestyle="dashed",
)
fig.add_artist(con)

utils_mpl.save_svg(fig, f"{RENDER_DIR}/{TOPOLOGY}_abs_error.svg")
plt.show()