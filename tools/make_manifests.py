#!/usr/bin/env python3
"""Emit the experiment manifests. Run: ``python3 tools/make_manifests.py``

This replaces a hand-maintained topology table. Hosts are declared once; graph
structure comes from the generators; irregular graphs (the clustered one) declare
their edges explicitly. 
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vertex.topology.generators import ring  # noqa: E402
from vertex.topology.loader import load_manifest  # noqa: E402
from vertex.topology.validate import check  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "experiments"

#: Physical hosts, in a fixed order. Ten Raspberry Pis, each running up to three
#: agents. These are the current lab addresses; they change when a host is
#: re-imaged or moved to a different interface, and they are the ONLY part of a
#: manifest that is not derivable.
HOSTS = ["10.6.5.1", "10.6.5.2", "10.6.5.4", "10.6.5.5", 
         "10.6.5.7", "10.6.5.8", "10.6.5.9", "10.6.5.10",
         "10.6.5.12", "10.6.5.13"]

#: Ids 1-10 are BLE agents, 11-20 Wi-Fi, 21-30 bridges; agent k of each band
#: lives on HOSTS[k]. Bridges carry both radios, which is why they are the ones
#: that join the BLE and Wi-Fi subnets.
BANDS = [("ble", 1), ("wifi", 11), ("bridge", 21)]

CONTROLLER = {
    "name": "finite_time_adaptive",
    "dt_s": 0.2,
    # Rates, not per-step increments: the recursion is x += dt*(u+nu), so a gain
    # means the same thing at any dt. Rescaled 2026-09-04 from the per-step form
    # by gain/dt and eta/dt^2, which preserves every previously measured
    # trajectory exactly.
    "eta": 5e-5,                        # was 2e-6 per step
    "gain_ij": 0.1,                     # was 0.02 per step
    "alpha": 0.5,
    "delta": 0.01,
    "disturbance": {
        "enabled": True,
        "noise_amplitude": 2.5e-3,
        "noise_offset": 0.5,
        "beta": 5e-4,
        "sine_amplitude": 3.75e-3,
        "sine_frequency_hz": 2.0,
        "period_samples": 1000,
        # sine_phase_s omitted: each node derives a distinct reproducible phase
        # from `seed`, so the fleet is not disturbed in lockstep.
    },
}


#: 25 Hz dynamics, 5 Hz publish. Same continuous-time experiment as CONTROLLER,
#: sampled five times finer -- which requires rescaling, not just a smaller dt.
#:
#: `alpha` and `eta` are PER-STEP gains: the law does `vstate += alpha * v_i` with
#: no dt anywhere, so the per-second gain is alpha/dt. They are kept at
#: CONTROLLER's values by request, which makes the coupling 5x faster per second
#: than n6-ring rather than equivalent to it.
#:
#: `delta` is a dead-band in state units rather than a rate, so it does not scale.
#:
#: `beta` and `sine_amplitude` are dt-invariant: the step adds `disturbance * dt`,
#: so their per-second contribution is independent of dt.
#:
#: `noise_amplitude` is NOT. Independent draws accumulate as a random walk with
#: std proportional to amp*sqrt(dt), so a 5x smaller dt gives sqrt(5) less noise
#: for the same amplitude. Multiplied by sqrt(5) to hold the noise power fixed.
CONTROLLER_FAST = {
    "name": "finite_time_adaptive",
    "dt_s": 0.04,                       # 25 Hz
    # Reverted to CONTROLLER's values on request. Note the consequence: alpha is
    # a per-step gain, so at dt=0.04 the per-second coupling is alpha/dt =
    # 0.5 /s against n6-ring's 0.1 /s. This configuration converges ~5x faster in
    # wall-clock terms and is NOT the same continuous-time experiment as n6-ring
    # -- deliberate, but the two are no longer directly comparable.
    "eta": 1.25e-3,                     # was 2e-6 per step (/dt^2)
    "gain_ij": 0.5,                     # was 0.02 per step (/dt)
    "alpha": 0.5,
    "delta": 0.01,                      # unchanged: a threshold, not a rate
    "disturbance": {
        "enabled": True,
        "noise_amplitude": 5.5902e-3,   # 2.5e-3 * sqrt(5)
        "noise_offset": 0.5,
        "beta": 5e-4,                   # dt-invariant
        "sine_amplitude": 3.75e-3,      # dt-invariant
        "sine_frequency_hz": 11.0,
        # 3000 * 0.04 s = 120 s, so the disturbance does not repeat inside a
        # 120 s run. At the old 1000 it would cycle every 40 s and repeat three
        # times, correlating the run with its own disturbance.
        "period_samples": 3000,
        # sine_phase_s omitted: each node derives a distinct reproducible phase
        # from `seed`, so the fleet is not disturbed in lockstep.
    },
}


#: The advertising interval is a DELIVERY CEILING, not a tuning knob.
#:
#: `broadcaster_update()` / `cmd_le_set_adv_data` rewrite the payload; neither
#: changes how often the controller radiates. A neighbour therefore observes at
#: most one distinct value per advertising interval, so publishing faster than
#: T_adv overwrites values before they are ever transmitted:
#:
#:     delivery <= min(1, publish_period_s / adv_interval_ms*1e-3)
#:
#: That is undersampling at the TRANSMITTER, and it is indistinguishable from
#: loss in any statistic counting distinct sequence numbers. Measured over a
#: 4-point x 10-repeat sweep: the two points where the bound was active
#: normalised to 0.818 and 0.826 of it, the two unconstrained points to 0.997
#: and 0.962. See docs/PLATFORM.md A3.4.
#:
#: So a manifest that sets `publish_period_s` without setting `radio` is only
#: honest while publish_period_s >= 0.1 (the RadioSpec default). Below that it
#: measures the advertising interval. Use this helper instead of writing the
#: block by hand -- it keeps the two values in step, which is precisely what
#: drifted apart in the original experiments and drew the reviewer's question.
#: The CYW43455 rejects ADV_NONCONN_IND below this with "invalid HCI command
#: parameters" (opcode 0x2006). Bluetooth 4.x required it; 5.0 dropped it; this
#: controller kept it. Measured with `scripts/adv_floor.py`, after it cost 10 runs.
ADV_FLOOR_MS = 100.0


def radio_for(publish_period_s: float, *, adv_interval_ms: float | None = None,
              adv_interval_max_ms: float | None = None,
              scan_interval_ms: float | None = None,
              scan_window_ms: float | None = None,
              scan_ratio: float = 1.0) -> dict:
    """RadioSpec for a publish rate. Defaults to advertising AS FAST AS ALLOWED.

    The obvious-looking choice, `adv = publish`, is wrong, and the sweep measured
    how wrong. The delivery ceiling is `min(1, T_pub/T_adv)`, which is 1.0 for
    *any* `T_adv <= T_pub` -- so matching them buys nothing the floor does not
    already buy, and it throws away redundancy. The number of advertising events
    carrying one published value is `k = T_pub / T_adv`, and at `adv = publish`
    that is 1: a single lost advertisement is a lost value.

    Measured at 5 Hz publish, both configurations at ceiling 1.0:

        adv 200 ms (k=1):  nRF->nRF 0.883   Pi->nRF 0.868   nRF->Pi 0.684
        adv 100 ms (k=2):  nRF->nRF 0.964   Pi->nRF 0.934   nRF->Pi 0.832

    +0.07 to +0.15 for nothing but advertising twice as often. So the default is
    the floor, always.

    Pass `adv_interval_ms` explicitly only to vary AIRTIME deliberately -- that is
    what the sweep manifests do, and they pay k=1 for it. To vary airtime without
    that cost, vary the number of advertisers instead (PLATFORM.md 8.3).
    """
    adv_ms = ADV_FLOOR_MS if adv_interval_ms is None else float(adv_interval_ms)
    if adv_ms < ADV_FLOOR_MS:
        raise ValueError(f"adv_interval_ms={adv_ms} is below the {ADV_FLOOR_MS:g} ms "
                         f"controller floor; see scripts/adv_floor.py")
    if adv_ms > 10240.0:
        raise ValueError(f"adv_interval_ms={adv_ms} exceeds the 10240 ms maximum")
    si = adv_ms if scan_interval_ms is None else float(scan_interval_ms)
    sw = (round(si * scan_ratio, 4) if scan_window_ms is None
          else float(scan_window_ms))
    if sw > si:
        raise ValueError(f"scan_window_ms={sw} exceeds scan_interval_ms={si}")
    out = {
        "adv_interval_ms": adv_ms,
        "scan_interval_ms": si,
        "scan_window_ms": sw,
    }
    if adv_interval_max_ms is not None:
        amax = float(adv_interval_max_ms)
        if amax < adv_ms:
            raise ValueError(f"adv_interval_max_ms={amax} is below "
                             f"adv_interval_ms={adv_ms}")
        out["adv_interval_max_ms"] = amax
    return out


def ceiling_for(publish_period_s: float) -> float:
    """Highest delivery ratio attainable at this publish rate, given the floor."""
    return min(1.0, publish_period_s * 1000.0 / max(ADV_FLOOR_MS,
                                                    publish_period_s * 1000.0))


#: 50 Hz dynamics, 10 Hz publish. The configuration the sweep points to.
#:
#: 10 Hz publish is the fastest rate that is not undersampled: the advertising
#: floor is 100 ms (PLATFORM.md 6.3), so 10 Hz is exactly the highest publish rate
#: with a ceiling of 1.0. Faster publishing cannot be carried -- p080 and p040
#: both delivered ~8.7 values/s, the same as this, while paying a 0.80 and 0.40
#: ceiling for it. Publishing at 10 Hz gets the same information rate with no
#: undersampling and a healthy staleness margin (window 300 ms vs a 114 ms arrival
#: gap, 2.6x -- so PLATFORM.md 8.4 does not bite here).
#:
#: The gain from dt=0.02 is on the LOCAL side, not the network side: the 11 Hz
#: disturbance gets 4.55 samples per cycle instead of 2.27, which was marginal
#: against a 12.5 Hz Nyquist limit. The network is already saturated either way.
#:
#: Every rate-dependent parameter is rescaled from CONTROLLER_FAST by f = dt_new/
#: dt_old = 0.5, per the rules in that block:
#:   alpha, eta    x0.5     -- per-STEP gains, so per-second = alpha/dt must hold
#:   delta         unchanged -- a threshold in state units, not a rate
#:   beta, sine    unchanged -- dt-invariant, the step adds disturbance*dt
#:   noise         xsqrt(2)  -- independent draws accumulate as amp*sqrt(dt)
#:   period_samples x2       -- still 120 s, or the disturbance repeats mid-run
#:
#: The sine is 2 Hz, NOT n6-fast's 11 Hz, and the reason is that there are two
#: sampling rates here rather than one:
#:
#:   dt      = 50 Hz  -> what the local integrator sees   (Nyquist 25 Hz)
#:   publish = 10 Hz  -> what NEIGHBOURS see              (Nyquist  5 Hz)
#:
#: 11 Hz clears the first and fails the second: sampled at the 10 Hz publish rate
#: it folds to 1 Hz, so every neighbour reads a 1 Hz artefact that no agent is
#: actually producing. n6-fast did not have this problem because its sine was
#: chosen against dt alone and its publish rate happened to be 5 Hz.
#:
#: 2 Hz clears both -- 25 samples/cycle locally, 5 on the exchanged stream -- and
#: still avoids the lockstep pathology: 0.04 cycles/step x 5 steps = 0.2 cycles
#: per publish period, not a whole number, so it cannot sum to zero.
#:
#: Choosing a disturbance frequency requires checking it against the SLOWER of the
#: two rates. The publish rate is the one that reaches the control law.
CONTROLLER_50HZ = {
    "name": "finite_time_adaptive",
    "dt_s": 0.02,                       # 50 Hz
    # NOTE the eta relationship to CONTROLLER_FAST is NOT 1:1. In the per-step
    # form eta was halved with the dt ratio when preserving the continuous rate
    # needed a quartering, so the 50 Hz configuration has always adapted twice as
    # fast as the 25 Hz one. These values preserve that, deliberately: changing
    # it would invalidate every 50 Hz run collected so far. See PLATFORM.md.
    "eta": 2.5e-3,                      # was 1e-6 per step (/dt^2)
    "gain_ij": 0.5,                     # was 0.01 per step (/dt)
    "alpha": 0.5,
    "delta": 0.01,
    "disturbance": {
        "enabled": True,
        "noise_amplitude": 7.9057e-3,   # 5.5902e-3 * sqrt(2)
        "noise_offset": 0.5,
        "beta": 5e-4,
        "sine_amplitude": 3.75e-3,
        "sine_frequency_hz": 2.0,       # see the note above: 11 Hz folds to 1 Hz
        "period_samples": 6000,         # 6000 * 0.02 s = 120 s
    },
}


#: The two-host family runs on hosts 2 and 3 of the declared list, not 1 and 2.
#:
#: Not tidiness -- reproducibility. Every n6 run collected so far (n6-fast x10,
#: n650 x10, the four sweep points x10) has node 1 on the second declared host and
#: node 2 on the third. Section 6.4 showed the receiver asymmetry is a property of
#: a specific board's front-end, so re-running n6 on a different pair is a
#: different experiment, not a replication.
#:
#: Nine agents use all three in order, so `n9-*` has node k of each band on host k
#: and needs no exception.
def n6_pair() -> list[str]:
    return HOSTS[1:3] if len(HOSTS) >= 3 else HOSTS[:2]


def hosts_for(n_per_band: int = 10, publish_period_s: float = 1.0,
              hosts: list[str] | None = None) -> list[dict]:
    """Declare the agent-to-host binding, without any graph structure.

    `hosts` overrides the full lab list, for a manifest that runs on a subset.
    The band offsets are kept whatever the size, so a node id still says what
    transport it is: 1.. is BLE, 11.. Wi-Fi, 21.. bridge, in every manifest.
    """
    pool = hosts if hosts is not None else HOSTS
    if n_per_band > len(pool):
        raise ValueError(
            f"{n_per_band} agents per band needs {n_per_band} hosts, got {len(pool)}; "
            f"two agents of the same type on one address collide at bind time"
        )
    nodes = []
    for kind, base in BANDS:
        for k in range(n_per_band):
            nodes.append({
                "id": base + k,
                "ip": pool[k],
                "type": kind,
                "enabled": True,
                "publish_period_s": publish_period_s,
            })
    return sorted(nodes, key=lambda n: n["id"])


# ── the clustered topology: two 10-agent groups per transport, bridge-joined ──
# Irregular by design, so its edges are declared. Agents 21 and 30 are the
# cut-points that connect the BLE and Wi-Fi subnets, and are disabled so the
# clusters coordinate only through the remaining bridges.
CLUSTER_EDGES = {
    1: [2, 3, 21],   2: [1, 4],      3: [1, 5],      4: [2, 5, 6],
    5: [3, 4, 7],    6: [4, 7, 8],   7: [5, 6, 9],   8: [6, 10],
    9: [7, 10],     10: [8, 9, 30],
    11: [12, 13, 21], 12: [11, 14],  13: [11, 15],   14: [12, 15, 16],
    15: [13, 14, 17], 16: [14, 17, 18], 17: [15, 16, 19], 18: [16, 20],
    19: [17, 20],   20: [18, 19, 30],
    21: [1, 11, 22, 23], 22: [21, 24], 23: [21, 25], 24: [22, 25, 26],
    25: [23, 24, 27], 26: [24, 27, 28], 27: [25, 26, 29], 28: [26, 30],
    29: [27, 30],   30: [10, 20, 28, 29],
}
CLUSTER_DISABLED = {21, 30}


#: The first-run subset: three Pis, nine agents. Smallest configuration that
#: exercises every path the platform compares -- BLE-only, Wi-Fi-only, and the
#: bridge that carries both -- with each host running all three at once, which is
#: where the CYW43455 coexistence effect actually appears.
FIRST_RUN_HOSTS = list(HOSTS[:3])

#: Traversal order for the generated topologies: the ble block, five bridges, the
#: wifi block, five bridges. A generator walks its id list in order, so this is
#: what keeps a bridge at every ble/wifi boundary -- and five of them, so even the
#: degree-4 ring clears the gap. Consecutive entries are also on different hosts,
#: so no link is intra-host (a local UDP broadcast never reaches the radio).
BAND_ORDER = (list(range(1, 11))       # ble     1..10
              + list(range(21, 26))    # bridge 21..25
              + list(range(11, 21))    # wifi   11..20
              + list(range(26, 31)))   # bridge 26..30


def manifests() -> dict[str, dict]:
    """Every manifest that the declared hosts can actually accommodate.

    A manifest needing more hosts than exist is skipped with a note rather than
    aborting the run: `--hosts` with two addresses is a two-host bench, and it
    should still get its two-host manifests written.
    """
    out: dict[str, dict] = {}
    if len(HOSTS) < 2:
        # Every edge in every manifest must cross hosts -- an intra-host link
        # never reaches the radio -- so one host can build nothing at all.
        print(f"skip     everything             needs >=2 hosts, "
              f"{len(HOSTS)} declared")
        return out


    # ── n4: two hosts, no nRF. The step before n6. ──────────────────────────
    # Only `wifi` and `bridge`, so **no nRF is needed at all** -- no serial link,
    # no firmware to flash. Useful when the second board is not ready, and useful
    # on its own: `bridge` and `wifi` run the SAME controller in the same process,
    # so a difference between them is the medium and not the implementation. That
    # is the platform's cleanest comparison and this is the smallest manifest that
    # makes it.
    #
    # Covers UDP between hosts (11-12) and Pi-to-Pi BLE via the two bridges
    # (21-22), which n6-ring does not -- its cycle leaves the 21-22 edge out.
    #
    # 4-cycle: 11(wifi,h0) - 12(wifi,h1) - 21(bri,h0) - 22(bri,h1) - back to 11.
    # Every edge crosses hosts; no ble/wifi pair exists to worry about.
    N4_ORDER = [11, 12, 21, 22]
    n4 = [n for n in hosts_for(2, hosts=n6_pair()) if n["type"] != "ble"]
    n4_edges = ring(ids=N4_ORDER)
    for node in n4:
        node["neighbors"] = n4_edges[node["id"]]
    out["n4-noble"] = {
        "name": "n4-noble",
        "description": (
            "4 agents on 2 hosts, wifi + bridge only: no nRF, no firmware, no "
            "serial link. The smallest manifest that isolates the medium -- "
            "`bridge` and `wifi` run the same controller in the same process, so a "
            "difference between them is the transport. Also the only manifest that "
            "exercises bridge-to-bridge BLE, which n6-ring's cycle omits."
        ),
        "seed": 20260818,
        "controller": CONTROLLER,
        "nodes": n4,
    }

    # ── n6-fast: the symmetric-rate configuration ───────────────────────────
    # Same topology as n6-ring, same forced ordering, different rates: 25 Hz
    # dynamics and a 5 Hz publish, with the gains rescaled so the continuous-time
    # dynamics are unchanged (see CONTROLLER_FAST).
    #
    # The sine is at 11 Hz, not 10. At dt = 0.04 a 10 Hz sine advances 0.4 cycles
    # per step = 2/5, so its evaluated sequence repeats every 5 steps -- exactly
    # the publish period -- and those five values sum to zero. That holds for ANY
    # initial phase: 0.4*(n+5) = 0.4n + 2 is two whole cycles later regardless of
    # phase, and the five samples are the 5th roots of unity rotated by it. So the
    # per-node phase desynchronises the nodes from EACH OTHER, which is real, but
    # cannot desynchronise a node from its own publish rate. The sine would ripple
    # at 25 Hz and contribute exactly nothing cumulative.
    #
    # 11 Hz gives 11/25 cycles per step: a 25-step repeat (1.00 s), 25 distinct
    # values, no coincidence with the 5-step publish period, and a non-zero net
    # contribution per window. It is the fastest frequency this step rate carries
    # cleanly -- 2.27 samples per cycle, inside the 12.5 Hz Nyquist limit.
    n6f = hosts_for(2, hosts=n6_pair())
    n6f_edges = ring(ids=[1, 2, 21, 12, 11, 22])
    for node in n6f:
        node["neighbors"] = n6f_edges[node["id"]]
        node["publish_period_s"] = 0.2
    out["n6-fast"] = {
        "name": "n6-fast",
        "description": (
            "n6-ring's topology at 25 Hz dynamics and a 5 Hz publish, with alpha "
            "and eta divided by 5 so the continuous-time dynamics match n6-ring "
            "and the two are comparable. Symmetric rates: the nRF now absorbs, "
            "steps and reports at dt and publishes at publish_period_s, exactly "
            "as a Pi agent does. Sine disturbance at 11 Hz, chosen so its "
            "evaluated sequence does not repeat on the publish period."
        ),
        "seed": 20260818,
        "controller": CONTROLLER_FAST,
        "nodes": n6f,
    }

    # ── n6-50hz: 50 Hz dynamics, 10 Hz publish ──────────────────────────────
    # Same forced ring as n6-fast, same seed, so it is directly comparable.
    # Advertising at the floor via radio_for's default, which is also exactly the
    # publish period here -- the one rate where "match the publish period" and
    # "advertise as fast as allowed" coincide.
    n650 = hosts_for(2, hosts=n6_pair())
    n650_edges = ring(ids=[1, 2, 21, 12, 11, 22])
    for node in n650:
        node["neighbors"] = n650_edges[node["id"]]
        node["publish_period_s"] = 0.1
    out["n6-50hz"] = {
        "name": "n6-50hz",
        "description": (
            "6 agents, 50 Hz dynamics, 10 Hz publish. 10 Hz is the fastest "
            "publish rate the 100 ms advertising floor can carry without "
            "undersampling, so this is the predicted optimum: the same ~8.7 "
            "delivered values/s as the 12.5 and 25 Hz sweep points, but at a "
            "ceiling of 1.0 and with a 2.6x staleness margin. dt=0.02 resolves "
            "the 2 Hz disturbance at 25 samples/cycle locally and 5 on the "
            "exchanged stream, so it is unaliased at both rates. Gains "
            "rescaled from n6-fast by dt ratio 0.5; same seed and topology."
        ),
        "seed": 20260818,
        "controller": CONTROLLER_50HZ,
        "radio": radio_for(0.1),
        "nodes": n650,
    }

    # ── n6: two hosts, six agents. The step before n9. ──────────────────────
    # At this size the topology is FORCED, not chosen. Every edge must cross hosts
    # (an intra-host link never reaches the radio) and must not join `ble` to
    # `wifi` (no shared medium), which leaves seven legal edges among the six
    # agents -- and exactly ONE Hamiltonian cycle through them:
    #
    #   1(ble,h0) - 2(ble,h1) - 21(bri,h0) - 12(wifi,h1) - 11(wifi,h0) - 22(bri,h1)
    #
    # Every path the platform compares appears once: nRF-to-nRF (1-2), Pi-to-Pi
    # over BLE (via the bridges), UDP (11-12), and both mixed hops where an nRF
    # advertises and a Pi's HCI scanner receives it (2-21, 22-1) -- the direction
    # loopback test B validated. Degree 2, lambda_2 = 1.0.
    #
    # The seventh legal edge, 21-22, is the only densification available; adding it
    # takes the bridges to degree 3.
    N6_ORDER = [1, 2, 21, 12, 11, 22]
    n6 = hosts_for(2, hosts=n6_pair())
    n6_edges = ring(ids=N6_ORDER)
    for node in n6:
        node["neighbors"] = n6_edges[node["id"]]
    out["n6-ring"] = {
        "name": "n6-ring",
        "description": (
            "6 agents on 2 hosts: the two-host bring-up. Each host runs ble + wifi "
            "+ bridge. The only 6-cycle that both crosses hosts on every edge and "
            "never joins ble to wifi, so the ordering is forced rather than "
            "chosen. Covers nRF-to-nRF, bridge-to-bridge over BLE, UDP, and both "
            "nRF-advertises/Pi-scans hops."
        ),
        "seed": 20260818,
        "controller": CONTROLLER,
        "nodes": n6,
    }

    # ── n9: the bring-up target ──────────────────────────────────────────────
    # A 9-cycle, but the order is not free. Two constraints bind it, and the
    # obvious ordering (1,2,3,11,12,13,21,22,23) violates both:
    #
    # 1. NO ble-wifi EDGE. A `ble` agent transmits only on its radio and a `wifi`
    #    agent only on a socket, so such a link cannot carry a packet -- the
    #    validator now rejects it. The three bridges are the only agents on both
    #    media, so they must sit at every boundary between the two blocks. That
    #    forces the shape: bridge, the ble run, bridge, the wifi run, bridge.
    #
    # 2. NO INTRA-HOST EDGE. Agents k of each band share host k, and a link
    #    between two agents on one host does not use the radio at all: a local UDP
    #    broadcast is delivered by the kernel, and two BLE radios centimetres
    #    apart are not a link under test. Such a link reports ~100% delivery and
    #    ~0 delay, flattering any average it lands in.
    #
    # The ordering below satisfies both, which is why it is written out rather
    # than generated. Hosts are h0/h1/h2 for the three Pis:
    #
    #   21(h0,bri) 2(h1,ble) 1(h0,ble) 3(h2,ble) 22(h1,bri)
    #   13(h2,wifi) 11(h0,wifi) 12(h1,wifi) 23(h2,bri)  -> back to 21
    #
    # Degree 2 everywhere, well inside the firmware's 4-neighbour limit.
    N9_ORDER = [21, 2, 1, 3, 22, 13, 11, 12, 23]
    # NOT `return out`: bailing out of the whole function here silently dropped
    # every manifest defined below, so a 2-host lab quietly produced 3 manifests
    # instead of 11 and said only "skip n9-ring".
    if len(FIRST_RUN_HOSTS) < 3:
        print(f"skip     n9-ring                  needs 3 hosts, "
              f"{len(FIRST_RUN_HOSTS)} declared")
    if len(FIRST_RUN_HOSTS) >= 3:
        n9 = hosts_for(3, hosts=FIRST_RUN_HOSTS)
        n9_edges = ring(ids=N9_ORDER)
        for node in n9:
            node["neighbors"] = n9_edges[node["id"]]
        out["n9-ring"] = {
            "name": "n9-ring",
            "description": (
                "9 agents on 3 hosts: the bring-up target. Every host runs ble + wifi "
                "+ bridge, so all three paths are exercised and the two radios on each "
                "Pi contend as they will in a full run. Undirected 9-cycle, degree 2 "
                "everywhere. The ordering is constrained, not arbitrary: bridges sit "
                "at each ble/wifi boundary because those two share no medium, and no "
                "link is intra-host because such a link never reaches the radio. "
                "Edges are declared for exactly that reason."
            ),
            "seed": 20260818,
            "controller": CONTROLLER,
            "nodes": n9,
        }

        # ── n9-50hz: the validated rate configuration at 9 agents ───────────
        # Same controller as n6-50hz, so the two are directly comparable, and the
        # same forced ordering. Degree 2, inside the firmware's 4-neighbour limit.
        #
        # This doubles as the airtime experiment of section 8.3: advertisers go
        # from 4 to 6, so BLE duty goes 3.46% -> 5.18% at 1.5x with every
        # per-node parameter held. Note which comparisons are clean:
        #
        #   DELIVERY is a per-link property and IS comparable, so a drop from
        #   n6-50hz's 0.877 / 0.857 / 0.663 would be an airtime effect.
        #
        #   CONVERGENCE is NOT: lambda_2 falls 1.0 -> 0.4679, so ~2.1x slower
        #   (about 28 s) is expected from the graph alone. Comparing raw
        #   convergence would mix airtime with topology.
        n950 = hosts_for(3, hosts=FIRST_RUN_HOSTS)
        n950_edges = ring(ids=N9_ORDER)
        for node in n950:
            node["neighbors"] = n950_edges[node["id"]]
            node["publish_period_s"] = 0.1
        out["n9-50hz"] = {
            "name": "n9-50hz",
            "description": (
                "9 agents on 3 hosts at n6-50hz's rates: 50 Hz dynamics, 10 Hz "
                "publish, 2 Hz sine, advertising at the 100 ms floor. Directly "
                "comparable to n6-50hz on per-link DELIVERY, which makes it the "
                "airtime test -- 6 advertisers instead of 4, so 5.18% BLE duty "
                "against 3.46%, every other parameter held. NOT comparable on "
                "convergence: lambda_2 is 0.4679 against 1.0, so roughly 2.1x "
                "slower is expected from topology alone."
            ),
            "seed": 20260818,
            "controller": CONTROLLER_50HZ,
            "radio": radio_for(0.1),
            "nodes": n950,
        }

        # ── n9-jitter: the same run with an advertising interval RANGE ────────
        # Every run so far advertised with interval_min == interval_max, so the
        # controller had no range to spread events over. At the trigger all nine
        # agents start within ~10-30 ms, so every advertiser's 100 ms cycle begins
        # nearly in phase -- and a radio cannot scan while it advertises, so a
        # pair whose events coincide is blind to each other until their clocks
        # drift apart. Measured as one link silent for 7.5-26 s at run start in
        # ~10% of nine-agent runs, and only ~3% of six-agent ones, which is the
        # right direction for a collision explanation (PLATFORM.md 6.10).
        #
        # 100-120 ms gives the controller a 20 ms spreading range on both ends.
        # Everything else is identical to n9-50hz, so this is a paired test.
        njit = hosts_for(3, hosts=FIRST_RUN_HOSTS)
        njit_edges = ring(ids=N9_ORDER)
        for node in njit:
            node["neighbors"] = njit_edges[node["id"]]
            node["publish_period_s"] = 0.1
        out["n9-jitter"] = {
            "name": "n9-jitter",
            "description": (
                "n9-50hz with an advertising interval RANGE of 100-120 ms "
                "instead of a fixed 100 ms, on both the Pi and the nRF. Paired "
                "with n9-50hz: identical in every other field. Tests whether the "
                "run-start link stalls are advertising-event collisions between "
                "two radios locked to the same nominal interval."
            ),
            "seed": 20260818,
            "controller": CONTROLLER_50HZ,
            "radio": {**radio_for(0.1), "adv_interval_max_ms": 120.0},
            "nodes": njit,
        }

        # ── n9-k2 / n9-k2-dupfilter: does duplicate filtering eat the retry? ──
        # 10 Hz advertising against a 5 Hz publish gives k = 2: every value goes
        # out twice, with identical bytes and identical address. That is a
        # duplicate by any definition, so a filtering receiver may drop exactly
        # the retry that redundancy depends on.
        #
        # The nRF scanner filters in firmware and cannot be changed without a
        # reflash; the Pi scanner is configurable, so the Pi is the side that
        # gets A/B'd. The pair below differ ONLY in radio.filter_duplicates.
        #
        # Prediction: if filtering suppresses retries, nRF->Pi delivery under the
        # filtered variant falls back toward its k=1 value (0.684 measured at 6
        # agents). If it does not move, the shortfall seen in PLATFORM.md 6.6 is
        # correlated loss and this asymmetry is harmless.
        for tag, dup in (("n9-k2", False), ("n9-k2-dupfilter", True)):
            nk = hosts_for(3, hosts=FIRST_RUN_HOSTS)
            nk_edges = ring(ids=N9_ORDER)
            for node in nk:
                node["neighbors"] = nk_edges[node["id"]]
                node["publish_period_s"] = 0.2      # 5 Hz against 10 Hz adv -> k=2
            out[tag] = {
                "name": tag,
                "description": (
                    f"9 agents, 50 Hz dynamics, 5 Hz publish, advertising at the "
                    f"100 ms floor: k = 2, so every value is transmitted twice. "
                    f"Pi-side duplicate filtering {'ON' if dup else 'OFF'}. Paired "
                    f"with {'n9-k2' if dup else 'n9-k2-dupfilter'}; the two differ "
                    f"in that field alone, so the difference between them is the "
                    f"cost of filtering a retry."
                ),
                "seed": 20260818,
                "controller": CONTROLLER_50HZ,
                "radio": {**radio_for(0.1), "filter_duplicates": dup},
                "nodes": nk,
            }


    # ── publish-rate sweep: four points, ceiling pinned at 1.0 ──────────────
    # The point of the sweep is airtime, so the advertising interval MUST track
    # the publish period; held at the 100 ms default it caps delivery at
    # min(1, T_pub/0.1) and the sweep measures that instead. The first attempt
    # did exactly this -- docs/PLATFORM.md A3.4.
    #
    # 6 agents, n6-fast's forced ring, identical seed across all four so the
    # initial conditions are shared and only the rate differs.
    for tag, pub in (("p400", 0.400), ("p200", 0.200),
                     ("p080", 0.080), ("p040", 0.040)):
        sw = hosts_for(2, hosts=n6_pair())
        sw_edges = ring(ids=[1, 2, 21, 12, 11, 22])
        for node in sw:
            node["neighbors"] = sw_edges[node["id"]]
            node["publish_period_s"] = pub
        # Advertisers only. `wifi` agents have no radio, so counting all six
        # overstated BLE duty by 1.5x in every generated description.
        n_adv = sum(1 for nd in sw if nd["type"] in ("ble", "bridge"))
        adv_hz = 1000.0 / max(ADV_FLOOR_MS, pub * 1000.0)
        duty = n_adv * adv_hz * 864e-6 * 100.0
        ceil = ceiling_for(pub)
        cap = ("delivery ceiling 1.0" if ceil >= 1.0 else
               f"delivery CAPPED at {ceil:.2f} -- the radio will not advertise "
               f"faster than {ADV_FLOOR_MS:g} ms, so this point measures the "
               f"ceiling as well as the medium and must be normalised by it")
        out[f"sweep-{tag}"] = {
            "name": f"sweep-{tag}",
            "description": (
                f"Publish-rate sweep at {1/pub:g} Hz ({pub:g} s). Advertising "
                f"interval matched to the publish period where the controller "
                f"allows it: {cap}. ~{duty:.2f}% BLE duty over {n_adv} advertisers "
                f"({adv_hz:g} Hz each). Same "
                f"seed and topology as the other three points."
            ),
            "seed": 20260818,
            "controller": CONTROLLER_FAST,
            # Explicit, not the default: this sweep varies airtime by varying
            # the advertising interval, which costs k=1 redundancy at the two
            # slow points. That is the trade the sweep was for. New manifests
            # should take radio_for's default (the floor) instead.
            "radio": radio_for(pub, adv_interval_ms=max(ADV_FLOOR_MS, pub * 1000)),
            "nodes": sw,
        }


    # ── n18-50hz: six hosts, 18 agents, at n6/n9-50hz's rates ───────────────
    # Six Pis, three agents each: ble 1..6, wifi 11..16, bridge 21..26.
    #
    # An 18-cycle, and the order is constrained rather than chosen. Every edge
    # must cross hosts (an intra-host link never reaches the radio) and must not
    # join `ble` to `wifi` (no shared medium). Within a band, consecutive ids are
    # already on consecutive hosts, so each band contributes a legal chain; the
    # bridges are what splice the three chains into one cycle:
    #
    #   1-2-3-4-5-6  21  12-13-14-15-16-11  22-23-24-25-26  -> back to 1
    #
    # Degree 2 everywhere, well inside the firmware's 4-neighbour limit, and
    # lambda_2 = 0.1206. All five link classes appear, which is what makes the
    # per-class delivery table complete: ble-ble x5, wifi-wifi x5, bridge-bridge
    # x4, ble-bridge x2 (6-21, 26-1) and bridge-wifi x2 (21-12, 11-22).
    if len(HOSTS) >= 6:
        N18_ORDER = [1, 2, 3, 4, 5, 6, 21, 12, 13, 14, 15, 16, 11,
                     22, 23, 24, 25, 26]
        n1850 = hosts_for(6, hosts=HOSTS[:6])
        n1850_edges = ring(ids=N18_ORDER)
        for node in n1850:
            node["neighbors"] = n1850_edges[node["id"]]
            node["publish_period_s"] = 0.1
        out["n18-50hz"] = {
            "name": "n18-50hz",
            "description": (
                "18 agents on 6 hosts at n6/n9-50hz's rates: 50 Hz dynamics, "
                "10 Hz publish, advertising at the 100 ms floor so the delivery "
                "ceiling is 1.0. Same controller and seed as n9-50hz, so the "
                "three sizes form a scaling series on per-link delivery. 18-cycle, "
                "degree 2, lambda_2 = 0.1206. Twelve advertisers at 10 Hz put "
                "~10.4% BLE duty on the band, twice n9-50hz's ~5.2%, so a "
                "delivery drop against n9-50hz at the same rates is an airtime "
                "effect rather than a rate effect."
            ),
            "seed": 20260818,
            "controller": CONTROLLER_50HZ,
            # Advertising RANGE, not a fixed interval, and a 56.25% scan duty.
            #
            # The range is the fix for the run-start collapses. The nRF's
            # controller applies the spec's random advDelay to every advertising
            # event, so two nRFs never hold a relative phase; the CYW43455
            # appears not to, so a bridge presents a steady phase and can sit
            # inside an nRF's own advertising window -- which blanks its
            # receiver -- for as long as the two crystals take to drift apart.
            # Measured in n18-50hz-0 without the range: 26->1 delivered 7 of
            # ~1200 packets with 41 s gaps at -53 dBm, while 1->26 over the same
            # pair was steady, and 21->6 ran perfectly for 96 s and then stopped.
            # A 100-120 ms range dithers the phase so no pair can lock.
            #
            # Scanning drops from 100% to 56.25% duty (11.25 of 20 ms). A shorter
            # interval revisits the channel five times per advertising interval
            # instead of once, so a single blanked window no longer costs the
            # whole 100 ms; and on the Pi the receive front-end request falls
            # from continuous to 56%, which is the exposure that loses to WLAN.
            "radio": radio_for(0.1, adv_interval_max_ms=120.0,
                               scan_interval_ms=20.0, scan_window_ms=11.25),
            "nodes": n1850,
        }

        # ── n18-50hz-scan100: the same run at 100% scan duty ────────────────
        # Paired with n18-50hz and differing ONLY in scan_window_ms, so the
        # difference between the two is the receive duty cycle alone. Both keep
        # the 100-120 ms advertising dither and the 20 ms scan interval, so
        # neither phase locking nor channel-rotation rate is a variable here.
        #
        # Two things it should separate, which n18-50hz cannot on its own
        # because the dither and the duty changed together:
        #
        #   delivery -- 56.25% duty cannot receive more than 56% of events, and
        #     n18-50hz measured 0.457/0.465 nRF->Pi against a predicted
        #     0.909 x 0.5625 = 0.511. At 100% duty the prediction is 0.909, set
        #     by the dither's mean interval alone. If the measurement lands
        #     there, per-link delivery is duty-limited and nothing else.
        #   coexistence -- on the Pi, scan duty IS the front-end exposure that
        #     loses arbitration to WLAN (PLATFORM.md 6.4). 100% duty restores
        #     the ~83x transmit/receive asymmetry that 56.25% reduces to ~56x,
        #     so this arm is also the one to run under wlan_load.sh.
        #
        # The nRF has nothing to arbitrate against, so it pays only its own ~1%
        # advertising blanking either way. The cost of raising duty is entirely
        # on the bridge, which is why the two arms exist rather than one choice.
        n1850s = hosts_for(6, hosts=HOSTS[:6])
        for node in n1850s:
            node["neighbors"] = n1850_edges[node["id"]]
            node["publish_period_s"] = 0.1
        out["n18-50hz-scan100"] = {
            "name": "n18-50hz-scan100",
            "description": (
                "n18-50hz at 100% scan duty: 11.25 ms window -> 20 ms window, "
                "everything else identical. Paired with n18-50hz, so the "
                "difference between them is the receive duty cycle alone -- on "
                "delivery, where 56.25% duty caps reception, and on coexistence, "
                "where continuous scanning is the front-end request that loses to "
                "WLAN on the bridge. Same 100-120 ms advertising dither, so no "
                "pair can phase lock in either arm."
            ),
            "seed": 20260818,
            "controller": CONTROLLER_50HZ,
            "radio": radio_for(0.1, adv_interval_max_ms=120.0,
                               scan_interval_ms=20.0, scan_window_ms=20.0),
            "nodes": n1850s,
        }
    else:
        print(f"skip     n18-50hz                 needs 6 hosts, "
              f"{len(HOSTS)} declared")

    if len(HOSTS) < 10:
        print(f"skip     n30-*                    need 10 hosts, "
              f"{len(HOSTS)} declared")
        return out          # last block in the function, so this one is safe

    clustered = hosts_for()
    for n in clustered:
        n["neighbors"] = CLUSTER_EDGES[n["id"]]
        if n["id"] in CLUSTER_DISABLED:
            n["enabled"] = False
    # G3 is time-varying, which is the whole point of it: the three clusters
    # converge separately while 21 and 30 are down, then the bridges come up and
    # the components merge. Statically it cannot reach agreement and should not --
    # there is no path between clusters. The merge transient is the measurement.
    CLUSTER_MERGE_AT_S = 60.0
    out["n30-clusters"] = {
        "name": "n30-clusters",
        "description": (
            "30 agents in clustered groups per transport, joined through bridge "
            "agents. Agents 21 and 30 are the BLE/Wi-Fi cut-points and start "
            "disabled, so the clusters coordinate only through the remaining "
            "bridges. Edges are declared because this graph is not regular."
        ),
        "seed": 20260818,
        "controller": CONTROLLER,
        "nodes": clustered,
        "events": [{"at_s": CLUSTER_MERGE_AT_S,
                    "nodes": sorted(CLUSTER_DISABLED),
                    "set": {"enabled": True}}],
    }

    # Regular topologies over BAND_ORDER rather than 1..30.
    #
    # The generators walk their id list in order, so the *order* decides who
    # neighbours whom. Walking 1..30 numerically puts a `ble` agent next to a
    # `wifi` agent at the 10/11 boundary, and those two share no medium -- the
    # link cannot carry a packet, so the graph the law runs on is not the one
    # declared. That was true of all three of these manifests.
    #
    # BAND_ORDER is a relabelling, not a redesign: for a ring it produces the same
    # cycle graph, so lambda_2, degree and every other graph property are
    # unchanged. What changes is which transport sits at which position, which is
    # the experimental variable and now realisable.
    for name, gen, params, desc in [
        ("n30-ring-directed", "ring", {"directed": True, "ids": BAND_ORDER},
         "30-agent directed cycle: each agent reads only its predecessor. The "
         "sparsest strongly connected graph, so the slowest convergence. Ordered "
         "so bridges sit at each ble/wifi boundary -- those two share no medium."),
        ("n30-ring4", "ring", {"k": 2, "ids": BAND_ORDER},
         "30-agent undirected ring, degree 4. Each BLE agent sits exactly at the "
         "firmware's neighbour limit, so this is the densest ring the "
         "microcontroller can serve. Five bridges per boundary, so degree 4 still "
         "clears the ble/wifi gap."),
        ("n30-line-directed", "line", {"directed": True, "ids": BAND_ORDER},
         "30-agent open directed chain. Deliberately NOT strongly connected -- "
         "the first agent has no inputs -- as a control case for the connectivity "
         "check."),
    ]:
        out[name] = {
            "name": name, "description": desc, "seed": 20260818,
            "controller": CONTROLLER,
            "structure": {"generator": gen, "params": params},
            "nodes": hosts_for(),
        }

    # ── the three paper topologies at the 50 Hz / 10 Hz rates ───────────────
    # G1, G2 and G3 as run at 6, 9 and 18 agents, now at the full 30. Same
    # controller, radio block and seed as n18-50hz, so the four sizes form one
    # series and topology is the only variable within this group of three.
    #
    # The advertising RANGE matters more here than anywhere else: 20 advertisers is
    # the largest population the platform has run, and a fixed 100 ms interval lets
    # any pair phase-lock against a scanner's own advertising window (measured at
    # 18 agents: one link delivered 7 of ~1200 packets for a whole run). 20
    # advertisers at 10 Hz is ~17.3% BLE duty, so these are also the
    # airtime-heaviest manifests in the set.
    #
    # Scanning at 90% duty (18 of 20 ms), not n18-50hz's 56.25% and not 100%.
    # The 18-agent pair measured what duty buys: nRF receivers convert it almost
    # one-for-one (0.34-0.41 at 56.25% -> 0.67-0.82 at 100%), so at lambda_2 =
    # 0.0219 for the directed cycle the delivery is worth having. The 10% left
    # unscanned is deliberate rather than a rounding: a node's own advertising
    # event needs the radio for ~1.2 ms, and leaving a 2 ms gap in every 20 ms
    # window lets it slot in without preempting the scan -- which is the same
    # scan-versus-advertise contention the advertising range exists to break.
    #
    # Neighbour counts sit at or below the firmware's limit of four: in-degree 1
    # for the directed cycle, 4 for the degree-4 ring, and 4 at the two cut-points
    # of the clustered graph.
    RADIO_50HZ_N30 = radio_for(0.1, adv_interval_max_ms=120.0,
                               scan_interval_ms=20.0, scan_window_ms=18.0)

    for name, gen, params, desc in [
        ("n30-dring-50hz", "ring", {"directed": True, "ids": BAND_ORDER},
         "G1 at 30 agents, 50 Hz dynamics, 10 Hz publish: directed cycle, each "
         "agent reads only its predecessor. In-degree 1, the sparsest strongly "
         "connected graph and therefore the slowest convergence of the three. "
         "Ordered over BAND_ORDER so a bridge sits at each ble/wifi boundary."),
        ("n30-ring4-50hz", "ring", {"k": 2, "ids": BAND_ORDER},
         "G2 at 30 agents, 50 Hz dynamics, 10 Hz publish: undirected ring of "
         "degree 4, the densest ring the microcontroller can serve. Paired with "
         "n30-dring-50hz on everything but the edge set, so the difference "
         "between them is connectivity alone -- transmitted and received airtime "
         "are both O(1) in the degree on a broadcast medium, and neither moves."),
    ]:
        out[name] = {
            "name": name, "description": desc, "seed": 20260818,
            "controller": CONTROLLER_50HZ,
            "radio": RADIO_50HZ_N30,
            "structure": {"generator": gen, "params": params},
            "nodes": hosts_for(publish_period_s=0.1),
        }

    # G3 is declared edge by edge rather than generated: it is irregular by
    # design, and 21 and 30 are the cut-points that join the BLE and Wi-Fi
    # subnets. They start disabled, so the three clusters coordinate separately,
    # and the scheduled event brings them up mid-run. Statically the graph has no
    # path between clusters and cannot reach agreement; the merge transient is
    # the measurement.
    clustered50 = hosts_for(publish_period_s=0.1)
    for n in clustered50:
        n["neighbors"] = CLUSTER_EDGES[n["id"]]
        if n["id"] in CLUSTER_DISABLED:
            n["enabled"] = False
    out["n30-clusters-50hz"] = {
        "name": "n30-clusters-50hz",
        "description": (
            "G3(t) at 30 agents, 50 Hz dynamics, 10 Hz publish: three clusters "
            "joined only through bridges 21 and 30, which start disabled and are "
            "enabled at t = 60 s. Time-varying by construction, so the merge "
            "transient rather than the steady state is the quantity of interest. "
            "Degree 2 to 4; the two cut-points carry four neighbours each."
        ),
        "seed": 20260818,
        "controller": CONTROLLER_50HZ,
        "radio": RADIO_50HZ_N30,
        "nodes": clustered50,
        "events": [{"at_s": CLUSTER_MERGE_AT_S,
                    "nodes": sorted(CLUSTER_DISABLED),
                    "set": {"enabled": True}}],
    }
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Regenerate the experiment manifests.")
    ap.add_argument(
        "--hosts", default=os.environ.get("VERTEX_HOSTS"),
        help="comma-separated host addresses, replacing the built-in HOSTS. The "
             "one part of a manifest that is not derivable, and the part that "
             "changes when a Pi is re-imaged or swapped -- so it belongs here "
             "rather than in a hand-edit of a generated file, which the next run "
             "of this script would silently revert. Also VERTEX_HOSTS.")
    args = ap.parse_args(argv)
    if args.hosts:
        hosts = [h.strip() for h in args.hosts.split(",") if h.strip()]
        if not hosts:
            print("--hosts was empty", file=sys.stderr)
            return 2
        HOSTS[:] = hosts
        FIRST_RUN_HOSTS[:] = hosts[:3]
        print(f"hosts: {', '.join(HOSTS)}")

    OUT.mkdir(exist_ok=True)
    failures = 0
    for name, doc in manifests().items():
        rep = check(load_manifest(doc))
        status = "ok" if rep.ok else "INVALID"
        if not rep.ok:
            failures += 1
        path = OUT / f"{name}.yaml"
        with path.open("w", encoding="utf-8") as fh:
            fh.write(f"# Generated by tools/make_manifests.py -- edit that, not this.\n")
            yaml.safe_dump(doc, fh, sort_keys=False, width=100)
        print(f"{status:8} {path.name:24} nodes={rep.n_nodes} edges={rep.n_edges} "
              f"lambda2={rep.algebraic_connectivity if rep.algebraic_connectivity is None else round(rep.algebraic_connectivity,4)} "
              f"warnings={len(rep.warnings)}")
        for e in rep.errors:
            print(f"         ERROR {e}")
    return failures


if __name__ == "__main__":
    raise SystemExit(main())
