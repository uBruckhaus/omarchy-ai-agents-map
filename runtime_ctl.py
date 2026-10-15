"""Start and stop local LLM runtimes (right-click on the map).

Only this user's own means are used, so no password is ever needed:

- a runtime with a user service (llama-server.service, ollama.service, …):
  `systemctl --user start|stop|enable|disable <unit>`
- LM Studio without one: its own CLI, `lms server start|stop`

Everything runs with a timeout; callers run it on a thread.
"""

import shutil
import subprocess
from pathlib import Path

TIMEOUT = 30


def lms_binary() -> str | None:
    found = shutil.which("lms")
    if found:
        return found
    candidate = Path.home() / ".lmstudio/bin/lms"
    return str(candidate) if candidate.is_file() else None


def _run(argv: list[str]) -> tuple[bool, str]:
    try:
        result = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=TIMEOUT)
    except (OSError, subprocess.SubprocessError) as error:
        return False, str(error)
    message = (result.stderr or result.stdout or "").strip().splitlines()
    return result.returncode == 0, (message[-1] if message else "")[:300]


def can_control(rt) -> bool:
    return bool(rt.installed and (rt.service or (rt.key == "lmstudio" and lms_binary())))


def start_at_login(rt) -> bool | None:
    """True/False for a runtime with a user service, None when there is none."""
    if not rt.service:
        return None
    ok, _out = _run(["systemctl", "--user", "is-enabled", "--quiet", f"{rt.service}.service"])
    return ok


def start(rt) -> tuple[bool, str]:
    if rt.service:
        return _run(["systemctl", "--user", "start", f"{rt.service}.service"])
    if rt.key == "lmstudio" and lms_binary():
        return _run([lms_binary(), "server", "start"])
    return False, "no way to start it"


def stop(rt) -> tuple[bool, str]:
    if rt.service:
        return _run(["systemctl", "--user", "stop", f"{rt.service}.service"])
    if rt.key == "lmstudio" and lms_binary():
        return _run([lms_binary(), "server", "stop"])
    return False, "no way to stop it"


def set_start_at_login(rt, on: bool) -> tuple[bool, str]:
    if not rt.service:
        return False, "no user service"
    return _run(["systemctl", "--user", "enable" if on else "disable", f"{rt.service}.service"])
