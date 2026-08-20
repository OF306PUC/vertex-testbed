"""One virtual agent process on one Raspberry Pi.

This is where a manifest parameter actually becomes a running controller. A Pi runs
three of these -- ``ble``, ``wifi``, ``bridge`` -- 

Lifecycle::

    launch      binds the control port. Idle: no identity, no transport, no loops.
    configure   receives an AgentAssignment -> builds controller, transport, agent
    start       opens the run log, starts the transport, launches both loops
    stop        halts the loops, finalises the log, releases the transport
    fetch       returns a stored artefact as bytes

"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any, Callable

from ..clock import Clock, WallClock
from ..control.protocol import Response, ok
from ..control.server import ControlServer
from ..controllers.base import create
from ..net import CONTROL_PORTS, STATE_PORT, AgentType, broadcast_address
from ..transports.base import Transport
from ..transports.udp import UdpTransport
from .agent import Agent, AgentConfig
from .assignment import AgentAssignment
from .runlog import RunLog, RunMeta

__all__ = ["AgentService"]

#: Artefacts ``fetch`` will serve, mapped to their filename suffix.
ARTIFACTS = {"meta": ".meta.json", "rows": None, "json": ".json"}


class AgentService:
    """Control surface and lifecycle for one agent."""

    def __init__(
        self,
        node_type: AgentType | str,
        *,
        data_dir: str | Path = "data",
        host_ip: str | None = None,
        control_host: str = "0.0.0.0",
        control_port: int | None = None,
        state_port: int = STATE_PORT,
        log_format: str = "binary",
        clock: Clock | None = None,
        transport_factory: Callable[[int, Clock], Transport] | None = None,
        environment: dict[str, Any] | None = None,
    ) -> None:
        self.node_type = AgentType(node_type)
        self.data_dir = Path(data_dir)
        self.host_ip = host_ip
        self.state_port = state_port
        self.log_format = log_format
        self.clock = clock or WallClock(time.time())
        self.environment = environment or {}
        self._transport_factory = transport_factory

        self.control = ControlServer(
            self._handlers(), host=control_host,
            port=control_port if control_port is not None
            else CONTROL_PORTS[self.node_type],
        )

        self.assignment: AgentAssignment | None = None
        self.agent: Agent | None = None
        self.runlog: RunLog | None = None
        self.run_name: str | None = None
        self._tasks: list[asyncio.Task] = []
        self._started_at: float = 0.0

    # lifecycle:
    async def serve(self) -> "AgentService":
        await self.control.start()
        return self

    async def shutdown(self) -> None:
        await self._halt()
        await self.control.stop()

    @property
    def control_port(self) -> int:
        return self.control.bound_port

    @property
    def running(self) -> bool:
        return bool(self._tasks)

    # building the agent:
    def _make_transport(self, node_id: int) -> Transport:
        if self._transport_factory is not None:
            return self._transport_factory(node_id, self.clock)
        target = broadcast_address(self.host_ip) if self.host_ip else "255.255.255.255"
        return UdpTransport(node_id, self.clock,
                            send_to=(target, self.state_port),
                            bind_port=self.state_port, broadcast=True)

    def _build(self, assignment: AgentAssignment) -> Agent:
        controller = create(assignment.controller,
                            assignment.to_controller_params(),
                            seed=assignment.disturbance_seed)
        return Agent(
            AgentConfig(
                node_id=assignment.node_id,
                neighbor_ids=tuple(assignment.neighbors),
                dt_s=assignment.dt_s,
                publish_period_s=assignment.publish_period_s,
                enabled=assignment.enabled,
                max_neighbor_age_s=assignment.max_neighbor_age_s,
            ),
            controller,
            self._make_transport(assignment.node_id),
            self.clock,
            record_history=False,       # the run log is the record; history would
                                        # duplicate a 26-minute run in memory
        )

    async def _halt(self) -> None:
        for t in self._tasks:
            t.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        if self.agent is not None:
            await self.agent.stop()

    # command handlers:
    def _handlers(self) -> dict[str, Any]:
        return {
            "status": self._status, "configure": self._configure,
            "start": self._start, "stop": self._stop,
            "list_runs": self._list_runs, "fetch": self._fetch,
        }

    async def _status(self, args: dict[str, Any]) -> tuple[Response, None]:
        a = self.assignment
        data: dict[str, Any] = {
            "node_type": str(self.node_type),
            "node_id": a.node_id if a else None,
            "configured": a is not None,
            "running": self.running,
            "run_name": self.run_name,
            "samples": self.runlog.samples if self.runlog else 0,
            "control_port": self.control_port,
        }
        if self.agent is not None:
            data["neighbors_heard"] = list(self.agent.neighbors.heard)
            data["neighbors_missing"] = list(self.agent.neighbors.missing)
            data["published"] = self.agent.published
            data["links"] = {
                str(nid): {"delivery_ratio": round(st.delivery_ratio, 4),
                           "received": st.received, "lost": st.lost,
                           "median_delay_us": st.median_delay_us}
                for nid, st in self.agent.neighbors.link_stats().items()
            }
            data["timing"] = {
                "control_iterations": self.agent.control_timing.iterations,
                "control_mean_error_s": self.agent.control_timing.mean_error_s,
                "control_max_error_s": self.agent.control_timing.max_error_s,
                "control_overruns": self.agent.control_timing.overruns,
            }
        return ok(**data), None

    async def _configure(self, args: dict[str, Any]) -> tuple[Response, None]:
        assignment = AgentAssignment.model_validate(args)
        if assignment.node_type != self.node_type:
            raise ValueError(
                f"assignment is for a {assignment.node_type} agent but this process "
                f"serves {self.node_type}; check the manifest's ip/type mapping"
            )

        if self.running and self.agent is not None:
            # Live update: apply to the running controller without touching its
            # integrators, so a scripted perturbation is an event, not a restart.
            self.agent.controller.set_params(assignment.to_controller_params())
            self.agent.config = AgentConfig(
                node_id=assignment.node_id,
                neighbor_ids=tuple(assignment.neighbors),
                dt_s=assignment.dt_s,
                publish_period_s=assignment.publish_period_s,
                enabled=assignment.enabled,
                max_neighbor_age_s=assignment.max_neighbor_age_s,
            )
            self.assignment = assignment
            return ok(node_id=assignment.node_id, live_update=True), None

        self.assignment = assignment
        self.agent = self._build(assignment)
        return ok(node_id=assignment.node_id, live_update=False), None

    async def _start(self, args: dict[str, Any]) -> tuple[Response, None]:
        if self.assignment is None or self.agent is None:
            raise RuntimeError("configure() before start()")
        if self.running:
            raise RuntimeError(f"already running {self.run_name!r}")

        run_name = str(args.get("run_name") or "run")
        a = self.assignment
        self.runlog = RunLog(
            self.data_dir,
            RunMeta(
                run_name=run_name, node_id=a.node_id, node_type=str(a.node_type),
                manifest_name=a.manifest_name, seed=a.seed, run_index=a.run_index,
                dt_s=a.dt_s, publish_period_s=a.publish_period_s,
                neighbors=list(a.neighbors),
                controller=a.model_dump(),
                units="engineering",
                environment=dict(self.environment),
            ),
            fmt=self.log_format,
        ).start(started_at=_utc_now())

        self.agent.on_sample = self._record
        await self.agent.start()
        self.run_name = run_name
        self._started_at = time.time()
        self._tasks = [
            asyncio.create_task(self.agent.run_control_loop()),
            asyncio.create_task(self.agent.run_publish_loop()),
        ]
        return ok(run_name=run_name, node_id=a.node_id), None

    def _record(self, t_s: float, out, vstates, fresh) -> None:
        if self.runlog is not None:
            self.runlog.append(t_s, out.state, out.vstate, out.vartheta, vstates, fresh)

    async def _stop(self, args: dict[str, Any]) -> tuple[Response, None]:
        if not self.running:
            return ok(run_name=self.run_name, samples=
                      self.runlog.samples if self.runlog else 0, was_running=False), None
        await self._halt()
        samples = 0
        if self.runlog is not None:
            self.runlog.finalize(ended_at=_utc_now())
            samples = self.runlog.samples
        run_name, self.run_name = self.run_name, None
        return ok(run_name=run_name, samples=samples, was_running=True,
                  duration_s=round(time.time() - self._started_at, 3)), None

    async def _list_runs(self, args: dict[str, Any]) -> tuple[Response, None]:
        if not self.data_dir.exists():
            return ok(runs=[]), None
        return ok(runs=sorted(p.name for p in self.data_dir.iterdir() if p.is_dir())), None

    async def _fetch(self, args: dict[str, Any]) -> tuple[Response, bytes]:
        run_name = str(args.get("run_name") or "")
        artifact = str(args.get("artifact") or "json")
        if artifact not in ARTIFACTS:
            raise ValueError(f"unknown artefact {artifact!r}; "
                             f"choose from {sorted(ARTIFACTS)}")
        if self.assignment is None:
            raise RuntimeError("agent has no identity; configure() first")

        node = self.assignment.node_id
        run_dir = self.data_dir / run_name
        if artifact == "rows":
            matches = sorted(run_dir.glob(f"{node}.*"))
            path = next((p for p in matches
                         if p.suffix in (".bin", ".csv", ".jsonl")), None)
        else:
            path = run_dir / f"{node}{ARTIFACTS[artifact]}"
        if path is None or not path.exists():
            raise FileNotFoundError(
                f"no {artifact} artefact for node {node} in run {run_name!r}")

        blob = path.read_bytes()
        return Response(ok=True, kind="blob", n_bytes=len(blob), name=path.name), blob


def _utc_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z")
