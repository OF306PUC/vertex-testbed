"""Locating the off-line oracle, and skipping cleanly when it is absent.

The checks in this directory compare `vertex/` against
`reviewers-exp/sim`, which is the single-process model every Phase 1 number
was measured with. That directory is **not in version control**: it is in
`.gitignore` along with `plotting-tools/`, because it carries generated
figures and a working paper alongside the model.

So a fresh clone has the checks but not the thing they compare against. They
skip with a message rather than failing, because a missing oracle is not a
regression in the port, and `check_all.sh` reporting a failure for it would
train a reader to ignore that line.

If the oracle should travel with the repository, the fix is to un-ignore it
rather than to weaken these checks.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ORACLE = ROOT / "reviewers-exp"


def require(what: str) -> int | None:
    """Put the oracle on the path, or return an exit code meaning "skipped"."""
    if not (ORACLE / "sim" / "__init__.py").is_file():
        print(f"  skip {what}: no oracle at {ORACLE.relative_to(ROOT)}/ "
              f"(gitignored; see test/oracle/_oracle.py)")
        return 0
    for p in (str(ROOT), str(ORACLE)):
        if p not in sys.path:
            sys.path.insert(0, p)
    return None
