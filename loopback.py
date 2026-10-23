"""Is a local server on 127.0.0.1 ours? Before anything is sent to a local LLM's port: the
socket listening there must belong to this user account, else another account could have put
a program on that port to read what we send (models_ctl).
"""

import http.client
import io
import os
import socket
import time
from pathlib import Path
from typing import cast


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


class StreamCancelled(Exception):
    """stream_lines: cancelled() said so."""


class _DeadlineReader(io.RawIOBase):
    """A socket's bytes where each wait ends at the deadline, after `quiet` seconds without a byte,
    or (checked every second, also while bytes trickle in) when cancelled() says so."""

    def __init__(self, sock, deadline: float, quiet: float, cancelled):
        self.sock, self.deadline, self.quiet, self.cancelled = sock, deadline, quiet, cancelled

    def readable(self):
        return True

    def readinto(self, buffer) -> int:
        silent = 0.0
        while True:
            if self.cancelled():
                raise StreamCancelled()
            wait = min(1.0, self.deadline - time.monotonic())
            if wait <= 0:
                raise TimeoutError("the reply took too long")
            self.sock.settimeout(wait)
            try:
                return self.sock.recv_into(buffer)
            except socket.timeout:
                silent += wait
                if silent >= self.quiet:
                    raise TimeoutError("the server went quiet")


class _Sock:
    """What http.client.HTTPResponse reads from: our deadline-bound reader."""

    def __init__(self, reader):
        self.reader = reader

    def makefile(self, _mode, *_args, **_kwargs):
        return io.BufferedReader(self.reader, 65536)


def stream_lines(port: int, path: str, body: bytes, *, deadline: float, cancelled=lambda: False,
                 head_within: float = 30.0, quiet: float = 120.0, max_line: int = 65536, max_lines: int = 200000):
    """POST body (JSON) to this user's own server on 127.0.0.1:<port> and yield the reply's lines.
    One deadline (time.monotonic()) covers connecting, the status line and headers (also within
    head_within seconds), and every read of the body; a line over max_line bytes, more than
    max_lines lines, a non-2xx status or a malformed reply ends it with an error."""
    require_own_listener(port)
    started = time.monotonic()
    head_by = min(deadline, started + head_within)
    sock = socket.create_connection(("127.0.0.1", port), timeout=max(0.1, min(10.0, head_by - started)))
    try:
        sock.sendall(b"POST " + path.encode("ascii") + b" HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n"
                     b"Content-Type: application/json\r\nContent-Length: %d\r\nConnection: close\r\n\r\n"
                     % (port, len(body)) + body)
        reader = _DeadlineReader(sock, head_by, quiet, cancelled)
        response = http.client.HTTPResponse(cast(socket.socket, _Sock(reader)))   # it only calls makefile()
        response.begin()                          # status line and headers: by head_by
        if not 200 <= response.status < 300:
            raise ConnectionError(f"the server answered {response.status}")
        reader.deadline = deadline
        for _count in range(max_lines):
            raw = response.readline(max_line + 1)
            if not raw:
                return
            if len(raw) > max_line:
                raise ValueError(f"a reply line over {max_line} bytes")
            yield raw
        raise ValueError(f"more than {max_lines} reply lines")
    finally:
        sock.close()
