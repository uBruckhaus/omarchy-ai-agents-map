"""AI Manager - settings and small helpers shared by the popup.

Settings live in ~/.config/omarchy/ai-manager/settings.json (private, written
atomically, read with a size cap). Nothing here starts a network request.
"""

import json
import os
import re
import socket
import stat
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import i18n

SETTINGS_MAX_BYTES = 256 * 1024
MIN_WINDOW_SIZE = (420, 320)   # the card's width and its page height, logical px
VIEWS = ("main", "map")    # the two main views; "settings" is a sub-page


def settings_file() -> Path:
    return Path.home() / ".config/omarchy/ai-manager/settings.json"


i18n.SETTINGS_FILE = settings_file()


def load_settings(path: Path | None = None) -> dict:
    path = path or settings_file()
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return {}
    try:
        with os.fdopen(fd, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                return {}
            data = json.loads(handle.read(SETTINGS_MAX_BYTES + 1)[:SETTINGS_MAX_BYTES] or b"{}")
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def atomic_write(path: Path, text: str) -> None:
    """tmp + fsync + replace, always 0600."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def update_settings(path: Path | None = None, **values) -> None:
    path = path or settings_file()
    data = load_settings(path)
    for key, value in values.items():
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
    atomic_write(path, json.dumps(data, indent=2) + "\n")


def _int_pair(value, low: int, high: int):
    if isinstance(value, list) and len(value) == 2 and \
            all(isinstance(v, int) and not isinstance(v, bool) and low <= v < high for v in value):
        return value[0], value[1]
    return None


def window_size(path: Path | None = None):
    """Card width and page height last resized to, or None for the default."""
    size = _int_pair(load_settings(path).get("size"), 0, 20000)
    if size and size[0] >= MIN_WINDOW_SIZE[0] and size[1] >= MIN_WINDOW_SIZE[1]:
        return size
    return None


def window_position(path: Path | None = None):
    """Top-left of the card last moved to (monitor-relative), or None for centred."""
    return _int_pair(load_settings(path).get("position"), 0, 100000)


def save_window_size(size, path: Path | None = None) -> None:
    update_settings(path, size=None if size is None else [int(size[0]), int(size[1])])


def save_window_position(position, path: Path | None = None) -> None:
    update_settings(path, position=None if position is None else [int(position[0]), int(position[1])])


def start_view(path: Path | None = None) -> str:
    """The view the popup opens on: "main", "map" or "last"."""
    value = load_settings(path).get("start_view")
    value = "map" if value == "builder" else value   # the Map replaced the Builder placeholder
    return value if value in (*VIEWS, "last") else "main"


def last_view(path: Path | None = None) -> str:
    value = load_settings(path).get("last_view")
    value = "map" if value == "builder" else value
    return value if value in VIEWS else "main"


# --- Hyprland -----------------------------------------------------------------

def read_bounded_output(argv: list[str], max_bytes: int = 64 * 1024, timeout: float = 5.0) -> tuple[bytes, bool]:
    """Run argv (no shell), read stdout with a byte cap and a deadline."""
    try:
        proc = subprocess.Popen(list(argv), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL)
    except (OSError, ValueError):
        return b"", False
    chunks, state = [], {"total": 0, "over": False}

    def reader():
        try:
            while chunk := proc.stdout.read(65536):
                state["total"] += len(chunk)
                if state["total"] > max_bytes:
                    state["over"] = True
                    break
                chunks.append(chunk)
        except (OSError, ValueError):
            state["over"] = True

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive() or state["over"]:
        proc.kill()
        thread.join(2.0)
        state["over"] = True
    try:
        code = proc.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        proc.kill()
        code = proc.wait()
    return b"".join(chunks)[:max_bytes], code == 0 and not state["over"]


def _hyprland_socket() -> Path | None:
    runtime = os.environ.get("XDG_RUNTIME_DIR", "")
    signature = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE", "")
    if not os.path.isabs(runtime) or not re.fullmatch(r"[A-Za-z0-9_]{1,200}", signature):
        return None
    return Path(runtime) / "hypr" / signature / ".socket.sock"


def _hyprland_query(command: bytes, max_bytes: int = 4096, timeout: float = 0.5) -> bytes | None:
    """One bounded request on Hyprland's IPC socket; fast enough for pointer motion."""
    path = _hyprland_socket()
    if path is None:
        return None
    deadline = time.monotonic() + timeout
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(str(path))
            sock.sendall(command)
            chunks, total = [], 0
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                sock.settimeout(remaining)
                chunk = sock.recv(4096)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    return None
                chunks.append(chunk)
    except OSError:
        return None
    return b"".join(chunks)


def cursor_position():
    """Global pointer position from Hyprland, or None."""
    raw = _hyprland_query(b"j/cursorpos")
    if raw is None:
        raw, ok = read_bounded_output(["hyprctl", "cursorpos", "-j"], max_bytes=4096, timeout=1.0)
        if not ok:
            return None
    try:
        data = json.loads(raw)
        return int(data["x"]), int(data["y"])
    except (ValueError, KeyError, TypeError):
        return None


def hyprland_rounding() -> int:
    """Follow Hyprland's window rounding like the Omarchy shell does."""
    raw = _hyprland_query(b"j/getoption decoration:rounding")
    if raw is None:
        raw, _ok = read_bounded_output(["hyprctl", "getoption", "decoration:rounding", "-j"],
                                       max_bytes=4096, timeout=2.0)
    # {"option": "decoration:rounding", "int": 0, "set": true}
    try:
        return max(0, min(64, int(json.loads(raw).get("int", 0))))
    except (ValueError, AttributeError, TypeError):
        return 0
