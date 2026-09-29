"""Experiment manifests -- the declarative definition of a run.
"""

from __future__ import annotations

import ipaddress
from typing import Annotated, Any, ClassVar, Literal

from pydantic import (BaseModel, ConfigDict, Field, ValidationInfo, field_validator,
                      model_validator)

from ..net import AgentType

__all__ = ["AgentType", "NodeSpec", "DisturbanceSpec", "ControllerSpec",
           "RadioSpec", "StructureSpec", "ScheduledEvent", "ExperimentManifest",
           "MAX_NODE_ID"]

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


class MicrogridSpec(BaseModel):
    """Fleet-wide settings for the microgrid benchmark's controllers.

    Everything here is identical across agents; what differs per agent is in
    :class:`NodePlantSpec` and in `coordination`. Splitting them that way is
    what makes a typo visible: a gain that was meant to be common cannot end
    up different on one node, because there is only one place to write it.

    Declared modifications against the source are named as such in the field
    descriptions, so a reader of the manifest can see which numbers are the
    document's and which are ours.
    """

    model_config = ConfigDict(extra="forbid")

    #: Which physical interface. `adaptive` is C_AA when eps = 0 and C_DZ when
    #: it is positive; `lc` is C_LC0 with the terminal policy and C_LC+ without.
    interface: Literal["adaptive", "lc"] = "adaptive"

    # --- the plant, common part ---------------------------------------------
    profile: Literal["rep", "per"] = "per"
    p_max: float = Field(default=10.0, gt=0, description="kW")
    q_max: float = Field(default=12.0, gt=0, description="kvar")

    # --- the common virtual layer -------------------------------------------
    T_o: float = Field(default=30.0, gt=0, description="observer deadline")
    alpha_0: float = Field(
        default=1.0, gt=0,
        description="DECLARED MODIFICATION: the source derives 0.1 from "
                    "1/(a_o T_o); at that value the observer misses the "
                    "calibration band")
    delta_o: float = Field(
        default=0.125, gt=0,
        description="regularization width; the document ties it to h_v")
    kappa_o: float = 1e-3
    xi: float = 0.025
    c_0: float = 0.1

    # --- the physical interface ---------------------------------------------
    mu_max: float = Field(default=10.0, gt=0)
    #: adaptive only. An increment per update, not a rate: gamma = h gamma_ct.
    gamma: float = Field(default=0.0025, ge=0)
    #: Dead-zone threshold. A single number applies to every agent; a
    #: mapping gives one per agent, which is what eq:epsilon_calibration
    #: produces, since it sizes each threshold from that agent's own measured
    #: implementation error. 0 everywhere selects C_AA.
    eps: float | dict[int, float] = Field(default=0.0)

    @field_validator("eps")
    @classmethod
    def _eps_nonnegative(cls, v):
        bad = ([v] if isinstance(v, (int, float)) and v < 0
               else sorted(k for k, x in v.items() if x < 0)
               if isinstance(v, dict) else [])
        if bad:
            raise ValueError(f"dead-zone thresholds must be >= 0, got {bad}")
        return v

    def eps_for(self, node_id: int) -> float:
        """This agent's threshold, from the scalar or the per-agent mapping."""
        if isinstance(self.eps, dict):
            return float(self.eps.get(node_id, 0.0))
        return float(self.eps)
    vartheta0: float = 0.0
    #: lc only.
    T_c: float = Field(default=40.0, gt=0, description="control deadline")
    beta_c: float = Field(default=2.0, gt=0)
    Phi: float = Field(
        default=3e-3,
        description="adaptation gain. At the source's 3e-3 the estimate is "
                    "inert; raising it is a DECLARED MODIFICATION")
    terminal: bool = Field(
        default=True,
        description="True selects C_LC0, which zeroes the command AND freezes "
                    "S_hat after T_c; False selects C_LC+")
    adapt_band: float | None = Field(
        default=None,
        description="DECLARED MODIFICATION: hold the parameter update until "
                    "||sigma||_1 falls below this, so the transient deposits "
                    "nothing. None is the document's behaviour")


class NodePlantSpec(BaseModel):
    """One generator's emulator data.

    ``S`` is the unknown the LC interface estimates and the adaptive ones
    never see. It is here because the emulator needs it and nothing else does.
    """

    model_config = ConfigDict(extra="forbid")

    kappa: float = Field(gt=0, description="rating share; sum over agents = 1")
    S: tuple[float, float] = Field(description="the unknown parameter")
    #: Initial condition in PHYSICAL coordinates, which is where the nameplate
    #: constrains it. The controller normalizes by kappa.
    X0: tuple[float, float] = (0.0, 0.0)
    #: Initial virtual estimate, in the normalized coordinates z lives in.
    z0: tuple[float, float] = (0.0, 0.0)


class ControllerSpec(BaseModel):
    """Controller selection and gains, in engineering units."""

    model_config = ConfigDict(extra="forbid")

    name: str = "finite_time_adaptive"
    dt_s: float = Field(default=0.2, gt=0, description="Control period, seconds")
    eta: float = Field(default=5e-5, description="Adaptation RATE, per second")
    gain_ij: float = Field(default=0.1, description="Coupling gain (rate), the paper's a_ij")
    #: Exponent of the sign-power coupling: v_i = -sum sign(z_i - z_j) |z_i - z_j|^alpha.
    #: 1/2 is the finite-time case and is what every run before 2026-08-28
    #: used, when it was a hard-coded square root.
    alpha: float = Field(default=0.5, gt=0.0, le=1.0,
                         description="Sign-power exponent")
    delta: float = Field(default=0.01, description="Adaptation dead-band")
    disturbance: DisturbanceSpec = DisturbanceSpec()
    #: Present only for the microgrid benchmark's controllers, which ignore
    #: the flat gains above and read this instead.
    microgrid: MicrogridSpec | None = None

    @property
    def eta_well_below_gain(self) -> bool:
        """Whether ``eta << gain_ij``, which the discrete form requires.

        Exposed rather than enforced: exploring the boundary is a legitimate
        experiment, so validation reports it instead of refusing to run.
        """
        return abs(self.eta) * 100 <= abs(self.gain_ij)


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
    #: Emulator data, for the microgrid benchmark only.
    plant: NodePlantSpec | None = None

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


class RadioSpec(BaseModel):
    """BLE advertising and scanning parameters.
    
    Where they go depends on the agent:

    * ``ble``    -- into a RADIO frame, applied on the nRF.
    * ``bridge`` -- into the Pi's own controller via raw HCI.
    * ``wifi``   -- nowhere; recorded anyway, so the three agent types in one
      experiment carry the same environment block and stay comparable.

    Milliseconds here, 0.625 ms units on the wire. The conversion happens once,
    at the transport, and both the request and the programmed units are recorded.
    """

    model_config = ConfigDict(extra="forbid")

    #: 100 ms, not the 20 ms of Bluetooth 5.0. Bluetooth 4.x required
    #: Advertising_Interval_Min >= 0x00A0 for ADV_NONCONN_IND and ADV_SCAN_IND;
    #: 5.0 dropped it, but the CYW43455 enforces the 4.x rule and rejects
    #: anything faster with "invalid HCI command parameters" on opcode 0x2006.
    #: Measured, not assumed -- `scripts/adv_floor.py` reports it per adv type.
    #:
    #: This is a hard ceiling on the platform: a bridge cannot publish faster
    #: than 10 Hz without capping its own delivery (see PLATFORM.md 6.3).
    #: Rejecting here costs a validation error; not rejecting cost 10 runs.
    adv_interval_ms: float = Field(
        default=100.0, ge=100.0, le=10240.0,
        description="Advertising interval. The CYW43455 enforces the Bluetooth "
                    "4.x floor of 100 ms for non-connectable undirected "
                    "advertising, not the 20 ms of 5.0.",
    )
    #: Upper end of the advertising interval. None means min == max, which is
    #: what every run before 2026-08-25 used and which leaves the controller no
    #: range to spread events over: at the trigger all advertisers start within
    #: the trigger spread (~10-30 ms), so their 100 ms cycles begin nearly in
    #: phase. A radio cannot scan while it advertises, so a pair whose events
    #: coincide stays blind to each other until their clocks drift apart --
    #: measured as one link silent for 7.5-26 s at run start, in ~10% of
    #: nine-agent runs (PLATFORM.md 6.10).
    adv_interval_max_ms: float | None = Field(default=None, ge=100.0, le=10240.0)

    scan_interval_ms: float = Field(default=100.0, ge=2.5, le=10240.0)
    scan_window_ms: float = Field(
        default=100.0, ge=2.5, le=10240.0,
        description="Time spent listening within each interval. Equal to the "
                    "interval means continuous scanning.",
    )
    channel_map: int = Field(
        default=0x07, ge=0x01, le=0x07,
        description="Bitmask over advertising channels 37/38/39 = 2402/2426/2480 "
                    "MHz. Restricting it is how a run steers clear of the WLAN "
                    "channel in use; 0x07 uses all three.",
    )
    #: Suppress repeated advertising reports in the controller, below the host.
    #:
    #: Designed for device DISCOVERY, where an advertiser repeating every
    #: interval should be reported once. Here an advertisement is a data packet
    #: whose payload changes every publish, so the feature is being applied to a
    #: channel it was not designed for. It matters most when redundancy is in
    #: use: at k = T_pub/T_adv > 1 the same value is advertised more than once
    #: with identical bytes, and a filtering receiver may drop exactly the retry
    #: that redundancy depends on (PLATFORM.md 6.6 measures the retry as worth
    #: +0.07 to +0.15 delivery).
    #:
    #: Default False, which is what every run to date used on the Pi. The nRF
    #: scanner has it ON in firmware, so the two ends differ; this field exists
    #: to A/B the Pi side without reflashing.
    filter_duplicates: bool = False

    passive_scan: bool = Field(
        default=True,
        description="Passive scanning never transmits a scan request, so it adds "
                    "no TX airtime -- which is the thing that blanks our own BLE "
                    "receive window.",
    )

    #: Wi-Fi transmit power to enforce on the wireless interface, in dBm.
    #:
    #: Declared here because it is otherwise an UNCONTROLLED variable. The nRF
    #: reports a granted +8 dBm in its STATS, but `iw` reports 31.00 dBm on a Pi,
    #: which is the brcmfmac placeholder and not a measurement (vertex/net.py).
    #: So every run collected before 2026-09-11 has a known BLE transmit power
    #: and an unknown Wi-Fi one, which leaves the BLE-versus-Wi-Fi comparison
    #: with an uncontrolled term in it.
    #:
    #: Setting it is verified to work: 800 mBm reads back +8.0 dBm
    #: (PLATFORM.md 8.x, scripts/wifi_txpower.sh). It does NOT survive a reboot
    #: and nothing else re-asserts it, which is why it belongs in the manifest
    #: rather than in an operator's shell history.
    #:
    #: `None` means "do not touch", preserving the behaviour of every manifest
    #: written before this field existed.
    tx_power_dbm: float | None = Field(
        default=None, ge=0.0, le=30.0,
        description="Wi-Fi transmit power to set on the interface, in dBm. None "
                    "leaves the driver default, which reports as an untrusted "
                    "placeholder and is therefore unknown.",
    )

    @model_validator(mode="after")
    def _adv_range_ordered(self) -> "RadioSpec":
        if (self.adv_interval_max_ms is not None
                and self.adv_interval_max_ms < self.adv_interval_ms):
            raise ValueError(
                f"adv_interval_max_ms ({self.adv_interval_max_ms}) is below "
                f"adv_interval_ms ({self.adv_interval_ms})")
        return self

    @model_validator(mode="after")
    def _window_fits(self) -> "RadioSpec":
        if self.scan_window_ms > self.scan_interval_ms:
            raise ValueError(
                f"scan_window_ms ({self.scan_window_ms}) exceeds scan_interval_ms "
                f"({self.scan_interval_ms}); the window is the listening portion "
                f"of the interval, so it cannot be longer than it"
            )
        return self

    @property
    def scan_duty_cycle(self) -> float:
        return self.scan_window_ms / self.scan_interval_ms


class StructureSpec(BaseModel):
    """Name a generator instead of enumerating edges."""

    model_config = ConfigDict(extra="forbid")

    generator: str
    params: dict[str, Any] = Field(default_factory=dict)


class CoordinationSpec(BaseModel):
    """The exogenous reference and which agents can see it.

    Present only for the microgrid benchmark; the scalar coordination law has
    no exogenous reference and omits this block entirely.

    The two fields live together because neither means anything alone. The
    reference is declared **once**, not per node: a per-node field lets two
    agents disagree by a typo, and a fleet that agrees on the wrong value
    produces a run that looks correct and measures nothing.

    Which agents see it is the other half. Only a pinned agent is *sent* the
    reference at all, so "DG 1 alone receives it" is a property of what
    crosses the control plane, not a convention inside the controller.
    """

    model_config = ConfigDict(extra="forbid")

    #: The setpoint, in the same coordinates the controller works in. Two
    #: components for the microgrid benchmark: kW and kvar.
    reference: tuple[float, float]
    #: Agent id -> pinning coefficient b_i. Absent means b_i = 0, which means
    #: that agent does not know the reference and must reach it through the
    #: graph.
    pinned: dict[int, float] = Field(default_factory=dict)

    @field_validator("pinned")
    @classmethod
    def _positive_weights(cls, v: dict[int, float]) -> dict[int, float]:
        bad = sorted(k for k, w in v.items() if w <= 0)
        if bad:
            raise ValueError(
                f"pinning coefficients must be > 0, got zero or negative at "
                f"{bad}; omit an agent instead of pinning it with weight 0")
        return v


class ScheduledEvent(BaseModel):
    """A change applied to a running fleet at a fixed offset from the trigger.

    The agents already accept it: `configure` on a running agent applies to the
    live controller without touching its integrators, so a perturbation is an
    event rather than a restart. What was missing was anything to schedule it.

    The motivating case is `n30-clusters`, which disables the two bridges that
    join its three clusters. Statically that graph has no path between clusters
    and cannot reach agreement -- correctly so. The experiment it is meant to
    express is time-varying: the clusters converge separately, then the bridges
    come up and the components merge. That is G3(t), and the merge transient is
    the measurement.
    """

    model_config = ConfigDict(extra="forbid")

    at_s: float = Field(gt=0.0, description="offset from the run trigger")
    nodes: list[int] = Field(min_length=1)
    #: Fields to override on those nodes. `enabled` is the one this exists for.
    set: dict[str, Any] = Field(default_factory=dict)

    #: Fields an event may never touch. The reference is constant by
    #: assumption: `rdot = 0` is what lets the observer drop its
    #: reference-derivative estimator exactly, and a reference that moves
    #: mid-run brings that estimator, its deadline and its regularizer back.
    FROZEN: ClassVar[tuple[str, ...]] = ("reference", "pinned",
                                         "coordination")

    @model_validator(mode="after")
    def _has_changes(self) -> "ScheduledEvent":
        if not self.set:
            raise ValueError("a scheduled event with no `set` changes nothing")
        frozen = sorted(set(self.set) & set(self.FROZEN))
        if frozen:
            raise ValueError(
                f"event at {self.at_s} s changes {frozen}, which must stay "
                "constant: the observer's derivative estimator was removed on "
                "the assumption that the reference does not move")
        return self


class ExperimentManifest(BaseModel):
    """A complete, reproducible experiment definition."""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""
    nodes: list[NodeSpec]
    controller: ControllerSpec = ControllerSpec()
    radio: RadioSpec = RadioSpec()
    structure: StructureSpec | None = None
    #: The exogenous reference, for laws that have one. Omitted by the scalar
    #: coordination law, which agrees on an emergent value instead.
    coordination: CoordinationSpec | None = None
    #: Mid-run changes, applied in order of `at_s`. Empty for a static run.
    events: list[ScheduledEvent] = Field(default_factory=list)

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
