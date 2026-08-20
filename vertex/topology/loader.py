"""Load manifests and derive reproducible initial conditions.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import numpy as np
import yaml

from ..controllers.base import ControllerParams, DisturbanceParams
from ..net import AgentType
from .generators import generate
from .models import ExperimentManifest, NodeSpec

__all__ = ["IC_RANGES", "load_manifest", "load_manifest_file", "node_seed",
           "initial_conditions", "controller_params_for", "resolve_run",
           "NodeRuntime"]

#: Initial-condition band per agent type, in engineering units.
IC_RANGES: dict[AgentType, tuple[float, float]] = {
    AgentType.BLE: (0.0, 10.0),
    AgentType.WIFI: (10.0, 20.0),
    AgentType.BRIDGE: (20.0, 30.0),
}

#: Range for a derived disturbance phase, in seconds.
PHASE_RANGE_S = (0.0, 1.0)


def node_seed(seed: int, run_index: int, node_id: int, purpose: str = "ic") -> int:
    """Deterministic substream seed for one node and one purpose.
    """
    tag = sum((i + 1) * b for i, b in enumerate(purpose.encode()))
    ss = np.random.SeedSequence([seed, run_index, node_id, tag])
    return int(ss.generate_state(1, dtype=np.uint32)[0])


def initial_conditions(node: NodeSpec, seed: int, run_index: int = 0) -> dict[str, float]:
    """Initial conditions for one node, in engineering units."""
    rng = np.random.default_rng(node_seed(seed, run_index, node.id, "ic"))
    lo, hi = IC_RANGES[node.type]
    return {
        "state": float(rng.uniform(lo, hi)),
        "vstate": float(rng.uniform(lo, hi)),
        "vartheta": 0.0,          # the adaptive gain always starts from zero
        "sine_phase_s": float(rng.uniform(*PHASE_RANGE_S)),
    }


class NodeRuntime:
    """A node resolved for one run: spec, parameters, seeds, initial conditions."""

    __slots__ = ("spec", "params", "disturbance_seed", "ics", "run_index")

    def __init__(self, spec: NodeSpec, params: ControllerParams,
                 disturbance_seed: int, ics: dict[str, float], run_index: int) -> None:
        self.spec = spec
        self.params = params
        self.disturbance_seed = disturbance_seed
        self.ics = ics
        self.run_index = run_index

    @property
    def id(self) -> int:
        return self.spec.id

    def __repr__(self) -> str:      # pragma: no cover
        return (f"NodeRuntime(id={self.id}, type={self.spec.type}, "
                f"x0={self.ics['state']:.6f}, z0={self.ics['vstate']:.6f})")


def controller_params_for(
    manifest: ExperimentManifest, node: NodeSpec, run_index: int = 0
) -> ControllerParams:
    """Merge the manifest's controller settings with this node's initial conditions."""
    ics = initial_conditions(node, manifest.seed, run_index)
    c = manifest.controller
    d = c.disturbance
    return ControllerParams(
        dt_s=c.dt_s,
        state=ics["state"], vstate=ics["vstate"], vartheta=ics["vartheta"],
        eta=c.eta, alpha=c.alpha, delta=c.delta,
        disturbance=DisturbanceParams(
            enabled=d.enabled,
            noise_amplitude=d.noise_amplitude,
            noise_offset=d.noise_offset,
            beta=d.beta,
            sine_amplitude=d.sine_amplitude,
            sine_frequency_hz=d.sine_frequency_hz,
            # An explicit phase pins every node to the same value; otherwise each
            # node draws a distinct reproducible phase, so the fleet is not
            # disturbed in lockstep.
            sine_phase_s=(d.sine_phase_s if d.sine_phase_s is not None
                          else ics["sine_phase_s"]),
            period_samples=d.period_samples,
        ),
    )


def resolve_run(manifest: ExperimentManifest, run_index: int = 0) -> dict[int, NodeRuntime]:
    """Everything each node needs for one run of a multi-run experiment."""
    return {
        n.id: NodeRuntime(
            spec=n,
            params=controller_params_for(manifest, n, run_index),
            disturbance_seed=node_seed(manifest.seed, run_index, n.id, "disturbance"),
            ics=initial_conditions(n, manifest.seed, run_index),
            run_index=run_index,
        )
        for n in manifest.nodes
    }


def load_manifest(raw: dict[str, Any]) -> ExperimentManifest:
    """Validate a manifest mapping, expanding ``structure`` into node neighbours.
    """
    data = copy.deepcopy(raw)
    structure = data.get("structure")
    if structure:
        declared = [n for n in data.get("nodes", []) if n.get("neighbors")]
        if declared:
            ids = sorted(n.get("id") for n in declared)
            raise ValueError(
                f"manifest sets both `structure` ({structure.get('generator')!r}) and "
                f"explicit `neighbors` on nodes {ids}; pick one -- either generate "
                "the graph or declare it"
            )
        edges = generate(structure["generator"], structure.get("params"))
        ids = {n["id"] for n in data["nodes"]}
        unknown = sorted(set(edges) - ids)
        if unknown:
            raise ValueError(
                f"generator {structure['generator']!r} produced ids {unknown} that "
                f"the manifest does not declare; declared: {sorted(ids)}"
            )
        for n in data["nodes"]:
            n["neighbors"] = edges.get(n["id"], [])
    return ExperimentManifest.model_validate(data)


def load_manifest_file(path: str | Path) -> ExperimentManifest:
    """Load and validate a YAML manifest."""
    p = Path(path)
    with p.open(encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    if not isinstance(raw, dict):
        raise ValueError(f"{p}: expected a YAML mapping, got {type(raw).__name__}")
    raw.setdefault("name", p.stem)
    return load_manifest(raw)
