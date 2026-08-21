#!/usr/bin/env python3
"""Does UDP broadcast actually cross between these hosts?

Run on both Pis at once. It uses the same port and the same address computation as
`UdpTransport`, so a pass here means the medium works and the fault is in vertex; a
fail means the network is not carrying broadcast and no amount of agent debugging
will help.

    # on each Pi, at the same time
    python3 scripts/udp_check.py --interface wlan0 --seconds 10

Isolates the four things that produce silent zero-delivery:

  * the wrong broadcast address (`broadcast_address()` assumes /24 -- if the subnet
    is not /24 the datagrams go to an address nobody is listening on);
  * AP client isolation, which blocks station-to-station traffic on many access
    points and is invisible from either end;
  * a firewall on the receiving host;
  * SO_REUSEPORT sharing, since three agents on one host bind the same port.
"""
from __future__ import annotations

import argparse
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vertex.net import (STATE_PORT, InterfaceError, broadcast_address,
                        interface_broadcast, interface_prefixlen,
                        resolve_local_ip)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--interface", default="wlan0")
    ap.add_argument("--port", type=int, default=STATE_PORT)
    ap.add_argument("--seconds", type=float, default=10.0)
    ap.add_argument("--prefixlen", type=int, default=24,
                    help="subnet prefix; UdpTransport assumes 24")
    args = ap.parse_args()

    me = resolve_local_ip(args.interface)
    guessed = broadcast_address(me, args.prefixlen)
    try:
        bcast, source = interface_broadcast(args.interface), "kernel"
    except InterfaceError:
        bcast, source = guessed, f"assumed /{args.prefixlen}"

    plen = interface_prefixlen(args.interface)
    print(f"  local {me}/{plen}  ->  broadcast {bcast}:{args.port}  ({source})")
    if bcast != guessed:
        print(f"  NOTE a /{args.prefixlen} assumption would use {guessed}, which on "
              f"a /{plen} network is an")
        print(f"       ordinary host address -- every datagram goes nowhere, "
              f"silently. That was the bug.")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.settimeout(0.2)
    sock.bind(("0.0.0.0", args.port))

    sent = 0
    heard: dict[str, int] = {}
    own = 0
    deadline = time.monotonic() + args.seconds
    next_tx = 0.0
    while time.monotonic() < deadline:
        now = time.monotonic()
        if now >= next_tx:
            try:
                sock.sendto(f"vertex-udp-check from {me}".encode(), (bcast, args.port))
                sent += 1
            except OSError as exc:
                print(f"  FAIL sendto({bcast}): {exc}")
                return 1
            next_tx = now + 0.5
        try:
            data, addr = sock.recvfrom(1024)
        except socket.timeout:
            continue
        except OSError:
            continue
        if addr[0] == me:
            own += 1
        else:
            heard[addr[0]] = heard.get(addr[0], 0) + 1

    print(f"  sent {sent}, own datagrams looped back {own}")
    if heard:
        for host, n in sorted(heard.items()):
            print(f"  ok   heard {n} from {host}")
        print("  -> broadcast crosses between hosts; the medium is fine")
        return 0

    print("  FAIL heard nothing from any other host.")
    print("       If the other Pi was running this at the same time, the network is")
    print("       not carrying broadcast between them. In order of likelihood:")
    print("         1. AP client isolation -- if one Pi is the access point, or")
    print("            both associate to one, station-to-station traffic may be")
    print("            blocked. Check the AP's config; test with unicast to prove")
    print("            the hosts can reach each other at all.")
    print(f"         2. wrong broadcast address -- this used {bcast} on a /"
          f"{args.prefixlen}. Compare with `ip -4 addr show {args.interface}`.")
    print("         3. a firewall on the receiver (nft/iptables INPUT).")
    if own == 0:
        print("       NOTE: not even this host's own datagrams came back, which")
        print("       points at the address or a local filter rather than the AP.")
    return 1


def _kernel_broadcast(iface: str) -> str | None:
    """What `ip` says the broadcast address is, to compare with our /24 guess."""
    import re
    import subprocess
    try:
        out = subprocess.run(["ip", "-4", "addr", "show", iface],
                             capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return None
    m = re.search(r"inet (\S+)(?: brd (\S+))?", out)
    if not m:
        return None
    return f"{m.group(1)} brd {m.group(2) or '(none)'}"


if __name__ == "__main__":
    sys.exit(main())
