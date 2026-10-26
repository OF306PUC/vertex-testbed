#!/usr/bin/env python3
"""Choose the realisation to put in the figures, per topology, by rule.

    python3 plotting-tools/pick.py                      # 40 Hz arms of the campaign
    python3 plotting-tools/pick.py --rate 25hz --json out.json

Sixty cycles were collected per arm and one of them goes in the paper. Picking it
by eye picks the prettiest picture, which on a consensus run is the wrong thing:
the prettiest picture is a fast, clean convergence, and the fastest convergence
is what a *degraded* network produces. In a directed cycle a link that is barely
delivering makes the fleet track one node instead of averaging thirty, so it
settles sooner and to the wrong value. Convergence speed is therefore deliberately
absent from the ranking below.

What is ranked, in order:

  1. **Dead links.** Count of incoming links whose delivery falls under DEAD_RHO.
     One link gone changes the graph, and every statement the figure makes is a
     statement about the graph in the manifest. A run with a dead link is not a
     realisation of the experiment, so this is a gate more than a criterion --
     in this campaign nothing trips it, which is itself the thing to report.
  2. **Consensus offset**, |mean_i z_i(T) - zbar| with zbar the mean of the
     initial virtual states. The virtual layer is an average-consensus and the
     disturbance enters x, not z, so z must land on the average of its own
     initial conditions. How far it misses is how much the network cost.
     Compared at a tolerance of OFFSET_TOL, so runs that land equally close are
     separated by the next criterion rather than by a difference no reader sees.
  3. **Delivery**, the mean over links of the fraction of publication intervals
     in which that neighbour delivered. Windowed, not per sample: a sample-rate
     mean caps at dt/publish_period (0.2 here) and reads as 80% loss.

A run is eligible only if it is complete: thirty node logs that open, the
duration the figure needs (see TMAX), and run.json's own `ok`.

Clusters is G3(t): agents 21 and 30 are cut-points, disabled until the merge at
MERGE_T. Their links carry nothing before it by design, so links incident to
them are scored from the merge onwards and the pre-merge window is not counted
against them.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vertex.analysis import load_node          # noqa: E402

CAMPAIGN = ROOT / "runs/n30-regimeB"
TOPOLOGIES = ("dring", "ring4", "clusters")
N = 30
MERGE_T = 60.0          # G3(t) cut-points enabled here
CUTPOINTS = (21, 30)    # 1-based node ids, the agents that event enables
WARMUP_S = 2.0          # links are not scored while the fleet is still starting

#: How much of the run the figures show, per topology; None is all of it. A run
#: is judged over exactly the window it is drawn over, so the ranking cannot be
#: made on evidence the figure does not contain. Clusters is collected for 360 s
#: but the merge transient -- the quantity of interest -- is over by ~160 s, so
#: it is presented as a 300 s realisation, which is also ring4's length.
# All three cropped to a common 300 s so the summary figures compare
# equal spans. Every arm has converged and held well before this:
# T_settle is 136 s (dring), 43 s (ring4), 163 s (clusters).
TMAX = {"dring": 300.0, "ring4": 300.0, "clusters": 300.0}
DEAD_RHO = 0.05         # below this a link is not delivering at all
OFFSET_TOL = 0.01       # consensus offsets within this are a tie; same threshold
                        # the campaign uses to call the virtual states converged


def run_report(d: Path, topo: str) -> dict:
    """Every number the ranking uses for one run, plus why it was rejected."""
    rec: dict = {"name": d.name}
    try:
        meta = json.loads((d / "run.json").read_text())
    except Exception as e:
        return rec | {"eligible": False, "reason": f"no run.json ({e})"}
    rec["ok"] = bool(meta.get("ok"))
    rec["duration_s"] = meta.get("duration_s")

    nodes = {}
    for nid in range(1, N + 1):
        try:
            nodes[nid] = load_node(d, nid)
        except Exception as e:
            return rec | {"eligible": False, "reason": f"node {nid} unreadable: {e}"}

    tmax = TMAX.get(topo)
    L = min(len(n.t) for n in nodes.values())
    if tmax is not None:
        L = min(L, int(np.searchsorted(np.asarray(nodes[1].t), tmax, side="right")))
    t = np.asarray(nodes[1].t)[:L]
    Z = np.vstack([np.asarray(nodes[i].vstate)[:L] for i in range(1, N + 1)])

    # ---- 2: where the virtual consensus landed -----------------------------
    zbar = float(Z[:, 0].mean())
    z_end = Z[:, -1]
    rec["zbar"] = zbar
    rec["z_end_mean"] = float(z_end.mean())
    rec["offset"] = abs(float(z_end.mean()) - zbar)
    rec["spread_end"] = float(z_end.max() - z_end.min())

    # ---- 1 and 3: per-link delivery, by publication window -----------------
    links: dict[str, float] = {}
    for nid, n in nodes.items():
        j = json.loads((d / f"{nid}.meta.json").read_text())
        per = max(1, int(round(j["publish_period_s"] / j["dt_s"])))
        tt = np.asarray(n.t)
        for s in n.neighbours:
            gated = topo == "clusters" and (nid in CUTPOINTS or s in CUTPOINTS)
            start = MERGE_T if gated else WARMUP_S
            f = np.asarray(n.data[f"rx_{s}"], dtype=bool)
            i0 = int(np.searchsorted(tt, start))
            i1 = len(f) if tmax is None else int(np.searchsorted(tt, tmax, side="right"))
            f = f[i0:i1]
            nw = len(f) // per
            if nw == 0:
                continue
            got = f[: nw * per].reshape(nw, per).any(axis=1)
            links[f"{s}->{nid}"] = float(got.mean())

    rho = np.array(list(links.values()))
    dead = sorted(k for k, v in links.items() if v < DEAD_RHO)
    rec |= {"n_links": len(links), "dead_links": dead, "n_dead": len(dead),
            "delivery": float(rho.mean()), "rho_min": float(rho.min()),
            "worst_links": sorted(links.items(), key=lambda kv: kv[1])[:3]}

    want = float(tmax if tmax is not None else rec["duration_s"] or 0.0)
    short = bool(want) and float(t[-1]) < 0.95 * want
    rec["t_end"] = float(t[-1])
    if not rec["ok"]:
        return rec | {"eligible": False, "reason": "run.json ok=false"}
    if short:
        return rec | {"eligible": False,
                      "reason": f"short: {t[-1]:.0f}s of {rec['duration_s']:.0f}s"}
    return rec | {"eligible": True, "reason": ""}


def rank_key(r: dict) -> tuple:
    """Dead links, then consensus offset at OFFSET_TOL, then delivery."""
    return (r["n_dead"], round(r["offset"] / OFFSET_TOL), -r["delivery"])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--campaign", type=Path, default=CAMPAIGN)
    ap.add_argument("--rate", default="40hz")
    ap.add_argument("--top", type=int, default=5, help="rows printed per topology")
    ap.add_argument("--json", type=Path, default=None, help="write the full scan here")
    a = ap.parse_args()

    out, picks = {}, {}
    for topo in TOPOLOGIES:
        rows = []
        for d in sorted(a.campaign.glob(f"n30-{topo}-{a.rate}-c*")):
            rows.append(run_report(d, topo))
            print(".", end="", file=sys.stderr, flush=True)
        print(f"  {topo}", file=sys.stderr)
        out[topo] = rows

        good = sorted((r for r in rows if r["eligible"]), key=rank_key)
        rejected = [r for r in rows if not r["eligible"]]
        picks[topo] = good[0]["name"] if good else None

        print(f"\n=== {topo} ({a.rate}) -- {len(good)}/{len(rows)} eligible, "
              f"{len(links_dead := [r for r in good if r['n_dead']])} with dead links")
        print(f"{'run':26}{'dead':>6}{'offset':>9}{'delivery':>10}"
              f"{'rho_min':>9}{'spread':>9}")
        for r in good[:a.top]:
            print(f"{r['name']:26}{r['n_dead']:>6}{r['offset']:>9.4f}"
                  f"{r['delivery']:>10.4f}{r['rho_min']:>9.4f}{r['spread_end']:>9.4f}")
        if len(good) > a.top:
            w = good[-1]
            print(f"{'  ... worst: ' + w['name']:26}{w['n_dead']:>6}{w['offset']:>9.4f}"
                  f"{w['delivery']:>10.4f}{w['rho_min']:>9.4f}{w['spread_end']:>9.4f}")
        for r in rejected:
            print(f"  rejected {r['name']}: {r['reason']}")
        print(f"  PICK {picks[topo]}")

    print("\nRUNS = {" + ", ".join(f'"{k}": "{v}"' for k, v in picks.items()) + "}")
    if a.json:
        a.json.parent.mkdir(parents=True, exist_ok=True)
        a.json.write_text(json.dumps({"picks": picks, "runs": out}, indent=1))
        print(f"  wrote {a.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
