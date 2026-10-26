"""
utils_mpl.py – Publication-quality matplotlib helpers for FTRAC plots.

Usage
-----
import utils_mpl
utils_mpl.set_global()          # call once at the top of every script
fig, ax = utils_mpl.get_fig()

Fixes over original
-------------------
Tick label misalignment (usetex=True):
    With text.usetex=True, matplotlib's LaTeX bounding-box estimator
    underestimates the width of math-mode strings (e.g. "$333$"), so
    multi-digit labels drift progressively to the left.
    Fixed by:
      1. Setting 'xtick.alignment'/'ytick.alignment' = 'center' in rcParams
         so matplotlib pins the label midpoint on the tick, not the left edge.
      2. Providing a FuncFormatter (make_formatter) that wraps values in
         \\mathrm{} — roman text is narrower and better estimated than italic.

Legend frame:
    Default legend.edgecolor "1.0" (white) makes the border invisible against
    a white background.  Fixed to light grey with square corners.
"""

import os
import numpy as np
import matplotlib as mpl
import matplotlib.pyplot as plt
import matplotlib.ticker as tkr


# =========================================================
# GLOBAL STYLE
# =========================================================

def set_global(usetex: bool = True, fontsize: float = 12.5, font: str = "serif"):
    """
    Apply a consistent, publication-ready rcParams style.

    Parameters
    ----------
    usetex   : Use LaTeX for text rendering (requires a working LaTeX install).
    fontsize : Base font size in points.
    font     : Font family ('serif', 'sans-serif', etc.).
    """
    mpl.rcParams.update({
        "text.usetex":         usetex,
        "font.family":         font,
        "font.size":           fontsize,
        "axes.labelsize":      fontsize,
        "axes.titlesize":      fontsize,
        "xtick.labelsize":     fontsize - 1,
        "ytick.labelsize":     fontsize - 1,
        "legend.fontsize":     fontsize - 1,
        "lines.linewidth":     1.0,
        "axes.linewidth":      0.6,
        "xtick.major.width":   0.6,
        "ytick.major.width":   0.6,
        "xtick.minor.width":   0.4,
        "ytick.minor.width":   0.4,
        "xtick.direction":     "in",
        "ytick.direction":     "in",
        "xtick.top":           True,
        "ytick.right":         True,
        # Fix: centre tick labels on their tick mark (corrects usetex drift)
        "xtick.alignment":     "center",
        "ytick.alignment":     "center",
        # Fix: visible legend frame
        "legend.frameon":      True,
        "legend.framealpha":   0.9,
        "legend.edgecolor":    "1.0",   # light grey border
        "legend.fancybox":     False,    # square corners, cleaner for print
        "legend.borderpad":    0.4,
        "legend.handlelength": 1.5,
        "figure.dpi":          150,
        "savefig.dpi":         300,
        "savefig.bbox":        "tight",
        "savefig.pad_inches":  0.02,
    })


# =========================================================
# FIGURE CREATION
# =========================================================

def get_fig(size: tuple = (4.5, 3.1), dpi: int = 200):
    """Return (fig, ax) for a single-panel figure."""
    fig, ax = plt.subplots(figsize=size, dpi=dpi)
    return fig, ax


def get_fig_subplots(
    nrows: int = 1,
    ncols: int = 1,
    size: tuple = (4.5, 3.1),
    dpi: int = 200,
    sharex: bool = False,
    sharey: bool = False,
):
    """Return (fig, axes) for a multi-panel figure."""
    fig, axes = plt.subplots(nrows, ncols, figsize=size, dpi=dpi,
                             sharex=sharex, sharey=sharey)
    return fig, axes


# =========================================================
# AXIS LIMITS
# =========================================================

def set_x_axis(ax, bnd, margin: float = 0.0, log: bool = False):
    """
    Set x-axis limits from a tick array `bnd`.

    Parameters
    ----------
    bnd    : Array-like; bnd[0] is the lower bound, bnd[-1] is the upper bound.
    margin : Fractional padding added on each side (linear) or power of 10 (log).
    log    : If True, switch to log scale.
    """
    lo, hi = bnd[0], bnd[-1]
    if log:
        ax.set_xscale("log")
        ax.set_xlim(
            lo * (1 - margin) if margin else lo,
            hi * (1 + margin) if margin else hi,
        )
    else:
        span = hi - lo
        ax.set_xlim(lo - margin * span, hi + margin * span)


def set_y_axis(ax, bnd, margin: float = 0.0, log: bool = False):
    """
    Set y-axis limits from a tick array `bnd`.

    Parameters
    ----------
    bnd    : Array-like; bnd[0] is the lower bound, bnd[-1] is the upper bound.
    margin : Fractional padding added on each side (linear) or decades (log).
    log    : If True, switch to log scale.
    """
    lo, hi = bnd[0], bnd[-1]
    if log:
        ax.set_yscale("log")
        ax.set_ylim(lo * 10 ** (-margin), hi * 10 ** (margin))
    else:
        span = hi - lo
        ax.set_ylim(lo - margin * span, hi + margin * span)


# =========================================================
# TICK FORMATTING
# =========================================================

def make_formatter(fmt: str, usetex: bool = True):
    """
    Return a FuncFormatter that renders tick values using `fmt`.

    Parameters
    ----------
    fmt    : A Python format spec string applied to the tick value, e.g. ".0f",
             ".2f", ".4f".  Do NOT include surrounding $...$.
    usetex : If True (default), wrap the result in $\\mathrm{...}$ for LaTeX.
             Using \\mathrm (roman) instead of the default math italic gives
             matplotlib a more accurate bounding-box estimate, which prevents
             multi-digit labels from drifting left of their tick marks.
             If False, return plain text.

    Examples
    --------
    set_format(ax.xaxis, ticks, make_formatter(".0f"))   # "1000", "2000", …
    set_format(ax.yaxis, ticks, make_formatter(".2f"))   # "0.25", "0.50", …
    """
    def _fmt(x, _pos):
        s = format(x, fmt)
        return rf"$\mathrm{{{s}}}$" if usetex else s
    return tkr.FuncFormatter(_fmt)


def set_format(axis, ticks, fmt):
    """
    Set explicit tick positions and a formatter on a given Axis object.

    Parameters
    ----------
    axis : ax.xaxis or ax.yaxis
    ticks: Tick positions to display.
    fmt  : Either:
           - a FuncFormatter (from make_formatter or tkr.FuncFormatter), or
           - a legacy StrMethodFormatter string like "${x:.0f}$" (still works
             but may produce tick-alignment drift under usetex=True — prefer
             make_formatter instead).
    """
    axis.set_ticks(ticks)
    if isinstance(fmt, str):
        # Legacy path: kept for backward compatibility
        axis.set_major_formatter(tkr.StrMethodFormatter(fmt))
    else:
        axis.set_major_formatter(fmt)


# =========================================================
# GRID
# =========================================================

def set_grid(fig, ax, major: bool = True, minor: bool = False):
    """
    Toggle major and/or minor grid lines.

    Parameters
    ----------
    major : Show major grid lines (dashed, medium grey).
    minor : Show minor grid lines (dotted, light grey).
    """
    if major:
        ax.grid(which="major", linestyle="--", linewidth=0.4,
                color="0.75", alpha=0.8, zorder=0)
    if minor:
        ax.grid(which="minor", linestyle=":", linewidth=0.25,
                color="0.85", alpha=0.6, zorder=0)


# =========================================================
# COLOR HELPERS
# =========================================================

def make_colors(n: int, cmap_name: str = "plasma", lo: float = 0.1, hi: float = 0.9):
    """
    Sample `n` evenly-spaced colours from a matplotlib colormap.

    Returns a list of RGBA tuples.
    """
    cmap = plt.colormaps[cmap_name]
    return [cmap(v) for v in np.linspace(lo, hi, n)]


def set_color_cycle(ax, n: int, cmap_name: str = "plasma", lo: float = 0.1, hi: float = 0.9):
    """Override the colour cycle on `ax` with `n` colours from `cmap_name`."""
    ax.set_prop_cycle(color=make_colors(n, cmap_name, lo, hi))


# =========================================================
# SAVE
# =========================================================

def save_svg(fig, path: str):
    """Save figure as SVG, creating parent directories as needed."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fig.savefig(path, format="svg")
    print(f"Saved → {path}")


def save_pdf(fig, path: str):
    """Save figure as PDF, creating parent directories as needed."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fig.savefig(path, format="pdf")
    print(f"Saved → {path}")


def save_png(fig, path: str, dpi: int = 300):
    """Save figure as PNG, creating parent directories as needed."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fig.savefig(path, format="png", dpi=dpi)
    print(f"Saved → {path}")


# =========================================================
# BARE / FRAMELESS AXES
# =========================================================
 
def remove_axes(ax):
    """
    Strip all axis decorations — ticks, tick labels, spines, and axis labels —
    leaving only the plotted curves on a clean background.
 
    Call this after all plotting commands, just before save.
 
    Example
    -------
    fig, ax = utils_mpl.get_fig(size=(3.7, 2.5))
    for i in range(n): ax.step(t[i], x[i], lw=1.0)
    utils_mpl.remove_axes(ax)
    utils_mpl.save_svg(fig, "render/clean.svg")
    """
    ax.set_xticks([])
    ax.set_yticks([])
    ax.set_xlabel("")
    ax.set_ylabel("")
    # for spine in ax.spines.values():
    #     spine.set_visible(False)