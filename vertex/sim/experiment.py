"""Run a whole manifest in one process, in virtual time.

This is the payoff of the transport seam and the injectable clock: the agents here
are the *production* :class:`~vertex.agent.Agent`, running the production control
loops over the production wire codec. Only the medium and the clock are swapped. So
a convergence failure, an off-by-one in the neighbour table, or a topology that
cannot converge all show up on a laptop in under a second, before any hardware is
involved.

What it deliberately does not model: real radio timing, coexistence, and the
airtime effects that drive packet loss on hardware. Loss and delay can be *imposed*
here, but their values have to come from measurement, not from this simulator.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..agent import Agent, AgentConfig
from ..clock import VirtualClock
from ..controllers.base import create
from ..topology import ExperimentManifest, check, resolve_run
from ..transports import LoopbackBus, LoopbackTransport
from .runner import SimResult, drive_virtual_clock

__all__ = ["SimulatedExperiment", "ExperimentOutcome", "simulate"]


@dataclass
class ExperimentOutcome:
    """Result of a simulated experiment."""

    manifest_name: str
    run: SimResult
    agents: dict[int, Agent] = field(default_factory=dict)
    bus_counters: dict[str, int] = field(default_factory=dict)
    #: Weakly connected components of the *effective* graph (disabled agents
    #: removed). Global spread is the wrong statistic when there is more than one.
    components: list[list[int]] = field(default_factory=list)

    # convergence measures:
    def virtual_states(self) -> dict[int, float]:
        return {nid: a.controller.vstate for nid, a in self.agents.items()}

    def spread(self) -> float:
        """max - min of the virtual states: the disagreement still outstanding."""
        z = list(self.virtual_states().values())
        return (max(z) - min(z)) if z else 0.0

    def initial_spread(self) -> float:
        firsts = [a.history[0].vstate for a in self.agents.values() if a.history]
        return (max(firsts) - min(firsts)) if firsts else 0.0

    def contraction(self) -> float:
        """Fraction of the initial disagreement removed. 1.0 is exact agreement."""
        start = self.initial_spread()
        return 1.0 - (self.spread() / start) if start else 1.0

    def component_spreads(self) -> dict[tuple[int, ...], float]:
        """Residual disagreement *within* each component.
        """
        z = self.virtual_states()
        out: dict[tuple[int, ...], float] = {}
        for comp in (self.components or [sorted(z)]):
            vals = [z[n] for n in comp if n in z]
            out[tuple(comp)] = (max(vals) - min(vals)) if vals else 0.0
        return out

    def component_contractions(self) -> dict[tuple[int, ...], float]:
        """Per-component fraction of initial in-component disagreement removed."""
        firsts = {nid: a.history[0].vstate
                  for nid, a in self.agents.items() if a.history}
        finals = self.virtual_states()
        out: dict[tuple[int, ...], float] = {}
        for comp in (self.components or [sorted(finals)]):
            f0 = [firsts[n] for n in comp if n in firsts]
            f1 = [finals[n] for n in comp if n in finals]
            if not f0 or not f1:
                continue
            start = max(f0) - min(f0)
            end = max(f1) - min(f1)
            out[tuple(comp)] = 1.0 - (end / start) if start else 1.0
        return out

    def missing_links(self) -> dict[int, tuple[int, ...]]:
        """Declared neighbours never heard from -- first thing to check on a
        run that would not converge."""
        return {nid: a.neighbors.missing for nid, a in self.agents.items()
                if a.neighbors.missing}

    def worst_delivery_ratio(self) -> float:
        ratios = [s.delivery_ratio
                  for a in self.agents.values()
                  for s in a.neighbors.link_stats().values()]
        return min(ratios) if ratios else 1.0

    def summary(self) -> str:
        lines = [
            f"{self.manifest_name}: {self.run.summary()}",
            f"  spread {self.initial_spread():.6f} -> {self.spread():.6f} "
            f"(contraction {self.contraction() * 100:.2f}%)",
            f"  bus {self.bus_counters}, worst link delivery "
            f"{self.worst_delivery_ratio() * 100:.1f}%",
        ]
        if len(self.components) > 1:
            lines.append(f"  graph is in {len(self.components)} components -- global "
                         "spread is not the right measure; per component:")
            for comp, c in self.component_contractions().items():
                lines.append(f"    nodes {comp[0]}..{comp[-1]} ({len(comp)}): "
                             f"contraction {c * 100:.2f}%, "
                             f"spread {self.component_spreads()[comp]:.6f}")
        if self.missing_links():
            lines.append(f"  UNHEARD neighbours: {self.missing_links()}")
        return "\n".join(lines)


class SimulatedExperiment:
    """Builds agents from a manifest and drives them under a virtual clock."""

    def __init__(
        self,
        manifest: ExperimentManifest,
        *,
        run_index: int = 0,
        loss: float = 0.0,
        delay_s: float = 0.0,
        bus_seed: int = 0,
        record_history: bool = True,
    ) -> None:
        self.manifest = manifest
        self.clock = VirtualClock()
        self.bus = LoopbackBus(clock=self.clock, loss=loss, delay_s=delay_s,
                               seed=bus_seed)
        self.agents: dict[int, Agent] = {}

        for rt in resolve_run(manifest, run_index).values():
            controller = create(manifest.controller.name, rt.params,
                                seed=rt.disturbance_seed)
            self.agents[rt.id] = Agent(
                AgentConfig(
                    node_id=rt.id,
                    neighbor_ids=tuple(rt.spec.neighbors),
                    dt_s=manifest.controller.dt_s,
                    publish_period_s=rt.spec.publish_period_s,
                    enabled=rt.spec.enabled,
                ),
                controller,
                LoopbackTransport(self.bus, rt.id),
                self.clock,
                record_history=record_history,
            )

    async def run(self, *, duration_s: float) -> ExperimentOutcome:
        for agent in self.agents.values():
            await agent.start()

        factories = []
        for agent in self.agents.values():
            factories.append(agent.run_control_loop)
            factories.append(agent.run_publish_loop)

        result = await drive_virtual_clock(self.clock, factories, until_s=duration_s)

        for agent in self.agents.values():
            await agent.stop()

        return ExperimentOutcome(
            manifest_name=self.manifest.name, run=result,
            agents=self.agents, bus_counters=self.bus.counters,
            components=check(self.manifest).effective_components,
        )


async def simulate(manifest: ExperimentManifest, *, duration_s: float,
                   **kw) -> ExperimentOutcome:
    """Convenience wrapper: build and run in one call."""
    return await SimulatedExperiment(manifest, **kw).run(duration_s=duration_s)
