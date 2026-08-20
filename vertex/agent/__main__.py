"""Run one agent on this host.

    python3 -m vertex.agent --type wifi
    python3 -m vertex.agent --type ble --serial /dev/ttyACM0
    sudo -E python3 -m vertex.agent --type bridge      # HCI needs CAP_NET_ADMIN

One process per agent, three per Pi. The type decides almost everything else:

    ble      no local controller. The law runs on the nRF; this process relays
             configuration down the serial link and logs the STATE frames coming
             back, as scaled int32.
    wifi     controller here, published over UDP broadcast.
    bridge   controller here, published over BLE *and* UDP -- it is the only agent
             on both media, and the only path between the `ble` and `wifi` subnets.

The control port follows from the type (3001/3002/3003), so the hub needs only the
host address and the manifest. Nothing is configured here: the process starts,
serves, and waits for the hub to tell it who it is.

## What each type needs from the host

`ble`     a serial port to the nRF, and membership of `dialout`.
`bridge`  the HCI user channel, which is exclusive: BlueZ must be stopped and the
          adapter down (`sudo hciconfig hci0 down`), and the process needs
          CAP_NET_ADMIN -- either run under sudo or
          `setcap cap_net_admin,cap_net_raw+eip $(readlink -f $(which python3))`.
`wifi`    nothing beyond the LAN.
"""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys
import time
from pathlib import Path

from ..clock import WallClock
from ..net import AgentType, InterfaceError, resolve_local_ip
from .service import AgentService


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        prog="python3 -m vertex.agent",
        description="Serve one agent and wait for the hub.")
    ap.add_argument("--type", required=True,
                    choices=[str(t) for t in AgentType],
                    help="agent type; also selects the control port")
    ap.add_argument("--data-dir", default="data", type=Path,
                    help="where run logs are written (default: ./data)")
    ap.add_argument("--interface", default=None,
                    help="interface carrying the experiment LAN "
                         "(default: vertex.net.DEFAULT_INTERFACE)")
    ap.add_argument("--host-ip", default=None,
                    help="override the resolved address; skips interface lookup")
    ap.add_argument("--serial", default="/dev/ttyACM0",
                    help="nRF serial port; `ble` only (default: /dev/ttyACM0)")
    ap.add_argument("--baud", default=115200, type=int)
    ap.add_argument("--control-port", default=None, type=int,
                    help="override the port implied by --type; for two agents of "
                         "one type on a host, which the manifest forbids")
    ap.add_argument("--log-format", default="binary", choices=["binary", "csv", "jsonl"])
    ap.add_argument("--epoch", default=None, type=float,
                    help="experiment epoch as a Unix timestamp. Every node in a "
                         "run must be given the SAME value, or their timestamps "
                         "are not comparable and one-way delay is meaningless. "
                         "Defaults to this process's start time, which is only "
                         "correct for a single-node smoke test.")
    return ap.parse_args(argv)


async def run(args: argparse.Namespace) -> int:
    node_type = AgentType(args.type)

    if args.host_ip:
        host_ip = args.host_ip
    else:
        try:
            host_ip = resolve_local_ip(*( [args.interface] if args.interface else [] ))
        except InterfaceError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

    link = None
    if node_type is AgentType.BLE:
        # Imported here so a `wifi` agent does not need pyserial installed.
        from ..serial import LinkError, SerialLink
        link = SerialLink(args.serial, args.baud, loop=asyncio.get_running_loop())
        try:
            link.open()
        except LinkError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

    if args.epoch is None:
        print("warning: no --epoch given; this node's timestamps will not be "
              "comparable with the rest of the fleet", file=sys.stderr)

    service = AgentService(
        node_type,
        data_dir=args.data_dir,
        host_ip=host_ip,
        control_port=args.control_port,
        log_format=args.log_format,
        clock=WallClock(args.epoch if args.epoch is not None else time.time()),
        link=link,
        environment={"host_ip": host_ip, "interface": args.interface or "default"},
    )

    await service.serve()
    print(f"{node_type} agent on {host_ip}, control port {service.control_port}, "
          f"data {args.data_dir}"
          + (f", nRF {args.serial}" if link is not None else ""))

    # Wait for a signal rather than polling. The hub drives everything else, and a
    # run in progress must not be interrupted by this process getting bored.
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:                             # pragma: no cover
            pass
    await stop.wait()

    print("shutting down")
    await service.shutdown()
    if link is not None:
        link.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(run(parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
