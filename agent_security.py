"""Healthy & Security check: what an agent may do on this PC without asking, explained.

For the agents whose settings are known, read from their own settings files (only
the security keys; never credential files):

- Claude Code  ~/.claude/settings.json      permissions.defaultMode, sandbox.enabled,
                                            permissions.disableBypassPermissionsMode, allow/ask/deny rules
- Codex        ~/.codex/config.toml          approval_policy, sandbox_mode,
                                            [sandbox_workspace_write] network_access, trusted projects
- Copilot      ~/.copilot/settings.json      defaultPermissionMode (+ trustedFolders, allowedUrls)
- Cursor CLI   ~/.config/cursor/cli-config.json  approvalMode, sandbox.mode, sandbox.networkAccess,
                                            autoAcceptWebSearch, allow/deny rules
- Hermes       ~/.hermes/config.yaml         approvals.mode, security.redact_secrets, terminal.backend
- OpenCode     ~/.config/opencode/opencode.json  permission.edit/bash/webfetch/external_directory
- Pi           ~/.pi/agent/settings.json     defaultTools (Pi never asks: its tools are its permissions)

Every setting has its choices, what each means and how risky it is (0 safe … 3 high).
risks() adds what follows from them together (no approval and no sandbox, wide allow
rules, MCP servers) with what lowers each risk. plan() changes one setting the way
connect.py changes everything: old and new text, backup, Undo.
"""

import json
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import connect
import harnesses
from agent_details import _read, _strip_jsonc
from i18n import _, ngettext

HOME = Path.home()
LEVELS = ("low", "low", "medium", "high")     # risk 0–3 → overall level


@dataclass
class Choice:
    value: object             # None: the key is removed (the agent's default applies)
    label: str
    explain: str
    risk: int = 1             # 0 safe, 1 low, 2 medium, 3 high


@dataclass
class Setting:
    key: str                  # dotted path in the file
    title: str
    explain: str
    choices: list
    current: object = None    # value in the file, None when not set
    default: object = None    # what the agent does when it is not set
    path: Path | None = None
    fmt: str = "json"         # json, toml, yaml
    terms: tuple = ()         # glossary words this setting uses
    editable: bool = True
    recommended: object = None

    def value(self):
        return self.default if self.current is None else self.current

    def choice(self) -> Choice:
        value = self.value()
        found = next((c for c in self.choices if _same(c.value, value)), None)
        if found is None and self.current is None:
            found = next((c for c in self.choices if c.value is None), None)
        return found or Choice(value, _("Custom: {0}").format(_short(value)),
                               _("Set by hand; AI Agents Map leaves it as it is."), 1)


@dataclass
class Risk:
    level: str                # high, medium, low; ok: a protection that is in place
    title: str
    why: str
    lower: str                # what lowers it
    fix_key: str = ""         # a setting whose recommended value lowers it
    fix_value: object = None


@dataclass
class Report:
    agent: str
    known: bool
    settings: list = field(default_factory=list)
    rules: list = field(default_factory=list)          # (title, explain, entries)
    risks: list = field(default_factory=list)
    files: list = field(default_factory=list)
    note: str = ""
    glossary: tuple = ("sandbox", "approval", "rules", "mcp")    # words explained besides the settings'

    def level(self) -> str:
        order = ("low", "medium", "high")
        found = [r.level for r in self.risks if r.level in order] + [LEVELS[s.choice().risk] for s in self.settings]
        return max(found, key=order.index) if found else "medium"

    def terms(self) -> list:
        seen = []
        for setting in self.settings:
            for term in setting.terms:
                if term not in seen:
                    seen.append(term)
        for term in self.glossary:
            if term not in seen:
                seen.append(term)
        return seen


def _same(a, b) -> bool:
    if isinstance(a, list) and isinstance(b, list):
        return sorted(map(str, a)) == sorted(map(str, b))
    return a == b


def _short(value) -> str:
    if isinstance(value, list):
        return ", ".join(map(str, value))[:80]
    return str(value)[:80]


def terms() -> dict:
    """The words the card uses, explained for someone new to agents."""
    return {
        "sandbox": (_("Sandbox"), _(
            "A fenced-off area the agent's commands run in. Inside it a command can only touch the project folder "
            "(and what you allow), not the rest of your home folder, your SSH keys or browser data, and often "
            "not the internet. Without a sandbox a command the agent runs can do anything you can do.")),
        "approval": (_("Approval (asking first)"), _(
            "Before the agent edits a file or runs a command, it shows you what it wants to do and waits for "
            "Yes or No. The fewer things it may do without asking, the more you stay in control, and the more "
            "often you are asked.")),
        "rules": (_("Allow and deny rules"), _(
            "Lists of commands or tools the agent may use without asking (allow) or never (deny), for example "
            "“git status” allowed, “rm -rf” denied. Deny wins over allow. A rule that allows "
            "every shell command switches approval off for the shell.")),
        "trusted": (_("Trusted folders"), _(
            "Folders you said the agent may work in. In a trusted folder it may read files and use the folder's "
            "own instructions and settings, which could come from someone else when you clone a project.")),
        "network": (_("Network access"), _(
            "Whether commands may reach the internet. Code that can read your files and send data out could leak "
            "them; downloading packages also needs the network.")),
        "bypass": (_("Bypass (“YOLO”)"), _(
            "The agent does everything without asking and without limits. Meant only for a throwaway virtual "
            "machine or container. A wrong command, or instructions hidden in a web page or file (prompt "
            "injection), then go straight through.")),
        "classifier": (_("Automatic review"), _(
            "A second AI model looks at each action and lets the harmless ones through without asking you. Fewer "
            "questions, but it is a judgement, not a wall: it can be wrong.")),
        "mcp": (_("MCP servers"), _(
            "Small programs that give the agent extra tools (a browser, a database, an app). They run as you, "
            "with your rights, and the agent may call them; install only ones you trust and give them the "
            "smallest keys they need.")),
        "redaction": (_("Secret redaction"), _(
            "Text that looks like a password, token or API key is masked before the model sees it or it is "
            "written to logs.")),
        "tools": (_("Tools"), _(
            "What the agent can do at all: read files, write files, run shell commands, search the web. A tool "
            "it does not have cannot be misused.")),
        "loopback": (_("Only this PC (127.0.0.1)"), _(
            "A server that listens on 127.0.0.1 (localhost) can be reached only from programs on this PC. One that "
            "listens on 0.0.0.0 can be reached from every device on your network, and local LLM servers have no "
            "password.")),
        "localapi": (_("Local API"), _(
            "The address agents send their questions to. Every program on this PC can use it too, and read what the "
            "model answers; it needs no login.")),
        "models": (_("Model files"), _(
            "A model is a large data file. GGUF files from known publishers are safe to load; be careful with other "
            "formats (pickle .bin/.pt) and with models that come with their own code to run.")),
        "injection": (_("Prompt injection"), _(
            "Instructions hidden in a web page, file or tool answer that try to make the agent do something you "
            "did not ask for. Asking first, a sandbox and no network are what stop it.")),
    }


# --- reading ---------------------------------------------------------------------------------

def _json_file(path: Path) -> dict:
    text = _read(path)
    if not text:
        return {}
    try:
        data = json.loads(_strip_jsonc(text) if "//" in text or "/*" in text else text)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _dig(data, key: str):
    for part in key.split("."):
        if not isinstance(data, dict):
            return None
        data = data.get(part)
    return data


def _yaml_value(text: str, key: str):
    """`block.key` from YAML text (two levels, scalars only), or None."""
    top, _sep, sub = key.partition(".")
    block = re.search(rf"^{re.escape(top)}:[ \t]*\n((?:[ \t]+.*\n?|[ \t]*\n)+)", text, re.MULTILINE)
    if not block:
        return None
    found = re.search(rf"^[ \t]+{re.escape(sub)}:[ \t]*(.*?)[ \t]*(?:#.*)?$", block.group(1), re.MULTILINE)
    if not found:
        return None
    raw = found.group(1).strip()
    if raw.lower() in ("true", "yes", "on") and not found.group(1).startswith(("'", '"')):
        return True
    if raw.lower() in ("false", "no") and not found.group(1).startswith(("'", '"')):
        return False
    return raw.strip("'\"")


def _strs(value) -> list:
    return [str(v)[:200] for v in value][:200] if isinstance(value, list) else []


def _rebuild():
    """Which agent's settings can be read (Harness.security); run again when add-ons change agents."""
    global READERS
    READERS = {h.command: h.security for h in harnesses.all_harnesses() if h.security is not None}


_rebuild()
harnesses.on_change(_rebuild)

def known(agent: str) -> bool:
    return agent in READERS


def report_for(agent: str, mcp_servers: int = 0) -> Report:
    report = Report(agent, agent in READERS)
    reader = READERS.get(agent)
    if reader is not None:
        reader(report)
    else:
        report.note = _("AI Agents Map does not know this agent's security settings yet. Look in its own "
                        "settings or documentation for “permissions”, “approval” and "
                        "“sandbox”.")
    report.risks = risks(report, mcp_servers)
    return report


# --- risk management -------------------------------------------------------------------------

def _setting(report: Report, key: str):
    return next((s for s in report.settings if s.key == key), None)


def _risk_of(report: Report, key: str) -> int:
    setting = _setting(report, key)
    return setting.choice().risk if setting else -1


def risks(report: Report, mcp_servers: int = 0) -> list:
    """What the settings mean together, most serious first, each with what lowers it."""
    found = []
    agent = report.agent
    sandbox_key = {"claude": "sandbox.enabled", "codex": "sandbox_mode", "cursor-agent": "sandbox.mode"}.get(agent)
    sandbox = _setting(report, sandbox_key) if sandbox_key else None
    sandboxed = sandbox is not None and sandbox.value() in (True, "enabled", "read-only", "workspace-write")
    asks_key = {"claude": "permissions.defaultMode", "codex": "approval_policy", "copilot": "defaultPermissionMode",
                "cursor-agent": "approvalMode", "hermes": "approvals.mode", "opencode": "permission.bash",
                "pi": "defaultTools"}.get(agent)
    asks = _setting(report, asks_key) if asks_key else None
    if asks is not None and asks.choice().risk >= 3:
        found.append(Risk("high", _("Runs commands without asking"), _(
            "{0}: a wrong command or a prompt injection is carried out at once, with your rights.").format(
                asks.choice().label),
            _("Let it ask first, or use this only in a container or virtual machine."), asks.key, asks.recommended))
    if agent == "codex" and _risk_of(report, "sandbox_mode") == 3:
        found.append(Risk("high", _("No sandbox"), _("Commands may change anything in your home folder."),
                          _("Use the project-folder sandbox."), "sandbox_mode", "workspace-write"))
    elif sandbox_key and not sandboxed and asks is not None and asks.choice().risk >= 2:
        found.append(Risk("medium", _("Acts on its own without a sandbox"), _(
            "It may do things without asking, and nothing fences in what the commands touch."),
            _("Switch the sandbox on, or let it ask first."), sandbox_key, _setting(report, sandbox_key).recommended))
    for setting in report.settings:
        if setting.key in (asks_key, sandbox_key):
            continue
        if setting.choice().risk >= 2 and setting.editable:
            found.append(Risk("medium", setting.title, setting.choice().explain,
                              _("Choose “{0}”.").format(next(
                                  (c.label for c in setting.choices if _same(c.value, setting.recommended)), "")),
                              setting.key, setting.recommended))
    for title, key, entries in report.rules:
        if key.endswith("allow") and any(re.fullmatch(r"(Bash|Shell)(\(\*?\)|\(\*\*?\))?|\*", e) for e in entries):
            found.append(Risk("high", _("Every shell command allowed"), _(
                "An allow rule lets any command run without asking."), _(
                "Allow single commands such as Bash(git status) instead.")))
    if agent == "claude" and _risk_of(report, "permissions.disableBypassPermissionsMode") == 1:
        found.append(Risk("low", _("Bypass mode can be switched on"), _(
            "Starting it with --dangerously-skip-permissions switches every check off."),
            _("Block bypass mode."), "permissions.disableBypassPermissionsMode", "disable"))
    if mcp_servers:
        found.append(Risk("low", ngettext("1 MCP server", "{0} MCP servers", mcp_servers).format(mcp_servers), _(
            "Each runs as you and gives the agent more tools; one that is compromised can read or change "
            "your files."), _("Keep only servers you use and trust; remove the others on the map.")))
    if not sandboxed and report.known:
        found.append(Risk("low", _("Works with your rights"), _(
            "Without a sandbox, what the agent runs can read everything you can: SSH keys, browser profiles, "
            "documents."), _("Start it in the project folder, keep secrets out of it, review what it wants "
                             "to run.")))
    for setting in report.settings:
        if setting.choice().risk == 0:
            found.append(Risk("ok", f"{setting.title}: {setting.choice().label}", setting.choice().explain, ""))
    if sandboxed and sandbox.choice().risk != 0:
        found.append(Risk("ok", f"{sandbox.title}: {sandbox.choice().label}", sandbox.choice().explain, ""))
    order = ("high", "medium", "low", "ok")
    return sorted(found, key=lambda r: order.index(r.level))


LOOPBACK = {"127.0.0.1", "localhost", "::1", "[::1]"}


def runtime_report(rt) -> Report:
    """A local LLM: no settings of its own here, but who can reach it and what it runs."""
    report = Report(rt.key, False, glossary=("loopback", "localapi", "network", "models"))
    if not rt.installed:
        return report
    host = (rt.address or "").rpartition(":")[0]
    if host and host not in LOOPBACK:
        report.risks.append(Risk("high", _("Reachable from your network"), _(
            "It listens on {0}: other devices on your network can use it and see what is asked, without a "
            "password.").format(rt.address), _(
            "Let it listen on 127.0.0.1 only (its host setting, e.g. OLLAMA_HOST=127.0.0.1 or --host 127.0.0.1).")))
    elif host:
        report.risks.append(Risk("ok", _("Only this PC can reach it"), _("It listens on {0}.").format(rt.address),
                                 ""))
    report.risks.append(Risk("low", _("No password on the local API"), _(
        "Any program you run, and other users of this PC, can use it while it runs."), _(
        "Stop it when no agent needs it (AI Agents Map does that for connected agents).")))
    report.risks.append(Risk("low", _("Models come from the web"), _(
        "A model file is data, but a crafted one can attack the program that loads it."), _(
        "Get GGUF models from known publishers and keep the local LLM up to date.")))
    return report


# --- changing one setting --------------------------------------------------------------------

def _json_set(text: str, key: str, value) -> str:
    data = connect._json(text)
    parts = key.split(".")
    node = data
    for part in parts[:-1]:
        child = node.get(part)
        if isinstance(child, str) and node is data and part == "permission":
            child = {"*": child}           # OpenCode: one value for everything → per tool
        if not isinstance(child, dict):
            if value is None:
                return text
            child = {}
        node[part] = child
        node = child
    if value is None:
        node.pop(parts[-1], None)
        if not node and len(parts) > 1:
            parent = _dig(data, ".".join(parts[:-2])) if len(parts) > 2 else data
            parent.pop(parts[-2], None)
    else:
        node[parts[-1]] = value
    return connect._dump(data)


def _toml_scalar(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return json.dumps(value)


def toml_set(text: str, key: str, value) -> str:
    """One `key` or `table.key` in TOML text; everything else stays as written."""
    table, _sep, name = key.rpartition(".")
    lines = text.splitlines(keepends=True)
    headers = [i for i, line in enumerate(lines) if re.match(r"\s*\[", line)]
    if table:
        start = next((i for i in headers if re.match(rf"\s*\[\s*{re.escape(table)}\s*\]\s*(#.*)?$", lines[i])), None)
        if start is None:
            if value is None:
                return text
            tail = "" if not text or text.endswith("\n") else "\n"
            return text + tail + ("\n" if text.strip() else "") + f"[{table}]\n{name} = {_toml_scalar(value)}\n"
        end = next((i for i in headers if i > start), len(lines))
        first = start + 1
    else:
        first, end = 0, headers[0] if headers else len(lines)
    for i in range(first, end):
        if re.match(rf"\s*{re.escape(name)}\s*=", lines[i]):
            if value is None:
                del lines[i]
            else:
                lines[i] = f"{name} = {_toml_scalar(value)}\n"
            return "".join(lines)
    if value is None:
        return text
    lines.insert(first, f"{name} = {_toml_scalar(value)}\n")
    return "".join(lines)


def _yaml_scalar(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return json.dumps(str(value))       # quoted: an unquoted off would read as false


def yaml_set(text: str, key: str, value) -> str:
    """`block.key` in YAML text (two levels); everything else stays as written."""
    top, _sep, sub = key.partition(".")
    span = connect._top_block(text, top)
    line = f"  {sub}: {_yaml_scalar(value)}\n"
    if span is None:
        if value is None:
            return text
        tail = "" if not text or text.endswith("\n") else "\n"
        return text + tail + f"{top}:\n{line}"
    start, end = span
    block = text[start:end]
    head, _nl, body = block.partition("\n")
    if re.match(rf"{re.escape(top)}:[ \t]*\{{\s*\}}", head):           # `top: {}`
        head, body = f"{top}:", ""
    found = re.search(rf"^[ \t]+{re.escape(sub)}:.*(\n|$)", body, re.MULTILINE)
    if found:
        body = body[:found.start()] + ("" if value is None else line) + body[found.end():]
    elif value is not None:
        body = line + body
    return text[:start] + head + "\n" + body + text[end:]


def plan(agent: str, key: str, value) -> connect.Plan:
    """The change of one security setting, for the preview (old → new text)."""
    report = report_for(agent)
    setting = _setting(report, key)
    if setting is None or not setting.editable:
        raise ValueError(f"{key} cannot be changed here")
    if not any(_same(c.value, value) for c in setting.choices):
        raise ValueError(f"{value!r} is not a choice for {key}")
    old = connect._read(setting.path)
    if setting.fmt == "toml":
        new = toml_set(old, key, value)
        check = _dig(tomllib.loads(new), key)
        if (check if value is not None else None) != value:
            raise ValueError("the TOML file could not be changed safely")
    elif setting.fmt == "yaml":
        new = yaml_set(old, key, value)
        if value is not None and _yaml_value(new, key) not in (value, str(value)):
            raise ValueError("the YAML file could not be changed safely")
    else:
        new = _json_set(old, key, value)
    result = connect.Plan(agent, "", "security")
    if new != old:
        result.changes.append(connect.Change(setting.path, old, new))
    return result
