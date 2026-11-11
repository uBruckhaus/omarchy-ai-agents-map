"""The add-ons of this plugin: switched on and off, nothing else (since 1.0.0, Codex round 12).

The plugin brings all of its add-ons in its own `addons/` folder (signed like Maprisma's, data only);
they are the files of the reviewed plugin commit and are updated with the plugin. Nothing is fetched,
installed from a file, read from another folder or removed: the online catalog, Install from file
and the user's add-ons folder of Maprisma are left out, so an add-on can only be what the plugin
itself ships. Switching on/off is the settings list addons_enabled (and addons_off for the ones the
user switched off).
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

import addon_api
import addons
import ai_core


@dataclass
class Entry:
    """A catalog entry (kept as a type for the Add-ons page; this plugin has no catalog: always none)."""
    id: str
    name: str
    version: str
    kind: str = "feature"
    icon: str = ""
    summary: str = ""
    description: str = ""
    privacy: int = addon_api.PRIVACY["numbers"]
    capabilities: list = field(default_factory=list)
    harnesses: list = field(default_factory=list)
    reads: list = field(default_factory=list)
    sends: list = field(default_factory=list)
    commands: list = field(default_factory=list)        # detect hints: programs on PATH
    paths: list = field(default_factory=list)           # detect hints: "~/…" folders or files
    homepage: str = ""
    url: str = ""
    size: int = 0
    sha256: str = ""
    needs: str = ""


def catalog(*_args, **_kwargs) -> list:
    """No catalog in the plugin: its add-ons come with it."""
    return []


def online() -> bool:
    """Never: nothing about add-ons is fetched."""
    return False


# --- is its harness on this PC? ----------------------------------------------------------------------

def present(entry) -> bool:
    """The detect hints of an add-on find its program on PATH or its folder (read-only checks)."""
    if any(shutil.which(c) for c in entry.commands):
        return True
    home = Path.home()
    return any((home / p[2:]).exists() for p in entry.paths)


# --- switched on ---------------------------------------------------------------------------------------

def enabled() -> set:
    value = ai_core.setting("addons_enabled")
    return {v for v in value if isinstance(v, str)} if isinstance(value, list) else set()


def switched_off() -> set:
    """The add-ons the user switched off (addons_off): never switched on again by enable_found."""
    value = ai_core.setting("addons_off")
    return {v for v in value if isinstance(v, str)} if isinstance(value, list) else set()


def set_enabled(aid: str, on: bool) -> None:
    """The user switches an add-on on or off (as Maprisma's addons.set_enabled: off is remembered)."""
    now = enabled()
    now = now | {aid} if on else now - {aid}
    off = switched_off() - {aid} if on else switched_off() | {aid}
    ai_core.update_settings(addons_enabled=sorted(now), addons_off=sorted(off))


def enable_found(ids) -> list:
    """Maprisma's addons.enable_found (task 055): the add-ons of programs found on this PC are switched
    on at the start, except the ones the user switched off. Never switches one off. The ids switched on."""
    now, off = enabled(), switched_off()
    new = sorted(i for i in ids if isinstance(i, str) and i not in now and i not in off)
    if new:
        ai_core.update_settings(addons_enabled=sorted(now | set(new)))
    return new


def newer(a: str, b: str) -> bool:
    """Version a is newer than b (x.y.z)."""
    ta, tb = addons.version_tuple(a), addons.version_tuple(b)
    return bool(ta and tb) and ta > tb
