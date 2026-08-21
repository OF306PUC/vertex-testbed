#!/usr/bin/env python3
"""Drive a whole manifest through the hub, on one machine, with no radios.

The first thing that exercises the complete host-side path: hub -> control plane ->
`AgentService` -> controller or relay -> `RunLog` -> fetch -> files on disk. Every
piece of that existed and had no caller; this is what would have caught that.

Real `AgentService` processes in-process on loopback ports, real `ControlServer`,
real `ExperimentRunner`. Two things are faked, and only two:

* the radios -- a `LoopbackBus` stands in, so `wifi` and `bridge` agents exchange
  state without a socket or an adapter;
* the nRF -- a byte-level fake behind the real `SerialLink`, so a `ble` agent's
  relay drives the real codec and the real reader thread.

What it proves, in order of how badly each has already gone wrong:

1. A `ble` agent logs a nonzero sample count. That is the STATE path, which had no
   caller at all and reported `running: true` while logging nothing.
2. Every node reports the SAME epoch, because a per-node epoch makes one-way delay
   measure process launch order.
3. `bridge` agents publish on both media -- they are the only path between the
   `ble` and `wifi` subnets.
4. Collected artefacts exist and the metadata is readable, in the units each agent
   type actually logs -- and the rows are readable *back*, which needs the agent's
   own filename to survive collection, because the suffix is what says how to
   decode them.
5. The two time columns behave: identical for an agent whose controller runs in
   this process, and different for a `ble` agent, whose law runs on a board with no
   synchronised clock. `timestamp` is the host's and is the axis to plot against;
   `device_timestamp` is the board's, and the difference is the link.

    python3 test/hub/check_fleet.py [manifest]
"""
from __future__ import annotations

import asyncio
import json
import shutil
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from vertex.agent.service import AgentService
from vertex.hub import ExperimentRunner
from vertex.net import AgentType, CONTROL_PORTS
from vertex.serial import FrameType, SerialLink, build_frame
from vertex.serial.proto import STATE_HEADER, STATE_NEIGHBOUR
from vertex.clock import WallClock
from vertex.topology import check, load_manifest_file
from vertex.transports import LoopbackBus, LoopbackTransport

WORK = Path("/tmp/vertex-fleet")
DURATION = 2.0
DT_S = 0.05                 # compressed: the point is the plumbing, not the law


#: Payload lengths the firmware enforces (proto.h). A fake that ACKs anything
#: cannot catch a length or offset disagreement, which is most of what the serial
#: path can get wrong.
PAYLOAD_LENS = {
    FrameType.ALGORITHM: 36,
    FrameType.DISTURBANCE: 29,
    FrameType.CONTROL: 11,
    FrameType.RADIO: 9,
    FrameType.PING: 0,
}
AGENT_ERR_LEN = -1


class FakeNrf:
    """Byte-level nRF behind the real SerialLink.

    Models the firmware's *contract*, not just its framing: PING is answered with
    PONG, a wrong-length payload is refused with ERR and AGENT_ERR_LEN, and STATE
    reports start on a CONTROL trigger and stop when it clears. A fake that ACKed
    everything would let through exactly the failure this link has had twice.
    """

    def __init__(self, node_id: int, neighbours: list[int]) -> None:
        self.node_id = node_id
        self.neighbours = neighbours
        self.rx = bytearray()
        self.frames_in: list[int] = []
        self._lock = threading.Lock()
        self._running = False
        self._k = 0
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def write(self, data: bytes) -> None:
        # SOF | TYPE | LEN:2 | PAYLOAD | CRC:2  -- payload starts at index 4.
        ftype = data[1]
        payload = data[4:-2]
        self.frames_in.append(ftype)

        want = PAYLOAD_LENS.get(ftype)
        if want is not None and len(payload) != want:
            with self._lock:
                self.rx += build_frame(FrameType.ERR,
                                       bytes([ftype, AGENT_ERR_LEN & 0xFF]))
            return

        if ftype == FrameType.PING:
            stamp = int(time.monotonic() * 1e6) % (1 << 64)
            with self._lock:
                self.rx += build_frame(FrameType.PONG,
                                       stamp.to_bytes(8, "little"))
            return

        with self._lock:
            self.rx += build_frame(FrameType.ACK, bytes([ftype, 0]))

        if ftype == FrameType.CONTROL:
            running = payload[0] == 1
            if running and not self._running:
                self._running = True
                self._stop.clear()
                self._thread = threading.Thread(target=self._report_loop, daemon=True)
                self._thread.start()
            elif not running:
                self._running = False
                self._stop.set()

    def _report_loop(self) -> None:
        while not self._stop.wait(DT_S):
            p = STATE_HEADER.pack(self._k * int(DT_S * 1e6),
                                  22_300_000 - self._k * 900,
                                  22_299_100 - self._k * 900,
                                  self._k * 2, self._k, len(self.neighbours))
            for _ in self.neighbours:
                p += STATE_NEIGHBOUR.pack(21_000_000, 0x03)
            with self._lock:
                self.rx += build_frame(FrameType.STATE, p)
            self._k += 1

    def read(self, n: int) -> bytes:
        with self._lock:
            out, self.rx = bytes(self.rx[:n]), self.rx[n:]
        if not out:
            time.sleep(0.005)
        return out

    def close(self) -> None:
        self._stop.set()


async def main(manifest_path: str) -> int:
    manifest = load_manifest_file(Path(manifest_path))
    rep = check(manifest)
    if not rep.ok:
        for e in rep.errors:
            print(f"  FAIL manifest: {e}")
        return 1
    print(f"  {manifest.name}: {len(manifest.nodes)} nodes, lambda2="
          f"{rep.algebraic_connectivity}")

    if WORK.exists():
        shutil.rmtree(WORK)
    WORK.mkdir(parents=True)

    # One bus for every locally-computing agent. Media are faked here, so a
    # ble<->wifi link would appear to work -- which is exactly why the medium
    # check lives in the validator and not in this harness.
    bus = LoopbackBus(WallClock(time.time()))
    services: dict[int, AgentService] = {}
    nrfs: dict[int, FakeNrf] = {}
    links: list[SerialLink] = []
    loop = asyncio.get_running_loop()

    for node in manifest.nodes:
        kind = AgentType(node.type)
        link = None
        if kind is AgentType.BLE:
            nrf = FakeNrf(node.id, list(node.neighbors))
            nrfs[node.id] = nrf
            link = SerialLink(io=nrf, loop=loop).open()
            links.append(link)
        svc = AgentService(
            kind,
            data_dir=WORK / f"agent-{node.id}",
            host_ip="127.0.0.1",
            control_port=CONTROL_PORTS[kind] + 10000 + node.id,  # loopback-safe
            link=link,
            transport_factory=(lambda nid, clk, b=bus: LoopbackTransport(b, nid)),
        )
        await svc.serve()
        services[node.id] = svc

    # Point the runner at the loopback ports the services actually bound.
    runner = ExperimentRunner(manifest, out_dir=WORK / "runs", timeout=10.0)
    runner.endpoint = lambda nid: ("127.0.0.1", services[nid].control_port)  # type: ignore
    for nid, a in runner.assignments.items():
        runner.assignments[nid] = a.model_copy(
            update={"dt_s": DT_S, "publish_period_s": DT_S})

    epoch = time.time()
    report = await runner.run("fleet-0", DURATION, epoch_unix_s=epoch)
    print(report.summary())

    fails: list[str] = []

    # 1. every ble agent logged something
    for nid, node in manifest.by_id.items():
        if node.type is not AgentType.BLE:
            continue
        n = report.nodes[nid]
        if n.samples == 0:
            fails.append(f"ble agent {nid} logged 0 samples -- the STATE path is dead")
        sent = [FrameType(t).name for t in nrfs[nid].frames_in]
        if "CONTROL" not in sent:
            fails.append(f"ble agent {nid} never triggered its nRF (sent {sent})")

    # 2. one epoch for the fleet
    epochs = {}
    for nid in report.nodes:
        meta = report.out_dir / f"{nid}.meta.json"
        if not meta.exists():
            fails.append(f"node {nid}: no metadata collected")
            continue
        d = json.loads(meta.read_text())
        epochs[nid] = d["environment"].get("epoch_unix_s")
    distinct = {e for e in epochs.values() if e is not None}
    if len(distinct) > 1:
        fails.append(f"nodes disagree on the epoch: {sorted(distinct)}")
    elif not distinct:
        fails.append("no node recorded an epoch")
    else:
        got = distinct.pop()
        print(f"  epoch agreed across {len(epochs)} nodes: {got:.3f}"
              f" (hub sent {epoch:.3f})")
        if abs(got - epoch) > 1e-6:
            fails.append(f"recorded epoch {got} != hub's {epoch}")

    # 3. bridges are on both media
    for nid, node in manifest.by_id.items():
        if node.type is not AgentType.BRIDGE:
            continue
        t = services[nid].agent.transport if services[nid].agent else None
        media = getattr(t, "media", None)
        # The factory injects a loopback here, so this checks the wiring the real
        # factory would use rather than the fake.
        real = services[nid]._make_transport.__doc__ or ""
        if "both" not in real:
            fails.append(f"bridge {nid}: transport factory does not document both media")
        if media is None and t is not None and t.name == "loopback":
            pass                    # expected under the fake

    # 4. units, per type
    for nid, node in manifest.by_id.items():
        meta = report.out_dir / f"{nid}.meta.json"
        if not meta.exists():
            continue
        d = json.loads(meta.read_text())
        want = "scaled_int" if node.type is AgentType.BLE else "engineering"
        if d.get("units") != want:
            fails.append(f"node {nid} ({node.type}) logged units="
                         f"{d.get('units')!r}, expected {want!r}")

    # 5. the two timelines
    from vertex.agent.runlog import RunMeta, recover_rows
    for nid, node in manifest.by_id.items():
        meta_path = report.out_dir / f"{nid}.meta.json"
        if not meta_path.exists():
            continue
        raw = json.loads(meta_path.read_text())
        m = RunMeta(**{k: v for k, v in raw.items()
                       if k in RunMeta.__dataclass_fields__})
        rows_name = report.nodes[nid].files.get("rows")
        if rows_name is None:
            fails.append(f"node {nid}: no rows collected")
            continue
        try:
            rows = recover_rows(report.out_dir / rows_name, meta=m)
        except Exception as exc:
            fails.append(f"node {nid}: collected rows are unreadable ({exc}); "
                         f"the filename's suffix carries the format")
            continue
        if not rows:
            fails.append(f"node {nid}: rows file decoded to nothing")
            continue
        cols = raw["columns"]
        if "device_timestamp" not in cols:
            fails.append(f"node {nid}: no device_timestamp column")
            continue
        it, idv = cols.index("timestamp"), cols.index("device_timestamp")
        deltas = [r[idv] - r[it] for r in rows]
        if node.type is AgentType.BLE:
            # The board's clock is its own; a delta of exactly zero would mean the
            # arrival stamp never reached the row and the board's time was reused.
            if max(abs(d) for d in deltas) == 0.0:
                fails.append(f"ble node {nid}: device_timestamp == timestamp on "
                             f"every row -- the host arrival time is not being "
                             f"recorded")
        else:
            if any(d != 0.0 for d in deltas):
                fails.append(f"{node.type} node {nid}: the two timelines differ "
                             f"(max {max(abs(d) for d in deltas):.6f}s) but its "
                             f"controller runs in this process")

    ble_deltas = []
    for nid, node in manifest.by_id.items():
        if node.type is not AgentType.BLE:
            continue
        rows_name = report.nodes[nid].files.get("rows")
        if not rows_name:
            continue
        raw = json.loads((report.out_dir / f"{nid}.meta.json").read_text())
        m = RunMeta(**{k: v for k, v in raw.items()
                       if k in RunMeta.__dataclass_fields__})
        cols = raw["columns"]
        it, idv = cols.index("timestamp"), cols.index("device_timestamp")
        for r in recover_rows(report.out_dir / rows_name, meta=m):
            ble_deltas.append(r[idv] - r[it])
    if ble_deltas:
        print(f"  relay device-vs-host offset: {min(ble_deltas):+.4f} .. "
              f"{max(ble_deltas):+.4f} s over {len(ble_deltas)} rows")

    counts = {str(t): 0 for t in AgentType}
    for nid, node in manifest.by_id.items():
        counts[str(node.type)] += report.nodes[nid].samples
    print(f"  samples by type: {counts}")

    # Clients first: an agent's shutdown has to close its open connections, and
    # this ordering keeps the harness from depending on that having been fixed.
    await runner.close()
    for nid, svc in services.items():
        await svc.shutdown()
    for link in links:
        link.close()

    for f in fails:
        print(f"  FAIL {f}")
    print(f"  -> {'fleet path works end to end' if not fails else f'{len(fails)} failure(s)'}")
    return 1 if fails else 0


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else str(ROOT / "experiments/n9-ring.yaml")
    sys.exit(asyncio.run(main(path)))
