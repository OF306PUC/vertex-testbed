#!/usr/bin/env python3
"""Plot a collected run into `results/<run>/`.

    python3 tools/plot_run.py runs/n6-fast-0
    python3 tools/plot_run.py runs/n6-fast-0 --out results --show-device-clock

`results/` is gitignored, like `runs/`: both are outputs, and a plot is
reproducible from the run it came from.

Colour is by **agent type**, not by node, because the platform's question is
BLE-versus-Wi-Fi-versus-bridge. Two agents of one type share a colour and differ by
line style, so a systematic split between the three shows up as three bands rather
than six unrelated lines.

Everything is plotted against `timestamp` -- this host's clock, on the run's shared
epoch -- and never against the nRF's own `device_timestamp`, which counts from its
own CONTROL arrival and is not comparable between nodes. The offset between the two
is itself a panel, since it is the serial transit and it is only measurable on a
relay.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")           # no display on a hub over ssh
import matplotlib.pyplot as plt

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vertex.analysis import Run, load_run  # noqa: E402

#: One colour per agent type. The comparison the testbed exists for.
COLOUR = {"ble": "#c0392b", "wifi": "#2471a3", "bridge": "#1e8449"}
STYLE = ["-", "--", ":", "-."]


def _style(run: Run, nid: int) -> dict:
    node = run.nodes[nid]
    same = [i for i in run.ids if run.nodes[i].node_type == node.node_type]
    return {"color": COLOUR.get(node.node_type, "#555"),
            "linestyle": STYLE[same.index(nid) % len(STYLE)],
            "linewidth": 1.2,
            "label": f"{nid} {node.node_type}"}


def plot_convergence(run: Run, out: Path) -> Path:
    fig, ax = plt.subplots(2, 1, figsize=(11, 7), sharex=True,
                           gridspec_kw={"height_ratios": [3, 2]})

    for nid in run.ids:
        n = run.nodes[nid]
        ax[0].plot(n.t, n.vstate, **_style(run, nid))
    ax[0].set_ylabel("vstate  z")
    ax[0].set_title(f"{run.name}  ({run.manifest}, {run.duration_s:g}s)  "
                    f"— virtual state, the quantity that is broadcast")
    ax[0].legend(ncol=3, fontsize=8, loc="upper right")
    ax[0].grid(alpha=0.25)

    # Spread on a log axis: finite-time convergence is the claim, and a log axis
    # is where "reaches zero" is distinguishable from "decays towards it".
    ts = [i * run.duration_s / 400 for i in range(401)]
    ax[1].semilogy(ts, [max(run.spread(t), 1e-9) for t in ts], color="#111",
                   linewidth=1.3)
    ax[1].set_ylabel("spread  max−min  (log)")
    ax[1].set_xlabel("t (s), host clock on the run's epoch")
    ax[1].grid(alpha=0.25, which="both")

    fig.tight_layout()
    p = out / "convergence.png"
    fig.savefig(p, dpi=130)
    plt.close(fig)
    return p


def plot_internals(run: Run, out: Path) -> Path:
    fig, ax = plt.subplots(2, 1, figsize=(11, 7), sharex=True)

    for nid in run.ids:
        n = run.nodes[nid]
        ax[0].plot(n.t, n.sigma, **_style(run, nid))
    delta = (run.nodes[run.ids[0]].meta.get("controller") or {}).get("delta")
    if delta:
        for sign in (1, -1):
            ax[0].axhline(sign * delta, color="#888", linestyle=":", linewidth=0.9)
        ax[0].text(0.005, delta, f" delta = {delta:g}", va="bottom", fontsize=8,
                   color="#666", transform=ax[0].get_yaxis_transform())
    ax[0].set_ylabel("sigma = x − z")
    ax[0].set_title("local error, and the adaptive gain it drives")
    ax[0].legend(ncol=3, fontsize=8)
    ax[0].grid(alpha=0.25)

    for nid in run.ids:
        n = run.nodes[nid]
        ax[1].plot(n.t, n.vartheta, **_style(run, nid))
    ax[1].set_ylabel("vartheta  θ")
    ax[1].set_xlabel("t (s)")
    ax[1].grid(alpha=0.25)

    fig.tight_layout()
    p = out / "internals.png"
    fig.savefig(p, dpi=130)
    plt.close(fig)
    return p


def plot_links(run: Run, out: Path, window_s: float = 5.0) -> Path:
    """Freshness per link over time, plus the whole-run figure per link.

    Freshness, not delivery ratio: the bit says a packet arrived from that
    neighbour inside its window, so it is bounded by the ratio of publish period to
    report period as much as by the radio. Read it as "was this link live", and
    compare links of the same medium rather than across media.
    """
    links = run.links()
    fig, ax = plt.subplots(2, 1, figsize=(11, 7),
                           gridspec_kw={"height_ratios": [2, 1]})

    for src, dst, _ in links:
        n = run.nodes[dst]
        f = n.neighbour_fresh(src)
        k = max(1, int(window_s * max(n.rate_hz, 1e-9)))
        roll = [sum(f[max(0, i - k):i + 1]) / len(f[max(0, i - k):i + 1])
                for i in range(len(f))]
        medium = ("BLE" if run.nodes[src].node_type == "ble"
                  or n.node_type == "ble" else "UDP")
        ax[0].plot(n.t, roll, linewidth=1.0,
                   color="#c0392b" if medium == "BLE" else "#2471a3",
                   alpha=0.8, label=f"{src}→{dst} {medium}")
    ax[0].set_ylabel(f"freshness, {window_s:g}s rolling")
    ax[0].set_ylim(-0.05, 1.05)
    ax[0].set_title("per-link freshness  (red = BLE, blue = UDP)")
    ax[0].legend(ncol=4, fontsize=7)
    ax[0].grid(alpha=0.25)
    ax[0].set_xlabel("t (s)")

    labels = [f"{s}→{d}" for s, d, _ in links]
    vals = [f for _, _, f in links]
    cols = ["#c0392b" if (run.nodes[s].node_type == "ble"
                          or run.nodes[d].node_type == "ble") else "#2471a3"
            for s, d, _ in links]
    ax[1].bar(range(len(links)), vals, color=cols)
    ax[1].set_xticks(range(len(links)))
    ax[1].set_xticklabels(labels, rotation=45, ha="right", fontsize=7)
    ax[1].set_ylabel("whole run")
    ax[1].set_ylim(0, 1.05)
    ax[1].grid(alpha=0.25, axis="y")

    fig.tight_layout()
    p = out / "links.png"
    fig.savefig(p, dpi=130)
    plt.close(fig)
    return p


def plot_clocks(run: Run, out: Path) -> Path | None:
    """device_timestamp − timestamp, which is only non-zero for a relay.

    For a `ble` agent this is the serial transit plus the gap between a state being
    computed and its report being assembled. A drift across the run is the board's
    crystal against this host's chrony-corrected clock.
    """
    relays = [n for n in run.nodes.values()
              if any(abs(o) > 1e-9 for o in n.clock_offset_s)]
    if not relays:
        return None

    fig, ax = plt.subplots(figsize=(11, 4))
    for n in relays:
        ax.plot(n.t, [o * 1000 for o in n.clock_offset_s], linewidth=1.0,
                **{k: v for k, v in _style(run, n.node_id).items()
                   if k in ("color", "linestyle", "label")})
    ax.set_ylabel("device − host  (ms)")
    ax.set_xlabel("t (s)")
    ax.set_title("relay clock offset: serial transit, and any crystal drift")
    ax.legend(fontsize=8)
    ax.grid(alpha=0.25)
    fig.tight_layout()
    p = out / "clock_offset.png"
    fig.savefig(p, dpi=130)
    plt.close(fig)
    return p


def summarise(run: Run) -> str:
    lines = [f"{run.name}  manifest={run.manifest}  {run.duration_s:g}s",
             f"  trigger spread {run.report.get('trigger_spread_s', 0)*1000:.0f} ms",
             "",
             f"  {'node':>5} {'type':<7} {'rows':>6} {'Hz':>6} "
             f"{'vstate first':>13} {'vstate last':>12}"]
    for nid in run.ids:
        n = run.nodes[nid]
        lines.append(f"  {nid:>5} {n.node_type:<7} {len(n.t):>6} {n.rate_hz:>6.1f} "
                     f"{n.vstate[0]:>13.6f} {n.vstate[-1]:>12.6f}")
    lines += ["", "  spread over time:"]
    for frac in (0.0, 0.1, 0.25, 0.5, 0.75, 1.0):
        t = frac * run.duration_s
        lines.append(f"    t={t:>7.1f}s   {run.spread(t):.6f}")
    lines += ["", f"  {'link':>10} {'medium':<7} freshness"]
    for src, dst, f in run.links():
        medium = ("BLE" if run.nodes[src].node_type == "ble"
                  or run.nodes[dst].node_type == "ble" else "UDP")
        flag = "" if f > 0.9 else "   <-- low"
        lines.append(f"  {f'{src}→{dst}':>10} {medium:<7} {f:.3f}{flag}")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir", type=Path)
    ap.add_argument("--out", type=Path, default=Path("results"))
    ap.add_argument("--window", type=float, default=5.0,
                    help="rolling window for freshness, seconds")
    args = ap.parse_args()

    run = load_run(args.run_dir)
    out = args.out / run.name
    out.mkdir(parents=True, exist_ok=True)

    made = [plot_convergence(run, out), plot_internals(run, out),
            plot_links(run, out, args.window)]
    clocks = plot_clocks(run, out)
    if clocks:
        made.append(clocks)

    text = summarise(run)
    (out / "summary.txt").write_text(text + "\n", encoding="utf-8")
    print(text)
    print()
    for p in made + [out / "summary.txt"]:
        print(f"  wrote {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
