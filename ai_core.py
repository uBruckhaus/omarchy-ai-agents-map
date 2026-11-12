"""AI Agents Map - settings and small helpers shared by the popup.

Settings live in desktop.config_dir()/settings.json (identity.py selects the edition's folder; private, written
atomically, read with a size cap). Nothing here starts a network request.
"""

import json
import os
import stat
import subprocess
import threading
import time
from pathlib import Path
from typing import cast

import i18n

SETTINGS_MAX_BYTES = 256 * 1024
MIN_WINDOW_SIZE = (420, 320)   # the card's width and its page height, logical px
# One rhythm for every page: GAP between blocks (sections, cards, forms, card rows, list and detail
# panes), GAP_TIGHT between lines that belong together (hint lines, terminal tabs, a card's text),
# GAP_INLINE between controls side by side (buttons in a row, a label and its dropdown).
GAP = 16
GAP_TIGHT = 6
GAP_INLINE = 8
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


def add_user_program_dirs(environ=None) -> list:
    """Started from the app launcher, the app has the desktop session's PATH, which lacks what a
    shell's `mise activate` adds: mise's shims and ~/.local/bin go at its end (what is already on it
    still wins), so agents installed with mise are found and started, and their children find them
    too. Returns the folders added."""
    environ = os.environ if environ is None else environ
    home = Path.home()
    mise_data = Path(environ.get("MISE_DATA_DIR") or home / ".local/share/mise")
    if not mise_data.is_absolute():               # relative: would mean another folder in every child's cwd
        mise_data = home / ".local/share/mise"
    current = environ.get("PATH", "")
    path = current.split(os.pathsep)
    added = [str(d) for d in (mise_data / "shims", home / ".local/bin")
             if d.is_dir() and str(d) not in path]
    if added:                                     # appended as they are: empty parts keep their meaning
        environ["PATH"] = os.pathsep.join(([current] if current else []) + added)
    return added


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
            raw = handle.read(SETTINGS_MAX_BYTES + 1)
            if len(raw) > SETTINGS_MAX_BYTES:
                return {}
            data = json.loads(raw or b"{}")
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _trusted_private_directory(fd: int) -> None:
    info = os.fstat(fd)
    if (info.st_uid not in (0, os.getuid()) or
            (info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX)):
        raise PermissionError('Unsafe private-write ancestor')


def _resolve_private_parent(path: Path) -> Path:
    """Resolve existing links once, checking even directories hidden by a link.

    This descriptor-based realpath also permits a missing suffix. The caller
    subsequently pins the returned chain with a second no-follow walk.
    """
    import errno
    from collections import deque
    pending = deque(path.parts[1:])
    resolved = []
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    links = 0
    try:
        _trusted_private_directory(fd)
        while pending:
            part = pending.popleft()
            if part in ('', '.'):
                continue
            if part == '..':
                child = os.open('..', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
                os.close(fd)
                fd = child
                if resolved:
                    resolved.pop()
                _trusted_private_directory(fd)
                continue
            try:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            except FileNotFoundError:
                return Path(os.path.normpath(str(Path('/', *resolved, part, *pending))))
            except OSError as error:
                if error.errno not in (errno.ELOOP, errno.ENOTDIR):
                    raise
                target = os.readlink(part, dir_fd=fd)
                links += 1
                if links > 40:
                    raise OSError(errno.ELOOP, 'Too many parent symlinks')
                if target.startswith('/'):
                    os.close(fd)
                    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
                    resolved.clear()
                    _trusted_private_directory(fd)
                pending.extendleft(reversed(Path(target).parts[1:] if target.startswith('/') else Path(target).parts))
                continue
            os.close(fd)
            fd = child
            resolved.append(part)
            _trusted_private_directory(fd)
        return Path('/', *resolved)
    finally:
        os.close(fd)


def atomic_write(path: Path, text: str | bytes, mode: int = 0o600, dir_mode: int = 0o700) -> None:
    """Create a unique no-follow temporary file via a pinned parent descriptor,
    set its mode before writing, fsync, replace and fsync the directory. 0600 by default."""
    # Resolve the user's symlink layout once, then pin the resolved chain.
    # Both passes reject untrusted owners and non-sticky writable ancestors.
    import secrets
    path = Path(os.path.abspath(path))
    resolved_parent = _resolve_private_parent(path.parent)
    parent = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    tmp = None
    created = False
    fd = None
    try:
        _trusted_private_directory(parent)
        for part in resolved_parent.parts[1:]:
            try:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
            except FileNotFoundError:
                try:
                    os.mkdir(part, mode=dir_mode, dir_fd=parent)
                except FileExistsError:
                    pass
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
            os.close(parent)
            parent = child
            _trusted_private_directory(parent)
        tmp = f'.{path.name}-{secrets.token_hex(16)}'
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                     mode, dir_fd=parent)
        created = True
        os.fchmod(fd, mode)
        data = text if isinstance(text, bytes) else text.encode('utf-8')
        with os.fdopen(fd, 'wb') as handle:
            fd = None
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path.name, src_dir_fd=parent, dst_dir_fd=parent)
        os.fsync(parent)
    finally:
        if fd is not None:
            os.close(fd)
        if created and tmp is not None:
            try:
                os.unlink(tmp, dir_fd=parent)
            except FileNotFoundError:
                pass
        os.close(parent)


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


def open_regular(path, follow: bool = True) -> int:
    """A descriptor of a regular file, opened without blocking and checked on the opened file (a
    FIFO or device put in its place raises OSError instead of hanging the reader). Close it."""
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC | (0 if follow else os.O_NOFOLLOW))
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(f"{path} is not a regular file")
    except BaseException:
        os.close(fd)
        raise
    return fd


def scandir_head(folder, limit: int) -> list:
    """At most `limit` entries of a folder (os.DirEntry), in no order: never more listed or kept, so
    a folder someone filled with millions of names costs no more than `limit` of them."""
    found = []
    with os.scandir(folder) as entries:
        for entry in entries:
            found.append(entry)
            if len(found) >= limit:
                break
    return found


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
    lock = _settings_lock(path)
    try:
        _update_locked(path, values)
    finally:
        os.close(lock)


def _settings_lock(path: Path) -> int:
    """One writer at a time (the app's core, another build of the app): an exclusive flock on
    <settings>.lock beside the settings, this account's own regular file, never through a link.
    Missing folders are made 0700, as atomic_write makes them."""
    import fcntl
    missing = []
    folder = Path(os.path.abspath(path)).parent
    while not folder.exists():
        missing.append(folder)
        folder = folder.parent
    for folder in reversed(missing):
        try:
            os.mkdir(folder, 0o700)
        except FileExistsError:
            pass
    fd = os.open(f"{os.path.abspath(path)}.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        os.close(fd)
        raise OSError("Untrusted settings lock")
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def _update_locked(path: Path, values: dict) -> None:
    data = load_settings(path)
    for key, value in values.items():
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
    history = data.get('agent_project_history')
    if isinstance(history, dict):
        data['agent_project_history'] = {key: entries[:50] for key, entries in history.items()
                                         if isinstance(entries, list)}
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    if len(text.encode("utf-8")) > SETTINGS_MAX_BYTES:
        # Preserve the existing settings if unusually large values still exceed the byte cap.
        raise OSError("Settings exceed the readable size limit; existing settings were preserved")
    atomic_write(path, text)


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
    import process_group
    deadline = time.monotonic() + timeout
    within = False
    try:
        while True:
            # Waited for without reaping it (WNOWAIT): its pid, the group id, stays ours until the
            # group has been killed below.
            if process_group._exited(process.pid, every):
                within = folder_bytes(cwd, budget) <= budget
                break
            if time.monotonic() > deadline or folder_bytes(cwd, budget) > budget:
                break
    finally:
        # Always the whole group, also after the program itself has ended: a child it left behind
        # must not go on writing unwatched. Only then is the program reaped.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            pass
        code = process.wait()
    return within and code == 0


def read_bounded_output(argv: list[str], max_bytes: int = 64 * 1024, timeout: float = 5.0,
                        stdin: bytes | None = None, cwd: str | None = None, env: dict | None = None,
                        merge_stderr: bool = False, cancel=None, keep_children: bool = False) -> tuple[bytes, bool]:
    """Run argv (no shell), read stdout with a byte cap and one deadline; afterwards (also after
    success) everything it started in its process group is ended (process_group.end_child).
    `stdin`: bytes handed to the program (private data never goes into argv);
    `merge_stderr`: read error messages too (same cap);
    `cancel`: an Event; once it is set the program and its children are ended (not ok);
    `keep_children`: a launcher whose server is meant to keep running after it ends."""
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
    if cancel is None:
        thread.join(timeout)
    else:
        deadline = time.monotonic() + timeout
        while thread.is_alive() and not cancel.is_set() and time.monotonic() < deadline:
            thread.join(min(0.2, max(0.0, deadline - time.monotonic())))
    import process_group
    if thread.is_alive() or state["over"]:
        state["over"] = True
        if keep_children:
            proc.kill()
            proc.wait()
        else:
            process_group.end_child(proc, grace=0.0)   # its children too (curl, npx, …)
        thread.join(2.0)
        code = proc.returncode
    elif keep_children:              # it starts a server that is meant to stay (a launcher)
        try:
            code = proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            code = proc.wait()
    else:
        # Its output is closed; it may take a moment to end. Then whatever it left in its group
        # (a child with other output streams) ends too, before the program is reaped.
        code = process_group.end_child(proc, wait=2.0)
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


MAX_TERMINAL_PASTE = 1024 * 1024       # text pasted into the embedded terminal


def clipboard_text(max_bytes: int = MAX_PASTE_BYTES, timeout: float = 2.0, lines: bool = False,
                   terminal: bool = False, primary: bool = False) -> str | None:
    """Plain text from the Wayland clipboard, read with `wl-paste` under a
    byte cap and a deadline (GTK's own paste buffers the whole offer with
    neither). Control characters become spaces (line breaks stay when
    `lines`). '' when the clipboard has no text; None when the offer was too
    long, too slow or failed. `terminal`: for the embedded terminal (tabs stay, nothing else
    of the control characters). `primary`: the PRIMARY selection (Shift+Insert) instead of the
    clipboard, read the same bounded way."""
    which = ["--primary"] if primary else []
    types_raw, _ = read_bounded_output(["wl-paste", *which, "--list-types"], max_bytes=65536, timeout=timeout)
    # An empty clipboard makes wl-paste fail: nothing to paste, not an error.
    offered = {line.strip() for line in types_raw.decode("utf-8", "replace").splitlines()}
    for mime in _TEXT_MIMES:
        if mime not in offered:
            continue
        data, ok = read_bounded_output(["wl-paste", *which, "--no-newline", "--type", mime],
                                       max_bytes=max_bytes, timeout=timeout)
        if not ok:
            return None
        text = data.decode("utf-8", "replace")
        if terminal:              # line breaks and tabs stay; other control characters (ESC…) go
            return "".join(ch if ch in "\n\t" or not (ord(ch) < 32 or 127 <= ord(ch) < 160) else ""
                           for ch in text.replace("\r\n", "\n"))
        if lines:
            return "".join(ch if ch == "\n" or not (ord(ch) < 32 or 127 <= ord(ch) < 160) else " "
                           for ch in text.replace("\r\n", "\n"))
        return " ".join("".join(" " if ord(ch) < 32 or 127 <= ord(ch) < 160 else ch for ch in text).split())
    return ""
