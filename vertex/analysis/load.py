"""Read a collected run. The piece between `runs/` on disk and any analysis.

Until now reading a run meant calling `recover_rows` with a hand-built `RunMeta`
and knowing which columns were which -- every plot and every metric re-derived
that. This is the one place that knows the layout.

Two things it does that a caller should not have to:

**Normalises units.** A `ble` agent logs scaled int32 because its law runs on the
nRF; `wifi` and `bridge` log engineering units. Both appear in one run, so anything
comparing them has to convert first. `normalize_run` is reused rather than
reimplemented, so there is one rounding convention.

**Keeps both timelines apart.** `timestamp` is this host's clock and is what to
plot against; `device_timestamp` is whatever computed the sample. They are the same
number for a locally-computing agent and differ by the serial transit for a relay.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..agent.runlog import FORMATS, RunMeta, recover_rows
from .units import ENGINEERING, normalize_run

__all__ = ["NodeRun", "Run", "load_run", "load_node"]


@dataclass
class NodeRun:
    """One node's rows, columns by name, in engineering units."""

    node_id: int
    node_type: str
    meta: dict[str, Any]
    data: dict[str, list[float]]

    @property
    def t(self) -> list[float]:
        """Host clock, seconds from the run's epoch. The axis to plot against."""
        return self.data["timestamp"]

    @property
    def device_t(self) -> list[float]:
        return self.data.get("device_timestamp", self.data["timestamp"])

    @property
    def state(self) -> list[float]:
        return self.data["state"]

    @property
    def vstate(self) -> list[float]:
        return self.data["vstate"]

    @property
    def vartheta(self) -> list[float]:
        return self.data["vartheta"]

    @property
    def sigma(self) -> list[float]:
        """x - z, the correction still outstanding on this node."""
        return [x - z for x, z in zip(self.state, self.vstate)]

    @property
    def neighbours(self) -> list[int]:
        return [int(c) for c in self.data if c.isdigit()]

    def neighbour_vstate(self, nid: int) -> list[float]:
        return self.data[str(nid)]

    def neighbour_fresh(self, nid: int) -> list[float]:
        """1.0 where a packet arrived from that neighbour in the window, else 0."""
        return self.data[f"rx_{nid}"]

    def freshness(self, nid: int) -> float:
        f = self.neighbour_fresh(nid)
        return sum(f) / len(f) if f else 0.0

    @property
    def rate_hz(self) -> float:
        span = self.t[-1] - self.t[0] if len(self.t) > 1 else 0.0
        return (len(self.t) - 1) / span if span else 0.0

    @property
    def clock_offset_s(self) -> list[float]:
        """device_timestamp - timestamp: the link, for a relay. Zero otherwise."""
        return [d - h for d, h in zip(self.device_t, self.t)]


@dataclass
class Run:
    name: str
    manifest: str
    epoch_unix_s: float
    duration_s: float
    nodes: dict[int, NodeRun] = field(default_factory=dict)
    report: dict[str, Any] = field(default_factory=dict)

    @property
    def ids(self) -> list[int]:
        return sorted(self.nodes)

    def by_type(self, node_type: str) -> list[NodeRun]:
        return [n for n in self.nodes.values() if n.node_type == node_type]

    def spread(self, at_s: float) -> float:
        """max - min of `vstate` across nodes, sampled at `at_s`.

        Nearest sample per node rather than interpolation: agents log at different
        rates -- a relay reports at its own period -- and interpolating would
        invent values between them.
        """
        vals = [self.vstate_at(n, at_s) for n in self.nodes.values()]
        vals = [v for v in vals if v is not None]
        return (max(vals) - min(vals)) if vals else 0.0

    @staticmethod
    def vstate_at(node: NodeRun, at_s: float) -> float | None:
        if not node.t:
            return None
        i = min(range(len(node.t)), key=lambda k: abs(node.t[k] - at_s))
        return node.vstate[i]

    def links(self) -> list[tuple[int, int, float]]:
        """(source, receiver, freshness) for every declared link."""
        out = []
        for nid, node in sorted(self.nodes.items()):
            for src in node.neighbours:
                out.append((src, nid, node.freshness(src)))
        return out


def load_node(run_dir: Path, node_id: int) -> NodeRun:
    """One node, units normalised to engineering."""
    meta_path = run_dir / f"{node_id}.meta.json"
    raw = json.loads(meta_path.read_text(encoding="utf-8"))
    meta = RunMeta(**{k: v for k, v in raw.items()
                      if k in RunMeta.__dataclass_fields__})

    rows_path = next(
        (p for p in (run_dir / f"{node_id}{ext}" for ext in FORMATS.values())
         if p.exists()), None)
    if rows_path is None:
        raise FileNotFoundError(
            f"no rows file for node {node_id} in {run_dir}; looked for "
            f"{sorted(FORMATS.values())}")

    rows = recover_rows(rows_path, meta=meta)
    names = raw["columns"]
    data = {n: [] for n in names}
    for row in rows:
        for i, n in enumerate(names):
            if i < len(row):
                data[n].append(row[i])

    # Reuse the tested converter rather than dividing by 1e6 here: a relay logs
    # scaled int32 and a local agent engineering units, and both appear in one run.
    payload = normalize_run({"meta": raw, "data": data}, to=ENGINEERING)
    return NodeRun(node_id=node_id, node_type=raw["node_type"],
                   meta=payload["meta"], data=payload["data"])


def load_run(run_dir: str | Path) -> Run:
    """Every node in a collected run."""
    d = Path(run_dir)
    if not d.is_dir():
        raise FileNotFoundError(f"{d} is not a directory")

    report: dict[str, Any] = {}
    rj = d / "run.json"
    if rj.exists():
        report = json.loads(rj.read_text(encoding="utf-8"))

    ids = sorted(int(p.stem.split(".")[0])
                 for p in d.glob("*.meta.json"))
    nodes = {i: load_node(d, i) for i in ids}
    first = next(iter(nodes.values()), None)
    return Run(
        name=report.get("run_name", d.name),
        manifest=report.get("manifest",
                            first.meta.get("manifest_name", "") if first else ""),
        epoch_unix_s=report.get("epoch_unix_s", 0.0),
        duration_s=report.get("duration_s", 0.0),
        nodes=nodes, report=report,
    )
