"""Is a local server on 127.0.0.1 ours? Before anything is sent to a local LLM's port: the
socket listening there must belong to this user account, else another account could have put
a program on that port to read what we send (models_ctl).
"""

import os
from pathlib import Path


def loopback_listeners(port: int, proc: str = "/proc/net") -> list[int]:
    """UIDs of the sockets listening on a loopback-reachable address on this
    TCP port, from /proc/net/tcp{,6} (which shows every socket's owner)."""
    owners = []
    for name in ("tcp", "tcp6"):
        try:
            lines = Path(proc, name).read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            if len(fields) < 8 or fields[3] != "0A":     # 0A = LISTEN
                continue
            address, _sep, hex_port = fields[1].partition(":")
            if int(hex_port, 16) != port:
                continue
            if name == "tcp":
                reachable = address == "00000000" or address.endswith("7F")
            else:
                reachable = address in ("0" * 32, "0" * 24 + "01000000") or (
                    address.startswith("0" * 16 + "FFFF0000") and address.endswith("7F"))
            if reachable:
                owners.append(int(fields[7]))
    return owners


def require_own_listener(port: int):
    owners = loopback_listeners(port)
    if not owners:
        raise ConnectionError("not running")
    if any(uid != os.getuid() for uid in owners):
        raise PermissionError(f"port {port} belongs to another user account")
