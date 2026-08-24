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
HOSTS = ["10.6.5.1", "10.6.5.2", "10.6.5.3", "10.6.5.4", 
         "10.6.5.5", "10.6.5.6", "10.6.5.7", "10.6.5.8",
         "10.6.5.9", "10.6.5.10"]

#: Ids 1-10 are BLE agents, 11-20 Wi-Fi, 21-30 bridges; agent k of each band
#: lives on HOSTS[k]. Bridges carry both radios, which is why they are the ones
#: that join the BLE and Wi-Fi subnets.
BANDS = [("ble", 1), ("wifi", 11), ("bridge", 21)]

CONTROLLER = {
    "name": "finite_time_adaptive",
    "dt_s": 0.2,
    "eta": 2e-6,
    "alpha": 0.02,
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
    "eta": 2e-6,
    "alpha": 0.02,
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


def radio_for(publish_period_s: float, *, scan_ratio: float = 1.0) -> dict:
    """RadioSpec matched to a publish rate, clamped at the controller floor.

    Above 10 Hz the ceiling CANNOT be held at 1.0 -- the radio will not advertise
    that fast. Clamping rather than raising is deliberate, and the reason is
    symmetry: the same value goes to the nRF and to the Pi, so both agent classes
    sit on the SAME ceiling. Letting the nRF run at its own floor while the Pi
    is pinned at 100 ms would give the two classes different ceilings, which is
    precisely the defect the reviewer identified in the JS platform.

    The caller is expected to report the resulting ceiling, which
    `ceiling_for()` returns.
    """
    adv_ms = max(ADV_FLOOR_MS, publish_period_s * 1000.0)
    if adv_ms > 10240.0:
        raise ValueError(f"publish_period_s={publish_period_s} needs "
                         f"adv_interval_ms={adv_ms}, above the 10240 ms maximum")
    return {
        "adv_interval_ms": adv_ms,
        "scan_interval_ms": adv_ms,
        "scan_window_ms": round(adv_ms * scan_ratio, 4),
    }


def ceiling_for(publish_period_s: float) -> float:
    """Highest delivery ratio attainable at this publish rate, given the floor."""
    return min(1.0, publish_period_s * 1000.0 / max(ADV_FLOOR_MS,
                                                    publish_period_s * 1000.0))


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
    n4 = [n for n in hosts_for(2, hosts=HOSTS[:2]) if n["type"] != "ble"]
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
    n6f = hosts_for(2, hosts=HOSTS[:2])
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
    n6 = hosts_for(2, hosts=HOSTS[:2])
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

        clustered = hosts_for()
        for n in clustered:
            n["neighbors"] = CLUSTER_EDGES[n["id"]]
            if n["id"] in CLUSTER_DISABLED:
                n["enabled"] = False
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
        sw = hosts_for(2, hosts=HOSTS[:2])
        sw_edges = ring(ids=[1, 2, 21, 12, 11, 22])
        for node in sw:
            node["neighbors"] = sw_edges[node["id"]]
            node["publish_period_s"] = pub
        duty = 6.0 * (1.0 / pub) * 864e-6 * 100.0
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
                f"allows it: {cap}. ~{duty:.2f}% BLE duty over 6 agents. Same "
                f"seed and topology as the other three points."
            ),
            "seed": 20260818,
            "controller": CONTROLLER_FAST,
            "radio": radio_for(pub),
            "nodes": sw,
        }


    if len(HOSTS) < 10:
        print(f"skip     n30-*                    need 10 hosts, "
              f"{len(HOSTS)} declared")
        return out          # last block in the function, so this one is safe

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
