"""A timer's occurrence handed to whoever holds the project store (Maprisma has no counterpart).

The store (<config>/ai-project-center) has one owner at a time (ProjectCenter's exclusive lock), and
only its owner may start a run. When a timer fires, that owner is the app (its window open or
hidden), a headless host started by an earlier timer, or nobody. So the timer's program, `job()`:

1. puts a request into the queue, a private folder in $XDG_RUNTIME_DIR (pidfile.private_dir, 0700,
   this account's own, never through a link): `<nonce>.req` = {"project": id, "at": time};
2. waits while the store's owner takes it: an owner looks at the queue every second or two and
   *claims* a request by renaming it to `<nonce>.claimed` (one rename: exactly one owner gets it),
   runs the occurrence (project_schedule.trigger) and writes `<nonce>.ans`;
3. when nobody claims it, it opens the store itself (ProjectCenter: the lock decides between two
   triggers that try at once) and becomes the headless host: it serves the queue (its own request
   first), waits under the supervisors until no project works any more, takes requests of other
   timers meanwhile, and only then lets the store go (`host()`).

While the app runs (its PID file names a process that passes the toggle's identity checks: owner,
kernel start time, interpreter, exact script) the job gives it APP_GRACE seconds before it tries the
store itself, so a run starts in the window the user sees. Nothing is ever signalled: the PID file is
only a hint about waiting, so a stale or reused PID cannot be made to receive anything.

Safety: only a process holding the store's lock claims requests, so two hosts never serve one; a
request is a regular file of this account in a folder only this account can enter, at most 1 KiB of
JSON whose project id must be 32 hex digits and whose time must be recent (a request left by a host
that died is dropped, not run late); the answer names only an outcome code. A job that gives up
withdraws its request first (unlink): if that fails, an owner claimed it and its answer is awaited.
"""

from __future__ import annotations

import errno
import json
import os
import re
import secrets
import signal
import stat
import threading
import time
from pathlib import Path

QUEUE_NAME = "ai-agents-map-schedule"
MAX_REQUEST = 1024
MAX_REQUESTS = 16                 # served per look; the rest at the next one
MAX_LISTED = 1000                 # folder entries looked at per look
REQUEST_AGE = 180.0               # seconds a request may wait before it counts as stale
APP_GRACE = 15.0                  # the app looks at the queue every 2 s
JOB_DEADLINE = 120.0              # nobody claimed and the store stayed busy: the occurrence is skipped
ANSWER_WAIT = 120.0               # claimed: the owner answers after one "get" and one "run"
HOST_LOOK = 1.0
LEFTOVER_AGE = 600.0
_NONCE = re.compile(r"[0-9a-f]{32}")
_ID = re.compile(r"[0-9a-f]{32}")
OUTCOMES = ("started", "running", "not_approved", "stopped", "review_changes", "ended", "failed",
            "unscheduled", "unknown", "stale", "invalid")
HERE = Path(__file__).resolve().parent


# --- the queue ---------------------------------------------------------------------------------------

def queue_dir() -> Path | None:
    import pidfile
    base = pidfile.private_dir()
    return base / QUEUE_NAME if base is not None else None


def _open_queue(directory: Path | None = None) -> int:
    """A descriptor of the queue folder: created 0700, this account's own, no link, closed to others."""
    directory = directory or queue_dir()
    if directory is None:
        raise OSError(errno.EACCES, "no private runtime folder")
    try:
        os.mkdir(directory, 0o700)
    except FileExistsError:
        pass
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    info = os.fstat(fd)
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        os.close(fd)
        raise OSError(errno.EACCES, "the schedule queue is not private")
    return fd


def _create(fd: int, name: str, body: dict) -> None:
    """`name` appears only complete: the JSON goes into a new private file (never through a link),
    every byte written, then it is renamed to `name` (a reader never sees a half-written request or
    answer). An existing `name` is not replaced (FileExistsError)."""
    data = json.dumps(body).encode("ascii")
    temporary = f"{name}.{secrets.token_hex(8)}.tmp"
    out = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600,
                  dir_fd=fd)
    try:
        try:
            view = memoryview(data)
            while view:
                written = os.write(out, view)
                if written <= 0:
                    raise OSError(errno.EIO, "short write")
                view = view[written:]
        finally:
            os.close(out)
        os.link(temporary, name, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
    finally:
        _unlink(fd, temporary)


def _read(fd: int, name: str) -> dict | None:
    """A small JSON object from this account's own regular file in the queue, else None."""
    try:
        handle = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=fd)
    except OSError:
        return None
    try:
        info = os.fstat(handle)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_size > MAX_REQUEST:
            return None
        value = json.loads(os.read(handle, MAX_REQUEST + 1).decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    finally:
        os.close(handle)
    return value if isinstance(value, dict) else None


def _unlink(fd: int, name: str) -> bool:
    try:
        os.unlink(name, dir_fd=fd)
        return True
    except OSError:
        return False


def submit(project_id: str, directory: Path | None = None, now=time.time) -> str:
    """Queues one occurrence; its nonce."""
    if not _ID.fullmatch(project_id or ""):
        raise ValueError("project id")
    nonce = secrets.token_hex(16)
    fd = _open_queue(directory)
    try:
        _create(fd, f"{nonce}.req", {"project": project_id, "at": now()})
    finally:
        os.close(fd)
    return nonce


def pending(nonce: str, directory: Path | None = None) -> bool:
    """Not claimed yet."""
    fd = _open_queue(directory)
    try:
        os.stat(f"{nonce}.req", dir_fd=fd, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False
    finally:
        os.close(fd)


def withdraw(nonce: str, directory: Path | None = None) -> bool:
    """Takes the request back; False when an owner claimed it already."""
    fd = _open_queue(directory)
    try:
        return _unlink(fd, f"{nonce}.req")
    finally:
        os.close(fd)


def answer(nonce: str, directory: Path | None = None) -> str | None:
    """The owner's outcome for this request (read once: the answer file goes), or None."""
    fd = _open_queue(directory)
    try:
        value = _read(fd, f"{nonce}.ans")
        if value is None:
            return None
        _unlink(fd, f"{nonce}.ans")
        outcome = value.get("outcome")
        return outcome if outcome in OUTCOMES else "invalid"
    finally:
        os.close(fd)


def waiting(directory: Path | None = None) -> bool:
    """A request waits (cheap: the app asks every two seconds)."""
    try:
        directory = directory or queue_dir()
        if directory is None:
            return False
        with os.scandir(directory) as entries:
            return any(entry.name.endswith(".req") for entry in entries)
    except OSError:
        return False


# --- the owner's side --------------------------------------------------------------------------------

def serve(open_call, notify, directory: Path | None = None, now=time.time) -> list:
    """Claims and runs the waiting requests; [(nonce, outcome)]. `open_call()` gives the store owner's
    ProjectCenter.call; when it cannot (the store is someone else's), nothing is claimed."""
    import project_schedule
    try:
        fd = _open_queue(directory)
    except OSError:
        return []
    try:
        names = sorted(_requests(fd))
        _clean(fd, now())
        if not names:
            return []
        try:
            call = open_call()
        except Exception:
            return []                          # not the store's owner: another host claims them
        served = []
        for name in names[:MAX_REQUESTS]:
            nonce = name[:-4]
            try:
                os.rename(name, f"{nonce}.claimed", src_dir_fd=fd, dst_dir_fd=fd)
            except OSError:
                continue                       # withdrawn, or claimed by another owner
            request = _read(fd, f"{nonce}.claimed")
            _unlink(fd, f"{nonce}.claimed")
            outcome = _outcome(request, now())
            if outcome is None:
                try:
                    outcome = project_schedule.trigger(call, request["project"], notify)
                except Exception:
                    outcome = "failed"
            try:
                _create(fd, f"{nonce}.ans", {"outcome": outcome})
            except OSError:
                pass
            served.append((nonce, outcome))
        return served
    finally:
        os.close(fd)


def _requests(fd: int) -> list:
    """The waiting requests' names, at most MAX_LISTED looked at (the folder is private, but a
    runaway writer of this account must not make every look allocate without limit)."""
    found = []
    with os.scandir(fd) as entries:
        for count, entry in enumerate(entries):
            if count >= MAX_LISTED:
                break
            name = entry.name
            if name.endswith(".req") and _NONCE.fullmatch(name[:-4]):
                found.append(name)
    return found


def _outcome(request, at: float) -> str | None:
    """Why a claimed request is not run (None: run it)."""
    if not isinstance(request, dict) or set(request) != {"project", "at"} \
            or not isinstance(request["project"], str) or not _ID.fullmatch(request["project"]) \
            or type(request["at"]) not in (int, float):
        return "invalid"
    if not at - REQUEST_AGE <= request["at"] <= at + 5:
        return "stale"
    return None


def _clean(fd: int, at: float) -> None:
    """Answers nobody read and claims a host left behind when it died."""
    try:
        with os.scandir(fd) as entries:
            names = [entry.name for _count, entry in zip(range(MAX_LISTED), entries)]
    except OSError:
        return
    for name in names:
        if name.endswith((".ans", ".claimed", ".tmp")) or (name.endswith(".req") and _NONCE.fullmatch(name[:-4])):
            try:
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            except OSError:
                continue
            if at - info.st_mtime > (LEFTOVER_AGE if not name.endswith(".req") else REQUEST_AGE * 2):
                _unlink(fd, name)


# --- the app ----------------------------------------------------------------------------------------

def app_running(proc: str = "/proc", python: str = "/usr/bin/python3", here: Path = HERE) -> bool:
    """The app's PID file names a live app of this account: owner, kernel start time, interpreter and
    exact script checked as ai-agents-map-toggle checks them. Only a hint about waiting."""
    import pidfile
    try:
        record = pidfile.read()
    except OSError:
        return False
    if not record or not record[1]:
        return False
    pid, start = record
    try:
        if os.stat(f"{proc}/{pid}").st_uid != os.getuid() or pidfile.process_starttime(pid) != start:
            return False
        if os.path.realpath(f"{proc}/{pid}/exe") != os.path.realpath(python):
            return False
        with open(f"{proc}/{pid}/cmdline", "rb") as handle:
            argv = handle.read(64 * 1024).split(b"\0")
    except OSError:
        return False
    script = next((arg for arg in argv[1:] if arg and not arg.startswith(b"-")), b"")
    return os.path.realpath(script.decode("utf-8", "replace")) == str(here / "ai_popup.py")


# --- the timer's program -----------------------------------------------------------------------------

def _prepare_environment() -> None:
    """As the app does at its start: no agent-session variables, the user's program folders on PATH
    (a user service has systemd's short PATH)."""
    import ai_core
    ai_core.drop_agent_session_vars()
    ai_core.add_user_program_dirs()


def _open_store():
    import project_center
    import schedule_units
    return project_center.ProjectCenter(schedules=schedule_units.sync)


def job(project_id: str, open_store=None, notify=None, app=None, directory: Path | None = None,
        clock=time.monotonic, sleep=time.sleep, deadline: float = JOB_DEADLINE) -> int:
    """One timer occurrence (see the module docstring); the exit status (0 handed over or served)."""
    import project_schedule
    from project_supervisor import ProjectError
    if open_store is None:
        _prepare_environment()
    open_store = open_store or _open_store
    notify = notify or project_schedule.notify
    try:
        nonce = submit(project_id, directory)
    except (OSError, ValueError):
        notify("Scheduled run failed", "The schedule's private queue folder is not available.")
        return 1
    start = clock()
    grace_until = start + APP_GRACE if (app or app_running)() else start
    while clock() - start < deadline:
        if not pending(nonce, directory):
            return _awaited(nonce, directory, clock, sleep)
        if clock() >= grace_until:
            try:
                center = open_store()
            except ProjectError as error:
                if error.code != "project_center_busy":
                    if withdraw(nonce, directory):
                        notify("Scheduled run failed", f"A scheduled project could not start: {_failure(error)}")
                        return 1
                    return _awaited(nonce, directory, clock, sleep)
            else:
                return host(center, notify, directory, clock=clock, sleep=sleep, nonce=nonce)
        sleep(0.5)
    if withdraw(nonce, directory):
        notify("Scheduled run skipped", "The project list stayed in use by another program, so a scheduled "
               "project could not start.")
        return 1
    return _awaited(nonce, directory, clock, sleep)


def _failure(error) -> str:
    import project_texts
    return project_texts.failure(error)


def _awaited(nonce: str, directory, clock, sleep) -> int:
    start = clock()
    while clock() - start < ANSWER_WAIT:
        outcome = answer(nonce, directory)
        if outcome is not None:
            return 0 if outcome not in ("failed", "invalid") else 1
        sleep(0.25)
    return 1


def host(center, notify, directory: Path | None = None, clock=time.monotonic, sleep=time.sleep,
         stop: threading.Event | None = None, nonce: str | None = None) -> int:
    """The store's owner without a window: serves the queue (`nonce`: its own request, first) until no
    project works any more, then lets the store go (shutdown stops what is left on SIGTERM: a logout,
    `systemctl stop`). The exit status follows its own request's outcome."""
    stop = stop or _stop_on_signals()
    outcome = None
    try:
        while True:
            for served, result in serve(lambda: center.call, notify, directory):
                if served == nonce:
                    outcome = result
                    answer(nonce, directory)          # its own answer: nobody else reads it
            if stop.is_set() or (not center.working() and not waiting(directory)):
                break
            stop.wait(HOST_LOOK)
    finally:
        _release(center, clock, sleep)
    return 1 if outcome in ("failed", "invalid") else 0


def _stop_on_signals() -> threading.Event:
    stop = threading.Event()
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(sig, lambda *_a: stop.set())
    return stop


def _release(center, clock, sleep) -> None:
    """shutdown() until it confirms (each try waits about 10 s for the supervisors); detach as the last
    resort, which leaves each supervisor to stop its own tree."""
    from project_supervisor import ProjectError
    for _attempt in range(6):
        try:
            center.shutdown()
            return
        except ProjectError:
            sleep(1.0)
    center.detach()
