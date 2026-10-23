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
from typing import cast

import i18n

SETTINGS_MAX_BYTES = 256 * 1024
MIN_WINDOW_SIZE = (420, 320)   # the card's width and its page height, logical px
# One rhythm for every page: GAP between blocks (sections, cards, forms, card rows), GAP_TIGHT
# between lines that belong together (hint lines, terminal tabs, a card's text).
GAP = 16
GAP_TIGHT = 6
VIEWS = ("main", "setup")    # the cards with the map beside them, and Setup; the rest are sub-pages


# Variables an agent session sets for what runs inside it (Claude Code: CLAUDECODE, CLAUDE_CODE_*
# such as CLAUDE_CODE_CHILD_SESSION, CLAUDE_PID, CLAUDE_EFFORT). When AI Agents Map was started from
# inside such a session (a restart from an agent's terminal), agents started here would inherit them
# and believe they run inside that session: Claude Code then stops saving its transcripts. AI_AGENT
# (vendor-neutral) and the other agents' markers make tools such as mise think an agent runs them:
# an install tab then gets a new progress line every few seconds instead of one that redraws.
AGENT_SESSION_EXACT = {"CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT", "AI_AGENTS_MAP_NO_PROVIDERS", "AI_AGENT",
                       "CODEX_THREAD_ID", "CODEX_SANDBOX", "CODEX_CI", "OPENCODE", "OPENCODE_PID",
                       "OPENCODE_CLIENT", "OPENCODE_SERVER", "PI_CODING_AGENT", "GEMINI_CLI", "QWEN_CODE",
                       "CRUSH", "GOOSE_TERMINAL", "AMP_CURRENT_THREAD_ID", "COPILOT_CLI", "CURSOR_AGENT"}
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
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)   # a FIFO in its place: not waited on
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


def atomic_write(path: Path, text: str | bytes, mode: int = 0o600, dir_mode: int = 0o700) -> None:
    """A unique temporary file next to it (mkstemp: created new, never through a link left there),
    its mode set before anything is written, fsync, then replace. 0600 unless mode says otherwise."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=dir_mode)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}-", dir=str(path.parent))
    try:
        data = text if isinstance(text, bytes) else text.encode("utf-8")
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), mode)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


NOT_PLAIN = str.maketrans({c: None for c in "<>&" + "".join(map(chr, [*range(32), *range(0x7f, 0xa0)]))})


def plain_text(text, most: int = 200) -> str:
    """Text for a desktop notification: notification daemons read title and body as markup
    (<img src=…> would load a remote image, &amp; turn into &), so markup characters and control
    characters go, white space is folded and the length capped."""
    text = " ".join(str(text or "").replace("\n", " ").split()).translate(NOT_PLAIN)
    return text if len(text) <= most else text[:most - 1].rstrip() + "…"


def read_head(path, limit: int, follow: bool = True) -> bytes:
    """At most `limit` bytes from the start of a regular file, never more read into memory (what
    `path.read_bytes()[:limit]` meant). Opened without blocking (a FIFO left in its place cannot hang
    the reader) and checked on the opened file itself; anything else raises OSError."""
    flags = os.O_RDONLY | os.O_NONBLOCK | (0 if follow else os.O_NOFOLLOW)
    fd = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(f"{path} is not a regular file")
        chunks, left = [], limit
        while left > 0:
            chunk = os.read(fd, min(left, 1 << 20))
            if not chunk:
                break
            chunks.append(chunk)
            left -= len(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def read_whole(path, limit: int, follow: bool = True) -> bytes:
    """A whole regular file of at most `limit` bytes, for reading in order to rewrite it: a larger
    one raises OSError instead of handing back its start (a prefix written back would lose the rest)."""
    data = read_head(path, limit + 1, follow)
    if len(data) > limit:
        raise OSError(f"{path} is larger than {limit} bytes")
    return data


def setting(key: str, path: Path | None = None):
    """One setting (settings_schema.SCHEMA): the stored value when it has the declared shape, else
    its default."""
    import settings_schema
    return settings_schema.value(load_settings(path), key)


def update_settings(path: Path | None = None, **values) -> None:
    """Write settings (None removes one). Only keys settings_schema declares: a typo would otherwise
    become a new setting nobody reads."""
    import settings_schema
    unknown = [key for key in values if key not in settings_schema.SCHEMA and key not in settings_schema.RENAMED
               and key not in settings_schema.RETIRED]
    if unknown:
        raise KeyError(f"not a declared setting: {', '.join(unknown)} (settings_schema.SCHEMA)")
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
    value = setting("last_view", path)
    value = "map" if value == "builder" else value
    return value if value in VIEWS else "main"


# --- Hyprland -----------------------------------------------------------------

def folder_bytes(folder, most: int = 0) -> int:
    """Bytes of the files below folder (lstat, links not followed); stops counting past `most`."""
    total = 0
    for root, _dirs, files in os.walk(folder):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
            if most and total > most:
                return total
    return total


def run_with_disk_budget(argv: list[str], cwd: str, budget: int, timeout: float, every: float = 0.5) -> bool:
    """Run argv (no shell, output discarded) in cwd while what it writes there stays within `budget`
    bytes: measured every `every` seconds, and its whole process group ended at once when it goes over
    (or after `timeout`). For a program that unpacks itself (an AppImage's --appimage-extract), whose
    size cannot be known before. True when it ended by itself within the budget, with exit status 0."""
    import signal
    import time
    process = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, start_new_session=True)
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                code = process.wait(timeout=every)
                return code == 0 and folder_bytes(cwd, budget) <= budget
            except subprocess.TimeoutExpired:
                pass
            if time.monotonic() > deadline or folder_bytes(cwd, budget) > budget:
                return False
    finally:
        # Always the whole group, also after the program itself has ended: a child it left behind
        # must not go on writing unwatched.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            pass
        if process.poll() is None:
            process.wait()


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


from settings_schema import LOOKS, MAP_LAYERS  # noqa: E402,F401  (declared with the settings)


def saved_look(path: Path | None = None) -> str:
    """Settings › Look on Omarchy: "omarchy" (its theme; the default) or "adwaita" (GNOME's look).
    Other desktops always have Adwaita."""
    import desktop
    if desktop.NAME != "omarchy":
        return "adwaita"
    return cast(str, setting("look", path))          # settings_schema declares a str


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
    return corners.choice(setting("corners", path))


def map_animations() -> str:
    """Settings › Map animations: "all", "agents" or "off" (an earlier "Animate active lines" off
    became "off" in migrate_settings())."""
    return cast(str, setting("map_animations"))      # settings_schema declares a str


def map_layers(path: Path | None = None) -> list:
    """The categories switched on in the Map (all by default)."""
    value = setting("map_layers", path)
    return [v for v in MAP_LAYERS if v in value]


def migrate_settings(path: Path | None = None) -> None:
    """On start: earlier keys become today's, retired ones go (settings_schema.migrate)."""
    import settings_schema
    path = path or settings_file()
    changes = settings_schema.migrate(load_settings(path))
    if changes:
        update_settings(path, **changes)


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
