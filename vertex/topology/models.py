"""Experiment manifests -- the declarative definition of a run.
"""

from __future__ import annotations

import ipaddress
from typing import Annotated, Any, Literal

from pydantic import (BaseModel, ConfigDict, Field, ValidationInfo, field_validator,
                      model_validator)

from ..net import AgentType

__all__ = ["AgentType", "NodeSpec", "DisturbanceSpec", "ControllerSpec",
           "StructureSpec", "ExperimentManifest", "MAX_NODE_ID"]

#: Upper bound on a node id: the wire format carries it in a uint8.
MAX_NODE_ID = 255


class DisturbanceSpec(BaseModel):
    """Additive disturbance, in engineering units.

    ``nu(t) = noise_amplitude*(U - noise_offset) + beta
              + sine_amplitude*sin(2*pi*sine_frequency_hz*(t - sine_phase_s))``
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    noise_amplitude: float = Field(default=0.0, ge=0)
    noise_offset: float = Field(
        default=0.5,
        description="Subtracted from the uniform draw, so 0.5 centres the noise "
                    "on zero. Not an offset added to the output.",
    )
    beta: float = 0.0
    sine_amplitude: float = Field(default=0.0, ge=0)
    sine_frequency_hz: float = Field(default=0.0, ge=0)
    sine_phase_s: float | None = Field(
        default=None,
        description="Phase offset in seconds. None means 'derive from the run "
                    "seed', which gives each node a distinct reproducible phase; "
                    "set a value only to pin every node to the same phase.",
    )
    period_samples: int = Field(
        default=1000, gt=0,
        description="Cycle length of the step counter that drives the sinusoid's "
                    "time argument, hence the disturbance repeat period.",
    )


class ControllerSpec(BaseModel):
    """Controller selection and gains, in engineering units."""

    model_config = ConfigDict(extra="forbid")

    name: str = "finite_time_adaptive"
    dt_s: float = Field(default=0.2, gt=0, description="Control period, seconds")
    eta: float = Field(default=2e-6, description="Adaptation rate")
    alpha: float = Field(default=0.02, description="Coordination gain")
    delta: float = Field(default=0.01, description="Adaptation dead-band")
    disturbance: DisturbanceSpec = DisturbanceSpec()

    @property
    def eta_well_below_alpha(self) -> bool:
        """Whether ``eta << alpha``, which the discrete form requires.

        Exposed rather than enforced: exploring the boundary is a legitimate
        experiment, so validation reports it instead of refusing to run.
        """
        return abs(self.eta) * 100 <= abs(self.alpha)


class NodeSpec(BaseModel):
    """One logical agent, bound to a host and a transport."""

    model_config = ConfigDict(extra="forbid")

    id: Annotated[int, Field(ge=1, le=MAX_NODE_ID)]
    ip: str
    type: AgentType
    enabled: bool = True
    publish_period_s: float = Field(
        default=1.0, gt=0,
        description="How often this agent broadcasts its state. Under a push "
                    "model this is a transmit rate, not a polling rate.",
    )
    neighbors: list[int] = Field(
        default_factory=list,
        description="Ids this agent reads state FROM. Directed: listing j here "
                    "means j influences us, not the reverse.",
    )

    @field_validator("ip")
    @classmethod
    def _valid_ipv4(cls, v: str) -> str:
        try:
            ipaddress.IPv4Address(v)
        except ipaddress.AddressValueError as exc:
            raise ValueError(f"{v!r} is not a valid IPv4 address") from exc
        return v

    @field_validator("neighbors")
    @classmethod
    def _no_self_loop_or_dupes(cls, v: list[int], info: ValidationInfo) -> list[int]:
        own = info.data.get("id")
        if own is not None and own in v:
            raise ValueError(f"node {own} lists itself as a neighbour")
        if len(set(v)) != len(v):
            dupes = sorted({n for n in v if v.count(n) > 1})
            raise ValueError(f"duplicate neighbours {dupes}")
        return v


class StructureSpec(BaseModel):
    """Name a generator instead of enumerating edges."""

    model_config = ConfigDict(extra="forbid")

    generator: str
    params: dict[str, Any] = Field(default_factory=dict)


class ExperimentManifest(BaseModel):
    """A complete, reproducible experiment definition."""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""
    nodes: list[NodeSpec]
    controller: ControllerSpec = ControllerSpec()
    structure: StructureSpec | None = None

    seed: int = Field(
        default=0,
        description="Seeds initial conditions AND disturbance streams, so a run "
                    "is reproducible end to end from this one number.",
    )
    ic_scheme: Literal["pcg64-v1", "legacy-arc4"] = Field(
        default="pcg64-v1",
        description=(
            "Which generator produced the initial conditions. Recorded rather "
            "than assumed: data collected before this manifest format used an "
            "ARC4-based stream whose exact values PCG64 will not reproduce. "
            "Stamping the scheme keeps older runs interpretable instead of "
            "silently comparing across two different IC distributions."
        ),
    )

    @property
    def ids(self) -> list[int]:
        return [n.id for n in self.nodes]

    @property
    def by_id(self) -> dict[int, NodeSpec]:
        return {n.id: n for n in self.nodes}

    @model_validator(mode="after")
    def _coherent(self) -> "ExperimentManifest":
        ids = [n.id for n in self.nodes]
        if not ids:
            raise ValueError("manifest declares no nodes")
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"duplicate node ids {dupes}")
        known = set(ids)
        for n in self.nodes:
            unknown = sorted(set(n.neighbors) - known)
            if unknown:
                raise ValueError(
                    f"node {n.id} references undeclared neighbour(s) {unknown}; "
                    f"declared ids are {sorted(known)}"
                )

        # The control port derives from the transport type, so two same-type
        # agents on one address would collide at bind time -- on hardware, after
        # deployment. Cheaper to refuse here.
        slots: dict[tuple[str, str], list[int]] = {}
        for n in self.nodes:
            slots.setdefault((n.ip, str(n.type)), []).append(n.id)
        clashes = {k: v for k, v in slots.items() if len(v) > 1}
        if clashes:
            detail = "; ".join(f"{ip} {t}: nodes {sorted(v)}"
                               for (ip, t), v in sorted(clashes.items()))
            raise ValueError(
                f"more than one agent of the same type on one host ({detail})")
        return self
