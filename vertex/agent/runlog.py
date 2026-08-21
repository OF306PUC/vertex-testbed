"""The authoritative per-node run log.

Files, per node per run::

    <dir>/<run>/<node_id>.meta.json    written once, before the first sample
    <dir>/<run>/<node_id>.<ext>        appended;  .bin | .csv | .jsonl
    <dir>/<run>/<node_id>.json         written at finalize, columnar, for analysis

``csv`` and ``jsonl`` remain available for cases where being able to ``cat`` the
file matters more than speed. The cost is stated above; nothing else changes.

:meth:`finalize` transposes into the columnar layout the analysis UI already reads::

    { "meta": {...}, "params": {...},
      "data": { "timestamp": [...], "device_timestamp": [...], "state": [...],
                "vstate": [...], "vartheta": [...],
                "<neighbour_id>": [...], "rx_<id>": [...] } }

## Two time columns, and which one to plot against

``timestamp``        This host's clock, seconds since the experiment epoch. Shared
                     across the fleet through chrony, so **this is the timeline to
                     plot against** and the only one on which two nodes' samples
                     are comparable.
``device_timestamp`` The clock of whatever computed the sample.

For a `wifi` or `bridge` agent the controller runs in this process, so the two are
the same number. For a `ble` agent they are not: the law runs on an nRF with no
synchronised clock, and its `t_us` counts from when its own CONTROL frame arrived.
Recording both keeps that distinction instead of picking one and losing it --
``device_timestamp - timestamp`` is the serial transit plus scheduling, which is
otherwise indistinguishable from the board having been late.
"""

from __future__ import annotations

import csv
import json
import os
import struct
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal, Sequence

__all__ = ["SCHEMA_VERSION", "FORMATS", "RunMeta", "RunLog", "read_run_file",
           "recover_rows", "record_width"]

#: Bumped whenever the on-disk layout changes in a way readers must notice.
SCHEMA_VERSION = 5
# v5  rx_<id> became an ARRIVAL flag on every agent type -- "a packet arrived since
#     the last sample". Previously a Pi agent logged a STALENESS flag ("younger
#     than max_neighbor_age_s") while the nRF logged an arrival flag, so the same
#     column meant two things and the two were compared as though they did not.
#     The LAYOUT is identical to v4, which is exactly why this needed a bump: a
#     reader keying on the version is the only way to tell the two apart, and runs
#     collected as v4 are ambiguous between the two meanings.
# v4  added device_timestamp as the second column.

LogFormat = Literal["binary", "csv", "jsonl"]
FORMATS: dict[str, str] = {"binary": ".bin", "csv": ".csv", "jsonl": ".jsonl"}

#: Every column is a float64, including the freshness flags (0.0 / 1.0). 
_ITEM = 8


def record_width(n_neighbors: int) -> int:
    """Columns per record: t, device_t, x, z, theta, then (vstate, fresh) each."""
    return 5 + 2 * n_neighbors


def git_hash(cwd: str | Path | None = None) -> str:
    """Current commit, or ``"unknown"``. Provenance, not control flow."""
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=cwd, timeout=5,
                             capture_output=True, text=True, check=True)
        return out.stdout.strip()
    except Exception:
        return "unknown"


@dataclass
class RunMeta:
    """Everything needed to interpret a run without guessing.
    """

    run_name: str
    node_id: int
    node_type: str
    manifest_name: str = ""
    seed: int = 0
    run_index: int = 0
    dt_s: float = 0.0
    publish_period_s: float = 0.0
    neighbors: list[int] = field(default_factory=list)
    controller: dict[str, Any] = field(default_factory=dict)
    started_at: str = ""
    ended_at: str = ""
    samples: int = 0
    git_hash: str = ""
    schema_version: int = SCHEMA_VERSION
    #: On-disk row format and its record layout. Recorded so a reader never has to
    #: infer either -- a bare binary file is unreadable without them.
    # Which representation the numbers are in. Recorded, never inferred: agents
    # whose law runs on the nRF report scaled int32, agents running it here
    # report engineering units, and both occur in one experiment.
    units: str = "engineering"
    log_format: str = "binary"
    columns: list[str] = field(default_factory=list)
    record_bytes: int = 0
    host: str = ""
    #: Free-form context captured at collection time -- radio channel, power-save
    #: state, chrony offset. These cannot be reconstructed afterwards and are the
    #: usual reason two runs disagree, so there is a place for them here.
    environment: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class RunLog:
    """Append-only log for one node, one run."""

    def __init__(
        self,
        directory: str | Path,
        meta: RunMeta,
        *,
        fmt: LogFormat = "binary",
        flush_every: int = 25,
    ) -> None:
        if flush_every < 1:
            raise ValueError(f"flush_every must be >= 1, got {flush_every}")
        if fmt not in FORMATS:
            raise ValueError(f"unknown format {fmt!r}; choose from {sorted(FORMATS)}")
        self.dir = Path(directory) / meta.run_name
        self.meta = meta
        self.fmt = fmt
        self.flush_every = flush_every
        self.ncols = record_width(len(meta.neighbors))
        self._pack = struct.Struct(f"<{self.ncols}d").pack
        self.meta.log_format = fmt
        self.meta.record_bytes = self.ncols * _ITEM if fmt == "binary" else 0
        self.meta.columns = self.column_names()
        self._fh = None
        self._buf: bytearray | list = bytearray() if fmt == "binary" else []
        self._since_flush = 0
        self._count = 0

    def column_names(self) -> list[str]:
        cols = ["timestamp", "device_timestamp", "state", "vstate", "vartheta"]
        for nid in self.meta.neighbors:
            cols += [str(nid), f"rx_{nid}"]
        return cols

    # paths: ---------------------------------------------------------------------
    @property
    def rows_path(self) -> Path:
        return self.dir / f"{self.meta.node_id}{FORMATS[self.fmt]}"

    @property
    def meta_path(self) -> Path:
        return self.dir / f"{self.meta.node_id}.meta.json"

    @property
    def final_path(self) -> Path:
        return self.dir / f"{self.meta.node_id}.json"

    @property
    def samples(self) -> int:
        return self._count

    # lifecycle:
    def start(self, *, started_at: str = "") -> "RunLog":
        self.dir.mkdir(parents=True, exist_ok=True)
        if started_at:
            self.meta.started_at = started_at
        if not self.meta.git_hash:
            self.meta.git_hash = git_hash()
        if not self.meta.host:
            self.meta.host = os.uname().nodename if hasattr(os, "uname") else ""
        self._write_meta()
        self._fh = self.rows_path.open("wb" if self.fmt == "binary" else "w",
                                       **({} if self.fmt == "binary"
                                          else {"encoding": "utf-8", "newline": ""}))
        if self.fmt == "csv":
            self._fh.write(",".join(self.column_names()) + "\n")
        self._count = 0
        self._since_flush = 0
        return self

    def _write_meta(self) -> None:
        self.meta.samples = self._count
        self.meta_path.write_text(
            json.dumps(self.meta.to_dict(), indent=2), encoding="utf-8"
        )

    def append(
        self,
        t_s: float,
        state: float,
        vstate: float,
        vartheta: float,
        neighbor_vstates: Sequence[float] = (),
        neighbor_fresh: Sequence[bool] = (),
        device_t_s: float | None = None,
    ) -> None:
        """Record one control step.

        The hot path: build the row, pack it into an in-memory buffer, return.

        ``t_s`` is this host's clock and is the timeline to plot against.
        ``device_t_s`` is the computing device's own -- omit it when that device is
        this process, which is every case except a `ble` agent relaying an nRF.

        ``neighbor_fresh`` is 1 when a packet arrived from that neighbour inside its
        freshness window and 0 when the value is a retained stale one.
        """
        if self._fh is None:
            raise RuntimeError("append() before start()")

        row = [t_s, t_s if device_t_s is None else device_t_s,
               state, vstate, vartheta]
        n = len(self.meta.neighbors)
        for i in range(n):
            row.append(float(neighbor_vstates[i]) if i < len(neighbor_vstates) else 0.0)
            row.append(1.0 if i < len(neighbor_fresh) and neighbor_fresh[i] else 0.0)

        if self.fmt == "binary":
            self._buf.extend(self._pack(*row))
        else:
            self._buf.append(row)

        self._count += 1
        self._since_flush += 1
        if self._since_flush >= self.flush_every:
            self.flush()

    def flush(self) -> None:
        """Write the buffer out in one call and clear it."""
        if self._fh is None or not self._buf:
            self._since_flush = 0
            return
        if self.fmt == "binary":
            self._fh.write(self._buf)
            self._buf = bytearray()
        elif self.fmt == "csv":
            csv.writer(self._fh).writerows(self._buf)
            self._buf = []
        else:
            self._fh.write("".join(
                json.dumps(r, separators=(",", ":")) + "\n" for r in self._buf))
            self._buf = []
        self._fh.flush()
        self._since_flush = 0

    def close(self) -> None:
        if self._fh is not None:
            self.flush()
            self._fh.close()
            self._fh = None
        self._write_meta()

    def finalize(self, *, ended_at: str = "", keep_rows: bool = True) -> Path:
        """Close the log and write the columnar file analysis consumes.

        ``keep_rows`` retains the append-only original. 
        """
        if ended_at:
            self.meta.ended_at = ended_at
        self.close()

        rows = recover_rows(self.rows_path, meta=self.meta)
        payload = {
            "meta": self.meta.to_dict(),
            "params": self.meta.controller,
            "data": self._transpose(rows),
        }
        self.final_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        if not keep_rows:
            self.rows_path.unlink(missing_ok=True)
        return self.final_path

    def _transpose(self, rows: list[Sequence[Any]]) -> dict[str, list[Any]]:
        names = self.column_names()
        cols: dict[str, list[Any]] = {n: [] for n in names}
        for row in rows:
            if len(row) < 5:
                continue
            for i, name in enumerate(names):
                if i < len(row):
                    v = row[i]
                    cols[name].append(int(v) if name.startswith("rx_") else v)
                else:
                    cols[name].append(0 if name.startswith("rx_") else None)
        return cols

    # context manager: -----------------------------------------------------------
    def __enter__(self) -> "RunLog":
        return self.start()

    def __exit__(self, exc_type, exc, tb) -> None:
        # Finalise even on an exception: a run that crashed is exactly the run whose
        # data is most worth keeping.
        try:
            self.finalize()
        except Exception:                                   # pragma: no cover
            self.close()

    def __repr__(self) -> str:      # pragma: no cover
        return (f"RunLog(run={self.meta.run_name!r}, node={self.meta.node_id}, "
                f"fmt={self.fmt!r}, samples={self._count})")


def recover_rows(path: str | Path, *, meta: RunMeta | None = None) -> list[list[Any]]:
    """Read a rows file, tolerating a truncated tail
    """
    p = Path(path)
    if not p.exists():
        return []

    if meta is None:
        sidecar = p.with_suffix("").with_suffix(".meta.json")
        if not sidecar.exists():
            sidecar = p.parent / f"{p.stem}.meta.json"
        if sidecar.exists():
            raw = json.loads(sidecar.read_text(encoding="utf-8"))
            meta = RunMeta(**{k: v for k, v in raw.items()
                              if k in RunMeta.__dataclass_fields__})

    suffix = p.suffix
    if suffix == ".bin":
        if meta is None or not meta.record_bytes:
            raise ValueError(f"cannot read {p}: no record layout (missing .meta.json)")
        blob = p.read_bytes()
        usable = len(blob) - (len(blob) % meta.record_bytes)
        ncols = meta.record_bytes // _ITEM
        unpack = struct.Struct(f"<{ncols}d").unpack_from
        return [list(unpack(blob, off)) for off in range(0, usable, meta.record_bytes)]

    out: list[list[Any]] = []
    with p.open(encoding="utf-8") as fh:
        if suffix == ".csv":
            for i, row in enumerate(csv.reader(fh)):
                if i == 0 and row and row[0] == "timestamp":
                    continue
                try:
                    out.append([float(x) for x in row])
                except ValueError:
                    continue        # truncated tail
        else:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, list):
                    out.append(row)
    return out


def read_run_file(path: str | Path) -> dict[str, Any]:
    """Load a finalised run file."""
    return json.loads(Path(path).read_text(encoding="utf-8"))
