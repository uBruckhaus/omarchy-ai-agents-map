"""AI Agents Map - settings and small helpers shared by the popup.

Settings live in desktop.config_dir()/settings.json (~/.config/omarchy/ai-agents-map on Omarchy; private, written
atomically, read with a size cap). Nothing here starts a network request.
"""

import json
import os
import stat
import subprocess
import tempfile
import threading
from pathlib import Path

import i18n

SETTINGS_MAX_BYTES = 256 * 1024
MIN_WINDOW_SIZE = (420, 320)   # the card's width and its page height, logical px
VIEWS = ("main", "setup")    # the cards with the map beside them, and My setup; the rest are sub-pages


# Variables an agent session sets for what runs inside it (Claude Code: CLAUDECODE, CLAUDE_CODE_*
# such as CLAUDE_CODE_CHILD_SESSION, CLAUDE_PID, CLAUDE_EFFORT). When AI Agents Map was started from
# inside such a session (a restart from an agent's terminal), agents started here would inherit them
# and believe they run inside that session: Claude Code then stops saving its transcripts.
AGENT_SESSION_EXACT = {"CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT", "AI_AGENTS_MAP_NO_PROVIDERS"}
AGENT_SESSION_PREFIXES = ("CLAUDE_CODE_",)


def is_agent_session_var(name: str) -> bool:
    return name in AGENT_SESSION_EXACT or name.startswith(AGENT_SESSION_PREFIXES)


def drop_agent_session_vars(environ=None) -> list:
    """Remove those variables (from os.environ by default); returns the names removed."""
    environ = os.environ if environ is None else environ
    names = [k for k in list(environ) if is_agent_session_var(k)]
    for name in names:
        del environ[name]
    return names


def settings_file() -> Path:
    import desktop
    return desktop.config_dir() / "settings.json"


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


def start_view(path: Path | None = None) -> str:
    """The view the popup opens on: always "main" now (an old "map" or "builder" too)."""
    return "main"


def last_view(path: Path | None = None) -> str:
    value = load_settings(path).get("last_view")
    value = "map" if value == "builder" else value
    return value if value in VIEWS else "main"


# --- Hyprland -----------------------------------------------------------------

def read_bounded_output(argv: list[str], max_bytes: int = 64 * 1024, timeout: float = 5.0,
                        stdin: bytes | None = None, cwd: str | None = None, env: dict | None = None,
                        merge_stderr: bool = False) -> tuple[bytes, bool]:
    """Run argv (no shell), read stdout with a byte cap and one deadline.
    `stdin`: bytes handed to the program (private data never goes into argv);
    `merge_stderr`: read error messages too (same cap)."""
    try:
        proc = subprocess.Popen(list(argv), stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT if merge_stderr else subprocess.DEVNULL,
                                cwd=cwd, env=env, start_new_session=True)
    except (OSError, ValueError):
        return b"", False
    if stdin is not None:
        def feed():
            try:
                proc.stdin.write(stdin)
                proc.stdin.close()
            except (OSError, ValueError):
                pass
        threading.Thread(target=feed, daemon=True).start()
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
        try:
            os.killpg(proc.pid, 9)          # its children too (curl, npx, …)
        except OSError:
            proc.kill()
        thread.join(2.0)
        state["over"] = True
    try:
        code = proc.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        proc.kill()
        code = proc.wait()
    return b"".join(chunks)[:max_bytes], code == 0 and not state["over"]


LOOKS = ("omarchy", "adwaita")


def saved_look(path: Path | None = None) -> str:
    """Settings › Look on Omarchy: "omarchy" (its theme; the default) or "adwaita" (GNOME's look).
    Other desktops always have Adwaita."""
    import desktop
    if desktop.NAME != "omarchy":
        return "adwaita"
    value = load_settings(path).get("look")
    return value if value in LOOKS else "omarchy"


def look_colors() -> dict:
    """The colours of the look in use: the desktop theme's, or Adwaita's (light/dark and accent
    as the system has them) when Omarchy is set to GNOME's look."""
    import desktop
    if desktop.NAME == "omarchy" and saved_look() == "adwaita":
        import desktop_generic
        return desktop_generic.theme_colors()
    return desktop.theme_colors()


def saved_corners(path: Path | None = None) -> str:
    """"windows" (follow Hyprland's window rounding; the default), "rounded" or "square"
    — the same setting as App Manager and Theme Manager."""
    import corners
    return corners.choice(load_settings(path).get("corners"))


MAP_LAYERS = ("agents", "online", "local", "models", "mcp", "skills")


def map_layers(path: Path | None = None) -> list:
    """The categories switched on in the Map (all by default)."""
    value = load_settings(path).get("map_layers")
    if isinstance(value, list):
        return [v for v in MAP_LAYERS if v in value]
    return list(MAP_LAYERS)


# What Hide, the column lists, the overview's Show list and My setup · + Available kept: the map
# shows what is set up now (My setup adds and removes), so these are dropped on the first start.
RETIRED_SETTINGS = ("hidden_items", "hidden_nodes", "hidden_providers", "card_hidden", "start_scope",
                    "show_more_hint")


def drop_retired_settings(path: Path | None = None) -> None:
    path = path or settings_file()
    stale = [key for key in RETIRED_SETTINGS if key in load_settings(path)]
    if stale:
        update_settings(path, **{key: None for key in stale})


# --- clipboard ---------------------------------------------------------------------------------

MAX_PASTE_BYTES = 8192                 # text pasted into a field
_TEXT_MIMES = ("text/plain;charset=utf-8", "text/plain", "UTF8_STRING", "TEXT", "STRING")


def clipboard_text(max_bytes: int = MAX_PASTE_BYTES, timeout: float = 2.0, lines: bool = False) -> str | None:
    """Plain text from the Wayland clipboard, read with `wl-paste` under a
    byte cap and a deadline (GTK's own paste buffers the whole offer with
    neither). Control characters become spaces (line breaks stay when
    `lines`). '' when the clipboard has no text; None when the offer was too
    long, too slow or failed."""
    types_raw, _ = read_bounded_output(["wl-paste", "--list-types"], max_bytes=65536, timeout=timeout)
    # An empty clipboard makes wl-paste fail: nothing to paste, not an error.
    offered = {line.strip() for line in types_raw.decode("utf-8", "replace").splitlines()}
    for mime in _TEXT_MIMES:
        if mime not in offered:
            continue
        data, ok = read_bounded_output(["wl-paste", "--no-newline", "--type", mime],
                                       max_bytes=max_bytes, timeout=timeout)
        if not ok:
            return None
        text = data.decode("utf-8", "replace")
        if lines:
            return "".join(ch if ch == "\n" or not (ord(ch) < 32 or 127 <= ord(ch) < 160) else " "
                           for ch in text.replace("\r\n", "\n"))
        return " ".join("".join(" " if ord(ch) < 32 or 127 <= ord(ch) < 160 else ch for ch in text).split())
    return ""
