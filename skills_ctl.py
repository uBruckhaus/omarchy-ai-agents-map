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

import agent_profiles
import connect

HOME = Path.home()
SHARED = "~/.agents/skills"
NATIVE = {
    "claude": ["~/.claude/skills"],
    "codex": [SHARED, "~/.codex/skills"],
    "copilot": ["~/.copilot/skills", SHARED],
    "cursor-agent": ["~/.cursor/skills", SHARED, "~/.claude/skills", "~/.codex/skills"],
    "gemini": ["~/.gemini/skills", SHARED],
    "opencode": ["~/.config/opencode/skills", "~/.claude/skills", SHARED],
    "pi": ["~/.pi/agent/skills", SHARED],
    "omp": ["~/.omp/agent/skills"],
    "hermes": ["~/.hermes/skills"],
    "crush": ["~/.config/crush/skills", "~/.config/agents/skills", SHARED, "~/.claude/skills"],
    "grok": ["~/.grok/skills"],
    "openclaw": ["~/.openclaw/skills", SHARED],
    "dsh": ["~/.dsh/skills", SHARED],
    "bionic": ["~/.lmstudio/skills"],          # global skills, shared with LM Studio
    "muse": ["~/.config/muse/skills", SHARED, "~/.claude/skills", "~/.codex/skills"],
}
# The agent's own folder: where its links go.
OWN = {agent: dirs[0] for agent, dirs in NATIVE.items()}
OWN["codex"] = "~/.codex/skills"
SETTINGS_LIST = ("pi", "hermes")       # a folder list in their settings we write
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


def hermes_dirs(text: str) -> list[str]:
    block = connect._top_block(text, "skills")
    if block is None:
        return []
    body = text[block[0]:block[1]]
    found = re.search(r"^  external_dirs:[ \t]*\n((?:    - .*\n?)*)", body, re.MULTILINE)
    if found is None:
        inline = re.search(r"^  external_dirs:[ \t]*\[(.*)\]", body, re.MULTILINE)
        return [d.strip().strip("'\"") for d in inline.group(1).split(",") if d.strip()] if inline else []
    return [line[6:].strip().strip("'\"") for line in found.group(1).splitlines() if line.strip()]


def config_dirs(agent: str) -> list[str]:
    """Extra skill folders the agent's settings list."""
    try:
        if agent in ("pi", "omp"):
            found = _json(HOME / f".{agent}/agent/settings.json").get("skills")
        elif agent == "hermes":
            found = hermes_dirs(connect._read(HOME / ".hermes/config.yaml"))
        elif agent == "crush":
            options = _json(HOME / ".config/crush/crush.json").get("options")
            found = options.get("skills_paths") if isinstance(options, dict) else None
        elif agent == "grok":
            import tomllib
            found = tomllib.loads(connect._read(HOME / ".grok/config.toml")).get("skills", {}).get("paths")
        elif agent == "openclaw":
            load = _json(HOME / ".openclaw/openclaw.json").get("skills", {})
            found = load.get("load", {}).get("extraDirs") if isinstance(load, dict) else None
        else:
            found = None
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

def hermes_text(text: str, folder: str, remove: bool) -> str:
    """skills.external_dirs in config.yaml, as text: one `- path` line."""
    item = f"    - {folder}\n"
    block = connect._top_block(text, "skills")
    if block is None:
        if remove:
            return text
        return text + ("" if text.endswith("\n") or not text else "\n") + "skills:\n  external_dirs:\n" + item
    start, end = block
    body = text[start:end]
    listed = re.search(r"^  external_dirs:[ \t]*\n((?:    - .*\n?)*)", body, re.MULTILINE)
    if listed is None:
        if remove:
            return text
        if re.search(r"^  external_dirs:", body, re.MULTILINE):
            raise ValueError("skills.external_dirs is written in a form this cannot edit")
        body = body.rstrip("\n") + "\n  external_dirs:\n" + item
        return text[:start] + body + ("\n" if text[end:] and not body.endswith("\n") else "") + text[end:]
    lines = [line for line in listed.group(1).splitlines(keepends=True) if line.strip()]
    keep = [line for line in lines if line[6:].strip().strip("'\"") not in (folder, tilde(folder))]
    if not remove:
        keep.append(item)
    if keep:
        new_list = "  external_dirs:\n" + "".join(l if l.endswith("\n") else l + "\n" for l in keep)
    else:
        new_list = ""
    body = body[:listed.start()] + new_list + body[listed.end():]
    return text[:start] + body + text[end:]


def pi_text(text: str, folder: str, remove: bool) -> str:
    data = connect._json(text)
    dirs = data.get("skills") if isinstance(data.get("skills"), list) else []
    dirs = [d for d in dirs if not (isinstance(d, str) and str(expand(d)) == folder)]
    if not remove:
        dirs.append(folder)
    if dirs:
        data["skills"] = dirs
    else:
        data.pop("skills", None)
    return connect._dump(data)


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
    own = expand(own_dir(agent)) if own_dir(agent) else None
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
    own = expand(own_dir(agent))
    for skill in skills_of(path):
        link = own / skill.name
        if not link.exists() and not link.is_symlink():
            result.links_add.append((link, skill))
    return result


def _settings_change(agent: str, folder: str, remove: bool) -> list:
    file = HOME / (".pi/agent/settings.json" if agent == "pi" else ".hermes/config.yaml")
    old = connect._read(file)
    new = pi_text(old, folder, remove) if agent == "pi" else hermes_text(old, folder, remove)
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
