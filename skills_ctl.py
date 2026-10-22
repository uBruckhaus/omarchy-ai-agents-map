"""Where each agent reads skills, and giving an agent a skill set (a folder
of skills, a box on the map) or taking it away.

Skills are folders with a SKILL.md (the Agent Skills format every agent
here understands). An agent reads the folders in NATIVE by itself (from
the agents' docs, 2026-10) and the extra folders its settings list (Pi,
Hermes, Crush, Grok, OpenClaw).

Giving an agent a set:
- Pi and Hermes: the folder goes into their settings list
  (settings.json "skills", config.yaml skills.external_dirs).
- every other agent: one link per skill in the agent's own folder (OWN),
  like ~/.local/bin/sync-agent-skills does for Claude and Codex.
Taking it away undoes exactly that. A folder the agent reads by itself
(~/.agents/skills for most of them) cannot be taken away from one agent.
"""

import json
import os
import re
from pathlib import Path
from typing import cast

import agent_profiles
import connect
import harnesses

HOME = Path.home()
SHARED = "~/.agents/skills"


def _rebuild():
    """Tables from the adapters (run again when add-ons add or remove some)."""
    global NATIVE, OWN, SETTINGS_LIST
    # The folders each agent reads by itself, its own folder (where its links go) and the agents whose
    # settings keep a folder list we write (Harness.skills).
    with_skills = [h for h in harnesses.all_harnesses() if h.skills is not None]
    NATIVE = {h.command: list(h.skills.dirs) for h in with_skills}
    OWN = {h.command: h.skills.own for h in with_skills}
    SETTINGS_LIST = tuple(h.command for h in with_skills if h.skills.settings_list)


_rebuild()
harnesses.on_change(_rebuild)

MAX_SKILLS = 200


def expand(path: str) -> Path:
    path = str(path)
    if path == "~" or path.startswith("~/"):
        return HOME / path[2:]
    return Path(path)


def tilde(path) -> str:
    text = str(path)
    home = str(HOME)
    return "~" + text[len(home):] if text == home or text.startswith(home + "/") else text


def _real(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:
        return path


# --- the folders an agent reads ------------------------------------------------------------

def _json(path: Path) -> dict:
    try:
        data = json.loads(connect._read(path) or "{}")
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def config_dirs(agent: str) -> list[str]:
    """Extra skill folders the agent's settings list (harnesses.Skills.listed)."""
    h = harnesses.get(agent)
    try:
        found = h.skills.listed() if h is not None and h.skills is not None and h.skills.listed else None
    except (OSError, ValueError, AttributeError):
        found = None
    return [d for d in found[:20] if isinstance(d, str) and d] if isinstance(found, list) else []


def native_dirs(agent: str) -> list[str]:
    """Built in, or from a profile looked up on the web (agent_profiles)."""
    if agent in NATIVE:
        return NATIVE[agent]
    profile = agent_profiles.skills_profile(agent)
    return profile["dirs"] if profile else []


def own_dir(agent: str) -> str | None:
    if agent in OWN:
        return OWN[agent]
    profile = agent_profiles.skills_profile(agent)
    return profile["own"] if profile else None


def skill_dirs(agent: str) -> list[Path]:
    dirs = []
    for path in native_dirs(agent) + config_dirs(agent):
        path = expand(path)
        if path not in dirs:
            dirs.append(path)
    return dirs


def skills_of(folder: Path) -> list[Path]:
    """Skill folders (with a SKILL.md) in a set, one category level deep (Hermes)."""
    found = []
    try:
        entries = sorted(folder.iterdir())[:MAX_SKILLS]
    except OSError:
        return found
    for entry in entries:
        if entry.name.startswith(".") or not entry.is_dir():
            continue
        if (entry / "SKILL.md").is_file():
            found.append(entry)
        else:
            try:
                found += [e for e in sorted(entry.iterdir())[:MAX_SKILLS]
                          if not e.name.startswith(".") and (e / "SKILL.md").is_file()]
            except OSError:
                pass
    return found[:MAX_SKILLS]


# --- settings lists ------------------------------------------------------------------------------

# --- plan ---------------------------------------------------------------------------------------

class Reach:
    """How an agent gets a set today."""
    def __init__(self):
        self.native = []         # folders it reads by itself that hold the set (or link into it)
        self.listed = False      # in its settings list (Pi, Hermes)
        self.links = []          # (link, target) in its own folder pointing into the set


def reach(agent: str, folder: Path) -> Reach:
    folder = _real(folder)
    result = Reach()
    own = expand(mine) if (mine := own_dir(agent)) else None
    native = [expand(d) for d in native_dirs(agent)]
    configured = [expand(d) for d in config_dirs(agent)]
    # In the settings list we write (Pi, Hermes): removable there, even when also read by itself.
    result.listed = agent in SETTINGS_LIST and any(_real(d) == folder for d in configured)
    listed = [d for d in configured if d not in native]
    for path in native + listed:
        real = _real(path)
        if real == folder:
            if (path in native or agent not in SETTINGS_LIST) and path not in result.native:
                result.native.append(path)
            continue
        try:
            entries = list(path.iterdir())[:MAX_SKILLS]
        except OSError:
            continue
        for entry in entries:
            if not entry.is_symlink():
                continue
            target = _real(entry)
            if target.parent != folder and folder not in target.parents:
                continue
            if own is not None and path == own:
                result.links.append((entry, Path(os.readlink(entry))))
            elif path not in result.native:
                result.native.append(path)     # through a folder other agents read too
    return result


def is_connected(agent: str, folder: Path) -> bool:
    found = reach(agent, folder)
    return bool(found.native or found.listed or found.links)


def plan(agent: str, folder: str, remove: bool = False) -> connect.Plan:
    """Give `agent` the skill set in `folder` (~ allowed), or take it away.
    Plan.notes lists what stays (folders the agent reads by itself)."""
    if not own_dir(agent):
        raise ValueError(agent)
    path = _real(expand(folder))
    result = connect.Plan(agent, "", tilde(path), connected=is_connected(agent, path))
    result.notes = []
    found = reach(agent, path)
    if remove:
        result.notes = [tilde(p) for p in found.native]
        if found.listed:
            result.changes = _settings_change(agent, str(path), remove=True)
        result.links_remove = list(found.links)
        return result
    if found.native or found.listed:
        return result
    if agent in SETTINGS_LIST:
        result.changes = _settings_change(agent, str(path), remove=False)
        return result
    own = expand(cast(str, own_dir(agent)))     # checked at the start
    for skill in skills_of(path):
        link = own / skill.name
        if not link.exists() and not link.is_symlink():
            result.links_add.append((link, skill))
    return result


def _settings_change(agent: str, folder: str, remove: bool) -> list:
    skills = harnesses.get(agent).skills
    file = expand(skills.settings_file)
    old = connect._read(file)
    new = skills.write(old, folder, remove)
    return [connect.Change(file, old, new)] if new != old else []


def description(skill: Path, limit: int = 16 * 1024) -> str:
    """The description from a SKILL.md's front matter (folded or plain), shortened."""
    try:
        with open(skill / "SKILL.md", encoding="utf-8", errors="replace") as handle:
            text = handle.read(limit)
    except OSError:
        return ""
    front = re.match(r"^---\s*\n(.*?)\n---", text, re.DOTALL)
    if front is None:
        return ""
    lines = front.group(1).splitlines()
    for index, line in enumerate(lines):
        found = re.match(r"^description:\s*(.*)$", line)
        if not found:
            continue
        value = found.group(1).strip()
        if value in ("", ">", "|", ">-", "|-", ">+", "|+"):
            more = []
            for following in lines[index + 1:]:
                if following and not following[0].isspace():
                    break
                more.append(following.strip())
            value = " ".join(m for m in more if m)
        value = value.strip().strip("'\"")
        return value[:300] + ("…" if len(value) > 300 else "")
    return ""


def how(agent: str, folder) -> str:
    """"native", "listed", "links" or "" — how the agent gets the set."""
    found = reach(agent, expand(str(folder)))
    return "listed" if found.listed else "links" if found.links else "native" if found.native else ""
