#!/usr/bin/env python3
"""Measure the reception-completeness mechanism: does P(complete) fall as rho^d?

    python3 tools/capture_analysis.py runs/<campaign>
    python3 tools/capture_analysis.py runs/<campaign> --arm ring4-40hz

The claim under test (docs/review/r3_g2_traffic_attribution.tex): under broadcast,
a denser graph costs nothing in airtime, but the receiver must acquire d values
per publication interval from d uncoordinated transmitters, so the probability of
a fully refreshed neighbourhood falls geometrically in the degree.

Four measurements, in the order their validity depends on each other:

  0. Is rho degree-independent?  The premise. If per-link capture falls with the
     degree, the mechanism as stated is wrong and this is where it shows.
  1. P_complete measured vs rho^d.  Binned by publication interval: did EVERY
     neighbour arrive in this window? The gap to rho^d is the correlation term.
     Measured >= rho^d, not <=: loss is common-mode, so complete neighbourhoods
     are MORE frequent than independence predicts. On BLE the excess is nil
     (ratio 1.00-1.08, captures independent); on wifi it reaches 5.9x, because
     the AP batches broadcast frames at DTIM boundaries and so acts as the
     scheduler BLE lacks. Pooling the media hides this, and this tool pools
     them: split by the node_type field of <nid>.meta.json. See PLATFORM 5.5b.
  2. The within-run degree sweep.  n30-clusters carries degrees 2, 3 and 4 in one
     run, so log P_complete against d has a slope of log rho with no cross-run
     confound at all: same fleet, same instant, same ambient traffic.
  3. Staleness cost in the units of the law.  The coupling term the node computed
     from held values, against the one it would have computed from its
     neighbours' true values at that instant, reconstructed from their own logs
     on the shared host clock.

Binning by publication interval rather than by sample is deliberate: the rx_
column under-counts when two arrivals land in one sample, but "did at least one
arrive in this window" is unaffected by that, so P_complete is the more robust
statistic.
"""
from __future__ import annotations

import argparse
import json
import math
import statistics as st
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vertex.analysis import load_node


def node_capture(run_dir: Path, nid: int):
    """Per-node capture statistics for one run.

    Returns (degree, {src: rho}, p_complete, mean_stale, windows) or None.
    """
    n = load_node(run_dir, nid)
    nbrs = list(n.neighbours)
    if not nbrs:
        return None
    meta = json.loads((run_dir / f"{nid}.meta.json").read_text())
    ctrl = meta.get("controller") or {}
    dt, pub = ctrl.get("dt_s"), ctrl.get("publish_period_s")
    if not dt or not pub:
        return None

    per_win = max(1, int(round(pub / dt)))          # samples per publication window
    flags = {s: n.data[f"rx_{s}"] for s in nbrs}
    rows = len(n.t)
    nwin = rows // per_win

    hits = {s: 0 for s in nbrs}
    complete = 0
    stale_total = 0
    for w in range(nwin):
        a, b = w * per_win, (w + 1) * per_win
        got = 0
        for s in nbrs:
            if any(flags[s][a:b]):
                hits[s] += 1
                got += 1
        if got == len(nbrs):
            complete += 1
        stale_total += len(nbrs) - got

    if nwin == 0:
        return None
    rho = {s: hits[s] / nwin for s in nbrs}
    return len(nbrs), rho, complete / nwin, stale_total / nwin, nwin


def coupling_error(run_dir: Path, nid: int, alpha: float = 0.5):
    """Mean |v_i(held) - v_i(true)|: what staleness costs, in the law's units.

    The held values are the neighbour columns the node actually used. The true
    values come from each neighbour's OWN log, sampled at the same host-clock
    instant -- both timelines are on the run's shared epoch, so they align.

    UNIT TRAP. A `wifi` or `bridge` agent declares `units: engineering` and its
    own state columns are converted on read, but its NEIGHBOUR columns are the
    raw scaled int32 taken off the wire (NeighborTable.snapshot returns
    rec.vstate unconverted), and normalize_run leaves them alone because the node
    declared engineering. So one row mixes units. A `ble` relay declares
    scaled_int and is converted wholesale, so it is self-consistent. Compensate
    here rather than in the loader; the underlying inconsistency is logged as a
    defect.
    """
    from vertex.numeric import SCALE_FACTOR
    units = (json.loads((run_dir / f"{nid}.meta.json").read_text())
             .get("units", "engineering"))
    held_scale = 1.0 / SCALE_FACTOR if units == "engineering" else 1.0
    n = load_node(run_dir, nid)
    nbrs = list(n.neighbours)
    if not nbrs:
        return None
    try:
        peers = {s: load_node(run_dir, s) for s in nbrs}
    except Exception:
        return None

    def sign(x):
        return (x > 0) - (x < 0)

    # index each peer's series by time once, then walk in step
    errs = []
    step = max(1, len(n.t) // 400)               # ~400 probes is plenty for a mean
    for i in range(0, len(n.t), step):
        t = n.t[i]
        z = n.vstate[i]
        v_held = v_true = 0.0
        for s in nbrs:
            held = n.data[str(s)][i] * held_scale
            p = peers[s]
            j = min(int(t * p.rate_hz), len(p.vstate) - 1) if p.rate_hz else 0
            true = p.vstate[j]
            v_held += -sign(z - held) * abs(z - held) ** alpha
            v_true += -sign(z - true) * abs(z - true) ** alpha
        errs.append(abs(v_held - v_true))
    return st.mean(errs) if errs else None


def analyse(campaign: Path, arms: list[str], with_coupling: bool):
    runs = defaultdict(list)
    for d in sorted(campaign.glob("n30-*-c*/")):
        arm = d.name.rsplit("-c", 1)[0].replace("n30-", "")
        if arms and arm not in arms:
            continue
        runs[arm].append(d)

    # ---- 0 and 1: rho, P_complete, and the rho^d prediction, per arm ----------
    print("=" * 78)
    print("0/1.  per-link capture rho, and neighbourhood completeness")
    print("=" * 78)
    print(f"{'arm':16} {'d':>2} {'nodes':>6} {'rho':>16} {'P_comp meas':>13} "
          f"{'rho^d':>8} {'ratio':>6}")
    by_degree_all = defaultdict(list)
    for arm in sorted(runs):
        acc = defaultdict(lambda: ([], [], []))          # degree -> rhos, pc, stale
        for d in runs[arm]:
            for nid in range(1, 31):
                r = node_capture(d, nid)
                if r is None:
                    continue
                deg, rho, pc, stale, _ = r
                acc[deg][0].extend(rho.values())
                acc[deg][1].append(pc)
                acc[deg][2].append(stale)
                by_degree_all[(arm, deg)].append((st.mean(rho.values()), pc))
        for deg in sorted(acc):
            rhos, pcs, _ = acc[deg]
            mr, mp = st.mean(rhos), st.mean(pcs)
            sr = st.pstdev(rhos)
            pred = mr ** deg
            print(f"{arm:16} {deg:>2} {len(pcs):>6} {mr:>8.4f} +-{sr:<5.3f} "
                  f"{mp:>13.4f} {pred:>8.4f} {mp/pred if pred else 0:>6.2f}")

    # ---- 2: the within-run degree sweep, clusters only ------------------------
    print()
    print("=" * 78)
    print("2.  within-run degree sweep (same fleet, same instant, same ambient)")
    print("=" * 78)
    for arm in sorted(a for a in runs if "clusters" in a):
        print(f"\n  {arm}")
        print(f"    {'d':>2} {'nodes':>6} {'rho':>8} {'P_comp':>8} {'rho^d':>8} "
              f"{'stale/win':>10}")
        acc = defaultdict(lambda: ([], [], []))
        for d in runs[arm]:
            for nid in range(1, 31):
                r = node_capture(d, nid)
                if r is None:
                    continue
                deg, rho, pc, stale, _ = r
                acc[deg][0].extend(rho.values())
                acc[deg][1].append(pc)
                acc[deg][2].append(stale)
        pts = []
        for deg in sorted(acc):
            rhos, pcs, sts = acc[deg]
            mr, mp = st.mean(rhos), st.mean(pcs)
            print(f"    {deg:>2} {len(pcs):>6} {mr:>8.4f} {mp:>8.4f} "
                  f"{mr**deg:>8.4f} {st.mean(sts):>10.3f}")
            if mp > 0:
                pts.append((deg, math.log(mp)))
        if len(pts) >= 2:
            xb = st.mean(p[0] for p in pts); yb = st.mean(p[1] for p in pts)
            num = sum((x-xb)*(y-yb) for x, y in pts)
            den = sum((x-xb)**2 for x, _ in pts)
            slope = num/den if den else float("nan")
            print(f"    fit: d(log P_complete)/dd = {slope:+.4f}  "
                  f"-> implied rho = {math.exp(slope):.4f}")

    # ---- 3: what staleness costs the coupling term ---------------------------
    if with_coupling:
        print()
        print("=" * 78)
        print("3.  staleness cost in the coupling term, |v_held - v_true|")
        print("=" * 78)
        print(f"{'arm':16} {'d':>2} {'nodes':>6} {'mean |dv|':>11}")
        for arm in sorted(runs):
            acc = defaultdict(list)
            for d in runs[arm][:5]:                 # 5 runs is enough for a mean
                for nid in range(1, 31):
                    r = node_capture(d, nid)
                    if r is None:
                        continue
                    e = coupling_error(d, nid)
                    if e is not None:
                        acc[r[0]].append(e)
            for deg in sorted(acc):
                print(f"{arm:16} {deg:>2} {len(acc[deg]):>6} "
                      f"{st.mean(acc[deg]):>11.6f}")


def plot(campaign: Path, out: Path):
    """One figure per publication rate, G1 and G2 only, split by medium.

    G1 (dring) contributes the d=1 point and G2 (ring4) the d=4 point, so each
    line is a two-point degree sweep at a fixed rate. Splitting by node_type is
    the whole point of the figure: the BLE series should sit on rho^d and the
    wifi series should sit well above it, and that contrast is what identifies
    the absence of a scheduler rather than channel load as the cause.

    G3 (clusters) is deliberately excluded. It is irregular, so its points are
    degree-mixed and would clutter a plot whose x axis is the degree.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    RATES = {"40hz": ("8 Hz publish", "125 ms"), "25hz": ("5 Hz publish", "200 ms")}
    MEDIA = {"ble":    ("tab:red",   "o", "BLE relay (no scheduler)"),
             "bridge": ("tab:green", "^", "bridge (BLE + UDP)"),
             "wifi":   ("tab:blue",  "s", "Wi-Fi agent (AP batches)")}
    ARM = {"dring": "$\\mathcal{G}_1$", "ring4": "$\\mathcal{G}_2$"}

    written = []
    for rate, (rlabel, tpub) in RATES.items():
        # (medium, degree) -> rhos, p_completes, stales
        acc = defaultdict(lambda: ([], [], []))
        seen_arm = {}
        for topo in ("dring", "ring4"):
            for run in sorted(campaign.glob(f"n30-{topo}-{rate}-c*/")):
                for meta in sorted(run.glob("*.meta.json")):
                    nid = int(meta.stem.split(".")[0])
                    kind = json.loads(meta.read_text()).get("node_type")
                    r = node_capture(run, nid)
                    if r is None or kind not in MEDIA:
                        continue
                    deg, rho, pc, stale, _ = r
                    acc[(kind, deg)][0].extend(rho.values())
                    acc[(kind, deg)][1].append(pc)
                    acc[(kind, deg)][2].append(stale)
                    seen_arm[deg] = ARM[topo]
        if not acc:
            continue

        fig, ax = plt.subplots(1, 3, figsize=(13.5, 4.3))
        degs = sorted({d for _, d in acc})

        for kind, (c, m, lab) in MEDIA.items():
            ds = [d for d in degs if (kind, d) in acc]
            if not ds:
                continue
            rho_m = [st.mean(acc[(kind, d)][0]) for d in ds]
            rho_s = [st.pstdev(acc[(kind, d)][0]) for d in ds]
            pc_m  = [st.mean(acc[(kind, d)][1]) for d in ds]
            stl_m = [st.mean(acc[(kind, d)][2]) for d in ds]
            rbar  = st.mean([v for d in ds for v in acc[(kind, d)][0]])

            ax[0].errorbar(ds, rho_m, yerr=rho_s, marker=m, color=c,
                           capsize=4, alpha=.85, label=lab)
            ax[1].plot(ds, pc_m, marker=m, color=c, alpha=.9, label=lab)
            ax[1].plot(ds, [rbar ** d for d in ds], color=c, alpha=.4,
                       linestyle=":", linewidth=1.4)
            ax[2].plot(ds, stl_m, marker=m, color=c, alpha=.9, label=lab)
            ax[2].plot(ds, [d * (1 - rbar) for d in ds], color=c, alpha=.4,
                       linestyle=":", linewidth=1.4)
            # how far above independence each point sits
            for d, y in zip(ds, pc_m):
                if d > 1:
                    ax[1].annotate(f"{y / rbar ** d:.2f}x", (d, y),
                                   textcoords="offset points", xytext=(7, -2),
                                   fontsize=8, color=c)

        for a in ax:
            a.set_xlabel("degree $d$")
            a.set_xticks(degs)
            a.set_xticklabels([f"{seen_arm[d]}\n$d={d}$" for d in degs])
            a.grid(alpha=.3)
        ax[0].set_ylabel(r"per-link capture  $\rho$")
        ax[0].set_title("(a) premise: $\\rho$ is flat in $d$")
        ax[0].set_ylim(0, 1)
        ax[1].set_ylabel(r"$P_\mathrm{complete}$")
        ax[1].set_title("(b) measured (solid) vs $\\rho^d$ (dotted)")
        ax[1].set_yscale("log")
        ax[1].grid(alpha=.3, which="both")
        ax[2].set_ylabel("stale neighbours per window")
        ax[2].set_title("(c) measured vs $d(1-\\rho)$ (dotted)")
        ax[0].legend(fontsize=8, loc="lower left")

        fig.suptitle(f"neighbourhood completeness, {rlabel} "
                     f"($T_{{pub}}$ = {tpub}, $T_{{adv}}$ = 100-150 ms)",
                     fontsize=11)
        fig.tight_layout(rect=(0, 0, 1, .94))
        out.mkdir(parents=True, exist_ok=True)
        f = out / f"capture_completeness_{rlabel.split()[0]}hz.png"
        fig.savefig(f, dpi=140)
        plt.close(fig)
        print(f"  wrote {f}")
        written.append(f)
    return written


def by_medium(campaign: Path, arms: list[str], cycles: int | None = None):
    """Measurement 1, split by node_type, and by which radio TRANSMITTED.

    Pooling the three node kinds is what made the fleet-wide ratio read 2.4 and
    hid the real result: BLE receivers sit on rho^d almost exactly, while Wi-Fi
    receivers sit far above it because the access point batches broadcast frames
    at DTIM boundaries and so supplies the sequencing BLE has none of. Splitting
    is therefore not a refinement of measurement 1, it is the measurement that
    identifies the mechanism. See PLATFORM 5.5b.

    The transmitter split is the second table. A receiver's links are classified
    by the kind of node that sent them, which is what localised the 2026-09-10
    advertising defect to the Pi's transmit path.
    """
    runs = defaultdict(list)
    for d in sorted(campaign.glob("n30-*-c*/")):
        arm = d.name.rsplit("-c", 1)[0].replace("n30-", "")
        if arms and arm not in arms:
            continue
        runs[arm].append(d)

    print("=" * 78)
    print("1b.  completeness by MEDIUM (which radio receives)")
    print("=" * 78)
    print(f"{'arm':16} {'medium':8} {'d':>2} {'rho':>7} {'P_comp':>8} "
          f"{'rho^d':>8} {'ratio':>6} {'n':>5}")
    link = defaultdict(list)                     # (arm, tx_kind, rx_kind) -> rhos
    for arm in sorted(runs):
        cell = defaultdict(lambda: ([], []))
        for run in (runs[arm][:cycles] if cycles else runs[arm]):
            kinds = {int(m.stem.split(".")[0]):
                     json.loads(m.read_text()).get("node_type")
                     for m in run.glob("*.meta.json")}
            for nid, rxk in sorted(kinds.items()):
                r = node_capture(run, nid)
                if r is None:
                    continue
                deg, rho, pc, _stale, _nw = r
                cell[(rxk, deg)][0].extend(rho.values())
                cell[(rxk, deg)][1].append(pc)
                for s, v in rho.items():
                    link[(arm, kinds.get(s), rxk)].append(v)
        for (kind, deg) in sorted(cell, key=lambda k: (str(k[0]), k[1])):
            rhos, pcs = cell[(kind, deg)]
            if not pcs:
                continue
            mr, mp = st.mean(rhos), st.mean(pcs)
            pred = mr ** deg
            print(f"{arm:16} {str(kind):8} {deg:>2} {mr:7.3f} {mp:8.4f} "
                  f"{pred:8.4f} {mp/pred if pred else float('nan'):6.2f} {len(pcs):5d}")

    print()
    print("=" * 78)
    print("1c.  per-link rho by TRANSMITTER -> RECEIVER, and dead links (rho < 0.30)")
    print("=" * 78)
    print(f"{'arm':16} {'tx':8} {'rx':8} {'rho':>7} {'sd':>7} {'dead':>11} {'n':>5}")
    for key in sorted(link, key=lambda k: (k[0], str(k[1]), str(k[2]))):
        v = link[key]
        if not v:
            continue
        dead = sum(1 for x in v if x < 0.30)
        print(f"{key[0]:16} {str(key[1]):8} {str(key[2]):8} {st.mean(v):7.3f} "
              f"{st.pstdev(v):7.3f} {dead:4d} ({dead/len(v):4.0%}) {len(v):5d}")
    print()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("campaign", type=Path)
    ap.add_argument("--arm", action="append", default=[],
                    help="restrict to an arm, e.g. ring4-40hz. Repeatable.")
    ap.add_argument("--plot", type=Path, default=None, metavar="DIR",
                    help="also write capture_completeness.png here")
    ap.add_argument("--by-medium", action="store_true",
                    help="split measurement 1 by node_type and by transmitter. "
                         "This is the measurement that identifies the mechanism; "
                         "the pooled tables above average the media together.")
    ap.add_argument("--cycles", type=int, default=None, metavar="N",
                    help="use only the first N cycles of each arm, for reading a "
                         "campaign that is still running")
    ap.add_argument("--no-coupling", action="store_true",
                    help="skip measurement 3, which loads every neighbour's log")
    a = ap.parse_args()
    if not a.campaign.is_dir():
        print(f"no such campaign: {a.campaign}", file=sys.stderr)
        return 2
    if a.by_medium:
        by_medium(a.campaign, a.arm, a.cycles)
    else:
        analyse(a.campaign, a.arm, not a.no_coupling)
    if a.plot:
        plot(a.campaign, a.plot)
    return 0


if __name__ == "__main__":
    sys.exit(main())
