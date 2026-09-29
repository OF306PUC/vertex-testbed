"""The controller plugin seam.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field, replace
from typing import Any, Sequence

from ..numeric import quantize

__all__ = ["ControllerOutput", "ControllerParams", "DisturbanceParams",
           "Controller", "REGISTRY", "register", "create"]


@dataclass(frozen=True, slots=True)
class ControllerOutput:
    """One step's result, in engineering units.

    ``state`` and ``vstate`` are sequences because the coordination law is no
    longer scalar: the microgrid benchmark carries ``[P, Q]`` per agent. The
    scalar law uses length 1 and its numbers, order and log columns are
    unchanged.

    ``extra`` carries whatever else that controller logs, in the order its
    :meth:`Controller.channels` declares. It is a flat tuple rather than a
    mapping because it is written on the hot path, once per control period,
    into a fixed-width binary row; the names live in the run metadata so a
    reader never has to infer them.

    The families genuinely differ in what they have: AA and DZ carry a sliding
    gain and no parameter estimate, the LC variants the reverse. A flat
    declared tuple says that plainly, where a fixed field for each would put a
    permanent NaN in half the runs.
    """

    state: Sequence[float]       # x
    vstate: Sequence[float]      # z -- the only quantity broadcast to neighbours
    extra: Sequence[float] = ()  # the controller's declared channels, in order

    def channels(self) -> tuple[float, ...]:
        """Every value this step produced, flat, in declared column order."""
        return (*self.state, *self.vstate, *self.extra)

    def scaled(self) -> tuple[int, ...]:
        """Every channel as a scaled integer, for the wire and the logs.

        Order is state, then vstate, then the extras, which for the scalar law
        is exactly ``(state, vstate, vartheta)`` as before.
        """
        return tuple(quantize(v) for v in self.channels())


@dataclass(frozen=True, slots=True)
class DisturbanceParams:
    """Additive disturbance: uniform noise + constant bias + sinusoid.

    ``nu(t) = noise_amplitude * (U - noise_offset) + beta
              + sine_amplitude * sin(2*pi*sine_frequency_hz*(t - sine_phase_s))``

    where ``U`` is uniform on [0, 1). Note ``noise_offset`` shifts the *uniform
    draw*, so 0.5 centres the noise on zero; it is not an offset added to the
    output. ``beta`` is the constant component.
    """

    enabled: bool = False
    noise_amplitude: float = 0.0
    noise_offset: float = 0.0
    beta: float = 0.0
    sine_amplitude: float = 0.0
    sine_frequency_hz: float = 0.0
    sine_phase_s: float = 0.0
    #: Length of the internal step counter's cycle. The sinusoid's time argument
    #: is derived from that counter, so this sets the disturbance's repeat period.
    period_samples: int = 1000


@dataclass(frozen=True, slots=True)
class ControllerParams:
    """Controller configuration and initial conditions, in engineering units.

    Greek names are the paper's symbols and are kept deliberately: ``eta`` is the
    adaptation rate, ``delta`` the adaptation dead-band, and ``alpha`` the
    EXPONENT of the sign-power coupling, not a gain. The coupling gain is
    ``gain_ij``, after the paper's :math:`a_{ij}`.
    """

    dt_s: float = 0.2
    state: float = 0.0
    vstate: float = 0.0
    vartheta: float = 0.0
    # --- the vector family ---------------------------------------------------
    # Optional blocks, absent for the scalar law, which configures itself from
    # the flat fields above. A controller that needs one says so and fails at
    # construction rather than running with a default that means nothing.
    #: Emulator data, including the unknown parameter. Held by the plant.
    plant: Any | None = None
    #: The common virtual layer's schedule, gains and pinning.
    virtual: Any | None = None
    #: The physical interface's own parameters, which decide which arm this is.
    interface: Any | None = None
    #: How many neighbours this agent reads, so the layer can size its sum.
    degree: int = 0
    #: Initial condition in **physical** coordinates, which is where the
    #: nameplate constrains it. The controller normalizes by kappa_i.
    state0_P: float = 0.0
    state0_Q: float = 0.0
    vstate0_P: float = 0.0
    vstate0_Q: float = 0.0
    eta: float = 5e-5               # rate per second, not per step
    gain_ij: float = 0.1
    alpha: float = 0.5
    delta: float = 0.01
    disturbance: DisturbanceParams = field(default_factory=DisturbanceParams)

    def __post_init__(self) -> None:
        # A zero period would spin the control loop.
        if self.dt_s <= 0:
            raise ValueError(f"dt_s must be > 0, got {self.dt_s}")

    def evolve(self, **changes: Any) -> "ControllerParams":
        """Return a copy with fields replaced."""
        return replace(self, **changes)


class Controller(ABC):
    """A coordination algorithm running on one agent."""

    #: Registry key manifests use to select this controller.
    name: str = "abstract"

    #: Components per state and per virtual state. 1 for the scalar law, 2 for
    #: the microgrid benchmark's ``[P, Q]``.
    dim: int = 1

    #: Whether the agent records the realized period and the computation time
    #: of each update beside the controller's own channels.
    #:
    #: sec:metrics asks for both, so a performance difference can be read
    #: against the implementation conditions rather than assumed independent
    #: of them. Off by default: turning it on widens the row, and a run
    #: recorded before it existed must stay readable byte for byte.
    logs_timing: bool = False

    #: Whether the packet is emitted from inside the control step, after the
    #: commit, rather than from a separate periodic task.
    #:
    #: False keeps the two independent loops the scalar law has always used.
    #: True is what a law whose gains depend on absolute time needs: with two
    #: loops the publish instant ties with a control instant every period,
    #: the tie-break is not stable, and the exchange lands a tick early or
    #: late depending on how the two happen to interleave. Measured on the
    #: virtual clock, the completed-step count at successive publishes went
    #: 4, 10, 14, 19: increments of 6, 4, 5 rather than 5.
    #:
    #: sec:testbed puts the transmission inside the tick for this reason:
    #: "then commit the virtual and adaptive states ... At each communication
    #: instant, transmit the encoding of the current committed virtual state."
    publishes_in_tick: bool = False

    #: Whether a neighbour whose packet is overdue should keep contributing
    #: its last value. False drops the term, which is what the scalar law has
    #: always done; True holds it, which a Laplacian disagreement needs,
    #: because that is defined over a fixed edge set and dropping a term
    #: changes the graph rather than only ageing the information.
    holds_stale_neighbours: bool = False

    #: Suffixes for the components when ``dim > 1``, so a log column reads
    #: ``state_P`` rather than ``state_0``. Empty when ``dim == 1``, where the
    #: column is just ``state``.
    coords: tuple[str, ...] = ()

    @classmethod
    def channels(cls) -> tuple[str, ...]:
        """Names of the extra logged channels, in :attr:`ControllerOutput.extra`
        order. One name per scalar: a two-component channel declares both.

        This is the controller's log schema. The run metadata records the
        resolved column list, so a reader never infers it and a controller
        that grows a channel cannot silently shift an existing column.
        """
        return ()

    @classmethod
    def column_names(cls) -> list[str]:
        """``state`` and ``vstate`` columns plus the declared extras."""
        if cls.dim == 1:
            base = ["state", "vstate"]
        else:
            if len(cls.coords) != cls.dim:
                raise ValueError(
                    f"{cls.name}: dim = {cls.dim} needs {cls.dim} coord "
                    f"suffixes, got {cls.coords}")
            base = [f"{w}_{c}" for w in ("state", "vstate") for c in cls.coords]
        return base + list(cls.channels())

    @abstractmethod
    def reset(self) -> None:
        """Return to initial conditions. Called when a run is triggered."""

    @abstractmethod
    def step(
        self,
        neighbor_vstates: Sequence[Any],
        neighbor_enabled: Sequence[Any],
    ) -> ControllerOutput:
        """Advance one control period.
        """

    @property
    @abstractmethod
    def vstate(self) -> float:
        """Current virtual state -- the quantity broadcast to neighbours."""


#: Controller implementations by manifest name. Populated by :func:`register`.
REGISTRY: dict[str, type[Controller]] = {}


def register(cls: type[Controller]) -> type[Controller]:
    """Class decorator adding a controller to :data:`REGISTRY`."""
    if cls.name in REGISTRY and REGISTRY[cls.name] is not cls:
        raise ValueError(f"controller name {cls.name!r} is already registered")
    REGISTRY[cls.name] = cls
    return cls


def create(name: str, params: ControllerParams, **kw: Any) -> Controller:
    """Instantiate a registered controller by manifest name."""
    try:
        cls = REGISTRY[name]
    except KeyError:
        raise ValueError(
            f"unknown controller {name!r}; registered: {sorted(REGISTRY)}"
        ) from None
    return cls(params, **kw)
