"""Ports, addressing, and local-interface resolution.
"""

from __future__ import annotations

import fcntl
import ipaddress
import os
import socket
import struct
from enum import StrEnum

__all__ = ["AgentType", "HUB_PORT", "STATE_PORT", "CONTROL_PORTS",
           "DEFAULT_INTERFACE", "AGENT_MEDIA", "InterfaceError", "list_interfaces",
           "interface_broadcast", "interface_prefixlen", "interface_for_ip",
           "wlan_state", "set_wlan_txpower",
           "SIOCGIFBRDADDR",
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

#: Per-type control plane: parameter push and log retrieval.
CONTROL_PORTS: dict[AgentType, int] = {
    AgentType.BLE: 3001,
    AgentType.WIFI: 3002,
    AgentType.BRIDGE: 3003,
}

#: Which media each agent type can actually transmit and receive on.
AGENT_MEDIA: dict["AgentType", frozenset[str]] = {}


#: Interface carrying the experiment LAN. 
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


#: ioctl for "give me this interface's broadcast address" (linux/sockios.h).
SIOCGIFBRDADDR = 0x8919


def interface_broadcast(interface: str = DEFAULT_INTERFACE) -> str:
    """The **kernel's own** broadcast address for ``interface``.

    Ask, do not derive. `broadcast_address()` below has to be told a prefix
    length, and a wrong one is silent: on a /22 lab network a /24 guess sends to
    10.6.5.255, which is an ordinary host address nobody holds. Both agents stay
    healthy, every datagram is accepted by the socket, and the link simply never
    carries anything -- measured as 0/3 UDP links delivering while 3/3 BLE links
    worked.

    SIOCGIFBRDADDR via ioctl: Linux-specific and stdlib-only, so it adds no
    dependency to ten Raspberry Pis.

    :raises InterfaceError: if the interface has no broadcast address.
    """
    import fcntl
    import struct

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        packed = fcntl.ioctl(s.fileno(), SIOCGIFBRDADDR,
                             struct.pack("256s", interface.encode()[:15]))
        return socket.inet_ntoa(packed[20:24])
    except OSError as exc:
        raise InterfaceError(
            f"cannot read a broadcast address for {interface!r}: {exc}. "
            f"available: {sorted(list_interfaces()) or 'none'}"
        ) from None
    finally:
        s.close()


def interface_for_ip(ip: str) -> str | None:
    """Which interface holds ``ip``, or None.

    So the broadcast address can be found from the address alone. Requiring the
    caller to pass an interface name meant it could be forgotten -- and it was:
    `AgentService` was constructed without one, silently fell back to the assumed
    /24, and every UDP link in a run delivered nothing.
    """
    for name, addr in list_interfaces().items():
        if addr == ip:
            return name
    return None


def interface_prefixlen(interface: str = DEFAULT_INTERFACE) -> int | None:
    """Prefix length of ``interface``, or None. For recording with a run."""
    import fcntl
    import struct
    SIOCGIFNETMASK = 0x891B
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        packed = fcntl.ioctl(s.fileno(), SIOCGIFNETMASK,
                            struct.pack("256s", interface.encode()[:15]))
        mask = socket.inet_ntoa(packed[20:24])
        return ipaddress.IPv4Network(f"0.0.0.0/{mask}").prefixlen
    except (OSError, ValueError):
        return None
    finally:
        s.close()


#: Absolute path to `iw`, or None if it is not installed.
#:
#: It lives in /usr/sbin, which is on root's secure_path but NOT on an
#: unprivileged user's PATH on Debian. A bare "iw" therefore resolves for an
#: agent started under sudo and fails for one started without, and wlan_state()
#: then returns {} so the run records no wireless state at all. That is a
#: silent, launch-method-dependent hole: the same Wi-Fi agent logged power_save
#: and channel when started from an interactive login shell and logged neither
#: when started over `ssh host 'bash ...'`.
#:
#: Resolve it explicitly so what gets recorded does not depend on how the agent
#: was launched. scripts/rf_survey.sh carries the same fix for the same reason.
def _iw_binary() -> "str | None":
    import shutil
    found = shutil.which("iw")
    if found:
        return found
    for cand in ("/usr/sbin/iw", "/sbin/iw", "/usr/local/sbin/iw"):
        if os.path.exists(cand):
            return cand
    return None


def wlan_state(interface: str = DEFAULT_INTERFACE) -> dict[str, Any]:
    """Wireless state that changes what a run measures: power save, channel, power.

    Best effort and never raising -- a missing `iw` or a wired interface yields an
    empty dict rather than failing a run.

    Recorded because it is first-order and not reconstructable. A UDP one-way delay
    of 172 ms was measured on a link that should be ~1 ms, and buffered traffic on a
    power-saving station is exactly that magnitude: a sleeping station's packets
    wait for the next beacon. `power_save` therefore belongs with the data, not in
    someone's memory of how the bench was set up. The channel matters for the same
    reason on the other side -- WLAN 1/6/11 sit under the BLE advertising channels
    (see PLATFORM.md 3 A3.1).
    """
    import re
    import subprocess

    out: dict[str, Any] = {}
    iw = _iw_binary()
    if iw is None:
        return out
    def _run(args: list[str]) -> str:
        try:
            return subprocess.run([iw, *args], capture_output=True, text=True,
                                  timeout=5).stdout
        except Exception:
            return ""

    ps = _run(["dev", interface, "get", "power_save"])
    m = re.search(r"Power save:\s*(\w+)", ps)
    if m:
        out["power_save"] = m.group(1).lower()

    info = _run(["dev", interface, "info"])
    m = re.search(r"channel\s+(\d+)\s+\((\d+)\s*MHz\)", info)
    if m:
        out["wlan_channel"] = int(m.group(1))
        out["wlan_freq_mhz"] = int(m.group(2))
    m = re.search(r"txpower\s+([-\d.]+)\s*dBm", info)
    if m:
        tp = float(m.group(1))
        out["wlan_txpower_dbm"] = tp
        # 31.00 dBm is ~1.3 W: above what the CYW43455 can radiate and above
        # every 2.4 GHz regulatory limit. brcmfmac reports it as a fixed
        # placeholder when it does not expose real transmit power. Recording it
        # unflagged would put a fabricated number in every run's environment.
        out["wlan_txpower_trusted"] = tp < 30.0
        if tp >= 30.0:
            out["wlan_txpower_note"] = (
                "driver placeholder, not a measurement -- treat as unknown")
    m = re.search(r"type\s+(\w+)", info)
    if m:
        out["wlan_type"] = m.group(1)
    return out


def set_wlan_txpower(iface: str, dbm: float) -> dict[str, object]:
    """Set the interface's transmit power and report what actually took effect.

    Returns a dict for the run environment, never raises. Keys:
    `wlan_txpower_requested_dbm`, `wlan_txpower_set` (did the command succeed),
    `wlan_txpower_readback_dbm`, `wlan_txpower_matched`, and on failure
    `wlan_txpower_error`.

    It is recorded rather than enforced because the failure modes are quiet. The
    driver may accept the command and ignore it, and `iw` reports 31.00 dBm as a
    placeholder when it has no real value, so neither "the command returned 0"
    nor "iw reports a number" is evidence on its own. Only the readback is, and
    only once it is below the placeholder. Analysis can then reject a run whose
    `wlan_txpower_matched` is false instead of averaging it in unnoticed.

    Needs CAP_NET_ADMIN. Agents that already run under sudo have it; the rest
    go through `sudo -n`, which fails fast rather than waiting on a password
    prompt that nothing will ever answer.
    """
    import shutil
    import subprocess

    out: dict[str, object] = {"wlan_txpower_requested_dbm": float(dbm)}
    mbm = str(int(round(float(dbm) * 100)))
    iw = _iw_binary()
    if iw is None:
        out.update(wlan_txpower_set=False, wlan_txpower_error="iw not found")
        return out
    cmd = [iw, "dev", iface, "set", "txpower", "fixed", mbm]
    if os.geteuid() != 0:
        if not shutil.which("sudo"):
            out.update(wlan_txpower_set=False, wlan_txpower_error="not root, no sudo")
            return out
        cmd = ["sudo", "-n", *cmd]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        out["wlan_txpower_set"] = r.returncode == 0
        if r.returncode != 0:
            out["wlan_txpower_error"] = (r.stderr or r.stdout).strip()[:200]
    except Exception as exc:                       # noqa: BLE001 - never fail a run
        out.update(wlan_txpower_set=False, wlan_txpower_error=f"{type(exc).__name__}: {exc}")

    back = wlan_state(iface).get("wlan_txpower_dbm")
    out["wlan_txpower_readback_dbm"] = back
    out["wlan_txpower_matched"] = (back is not None and abs(float(back) - float(dbm)) < 0.5)
    return out


def broadcast_address(ip: str, prefixlen: int = 24) -> str:
    """Subnet broadcast address for ``ip``, from an ASSUMED prefix length.

    Prefer :func:`interface_broadcast`, which asks the kernel. This is the
    fallback for when the interface name is not known, and its default of /24 is a
    guess that has already been wrong on the lab network (/22). Wrong here is
    silent, so callers should say which one they used.
    """
    return str(ipaddress.IPv4Network(f"{ip}/{prefixlen}", strict=False).broadcast_address)


# Populated after AgentType is defined; see the declaration above.
AGENT_MEDIA.update({
    AgentType.BLE: frozenset({"ble"}),
    AgentType.WIFI: frozenset({"udp"}),
    AgentType.BRIDGE: frozenset({"ble", "udp"}),
})
