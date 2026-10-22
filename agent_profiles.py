"""How to set up MCP servers and skills for an agent this plugin has no
built-in knowledge of (a new one in Omarchy's agent menu, say a DeepSeek
harness): looked up on the web by an installed agent, once, and kept.

The researcher is Claude Code (`claude -p` with only WebSearch and WebFetch:
no shell, no files) or else Codex (`codex --search exec`, read-only
sandbox). It answers with a small JSON profile; everything in it is
checked before use (PROFILE rules below), because it comes from the web:

- paths only under the home folder, no "..", nothing like ~/.ssh or a
  credentials file;
- the MCP file is JSON, TOML or YAML, its key a plain (dotted) name, the
  field names from a fixed list;
- nothing in a profile is ever run.

A profile only says where things go; every change it leads to still goes
through the preview, backup and Undo (connect.py).

Profiles live in desktop.config_dir()/agents/<command>.json (~/.config/omarchy/ai-agents-map on Omarchy).
"""

import datetime as dt
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import desktop

HOME = Path.home()
PROFILE_DIR = desktop.config_dir() / "agents"
WORK_DIR = HOME / ".cache/ai-agents-map/research"
RETRY_AFTER = dt.timedelta(days=7)
TIMEOUT = 600
MAX_ANSWER = 64 * 1024

_COMMAND = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_PATH = re.compile(r"^~/[A-Za-z0-9._@+/-]{1,200}$")
_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,40}(\.[A-Za-z_][A-Za-z0-9_-]{0,40}){0,3}$")
# Never a place for settings we write, whatever a web page says.
_PRIVATE = re.compile(r"(^|/)\.(ssh|gnupg|password-store|aws|azure|kube|docker|pki|netrc|git-credentials)(/|$)"
                      r"|auth|credential|secret|token|password|keyring|\.pem$|\.key$", re.IGNORECASE)
FIELDS = {
    "type_field": ("", "type", "transport"),
    "stdio_type": ("", "stdio", "local"),
    "http_type": ("", "http", "streamable_http", "streamable-http", "remote", "sse"),
    "url_field": ("url", "httpUrl", "serverUrl"),
    "env_field": ("env", "environment"),
    "headers_field": ("headers", "http_headers"),
}
FORMATS = ("json", "toml", "yaml")

PROMPT = """Find out, from the official documentation of the AI coding agent "{name}" (command `{command}`{spec}{version}) on Linux, where it reads
1. Agent Skills (folders with a SKILL.md) for the user, not per project, and
2. MCP servers for the user (the global config file and its key).

Search the web; prefer the agent's own docs and repository. Answer with ONLY one JSON object, no other text:
{{
  "skills": {{"dirs": ["~/..."], "own": "~/..."}},
  "mcp": {{"file": "~/...", "format": "json|toml|yaml", "key": "mcpServers",
          "type_field": "|type|transport", "stdio_type": "|stdio|local",
          "http_type": "|http|streamable_http|streamable-http|remote|sse",
          "url_field": "url|httpUrl|serverUrl", "env_field": "env|environment",
          "headers_field": "headers|http_headers", "command_list": false}},
  "sources": ["https://..."],
  "notes": "one short sentence"
}}
Rules: paths start with ~/ ; "dirs" lists every user-level skills folder the agent reads by itself (include ~/.agents/skills
only if the docs say so); "own" is the agent's own skills folder. "key" may be dotted for nested JSON (e.g. "mcp.servers").
"type_field" is the field naming the transport ("" when there is none); "command_list" is true when command and args are
one list. Use null for "skills" or "mcp" when the agent has no such feature, and say so in "notes"."""


def _path_ok(path) -> bool:
    return isinstance(path, str) and bool(_PATH.match(path)) and ".." not in path.split("/") \
        and not _PRIVATE.search(path) and path.count("/") >= 2


def validate(data) -> dict:
    """The parts of a researched profile that pass the rules; raises ValueError when nothing usable is left."""
    if not isinstance(data, dict):
        raise ValueError("not a JSON object")
    profile = {}
    skills = data.get("skills")
    if isinstance(skills, dict):
        dirs = [d for d in skills.get("dirs", []) if _path_ok(d)][:8] if isinstance(skills.get("dirs"), list) else []
        own = skills.get("own") if _path_ok(skills.get("own")) else (dirs[0] if dirs else None)
        if own:
            profile["skills"] = {"dirs": list(dict.fromkeys([own] + dirs)), "own": own}
    mcp = data.get("mcp")
    if isinstance(mcp, dict) and _path_ok(mcp.get("file")) and mcp.get("format") in FORMATS \
            and isinstance(mcp.get("key"), str) and _KEY.match(mcp["key"]):
        if mcp["format"] != "json" and "." in mcp["key"]:
            raise ValueError("a dotted key only in JSON")
        clean = {"file": mcp["file"], "format": mcp["format"], "key": mcp["key"],
                 "command_list": mcp.get("command_list") is True}
        for field, allowed in FIELDS.items():
            value = mcp.get(field) or ""
            clean[field] = value if value in allowed else allowed[0]
        profile["mcp"] = clean
    if not profile:
        raise ValueError("no usable skills or MCP setup in the answer")
    sources = data.get("sources")
    profile["sources"] = [s for s in sources if isinstance(s, str) and s.startswith("https://") and len(s) < 300][:6] \
        if isinstance(sources, list) else []
    notes = data.get("notes")
    profile["notes"] = notes[:300] if isinstance(notes, str) else ""
    return profile


# --- stored profiles ---------------------------------------------------------------------------

def profile_file(command: str) -> Path:
    if not _COMMAND.match(command):
        raise ValueError(command)
    return PROFILE_DIR / f"{command}.json"


def load(command: str) -> dict | None:
    """The stored profile ("status": "ok" with skills/mcp, or "failed"), or None."""
    try:
        path = profile_file(command)
        if path.stat().st_size > MAX_ANSWER:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("status") == "ok":
        try:
            return {**validate(data), "status": "ok", "checked": str(data.get("checked", "")),
                    "researcher": str(data.get("researcher", ""))}
        except ValueError:
            return None
    return {"status": "failed", "checked": str(data.get("checked", "")), "error": str(data.get("error", ""))[:300]}


def save(command: str, data: dict):
    path = profile_file(command)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(prefix=f".{command}-", dir=str(path.parent))
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    os.replace(tmp, path)


def mcp_profile(command: str) -> dict | None:
    profile = load(command)
    return profile.get("mcp") if profile and profile.get("status") == "ok" else None


def skills_profile(command: str) -> dict | None:
    profile = load(command)
    return profile.get("skills") if profile and profile.get("status") == "ok" else None


def needs_research(command: str, now: dt.datetime | None = None) -> bool:
    """No profile yet, or a failed look-up older than RETRY_AFTER."""
    profile = load(command)
    if profile is None:
        return True
    if profile["status"] == "ok":
        return False
    try:
        checked = dt.datetime.fromisoformat(profile["checked"])
    except ValueError:
        return True
    return (now or dt.datetime.now()) - checked > RETRY_AFTER


# --- looking it up --------------------------------------------------------------------------------

def researchers(installed) -> list[str]:
    """Installed agents that can look things up on the web without a prompt (their adapter's
    research fact: the web and nothing else), in the agent list's order."""
    import harnesses
    return [h.command for h in harnesses.all_harnesses()
            if h.research and h.command in installed and shutil.which(h.command)]


def research_command(researcher: str, prompt: str, schema_file: Path) -> list[str]:
    import harnesses
    argv: tuple[str, ...] = harnesses.get(researcher).research
    binary = shutil.which(researcher) or researcher
    fill = {"{prompt}": prompt, "{schema}": str(schema_file)}
    return [binary] + [fill.get(a, a) for a in argv[1:]]


def first_json(text: str):
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", text):
        try:
            value, _end = decoder.raw_decode(text, match.start())
        except ValueError:
            continue
        if isinstance(value, dict) and ("skills" in value or "mcp" in value):
            return value
    raise ValueError("no JSON answer")


SCHEMA = {"type": "object", "additionalProperties": True,
          "properties": {"skills": {"type": ["object", "null"]}, "mcp": {"type": ["object", "null"]},
                         "sources": {"type": "array", "items": {"type": "string"}}, "notes": {"type": "string"}},
          "required": ["skills", "mcp", "sources", "notes"]}


def _bounded(argv, **kwargs):
    import ai_core
    # Our own look-up run: the provider watcher does not start the agent's local LLMs for it.
    kwargs.setdefault("env", {**os.environ, "AI_AGENTS_MAP_NO_PROVIDERS": "1"})
    return ai_core.read_bounded_output(argv, **kwargs)


def research(agent, installed, run=_bounded) -> dict:
    """Look `agent` (agents.Agent) up and store the result. Returns the stored
    profile; a failure is stored too (tried again after RETRY_AFTER)."""
    import ai_helper
    now = dt.datetime.now().isoformat(timespec="seconds")
    found = ai_helper.in_order(installed)          # Settings › Agent for AI checks first
    if not found:
        result = {"status": "failed", "checked": now, "error": "no agent that can search the web"}
        save(agent.command, result)
        return result
    import agents as agents_mod
    spec = agents_mod.SPECS.get(agent.command, "")
    prompt = PROMPT.format(name=agent.name, command=agent.command, spec=f", installed as {spec}" if spec else "",
                           version=f", version {agent.version}" if agent.version else "")
    errors = []
    for researcher in found:
        try:
            text = ai_helper.look_up(researcher, "agent-setup", prompt, SCHEMA, run=run,
                                     result_of=lambda answer: validate(first_json(answer)) and agent.name)
            profile = validate(first_json(text[-MAX_ANSWER:]))
        except (OSError, subprocess.TimeoutExpired, RuntimeError, ValueError) as error:
            errors.append(f"{researcher}: {error}")
            continue
        result = {"status": "ok", "checked": now, "researcher": researcher, **profile}
        save(agent.command, result)
        return result
    result = {"status": "failed", "checked": now, "error": "; ".join(errors)[:300]}
    save(agent.command, result)
    return result
