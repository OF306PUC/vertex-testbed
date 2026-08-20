"""Graph preconditions, checked before a run is triggered.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import networkx as nx
import numpy as np

from ..wire.codec import N_MAX_NEIGHBORS_FIRMWARE
from .models import AgentType, ExperimentManifest

__all__ = ["GraphReport", "build_graph", "check"]


def build_graph(manifest: ExperimentManifest, *, enabled_only: bool = False) -> nx.DiGraph:
    """Directed graph of information flow.

    Edge ``j -> i`` means *i reads j's state*, i.e. j influences i.
    """
    g = nx.DiGraph()
    keep = {n.id for n in manifest.nodes if n.enabled or not enabled_only}
    for n in manifest.nodes:
        if n.id in keep:
            g.add_node(n.id, type=str(n.type), enabled=n.enabled,
                       publish_period_s=n.publish_period_s, ip=n.ip)
    for n in manifest.nodes:
        if n.id not in keep:
            continue
        for j in n.neighbors:
            if j in keep:
                g.add_edge(j, n.id)
    return g


@dataclass
class GraphReport:
    """Findings. ``errors`` block a run; ``warnings`` are for the operator."""

    n_nodes: int
    n_edges: int
    is_weakly_connected: bool
    is_strongly_connected: bool
    is_balanced: bool
    is_symmetric: bool
    algebraic_connectivity: float | None
    in_degrees: dict[int, int]
    out_degrees: dict[int, int]
    isolated: list[int]
    sources: list[int] = field(default_factory=list)
    sinks: list[int] = field(default_factory=list)
    disabled: list[int] = field(default_factory=list)
    # Properties of the graph with disabled agents removed -- the one that runs.
    effective_strongly_connected: bool = True
    effective_algebraic_connectivity: float | None = None
    effective_components: list[list[int]] = field(default_factory=list)
    by_type: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def fragmented_by_disabling(self) -> bool:
        return len(self.effective_components) > 1

    def summary(self) -> str:
        lines = [
            f"nodes={self.n_nodes} edges={self.n_edges} "
            f"by_type={self.by_type}",
            f"weakly_connected={self.is_weakly_connected} "
            f"strongly_connected={self.is_strongly_connected} "
            f"balanced={self.is_balanced} symmetric={self.is_symmetric}",
        ]
        if self.algebraic_connectivity is not None:
            lines.append(f"algebraic_connectivity(lambda_2)={self.algebraic_connectivity:.6f}")
        else:
            lines.append("algebraic_connectivity(lambda_2)=n/a (not strongly connected)")
        if self.disabled:
            lines.append(
                f"disabled={self.disabled} -> effective: "
                f"strongly_connected={self.effective_strongly_connected}, "
                f"components={len(self.effective_components)}, "
                f"lambda_2={self.effective_algebraic_connectivity}")
        for e in self.errors:
            lines.append(f"  ERROR   {e}")
        for w in self.warnings:
            lines.append(f"  warning {w}")
        return "\n".join(lines)


def _algebraic_connectivity(g: nx.DiGraph) -> float | None:
    """Second-smallest Laplacian eigenvalue -- the convergence-rate proxy.
    """
    if g.number_of_nodes() < 2 or not nx.is_strongly_connected(g):
        return None
    order = sorted(g.nodes())
    a = nx.to_numpy_array(g, nodelist=order)
    lap = np.diag(a.sum(axis=1)) - a
    try:
        eig = np.linalg.eigvals(lap)
    except np.linalg.LinAlgError:      # pragma: no cover
        return None
    return float(np.sort_complex(eig)[1].real)


def check(manifest: ExperimentManifest, *, require_strong: bool | None = None) -> GraphReport:
    """Validate a manifest's graph.
    """
    g = build_graph(manifest)
    if require_strong is None:
        require_strong = False

    in_deg = dict(g.in_degree())
    out_deg = dict(g.out_degree())
    isolated = sorted(n for n in g.nodes if in_deg[n] == 0 and out_deg[n] == 0)
    sources = sorted(n for n in g.nodes if in_deg[n] == 0 and out_deg[n] > 0)
    sinks = sorted(n for n in g.nodes if out_deg[n] == 0 and in_deg[n] > 0)
    balanced = all(in_deg[n] == out_deg[n] for n in g.nodes)
    symmetric = all(g.has_edge(v, u) for u, v in g.edges)

    weak = nx.is_weakly_connected(g) if g.number_of_nodes() else False
    strong = nx.is_strongly_connected(g) if g.number_of_nodes() else False

    by_type: dict[str, int] = {}
    for t in AgentType:
        c = sum(1 for n in manifest.nodes if n.type == t)
        if c:
            by_type[str(t)] = c

    eff = build_graph(manifest, enabled_only=True)
    eff_components = [sorted(c) for c in nx.weakly_connected_components(eff)]
    eff_strong = nx.is_strongly_connected(eff) if eff.number_of_nodes() else False

    rep = GraphReport(
        n_nodes=g.number_of_nodes(), n_edges=g.number_of_edges(),
        is_weakly_connected=weak, is_strongly_connected=strong,
        is_balanced=balanced, is_symmetric=symmetric,
        algebraic_connectivity=_algebraic_connectivity(g),
        in_degrees=in_deg, out_degrees=out_deg, isolated=isolated,
        sources=sources, sinks=sinks,
        disabled=sorted(n.id for n in manifest.nodes if not n.enabled),
        effective_strongly_connected=eff_strong,
        effective_algebraic_connectivity=_algebraic_connectivity(eff),
        effective_components=eff_components,
        by_type=by_type,
    )

    if isolated:
        rep.errors.append(f"nodes {isolated} have no links at all")
    if not weak:
        comps = [sorted(c) for c in nx.weakly_connected_components(g)]
        rep.errors.append(
            f"graph is disconnected into {len(comps)} components {comps}; "
            "consensus cannot be reached across components"
        )

    if not strong:
        msg = ("graph is not strongly connected"
               + (f"; sources={rep.sources}" if rep.sources else "")
               + (f"; sinks={rep.sinks}" if rep.sinks else ""))
        if require_strong:
            rep.errors.append(
                msg + " -- information cannot reach every agent"
            )
        else:
            rep.warnings.append(msg)

    if not balanced:
        offenders = {n: (in_deg[n], out_deg[n]) for n in g.nodes
                     if in_deg[n] != out_deg[n]}
        # Not an error: the law still converges. But agreement lands on a
        # weighted combination of the initial conditions, not their mean.
        rep.warnings.append(
            f"graph is not weight-balanced (in!=out for {offenders}); the "
            "agreement value will be a weighted combination, not the mean"
        )

    if not manifest.controller.eta_well_below_alpha:
        rep.warnings.append(
            f"eta={manifest.controller.eta} is not much smaller than "
            f"alpha={manifest.controller.alpha}; the discrete form assumes "
            "eta << alpha, since both absorb the step size"
        )

    over = {n.id: len(n.neighbors) for n in manifest.nodes
            if n.type is AgentType.BLE and len(n.neighbors) > N_MAX_NEIGHBORS_FIRMWARE}
    if over:
        rep.errors.append(
            f"agents {over} run their control law on the microcontroller, whose "
            f"firmware holds at most {N_MAX_NEIGHBORS_FIRMWARE} neighbours; the "
            "surplus links would be silently ignored rather than reported"
        )

    for n in manifest.nodes:
        if not n.enabled and n.neighbors:
            rep.warnings.append(
                f"node {n.id} is disabled but is still read by its neighbours; "
                "its last state will be treated as frozen, not absent"
            )
            
    if rep.fragmented_by_disabling:
        rep.warnings.append(
            f"disabling {rep.disabled} splits the graph into "
            f"{len(rep.effective_components)} components "
            f"{rep.effective_components}; agreement is reachable within each, not "
            f"across them, and the declared lambda_2 "
            f"({rep.algebraic_connectivity}) does not describe this run")

    periods = {n.publish_period_s for n in manifest.nodes}
    if len(periods) > 1:
        rep.warnings.append(f"heterogeneous publish periods {sorted(periods)} s")
    for n in manifest.nodes:
        if n.publish_period_s < manifest.controller.dt_s:
            rep.warnings.append(
                f"node {n.id}: publish period {n.publish_period_s} s is shorter "
                f"than the control period dt={manifest.controller.dt_s} s -- "
                "transmitting faster than the state changes spends airtime for "
                "no new information, and airtime is what costs us packet loss"
            )
    return rep
