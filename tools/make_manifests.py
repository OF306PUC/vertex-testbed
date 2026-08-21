#!/usr/bin/env python3
"""Emit the experiment manifests. Run: ``python3 tools/make_manifests.py``

This replaces a hand-maintained topology table. Hosts are declared once; graph
structure comes from the generators; irregular graphs (the clustered one) declare
their edges explicitly. 
"""
from __future__ import annotations

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
HOSTS = ["192.168.0.190", "192.168.0.198", "192.168.0.179", "192.168.0.138",
         "192.168.0.124", "192.168.0.167", "192.168.0.180", "192.168.0.137",
         "192.168.0.127", "192.168.0.176"]

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
FIRST_RUN_HOSTS = HOSTS[:3]

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
    out: dict[str, dict] = {}

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
    return out


def main() -> int:
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
