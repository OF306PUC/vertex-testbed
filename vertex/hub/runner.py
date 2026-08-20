"""Drive one run across every node in a manifest, then collect the results.

The pieces this assembles all existed and had no caller: `assignments_for()`
produces exactly the `configure` payload keyed by node id, `CONTROL_PORTS` turns a
host and a type into an endpoint, and `ControlClient` speaks to one agent. What was
missing is the fan-out.

## Why the phases are ordered the way they are

**Connect and configure everything before triggering anything.** A run where node 7
was configured and node 8 was not is not a shorter run, it is a different
experiment. So configuration is a barrier: if any node refuses, nothing starts.

**One epoch for the whole fleet.** Timestamps are only comparable on a shared
origin, so the epoch is decided here, once, and every node is told the same value.
An agent left to default to its own start time yields per-node origins and a
one-way delay that measures process launch order.

**Trigger as tightly as possible, and record the spread.** The agents cannot be
started simultaneously, so `start` is issued concurrently and the observed spread
is recorded with the run. It bounds how much of any early transient is real.

**Stop before fetching.** Fetching pulls a file the agent may still be appending
to. Stop first, then read.

**Collect even from a failed run.** A run that half-failed is still evidence, and
the partial logs are usually what explain the failure. So collection runs whatever
happened, and the report says what was missing.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from ..agent.assignment import AgentAssignment, assignments_for
from ..control import ControlClient, ControlError
from ..net import CONTROL_PORTS, AgentType
from ..topology import ExperimentManifest

__all__ = ["NodeOutcome", "RunReport", "ExperimentRunner"]

#: Artefacts pulled from each agent. `meta` first: without it the rows are
#: unreadable, so a truncated collection should still leave something interpretable.
ARTIFACTS = ("meta", "rows")


@dataclass
class NodeOutcome:
    node_id: int
    node_type: str
    address: str
    configured: bool = False
    started: bool = False
    stopped: bool = False
    samples: int = 0
    started_at: float | None = None      # hub-side monotonic, for the spread
    files: dict[str, str] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.configured and self.started and not self.errors


@dataclass
class RunReport:
    run_name: str
    manifest: str
    epoch_unix_s: float
    duration_s: float
    nodes: dict[int, NodeOutcome] = field(default_factory=dict)
    trigger_spread_s: float = 0.0
    out_dir: Path | None = None

    @property
    def ok(self) -> bool:
        return bool(self.nodes) and all(n.ok for n in self.nodes.values())

    def summary(self) -> str:
        good = sum(1 for n in self.nodes.values() if n.ok)
        samples = sum(n.samples for n in self.nodes.values())
        lines = [
            f"run {self.run_name} ({self.manifest}): {good}/{len(self.nodes)} nodes ok, "
            f"{samples} samples, {self.duration_s:g}s, "
            f"trigger spread {self.trigger_spread_s * 1000:.0f} ms",
        ]
        for nid, n in sorted(self.nodes.items()):
            flag = "ok  " if n.ok else "FAIL"
            lines.append(f"  {flag} {nid:>3} {n.node_type:<7} {n.address:<21} "
                         f"samples={n.samples} files={len(n.files)}"
                         + (f" -- {n.errors[0]}" if n.errors else ""))
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_name": self.run_name,
            "manifest": self.manifest,
            "epoch_unix_s": self.epoch_unix_s,
            "duration_s": self.duration_s,
            "trigger_spread_s": self.trigger_spread_s,
            "ok": self.ok,
            "nodes": {str(k): {
                "node_id": v.node_id, "node_type": v.node_type,
                "address": v.address, "configured": v.configured,
                "started": v.started, "stopped": v.stopped,
                "samples": v.samples, "files": v.files, "errors": v.errors,
            } for k, v in sorted(self.nodes.items())},
        }


class ExperimentRunner:
    """One manifest, one run, across every node in it.

    Parameters
    ----------
    manifest:
        Validate it with `topology.check` before handing it over -- this class
        does not, because a caller may deliberately run a control case that the
        validator warns about (the open directed line, for instance).
    out_dir:
        Where collected artefacts land, under `<out_dir>/<run_name>/`.
    timeout:
        Per-command control-plane timeout. Not the run duration.
    """

    def __init__(self, manifest: ExperimentManifest, *,
                 out_dir: Path | str = "runs", timeout: float = 10.0,
                 run_index: int = 0) -> None:
        self.manifest = manifest
        self.out_dir = Path(out_dir)
        self.timeout = timeout
        self.run_index = run_index
        self.assignments: dict[int, AgentAssignment] = assignments_for(
            manifest, run_index)
        self._clients: dict[int, ControlClient] = {}

    # ── endpoints ────────────────────────────────────────────────────────────
    def endpoint(self, node_id: int) -> tuple[str, int]:
        node = self.manifest.by_id[node_id]
        return node.ip, CONTROL_PORTS[AgentType(node.type)]

    def _client(self, node_id: int) -> ControlClient:
        if node_id not in self._clients:
            host, port = self.endpoint(node_id)
            self._clients[node_id] = ControlClient(
                host, port, timeout=self.timeout, node_id=node_id)
        return self._clients[node_id]

    async def close(self) -> None:
        await asyncio.gather(*(c.close() for c in self._clients.values()),
                             return_exceptions=True)
        self._clients.clear()

    # ── phases ───────────────────────────────────────────────────────────────
    async def _configure_one(self, node_id: int, out: NodeOutcome) -> None:
        payload = self.assignments[node_id].model_dump(mode="json")
        try:
            await self._client(node_id).configure(**payload)
            out.configured = True
        except ControlError as exc:
            out.errors.append(f"configure: {exc}")

    async def _start_one(self, node_id: int, run_name: str, out: NodeOutcome,
                         epoch_unix_s: float) -> None:
        try:
            # The epoch travels with the trigger: it is per-run, and every node
            # gets the same value or their timestamps share no origin.
            await self._client(node_id).start(run_name, epoch_unix_s=epoch_unix_s)
            out.started = True
            out.started_at = time.monotonic()
        except ControlError as exc:
            out.errors.append(f"start: {exc}")

    async def _stop_one(self, node_id: int, out: NodeOutcome) -> None:
        try:
            data = await self._client(node_id).stop()
            out.stopped = True
            out.samples = int(data.get("samples", 0) or 0)
        except ControlError as exc:
            out.errors.append(f"stop: {exc}")

    async def _collect_one(self, node_id: int, run_name: str, out: NodeOutcome,
                           run_dir: Path) -> None:
        for artifact in ARTIFACTS:
            try:
                blob = await self._client(node_id).fetch(run_name, artifact)
            except ControlError as exc:
                # Not fatal. A node with no rows still has metadata worth keeping,
                # and the absence is itself the finding.
                out.errors.append(f"fetch {artifact}: {exc}")
                continue
            suffix = ".meta.json" if artifact == "meta" else ".rows"
            path = run_dir / f"{node_id}{suffix}"
            path.write_bytes(blob)
            out.files[artifact] = path.name

    # ── the run ──────────────────────────────────────────────────────────────
    async def run(self, run_name: str, duration_s: float, *,
                  epoch_unix_s: float | None = None,
                  only: Iterable[int] | None = None,
                  settle_s: float = 0.0) -> RunReport:
        """Configure, trigger, wait, stop, collect.

        `settle_s` delays the trigger after configuration, for radios that need a
        moment after being reconfigured. `only` restricts the run to a subset of
        node ids, for bringing one host up at a time.
        """
        ids = sorted(only) if only is not None else sorted(self.assignments)
        unknown = [i for i in ids if i not in self.assignments]
        if unknown:
            raise ValueError(f"node ids not in the manifest: {unknown}")

        epoch = time.time() if epoch_unix_s is None else float(epoch_unix_s)
        report = RunReport(run_name=run_name, manifest=self.manifest.name,
                           epoch_unix_s=epoch, duration_s=duration_s)
        for nid in ids:
            node = self.manifest.by_id[nid]
            host, port = self.endpoint(nid)
            report.nodes[nid] = NodeOutcome(
                node_id=nid, node_type=str(node.type), address=f"{host}:{port}")

        # 1. configure -- a barrier. A partially configured fleet is a different
        #    experiment, not a shorter one.
        await asyncio.gather(*(self._configure_one(i, report.nodes[i])
                               for i in ids))
        if not all(report.nodes[i].configured for i in ids):
            bad = [i for i in ids if not report.nodes[i].configured]
            for i in ids:
                if report.nodes[i].configured:
                    report.nodes[i].errors.append(
                        f"not started: nodes {bad} failed to configure")
            return report

        if settle_s > 0:
            await asyncio.sleep(settle_s)

        # 2. trigger, as tightly as the control plane allows, and record how
        #    tightly it actually managed.
        await asyncio.gather(*(self._start_one(i, run_name, report.nodes[i], epoch)
                               for i in ids))
        stamps = [n.started_at for n in report.nodes.values() if n.started_at]
        report.trigger_spread_s = (max(stamps) - min(stamps)) if len(stamps) > 1 else 0.0

        # 3. wait out the run
        await asyncio.sleep(duration_s)

        # 4. stop before fetching: a fetch mid-run reads a file still being
        #    appended to.
        await asyncio.gather(*(self._stop_one(i, report.nodes[i]) for i in ids))

        # 5. collect regardless of what failed -- the partial logs are usually
        #    what explain the failure.
        run_dir = self.out_dir / run_name
        run_dir.mkdir(parents=True, exist_ok=True)
        report.out_dir = run_dir
        await asyncio.gather(*(self._collect_one(i, run_name, report.nodes[i], run_dir)
                               for i in ids))

        (run_dir / "run.json").write_text(
            json.dumps(report.to_dict(), indent=2), encoding="utf-8")
        return report

    async def status(self, only: Iterable[int] | None = None) -> dict[int, Any]:
        """Poll every agent. For checking the fleet before committing to a run."""
        ids = sorted(only) if only is not None else sorted(self.assignments)

        async def one(nid: int):
            try:
                return await self._client(nid).status()
            except ControlError as exc:
                return {"error": str(exc)}

        results = await asyncio.gather(*(one(i) for i in ids))
        return dict(zip(ids, results))
