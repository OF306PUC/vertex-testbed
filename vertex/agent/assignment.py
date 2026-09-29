"""What the hub assigns to one agent.

The mechanism by which manifest parameters reach a running agent, and the reason an
agent needs no configuration file of its own.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..controllers.base import ControllerParams, DisturbanceParams
from ..net import AgentType
from ..topology import ExperimentManifest, controller_params_for
from ..topology.loader import node_seed

__all__ = ["AgentAssignment", "assignment_for", "assignments_for"]

#: Which dataclass rebuilds each block. Keyed by the name the assignment uses.
_BLOCKS = ("plant", "virtual", "interface")


def _microgrid_params(m: dict[str, Any]) -> dict[str, Any]:
    """Rebuild the controller's parameter blocks from the carried dicts."""
    from ..controllers.interface_adaptive import AdaptiveParams
    from ..controllers.interface_lc import LCParams
    from ..controllers.plant import PlantParams
    from ..controllers.virtual import VirtualParams

    iface_kind = m.get("interface_kind", "adaptive")
    build = {"plant": PlantParams, "virtual": VirtualParams,
             "interface": AdaptiveParams if iface_kind == "adaptive" else LCParams}
    out = {k: v for k, v in m.items()
           if k not in _BLOCKS and k != "interface_kind"}
    for name in _BLOCKS:
        out[name] = build[name](**m[name])
    return out


class AgentAssignment(BaseModel):
    """One agent's complete configuration for one run.

    Values are in engineering units, matching the controller. Quantisation to the
    wire happens in the codec, not here.
    """

    model_config = ConfigDict(extra="forbid")

    # identity
    node_id: int = Field(ge=1, le=255)
    node_type: AgentType
    enabled: bool = True
    neighbors: list[int] = Field(default_factory=list)

    # timing
    dt_s: float = Field(gt=0)
    publish_period_s: float = Field(gt=0)
    max_neighbor_age_s: float | None = None

    # control law
    controller: str = "finite_time_adaptive"
    state: float = 0.0
    vstate: float = 0.0
    vartheta: float = 0.0
    eta: float = 5e-5
    gain_ij: float = 0.1
    alpha: float = 0.5              # sign-power exponent
    delta: float = 0.01

    # disturbance
    disturbance: dict[str, Any] = Field(default_factory=dict)
    disturbance_seed: int = 0

    #: The microgrid family's three parameter blocks, plus the per-agent
    #: scalars, as plain dicts so the whole assignment stays JSON. Empty for
    #: the scalar law. The dataclasses in `vertex/controllers/` remain the one
    #: definition of what the fields are; this only carries them.
    #:
    #: A node that is not pinned has `virtual.b = 0` and no reference, because
    #: the loader never puts one in. The value therefore does not cross the
    #: control plane to an agent that must not know it.
    microgrid: dict[str, Any] = Field(default_factory=dict)

    # radio -- milliseconds; the 0.625 ms conversion happens at the transport.
    radio: dict[str, Any] = Field(default_factory=dict)

    # provenance, carried so a log can be interpreted without the manifest
    manifest_name: str = ""
    seed: int = 0
    run_index: int = 0

    def to_controller_params(self) -> ControllerParams:
        if self.microgrid:
            return ControllerParams(dt_s=self.dt_s,
                                    **_microgrid_params(self.microgrid))
        d = self.disturbance
        return ControllerParams(
            dt_s=self.dt_s, state=self.state, vstate=self.vstate,
            vartheta=self.vartheta, eta=self.eta, gain_ij=self.gain_ij,
            alpha=self.alpha,
            delta=self.delta,
            disturbance=DisturbanceParams(
                enabled=bool(d.get("enabled", False)),
                noise_amplitude=float(d.get("noise_amplitude", 0.0)),
                noise_offset=float(d.get("noise_offset", 0.5)),
                beta=float(d.get("beta", 0.0)),
                sine_amplitude=float(d.get("sine_amplitude", 0.0)),
                sine_frequency_hz=float(d.get("sine_frequency_hz", 0.0)),
                sine_phase_s=float(d.get("sine_phase_s", 0.0)),
                period_samples=int(d.get("period_samples", 1000)),
            ),
        )

    def radio_environment(self) -> dict[str, Any]:
        """The radio block as it goes into ``RunMeta.environment``.
        """
        from ..radio.hci import ms_to_units

        r = dict(self.radio)
        if not r:
            return {}
        out = dict(r)
        for key in ("adv_interval_ms", "scan_interval_ms", "scan_window_ms"):
            if key in r:
                out[key.replace("_ms", "_units")] = ms_to_units(float(r[key]))
        si, sw = r.get("scan_interval_ms"), r.get("scan_window_ms")
        if si:
            out["scan_duty_cycle"] = float(sw) / float(si)
        # Where the parameters were actually applied. `wifi` records them without
        # applying them, and a reader must be able to tell the two apart.
        out["applied_on"] = {
            "ble": "nrf52", "bridge": "pi-hci", "wifi": "none",
        }.get(str(self.node_type), "unknown")
        return out


def assignment_for(
    manifest: ExperimentManifest, node_id: int, run_index: int = 0
) -> AgentAssignment:
    """Build one node's assignment from the manifest.
    """
    node = manifest.by_id[node_id]
    params = controller_params_for(manifest, node, run_index)
    d = params.disturbance
    return AgentAssignment(
        node_id=node.id, node_type=node.type, enabled=node.enabled,
        neighbors=list(node.neighbors),
        dt_s=params.dt_s, publish_period_s=node.publish_period_s,
        controller=manifest.controller.name,
        state=params.state, vstate=params.vstate, vartheta=params.vartheta,
        eta=params.eta, gain_ij=params.gain_ij, alpha=params.alpha,
        delta=params.delta,
        disturbance={
            "enabled": d.enabled, "noise_amplitude": d.noise_amplitude,
            "noise_offset": d.noise_offset, "beta": d.beta,
            "sine_amplitude": d.sine_amplitude,
            "sine_frequency_hz": d.sine_frequency_hz,
            "sine_phase_s": d.sine_phase_s, "period_samples": d.period_samples,
        },
        disturbance_seed=node_seed(manifest.seed, run_index, node.id, "disturbance"),
        microgrid=_microgrid_dicts(manifest, node),
        radio=manifest.radio.model_dump(),
        manifest_name=manifest.name, seed=manifest.seed, run_index=run_index,
    )


def _microgrid_dicts(manifest: ExperimentManifest, node) -> dict[str, Any]:
    """The microgrid blocks as JSON, or empty for the scalar law."""
    if manifest.controller.microgrid is None:
        return {}
    from dataclasses import asdict
    from ..topology.loader import microgrid_blocks

    blocks = microgrid_blocks(manifest, node)
    out: dict[str, Any] = {"interface_kind": manifest.controller.microgrid.interface}
    for k, v in blocks.items():
        out[k] = asdict(v) if k in _BLOCKS else v
    return out


def assignments_for(
    manifest: ExperimentManifest, run_index: int = 0
) -> dict[int, AgentAssignment]:
    """Every node's assignment for one run."""
    return {n.id: assignment_for(manifest, n.id, run_index) for n in manifest.nodes}
