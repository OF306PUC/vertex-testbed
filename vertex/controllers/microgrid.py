"""The microgrid benchmark's controller: plant, virtual layer, interface.

One agent runs four things and this composes three of them; the fourth,
logging, is the agent's. The tick order is the specification's and is the
substance of the digital design, not a detail:

    acquire the measurement and the held packet snapshot
    compute the virtual candidate and the effective slope g_i[k]
    evaluate the interface on the OLD physical, virtual and adaptive states
    saturate and apply
    update the emulator
    commit the virtual state

so the command at sample k never sees a state that sample k produced.

## Two registered controllers, not four

The comparison has four arms but only two implementations, because each pair
differs in exactly one number and a second class would be a second thing that
could differ:

    microgrid_adaptive   C_AA when eps = 0, C_DZ when it is positive
    microgrid_lc         C_LC0 with the terminal policy, C_LC+ without

## Where S_i is, and is not

The emulator holds it. Neither interface is constructed with a reference to
the plant, so the information barrier is the absence of a reference rather
than a convention. ``log_state`` exposes the unquantized state for the
offline calibration the dead-zone sizing needs; it is not on any path the
interfaces can reach.
"""

from __future__ import annotations

import math
from typing import Any, Sequence

#: When the saturation branch arms, for an interface that has no T_c of its
#: own. The adaptive interfaces do not; the control deadline is still the
#: point past which sustained saturation means something has gone wrong.
T_C_DEFAULT = 40.0

from ..numeric import INV_SCALE_FACTOR, is_finite_number, to_finite_float
from .base import Controller, ControllerOutput, ControllerParams, register
from .interface_adaptive import AdaptiveInterface, AdaptiveParams
from .interface_lc import LCInterface, LCParams
from .plant import MicrogridPlant, PlantParams
from .virtual import VirtualLayer, VirtualParams

__all__ = ["MicrogridController", "MicrogridAdaptive", "MicrogridLC"]

#: eq:watchdog. A trial that trips one of these is a failure and is reported
#: as one, not removed: the adaptive gain is deliberately neither projected
#: nor capped, because capping it would conceal the drift mechanism the
#: comparison exists to measure, so something has to notice when it runs away.
#:
#: The bounds are three orders above anything a converging run reaches, so
#: tripping means the run has left the regime and not that it was slow.
#: Note what is NOT enforced here: eq:power_ratings. A generator past its
#: nameplate is reported by the analysis, because clipping the emulator would
#: hide exactly the behaviour C_LC0 exhibits.
X_WATCH = 5.0e3
VARTHETA_WATCH = 5.0e3
SAT_PERSIST_UPDATES = 5

#: Logged beside x and z on every arm. The family-specific channels follow.
COMMON_CHANNELS = ("g_P", "g_Q", "mu_P", "mu_Q", "muapp_P", "muapp_Q",
                   "sigma_P", "sigma_Q", "c", "theta", "saturated", "trig",
                   "watchdog")

#: Watchdog causes, in the order they are tested. The logged channel is the
#: index, 0 meaning the run is still inside its envelope.
WATCHDOG_CAUSES = ("", "state", "virtual", "vartheta", "saturation")


class MicrogridController(Controller):
    """Shared body. The subclasses pick the interface and the log schema."""

    dim = 2
    coords = ("P", "Q")
    holds_stale_neighbours = True
    publishes_in_tick = True
    logs_timing = True

    def __init__(self, params: ControllerParams, *, seed: int | None = None,
                 uniform=None) -> None:
        self.params = params
        self.dt = params.dt_s
        self._blocks(params)
        self.reset()

    # --- configuration -------------------------------------------------------
    def _blocks(self, p: ControllerParams) -> None:
        for name in ("plant", "virtual"):
            if getattr(p, name, None) is None:
                raise ValueError(
                    f"{self.name} needs a `{name}` block in ControllerParams; "
                    "the scalar law's flat fields do not configure it")
        self.plant_params: PlantParams = p.plant
        self.virtual_params: VirtualParams = p.virtual
        self.degree = int(p.degree)
        # x_i[0] = X_i[0] / kappa_i. The manifest carries the physical initial
        # condition, because that is the one the nameplate constrains.
        self.x0 = (p.state0_P / self.plant_params.kappa,
                   p.state0_Q / self.plant_params.kappa)

    def set_params(self, params: ControllerParams) -> None:
        """Update mid-run, preserving integrator state.

        The prescribed-time schedules are functions of run time, so the step
        counter is deliberately not reset here: a mid-run reconfiguration must
        not rewind the deadline.
        """
        self.params = params
        self.dt = params.dt_s
        self._blocks(params)

    # --- lifecycle -----------------------------------------------------------
    def reset(self) -> None:
        self.plant = MicrogridPlant(self.plant_params, *self.x0)
        self.virtual = VirtualLayer(self.virtual_params, self.degree,
                                    self.params.vstate0_P,
                                    self.params.vstate0_Q)
        self.interface = self._make_interface()
        self.step_count = 0
        self._last = (0.0,) * len(COMMON_CHANNELS)
        self.watchdog_cause = ""
        self._sat_run = 0

    def _make_interface(self):                      # pragma: no cover
        raise NotImplementedError

    # --- introspection -------------------------------------------------------
    # --- the watchdog, eq:watchdog ------------------------------------------
    def _watchdog(self, saturated: bool, armed: bool) -> str:
        """Which bound this tick broke, or "" for none.

        Saturation only arms after the control deadline: the transient is
        expected to saturate while the plant is far from its share, and
        tripping on that would end every run at about t = 1 s.
        """
        self._sat_run = self._sat_run + 1 if saturated else 0
        x = (self.plant.xP, self.plant.xQ)
        if not all(math.isfinite(v) for v in x) or max(abs(v) for v in x) > X_WATCH:
            return "state"
        z = self.virtual.packet()
        if not all(math.isfinite(v) for v in z):
            return "virtual"
        v = self.interface.vartheta
        if math.isfinite(v) and v > VARTHETA_WATCH:
            return "vartheta"
        if armed and self._sat_run > SAT_PERSIST_UPDATES:
            return "saturation"
        return ""

    @property
    def tripped(self) -> bool:
        """Whether the watchdog has fired. Latching: a run does not recover."""
        return bool(self.watchdog_cause)

    @property
    def t_s(self) -> float:
        """Run time. Never wraps: the schedules are absolute in it, and a
        counter that wrapped would rewind every deadline."""
        return self.step_count * self.dt

    @property
    def vstate(self) -> tuple[float, float]:
        return self.virtual.packet()

    @property
    def state(self) -> tuple[float, float]:
        return self.plant.xP, self.plant.xQ

    @property
    def log_state(self) -> tuple[float, float]:
        """The unquantized emulator state, for offline calibration only.

        The dead-zone sizing rule needs the difference between what the
        controller measured and what the plant actually was, which no
        controller may read. Kept off the interfaces' path.
        """
        return self.plant.xP, self.plant.xQ

    # --- the neighbour snapshot ---------------------------------------------
    def _snapshot(self, neighbor_vstates: Sequence[Any],
                  neighbor_enabled: Sequence[Any]):
        """Held neighbour values as ``(z_P, z_Q)`` pairs, in declared order.

        ``neighbor_vstates`` is flat with :attr:`dim` scaled integers per
        neighbour. A neighbour whose packet was lost contributes its **last**
        value, not nothing: dropping the term would change the graph, holding
        it does not, and the coordination law is a disagreement over a fixed
        edge set.
        """
        vs = neighbor_vstates if isinstance(neighbor_vstates, (list, tuple)) else ()
        out = []
        for j in range(self.degree):
            a, b = 2 * j, 2 * j + 1
            zP = to_finite_float(vs[a]) * INV_SCALE_FACTOR if a < len(vs) else 0.0
            zQ = to_finite_float(vs[b]) * INV_SCALE_FACTOR if b < len(vs) else 0.0
            out.append((zP if is_finite_number(zP) else 0.0,
                        zQ if is_finite_number(zQ) else 0.0))
        return out

    # --- the control law -----------------------------------------------------
    def step(self, neighbor_vstates: Sequence[Any],
             neighbor_enabled: Sequence[Any]) -> ControllerOutput:
        t = self.t_s
        h = self.dt

        # 1. the held snapshot, frozen for this tick
        self.virtual.hold(self._snapshot(neighbor_vstates, neighbor_enabled))

        # 2. the virtual candidate and the effective slope
        zP_old, zQ_old = self.virtual.zP, self.virtual.zQ
        theta = self.virtual.theta(t, h)
        zP_new, zQ_new, c_new, gP, gQ, _eP, _eQ = self.virtual.candidate(t, h)

        # 3. the interface, on the old physical, virtual and adaptive states.
        #    Under S_0 the measurement is exact; a quantizer and bounded noise
        #    would be applied here and nowhere else.
        xP, xQ = self.plant.xP, self.plant.xQ
        (muP, muQ, aP, aQ, sP, sQ, _wP, _wQ,
         sat, trig, _fired) = self.interface.step(t, xP, xQ, zP_old, zQ_old,
                                                  gP, gQ)

        # 4 and 5. saturate, apply, advance the emulator
        self.plant.step(aP, aQ, t, h)

        # 6. commit the virtual state. The adaptive state was committed inside
        #    the interface, after its command was formed.
        self.virtual.commit(zP_new, zQ_new, c_new)

        if not self.watchdog_cause:
            self.watchdog_cause = self._watchdog(
                sat, armed=t >= getattr(self.params.interface, "T_c", T_C_DEFAULT))
        self.step_count += 1
        self._last = (gP, gQ, muP, muQ, aP, aQ, sP, sQ,
                      self.virtual.c, theta, 1.0 if sat else 0.0, trig,
                      float(WATCHDOG_CAUSES.index(self.watchdog_cause)))
        return ControllerOutput(state=(self.plant.xP, self.plant.xQ),
                                vstate=(zP_new, zQ_new),
                                extra=(*self._last, *self._family_channels()))

    def _family_channels(self) -> tuple[float, ...]:   # pragma: no cover
        raise NotImplementedError


@register
class MicrogridAdaptive(MicrogridController):
    """``C_AA`` when ``eps = 0``, ``C_DZ`` when it is positive."""

    name = "microgrid_adaptive"

    @classmethod
    def channels(cls) -> tuple[str, ...]:
        return (*COMMON_CHANNELS, "vartheta", "updates")

    def _make_interface(self) -> AdaptiveInterface:
        p = self.params.interface
        if not isinstance(p, AdaptiveParams):
            raise ValueError(f"{self.name} needs an AdaptiveParams interface "
                             f"block, got {type(p).__name__}")
        return AdaptiveInterface(p, self.dt)

    @property
    def label(self) -> str:
        return self.interface.label

    def _family_channels(self) -> tuple[float, ...]:
        return (self.interface.vartheta, float(self.interface.updates))


@register
class MicrogridLC(MicrogridController):
    """``C_LC0`` with the source's terminal policy, ``C_LC+`` without."""

    name = "microgrid_lc"

    @classmethod
    def channels(cls) -> tuple[str, ...]:
        return (*COMMON_CHANNELS, "S1", "S2", "updates")

    def _make_interface(self) -> LCInterface:
        p = self.params.interface
        if not isinstance(p, LCParams):
            raise ValueError(f"{self.name} needs an LCParams interface block, "
                             f"got {type(p).__name__}")
        return LCInterface(p, self.dt)

    @property
    def label(self) -> str:
        return self.interface.label

    def _family_channels(self) -> tuple[float, ...]:
        return (self.interface.S1, self.interface.S2,
                float(self.interface.updates))
