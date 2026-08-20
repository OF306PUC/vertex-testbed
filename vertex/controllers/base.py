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
    """

    state: float          # x
    vstate: float         # z -- the only quantity broadcast to neighbours
    vartheta: float       # theta -- adaptive gain

    def scaled(self) -> tuple[int, int, int]:
        """(state, vstate, vartheta) as scaled integers for the wire/logs."""
        return quantize(self.state), quantize(self.vstate), quantize(self.vartheta)


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
    adaptation rate, ``alpha`` the coordination gain, ``delta`` the adaptation
    dead-band.
    """

    dt_s: float = 0.2
    state: float = 0.0
    vstate: float = 0.0
    vartheta: float = 0.0
    eta: float = 2e-6
    alpha: float = 0.02
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
