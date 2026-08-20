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
    eta: float = 2e-6
    alpha: float = 0.02
    delta: float = 0.01

    # disturbance
    disturbance: dict[str, Any] = Field(default_factory=dict)
    disturbance_seed: int = 0

    # radio -- milliseconds; the 0.625 ms conversion happens at the transport.
    radio: dict[str, Any] = Field(default_factory=dict)

    # provenance, carried so a log can be interpreted without the manifest
    manifest_name: str = ""
    seed: int = 0
    run_index: int = 0

    def to_controller_params(self) -> ControllerParams:
        d = self.disturbance
        return ControllerParams(
            dt_s=self.dt_s, state=self.state, vstate=self.vstate,
            vartheta=self.vartheta, eta=self.eta, alpha=self.alpha,
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
        eta=params.eta, alpha=params.alpha, delta=params.delta,
        disturbance={
            "enabled": d.enabled, "noise_amplitude": d.noise_amplitude,
            "noise_offset": d.noise_offset, "beta": d.beta,
            "sine_amplitude": d.sine_amplitude,
            "sine_frequency_hz": d.sine_frequency_hz,
            "sine_phase_s": d.sine_phase_s, "period_samples": d.period_samples,
        },
        disturbance_seed=node_seed(manifest.seed, run_index, node.id, "disturbance"),
        radio=manifest.radio.model_dump(),
        manifest_name=manifest.name, seed=manifest.seed, run_index=run_index,
    )


def assignments_for(
    manifest: ExperimentManifest, run_index: int = 0
) -> dict[int, AgentAssignment]:
    """Every node's assignment for one run."""
    return {n.id: assignment_for(manifest, n.id, run_index) for n in manifest.nodes}
