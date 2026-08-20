#!/usr/bin/env python3
"""Bring up one flashed board. The first thing to run after `west flash`.

Everything else is either host-only or against a fake nRF. This talks to the real
coordination firmware over the real `SerialLink`, in the order things can fail.

    python3 test/nrf/check_board.py --port /dev/ttyACM0
    sudo -E python3 test/nrf/check_board.py --port /dev/ttyACM0 --scan   # + air

## Stages

1. **PING/PONG.** The narrowest useful test, and it exercises exactly what the
   `prj.conf` fix was about: a PING payload is empty, so the whole frame is 6
   bytes. If hardware byte counting is not active the UARTE driver never flushes
   anything that short and this times out -- which is the symptom that would
   otherwise look like a bad cable.

2. **Rejection.** A malformed ALGORITHM must come back as ERR with
   `AGENT_ERR_LEN`. A board that ACKs everything is not validating, and the first
   sign of that is normally a run configured with values it never applied.

3. **Configure and trigger.** N/A/D/R then CONTROL, each ACKed. This is where a
   field-offset disagreement between `agent.c` and `vertex/serial/proto.py` shows
   up as a rejection rather than as bad numbers later.

4. **STATE reports.** Collected for `--seconds`. Checks the report period matches
   the `clock` that was sent, that `t_us` advances monotonically, that the
   disturbance counter advances, and that the control law is actually moving the
   state. A board that reports a frozen state is running its timer but not its law.

5. **Air (`--scan`).** Scan for the board's own advertisements with the Pi's radio
   and decode them with the *host* codec. This is the only check that puts
   firmware-encoded v1 on a real radio and reads it back, and it cross-checks the
   vstate on the air against the vstate the same board reported over serial. The
   crossval harness proves the two codecs agree; this proves the radio path does.

`--scan` needs the HCI user channel: `sudo hciconfig hci0 down` first, and
CAP_NET_ADMIN.
"""
from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from vertex.numeric import dequantize
from vertex.serial import (FrameType, LinkError, LinkRejected, SerialLink,
                           decode_state, encode_algorithm, encode_control,
                           encode_disturbance, encode_network, encode_radio)

# Must match agent.h's AGENT_ERR_* codes.
AGENT_ERR_LEN, AGENT_ERR_RANGE, AGENT_ERR_TYPE = -1, -2, -3

NODE_ID = 1
NEIGHBOURS = [2]
DT_MS, CLOCK_MS = 200, 500
STATE0, VSTATE0 = 22.3, 22.3


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="/dev/ttyACM0")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--seconds", type=float, default=6.0)
    ap.add_argument("--scan", action="store_true",
                    help="also scan for the board's advertisements (needs HCI)")
    ap.add_argument("--device", type=int, default=0, help="hciN for --scan")
    args = ap.parse_args()

    fails: list[str] = []
    reports = []

    try:
        link = SerialLink(args.port, args.baud).open()
    except LinkError as exc:
        print(f"  FAIL {exc}")
        return 1

    # STATE frames arrive on the reader thread; no event loop here, so take them
    # directly. Appending to a list from one thread is fine.
    link.on_state(lambda f: reports.append((time.monotonic(), f)))

    try:
        # ── 1. PING ──────────────────────────────────────────────────────────
        try:
            t0 = time.monotonic()
            uptime_us = link.ping(timeout=2.0)
            rtt_ms = (time.monotonic() - t0) * 1000
            print(f"  ok   PING       uptime {uptime_us/1e6:.3f}s, RTT {rtt_ms:.1f} ms")
            print(f"                  (RTT bounds the CONTROL epoch bias -- see air_wire.h)")
        except LinkError as exc:
            print(f"  FAIL PING       {exc}")
            print("       A timeout here usually means CONFIG_UART_0_NRF_HW_ASYNC "
                  "is not set:\n"
                  "       without byte counting a 6-byte frame never leaves the "
                  "DMA buffer.")
            return 1

        # ── 2. rejection ─────────────────────────────────────────────────────
        try:
            link.request(FrameType.ALGORITHM, b"\x00" * 8, timeout=2.0)
            fails.append("a truncated ALGORITHM was ACKed; the board is not "
                         "validating payload lengths")
            print("  FAIL rejection  short ALGORITHM was accepted")
        except LinkRejected as exc:
            flag = "ok  " if exc.status == AGENT_ERR_LEN else "FAIL"
            print(f"  {flag} rejection  short ALGORITHM -> ERR status {exc.status}")
            if exc.status != AGENT_ERR_LEN:
                fails.append(f"expected AGENT_ERR_LEN ({AGENT_ERR_LEN}), "
                             f"got {exc.status}")
        except LinkError as exc:
            fails.append(f"rejection test: {exc}")
            print(f"  FAIL rejection  {exc}")

        # ── 3. configure and trigger ─────────────────────────────────────────
        from vertex.numeric import quantize
        frames = [
            ("NETWORK", FrameType.NETWORK,
             encode_network(enabled=True, node_id=NODE_ID, neighbors=NEIGHBOURS)),
            ("ALGORITHM", FrameType.ALGORITHM, encode_algorithm(
                dt_ms=DT_MS, clock_ms=CLOCK_MS,
                state0=quantize(STATE0), vstate0=quantize(VSTATE0),
                vartheta0=0, counter0=0,
                alpha=quantize(0.02), delta=quantize(0.01), eta=quantize(2e-6))),
            ("DISTURBANCE", FrameType.DISTURBANCE, encode_disturbance(
                active=True, sine_amplitude=quantize(3.75e-3),
                frequency=quantize(2.0), phase=quantize(0.0),
                noise_amplitude=quantize(2.5e-3), noise_offset=quantize(0.5),
                beta=quantize(5e-4), samples=1000)),
            ("RADIO", FrameType.RADIO, encode_radio(
                adv_min=160, adv_max=160, scan_interval=160, scan_window=48,
                active_scan=False, advertising=True)),
        ]
        for name, ftype, payload in frames:
            try:
                link.request(ftype, payload, timeout=2.0)
                print(f"  ok   {name:<10} {len(payload)} B accepted")
            except (LinkError, LinkRejected) as exc:
                fails.append(f"{name}: {exc}")
                print(f"  FAIL {name:<10} {exc}")

        epoch_us = int(time.time() * 1e6) % (1 << 48)
        try:
            link.request(FrameType.CONTROL,
                         encode_control(trigger=True, seed=12345,
                                        epoch_us=epoch_us), timeout=2.0)
            print(f"  ok   CONTROL    triggered, epoch {epoch_us}")
        except (LinkError, LinkRejected) as exc:
            fails.append(f"CONTROL: {exc}")
            print(f"  FAIL CONTROL    {exc}")
            return 1

        # ── 4. STATE reports ─────────────────────────────────────────────────
        reports.clear()
        print(f"  ..   collecting STATE for {args.seconds:g}s "
              f"(clock={CLOCK_MS} ms -> expect ~{args.seconds*1000/CLOCK_MS:.0f})")
        time.sleep(args.seconds)

        decoded = []
        for t, frame in list(reports):
            try:
                decoded.append((t, decode_state(frame.payload)))
            except Exception as exc:
                fails.append(f"undecodable STATE: {exc}")

        n = len(decoded)
        expected = args.seconds * 1000 / CLOCK_MS
        if n == 0:
            fails.append("no STATE frames at all -- the board triggered but is "
                         "not reporting")
            print("  FAIL STATE      nothing received")
        else:
            gaps = [(b[0] - a[0]) * 1000 for a, b in zip(decoded, decoded[1:])]
            med = statistics.median(gaps) if gaps else float("nan")
            flag = "ok  " if abs(n - expected) <= max(2, expected * 0.3) else "FAIL"
            print(f"  {flag} STATE      {n} reports, median gap {med:.0f} ms")
            if flag == "FAIL":
                fails.append(f"{n} reports in {args.seconds}s, expected ~{expected:.0f}")

            first, last = decoded[0][1], decoded[-1][1]
            t_us = [d.t_us for _, d in decoded]
            if any(b < a for a, b in zip(t_us, t_us[1:])):
                fails.append("t_us went backwards")
            counters = [d.counter for _, d in decoded]
            if len(decoded) > 1 and counters[-1] == counters[0]:
                fails.append("the disturbance counter did not advance -- the "
                             "control loop is not stepping")
            if len(decoded) > 1 and last.state == first.state:
                fails.append("state never changed -- the timer runs but the law "
                             "does not")

            print(f"       t_us      {first.t_us} -> {last.t_us}")
            print(f"       counter   {first.counter} -> {last.counter} "
                  f"(dt={DT_MS} ms, so ~{CLOCK_MS//DT_MS}/report)")
            print(f"       state     {dequantize(first.state):.6f} -> "
                  f"{dequantize(last.state):.6f}")
            print(f"       vstate    {dequantize(first.vstate):.6f} -> "
                  f"{dequantize(last.vstate):.6f}")
            print(f"       vartheta  {dequantize(first.vartheta):.6f} -> "
                  f"{dequantize(last.vartheta):.6f}")
            print(f"       neighbours n={len(last.neighbor_vstates)} "
                  f"enabled={list(last.neighbor_enabled)} "
                  f"fresh={list(last.neighbor_fresh)}")
            if len(last.neighbor_vstates) != len(NEIGHBOURS):
                fails.append(f"reported {len(last.neighbor_vstates)} neighbours, "
                             f"declared {len(NEIGHBOURS)}")
            if any(last.neighbor_fresh):
                print("       (a neighbour is fresh -- another board is on the air)")

        # ── 5. the air ───────────────────────────────────────────────────────
        if args.scan:
            heard = scan_for_board(args.device, NODE_ID, seconds=4.0)
            if heard is None:
                fails.append("scan failed; see the message above")
            elif not heard:
                fails.append("board never heard on the air, though it is "
                             "advertising per the RADIO ACK")
                print("  FAIL air        no advertisement from this node")
            else:
                pkt, count = heard
                print(f"  ok   air        {count} advertisement(s), decoded as v1")
                print(f"       node={pkt.node_id} seq={pkt.seq} "
                      f"vstate={dequantize(pkt.vstate):.6f} "
                      f"enabled={pkt.enabled} tx_time_us={pkt.tx_time_us}")
                if decoded:
                    serial_v = dequantize(decoded[-1][1].vstate)
                    air_v = dequantize(pkt.vstate)
                    print(f"       vstate on air {air_v:.6f} vs last serial "
                          f"{serial_v:.6f} (delta {abs(air_v-serial_v):.6f})")
                if pkt.tx_time_us == 0:
                    fails.append("tx_time_us is 0 on the air: the epoch from "
                                 "CONTROL did not reach the stamping path")
                if pkt.seq == 0 and count > 1:
                    fails.append("seq stayed 0 across advertisements; the "
                                 "sequence counter is not advancing")
    finally:
        try:
            link.request(FrameType.CONTROL,
                         encode_control(trigger=False), timeout=1.0)
        except Exception:
            pass
        print(f"  link: {link.counters.summary()}")
        link.close()

    for f in fails:
        print(f"  FAIL {f}")
    print(f"  -> {'board is alive and running the law' if not fails else f'{len(fails)} problem(s)'}")
    return 1 if fails else 0


def scan_for_board(device: int, node_id: int, *, seconds: float):
    """Scan with the Pi's radio for this node's advertisements.

    Returns (last_packet, count), () if nothing was heard, or None if the radio
    could not be opened.
    """
    import select

    from vertex.radio import find_manufacturer
    from vertex.radio.hci import (HciError, HciSocket, HciStatus,
                                  cmd_le_set_scan_enable,
                                  cmd_le_set_scan_parameters, cmd_reset,
                                  parse_adv_reports, parse_event)
    from vertex.wire import DecodeError, decode_any
    from vertex.wire.codec import COMPANY_ID

    try:
        sock = HciSocket(device).open()
    except HciError as exc:
        print(f"  FAIL air        {exc}")
        return None

    last, count = None, 0
    try:
        sock.command(cmd_reset())
        sock.command(cmd_le_set_scan_enable(False),
                     tolerate=(HciStatus.COMMAND_DISALLOWED,))
        sock.command(cmd_le_set_scan_parameters(interval=160, window=160,
                                                scan_type=0x00))
        # Duplicate filtering off: the payload changes every control period and a
        # suppressed duplicate is indistinguishable from a packet never sent.
        sock.command(cmd_le_set_scan_enable(True, filter_duplicates=False))

        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            ready, _, _ = select.select([sock.fileno], [], [],
                                        max(0.0, deadline - time.monotonic()))
            if not ready:
                break
            try:
                event = parse_event(sock.recv(1024))
            except (HciError, OSError):
                continue
            if event.code != 0x3E:
                continue
            for rpt in parse_adv_reports(event.params):
                value = find_manufacturer(rpt.data, COMPANY_ID)
                if value is None:
                    continue
                try:
                    pkt = decode_any(value)
                except DecodeError:
                    continue
                if pkt.node_id != node_id:
                    continue
                last, count = pkt, count + 1
    finally:
        try:
            sock.command(cmd_le_set_scan_enable(False),
                         tolerate=(HciStatus.COMMAND_DISALLOWED,))
        except Exception:
            pass
        sock.close()

    return (last, count) if last is not None else ()


if __name__ == "__main__":
    sys.exit(main())
