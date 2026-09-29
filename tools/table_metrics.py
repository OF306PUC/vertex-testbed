#!/usr/bin/env python3
"""The six per-agent metrics for the paper table, split by regime.

    python3 tools/table_metrics.py runs/<campaign> [--split 12] [--out results/table]

Metrics, in the order they appear in results/TABLE-reports.txt:

  0.1  MSE(x_i)   mean (x_i - z_avg)^2 over the steady-state window, against
                  the consensus average, because tracking that average is the
                  control objective. The other candidate, (x_i - z_i)^2 against
                  the agent's own reference, was measured and discarded: it
                  came out at 3.9e-6 at 40 Hz and 9.7e-6 at 25 Hz for every
                  topology, medium and network condition, so it reports the
                  sample rate and nothing else.
  0.2  T_conv     PER AGENT, not fleet-wide: the settling time of x_i onto its
                  own virtual state, i.e. the last instant at which
                  |x_i - z_i| > delta. Settling rather than first crossing, so
                  that a brief re-excursion under disturbance does not let an
                  agent count as converged before it stayed converged.
  1.   P_complete fraction of publication intervals in which ALL d neighbours
                  delivered. Counted jointly, not derived from rho.
  2.   rho        fraction of publication intervals in which a given neighbour
                  delivered, averaged over the agent's incoming links.
  3.   AoI        age of the cached neighbour value, in seconds: time since the
                  last fresh arrival, averaged over the run and over links.
                  This is staleness at the receiver, NOT end-to-end age: BLE
                  links carry no usable one-way delay, so adding transit time
                  would be available for UDP links only and the two columns
                  would not be comparable. Reported with the p95 as well,
                  because the mean hides the tail that actually hurts.
  4.   dv         virtual input discrepancy |v_i(cached) - v_i(fresh)|, the
                  coupling term the agent computed from held neighbour values
                  against the one it would have computed from their true values
                  at the same instant, reconstructed from their own logs.

The steady-state window starts at STEADY_FRAC of the run, which is after
convergence for every arm in this campaign. Reported per (arm, medium, regime).
"""
from __future__ import annotations

import argparse
import json
import statistics as st
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vertex.analysis import load_node          # noqa: E402
from vertex.numeric import SCALE_FACTOR        # noqa: E402

# The steady-state window opens once ALL agents have converged: each agent has
# settled onto its own virtual state AND the virtual states have reached
# consensus. Both conditions are needed. An agent tracking its own reference
# says nothing about whether that reference is yet near zbar, and opening the
# window at the first condition alone integrates the whole consensus transient,
# which measures convergence speed rather than residual accuracy and reverses
# the ordering between a sparse and a dense graph.
STEADY_FRAC = 0.70          # fallback only, if convergence is never reached
ALPHA = 0.5                 # sign-power exponent, matches the manifests
DELTA = 0.01                # dead-band; every campaign manifest uses this value
DV_PROBES = 400             # probes per node for metric 4, which loads every peer


def sign(x):
    return (x > 0) - (x < 0)


def run_metrics(d: Path, with_dv: bool = True):
    """Per-agent metrics for one run: {nid: {...}} plus the run-level T_conv."""
    metas = {}
    for m in d.glob("*.meta.json"):
        j = json.loads(m.read_text())
        metas[j["node_id"]] = j
    if len(metas) < 28:
        return None, None

    nodes, out = {}, {}
    for nid in sorted(metas):
        try:
            nodes[nid] = load_node(d, nid)
        except Exception:
            continue
    if not nodes:
        return None, None

    # ---- T_conv, run level -------------------------------------------------
    L = min(len(n.vstate) for n in nodes.values())
    Z = np.vstack([np.asarray(n.vstate)[:L] for n in nodes.values()])
    t = np.asarray(next(iter(nodes.values())).t)[:L]
    idx = np.where((Z.max(0) - Z.min(0)) < 0.01)[0]
    t_conv = float(t[idx[0]]) if len(idx) else None      # fleet consensus, T_c
    zbar = float(Z[:, 0].mean())

    # The value the fleet actually agreed on, as distinct from zbar, which is
    # the mean of the INITIAL virtual states. Packet loss displaces the achieved
    # consensus from zbar by about 0.3, so a settling time measured against zbar
    # would never be reached; measured against the achieved value it is.
    z_star = float(Z[:, -1].mean())

    # per-agent settling of x_i onto z_i, needed before the window can open
    t_set = {}
    for nid, n in nodes.items():
        xa, za = np.asarray(n.state), np.asarray(n.vstate)
        over = np.where(np.abs(xa - za) > DELTA)[0]
        t_set[nid] = (float(n.t[over[-1]]) + metas[nid]["dt_s"]
                      if len(over) and over[-1] < len(xa) - 2 else None)
    settled = [v for v in t_set.values() if v is not None]
    t0_run = max([t_conv] + settled) if (t_conv is not None and settled) else None

    for nid, n in nodes.items():
        j = metas[nid]
        per = max(1, int(round(j["publish_period_s"] / j["dt_s"])))
        dt = j["dt_s"]
        nw = len(n.t) // per
        if not nw or not n.neighbours:
            continue
        if t0_run is not None:
            i0 = int(np.searchsorted(np.asarray(n.t), t0_run))
            if i0 > len(n.t) - 10:                     # window too short to mean anything
                i0 = int(STEADY_FRAC * len(n.t))
        else:
            i0 = int(STEADY_FRAC * len(n.t))

        x = np.asarray(n.state)
        z = np.asarray(n.vstate)
        rec = {"kind": j["node_type"], "deg": len(n.neighbours)}

        # 0.1 MSE against the consensus average, steady state only
        rec["mse"] = float(np.mean((x[i0:] - zbar) ** 2))

        # The same error split into its two layers, which is what localises the
        # degradation. mse_virt is the coordination error among the virtual
        # states, the layer the network actually touches; mse_phys is each
        # agent's tracking of its own reference, which the network does not.
        rec["mse_virt"] = float(np.mean((z[i0:] - zbar) ** 2))
        rec["mse_phys"] = float(np.mean((x[i0:] - z[i0:]) ** 2))

        # 0.2 per-agent settling time of x_i onto z_i
        rec["t_conv"] = t_set[nid]
        rec["t0"] = t0_run

        # settling of the VIRTUAL state onto the agreed value: when this agent
        # joined the consensus, which is a different event from its physical
        # state catching its own reference and happens much later.
        ov = np.where(np.abs(z - z_star) > DELTA)[0]
        rec["t_settle"] = ((float(n.t[ov[-1]]) + dt)
                           if len(ov) and ov[-1] < len(z) - 2 else None)

        # 1 and 2: completeness and per-link delivery, by publication window
        flags = {s: n.data[f"rx_{s}"] for s in n.neighbours}
        hits, complete = defaultdict(int), 0
        for w in range(nw):
            a, b = w * per, (w + 1) * per
            got = 0
            for s in n.neighbours:
                if any(flags[s][a:b]):
                    hits[s] += 1
                    got += 1
            if got == len(n.neighbours):
                complete += 1
        rec["rho"] = float(np.mean([hits[s] / nw for s in n.neighbours]))
        rec["p_complete"] = complete / nw

        # 3: age of the cached value, in seconds, per sample then averaged
        ages = []
        for s in n.neighbours:
            f = np.asarray(flags[s], dtype=bool)
            age = np.empty(len(f))
            last = -1
            for k in range(len(f)):
                if f[k]:
                    last = k
                age[k] = (k - last) * dt if last >= 0 else np.nan
            ages.append(age)
        A = np.vstack(ages)
        rec["aoi_mean"] = float(np.nanmean(A))
        rec["aoi_p95"] = float(np.nanpercentile(A, 95))
        out[nid] = rec

    # ---- 4: virtual input discrepancy, needs every peer's log ---------------
    if with_dv:
        for nid, n in nodes.items():
            if nid not in out:
                continue
            held_scale = (1.0 / SCALE_FACTOR
                          if metas[nid].get("units", "engineering") == "engineering"
                          else 1.0)
            peers = {s: nodes[s] for s in n.neighbours if s in nodes}
            if len(peers) != len(n.neighbours):
                continue
            step = max(1, len(n.t) // DV_PROBES)
            errs = []
            for i in range(0, len(n.t), step):
                ti, zi = n.t[i], n.vstate[i]
                v_held = v_true = 0.0
                for s, p in peers.items():
                    held = n.data[str(s)][i] * held_scale
                    k = min(int(ti * p.rate_hz), len(p.vstate) - 1) if p.rate_hz else 0
                    true = p.vstate[k]
                    v_held += -sign(zi - held) * abs(zi - held) ** ALPHA
                    v_true += -sign(zi - true) * abs(zi - true) ** ALPHA
                errs.append(abs(v_held - v_true))
            out[nid]["dv"] = float(st.mean(errs)) if errs else None
            out[nid]["dv_rms"] = (float(np.sqrt(np.mean(np.square(errs))))
                                  if errs else None)
    return out, t_conv


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("campaign", type=Path)
    ap.add_argument("--split", type=int, default=12,
                    help="first cycle of regime B; cycles below it are regime A")
    ap.add_argument("--no-dv", action="store_true",
                    help="skip metric 4, which loads every neighbour's log")
    ap.add_argument("--out", type=Path, default=None,
                    help="also write the per-run cache here as JSON")
    a = ap.parse_args()

    cache, conv = {}, {}
    for d in sorted(a.campaign.glob("n30-*-c*/")):
        m, tc = run_metrics(d, with_dv=not a.no_dv)
        if m is None:
            print(f"  skipped {d.name}", file=sys.stderr)
            continue
        cache[d.name] = m
        conv[d.name] = tc
        print(f"  {d.name}", file=sys.stderr, flush=True)

    if a.out:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_text(json.dumps({"metrics": cache, "t_conv": conv}))

    def regime(name):
        return "A" if int(name.rsplit("-c", 1)[1]) < a.split else "B"

    agg = defaultdict(lambda: defaultdict(list))
    for name, per_node in cache.items():
        arm = name.rsplit("-c", 1)[0].replace("n30-", "")
        g = regime(name)
        if conv.get(name) is not None:
            agg[(arm, "-", g)]["t_conv"].append(conv[name])
        for rec in per_node.values():
            key = (arm, rec["kind"], g)
            for k in ("mse", "mse_virt", "mse_phys", "t_conv", "t_settle",
                      "rho", "p_complete", "aoi_mean", "aoi_p95", "dv", "dv_rms"):
                if rec.get(k) is not None:
                    agg[key][k].append(rec[k])

    cols = [("MSE", "mse", "{:10.4f}"), ("T_conv s", "t_conv", "{:10.1f}"),
            ("P_compl", "p_complete", "{:9.4f}"), ("rho", "rho", "{:8.3f}"),
            ("AoI ms", "aoi_mean", "{:8.1f}"), ("AoI p95", "aoi_p95", "{:9.1f}"),
            ("dv", "dv", "{:8.4f}")]
    for g in ("B", "A"):
        print(f"\n{'='*104}")
        print(f"REGIME {g}   ({'cycles %d+' % a.split if g=='B' else 'cycles 0-%d' % (a.split-1)})")
        print("=" * 104)
        hdr = f"{'arm':16}{'medium':8}" + "".join(f"{c[0]:>{len(c[2].format(0))}}" for c in cols) + f"{'n':>6}"
        print(hdr)
        for (arm, kind, gg) in sorted(agg):
            if gg != g or kind == "-":
                continue
            r = agg[(arm, kind, gg)]
            line = f"{arm:16}{kind:8}"
            for _, k, fmt in cols:
                v = r.get(k)
                if not v:
                    line += " " * len(fmt.format(0))
                elif k == "aoi_mean" or k == "aoi_p95":
                    line += fmt.format(st.mean(v) * 1000.0)
                else:
                    line += fmt.format(st.mean(v))
            print(line + f"{len(r.get('rho', [])):>6}")
        print(f"\n  {'arm':16}T_conv")
        for (arm, kind, gg) in sorted(agg):
            if gg != g or kind != "-":
                continue
            v = agg[(arm, kind, gg)]["t_conv"]
            print(f"  {arm:16}{st.mean(v):7.1f} +- {st.pstdev(v):<5.1f}  (n={len(v)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
