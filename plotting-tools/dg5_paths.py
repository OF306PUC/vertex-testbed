r"""Where a five-DG figure goes, decided once.

The layout:

    render/dg5/<set>/arms/      figures that put several arms on one axes
    render/dg5/<set>/<run>/     figures that draw a single run

`<set>` is the directory the runs live in -- `per-mixed`, `rep-bridge5` --
so a figure carries its uncertainty profile and its agent assignment in its
path, and two sets drawn in one session cannot overwrite each other.

`<run>` is the trial and arm only, `t0-LC0`, not the run's full name: the
set directory already said `mixed` and `per`, and repeating them inside it
made paths that are long to type into `\includegraphics` and hard to scan.

This used to be four copies of one function, one per script, and the copy in
`plot_dg5_profiles.py` was the odd one -- it hardcoded a destination, so the
replication-profile panels were written into the persistent profile's
directory. One definition cannot drift from itself.
"""
from __future__ import annotations

import re
from pathlib import Path

__all__ = ["RENDER", "set_name", "set_dir", "arms_dir", "run_dir",
           "run_label", "pair_stem"]

RENDER = Path(__file__).resolve().parents[1] / "plotting-tools/render/dg5"

#: `dg5-mixed-rep-0-t0-LC0` -> trial 0, arm LC0. The arm may carry a `+`
#: once the run name's `plus` is undone, so it is matched loosely.
_RUN_RE = re.compile(r"-t(\d+)-([A-Za-z0-9+]+)$")


def set_name(run: Path) -> str:
    """The run set a run belongs to: its parent, or itself if loose in `runs/`."""
    run = run.resolve()
    parent = run.parent.name
    return parent if parent not in ("runs", "") else run.name


def set_dir(run: Path) -> Path:
    return RENDER / set_name(run)


def run_label(run: Path) -> str:
    """`t0-LC0`, or the full directory name when it does not parse."""
    m = _RUN_RE.search(run.resolve().name)
    return f"t{m.group(1)}-{m.group(2)}" if m else run.resolve().name


def arms_dir(run: Path) -> Path:
    """Where multi-arm figures for this run's set go."""
    d = set_dir(run) / "arms"
    d.mkdir(parents=True, exist_ok=True)
    return d


def run_dir(run: Path) -> Path:
    """Where single-run figures for this run go."""
    d = set_dir(run) / run_label(run)
    d.mkdir(parents=True, exist_ok=True)
    return d


def pair_stem(runs) -> str:
    """`LC0-DZ`: the two arms a comparison figure contrasts, in drawing order.

    In the `per` sets both LC0 and LC+ exist, so a fixed stem meant two
    comparisons wrote one pair of files and whichever ran last survived. The
    arms belong in the name for the same reason the profile belongs in the
    directory.
    """
    return "-".join(a.replace("+", "plus") for a in runs)
