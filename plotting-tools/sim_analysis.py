# %%
import numpy as np
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
import utils_mpl

# =========================================================
# CONFIG  ← edit here
# =========================================================

TOPOLOGY   = "clusters"
NUM_AGENTS = 30

Ts    = 1
T_MAX = 2000

COLOR_MAP  = "turbo"

N_TAIL     = 100
DELTA_BAND = 0.01

CSV_DIR    = f"{TOPOLOGY}-{NUM_AGENTS}nodes-csv"

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

# =========================================================
# LOAD / PREPARE DATA
# =========================================================

t        = []
x        = []
z        = []
vartheta = []

max_t        = []
min_t        = []
max_x        = []
min_x        = []
max_z        = []
min_z        = []
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

maxt        = min(np.max(max_t), T_MAX)
mint        = np.min(min_t)
maxx        = np.max(max_x)
minx        = np.min(min_x)
maxz        = np.max(max_z)
minz        = np.min(min_z)
maxvartheta = np.max(max_vartheta)
minvartheta = np.min(min_vartheta)

z_avg     = np.mean([z[i][0] for i in range(NUM_AGENTS)])
abs_error = [np.abs(x[i] - z[i]) for i in range(NUM_AGENTS)]

# =========================================================
# HELPER
# =========================================================

def tail(arr, n=N_TAIL):
    return arr[-n:] if len(arr) >= n else arr

def tail_by_time(values, timestamps, maxt, n_tail=N_TAIL):
    """
    Select samples in the last n_tail seconds using int(timestamp) as
    the discrete-time index (valid because Ts = 1 s).
    """
    mask = timestamps.astype(int) >= int(maxt) - n_tail
    return values[mask]

# =========================================================
# PACKET LOSS ANALYSIS  (computed first — used in plots below)
#   gaps defined as inter-arrival > 2 × Ts
# =========================================================

gap_count = []   # number of gaps per agent
gap_locs  = []   # list of gap midpoint timestamps per agent

for i in range(NUM_AGENTS):
    ti   = t[i]
    mask = ti <= maxt
    ti_m = ti[mask]
    mids = []
    if len(ti_m) > 1:
        dt      = np.diff(ti_m)
        gap_idx = np.where(dt > 2 * Ts)[0]
        for g in gap_idx:
            mids.append(0.5 * (ti_m[g] + ti_m[g + 1]))
    gap_count.append(len(mids))
    gap_locs.append(mids)

gap_count        = np.array(gap_count)
worst_loss_agent = int(np.argmax(gap_count))   # 0-based
worst_loss_count = gap_count[worst_loss_agent]

print(f"[{TOPOLOGY}] Packet loss — worst agent: "
      f"{worst_loss_agent + 1}  ({worst_loss_count} gaps)")

# =========================================================
# METRIC 1 — MSE of z per agent  (consensus)
# =========================================================

z_tails   = [tail_by_time(z[i], t[i], maxt) for i in range(NUM_AGENTS)]
mse_z_per = np.array([np.mean((zt - z_avg) ** 2) for zt in z_tails])
mse_z     = float(np.mean(mse_z_per))

print(f"[{TOPOLOGY}] MSE of z  — mean: {mse_z:.6e} "
      f"  max: {mse_z_per.max():.6e} (agent {mse_z_per.argmax()+1})")

# =========================================================
# METRIC 2 — MSE of x per agent  (tracking)
# =========================================================

x_tails    = [tail_by_time(x[i], t[i], maxt) for i in range(NUM_AGENTS)]
mse_x_per  = np.array([np.mean((xt - z_avg) ** 2) for xt in x_tails])
mse_x_vs_z = float(np.mean(mse_x_per))

print(f"[{TOPOLOGY}] MSE of x  — mean: {mse_x_vs_z:.6e} "
      f"  max: {mse_x_per.max():.6e} (agent {mse_x_per.argmax()+1})")

# =========================================================
# METRIC 3 — Convergence time per agent + worst-case
# =========================================================

inside = [abs_error[i] <= DELTA_BAND for i in range(NUM_AGENTS)]

conv_time_per_agent = []
last_viol_idx       = []

for i in range(NUM_AGENTS):
    violations = np.where(~inside[i])[0]
    if len(violations) == 0:
        last_viol_idx.append(0)
        conv_time_per_agent.append(mint)
    else:
        idx = int(violations[-1])
        last_viol_idx.append(idx)
        conv_time_per_agent.append(t[i][idx] if idx < len(t[i]) else maxt)

conv_time_per_agent = np.array(conv_time_per_agent)
worst_conv_agent    = int(np.argmax(conv_time_per_agent))
conv_time           = conv_time_per_agent[worst_conv_agent]

print(f"[{TOPOLOGY}] Conv. time (δ={DELTA_BAND}): {conv_time:.2f} s"
      f"  —  worst agent: {worst_conv_agent + 1}")

# =========================================================
# PLOT A — MSE per agent  (z and x, side by side bars)
# =========================================================

agent_idx    = np.arange(1, NUM_AGENTS + 1)
xticks_mse   = np.arange(1, NUM_AGENTS + 1, max(1, NUM_AGENTS // 6))
bar_w        = 0.42

fig, ax = utils_mpl.get_fig(size=(4.2, 2.5), dpi=300)

# Scale both MSE series by the same power of 10 so ticks are plain decimals
_exp_ab  = int(np.floor(np.log10(max(mse_z_per.max(), mse_x_per.max(), 1e-30))))
_sc_ab   = 10 ** (-_exp_ab)

bars_z = ax.bar(agent_idx - bar_w/2, mse_z_per * _sc_ab, width=bar_w,
                label=r"$\mathrm{MSE}(z_i)$", zorder=2)
bars_x = ax.bar(agent_idx + bar_w/2, mse_x_per * _sc_ab, width=bar_w,
                label=r"$\mathrm{MSE}(x_i)$", zorder=2)

# Highlight worst-loss agent on both bars
bars_z[worst_loss_agent].set_edgecolor("red")
bars_z[worst_loss_agent].set_linewidth(1.0)
bars_x[worst_loss_agent].set_edgecolor("red")
bars_x[worst_loss_agent].set_linewidth(1.0)

# Mean reference lines
ax.axhline(mse_z * _sc_ab, color=bars_z[0].get_facecolor(),
           linestyle="--", lw=1.0, zorder=3)
ax.axhline(mse_x_vs_z * _sc_ab, color=bars_x[0].get_facecolor(),
           linestyle="--", lw=1.0, zorder=3)

yticks_mse = np.linspace(0, max(mse_z_per.max(), mse_x_per.max()) * _sc_ab * 1.05, 5)
utils_mpl.set_x_axis(ax, xticks_mse, margin=0.02)
utils_mpl.set_format(ax.xaxis, xticks_mse, utils_mpl.make_formatter(".0f"))
utils_mpl.set_y_axis(ax, yticks_mse, margin=0.0)
utils_mpl.set_format(ax.yaxis, yticks_mse, utils_mpl.make_formatter(".2f"))

ax.set_xlabel("Agent index")
ax.set_ylabel(rf"MSE")

legend_els = [
    Line2D([0], [0], color=bars_z[0].get_facecolor(),
           lw=4, label=r"$\mathrm{MSE}(z_i)$"),
    Line2D([0], [0], color=bars_x[0].get_facecolor(),
           lw=4, label=r"$\mathrm{MSE}(x_i)$"),
    Line2D([0], [0], color="none", marker="s", markerfacecolor="none",
           markeredgecolor="red", markersize=6,
           label=f"most loss: agent {worst_loss_agent + 1} ({worst_loss_count} gaps)"),
]
ax.legend(handles=legend_els, fontsize=7, loc="upper right")
ax.minorticks_on()
utils_mpl.set_grid(fig, ax, major=False, minor=True)
# utils_mpl.save_svg(fig, f"{RENDER_DIR}/{TOPOLOGY}_mse_per_agent.svg")
plt.show()

# =========================================================
# PLOT B — T_conv per agent
# =========================================================

xticks_conv = np.arange(1, NUM_AGENTS + 1, max(1, NUM_AGENTS // 6))
yticks_conv = np.linspace(0, maxt, 5)

fig, ax = utils_mpl.get_fig(size=(4.2, 2.5), dpi=300)

bars_conv = ax.bar(agent_idx, conv_time_per_agent, width=0.7, zorder=2)
bars_conv[worst_loss_agent].set_edgecolor("red")
bars_conv[worst_loss_agent].set_linewidth(1.0)

ax.axhline(conv_time, color="r", linestyle="-.", lw=1.25, zorder=3,
           label=f"worst conv.: agent {worst_conv_agent + 1}  ({conv_time:.0f} s)")

utils_mpl.set_x_axis(ax, xticks_conv, margin=0.02)
utils_mpl.set_format(ax.xaxis, xticks_conv, utils_mpl.make_formatter(".0f"))
utils_mpl.set_y_axis(ax, yticks_conv, margin=0.0)
utils_mpl.set_format(ax.yaxis, yticks_conv, utils_mpl.make_formatter(".0f"))

ax.set_xlabel("Agent index")
ax.set_ylabel(r"$T_{\mathrm{conv}}$ [s]")

legend_els = [
    Line2D([0], [0], color="r", linestyle="-.", lw=1.25,
           label=f"worst conv.: agent {worst_conv_agent + 1} ({conv_time:.0f} s)"),
    Line2D([0], [0], color="none", marker="s", markerfacecolor="none",
           markeredgecolor="red", markersize=6,
           label=f"most loss: agent {worst_loss_agent + 1} ({worst_loss_count} gaps)"),
]
ax.legend(handles=legend_els, fontsize=7, loc="upper right")
ax.minorticks_on()
utils_mpl.set_grid(fig, ax, major=False, minor=True)
# utils_mpl.save_svg(fig, f"{RENDER_DIR}/{TOPOLOGY}_tconv_per_agent.svg")
plt.show()

# =========================================================
# PLOT C — Timestamp scatter  (time vs agent index)
# =========================================================

xticks_ts = np.linspace(mint, maxt, 5)
yticks_ts = np.arange(1, NUM_AGENTS + 1, max(1, NUM_AGENTS // 6))

fig, ax = utils_mpl.get_fig(size=(4.2, 2.9), dpi=300)

for i in range(NUM_AGENTS):
    ti_m = t[i][t[i] <= maxt]
    ax.scatter(ti_m, np.full(ti_m.size, i + 1),
               s=0.4, linewidths=0, alpha=0.6, zorder=2)
    for mid in gap_locs[i]:
        ax.scatter(mid, i + 1, s=6, marker="|", color="red",
                   linewidths=0.8, alpha=0.85, zorder=3)

utils_mpl.set_x_axis(ax, xticks_ts, margin=0.0)
utils_mpl.set_format(ax.xaxis, xticks_ts, utils_mpl.make_formatter(".0f"))
utils_mpl.set_y_axis(ax, yticks_ts, margin=0.02)
utils_mpl.set_format(ax.yaxis, yticks_ts, utils_mpl.make_formatter(".0f"))

ax.set_xlabel("Time [s]")
ax.set_ylabel("Agent index")
ax.minorticks_on()
utils_mpl.set_grid(fig, ax, major=False, minor=True)

legend_els = [
    Line2D([0], [0], marker="o", color="none", markerfacecolor="gray",
           markersize=3, label="received"),
    Line2D([0], [0], marker="|", color="red", markersize=5,
           markeredgewidth=0.8,
           label=f"gap — worst: agent {worst_loss_agent + 1} ({worst_loss_count})"),
]
ax.legend(handles=legend_els, fontsize=7, loc="upper right")
# utils_mpl.save_svg(fig, f"{RENDER_DIR}/{TOPOLOGY}_timestamps_scatter.svg")
plt.show()

# =========================================================
# PLOT D — Clock drift  (sample index vs agent timestamp)
# =========================================================

n_common  = min(len(t[i]) for i in range(NUM_AGENTS))
k_axis    = np.arange(n_common)
t_ideal   = mint + k_axis * Ts

xticks_cd = np.linspace(0, n_common, 5).astype(int)
yticks_cd = np.linspace(mint, maxt, 5)

fig, ax = utils_mpl.get_fig(size=(4.2, 2.9), dpi=300)

for i in range(NUM_AGENTS):
    ax.plot(k_axis, t[i][:n_common], lw=0.5, alpha=0.7)

ax.plot(k_axis, t_ideal, "k--", lw=1.2, zorder=10,
        label=r"ideal ($k \cdot T_s$)")

utils_mpl.set_x_axis(ax, xticks_cd, margin=0.0)
utils_mpl.set_format(ax.xaxis, xticks_cd, utils_mpl.make_formatter(".0f"))
utils_mpl.set_y_axis(ax, yticks_cd, margin=0.0)
utils_mpl.set_format(ax.yaxis, yticks_cd, utils_mpl.make_formatter(".0f"))

ax.set_xlabel("Sample index $k$")
ax.set_ylabel("Agent timestamp [s]")
ax.minorticks_on()
utils_mpl.set_grid(fig, ax, major=False, minor=True)

legend_els = [
    Line2D([0], [0], color="k", linestyle="--", lw=1.2,
           label=r"ideal ($k \cdot T_s$)"),
    Line2D([0], [0], color="none", marker="s", markerfacecolor="none",
           markeredgecolor="red", markersize=6,
           label=f"most loss: agent {worst_loss_agent + 1} ({worst_loss_count} gaps)"),
]
ax.legend(handles=legend_els, fontsize=7, loc="upper left")
# utils_mpl.save_svg(fig, f"{RENDER_DIR}/{TOPOLOGY}_clock_drift.svg")
plt.show()

# =========================================================
# PACKET LOSS RATE per agent  (used in Plots E & F)
#   loss_rate[i] = gaps / total expected packets
# =========================================================

expected_total  = maxt / Ts
loss_rate       = gap_count / expected_total          # fraction [0, 1]
loss_pct        = loss_rate * 100.0                   # percentage

# Colormap: white (0% loss) → dark red (max loss)
_loss_norm   = plt.Normalize(vmin=0, vmax=max(loss_pct.max(), 1e-9))
_loss_cmap   = plt.cm.YlOrRd
_bar_colors  = [_loss_cmap(_loss_norm(p)) for p in loss_pct]

# =========================================================
# PLOT E — Packet-loss heatmap  (agent × time bins)
#   Color = fraction of packets received; gaps appear as dark cells
# =========================================================

N_BINS     = 40
bin_edges  = np.linspace(mint, maxt, N_BINS + 1)
bin_centers = 0.5 * (bin_edges[:-1] + bin_edges[1:])
bin_width  = bin_edges[1] - bin_edges[0]
expected_per_bin = bin_width / Ts          # expected packets per bin

heatmap = np.zeros((NUM_AGENTS, N_BINS))
for i in range(NUM_AGENTS):
    ti_m = t[i][t[i] <= maxt]
    counts, _ = np.histogram(ti_m, bins=bin_edges)
    heatmap[i] = np.clip(counts / max(expected_per_bin, 1), 0, 1)

fig, ax = utils_mpl.get_fig(size=(4.2, 2.9), dpi=300)

im = ax.imshow(
    heatmap,
    aspect="auto",
    origin="lower",
    extent=[mint, maxt, 0.5, NUM_AGENTS + 0.5],
    cmap="RdYlGn",          # green = received, red = dropped
    vmin=0, vmax=1,
    interpolation="nearest",
)

cbar = fig.colorbar(im, ax=ax, fraction=0.03, pad=0.01)
cbar.set_label("Rx fraction", fontsize=8)
cbar.ax.tick_params(labelsize=7)

xticks_hm = np.linspace(mint, maxt, 5)
yticks_hm = np.arange(1, NUM_AGENTS + 1, max(1, NUM_AGENTS // 6))
utils_mpl.set_x_axis(ax, xticks_hm, margin=0.0)
utils_mpl.set_format(ax.xaxis, xticks_hm, utils_mpl.make_formatter(".0f"))
utils_mpl.set_y_axis(ax, yticks_hm, margin=0.0)
utils_mpl.set_format(ax.yaxis, yticks_hm, utils_mpl.make_formatter(".0f"))

ax.set_xlabel("Time [s]")
ax.set_ylabel("Agent index")
ax.minorticks_off()
# utils_mpl.save_svg(fig, f"{RENDER_DIR}/{TOPOLOGY}_pkt_heatmap.svg")
plt.show()

# =========================================================
# PLOT F — 2×2 summary panel: MSE-z | MSE-x | T_conv | loss rate
#   Bars colored by per-agent packet loss rate (shared colormap)
# =========================================================

agent_idx = np.arange(1, NUM_AGENTS + 1)
xtick_ag  = np.arange(1, NUM_AGENTS + 1, max(1, NUM_AGENTS // 6))

# Independent per-subplot MSE scales so ticks are plain decimals
_exp_z  = int(np.floor(np.log10(max(mse_z_per.max(), 1e-30))))
_sc_z   = 10 ** (-_exp_z)
_exp_x  = int(np.floor(np.log10(max(mse_x_per.max(), 1e-30))))
_sc_x   = 10 ** (-_exp_x)

fig, axes = plt.subplots(2, 2, figsize=(8.4, 5.0), dpi=300,
                         constrained_layout=True)

# ── top-left: MSE(z) ──────────────────────────────────────
ax0 = axes[0, 0]
ax0.bar(agent_idx, mse_z_per * _sc_z, width=0.7, color=_bar_colors, zorder=2)
ax0.axhline(mse_z * _sc_z, color="k", linestyle="--", lw=1.0, zorder=3)
ytk = np.linspace(0, mse_z_per.max() * _sc_z * 1.1, 4)
utils_mpl.set_x_axis(ax0, xtick_ag, margin=0.02)
utils_mpl.set_format(ax0.xaxis, xtick_ag, utils_mpl.make_formatter(".0f"))
utils_mpl.set_y_axis(ax0, ytk, margin=0.0)
utils_mpl.set_format(ax0.yaxis, ytk, utils_mpl.make_formatter(".2f"))
ax0.set_xlabel("Agent", fontsize=8)
ax0.set_ylabel(rf"MSE$(z_i)$ ", fontsize=8)
ax0.tick_params(labelsize=7)
ax0.minorticks_on()
utils_mpl.set_grid(fig, ax0, major=False, minor=True)

# ── top-right: MSE(x) ─────────────────────────────────────
ax1 = axes[0, 1]
ax1.bar(agent_idx, mse_x_per * _sc_x, width=0.7, color=_bar_colors, zorder=2)
ax1.axhline(mse_x_vs_z * _sc_x, color="k", linestyle="--", lw=1.0, zorder=3)
ytk = np.linspace(0, mse_x_per.max() * _sc_x * 1.1, 4)
utils_mpl.set_x_axis(ax1, xtick_ag, margin=0.02)
utils_mpl.set_format(ax1.xaxis, xtick_ag, utils_mpl.make_formatter(".0f"))
utils_mpl.set_y_axis(ax1, ytk, margin=0.0)
utils_mpl.set_format(ax1.yaxis, ytk, utils_mpl.make_formatter(".2f"))
ax1.set_xlabel("Agent", fontsize=8)
ax1.set_ylabel(rf"MSE$(x_i)$ ", fontsize=8)
ax1.tick_params(labelsize=7)
ax1.minorticks_on()
utils_mpl.set_grid(fig, ax1, major=False, minor=True)

# ── bottom-left: T_conv ───────────────────────────────────
ax2 = axes[1, 0]
ax2.bar(agent_idx, conv_time_per_agent, width=0.7, color=_bar_colors, zorder=2)
ax2.axhline(conv_time, color="r", linestyle="-.", lw=1.0, zorder=3)
ytk = np.linspace(0, maxt, 5)
utils_mpl.set_x_axis(ax2, xtick_ag, margin=0.02)
utils_mpl.set_format(ax2.xaxis, xtick_ag, utils_mpl.make_formatter(".0f"))
utils_mpl.set_y_axis(ax2, ytk, margin=0.0)
utils_mpl.set_format(ax2.yaxis, ytk, utils_mpl.make_formatter(".0f"))
ax2.set_xlabel("Agent", fontsize=8)
ax2.set_ylabel(r"$T_{\rm conv}$ [s]", fontsize=8)
ax2.tick_params(labelsize=7)
ax2.minorticks_on()
utils_mpl.set_grid(fig, ax2, major=False, minor=True)

# ── bottom-right: packet loss rate (%) ────────────────────
ax3 = axes[1, 1]
ax3.bar(agent_idx, loss_pct, width=0.7, color=_bar_colors, zorder=2)
ytk = np.linspace(0, max(loss_pct.max() * 1.1, 0.1), 4)
utils_mpl.set_x_axis(ax3, xtick_ag, margin=0.02)
utils_mpl.set_format(ax3.xaxis, xtick_ag, utils_mpl.make_formatter(".0f"))
utils_mpl.set_y_axis(ax3, ytk, margin=0.0)
utils_mpl.set_format(ax3.yaxis, ytk, utils_mpl.make_formatter(".2f"))
ax3.set_xlabel("Agent", fontsize=8)
ax3.set_ylabel("Packet loss [\\%]", fontsize=8)
ax3.tick_params(labelsize=7)
ax3.minorticks_on()
utils_mpl.set_grid(fig, ax3, major=False, minor=True)

# ── shared colorbar on the right ──────────────────────────
sm   = plt.cm.ScalarMappable(cmap=_loss_cmap, norm=_loss_norm)
sm.set_array([])
cbar = fig.colorbar(sm, ax=axes.ravel().tolist(), fraction=0.02, pad=0.02)
cbar.set_label("Packet loss [\\%]", fontsize=8)
cbar.ax.tick_params(labelsize=7)

# utils_mpl.save_svg(fig, f"{RENDER_DIR}/{TOPOLOGY}_summary_panel.svg")
plt.show()

# =========================================================
# LaTeX TABLE  (copy-paste into paper)
# =========================================================

def print_latex_table():
    """Print a LaTeX tabular block: MSE(x_i), T_conv, Loss % per agent."""
    # Scale MSE(x) so ticks are plain decimals
    _tx = int(np.floor(np.log10(max(mse_x_per.max(), 1e-30))))
    _sx = 10 ** (-_tx)
    hdr_x = f"$\\mathrm{{MSE}}(x_i)\\,[\\times 10^{{{_tx}}}]$"

    col_fmt = "r r@{\\,} r@{\\,} r"
    print()
    print("% ── paste into your paper ─────────────────────────")
    print(r"\begin{table}[t]")
    print(r"  \centering")
    print(f"  \\caption{{Per-agent performance summary ({TOPOLOGY}, {NUM_AGENTS} agents)}}")
    print(r"  \label{tab:peragent}")
    print(f"  \\begin{{tabular}}{{{col_fmt}}}")
    print(r"    \toprule")
    print(f"    Agent & {hdr_x} & $T_{{\\rm conv}}$ [s] & Loss [\\%] \\\\")
    print(r"    \midrule")
    for i in range(NUM_AGENTS):
        ag = i + 1
        marker = r" $\star$" if (ag == worst_loss_agent + 1
                                  or ag == worst_conv_agent + 1) else ""
        print(f"    {ag}{marker} & {mse_x_per[i] * _sx:.2f}"
              f" & {conv_time_per_agent[i]:.0f}"
              f" & {loss_pct[i]:.2f} \\\\")
    print(r"    \bottomrule")
    print(r"  \end{tabular}")
    print(r"\end{table}")
    print("% ───────────────────────────────────────────────────")

print_latex_table()

# =========================================================
# SUMMARY PRINT
# =========================================================

print()
print(f"{'─'*50}")
print(f"  Topology : {TOPOLOGY}")
print(f"  Agents   : {NUM_AGENTS}  |  tail samples: {N_TAIL}")
print(f"  z_avg    : {z_avg:.4f}  (mean of z_i at t=0)")
print(f"{'─'*50}")
print(f"  MSE z    — mean : {mse_z:.6e}")
print(f"           — max  : {mse_z_per.max():.6e}  (agent {mse_z_per.argmax()+1})")
print(f"  MSE x    — mean : {mse_x_vs_z:.6e}")
print(f"           — max  : {mse_x_per.max():.6e}  (agent {mse_x_per.argmax()+1})")
print(f"  T_conv   : {conv_time:.2f} s  (agent {worst_conv_agent + 1} last to converge)")
print(f"  Pkt loss : {worst_loss_count} gaps  (agent {worst_loss_agent + 1})")
print(f"{'─'*50}")
print(f"  Per-agent detail:")
print(f"  {'idx':>4}  {'MSE z':>12}  {'MSE x':>12}  {'T_conv [s]':>12}  {'gaps':>6}")
print(f"  {'─'*4}  {'─'*12}  {'─'*12}  {'─'*12}  {'─'*6}")
for i in range(NUM_AGENTS):
    print(f"  {i+1:>4}  {mse_z_per[i]:>12.4e}  {mse_x_per[i]:>12.4e}"
          f"  {conv_time_per_agent[i]:>12.2f}  {gap_count[i]:>6}")
print(f"{'─'*50}")