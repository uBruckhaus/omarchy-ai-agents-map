"""One supervisor process per AI Project Center assignment (Maprisma's project_supervisor.dart).

The supervisor is this file run as its own program (`python3 -I project_supervisor.py supervise`,
`command()`); it is a Linux child subreaper (prctl PR_SET_CHILD_SUBREAPER), so every descendant of
the agent stays in its process tree even after setsid/setpgid or a double fork: an orphan re-parents
to the nearest subreaper ancestor. "Stopped" therefore means the kernel reports no child left
(waitpid ECHILD after the leader was reaped), not that a /proc scan came back empty.

Not owned: work an agent asks another service to start (systemd-run, D-Bus activation, container
daemons). Such processes are not descendants.

Control (the supervisor's stdin, JSON lines):
  {"argv": [..], "cwd": "..", "input": str|null, "lock": "..", "controller": {"pid": n, "start": ".."}}
  then {"launch": true} (the controller's go-ahead, sent only while it still owns the project store),
  then {"stop": true} any number of times. EOF means the controller is gone: the tree is stopped as
  for a stop request (before the go-ahead: nothing is launched).
Events (its stdout, JSON lines): {"locked": true, "start_ack": true} once the run lock is held,
  {"started": true} once the agent's process exists, {"out": ".."}, {"leader": code} when the agent
  ended by itself, {"stuck": n} while n descendants survive SIGKILL, {"error": code}, and
  {"exit": code} and {"stopped": true} as the last lines, written only when the tree is empty.

The controller's side is SupervisedProcess: no shell anywhere (argv words only, the prompt one word
of its own or on stdin), output in small chunks the project center bounds, and the supervisor itself
started with start_new_session=True and finally reaped through process_group.end_child (the agent
leader shares the supervisor's process group, so a last-resort group kill reaches it too).

This module imports only the standard library at the top: the supervisor runs it with `-I` (no
script folder or user site on sys.path). process_group is imported by the controller side only.
"""

from __future__ import annotations

import codecs
import collections
import ctypes
import fcntl
import json
import os
import select
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

MAX_CONTROL_LINE = 4 * 1024 * 1024      # the start line carries the prompt (at most ~120 KiB, JSON-escaped)
MAX_EVENT_LINE = 1024 * 1024
READ_CHUNK = 16 * 1024
OUTPUT_BUFFER = 128 * 1024              # output kept for the controller between reads (the oldest goes first)
START_DEADLINE = 5.0                    # the start line written and the run lock reported within this
PR_SET_CHILD_SUBREAPER = 36


class ProjectError(Exception):
    """A refusal with a stable machine code (Maprisma's ServiceError)."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


# --- /proc helpers -----------------------------------------------------------------------------------

def _stat_fields(pid: int) -> list[str] | None:
    """The fields of /proc/<pid>/stat after the command name (fields[0] = state, [1] = parent,
    [19] = start time), or None."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            raw = handle.read(4096).decode("ascii", "replace")
    except OSError:
        return None
    fields = raw[raw.rfind(")") + 2:].split()
    return fields if len(fields) > 19 else None


def start_time(pid: int) -> str | None:
    """The kernel start time of pid (its identity together with the pid), or None."""
    fields = _stat_fields(pid)
    return fields[19] if fields else None


class ProcessTree:
    """The live descendants of `root` in /proc, by parent links. Zombies are dead."""

    def __init__(self, root: int):
        self.root = root

    def members(self) -> dict[int, str]:
        """pid -> start time of each live (non-zombie) descendant."""
        parents: dict[int, list[int]] = {}
        starts: dict[int, str] = {}
        try:
            names = os.listdir("/proc")
        except OSError:
            return {}
        for name in names:
            if not name.isdigit():
                continue
            fields = _stat_fields(int(name))
            if fields is None or fields[0] in ("Z", "X"):
                continue
            try:
                parents.setdefault(int(fields[1]), []).append(int(name))
            except ValueError:
                continue
            starts[int(name)] = fields[19]
        found: dict[int, str] = {}
        pending = list(parents.get(self.root, ()))
        while pending:
            pid = pending.pop()
            if pid == self.root or pid in found:
                continue
            found[pid] = starts[pid]
            pending.extend(parents.get(pid, ()))
        return found

    @staticmethod
    def _signal(pid: int, birth: str, sig: int) -> None:
        try:
            fd = os.pidfd_open(pid)
        except OSError:
            return
        try:
            # After pidfd_open the identity is pinned: still the same process and still ours?
            if start_time(pid) == birth and os.stat(f"/proc/{pid}").st_uid == os.getuid():
                signal.pidfd_send_signal(fd, sig)
        except OSError:
            pass
        finally:
            os.close(fd)

    def stop(self, grace: float = 1.5, kill: float = 2.0) -> int:
        """SIGTERM, a grace period, then SIGKILL until the tree is empty or the deadline passes.
        Returns the number of live descendants left."""
        live = self.members()
        if not live:
            return 0
        for pid, birth in live.items():
            self._signal(pid, birth, signal.SIGTERM)
        grace_end = time.monotonic() + grace
        while time.monotonic() < grace_end:
            time.sleep(0.02)
            live = self.members()
            if not live:
                return 0
            for pid, birth in live.items():       # children forked during termination get it too
                self._signal(pid, birth, signal.SIGTERM)
        kill_end = time.monotonic() + kill
        while True:
            live = self.members()
            if not live:
                return 0
            if time.monotonic() > kill_end:
                return len(live)
            for pid, birth in live.items():
                self._signal(pid, birth, signal.SIGKILL)
            time.sleep(0.02)

    @staticmethod
    def reap() -> int:
        """Reap exited children without waiting: 0 when this process has no child left (ECHILD),
        else 1. Only after the leader's own Popen reaped it (a reap here would take its status)."""
        while True:
            try:
                pid, _status = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                return 0
            except InterruptedError:
                continue
            except OSError:
                return 1
            if pid == 0:
                return 1


# --- the supervisor program --------------------------------------------------------------------------

class _Emitter:
    """JSON lines to the controller; a controller that disappeared (EPIPE) must not end the
    supervisor before its tree is empty, so write failures are ignored."""

    def __init__(self, fd: int):
        self.fd = fd
        self.lock = threading.Lock()
        self.open = True

    def __call__(self, event: dict) -> None:
        data = (json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8", "replace")
        with self.lock:
            if not self.open:
                return
            try:
                view = memoryview(data)
                while view:
                    view = view[os.write(self.fd, view):]
            except OSError:
                self.open = False


def _read_line(stream) -> str | None:
    """One control line (malformed UTF-8 replaced), None at EOF or on a failing pipe."""
    try:
        raw = stream.readline(MAX_CONTROL_LINE)
    except (OSError, ValueError):
        return None
    if not raw:
        return None
    return raw.decode("utf-8", "replace")


def _valid_start(start) -> bool:
    if not isinstance(start, dict):
        return False
    argv, controller = start.get("argv"), start.get("controller")
    return (isinstance(argv, list) and bool(argv) and all(isinstance(w, str) and "\0" not in w for w in argv)
            and isinstance(start.get("cwd"), str)
            and (start.get("input") is None or isinstance(start.get("input"), str))
            and isinstance(controller, dict) and type(controller.get("pid")) is int
            and isinstance(controller.get("start"), str))


def _take_lock(path: str) -> int | None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return None
    for _ in range(100):
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            return fd
        except OSError:
            time.sleep(0.02)
    os.close(fd)
    return None


def _subreaper() -> bool:
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        return libc.prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) == 0
    except (OSError, AttributeError):
        return False


def supervise(control=None, out_fd: int = 1) -> int:
    emit = _Emitter(out_fd)
    control = control if control is not None else sys.stdin.buffer
    if not _subreaper():
        emit({"error": "subreaper_unavailable"})       # nothing was started: nothing can be left running
        emit({"stopped": True})
        return 2
    try:
        line = _read_line(control)
        if line is None:
            return 0
        start = json.loads(line)
    except ValueError:
        start = None
    if not _valid_start(start):
        emit({"error": "invalid_start"})
        emit({"stopped": True})
        return 2
    # The run lock is held while any owned process may live, also after the controller is gone; a
    # later controller sees the project (and its folder) as still in use.
    lock_fd = None
    if isinstance(start.get("lock"), str):
        lock_fd = _take_lock(start["lock"])
        if lock_fd is None:
            emit({"error": "run_lock_unavailable"})
            emit({"stopped": True})
            return 2

    def release():
        if lock_fd is not None:
            try:
                os.close(lock_fd)
            except OSError:
                pass

    # Ownership is continuous only if the controller that queued this start is still the parent
    # *after* the lock is held: had it died first, a replacement may already have dispatched.
    controller = start["controller"]
    parent = os.getppid()
    if parent != controller["pid"] or start_time(parent) != controller["start"]:
        release()
        emit({"error": "controller_gone"})
        emit({"stopped": True})
        return 2
    # Launch only on the controller's explicit go-ahead; EOF or anything else ends without launching.
    emit({"locked": True, "start_ack": True})
    try:
        line = _read_line(control)
        message = json.loads(line) if line is not None else None
        authorized = isinstance(message, dict) and message.get("launch") is True
    except ValueError:
        authorized = False
    if not authorized:
        release()
        emit({"error": "launch_withdrawn"})
        emit({"stopped": True})
        return 2
    text = start.get("input")
    try:
        # Same process group as the supervisor (its own session): a last-resort group kill of the
        # supervisor reaches the agent too. No shell; the lock descriptor is not inherited.
        leader = subprocess.Popen(start["argv"], cwd=start["cwd"], env=dict(os.environ),
                                  stdin=subprocess.PIPE if text is not None else subprocess.DEVNULL,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, close_fds=True)
    except (OSError, ValueError, subprocess.SubprocessError):
        release()
        emit({"error": "start_failed"})
        emit({"stopped": True})
        return 0
    emit({"started": True})            # the agent's process exists: only now has it received its prompt

    def feed():
        try:
            leader.stdin.write(text.encode("utf-8"))
        except OSError:
            pass
        finally:
            try:
                leader.stdin.close()
            except OSError:
                pass

    def pump():
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        fd = leader.stdout.fileno()
        while True:
            try:
                chunk = os.read(fd, READ_CHUNK)
            except InterruptedError:
                continue
            except OSError:
                chunk = b""
            piece = decoder.decode(chunk, final=not chunk)
            if piece:
                emit({"out": piece})
            if not chunk:
                return

    stop_requested = threading.Event()
    leader_done = threading.Event()

    def watch_control():
        while _read_line(control) is not None:
            stop_requested.set()
        stop_requested.set()            # EOF: the controller is gone; the tree must not outlive it

    def wait_leader():
        leader.wait()
        leader_done.set()

    if text is not None:
        threading.Thread(target=feed, daemon=True).start()
    output = threading.Thread(target=pump, daemon=True)
    output.start()
    threading.Thread(target=watch_control, daemon=True).start()
    threading.Thread(target=wait_leader, daemon=True).start()
    while not leader_done.is_set() and not stop_requested.is_set():
        leader_done.wait(0.05)
    exit_code = leader.returncode if leader_done.is_set() else None
    if exit_code is not None:
        emit({"leader": exit_code})
    # The owned tree ends with its assignment, even after a successful leader. Confirmation is the
    # kernel's: once the leader is reaped, adopted zombies are reaped here and the tree is empty
    # only when waitpid reports that no child at all remains.
    tree = ProcessTree(os.getpid())
    stuck, unseen = -1, 0
    while True:
        remaining = tree.stop()
        if exit_code is None:
            if leader_done.wait(1.0):
                exit_code = leader.returncode
            else:
                remaining = remaining or 1
        if exit_code is not None:
            children = tree.reap()
            if children == 0 and remaining == 0:
                break
            if children > 0 and remaining == 0:
                # A child the scan did not see yet: look again shortly. One it cannot see at all
                # (hidepid) is reported as surviving, never spun on.
                unseen += 1
                if unseen < 20:
                    time.sleep(0.1)
                    continue
                remaining = 1
        if remaining != stuck:
            emit({"stuck": remaining})
        stuck = remaining
        time.sleep(0.5)
    output.join(2.0)                    # the final output may be incomplete; the exit code is reliable
    # The tree is empty: release the run lock before reporting it, so the controller never sees a
    # confirmed stop while the lock is still held.
    release()
    emit({"exit": exit_code})
    emit({"stopped": True})
    return 0


# --- the controller's side ---------------------------------------------------------------------------

def command() -> list[str] | None:
    """The supervisor's argv: this file run by the same interpreter, isolated (-I)."""
    path = Path(__file__).resolve()
    return [sys.executable, "-I", str(path), "supervise"] if path.is_file() and sys.executable else None


class _Once:
    """A value set once (the first set wins, also across threads); wait() returns it (None while
    unset after the timeout)."""

    def __init__(self):
        self._event = threading.Event()
        self._lock = threading.Lock()
        self.value = None

    def set(self, value) -> bool:
        with self._lock:
            if self._event.is_set():
                return False
            self.value = value
            self._event.set()
            return True

    @property
    def done(self) -> bool:
        return self._event.is_set()

    def wait(self, timeout: float | None = None):
        self._event.wait(None if timeout is None else max(0.0, timeout))
        return self.value if self._event.is_set() else None


class SupervisedProcess:
    """The controller's view of one supervised assignment (the ProjectProcess interface: output(),
    started(), wait(), stop()). A stop that cannot be confirmed fails, and a later stop asks and
    waits again; nothing is cached except a confirmed empty tree."""

    def __init__(self, proc: subprocess.Popen):
        self._proc = proc
        self._send_lock = threading.Lock()
        self._reap_lock = threading.Lock()
        self._reaped = False
        # The agent's output between reads, bounded: a noisy agent drops its oldest chunks (dropped)
        # while the reader goes on draining the supervisor, so stop events are never held up.
        self._out = threading.Condition()
        self._chunks: collections.deque = collections.deque()
        self._chunk_bytes = 0
        self._out_error: Exception | None = None
        self._output_closed = False
        self.dropped = False
        try:
            os.set_blocking(proc.stdin.fileno(), False)    # every write has a deadline (_send)
        except (OSError, ValueError, AttributeError):
            pass
        self._leader: int | None = None
        self._exit: int | None = None
        self._settled = threading.Event()
        self._confirmed = _Once()
        self._locked = threading.Event()
        self._started = _Once()
        self._start_ack = False
        self.stuck = 0
        self._input_open = True
        self._close_requested = False
        self._launch_seen = False       # a frame that only comes after the agent's spawn arrived
        # Loss is decided by the end of its output, never by its exit alone: the final "stopped"
        # line may still be buffered when the exit arrives.
        self._reader = threading.Thread(target=self._read_events, daemon=True)
        self._reader.start()

    @classmethod
    def start(cls, supervisor: list[str], argv: list[str], cwd: str, environment: dict, input_text: str | None,
              lock: str, authorize=None) -> "SupervisedProcess":
        began = time.monotonic()
        try:
            proc = subprocess.Popen(supervisor, cwd=cwd, env=environment, stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, close_fds=True,
                                    start_new_session=True)
        except (OSError, ValueError, subprocess.SubprocessError):
            raise ProjectError("project_agent_unavailable") from None
        managed = cls(proc)
        deadline = began + START_DEADLINE
        # The start line (it carries the prompt) is written within the startup deadline, never
        # blocking on a supervisor that does not read.
        sent = managed._send({"argv": argv, "cwd": cwd, "input": input_text, "lock": lock,
                              "controller": {"pid": os.getpid(), "start": start_time(os.getpid())}},
                             deadline - time.monotonic())
        # The supervisor holds the run lock before the controller authorizes the launch, and the
        # controller authorizes it only while it still owns the store: no moment without an owner.
        while sent and not managed._locked.is_set() and not managed._confirmed.done and time.monotonic() < deadline:
            managed._locked.wait(0.02)
        if not sent or not managed._locked.is_set() or not managed._start_ack:
            managed._withdraw(10.0 - (time.monotonic() - began))
            raise ProjectError("project_agent_unavailable")
        # authorize(managed): the controller checks that it still owns the store and the attempt was
        # not stopped, and takes `managed` as the attempt's process, atomically (under its lock). A
        # stop or detach after that reaches this process: a stop line or EOF before the go-ahead
        # makes the supervisor end without launching.
        if authorize is not None and not authorize(managed):
            managed._withdraw(10.0 - (time.monotonic() - began))
            raise ProjectError("project_launch_withdrawn")
        if not managed._send({"launch": True}, 1.0):
            # Control closed by a stop or detach: withdrawn; a supervisor gone meanwhile: unavailable.
            requested = managed._close_requested
            managed._withdraw(10.0 - (time.monotonic() - began))
            raise ProjectError("project_launch_withdrawn" if requested else "project_agent_unavailable")
        return managed

    # --- events --------------------------------------------------------------------------------------

    def _read_events(self) -> None:
        stream = self._proc.stdout
        try:
            while True:
                raw = stream.readline(MAX_EVENT_LINE)
                if not raw:
                    break
                self._event(raw)
        except (OSError, ValueError):
            pass
        self._lost()

    def _push(self, text: str) -> None:
        with self._out:
            if self._output_closed:
                return
            self._chunks.append(text)
            self._chunk_bytes += len(text.encode("utf-8", "replace"))
            while self._chunk_bytes > OUTPUT_BUFFER and len(self._chunks) > 1:
                self._chunk_bytes -= len(self._chunks.popleft().encode("utf-8", "replace"))
                self.dropped = True
            self._out.notify_all()

    def _fail_output(self, error: Exception) -> None:
        with self._out:
            if not self._output_closed:
                self._out_error = error
                self._output_closed = True
                self._out.notify_all()

    def _close_output(self) -> None:
        with self._out:
            self._output_closed = True
            self._out.notify_all()

    def _event(self, raw: bytes) -> None:
        try:
            data = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            return
        if not isinstance(data, dict):
            return
        # These frames come only after the agent's spawn succeeded.
        if isinstance(data.get("out"), str) or any(type(data.get(k)) is int for k in ("leader", "stuck", "exit")) \
                or data.get("started") is True:
            self._launch_seen = True
            self._started.set(True)
        if isinstance(data.get("out"), str):
            self._push(data["out"])
        elif data.get("locked") is True:
            self._start_ack = data.get("start_ack") is True
            self._locked.set()
        elif data.get("started") is True:
            self._started.set(True)
        elif type(data.get("leader")) is int:
            self._leader = data["leader"]
        elif type(data.get("stuck")) is int:
            self.stuck = data["stuck"]
            if self._leader is not None:
                self._settled.set()     # the result is known, its cleanup is not confirmed
        elif type(data.get("exit")) is int:
            self._exit = data["exit"]
        elif isinstance(data.get("error"), str):
            self._started.set(False)
            self._fail_output(ProjectError("project_agent_unavailable"))
        elif data.get("stopped") is True and not self._confirmed.done:
            self._started.set(False)
            self.stuck = 0
            self._confirmed.set(True)
            self._settled.set()
            self._close_output()

    def _lost(self) -> None:
        self._input_open = False
        self._started.set(False)
        # A supervisor that disappears without "stopped" leaves the tree unconfirmed.
        self._confirmed.set(False)
        self._settled.set()
        self._close_output()

    def _send(self, message: dict, timeout: float = 2.0) -> bool:
        """One control line, written whole within `timeout` (the pipe is non-blocking). A line that
        cannot be written in time closes control: a half line must never be followed by another."""
        data = memoryview((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
        deadline = time.monotonic() + max(0.0, timeout)
        with self._send_lock:
            if not self._input_open:
                return False
            try:
                fd = self._proc.stdin.fileno()
                while data:
                    if not self._input_open:              # closed meanwhile (detach): stop here
                        break
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    _r, writable, _x = select.select([], [fd], [], min(remaining, 0.05))
                    if not writable:
                        continue
                    try:
                        data = data[os.write(fd, data):]
                    except BlockingIOError:
                        continue
            except (OSError, ValueError):
                pass
            if data or not self._input_open:
                # A half line, or control closed meanwhile (_close_input waits for this lock and
                # closes too): never another line after it.
                self._input_open = False
                self._close_stdin()
                return False
            return True

    def _close_stdin(self) -> None:
        try:
            self._proc.stdin.close()
        except (OSError, ValueError):
            pass

    def _close_input(self) -> None:
        """Close control. A write in progress sees the flag within one 50 ms slice and stops (its
        writes never block), so waiting for its lock is short; stdin is then closed for certain."""
        self._close_requested = True
        self._input_open = False
        with self._send_lock:
            self._close_stdin()

    def _reap(self, wait: float) -> None:
        import process_group
        with self._reap_lock:            # one reaper: a second must never signal a released group id
            if self._reaped:
                return
            process_group.end_child(self._proc, wait=max(0.0, wait))
            self._reaped = True
        # Reaped: its stdout ends (the reader sees EOF), then both pipes are released.
        if self._reader is not threading.current_thread():
            self._reader.join(2.0)
        self._close_input()
        if not self._reader.is_alive():
            try:
                self._proc.stdout.close()
            except (OSError, ValueError):
                pass

    def _withdraw(self, budget: float) -> None:
        """Close control before launch and wait (at most three seconds in all) for the
        supervisor's confirmed cleanup, so its run lock is released; then reap it."""
        end = time.monotonic() + max(0.0, min(budget, 3.0))
        self._started.set(False)
        self._close_input()
        self._confirmed.wait(min(2.0, end - time.monotonic()))
        # It never had the go-ahead, so nothing of the agent's runs: the single reaper gives it what
        # is left of the budget to end by itself, then ends whatever is left of it.
        self._reap(max(0.0, min(1.0, end - time.monotonic())))

    # --- ProjectProcess --------------------------------------------------------------------------

    def output(self):
        """The agent's output in chunks, until the supervisor's confirmed end (or its loss); raises
        ProjectError when the supervisor reported an error."""
        while True:
            with self._out:
                while not self._chunks and not self._output_closed:
                    self._out.wait()
                if self._chunks:
                    item = self._chunks.popleft()
                    self._chunk_bytes -= len(item.encode("utf-8", "replace"))
                elif self._out_error is not None:
                    raise self._out_error
                else:
                    return
            yield item

    def started(self, timeout: float = 10.0) -> bool:
        """True once the agent's own process exists; False when it never started."""
        return self._started.wait(timeout) is True

    def wait(self) -> int:
        """The leader's exit code once the supervisor confirmed an empty tree; a result whose
        cleanup is unconfirmed raises project_processes_still_running."""
        self._settled.wait()
        if not self._confirmed.done or self._confirmed.value is not True:
            raise ProjectError("project_processes_still_running")
        return self._exit if self._exit is not None else self._leader if self._leader is not None else -1

    def _never_launched(self) -> bool:
        """The supervisor is gone without "stopped", but it never reported an agent (no spawn frame):
        once it is reaped together with its process group (which an agent it was spawning shares),
        nothing of this assignment is left to confirm."""
        if self._launch_seen:
            return False
        self._reap(1.0)
        return True

    def stop(self, wait: float = 4.0) -> None:
        if self._confirmed.done:
            if self._confirmed.value is not True and not self._never_launched():
                raise ProjectError("project_processes_still_running")
            self._reap(2.0)
            return
        self._send({"stop": True}, 1.0)
        if self._confirmed.wait(wait) is not True and not (self._confirmed.done and self._never_launched()):
            raise ProjectError("project_processes_still_running")
        self._reap(2.0)

    def detach(self) -> None:
        """Release the control pipe: the supervisor stops its tree on EOF (and keeps the run lock
        until it is empty)."""
        self._started.set(False)
        self._close_input()


def run(argv: list[str], cwd: str, environment: dict, input_text: str | None, lock: str, may_launch,
        supervisor: list[str] | None = None) -> SupervisedProcess:
    """The project center's default runner: argv[0] resolved on the environment's PATH (absolute
    folders only, an executable regular file), then started under a supervisor."""
    supervisor = supervisor or command()
    if supervisor is None:
        raise ProjectError("project_supervisor_unavailable")
    program = argv[0]
    executable = None
    if program.startswith("/"):
        executable = program
    elif "/" not in program:
        for entry in (environment.get("PATH") or "").split(":"):
            if not entry.startswith("/"):
                continue
            candidate = os.path.join(entry, program)
            try:
                info = os.stat(candidate)
            except OSError:
                continue
            if (info.st_mode & 0o170000) == 0o100000 and info.st_mode & 0o111:
                executable = candidate
                break
    if executable is None:
        raise ProjectError("project_agent_unavailable")
    return SupervisedProcess.start(supervisor, [executable, *argv[1:]], cwd, environment, input_text, lock,
                                   authorize=may_launch)


if __name__ == "__main__" and sys.argv[1:] == ["supervise"]:
    sys.exit(supervise())
