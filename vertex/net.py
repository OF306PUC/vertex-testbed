"""Ports, addressing, and local-interface resolution.
"""

from __future__ import annotations

import fcntl
import ipaddress
import socket
import struct
from enum import StrEnum

__all__ = ["AgentType", "HUB_PORT", "STATE_PORT", "CONTROL_PORTS",
           "DEFAULT_INTERFACE", "AGENT_MEDIA", "InterfaceError", "list_interfaces",
           "resolve_local_ip", "control_endpoint", "state_endpoint",
           "broadcast_address"]

_SIOCGIFADDR = 0x8915


class AgentType(StrEnum):
    """Transport an agent uses to reach its neighbours."""

    BLE = "ble"
    WIFI = "wifi"
    BRIDGE = "bridge"


#: Hub control plane: HTTP + websocket for the operator UI.
HUB_PORT = 3000

#: Single UDP port every agent binds for state broadcast.
STATE_PORT = 3010

#: Per-type control plane: parameter push and log retrieval. Kept per-type so the
#: three agents on one host can be addressed and restarted independently.
CONTROL_PORTS: dict[AgentType, int] = {
    AgentType.BLE: 3001,
    AgentType.WIFI: 3002,
    AgentType.BRIDGE: 3003,
}

#: Which media each agent type can actually transmit and receive on. Two agents
#: can only exchange state if these intersect -- `ble` and `wifi` do not, which is
#: what a `bridge` exists to join. Kept beside the types rather than in the
#: validator so the transport factory and the graph check cannot disagree.
AGENT_MEDIA: dict["AgentType", frozenset[str]] = {}


#: Interface carrying the experiment LAN. The onboard wireless interface on a
#: Raspberry Pi; overridden per deployment rather than guessed.
DEFAULT_INTERFACE = "wlan0"


class InterfaceError(RuntimeError):
    """A named interface is missing or carries no IPv4 address."""


def list_interfaces() -> dict[str, str]:
    """Map interface name -> IPv4 address, for interfaces that have one.

    Interfaces without an IPv4 address (down, or v6-only) are omitted rather than
    reported with a placeholder.
    """
    found: dict[str, str] = {}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        for _, name in socket.if_nameindex():
            try:
                packed = fcntl.ioctl(
                    sock.fileno(), _SIOCGIFADDR,
                    struct.pack("256s", name.encode()[:15]),
                )
            except OSError:
                continue
            found[name] = socket.inet_ntoa(packed[20:24])
    return found


def resolve_local_ip(
    interface: str = DEFAULT_INTERFACE,
    *,
    interfaces: dict[str, str] | None = None,
) -> str:
    """IPv4 address of ``interface``.

    Raises :class:`InterfaceError` naming what *is* available, rather than
    silently falling back to another interface -- a wrong-but-plausible address
    surfaces later as a neighbour that never answers, which is far harder to
    diagnose than a startup failure.

    ``interfaces`` overrides discovery, for tests and for dry-run tooling.
    """
    table = list_interfaces() if interfaces is None else interfaces
    try:
        return table[interface]
    except KeyError:
        raise InterfaceError(
            f"interface {interface!r} has no IPv4 address; "
            f"available: {sorted(table) or 'none'}"
        ) from None


def control_endpoint(ip: str, agent_type: AgentType | str) -> str:
    """Base URL for an agent's control plane."""
    return f"http://{ip}:{CONTROL_PORTS[AgentType(agent_type)]}"


def state_endpoint(ip: str) -> tuple[str, int]:
    """UDP ``(host, port)`` an agent's state broadcasts are sent to."""
    return (ip, STATE_PORT)


def broadcast_address(ip: str, prefixlen: int = 24) -> str:
    """Subnet broadcast address for ``ip``.

    Subnet broadcast is preferred over IP multicast for state distribution: it
    avoids IGMP snooping and any access-point-side group handling, while giving
    the same one-frame-reaches-all-neighbours airtime property.
    """
    return str(ipaddress.IPv4Network(f"{ip}/{prefixlen}", strict=False).broadcast_address)


# Populated after AgentType is defined; see the declaration above.
AGENT_MEDIA.update({
    AgentType.BLE: frozenset({"ble"}),
    AgentType.WIFI: frozenset({"udp"}),
    AgentType.BRIDGE: frozenset({"ble", "udp"}),
})
