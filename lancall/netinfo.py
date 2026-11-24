"""Discovery of this machine's LAN addresses. Never sends any packets."""

from __future__ import annotations

import ipaddress
import socket

# Connecting a UDP socket only asks the OS which interface would route there.
# Private-range probes make this work on networks with no internet route.
_PROBES = ("8.8.8.8", "10.254.254.254", "172.31.254.254", "192.168.254.254")


def _usable(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return addr.version == 4 and not (addr.is_loopback or addr.is_link_local or addr.is_unspecified)


def local_ips() -> list[str]:
    """All usable IPv4 addresses of this machine; the default-route one first."""
    found: list[str] = []

    def add(ip: str) -> None:
        if _usable(ip) and ip not in found:
            found.append(ip)

    for target in _PROBES:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect((target, 9))
            add(sock.getsockname()[0])
        except OSError:
            pass
        finally:
            sock.close()

    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            add(ip)
    except OSError:
        pass

    return found


def hostname() -> str:
    try:
        return socket.gethostname().split(".")[0] or "localhost"
    except OSError:
        return "localhost"
