"""Configure… on the map: an agent's or local LLM's own setup, in a terminal tab of the window.

Where the app cannot do something (yet), the agent's or local LLM's own setup is the way out: its
wizard or menu (`hermes setup`, `ollama launch`) or its app window (LM Studio). What runs comes
from its add-on as data (harnesses.Configure: its own program and plain words, checked by
harnesses.configure_ok). A program that has no setup command of its own gets its help
(`<program> --help`): what it can be told, and how. After the tab ends, the map looks again.
"""

from __future__ import annotations

import shlex
import shutil
from dataclasses import dataclass

import harnesses
from i18n import N_


@dataclass(frozen=True)
class Entry:
    label: str                 # shown on its button (an English text, translated when shown)
    argv: tuple                # what runs
    note: str = ""
    window: bool = False       # an app window, not a terminal tab


HELP = N_("Its options (--help)")


def _commands(items) -> list:
    out = []
    for item in items:
        if harnesses.configure_ok(item.argv, (item.argv[0],) if item.argv else ()) and shutil.which(item.argv[0]):
            out.append(Entry(item.label, tuple(item.argv), item.note, item.window))
    return out


def _help(program: str) -> list:
    """`<program> --help` when the program is here (its own program, plain words, as any Configure)."""
    argv = (program, "--help")
    if program and harnesses.configure_ok(argv, (program,)) and shutil.which(program):
        return [Entry(HELP, argv)]
    return []


def for_agent(command: str) -> list:
    harness = harnesses.get(command)
    if harness is None:
        return []
    return _commands(harness.configure) or _help(command)


def _provider(key: str):
    """(provider, its configure): an add-on made before these facts gets the app's own."""
    import providers
    provider = providers.get(key)
    built_in = next((p for p in providers.built_in_local() if p.key == key), None)
    return provider or built_in, getattr(provider, "configure", ()) or getattr(built_in, "configure", ())


def provider_program(key: str) -> str:
    """The program a local LLM is used through: its installer's command, else its engine's program."""
    import install_ctl
    import llm_engine
    installer = install_ctl.installer(key)
    names = ([installer.command] if installer is not None and installer.command else []) + \
        list(llm_engine.of(key).programs)
    return next((n for n in names if shutil.which(n)), "")


def for_runtime(key: str) -> list:
    _provider_now, configure = _provider(key)
    return _commands(configure) or _help(provider_program(key))


def script(entry: Entry, done: str) -> str:
    """The bash line for a terminal tab: the note, the command (every word quoted), then `done`."""
    note = f"echo {shlex.quote(entry.note)}; echo; " if entry.note else ""
    return note + shlex.join(entry.argv) + f"; echo; echo {shlex.quote(done)}"
