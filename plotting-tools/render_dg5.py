r"""Regenerate the whole five-DG figure set, into the layout `dg5_paths` defines.

    python3 plotting-tools/render_dg5.py                 # every set, trial 0
    python3 plotting-tools/render_dg5.py --trial 3
    python3 plotting-tools/render_dg5.py --sets per-mixed

Each run set under `runs/` gets:

    arms/        the multi-arm figures   (plot_dg5_arms, _compare, _gain,
                                          and _profiles for the two mixed sets)
    t<n>-<arm>/  the single-run figures  (plot_dg5)

Four scripts write this tree, each with its own argument convention and its
own idea of which runs it wants. Here the set is enumerated from disk and each
script is handed the runs it needs, so "redraw everything" is one command and
no destination is ever named by hand -- `dg5_paths` derives every one.

`plot_dg5_profiles` is run once rather than per set: it couples the y limits
of the two mixed sets on purpose, so it reads both and writes into each.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "plotting-tools"

#: The pair each comparison figure wants. `plot_dg5_compare` contrasts the
#: proposed interface with the dead zone; `plot_dg5_gain` asks whether the
#: unbounded gain of AA changes what the plant sees, against DZ which stops.
#: An arm a set does not have is skipped rather than substituted.
PAIRS = {
    "plot_dg5_compare.py": (("LCplus", "LC0"), "DZ"),
    "plot_dg5_gain.py": (("AA",), "DZ"),
}


def arms_of(run_set: Path, trial: int) -> dict[str, Path]:
    """`{arm: run directory}` for one trial of one set, in run-name order."""
    out = {}
    for d in sorted(run_set.iterdir()):
        if d.is_dir() and f"-t{trial}-" in d.name:
            out[d.name.rsplit(f"-t{trial}-", 1)[1]] = d
    return out


def run(script: str, *args: Path) -> bool:
    cmd = [sys.executable, str(TOOLS / script), *map(str, args)]
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        print(f"    FAIL {script}: {p.stderr.strip().splitlines()[-1:]}")
        return False
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trial", type=int, default=0)
    ap.add_argument("--sets", nargs="*", default=None,
                    help="run-set directory names under runs/ (default: all)")
    a = ap.parse_args()

    names = a.sets or sorted(
        d.name for d in (ROOT / "runs").iterdir()
        if d.is_dir() and not d.name.startswith("_") and arms_of(d, a.trial))
    if not names:
        print("no run sets found", file=sys.stderr)
        return 2

    failures = 0
    for name in names:
        arms = arms_of(ROOT / "runs" / name, a.trial)
        if not arms:
            print(f"{name}: no trial-{a.trial} runs, skipped")
            continue
        print(f"{name}: {', '.join(arms)}")
        for arm, d in arms.items():
            failures += not run("plot_dg5.py", d)
        failures += not run("plot_dg5_arms.py", *arms.values())
        for script, (wanted, against) in PAIRS.items():
            have = [arms[w] for w in wanted if w in arms]
            if have and against in arms:
                # One pair per call: the scripts key their series by arm, so
                # handing them three runs would silently drop one.
                for d in have:
                    failures += not run(script, arms[against], d)

    mixed = [n for n in names if n.endswith("-mixed")]
    if len(mixed) == 2:
        failures += not run("plot_dg5_profiles.py", "--trial", str(a.trial))
    elif mixed:
        print("  profiles: needs both mixed sets, skipped")

    print(f"\n{'all figures regenerated' if not failures else f'{failures} script run(s) failed'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
