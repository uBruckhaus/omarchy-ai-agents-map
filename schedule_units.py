"""The systemd user timers of scheduled projects: one timer and one service per project, in
${XDG_CONFIG_HOME:-~/.config}/systemd/user, named ai-agents-map-schedule-<project id>.{timer,service}.

    sync({project id: schedule})   # the timers follow the store; {project id: why it failed}
    remove_all()                   # every timer of the plugin (project_schedule.py clear)

Why a timer and not the app: a schedule must also run while the window and its process are closed.
OnCalendar= comes from project_schedule.on_calendar (numbers and day names only) and Persistent=true
runs a time missed while the PC was off once after the next login.

Rules:
- Every file carries MARKER on its first line. A file of that name without it (or a link in its
  place) is someone else's: it is never replaced or removed, and that project gets no timer.
- Nothing of the project goes into a unit but its id (32 hex digits, checked again here): no title,
  no folder. ExecStart= is /usr/bin/python3 -I <plugin>/project_schedule.py run <id>, and the plugin
  folder must be plain (UNIT_SAFE): no spaces, %, quotes, $, backslashes, ; or control characters.
- systemctl --user only as argv lists (daemon-reload, enable --now, restart, disable --now; a timer
  whose disable --now fails keeps its files and is reported, the next sync tries again), each
  bounded by ai_core.read_bounded_output (own process group, output cap, deadline).
- An end date that has passed counts as off: its timer goes.
"""

from __future__ import annotations

import datetime as dt
import os
import re
import stat
from pathlib import Path

import project_schedule

MARKER = "# Managed by AI Agents Map"
PREFIX = "ai-agents-map-schedule-"
PYTHON = "/usr/bin/python3"
HERE = Path(__file__).resolve().parent
SYSTEMCTL_TIMEOUT = 30.0
MAX_UNIT_BYTES = 64 * 1024
UNIT_SAFE = re.compile(r"/[A-Za-z0-9._/+-]+")
UNIT_FOLDER = "unit folder"     # the failures key when the folder itself cannot be read
_NAME = re.compile(PREFIX + r"([0-9a-f]{32})\.(timer|service)")
_ID = re.compile(r"[0-9a-f]{32}")


def unit_dir() -> Path:
    """Where systemd looks for this user's own units ($XDG_CONFIG_HOME, as systemd reads it)."""
    config = os.environ.get("XDG_CONFIG_HOME")
    base = Path(config) if config and os.path.isabs(config) else Path.home() / ".config"
    return base / "systemd" / "user"


def installable(here: Path = HERE) -> bool:
    """The plugin folder can stand in ExecStart= as one plain word."""
    return bool(UNIT_SAFE.fullmatch(str(here / "project_schedule.py")))


def timer_name(project_id: str) -> str:
    if not _ID.fullmatch(project_id or ""):
        raise ValueError("project id")
    return f"{PREFIX}{project_id}.timer"


def service_name(project_id: str) -> str:
    return timer_name(project_id)[:-len(".timer")] + ".service"


def timer_text(project_id: str, schedule: dict) -> str:
    calendar = project_schedule.on_calendar(schedule)
    return f"""{MARKER}
[Unit]
Description=AI Agents Map: scheduled project run

[Timer]
OnCalendar={calendar}
Persistent=true
AccuracySec=1min
Unit={service_name(project_id)}

[Install]
WantedBy=timers.target
"""


def service_text(project_id: str, here: Path = HERE) -> str:
    """Type=oneshot: while a run the program waits for is going on, the timer does not start it again."""
    script = str(here / "project_schedule.py")
    if not UNIT_SAFE.fullmatch(script):
        raise ValueError("the plugin folder has characters a service file cannot hold")
    timer_name(project_id)
    return f"""{MARKER}
[Unit]
Description=AI Agents Map: scheduled project run

[Service]
Type=oneshot
ExecStart={PYTHON} -I {script} run {project_id}
TimeoutStartSec=infinity
"""


# --- files -------------------------------------------------------------------------------------------

def _read_own(path: Path) -> str | None:
    """The text of an existing unit when it is this account's own regular file (not a link), else
    None; FileNotFoundError when there is none."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        raise
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_size > MAX_UNIT_BYTES:
            return None
        return os.read(fd, MAX_UNIT_BYTES + 1).decode("utf-8", "replace")
    finally:
        os.close(fd)


def managed(path: Path) -> bool:
    """Ours: a regular file of this account whose first line is MARKER."""
    try:
        text = _read_own(path)
    except FileNotFoundError:
        return False
    return text is not None and text.split("\n", 1)[0] == MARKER


def _write(path: Path, text: str) -> bool:
    """Writes the unit unless a file of that name is not ours; True when it changed."""
    try:
        current = _read_own(path)
    except FileNotFoundError:
        current = None
    else:
        if current is None or current.split("\n", 1)[0] != MARKER:
            raise PermissionError(f"{path.name} exists and was not written by AI Agents Map")
        if current == text:
            return False
    import ai_core
    ai_core.atomic_write(path, text, mode=0o644, dir_mode=0o755)     # unit files: readable like the others
    return True


def _remove(path: Path) -> bool:
    if managed(path):
        path.unlink(missing_ok=True)
        return True
    return False


def _present(directory: Path) -> set:
    """Project ids that have one of our unit files (by name; the marker is checked before a change).
    The folder is read one entry at a time and only names of ours are kept, so other files cost
    time, never memory, and no unit of ours is ever left out. A folder that is not there holds
    none; any other error is raised (the caller reports it instead of "nothing to remove")."""
    found = set()
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                match = _NAME.fullmatch(entry.name)
                if match:
                    found.add(match.group(1))
    except FileNotFoundError:
        pass
    return found


# --- systemctl ---------------------------------------------------------------------------------------

def systemctl(*args, run=None) -> tuple[bool, str]:
    import ai_core
    run = run or ai_core.read_bounded_output
    out, ok = run(["systemctl", "--user", *args], max_bytes=16 * 1024, timeout=SYSTEMCTL_TIMEOUT,
                  merge_stderr=True)
    return ok, out.decode("utf-8", "replace").strip()[:300]


def sync(schedules: dict, directory: Path | None = None, today: dt.date | None = None, run=None,
         here: Path = HERE) -> dict:
    """The timers follow `schedules` ({project id: checked schedule}): new or changed ones are written
    and (re)started, the others' units are stopped and removed. {project id: why it failed}."""
    directory = directory or unit_dir()
    today = today or dt.date.today()
    failures: dict = {}
    wanted = {pid: schedule for pid, schedule in schedules.items()
              if _ID.fullmatch(str(pid)) and schedule and not project_schedule.ended(schedule, today)}
    written, removed = [], []
    for pid, schedule in wanted.items():
        try:
            timer = _write(directory / timer_name(pid), timer_text(pid, schedule))
            service = _write(directory / service_name(pid), service_text(pid, here))
        except (OSError, ValueError) as error:
            failures[pid] = str(error)
            continue
        if timer or service:
            written.append(pid)
    try:
        present = _present(directory)
    except OSError as error:
        failures[UNIT_FOLDER] = str(error) or "the unit folder cannot be read"
        present = set()
    for pid in sorted(present - set(wanted)):
        timer, service = directory / timer_name(pid), directory / service_name(pid)
        if managed(timer):
            ok, said = systemctl("disable", "--now", timer.name, run=run)
            if not ok:
                # Not known to be stopped: its files stay, so the next sync (or clear) tries again.
                failures[pid] = said or "systemctl could not stop the timer"
                continue
        gone = [_remove(timer), _remove(service)]
        if any(gone):
            removed.append(pid)
    if written or removed:
        ok, said = systemctl("daemon-reload", run=run)
        if not ok:
            failures.update({pid: said or "systemctl daemon-reload failed" for pid in written + removed})
            return failures
    for pid in written:
        # enable --now starts a new timer; restart makes a changed calendar count at once.
        ok, said = systemctl("enable", "--now", timer_name(pid), run=run)
        if ok:
            ok, said = systemctl("restart", timer_name(pid), run=run)
        if not ok:
            failures[pid] = said or "systemctl could not start the timer"
    return failures


def remove_all(directory: Path | None = None, run=None) -> dict:
    """Every timer of the plugin stopped and removed (the plugin's schedules cleared; the projects
    keep their schedule and get the timers again the next time the store is opened). Afterwards the
    folder is read again: a unit of ours still there is a failure, so `clear` never says "done"
    while a timer is left."""
    directory = directory or unit_dir()
    failures = sync({}, directory=directory, run=run)
    try:
        left = _present(directory)
    except OSError as error:
        failures[UNIT_FOLDER] = str(error) or "the unit folder cannot be read"
        return failures
    for pid in sorted(left):
        if managed(directory / timer_name(pid)) or managed(directory / service_name(pid)):
            failures.setdefault(pid, "its timer is still there")
    return failures
