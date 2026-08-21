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
from ..net import (CONTROL_PORTS, STATE_PORT, AgentType, InterfaceError,
                   broadcast_address, interface_broadcast, interface_for_ip,
                   interface_prefixlen)
from ..transports.base import Transport
from ..transports.ble import BleTransport
from ..transports.multi import MultiTransport
from ..transports.udp import UdpTransport
from .agent import Agent, AgentConfig
from .assignment import AgentAssignment
from .relay import BleRelay, radio_frame
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
        interface: str | None = None,
        control_host: str = "0.0.0.0",
        control_port: int | None = None,
        state_port: int = STATE_PORT,
        log_format: str = "binary",
        clock: Clock | None = None,
        transport_factory: Callable[[int, Clock], Transport] | None = None,
        environment: dict[str, Any] | None = None,
        link: Any = None,
        radio: dict[str, Any] | None = None,
    ) -> None:
        self.node_type = AgentType(node_type)
        self.data_dir = Path(data_dir)
        self.host_ip = host_ip
        #: Interface name, used to ask the KERNEL for the broadcast address rather
        #: than deriving one from an assumed prefix. A /24 guess on the lab's /22
        #: network sends every datagram to an ordinary host address, and the
        #: failure is completely silent -- 0/3 UDP links delivered while 3/3 BLE
        #: links worked.
        self.interface = interface
        self.state_port = state_port
        self.log_format = log_format
        self.clock = clock or WallClock(time.time())
        self.environment = environment or {}
        self._transport_factory = transport_factory
        #: Framed serial link to the nRF. Required for `ble`, unused otherwise.
        self.link = link
        #: Radio parameters. Loopback B measured a 6x swing in delivery from the
        #: scan window alone, so these belong with the run, not with the build.
        self.radio = radio

        self.control = ControlServer(
            self._handlers(), host=control_host,
            port=control_port if control_port is not None
            else CONTROL_PORTS[self.node_type],
        )

        self.assignment: AgentAssignment | None = None
        self.agent: Agent | None = None
        self.relay: BleRelay | None = None
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
        # A relay has no local loops, so task count cannot stand in for it.
        return bool(self._tasks) or (self.is_relay and self.run_name is not None)

    # building the agent:
    def _ble_transport(self, node_id: int) -> BleTransport:
        r = self._radio_settings()
        return BleTransport(
            node_id, self.clock,
            adv_interval_ms=float(r.get("adv_interval_ms", 100.0)),
            scan_interval_ms=float(r.get("scan_interval_ms", 100.0)),
            scan_window_ms=float(r.get("scan_window_ms", 100.0)),
            channel_map=int(r.get("channel_map", 0x07)),
            passive_scan=bool(r.get("passive_scan", True)))

    def _broadcast_target(self) -> tuple[str, str]:
        """(address, how it was determined). Never guesses silently.

        Derives the interface from `host_ip` when it was not supplied, rather than
        dropping to the /24 assumption. The caller-must-pass version was forgotten
        once and cost a whole run: the fallback works, so nothing complains, and
        every datagram goes to an address nobody holds.
        """
        iface = self.interface or (interface_for_ip(self.host_ip)
                                   if self.host_ip else None)
        if iface:
            try:
                return interface_broadcast(iface), f"kernel/{iface}"
            except InterfaceError:
                pass
        if self.host_ip:
            # Fallback with the assumption named, so a wrong prefix is at least
            # visible in the recorded environment rather than only in the delivery
            # ratio.
            return broadcast_address(self.host_ip), "assumed /24"
        return "255.255.255.255", "limited broadcast"

    def _udp_transport(self, node_id: int) -> UdpTransport:
        target, how = self._broadcast_target()
        self.environment.setdefault("udp_broadcast", target)
        self.environment.setdefault("udp_broadcast_source", how)
        iface = self.interface or (interface_for_ip(self.host_ip)
                                   if self.host_ip else None)
        if iface:
            self.environment.setdefault("interface_used", iface)
            self.environment.setdefault("prefixlen", interface_prefixlen(iface))
        return UdpTransport(node_id, self.clock,
                            send_to=(target, self.state_port),
                            bind_port=self.state_port, broadcast=True)

    def _make_transport(self, node_id: int) -> Transport:
        """The transport for a locally-computing agent.

        `wifi` gets UDP. `bridge` gets **both** BLE and UDP, which is what makes it
        a bridge: a `ble` agent has only a radio and a `wifi` agent only a socket,
        so they share no medium and cannot hear each other at all. The bridge is
        the only path between the two subnets, and a manifest that gives it
        neighbours on both -- `n30-clusters` does -- is unrunnable without it.

        The comparison this supports: `bridge` and `wifi` run the *same* controller
        in the same process, so a difference between them is the medium and not the
        implementation. A bridge is not airtime-comparable with either, though,
        because it transmits every packet twice; see transports/multi.py.
        """
        if self._transport_factory is not None:
            return self._transport_factory(node_id, self.clock)
        if self.node_type is AgentType.BRIDGE:
            return MultiTransport([self._ble_transport(node_id),
                                   self._udp_transport(node_id)])
        return self._udp_transport(node_id)

    @property
    def is_relay(self) -> bool:
        """True for `ble`: the control law runs on the nRF, not here.

        The distinction is not cosmetic. A relay has no local controller and no
        Transport.
        """
        return self.node_type is AgentType.BLE

    def _build_relay(self, assignment: AgentAssignment) -> BleRelay:
        if self.link is None:
            raise RuntimeError(
                f"a {self.node_type} agent relays to an nRF and needs a serial "
                "link; construct AgentService with link=...")
        # The link stamps arrivals, so it needs the clock the log is written on.
        if getattr(self.link, "clock", None) is None:
            try:
                self.link.clock = self.clock
            except Exception:
                pass
        relay = BleRelay(self.link, on_report=self._record_report,
                         clock=self.clock)
        relay.configure(assignment, radio=self._radio_frame(assignment))
        return relay

    def _radio_settings(self, assignment: AgentAssignment | None = None) -> dict[str, Any]:
        """Radio parameters in force: the constructor override, else the manifest.
        """
        if self.radio is not None:
            return dict(self.radio)
        a = assignment if assignment is not None else self.assignment
        return dict(a.radio) if a is not None and a.radio else {}

    def _radio_meta(self, assignment: AgentAssignment) -> dict[str, Any]:
        """The radio block for ``RunMeta.environment``.
        """
        r = self._radio_settings(assignment)
        if not r:
            return {}
        base = assignment.radio_environment() if assignment.radio else dict(r)
        if self.radio is not None:
            # Overridden at launch, so the manifest's block would misdescribe the
            # run. Recompute from what is actually in force.
            from ..radio.hci import ms_to_units
            base = dict(r)
            for key in ("adv_interval_ms", "scan_interval_ms", "scan_window_ms"):
                if key in r:
                    base[key.replace("_ms", "_units")] = ms_to_units(float(r[key]))
            if r.get("scan_interval_ms"):
                base["scan_duty_cycle"] = (float(r["scan_window_ms"])
                                           / float(r["scan_interval_ms"]))
            base["radio_source"] = "launch-override"
        else:
            base["radio_source"] = "manifest"
        base["channel_map_applied"] = self.node_type is AgentType.BRIDGE
        return {"radio": base}

    def _radio_frame(self, assignment: AgentAssignment):
        r = self._radio_settings(assignment)
        if not r:
            return None
        # channel_map is deliberately not forwarded: the RADIO frame has no field
        # for it because Zephyr's advertising API does not expose the advertising
        # channel map. It is applied for `bridge`, which drives HCI directly, and
        # recorded as unapplied for `ble` -- see radio_environment().
        return radio_frame(
            adv_interval_ms=float(r.get("adv_interval_ms", 100.0)),
            scan_interval_ms=float(r.get("scan_interval_ms", 100.0)),
            scan_window_ms=float(r.get("scan_window_ms", 100.0)),
            active_scan=not bool(r.get("passive_scan", True)))

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
        if self.is_relay and self.relay is not None:
            data.update(self.relay.status())
            data["node_type"] = str(self.node_type)
            return ok(**data), None

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
            # Transport counters. They exist on every transport and were reachable
            # from nowhere -- so when both bridges' BLE transmit path collapsed
            # 80-100 s into a run, the numbers that would have named the cause
            # (send_errors, send_timeouts, per-medium failures) were unobtainable
            # during the run and unrecorded after it.
            t = self.agent.transport
            st = getattr(t, "stats", None)
            if st is not None:
                data["transport"] = {"name": t.name, "stats": st.summary()}
            if hasattr(t, "member_stats"):
                data.setdefault("transport", {})["media"] = t.member_stats()

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

        if self.is_relay:
            if self.running and self.relay is not None:
                self.relay.configure(assignment,
                                     radio=self._radio_frame(assignment))
                self.assignment = assignment
                return ok(node_id=assignment.node_id, live_update=True,
                          mode="relay"), None
            self.assignment = assignment
            self.relay = self._build_relay(assignment)
            return ok(node_id=assignment.node_id, live_update=False,
                      mode="relay"), None

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
        if self.assignment is None or (self.agent is None and self.relay is None):
            raise RuntimeError("configure() before start()")
        if self.running:
            raise RuntimeError(f"already running {self.run_name!r}")

        run_name = str(args.get("run_name") or "run")

        # The experiment epoch is a PER-RUN quantity and the hub owns it: every
        # node must be handed the same value or their timestamps share no origin
        # and one-way delay measures process launch order. It arrives with the
        # trigger for the same reason the nRF's seed does -- the same
        # configuration replayed on a new epoch is a new run.
        #
        # Applied before the RunLog is built, so `tx_time_us` on the first packet
        # is already on the run's epoch, and before relay.start(), which reads the
        # clock to fill the nRF's CONTROL frame.
        epoch = args.get("epoch_unix_s")
        if epoch is not None:
            self.clock = WallClock(float(epoch))
            self._rebind_clock()

        a = self.assignment
        self.runlog = RunLog(
            self.data_dir,
            RunMeta(
                run_name=run_name, node_id=a.node_id, node_type=str(a.node_type),
                manifest_name=a.manifest_name, seed=a.seed, run_index=a.run_index,
                dt_s=a.dt_s, publish_period_s=a.publish_period_s,
                neighbors=list(a.neighbors),
                controller=a.model_dump(),
                # A relay logs what the nRF reported: scaled int32, unconverted.
                # `vertex.analysis.units` normalises on read.
                units="scaled_int" if self.is_relay else "engineering",
                environment={**self.environment, **self._radio_meta(a),
                             **_interpreter_provenance(),
                             "epoch_unix_s": getattr(self.clock, "epoch_unix_s", None)},
            ),
            fmt=self.log_format,
        ).start(started_at=_utc_now())

        if self.is_relay:
            # No loops here: the nRF owns the timing and reports at its own
            # `clock`. This process only writes what arrives.
            assert self.relay is not None
            self.relay.start()
            self.run_name = run_name
            self._started_at = time.time()
            self._tasks = []
            return ok(run_name=run_name, node_id=a.node_id, mode="relay"), None

        assert self.agent is not None
        self.agent.on_sample = self._record
        await self.agent.start()
        self.run_name = run_name
        self._started_at = time.time()
        self._tasks = [
            asyncio.create_task(self.agent.run_control_loop()),
            asyncio.create_task(self.agent.run_publish_loop()),
        ]
        return ok(run_name=run_name, node_id=a.node_id, mode="local"), None

    def _record(self, t_s: float, out, vstates, fresh) -> None:
        if self.runlog is not None:
            self.runlog.append(t_s, out.state, out.vstate, out.vartheta, vstates, fresh)

    def _record_report(self, report, rx_time_us: int | None = None) -> None:
        """Write one nRF report. State values pass through unscaled.

        Two timelines, and the row's primary one is **this host's**. The nRF has no
        synchronised clock, so its `t_us` counts from its own CONTROL arrival and is
        not comparable with another node's. This host's clock is chrony-synchronised
        and epoch-shared, so plotting against the arrival time puts a `ble` agent's
        samples on the same axis as a `wifi` agent's without touching firmware.

        The board's own reading is kept alongside as `device_timestamp`: their
        difference is the serial transit plus scheduling, which is otherwise
        indistinguishable from the board having been late.

        With no arrival time the board's clock is used for both -- better than
        fabricating a host time.
        """
        if self.runlog is None:
            return
        device_t_s = report.t_us / 1e6
        t_s = device_t_s if rx_time_us is None else rx_time_us / 1e6
        self.runlog.append(t_s, report.state, report.vstate, report.vartheta,
                           list(report.neighbor_vstates),
                           list(report.neighbor_fresh),
                           device_t_s=device_t_s)

    def _rebind_clock(self) -> None:
        """Push the run's clock into everything that cached a reference.

        Objects are built at `configure` and the epoch arrives at `start`, so every
        holder of a clock is holding the launch-time one until this runs. Doing it
        for some and not others is worse than not doing it at all: the relay's link
        was fixed in isolation and the local agents were not, so `tx_time_us` and
        `rx_time_us` ended up on two different origins and the one-way delays came
        out at -10.5 s, +10.8 s and +20.9 s -- each one the difference between two
        agents' start times, wearing the units of a delay.

        Enumerated rather than patched case by case, so a new clock holder is one
        line here instead of another wrong delay figure.
        """
        holders: list[Any] = [self.relay, self.link, self.agent]
        if self.agent is not None:
            t = self.agent.transport
            holders.append(t)
            holders.extend(getattr(t, "members", []) or [])
        for h in holders:
            if h is not None and hasattr(h, "clock"):
                try:
                    h.clock = self.clock
                except Exception:                               # pragma: no cover
                    pass

    def _transport_meta(self) -> dict[str, Any]:
        """Transport counters, for the run's metadata at stop.

        Recorded because a link that works and then stops is indistinguishable, in
        the rows alone, from a link that was never there -- and the difference is
        entirely in these counters.
        """
        if self.agent is None or self.agent.transport is None:
            return {}
        t = self.agent.transport
        out: dict[str, Any] = {"transport": t.name}

        # Per-link delivery from SEQUENCE NUMBERS -- the only per-link figure that
        # is comparable across media. `expected` is inferred from seq gaps, so it
        # counts published values rather than transmissions: a BLE link that
        # re-advertises each value twice scores a duplicate, not two deliveries,
        # and a receiver sampling faster than the sender publishes cannot undercount
        # it. The freshness column in the rows is a proxy for this and is affected
        # by both -- it read 0.14 on a UDP link the counters show at 98%.
        from dataclasses import asdict
        out["links"] = {
            str(nid): {k: v for k, v in asdict(st).items() if k != "delays_us"}
            | {"delivery_ratio": round(st.delivery_ratio, 4),
               "median_delay_us": st.median_delay_us}
            for nid, st in self.agent.neighbors.link_stats().items()
        }
        st = getattr(t, "stats", None)
        if st is not None:
            from dataclasses import asdict, is_dataclass
            out["transport_stats"] = asdict(st) if is_dataclass(st) else repr(st)
        if hasattr(t, "member_stats"):
            out["transport_media"] = t.member_stats()
        return out

    async def _stop(self, args: dict[str, Any]) -> tuple[Response, None]:
        if self.is_relay:
            if self.run_name is None:
                return ok(run_name=None, samples=0, was_running=False), None
            try:
                assert self.relay is not None
                self.relay.stop()
            except Exception:
                pass                    # log the samples regardless
            samples = 0
            if self.runlog is not None:
                self.runlog.finalize(ended_at=_utc_now())
                samples = self.runlog.samples
            run_name, self.run_name = self.run_name, None
            return ok(run_name=run_name, samples=samples, was_running=True,
                      duration_s=round(time.time() - self._started_at, 3)), None

        if not self.running:
            return ok(run_name=self.run_name, samples=
                      self.runlog.samples if self.runlog else 0, was_running=False), None
        # Read BEFORE _halt(): it stops the transport, and the counters go with it.
        tmeta = self._transport_meta()
        await self._halt()
        samples = 0
        if self.runlog is not None:
            if tmeta:
                self.runlog.meta.environment.update(tmeta)
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


def _interpreter_provenance() -> dict[str, Any]:
    """What is running this agent, recorded with its data.

    Not pedantry. Two Pis in one experiment turned out to be on 3.11.2 (the distro
    package) and 3.11.8 (built from source, and missing `_bz2` because libbz2-dev
    was absent at build time). That is a difference between hosts in an experiment
    whose whole purpose is comparing hosts, and it is not reconstructable from the
    data afterwards -- which is exactly what `environment` is for.
    """
    import platform
    import sys as _sys
    return {
        "python": platform.python_version(),
        "python_build": " ".join(platform.python_build()),
        "python_impl": platform.python_implementation(),
        "executable": _sys.executable,
        "platform": platform.platform(),
    }


def _utc_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z")
