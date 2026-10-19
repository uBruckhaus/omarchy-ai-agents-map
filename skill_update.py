"""Skills written for an MCP server (a SKILL.md that teaches agents its
tools, such as ~/.agents/skills/photocraft for the photocraft server) go
stale when the server changes: tools renamed, new ones, parameters gone.

- based_on(): which skills belong to which server: the skill folder has the
  server's name, or its SKILL.md calls the server's tools (mcp__<name>);
- status(): whether the server's tools changed since the skill was last
  updated here (the tools' fingerprint is kept per skill);
- rewrite(): an installed agent rewrites the SKILL.md against the server's
  current tool list. Claude Code runs with no tools at all (`--tools ""`),
  Codex in its read-only sandbox; the old SKILL.md and the tools go in on
  stdin, the new text comes back on stdout and is checked (front matter with
  the same name, size). It is only a proposal: the change goes through the
  preview, backup and Undo (connect.py) like every other change.

Only the user's own SKILL.md files under the home folder are offered: skills
an app installed elsewhere (/usr/share/<app>/skills) come with the app's own
updates.
"""

import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import desktop

HOME = Path.home()
STATE_FILE = desktop.config_dir() / "skill-updates.json"
WORK_DIR = HOME / ".cache/ai-agents-map/skill-update"
MAX_SKILL = 256 * 1024
TIMEOUT = 600
_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def read_skill(folder: Path) -> str:
    try:
        with open(Path(folder) / "SKILL.md", encoding="utf-8", errors="replace") as handle:
            return handle.read(MAX_SKILL + 1)
    except OSError:
        return ""


def own_skill_file(folder: Path) -> Path | None:
    """The real SKILL.md when it is this account's own regular file under the
    home folder (a link in ~/.claude/skills resolves to ~/.agents/skills/…)."""
    try:
        path = (Path(folder) / "SKILL.md").resolve(strict=True)
        info = path.lstat()
    except (OSError, RuntimeError):
        return None
    if not path.is_relative_to(HOME) or info.st_uid != os.getuid() or not path.is_file() \
            or info.st_nlink != 1 or info.st_size > MAX_SKILL:
        return None
    return path


def skill_name(text: str) -> str:
    front = re.match(r"^---\s*\n(.*?)\n---", text, re.DOTALL)
    found = re.search(r"^name:\s*['\"]?([^'\"\n]+?)['\"]?\s*$", front.group(1), re.MULTILINE) if front else None
    return found.group(1).strip() if found else ""


def based_on(skill_folders, servers) -> dict:
    """server name -> [skill folders] whose skill is about that server."""
    found = {}
    for folder in skill_folders:
        folder = Path(folder)
        text = None
        for name in servers:
            if not _NAME.match(name):
                continue
            if folder.name.lower() == name.lower():
                hit = True
            else:
                text = read_skill(folder) if text is None else text
                hit = re.search(rf"\bmcp__{re.escape(name)}(?:__|\b)", text) is not None
            if hit and folder not in found.setdefault(name, []):
                found[name].append(folder)
    return found


def fingerprint(tools: dict | None) -> str:
    """The tools' names, parameters and descriptions as one short hash ("" when not known)."""
    if not tools or tools.get("status") != "ok":
        return ""
    items = sorted((t.get("kind", ""), t.get("name", ""), t.get("description", ""), tuple(t.get("params", [])))
                   for t in tools.get("tools", []) + tools.get("prompts", []))
    return hashlib.sha256(json.dumps(items).encode()).hexdigest()[:16]


def load_state(path: Path = None) -> dict:
    try:
        data = json.loads((path or STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def remember(skill_file: Path, server: str, tools: dict, path: Path = None):
    """After an update (or "it is up to date"): the tools the skill now matches."""
    import ai_core
    state = load_state(path)
    state[str(skill_file)] = {"server": server, "tools": fingerprint(tools), "version": tools.get("server", ""),
                              "updated": dt.datetime.now().isoformat(timespec="seconds")}
    ai_core.atomic_write(path or STATE_FILE, json.dumps(state, indent=1, ensure_ascii=False))


def status(skill_file: Path, server: str, tools: dict | None, path: Path = None) -> str:
    """"current": matches the tools last asked; "changed": the server's tools changed since
    the skill was updated here; "unknown": never compared (or the tools were not asked yet)."""
    seen = load_state(path).get(str(skill_file))
    now = fingerprint(tools)
    if not seen or seen.get("server") != server or not now:
        return "unknown"
    return "current" if seen.get("tools") == now else "changed"


# --- the rewrite ----------------------------------------------------------------------------------

PROMPT = """You update an agent skill: the SKILL.md below teaches coding agents to use the MCP server
"{server}". The server now reports itself as "{version}" and offers the tools listed below (name,
parameters, description). Rewrite the SKILL.md so it matches them:

- fix tool names, parameters and behaviour that changed; drop tools that no longer exist;
- mention important new tools where they fit the skill's purpose;
- keep the front matter (name: {name}), the structure, the language and everything that is still true;
- never invent tools or parameters that are not in the list.

Answer with the complete new SKILL.md only (starting with ---), nothing before or after it.

<current-skill>
{skill}
</current-skill>

<server-tools>
{tools}
</server-tools>
"""


def tools_text(tools: dict) -> str:
    lines = []
    for item in tools.get("tools", []) + tools.get("prompts", []):
        params = ", ".join(item.get("params", []))
        kind = "prompt " if item.get("kind") == "prompt" else ""
        lines.append(f"- {kind}{item.get('name', '')}({params}): {item.get('description', '')}")
    return "\n".join(lines)


def writers(installed) -> list[str]:
    return [c for c in ("claude", "codex") if c in installed and shutil.which(c)]


def writer_command(writer: str, answer_file: Path) -> list[str]:
    binary = shutil.which(writer) or writer
    if writer == "claude":
        # No tools at all (no shell, files, web or MCP servers), no session kept; the prompt on stdin.
        return [binary, "-p", "--tools", "", "--strict-mcp-config", "--no-session-persistence",
                "--disable-slash-commands", "--output-format", "text"]
    return [binary, "exec", "--sandbox", "read-only", "--skip-git-repo-check", "--output-last-message",
            str(answer_file), "-"]


def check(answer: str, name: str) -> str:
    """The new SKILL.md out of the answer, or ValueError."""
    text = answer.strip()
    fenced = re.match(r"^```[a-zA-Z]*\n(.*)\n```$", text, re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()
    if not text.startswith("---"):
        raise ValueError("the answer is not a SKILL.md (no front matter)")
    if skill_name(text) != name:
        raise ValueError("the answer changed the skill's name")
    if len(text.encode()) > MAX_SKILL:
        raise ValueError("the answer is too long")
    if "\x00" in text:
        raise ValueError("the answer is not text")
    return text + "\n"


def _bounded(argv, **kwargs):
    import ai_core
    kwargs.setdefault("env", {**os.environ, "AI_AGENTS_MAP_NO_PROVIDERS": "1"})
    return ai_core.read_bounded_output(argv, **kwargs)


def rewrite(skill_file: Path, server: str, tools: dict, installed, run=_bounded) -> tuple[str, str]:
    """(new SKILL.md text, the agent that wrote it). Raises RuntimeError with every
    agent's reason when none gave a usable answer."""
    old = read_skill(skill_file.parent)
    name = skill_name(old)
    if not name:
        raise RuntimeError("the SKILL.md has no name in its front matter")
    if not tools or tools.get("status") != "ok" or not tools.get("tools"):
        raise RuntimeError("the server's tools are not known")
    found = writers(installed)
    if not found:
        raise RuntimeError("no agent that can write it (Claude Code, Codex)")
    prompt = PROMPT.format(server=server, version=tools.get("server") or server, name=name, skill=old,
                           tools=tools_text(tools)).encode("utf-8")
    WORK_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    answer_file = WORK_DIR / "answer.md"
    errors = []
    for writer in found:
        answer_file.unlink(missing_ok=True)
        try:
            out, ok = run(writer_command(writer, answer_file), max_bytes=MAX_SKILL * 4, timeout=TIMEOUT,
                          stdin=prompt, cwd=str(WORK_DIR))
            text = out.decode("utf-8", "replace")
            if writer == "codex" and answer_file.is_file() and not answer_file.is_symlink():
                text = answer_file.read_text(encoding="utf-8", errors="replace")[:MAX_SKILL * 2]
            if not ok:
                raise RuntimeError(text.strip()[-200:] or "no answer")
            return check(text, name), writer
        except (OSError, subprocess.SubprocessError, RuntimeError, ValueError) as error:
            errors.append(f"{writer}: {error}")
        finally:
            answer_file.unlink(missing_ok=True)
    raise RuntimeError("; ".join(errors)[:400])
