"""Schematic of BLE advertising and scanning: why a neighbourhood goes stale.

    python3 plotting-tools/plot_ble_timing.py [--seed 7] [--span 380]

This figure is EMULATED, not measured, and the caption should say so. The run
logs record whether a neighbour's value was fresh in a given control sample;
they do not record sub-millisecond channel timing, because nothing in the
platform timestamps individual advertising PDUs. What is emulated is only the
timing, and every parameter is taken from the deployed manifests:

    advertising interval   drawn per event from U[100, 150] ms
    advertising event      one PDU on each of channels 37, 38, 39 in sequence
    PDU duration           0.376 ms, a 39-byte payload at 1 Mbit/s
    scan interval / window 20 / 18.75 ms, and the scanner advances one channel
                           per interval
    publication period     125 ms

The capture rule has two parts, both physical. A receiver hears a PDU only if
it is awake at that instant AND parked on that PDU's channel; and the PDU
survives only if no other transmission overlaps it on the same channel.

Both are needed. Scheduling alone loses about 8% of events at this duty cycle,
so a complete neighbourhood would be the usual outcome and the figure would
show nothing going wrong. The measured per-link delivery is 0.74, and the
difference is contention: an nRF here decodes roughly 1270 advertising reports
per second, of which about 45% come from equipment outside the experiment. The
background traffic below is drawn at that measured rate. Noise and coexistence
are still not modelled.

What the figure is for: the transmitters advertise on their own uncoordinated
phases, and the receiver must collect one fresh value from each within a single
publication period. Whether it succeeds is a matter of whose PDU happens to
land inside a scan window on the matching channel, and the bottom row shows how
often the answer is "not all of them".
"""
import argparse
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plotting-tools"))

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle

import utils_mpl
from palette import shades

utils_mpl.set_global(fontsize=14.0)

ADV_MIN, ADV_MAX = 100.0, 150.0      # ms, the manifest's dither range
PDU = 0.376                          # ms on air, 39 bytes at 1 Mbit/s
INTER_CH = 0.15                      # ms between the three channel slots
SCAN_INT, SCAN_WIN = 20.0, 18.75     # ms
T_PUB = 125.0                        # ms
CHANNELS = (37, 38, 39)
CH_COLOR = {37: "#4C72B0", 38: "#DD8452", 39: "#55A868"}
# Calibrated so the emulator reproduces the platform's measured per-link
# delivery of 0.74 on BLE receivers. The decoded-report rate (~420/s/channel)
# counts only transmissions that SURVIVED, so using it directly over-states the
# colliding population and yields rho = 0.65. This rate is the one that closes
# the loop: duty capture 0.92 times collision survival 0.81 gives 0.74.
BG_RATE = 290.0                      # foreign PDUs per second per channel


def advertiser(span, rng, phase):
    """[(t_start, channel)] for every PDU this transmitter emits."""
    out, t = [], phase
    while t < span:
        for k, ch in enumerate(CHANNELS):
            ts = t + k * (PDU + INTER_CH)
            if ts < span:
                out.append((ts, ch))
        t += rng.uniform(ADV_MIN, ADV_MAX)
    return out


def scanner(span):
    """[(start, end, channel)] for each scan window, channel advancing per interval."""
    out, k = [], 0
    while k * SCAN_INT < span:
        out.append((k * SCAN_INT, k * SCAN_INT + SCAN_WIN, CHANNELS[k % 3]))
        k += 1
    return out


def background(span, rng):
    """Foreign transmissions, Poisson at the measured channel occupancy."""
    out = []
    for ch in CHANNELS:
        n = rng.poisson(BG_RATE * span / 1000.0)
        out += [(float(x), ch) for x in rng.uniform(0, span, n)]
    return sorted(out)


def collided(pdu_t, pdu_ch, bg):
    """Another transmission overlaps this one on the same channel."""
    return any(ch == pdu_ch and abs(bt - pdu_t) < PDU for bt, ch in bg)


def captured(pdu_t, pdu_ch, windows, bg):
    """Awake at that instant, parked on that channel, and nothing collided."""
    if collided(pdu_t, pdu_ch, bg):
        return False
    for s, e, ch in windows:
        if s <= pdu_t <= e:
            return ch == pdu_ch
    return False


def _add_inset(ax, ev):
    """One advertising event at true scale, as an inset on the main axes.

    At a 500 ms span a 0.376 ms PDU is under 0.1% of the axis and reads as an
    instant, so the main panel cannot show that a transmission occupies real
    airtime. Off by default: on a slide the inset crowds the top of the panel,
    and the point is easier to say aloud than to read off a 1.6 ms box.
    """
    axi = ax.inset_axes([0.60, 0.78, 0.36, 0.18])
    for k, ch in enumerate(CHANNELS):
        ts = ev + k * (PDU + INTER_CH)
        axi.add_patch(Rectangle((ts, 0.15), PDU, 0.7, color=CH_COLOR[ch], lw=0))
        axi.annotate(f"{ch}", xy=(ts + PDU / 2, 0.92), ha="center", fontsize=9,
                     color=CH_COLOR[ch])
    axi.set_xlim(ev - 0.3, ev + 3 * (PDU + INTER_CH))
    axi.set_ylim(0, 1.15)
    axi.set_yticks([])
    axi.set_xticks([])
    axi.set_title(f"one advertising event at true scale: "
                  f"3 PDUs of {PDU:g} ms, {3 * (PDU + INTER_CH):.1f} ms total",
                  fontsize=10, pad=2)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--span", type=float, default=380.0, help="ms shown")
    ap.add_argument("--tx", type=int, default=4, help="number of transmitters")
    ap.add_argument("--inset", action="store_true",
                    help="add the true-scale detail of one advertising event. "
                         "Off by default: on a slide it crowds the top of the "
                         "panel, and the point it makes is easier to say aloud "
                         "than to read off a 1.6 ms box.")
    a = ap.parse_args()
    rng = np.random.default_rng(a.seed)
    span = a.span

    # N transmitters on independent phases: nothing coordinates them
    ntx = a.tx
    tx = [advertiser(span, rng, rng.uniform(0, ADV_MIN)) for _ in range(ntx)]
    win = scanner(span)
    bg = background(span, rng)
    tint = shades("ble", ntx, l_lo=0.35, l_hi=0.62)

    RX = 0.45
    STEP = 0.8
    # Tx1 at the top, stacked downwards, with the receiver below them all
    rows = {i: RX + 0.95 + (ntx - 1 - i) * STEP for i in range(ntx)}
    top = max(rows.values())
    fig, ax = plt.subplots(figsize=(11.5, 3.6 + 0.42 * ntx), dpi=200)

    # publication periods
    n_pub = int(span // T_PUB)
    for p in range(n_pub + 1):
        ax.axvline(p * T_PUB, color="0.55", ls="--", lw=1.0, zorder=0)
    for p in range(n_pub):
        ax.annotate(f"$T_{{pub}}$ \\#{p+1}", xy=((p + 0.5) * T_PUB, top + 0.32),
                    ha="center", fontsize=9, color="0.35")

    # scan windows: shaded band on the receiver row, coloured by channel listened to
    for s, e, ch in win:
        ax.add_patch(Rectangle((s, RX - 0.16), min(e, span) - s, 0.32,
                               color=CH_COLOR[ch], alpha=0.30, lw=0, zorder=1))

    # transmissions, and which of them were heard
    got = {p: set() for p in range(n_pub)}
    # every transmitter must be heard for the neighbourhood to be complete
    for i, pdus in enumerate(tx):
        y = rows[i]
        for t_, ch in pdus:
            hit = captured(t_, ch, win, bg)
            ax.vlines(t_, y - 0.17, y + 0.17, color=CH_COLOR[ch],
                      lw=2.0 if hit else 1.0, alpha=1.0 if hit else 0.40, zorder=3)
            if hit:
                ax.plot(t_, y + 0.30, marker="v", ms=5, color=CH_COLOR[ch], zorder=4)
                p = int(t_ // T_PUB)
                if p < n_pub:
                    got[p].add(i)
        ax.text(-6, y, f"Tx{i+1}", ha="right", va="center", fontsize=12,
                color=tint[i], fontweight="bold")
    ax.text(-6, RX, "Rx", ha="right", va="center", fontsize=12, fontweight="bold")


    # per publication period: which transmitters were heard from
    for p in range(n_pub):
        s = sorted(got[p])
        label = ",".join(f"{i+1}" for i in s) if s else "none"
        complete = len(s) == ntx
        ax.annotate(f"heard: {label}", xy=((p + 0.5) * T_PUB, -0.28), ha="center",
                    fontsize=9, color=("#2E7D32" if complete else "#B3261E"),
                    fontweight="bold" if complete else "normal")
        ax.annotate("complete" if complete else "stale",
                    xy=((p + 0.5) * T_PUB, -0.58), ha="center", fontsize=8,
                    color=("#2E7D32" if complete else "#B3261E"))

    ax.set_xlim(-14, span)
    ax.set_ylim(-0.8, top + 0.75)
    ax.set_yticks([])
    ax.set_xlabel("Time [ms]")
    for sp in ("left", "right", "top"):
        ax.spines[sp].set_visible(False)

    handles = [plt.Line2D([], [], color=CH_COLOR[c], lw=3, label=f"ch {c}")
               for c in CHANNELS]
    handles += [plt.Line2D([], [], color="0.4", marker="v", ls="none",
                           label="captured by Rx"),
                plt.Line2D([], [], color="0.4", lw=1, alpha=0.4, label="missed"),
                Rectangle((0, 0), 1, 1, color="0.5", alpha=0.30,
                          label="Rx scan window, tinted by the channel it is "
                                "parked on (advances every 20 ms)")]
    ax.legend(handles=handles, loc="upper center", bbox_to_anchor=(0.5, -0.16),
              ncol=3, fontsize=11, frameon=False)

    # --- optional inset: one advertising event at true scale ---------------
    # At a 500 ms span a 0.376 ms PDU is 0.08% of the axis and reads as an
    # instant. The inset restores the real proportions: three transmissions of
    # 0.376 ms each, on the three channels in sequence, spanning about 1.3 ms.
    if a.inset:
        _add_inset(ax, tx[0][0][0])

    out = ROOT / "plotting-tools/render/slides"
    out.mkdir(parents=True, exist_ok=True)
    fig.savefig(out / "slide_ble_timing.png", bbox_inches="tight", dpi=300)
    plt.close(fig)
    print(f"  wrote {out/'slide_ble_timing.png'}")

    n_ok = sum(len(v) == ntx for v in got.values())
    print(f"  emulated: {n_ok}/{n_pub} publication periods with the whole neighbourhood heard")
    for p in range(n_pub):
        print(f"     period {p+1}: heard from {sorted(i+1 for i in got[p]) or 'none'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
