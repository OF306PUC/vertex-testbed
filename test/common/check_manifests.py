#!/usr/bin/env python3
"""The generator must survive any host count, not just the one in use.

Manifests are skipped when there are too few hosts for them. That skipping was
twice wrong in ways nothing caught: first an `if ... < 3: return out` that bailed
out of the whole function, silently emitting 3 manifests instead of 11; then a
30-agent block indented inside the 3-host guard, so a lab with exactly 3 hosts
crashed on `10 agents per band needs 10 hosts`.

Both were invisible at the host count being used at the time. This runs the
generator across the counts that matter and asserts it neither crashes nor
silently drops what it can build.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import make_manifests as mm                                   # noqa: E402
from vertex.topology import check, load_manifest              # noqa: E402

# 1 host builds nothing: every edge must cross hosts.
EXPECT = {1: set(),
          2: {"n4-noble", "n6-fast", "n6-ring", "sweep-p400"},
          3: {"n4-noble", "n6-fast", "n6-ring", "n9-ring", "sweep-p400"},
          10: {"n4-noble", "n6-ring", "n9-ring", "n30-clusters", "n30-ring4"}}


def main() -> int:
    fails = 0
    for n in (1, 2, 3, 10):
        hosts = [f"10.0.0.{i}" for i in range(1, n + 1)]
        mm.HOSTS[:] = hosts
        mm.FIRST_RUN_HOSTS[:] = hosts[:3]
        try:
            got = mm.manifests()
        except Exception as e:
            print(f"FAIL {n} hosts: generator raised {type(e).__name__}: {e}")
            fails += 1
            continue
        missing = EXPECT[n] - set(got)
        if missing:
            print(f"FAIL {n} hosts: did not build {sorted(missing)}")
            fails += 1
            continue
        for name, doc in got.items():
            rep = check(load_manifest(doc))
            if not rep.ok:
                print(f"FAIL {n} hosts: {name} invalid: {rep.errors[:1]}")
                fails += 1
        print(f"  {n:>2} hosts -> {len(got):>2} manifests, all valid")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
