"""Installing and removing an agent or a local LLM from its adapter's recipe (Harness.install,
Provider.install: harnesses.Way, best first).

choose() picks the first way that works on this PC and returns a Step: the exact command (always
shown, with a copy button), whether the app may start it (only plain words, a known program, in a
terminal window the user watches: desktop.run_in_terminal), whether it asks for a password (sudo, in
that terminal; the app never sees it), or that the app's own installer page does it. Removing never
touches the harness's data folders (Harness.data): the dialog names what stays.
"""

from __future__ import annotations

import re
import shlex
import shutil
from dataclasses import dataclass, field

MANAGERS = {          # install, remove (after sudo)
    "pacman": (("pacman", "-S", "--needed"), ("pacman", "-R")),
    "apt": (("apt", "install"), ("apt", "remove")),
    "dnf": (("dnf", "install"), ("dnf", "remove")),
    "zypper": (("zypper", "install"), ("zypper", "remove")),
}
TOOLS = {             # install, remove
    "npm": (("npm", "install", "-g"), ("npm", "uninstall", "-g")),
    "pipx": (("pipx", "install"), ("pipx", "uninstall")),
    "uv": (("uv", "tool", "install"), ("uv", "tool", "uninstall")),
}
UPDATES = {           # update, for the same tools
    "npm": ("npm", "update", "-g"),
    "pipx": ("pipx", "upgrade"),
    "uv": ("uv", "tool", "upgrade"),
}
# What the app may start: plain words only (no spaces, quotes, globs or $), so the terminal
# runs exactly what was shown. Anything else is only shown, for the user to copy.
_WORD = re.compile(r"[A-Za-z0-9@:/._=+,-]{1,300}")
_PROGRAM = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}")
_PACKAGE = re.compile(r"[a-z0-9@][a-z0-9@._+-]{0,99}")


@dataclass
class Step:
    kind: str                         # the way's kind
    commands: list = field(default_factory=list)   # what the user can copy (shell text)
    run: list | None = None           # the commands (argv lists) the app may start in a terminal, or None
    sudo: bool = False
    installer: bool = False           # the app's own installer page does it
    url: str = ""
    note: str = ""


def manager() -> str:
    """This system's package manager ("" when none is known)."""
    return next((m for m in MANAGERS if shutil.which(m)), "")


def _plain(argv) -> list | None:
    words = [str(w) for w in argv]
    if not words or not _PROGRAM.fullmatch(words[0] if words[0] != "sudo" else (words[1:] or ["-"])[0]):
        return None
    return words if all(_WORD.fullmatch(w) for w in words) else None


def _runnable(*commands) -> list | None:
    """The commands as argv lists when every one is plain words, else None (then only shown)."""
    found = [_plain(c) for c in commands]
    return found if all(found) else None


def _update_step(way) -> Step | None:
    """How what this way installed is updated (action "update"): mise upgrade, the tool's own
    upgrade, the way's declared update command; system packages come with the system's updates."""
    if way.kind == "installer":
        return Step("installer", installer=True)
    if way.kind == "mise" and way.spec and shutil.which("mise"):
        argv = ["mise", "--yes", "upgrade", way.spec]
        return Step("mise", [shlex.join(argv)], _runnable(argv))
    if way.kind == "tool" and way.tool in UPDATES and way.spec and shutil.which(way.tool):
        argv = [*UPDATES[way.tool], way.spec]
        return Step("tool", [shlex.join(argv)], _runnable(argv))
    if way.kind == "command" and way.update and shutil.which(str(way.update[0])):
        argv = list(way.update)
        return Step("command", [shlex.join(argv)], _runnable(argv), sudo=way.sudo or argv[0] == "sudo")
    if way.kind == "package" and dict(way.packages).get(manager()):
        return Step("manual", note="It comes with the system's updates.")
    return None


def _step(way, action: str) -> Step | None:
    """The way as a Step for this PC, or None when it does not work here."""
    if action == "update":
        return _update_step(way)
    remove = action == "uninstall"
    if way.kind == "installer":
        return Step("installer", installer=True)
    if way.kind == "mise":
        if not way.spec or not shutil.which("mise"):
            return None
        # --yes: mise asks before pruning in a terminal, and the chain would wait for an answer.
        if remove:
            # unuse takes it out of the global config; its versions are pruned only when nothing else
            # tracks them, so uninstall --all removes them for sure.
            commands = [["mise", "--yes", "unuse", "-g", way.spec], ["mise", "--yes", "uninstall", "--all", way.spec]]
        else:
            commands = [["mise", "--yes", "use", "-g", way.spec]]
        return Step("mise", [shlex.join(c) for c in commands], _runnable(*commands))
    if way.kind == "tool":
        if way.tool not in TOOLS or not way.spec or not shutil.which(way.tool):
            return None
        argv = [*TOOLS[way.tool][1 if remove else 0], way.spec]
        return Step("tool", [shlex.join(argv)], _runnable(argv))
    if way.kind == "command":
        if remove or not way.argv or not shutil.which(str(way.argv[0])):
            return None               # the desktop's installers only install
        argv = list(way.argv)
        return Step("command", [shlex.join(argv)], _runnable(argv), sudo=way.sudo)
    if way.kind == "package":
        m = manager()
        name = dict(way.packages).get(m)
        if not m or not isinstance(name, str) or not _PACKAGE.fullmatch(name):
            return None
        argv = ["sudo", *MANAGERS[m][1 if remove else 0], name]
        return Step("package", [shlex.join(argv)], _runnable(argv), sudo=True)
    if way.kind == "manual":
        return Step("manual", url=way.url, note=way.note)
    return None


def choose(ways, action: str = "install", source: str = "") -> Step | None:
    """The way to install (or, with action="uninstall", to remove) on this PC.
    source: how it is installed now ("mise", "package", "own"): removing uses the matching way."""
    ways = list(ways or ())
    if action in ("uninstall", "update"):
        # The way it is installed now: removing and updating go through that one.
        prefer = {"mise": ("mise",), "package": ("package", "command"),
                  "own": ("installer", "tool", "command")}.get(source, ())
        ways = [w for w in ways if w.kind in prefer] + ([w for w in ways if w.kind == "manual"]
                                                       if action == "uninstall" else [])
        if action == "update":
            for way in ways:
                step = _step(way, action)
                if step is not None:
                    return step
            return None
    for way in ways:
        step = _step(way, action)
        if step is not None:
            return step
    # Nothing works here as it is: at least every command there is, to copy.
    commands = []
    for way in ways:
        if way.kind == "mise" and way.spec:
            commands.append(shlex.join(["mise", "--yes", "unuse" if action == "uninstall" else "use", "-g", way.spec]))
            if action == "uninstall":
                commands.append(shlex.join(["mise", "--yes", "uninstall", "--all", way.spec]))
        elif way.kind == "tool" and way.tool in TOOLS and way.spec:
            commands.append(shlex.join([*TOOLS[way.tool][action == "uninstall"], way.spec]))
        elif way.kind == "package":
            for m, name in way.packages:
                if m in MANAGERS:
                    commands.append(shlex.join(["sudo", *MANAGERS[m][action == "uninstall"], name]))
    if commands:
        return Step("commands", commands)
    return None


def available(ways) -> bool:
    return choose(ways) is not None


def run(step: Step) -> bool:
    """Start the step's commands in one terminal window, one after the other while each succeeds
    (desktop.run_steps_in_terminal); False when they cannot be started from here."""
    if step is None or step.run is None:
        return False
    import desktop
    return bool(desktop.run_steps_in_terminal([list(c) for c in step.run]))
