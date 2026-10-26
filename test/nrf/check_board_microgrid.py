#!/usr/bin/env python3
"""Verify the microgrid law on a real board, with one board and no radio.

`check_board.py` exercises the scalar law; this is its counterpart for the
second family. `test/crossval/check_microgrid.py` already compares the same C
against the same Python, but compiled by gcc for x86. Here the code is built
by arm-none-eabi and runs on the nRF's own FPU, against its own libm -- so
`tanhf`, `expf` and `powf` are on the path for the first time.

## Why one board is enough

A node with `degree = 0` and `b = 1` is not a degenerate case, it is a pinned
node with no neighbours. The observer forms `D = degree + b = 1` and
`w = b*r`, so the virtual layer converges to the reference on its own and the
interface tracks it. Nothing here needs a second board or a radio, which is
what makes this runnable before the fleet exists.

## Why the comparison is exact rather than time-aligned

The law reads its own clock from `vars.counter` (microgrid_task.c), and that
counter travels in every STATE report. So report `counter = n` is the result
of the step that ran at `t = (n-1)*dt`, and it lines up with the n-th call to
the Python. No nearest-sample matching, no drift budget.

## What the tolerance means

This is not a precision check -- check_microgrid.py is, and it pins the
residual at a few hundred LSB. This asks whether the board is running the
same *equation*, so the tolerance is loose on purpose. A wrong branch, a
stale field offset or an unselected law misses by orders, not by ULPs.

    python3 test/nrf/check_board_microgrid.py --port /dev/ttyACM0
    python3 test/nrf/check_board_microgrid.py --law lc --terminal
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from vertex.controllers import ControllerParams, create
from vertex.controllers.interface_adaptive import AdaptiveParams
from vertex.controllers.interface_lc import LCParams
from vertex.controllers.plant import PlantParams
from vertex.controllers.virtual import VirtualParams
from vertex.numeric import dequantize, quantize
from vertex.serial import (FrameType, LinkError, LinkRejected, SerialLink,
                           decode_state, encode_algorithm, encode_control,
                           encode_disturbance, encode_network)
from vertex.serial.proto import decode_stats
from vertex.serial.proto import (LAW_MICROGRID_ADAPTIVE, LAW_MICROGRID_LC,
                                 MG_IFACE_FIELDS, MG_PLANT_FIELDS,
                                 MG_VIRTUAL_FIELDS, encode_microgrid_iface,
                                 encode_microgrid_plant,
                                 encode_microgrid_virtual)

NODE_ID = 1
#: The rates dg5 actually runs at, not check_board.py's 200/500 ms. The
#: prescribed-time gains are written against these.
DT_MS, CLOCK_MS = 25, 125
#: The scalar law's initial condition, used only by the negative control at
#: the end: after a NETWORK frame the board must come back here.
SCALAR_STATE0 = 22.3

KAPPA, S1, S2 = 5 / 15, 5.0, 5.0
X0_P, X0_Q = 4.0, 2.0
Z0_P, Z0_Q = 5.0, 1.0
R_P, R_Q = 20.0, 25.0
T_O, T_C = 30.0, 40.0

#: 2e-2 at a state of order 20, so 1e-3 relative. See the module docstring:
#: this separates laws, it does not measure precision.
TOL_LSB = 20_000


def mg_fields(lc: bool) -> dict:
    """One configuration, in the units the wire uses.

    `degree = 0` with `b = 1`: a pinned node with no neighbours.
    """
    return dict(
        kappa=quantize(KAPPA), S1=quantize(S1), S2=quantize(S2),
        p_max=quantize(10.0), q_max=quantize(12.0),
        T_o=int(T_O * 1000), alpha_0=quantize(1.0), delta_o=CLOCK_MS,
        kappa_o=quantize(1e-3), xi=quantize(0.025), c_0=quantize(0.1),
        b=quantize(1.0), rP=quantize(R_P), rQ=quantize(R_Q),
        degree=0,
        mu_max=quantize(10.0),
        gamma=0 if lc else quantize(0.0025),
        eps=0 if lc else quantize(0.09),
        vartheta_0=0,
        T_c=int(T_C * 1000) if lc else 0,
        beta_c=quantize(2.0) if lc else 0,
        Phi=quantize(3.0) if lc else 0,
        delta_c=0,
        adapt_band=-1,
        X0_P=quantize(X0_P), X0_Q=quantize(X0_Q),
        z0_P=quantize(Z0_P), z0_Q=quantize(Z0_Q),
    )


def algorithm_frame() -> bytes:
    """The scalar block. The microgrid law reads only `dt` and `clock` from it.

    Its `state0` is deliberately not X0_P: the negative control at the end
    relies on the two being distinguishable.
    """
    return encode_algorithm(
        dt_ms=DT_MS, clock_ms=CLOCK_MS,
        state0=quantize(SCALAR_STATE0), vstate0=quantize(SCALAR_STATE0),
        vartheta0=0, counter0=0, gain_ij=quantize(0.1),
        alpha=quantize(0.5), delta=quantize(0.01), eta=quantize(5e-5))


def quiet_disturbance() -> bytes:
    """Off, and a counter long enough that it cannot wrap inside a run.

    The counter is the law's clock, so a wrap would rewind every deadline.
    """
    return encode_disturbance(active=False, sine_amplitude=0, frequency=0,
                              phase=0, noise_amplitude=0, noise_offset=0,
                              beta=0, samples=1_000_000)


def py_rows(lc: bool, terminal: bool, n: int) -> list[tuple[int, int, int]]:
    """(xP, zP, vartheta) per step, scaled, from the Python implementation."""
    iface = (LCParams(kappa=KAPPA, profile="per", T_c=T_C, beta_c=2.0,
                      Phi=3.0, terminal=terminal, mu_max=10.0)
             if lc else
             AdaptiveParams(gamma=0.0025, eps=0.09, mu_max=10.0))
    c = create("microgrid_lc" if lc else "microgrid_adaptive", ControllerParams(
        dt_s=DT_MS / 1000.0, degree=0,
        plant=PlantParams(kappa=KAPPA, S1=S1, S2=S2, profile="per"),
        virtual=VirtualParams(T_o=T_O, alpha_0=1.0, delta_o=CLOCK_MS / 1000.0,
                              kappa_o=1e-3, xi=0.025, c_0=0.1,
                              b=1.0, rP=R_P, rQ=R_Q),
        interface=iface,
        state0_P=X0_P, state0_Q=X0_Q, vstate0_P=Z0_P, vstate0_Q=Z0_Q))
    out = []
    for _ in range(n):
        c.step([], [])
        it = c.interface
        out.append((quantize(c.plant.xP), quantize(c.virtual.zP),
                    quantize(getattr(it, "vartheta", 0.0) if not lc else 0.0)))
    return out


#: The counters that say where a frame died, if it died.
#:
#:   len_errors   the parser rejected the declared length
#:   crc_errors   it arrived corrupt
#:   resyncs      framing was lost and the parser hunted for the next SOF
#:   timeouts     a frame started and never finished
#:   frames_ok    it was delivered to control_on_frame -- so a silent frame
#:                that increments THIS one died in the dispatcher, not the wire
WIRE_COUNTERS = ("frames_ok", "crc_errors", "len_errors", "resyncs",
                 "timeouts", "rx_overrun_bytes", "rx_partial_flushes",
                 "tx_frames", "tx_dropped")


def _delta(before: dict, after: dict, key: str):
    """`after - before` for one counter, or None if either read failed."""
    a, b = after.get(key), before.get(key)
    return None if a is None or b is None else a - b


def wire_counters(link) -> dict:
    """The board's own view of the serial link. Empty if it will not answer."""
    try:
        reply = link.request(FrameType.STATS_REQ, b"", timeout=2.0)
        st = decode_stats(reply.payload)
        return {k: st.values.get(k) for k in WIRE_COUNTERS}
    except Exception:
        return {}


#: agent.h's AGENT_ERR_* codes. A rejection is a REPLY: it proves the frame
#: crossed the wire, the parser and the dispatcher. Silence proves none of it.
AGENT_ERR_LEN, AGENT_ERR_RANGE, AGENT_ERR_TYPE = -1, -2, -3


def probes(link) -> None:
    """Separate "does the board know 0x4D" from "can it receive 121 bytes".

    Both failures look identical from the host -- a timeout on one frame --
    and they have nothing to do with each other, so asking them together is
    what made the first three runs unreadable.

    Probe 1, an MG_IFACE frame with a 1-byte payload: seven bytes on the
    wire, so no size effect is possible. The board must answer SOMETHING.
      ERR -3  the parser admitted the type but control.c does not route it
      ERR -1  routed, and agent.c rejected the length -- both lists are right
      silence the parser dropped it at the type byte

    Probe 2, an ADV_TX frame with a 115-byte payload: `max_len_for` has
    always admitted ADV_TX up to 256 bytes, and control.c has never routed
    it, so a board of any vintage owes an ERR -3. It is the size the single
    microgrid frame used to be, and shares none of its code -- which is how
    the 64-byte transport limit was separated from the law in the first
    place. Kept as a standing check that the limit has not moved.
      ERR     a frame this big arrives intact; size is not the problem
      silence anything past ~64 bytes is being lost, which is a link fault
              and not a microgrid one
    """
    for label, ftype, payload, note in (
            ("type", FrameType.MG_IFACE, bytes([LAW_MICROGRID_ADAPTIVE]),
             "does the board know the microgrid frames"),
            ("size", FrameType.ADV_TX, bytes(115),
             "can it receive 121 bytes at all")):
        try:
            link.request(ftype, payload, timeout=2.0)
            print(f"  ok   probe {label:<5} {note}: ACCEPTED "
                  f"({len(payload)} B payload)")
        except LinkRejected as exc:
            meaning = {AGENT_ERR_LEN: "routed, rejected on length -- the type "
                                      "is fully wired",
                       AGENT_ERR_TYPE: "reached the dispatcher, which does not "
                                       "route it"}.get(exc.status, "rejected")
            print(f"  ok   probe {label:<5} {note}: ERR {exc.status} "
                  f"({meaning})")
        except LinkError:
            print(f"  FAIL probe {label:<5} {note}: SILENCE")


def size_threshold(link) -> None:
    """Find the largest host->board frame that gets a reply.

    ADV_TX is the probe because it is inert: `max_len_for` admits it up to
    256 bytes and control.c has never routed it, so every board owes an
    ERR -3 at any size. The reply is the measurement; the frame's meaning
    is irrelevant.

    Bisection, not a sweep: the boundary is sharp if it is a packet size and
    ragged if it is a timing race, and bisecting then re-testing the answer
    three times tells the two apart. A ragged boundary would not be a size
    limit at all.
    """
    def answers(payload_len: int) -> bool:
        try:
            link.request(FrameType.ADV_TX, bytes(payload_len), timeout=1.5)
            return True
        except LinkRejected:
            return True                 # a rejection IS a reply
        except LinkError:
            return False

    if not answers(1):
        print("  FAIL size        even a 7-byte frame gets no reply; this is "
              "not a size limit")
        return
    lo, hi = 1, 250                     # lo answers, hi is assumed not to
    if answers(hi):
        print(f"  ok   size        {hi} B payload answers; no limit found "
              f"below {hi + 6} B on the wire")
        return
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if answers(mid):
            lo = mid
        else:
            hi = mid
    stable = sum(answers(lo) for _ in range(3))
    print(f"  ..   size        largest payload that answers: {lo} B "
          f"({lo + 6} B on the wire); {lo + 1} B does not")
    print(f"       repeatable  {stable}/3 retries at {lo} B answered"
          f"{'' if stable == 3 else '  -- RAGGED, so this is a race, not a size'}")
    for named, n in (("USB CDC packet", 64), ("nRF UARTE chunk", 128)):
        if lo + 6 == n:
            print(f"       the boundary is exactly one {named} ({n} B)")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="/dev/ttyACM0")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--seconds", type=float, default=10.0,
                    help=f"must stay under T_o = {T_O:g}s: the prescribed-time "
                         f"gain diverges there by construction")
    ap.add_argument("--law", choices=["adaptive", "lc"], default="adaptive")
    ap.add_argument("--terminal", action="store_true",
                    help="LC0 rather than LC+ (only meaningful past T_c)")
    ap.add_argument("--tol-lsb", type=int, default=TOL_LSB)
    ap.add_argument("--write-chunk", type=int, default=0, metavar="N",
                    help="split every host->board write into N-byte pieces. "
                         "64 is the USB CDC packet size; if that alone lifts "
                         "the size limit, no protocol change is needed")
    ap.add_argument("--probe-size", action="store_true",
                    help="bisect for the largest host->board frame the link "
                         "will carry, then exit")
    ap.add_argument("--probe", action="store_true",
                    help="ask the two diagnostic questions first: does the "
                         "board know frame 0x4D, and can it receive 121 bytes")
    args = ap.parse_args()

    # No cap at T_o. alpha(t) does diverge there by construction, but the
    # fleet runs straight through it and the layer reaches the reference
    # first -- and the regime worth comparing on hardware is exactly the one
    # past T_o, which the cap made unreachable. Both sides integrate the same
    # blowup, so the comparison stays meaningful; what it cannot survive is a
    # DIFFERENT blowup, which is the thing being looked for.
    if args.seconds >= T_O:
        print(f"  ..   window      {args.seconds:g}s runs through T_o = "
              f"{T_O:g}s" + (f" and T_c = {T_C:g}s" if args.seconds >= T_C
                             else "") + "; alpha(t) diverges at T_o on both "
              f"sides by construction")

    lc = (args.law == "lc")
    fails: list[str] = []
    reports: list = []

    try:
        link = SerialLink(args.port, args.baud).open()
    except LinkError as exc:
        print(f"  FAIL {exc}")
        return 1
    link.write_chunk = args.write_chunk
    if args.write_chunk:
        print(f"  ..   writes      split into {args.write_chunk}-byte pieces")
    link.on_state(reports.append)

    try:
        uptime_us = link.ping(timeout=2.0)
        print(f"  ok   PING       uptime {uptime_us/1e6:.3f}s")

        if args.probe:
            probes(link)
        if args.probe_size:
            # A measurement run, not a check: configuring past this point
            # would only reprint the failure we are here to characterise.
            size_threshold(link)
            return 0

        # ── configure, exactly as the relay does ─────────────────────────────
        # The microgrid blocks after NETWORK, because NETWORK clears both the
        # law and the record of which blocks have arrived, and before CONTROL,
        # because the trigger latches the initial conditions.
        f = mg_fields(lc)
        seq = [
            ("NETWORK", FrameType.NETWORK,
             encode_network(enabled=True, node_id=NODE_ID, neighbors=[])),
            ("ALGORITHM", FrameType.ALGORITHM, algorithm_frame()),
            ("DISTURBANCE", FrameType.DISTURBANCE, quiet_disturbance()),
            # Three frames, in order. MG_IFACE carries `law` and the board
            # refuses it until the other two have landed, so a run that gets
            # this far has also proved the commit rule on hardware.
            ("MG_PLANT", FrameType.MG_PLANT, encode_microgrid_plant(
                profile=1, **{k: f[k] for k in MG_PLANT_FIELDS})),
            ("MG_VIRTUAL", FrameType.MG_VIRTUAL, encode_microgrid_virtual(
                **{k: f[k] for k in MG_VIRTUAL_FIELDS})),
            ("MG_IFACE", FrameType.MG_IFACE, encode_microgrid_iface(
                law=LAW_MICROGRID_LC if lc else LAW_MICROGRID_ADAPTIVE,
                terminal=args.terminal,
                **{k: f[k] for k in MG_IFACE_FIELDS})),
        ]
        for name, ftype, payload in seq:
            # Bracket the largest of the three (54 B on the wire) with the
            # board's own counters: a silent frame that raises `frames_ok`
            # reached the dispatcher and was dropped there, one that raises
            # `len_errors` or `crc_errors` died on the wire, and one that
            # moves nothing never arrived at all.
            before = (wire_counters(link)
                      if ftype is FrameType.MG_VIRTUAL else {})
            try:
                link.request(ftype, payload, timeout=2.0)
                print(f"  ok   {name:<11} {len(payload)} B accepted")
            except (LinkError, LinkRejected) as exc:
                print(f"  FAIL {name:<11} {exc}")
                fails.append(f"{name}: {exc}")
                after = wire_counters(link) if before else {}
                if before and after:
                    moved = {k: (before[k], after[k]) for k in WIRE_COUNTERS
                             if before.get(k) != after.get(k)}
                    print(f"       board counters that moved: "
                          f"{moved or 'none -- the frame never reached it'}")
                    if "frames_ok" in moved:
                        print("       frames_ok moved: the wire is fine and "
                              "control_on_frame() dropped it")

        # Snapshot before the trigger, so the run's own share of every
        # counter is a difference rather than a lifetime total.
        before_run = wire_counters(link)
        reports.clear()
        link.request(FrameType.CONTROL,
                     encode_control(trigger=True, seed=1, epoch_us=1),
                     timeout=2.0)
        arm = "C_LC0" if (lc and args.terminal) else "C_LC+" if lc else "C_DZ"
        print(f"  ok   CONTROL     triggered, arm {arm}")

        n_steps = int(args.seconds * 1000 / DT_MS)
        print(f"  ..   collecting STATE for {args.seconds:g}s "
              f"(dt={DT_MS} ms -> expect ~{n_steps})")
        time.sleep(args.seconds)
        got = list(reports)

        # ── the board ran the microgrid law at all ───────────────────────────
        # The clearest evidence, and it needs no reference trajectory: the
        # scalar law would have started from ALGORITHM's state0.
        if not got:
            print("  FAIL STATE      nothing received")
            fails.append("no STATE frames")
        else:
            first = decode_state(got[0].frame.payload)
            x0 = dequantize(first.state)
            # The state is NORMALISED: microgrid_reset sets x = X0_P/kappa,
            # because the manifest carries the physical initial condition and
            # the law integrates the normalised one. Comparing against X0_P
            # itself passed here by luck -- 12.0 is nearer 4.0 than 22.3 --
            # and would have passed for a board running neither.
            want = X0_P / KAPPA
            if abs(x0 - want) > 1e-3:
                near = ("ALGORITHM's state0" if abs(x0 - SCALAR_STATE0) < 1e-3
                        else "neither initial condition")
                print(f"  FAIL law        first state {x0:.4f}, expected "
                      f"X0_P/kappa = {want:.4f}; it is {near} "
                      f"(scalar state0 {SCALAR_STATE0})")
                fails.append("the board did not start from the microgrid IC")
            else:
                print(f"  ok   law         first state {x0:.4f} = X0_P/kappa "
                      f"({X0_P}/{KAPPA:.4f}), so microgrid_reset ran")

        # ── against the Python, indexed by the law's own counter ─────────────
        by_counter: dict[int, tuple[int, int, int]] = {}
        for timed in got:
            try:
                d = decode_state(timed.frame.payload)
            except Exception as exc:
                fails.append(f"undecodable STATE: {exc}")
                continue
            by_counter[d.counter] = (d.state, d.vstate, d.vartheta)

        if by_counter:
            ref = py_rows(lc, args.terminal, max(by_counter) + 1)
            names = ("xP", "zP", "vartheta")
            # `vartheta` is an absence under the LC interfaces, not a zero.
            checked = (0, 1) if lc else (0, 1, 2)
            worst = [0] * 3
            where = [0] * 3
            compared = 0
            for n, row in sorted(by_counter.items()):
                # counter n is the result of the step that ran at t=(n-1)*dt,
                # which is the n-th Python call: ref[n-1].
                if not 1 <= n <= len(ref):
                    continue
                compared += 1
                for i in checked:
                    d = abs(row[i] - ref[n - 1][i])
                    if d > worst[i]:
                        worst[i], where[i] = d, n
            bad = [names[i] for i in checked if worst[i] > args.tol_lsb]
            detail = " ".join(f"{names[i]}={worst[i]}" for i in checked)
            flag = "ok  " if not bad else "FAIL"
            print(f"  {flag} vs Python   {compared} steps compared, "
                  f"max |board - py| LSB: {detail}")
            if bad:
                for i in checked:
                    if worst[i] > args.tol_lsb:
                        print(f"       {names[i]} worst at counter {where[i]}")
                # An off-by-one in the counter mapping looks like a large
                # residual too, so say which of the two it is rather than
                # leaving the reader to guess.
                shifted = max(
                    abs(row[0] - ref[n][0])
                    for n, row in sorted(by_counter.items())
                    if 0 <= n < len(ref))
                if shifted <= args.tol_lsb:
                    print(f"       but a one-step shift agrees "
                          f"(xP={shifted}): this is the counter mapping, "
                          f"not the law")
                fails.append(f"board and Python disagree on {bad}")
            last = max(by_counter)
            print(f"       zP        {dequantize(by_counter[min(by_counter)][1]):.4f}"
                  f" -> {dequantize(by_counter[last][1]):.4f} "
                  f"(reference rP = {R_P})")
            span = max(by_counter) - min(by_counter) + 1
            lost = span - len(by_counter)
            print(f"       reports   {len(by_counter)} distinct counters over "
                  f"a span of {span}, {lost} missing")
            # Which side dropped them. The board counts every STATE it queued
            # and every one it could not; the difference against what arrived
            # here is the USB path. Reporting is at `dt`, so at 25 ms this is
            # the rate the campaign itself will run at -- loss here is not a
            # property of the microgrid law and would hit the scalar runs too.
            if lost:
                c = wire_counters(link)
                # Differenced, not read once: these counters run from the
                # board's boot, and after seventeen hours of uptime the raw
                # `tx_frames` is a lifetime total that says nothing about
                # this run. Reported once as an absolute number, it made a
                # 400-frame run look like 1294 queued.
                sent = _delta(before_run, c, "tx_frames")
                dropped = _delta(before_run, c, "tx_dropped")
                if sent is not None:
                    print(f"       tx        board queued {sent} in this run, "
                          f"dropped {dropped}; host received {len(by_counter)}")
                    if dropped:
                        print(f"       -> the board's TX queue overflowed: it "
                              f"could not hand them to the UART fast enough")
                    else:
                        print(f"       -> the board queued them all, so the "
                              f"loss is on the USB path to the host, not on "
                              f"the nRF")

        # ── negative control: NETWORK must take the law away again ───────────
        # The property the manifest contract rests on, asserted on hardware
        # rather than inferred from the source.
        link.request(FrameType.CONTROL, encode_control(trigger=False),
                     timeout=2.0)
        reports.clear()
        for ftype, payload in (
                (FrameType.NETWORK,
                 encode_network(enabled=True, node_id=NODE_ID, neighbors=[])),
                (FrameType.ALGORITHM, algorithm_frame()),
                (FrameType.DISTURBANCE, quiet_disturbance())):
            link.request(ftype, payload, timeout=2.0)
        link.request(FrameType.CONTROL,
                     encode_control(trigger=True, seed=1, epoch_us=1),
                     timeout=2.0)
        time.sleep(0.5)
        back = list(reports)
        if not back:
            print("  FAIL reset      no STATE after the second trigger")
            fails.append("no STATE after re-arming")
        else:
            x = dequantize(decode_state(back[0].frame.payload).state)
            if abs(x - SCALAR_STATE0) < abs(x - X0_P):
                print(f"  ok   reset       NETWORK put the board back on the "
                      f"scalar law (state {x:.4f})")
            else:
                print(f"  FAIL reset      state {x:.4f} still looks like the "
                      f"microgrid law; NETWORK did not reset it")
                fails.append("NETWORK did not reset the law")
        link.request(FrameType.CONTROL, encode_control(trigger=False),
                     timeout=2.0)

    except (LinkError, LinkRejected) as exc:
        print(f"  FAIL link error: {exc}")
        fails.append(str(exc))
    finally:
        link.close()

    print(f"  -> {'ok' if not fails else f'{len(fails)} problem(s)'}")
    for f in fails:
        print(f"     - {f}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
