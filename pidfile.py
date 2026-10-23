"""PID file for the single AI Agents Map instance, kept in a private per-user directory.

The record is only ``pid:starttime``. Whoever reads it must still check the
process against the plugin's own interpreter and script, never against paths
taken from the record itself.
"""
import os
import stat
from pathlib import Path

PID_NAME = "ai-agents-map.pid"


def is_private_dir(path):
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return (stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
            and stat.S_IMODE(info.st_mode) & 0o077 == 0)


def private_dir():
    """Return a directory only this user can write to, or None."""
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    if runtime_dir and is_private_dir(runtime_dir):
        return Path(runtime_dir)
    fallback = Path("/tmp") / f"ai-agents-map-{os.getuid()}"
    try:
        fallback.mkdir(mode=0o700)
    except FileExistsError:
        pass
    except OSError:
        return None
    # A directory someone else created (or a symlink) under that name is refused.
    return fallback if is_private_dir(fallback) else None


def pid_file():
    directory = private_dir()
    return directory / PID_NAME if directory else None


def process_starttime(pid):
    try:
        with open(f"/proc/{pid}/stat", "r") as f:
            content = f.read()
        return content[content.rfind(")") + 2:].split()[19]
    except (OSError, IndexError):
        return ""


def write(pid=None):
    path = pid_file()
    if path is None:
        return
    pid = pid or os.getpid()
    import tempfile
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}-", dir=str(path.parent))   # new and unique, 0600
    try:
        with os.fdopen(fd, "w") as f:
            f.write(f"{pid}:{process_starttime(pid)}\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def read():
    """Return (pid, starttime) from a regular file this user owns, else None."""
    path = pid_file()
    if path is None:
        return None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)   # a FIFO in its place: not waited on
    except OSError:
        return None
    with os.fdopen(fd, "rb") as f:
        info = os.fstat(f.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            return None
        parts = f.read(64).decode("ascii", "replace").strip().split(":")
    if not parts[0].isdigit():
        return None
    return int(parts[0]), parts[1] if len(parts) > 1 else ""


def remove_if_own(pid=None):
    """Delete the PID file only if it still names this process."""
    pid = pid or os.getpid()
    record = read()
    if record and record[0] == pid and record[1] in ("", process_starttime(pid)):
        pid_file().unlink(missing_ok=True)
