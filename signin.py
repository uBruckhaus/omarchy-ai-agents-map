"""Signing an agent in to its online provider: the agent's own sign-in, or an API key put where the
agent itself reads it (from the agent adapters: agent_status.Access.logins / .key).

- A sign-in (subscription, account) is always the agent's own command, run in a terminal the user
  watches; passwords and browser logins stay between the user and the agent.
- An API key is checked (one word of plain characters), then either handed to the agent's own
  command on stdin (codex login --with-api-key: the agent stores it itself) or written into the .env
  file the agent reads (owner-only, atomic, never through a link). It never goes into an argv, a log,
  a backup or the app's own settings, and nothing keeps it afterwards.
"""

from __future__ import annotations

import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

KEY_RE = re.compile(r"[A-Za-z0-9._~+/=:-]{8,512}")
VAR_RE = re.compile(r"[A-Z][A-Z0-9_]{1,63}")
MAX_ENV_FILE = 64 * 1024
STDIN_TIMEOUT = 30            # seconds for the agent's own key command
MAX_OUTPUT = 4096


@dataclass(frozen=True)
class Login:
    label: str                # what it signs in with ("Claude subscription")
    argv: tuple               # the agent's own command (plain words)
    note: str = ""            # what to do in it ("Type /login in it.")


@dataclass(frozen=True)
class KeyStep:
    var: str                  # the variable the agent reads ("GEMINI_API_KEY"), shown to the user
    url: str = ""             # where to get a key
    stdin: tuple = ()         # the agent's own command that reads the key from stdin
    env_file: str = ""        # or: the .env file the agent reads ("~/.gemini/.env")


def valid_key(key: str) -> bool:
    return bool(KEY_RE.fullmatch(key or ""))


def save_key(step: KeyStep, key: str, home: Path | None = None) -> tuple[bool, str]:
    """(worked, a short message without the key). Raises ValueError for a key that is not one word
    of plain characters or a step that names neither a command nor a file."""
    key = key.strip()
    if not valid_key(key):
        raise ValueError("not an API key")
    if step.stdin:
        return _to_stdin(step.stdin, key)
    if step.env_file and VAR_RE.fullmatch(step.var):
        home = Path.home() if home is None else home
        path = home / step.env_file[2:] if step.env_file.startswith("~/") else None
        if path is None:
            raise ValueError("env file outside home")
        write_env(path, step.var, key)
        return True, ""
    raise ValueError("no way to store the key")


def _to_stdin(argv: tuple, key: str) -> tuple[bool, str]:
    """The agent's own key command, the key on stdin; its answer read with a byte cap and one deadline."""
    import ai_core
    env = dict(os.environ)
    ai_core.drop_agent_session_vars(env)      # the agent's own command, not a child of an agent session
    out, ok = ai_core.read_bounded_output(list(argv), max_bytes=MAX_OUTPUT, timeout=STDIN_TIMEOUT,
                                          stdin=(key + "\n").encode(), env=env, merge_stderr=True)
    text = out.decode("utf-8", "replace").replace(key, "…")
    lines = [line for line in text.splitlines() if line.strip()]
    return ok, (lines[-1][:200] if lines else "")


def write_env(path: Path, var: str, value: str):
    """Set VAR=value in a .env file: other lines kept, an earlier VAR replaced; written to a new
    owner-only file next to it and moved over it (a link at the path is replaced, not followed)."""
    if not VAR_RE.fullmatch(var) or not valid_key(value):
        raise ValueError("bad variable or value")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    old = ""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        fd = None
    except OSError as error:            # a link (ELOOP): leave its target alone, replace the link
        if path.is_symlink():
            fd = None
        else:
            raise error
    if fd is not None:
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise PermissionError(f"{path} is not a file of yours")
            old = os.read(fd, MAX_ENV_FILE + 1).decode("utf-8", "replace")
            if len(old) > MAX_ENV_FILE:
                raise ValueError(f"{path} is too big")
        finally:
            os.close(fd)
    pattern = re.compile(rf"\s*(export\s+)?{re.escape(var)}\s*=")
    lines = [line for line in old.splitlines() if not pattern.match(line)]
    lines.append(f"{var}={value}")
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")    # exclusive, unique, 0600
    try:
        os.fchmod(fd, 0o600)
        data = ("\n".join(lines) + "\n").encode()
        while data:
            data = data[os.write(fd, data):]
        os.fsync(fd)
        os.close(fd)
        fd = None
        os.replace(tmp, path)
    except BaseException:
        if fd is not None:
            os.close(fd)
        Path(tmp).unlink(missing_ok=True)
        raise
