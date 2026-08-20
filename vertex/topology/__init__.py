"""Experiment manifests: declaration, generation, and validation (~154 ms).
"""
from .generators import REGISTRY as GENERATORS
from .generators import clusters, complete, generate, line, ring, star
from .loader import (IC_RANGES, NodeRuntime, controller_params_for,
                     initial_conditions, load_manifest, load_manifest_file,
                     node_seed, resolve_run)
from .models import (MAX_NODE_ID, AgentType, ControllerSpec, DisturbanceSpec,
                     ExperimentManifest, NodeSpec, StructureSpec)
from .validate import GraphReport, build_graph, check

__all__ = [
    # manifests
    "ExperimentManifest", "NodeSpec", "ControllerSpec", "DisturbanceSpec",
    "StructureSpec", "AgentType", "MAX_NODE_ID",
    # structure
    "ring", "line", "clusters", "complete", "star", "generate", "GENERATORS",
    # loading and initial conditions
    "load_manifest", "load_manifest_file", "resolve_run", "NodeRuntime",
    "initial_conditions", "controller_params_for", "node_seed", "IC_RANGES",
    # validation
    "check", "build_graph", "GraphReport",
]
