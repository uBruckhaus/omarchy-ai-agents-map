"""Agent adapters as data (docs/adapter-format.md): adapters/<command>.toml read, checked and turned
into the same harnesses.Harness a harness_<command>.py module builds. Texts are English; they are
translated where they are shown (i18n), like the app's own.

Nothing in a TOML file runs: what an adapter cannot say as a fact (a session store of its own, edits
of an agent's config text) comes from readers the app has (READERS, USAGE, …) or from its hooks:
adapters/<command>.py beside the TOML (in an add-on: hooks.py), functions with fixed names (HOOKS) at
the places the TOML marks "hook".
"""

from __future__ import annotations

import importlib.util
import tomllib
import types
from pathlib import Path

import harnesses
from harnesses import Harness, LocalLlm, Mcp, Sessions, Skills, Way, ref
from i18n import _

ADAPTERS = Path(__file__).resolve().parent / "adapters"
MAX_BYTES = 256 * 1024


class AdapterError(ValueError):
    """An adapter file that cannot be used: the message names the file and the key."""


# --- what a file may hold ------------------------------------------------------------------------

STR, BOOL, INT, STRS, TABLE, TABLES = "text", "true/false", "a number", "a list of texts", "a table", "tables"
TABLE_OR_HOOK, HOOK = "a table or \"hook\"", "\"hook\""
# The hooks' fixed names: the functions a hook file may give, each used where the TOML says "hook".
HOOKS = ("sessions", "security", "local_llm_connected", "local_llm_active", "local_llm_transform",
         "skills_listed", "skills_write", "mcp_read", "mcp_write", "mcp_native", "details",
         "models_choices", "models_plan")

TOP = {"command": STR, "name": STR, "label": STR, "full_name": STR, "icon": STR, "icon_font": STR,
       "spec": STR, "package": STR, "package_only": BOOL, "own_install_counts": BOOL, "short_version": BOOL,
       "offered": BOOL, "short_model_flag": BOOL, "fixed_provider": STR, "self_served": BOOL, "homepage": STR,
       "data": STRS, "install": TABLES, "update": TABLE, "sessions": TABLE, "skills": TABLE, "mcp": TABLE,
       "access": TABLE, "security": TABLE, "login_status": TABLE, "installer": STR, "usage": STR,
       "local_llm": TABLE, "ask": TABLE, "configure": TABLES, "home": STR, "activity": STRS, "details": TABLE,
       "program_dir": STR, "start": STRS, "web_search": BOOL, "chat_app": BOOL, "herdr": STR, "research": TABLE, "rewrite": TABLE, "task": TABLE,
       "models": TABLE, "tools": TABLE, "replies": TABLE}
WAY = {"kind": STR, "spec": STR, "tool": STR, "argv": STRS, "sudo": BOOL, "url": STR, "note": STR,
       "update": STRS, "packages": "a list of [manager, name]"}
UPDATE = {"self": STRS, "latest": STR}
# One prompt, one answer, without tools: argv with "{prompt}" where the text goes, and the games the
# app may play with it (AI Playground's chess tab).
ASK = {"argv": STRS, "note": STR, "games": STRS, "effort": TABLE}
GAMES = ("chess",)
# How long the agent may think (AI Playground's Thinking time): per level, its own options for how
# hard the model thinks (ask.effort), put in before the prompt.
THINKING = ("quick", "normal", "deep", "max")
SESSIONS = {"reader": STR, "hook": BOOL, "note": STR, "note_args": STRS}
DETAILS = {"reader": STR, "dir": STR, "hook": BOOL}     # its model and providers, read from its settings
MODELS = {"reader": STR, "hook": BOOL}                  # the online models it can be switched to
TOOLS = {"file": STR, "key": STR}                       # the tools it is given (a JSON list)
# One prompt with no tools but the web (research: Add skills and MCP look-ups, the daily AI checks;
# output: how its answer and token use come back, ai_helper.OUTPUTS), or none at all (rewrite: a
# skill rewritten for an MCP server's new tools); the agent itself, never a shell. task: the agent
# started in a terminal tab with a first prompt ("Do it with …"), asking before each step itself.
RESEARCH, REWRITE, TASK = {"argv": STRS, "output": STR}, {"argv": STRS}, {"argv": STRS}
OUTPUTS = ("text", "claude-json", "codex-jsonl")
# Its session files, for how fast its latest reply came (the map's "t/s"): the folder, how many
# folders deep the files are in it, and their format (one usage_box.FORMATS has).
REPLIES = {"folder": STR, "depth": INT, "format": STR, "contexts": TABLE}   # contexts: model name prefix -> tokens
REPLY_FORMATS = ("claude-jsonl", "codex-jsonl", "pi-jsonl")
SKILLS = {"dirs": STRS, "own": STR, "listed": TABLE_OR_HOOK, "settings_list": BOOL, "settings_file": STR,
          "write": HOOK}
LISTED = {"file": STR, "format": STR, "key": STR}       # extra skill folders a settings file lists
MCP = {"path": STR, "format": STR, "key": STR, "shape": STR, "in_app": STR, "defaults": TABLE,
       "read": HOOK, "write": HOOK, "native": HOOK}
LOCAL_LLM = {"files": STRS, "hooks": BOOL}             # the files connect.plan edits, by the local_llm_* hooks
LOGIN_STATUS = {"argv": STRS, "no": STR, "yes": STR}     # a command whose output says signed in or not
ACCESS = {"how": STR, "login": STR, "files": STRS, "env": STRS, "local_ok": BOOL, "logins": TABLES, "key": TABLE,
          "logout": STRS}
LOGIN = {"label": STR, "argv": STRS, "note": STR}
CONFIGURE = {"label": STR, "argv": STRS, "note": STR}    # its own setup command, in a terminal (Configure…)
KEY = {"var": STR, "url": STR, "stdin": STRS, "env_file": STR}
SECURITY = {"file": STR, "format": STR, "settings": TABLES, "rules": TABLES, "note": STR, "hook": BOOL,
            "sandbox": STR, "asks": STR}     # the settings that are its sandbox / when it asks first
SETTING = {"key": STR, "title": STR, "explain": STR, "default": "a value", "recommended": "a value",
           "terms": STRS, "editable": BOOL, "choices": TABLES, "current_if": STR, "warn": TABLE}
WARN = {"title": STR, "explain": STR, "fix": STR}         # a low risk shown while a risk-1 choice is set
CHOICE = {"value": "a value", "unset": BOOL, "label": STR, "explain": STR, "risk": INT}
RULE = {"key": STR, "title": STR, "also": STR, "keys_where": TABLE}   # also: a second file to fall back on;
                                                                        # keys_where: the table's keys whose
                                                                        # entry has these values
REQUIRED = {"top": ("command", "name"), "way": ("kind",), "login": ("label", "argv"), "configure": ("label", "argv"), "key": ("var",),
            "setting": ("key", "title", "explain", "choices"), "choice": ("label", "explain"),
            "rule": ("key", "title"), "security": ("file", "format"), "access": ("how",), "skills": ("dirs", "own"),
            "listed": ("file", "format", "key"), "login_status": ("argv", "no", "yes"), "ask": ("argv",)}

# Readers and version sources the app has; a file names them.
READERS = {"claude-files": ("claude_sessions", "collect_by_id")}
LATEST = {"claude-channel": ("agent_updates", "claude_channel_latest")}
INSTALLERS = {"agy": ("install_ctl", "Agy"), "dsh": ("install_ctl", "Dsh"),   # installers the app has
              "bionic": ("install_ctl", "Bionic")}
USAGE = {"pi": ("agent_status", "pi"), "hermes": ("agent_status", "hermes"),      # usage readers the app has
         "opencode": ("agent_status", "opencode")}
DETAIL_READERS = {name: ("agent_readers", function) for name, function in (     # model + providers readers
    ("claude", "claude"), ("codex", "codex"), ("copilot", "copilot"), ("cursor", "cursor"),
    ("opencode", "opencode"), ("pi", "pi_like"), ("hermes", "hermes"), ("dsh", "dsh"), ("openclaw", "openclaw"),
    ("lmstudio-app", "bionic"))}
MODEL_READERS = {"claude": ("online_models", "claude_choices", "claude_plan"),       # online model switchers
                 "codex": ("online_models", "codex_choices", "codex_plan")}
FORMATS = ("json", "toml", "yaml")


def _fits(value, kind: str) -> bool:
    if kind == STR:
        return isinstance(value, str)
    if kind == BOOL:
        return isinstance(value, bool)
    if kind == INT:
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == STRS:
        return isinstance(value, list) and all(isinstance(v, str) for v in value)
    if kind == TABLE:
        return isinstance(value, dict)
    if kind == TABLES:
        return isinstance(value, list) and all(isinstance(v, dict) for v in value)
    if kind == TABLE_OR_HOOK:
        return isinstance(value, dict) or value == "hook"
    if kind == HOOK:
        return value == "hook"
    if kind == "a list of [manager, name]":
        return isinstance(value, list) and all(isinstance(v, list) and len(v) == 2 and
                                               all(isinstance(x, str) for x in v) for v in value)
    return True                                                       # "a value": any TOML value


def _check(table: dict, allowed: dict, where: str, required: tuple = ()) -> None:
    for key, value in table.items():
        if key not in allowed:
            raise AdapterError(f"{where}: unknown key “{key}”")
        if not _fits(value, allowed[key]):
            raise AdapterError(f"{where}.{key}: must be {allowed[key]}")
    for key in required:
        if key not in table:
            raise AdapterError(f"{where}: “{key}” is missing")


def check(data: dict, name: str) -> None:
    """Refuse a file with an unknown key, a value of the wrong type or a missing one."""
    _check(data, TOP, name, REQUIRED["top"])
    for i, way in enumerate(data.get("install", [])):
        _check(way, WAY, f"{name}: install[{i}]", REQUIRED["way"])
    _check(data.get("update", {}), UPDATE, f"{name}: update")
    if "ask" in data:
        _check(data["ask"], ASK, f"{name}: ask", REQUIRED["ask"])
        argv = data["ask"]["argv"]
        # The agent itself, the prompt as one argument of its own: never a shell, never another program.
        if not argv or argv[0] != data["command"] or argv.count("{prompt}") != 1 \
                or any("{prompt}" in a and a != "{prompt}" for a in argv) or argv.count("{effort}") > 1 \
                or any("{effort}" in a and a != "{effort}" for a in argv):
            raise AdapterError(f"{name}: ask.argv must start with “{data['command']}” and hold “{{prompt}}” "
                               "once (and “{effort}” at most once), each as an argument of its own")
        for level, args in data["ask"].get("effort", {}).items():
            if level not in THINKING:
                raise AdapterError(f"{name}: ask.effort “{level}” is not one the app has ({', '.join(THINKING)})")
            if not _fits(args, STRS) or not args or not args[0].startswith("-") \
                    or any("{prompt}" in a or "{effort}" in a for a in args):
                raise AdapterError(f"{name}: ask.effort.{level} must be options (starting with “-”), "
                                   "without “{prompt}”")
        for game in data["ask"].get("games", []):
            if game not in GAMES:
                raise AdapterError(f"{name}: ask.games “{game}” is not one the app has ({', '.join(GAMES)})")
    for table, placeholders in (("research", ("{prompt}", "{schema}")), ("rewrite", ("{answer}",)),
                                ("task", ("{prompt}",))):
        if table not in data:
            continue
        _check(data[table], {"research": RESEARCH, "rewrite": REWRITE, "task": TASK}[table], f"{name}: {table}",
               ("argv",))
        if data[table].get("output", "text") not in OUTPUTS:
            raise AdapterError(f"{name}: {table}.output “{data[table]['output']}” is not one the app has "
                               f"({', '.join(OUTPUTS)})")
        argv = data[table]["argv"]
        if not argv or argv[0] != data["command"] or any(
                "{" in a and a not in placeholders for a in argv) or any(argv.count(p) > 1 for p in placeholders) \
                or (table in ("research", "task") and "{prompt}" not in argv):
            raise AdapterError(f"{name}: {table}.argv must start with “{data['command']}”; "
                               f"{', '.join(placeholders)} each once at most, as an argument of its own")
    if "details" in data:
        _check(data["details"], DETAILS, f"{name}: details")
        if data["details"].get("reader", "") not in ("", *DETAIL_READERS):
            raise AdapterError(f"{name}: details.reader “{data['details']['reader']}” is not one the app has "
                               f"({', '.join(DETAIL_READERS)})")
    if "models" in data:
        _check(data["models"], MODELS, f"{name}: models")
        if data["models"].get("reader", "") not in ("", *MODEL_READERS):
            raise AdapterError(f"{name}: models.reader “{data['models']['reader']}” is not one the app has")
    if "tools" in data:
        _check(data["tools"], TOOLS, f"{name}: tools", ("file", "key"))
    if "replies" in data:
        _check(data["replies"], REPLIES, f"{name}: replies", ("folder", "format"))
        if data["replies"]["format"] not in REPLY_FORMATS:
            raise AdapterError(f"{name}: replies.format “{data['replies']['format']}” is not one the app has "
                               f"({', '.join(REPLY_FORMATS)})")
        if not data["replies"]["folder"].startswith("~/") or not 0 <= data["replies"].get("depth", 1) <= 4:
            raise AdapterError(f"{name}: replies.folder must start with “~/”, replies.depth be 0 to 4")
        if not all(isinstance(k, str) and len(k) <= 100 and _fits(v, INT) and 0 < v < 10 ** 9
                   for k, v in data["replies"].get("contexts", {}).items()):
            raise AdapterError(f"{name}: replies.contexts must map model name beginnings to token counts")
    for i, entry in enumerate(data.get("configure", [])):
        _check(entry, CONFIGURE, f"{name}: configure[{i}]", REQUIRED["configure"])
        # The agent itself and plain words: never a shell, never another program.
        if not harnesses.configure_ok(entry["argv"], (data["command"],)):
            raise AdapterError(f"{name}: configure[{i}].argv must start with “{data['command']}”, plain words")
    # Arguments for the agent itself: plain words, never a shell or another program.
    if data.get("start") and not harnesses.configure_ok([data["command"], *data["start"]], (data["command"],)):
        raise AdapterError(f"{name}: start must be plain words")
    sessions = data.get("sessions", {})
    _check(sessions, SESSIONS, f"{name}: sessions")
    if "reader" in sessions and sessions["reader"] not in READERS:
        raise AdapterError(f"{name}: sessions.reader “{sessions['reader']}” is not one the app has "
                           f"({', '.join(READERS)})")
    if data.get("update", {}).get("latest", "") not in ("", *LATEST):
        raise AdapterError(f"{name}: update.latest “{data['update']['latest']}” is not one the app has")
    if "skills" in data:
        _check(data["skills"], SKILLS, f"{name}: skills", REQUIRED["skills"])
        if isinstance(data["skills"].get("listed"), dict):
            _check(data["skills"]["listed"], LISTED, f"{name}: skills.listed", REQUIRED["listed"])
            if data["skills"]["listed"]["format"] not in ("json", "toml"):
                raise AdapterError(f"{name}: skills.listed.format must be json or toml")
    _check(data.get("mcp", {}), MCP, f"{name}: mcp")
    if "local_llm" in data:
        _check(data["local_llm"], LOCAL_LLM, f"{name}: local_llm", ("files",))
    if data.get("usage", "") not in ("", *USAGE):
        raise AdapterError(f"{name}: usage “{data['usage']}” is not one the app has")
    if "login_status" in data:
        _check(data["login_status"], LOGIN_STATUS, f"{name}: login_status", REQUIRED["login_status"])
    if data.get("installer", "") not in ("", *INSTALLERS):
        raise AdapterError(f"{name}: installer “{data['installer']}” is not one the app has")
    if "access" in data:
        access = data["access"]
        _check(access, ACCESS, f"{name}: access", REQUIRED["access"])
        for i, login in enumerate(access.get("logins", [])):
            _check(login, LOGIN, f"{name}: access.logins[{i}]", REQUIRED["login"])
        if "key" in access:
            _check(access["key"], KEY, f"{name}: access.key", REQUIRED["key"])
    if "security" in data:
        security = data["security"]
        _check(security, SECURITY, f"{name}: security", () if security.get("hook") else REQUIRED["security"])
        if not security.get("hook") and security["format"] not in FORMATS:
            raise AdapterError(f"{name}: security.format must be one of {', '.join(FORMATS)}")
        for i, setting in enumerate(security.get("settings", [])):
            where = f"{name}: security.settings[{i}]"
            _check(setting, SETTING, where, REQUIRED["setting"])
            if "warn" in setting:
                _check(setting["warn"], WARN, f"{where}.warn", ("title", "explain", "fix"))
            for j, choice in enumerate(setting["choices"]):
                _check(choice, CHOICE, f"{where}.choices[{j}]", REQUIRED["choice"])
                if ("value" in choice) == bool(choice.get("unset")):
                    raise AdapterError(f"{where}.choices[{j}]: needs either “value” or “unset = true”")
                if not 0 <= choice.get("risk", 1) <= 3:
                    raise AdapterError(f"{where}.choices[{j}].risk: 0 (safe) to 3 (high)")
        for i, rule in enumerate(security.get("rules", [])):
            _check(rule, RULE, f"{name}: security.rules[{i}]", REQUIRED["rule"])


# --- turning facts into a Harness ----------------------------------------------------------------

def _icon(text: str) -> str:
    """"U+F06C4" → the glyph (a file holds no private-use characters, which do not show in editors)."""
    if text.upper().startswith("U+"):
        try:
            return chr(int(text[2:], 16))
        except ValueError:
            raise AdapterError(f"icon “{text}” is not a code point") from None
    return text


def _way(way: dict) -> Way:
    return Way(way["kind"], spec=way.get("spec", ""), tool=way.get("tool", ""), argv=tuple(way.get("argv", ())),
               sudo=way.get("sudo", False), packages=tuple(tuple(p) for p in way.get("packages", ())),
               url=way.get("url", ""), note=way.get("note", ""), update=tuple(way.get("update", ())))


def _access(facts: dict):
    """access() as the Python adapters have it: built when asked, texts translated then."""
    def access():
        from agent_status import Access
        from signin import KeyStep, Login
        key = facts.get("key")
        return Access(_(facts["how"]), facts.get("login", ""), tuple(facts.get("files", ())),
                      tuple(facts.get("env", ())), local_ok=facts.get("local_ok", False),
                      logins=tuple(Login(_(login["label"]), tuple(login["argv"]),
                                         _(login["note"]) if login.get("note") else "")
                                   for login in facts.get("logins", ())),
                      key=KeyStep(key["var"], key.get("url", ""), tuple(key.get("stdin", ())),
                                  key.get("env_file", "")) if key else None)
    return access


def _security(facts: dict):
    """security(report) from the declared settings: the file is read as its format says, every
    setting's current value looked up by its dotted key."""
    def security(report):
        from agent_security import HOME, Choice, Setting, _dig, _json_file, _read, _strs, _yaml_value
        fmt = facts["format"]
        path = HOME / facts["file"].removeprefix("~/")
        if fmt == "json":
            data = _json_file(path)
            current = lambda key: _dig(data, key)
        elif fmt == "toml":
            try:
                data = tomllib.loads(_read(path) or "")
            except tomllib.TOMLDecodeError:
                data = {}
            current = lambda key: _dig(data, key)
        else:
            text = _read(path) or ""
            current = lambda key: _yaml_value(text, key)
        report.files.append(path)
        if facts.get("note"):
            report.note = _(facts["note"])
        kinds = {"list": list, "text": str, "bool": bool}
        for s in facts.get("settings", ()):
            choices = [Choice(None if c.get("unset") else c["value"], _(c["label"]), _(c["explain"]),
                              c.get("risk", 1)) for c in s["choices"]]
            value = current(s["key"])
            if "current_if" in s and not isinstance(value, kinds[s["current_if"]]):
                value = None                  # e.g. Pi's tools: only a list counts as set
            warn = s.get("warn")
            report.settings.append(Setting(s["key"], _(s["title"]), _(s["explain"]), choices, value,
                                           s.get("default"), path, fmt, tuple(s.get("terms", ())),
                                           s.get("editable", True), s.get("recommended"),
                                           (_(warn["title"]), _(warn["explain"]), _(warn["fix"])) if warn else ()))
        for rule in facts.get("rules", ()):
            value = current(rule["key"])
            if "keys_where" in rule:          # e.g. Codex: the projects whose trust_level is "trusted"
                wanted = rule["keys_where"]
                value = [k for k, v in (value if isinstance(value, dict) else {}).items()
                         if isinstance(v, dict) and all(v.get(f) == x for f, x in wanted.items())]
            entries = _strs(value)
            if not entries and "also" in rule:   # e.g. Copilot: a managed file holds the list instead
                entries = _strs(_dig(_json_file(HOME / rule["also"].removeprefix("~/")), rule["key"]))
            if entries:
                report.rules.append((_(rule["title"]), rule["key"], entries))
    return security


def _listed(facts: dict):
    """listed() -> the extra skill folders a settings file lists (None when it lists none); a
    broken TOML file raises, as skills_ctl expects of listed()."""
    def listed():
        import connect
        import skills_ctl
        from agent_security import _dig
        path = skills_ctl.HOME / facts["file"].removeprefix("~/")
        data = skills_ctl._json(path) if facts["format"] == "json" else tomllib.loads(connect._read(path))
        return _dig(data, facts["key"])
    return listed


def _mcp_write(key: str, defaults: dict):
    """write(text, name, item) for an agent that needs some top-level keys in its MCP file
    (Muse refuses one without schema_version): the usual JSON edit, then those keys first."""
    def write(text: str, name: str, item):
        import connect
        import mcp_ctl
        new_text = mcp_ctl._json_text(text, key, name, item)
        data = connect._json(new_text)
        missing = {k: v for k, v in defaults.items() if k not in data}
        return connect._dump({**missing, **data}) if missing else new_text
    return write


def _installer(name: str):
    def installer():
        import importlib
        module, cls = INSTALLERS[name]
        return getattr(importlib.import_module(module), cls)()
    return installer


def harness(data: dict, name: str = "adapter", hooks=None) -> Harness:
    """The Harness of a checked adapter file; hooks: its hook functions (a module), where it says "hook"."""
    check(data, name)

    def hook(function: str):
        found = getattr(hooks, function, None) if hooks is not None else None
        if not callable(found):
            raise AdapterError(f"{name}: needs the hook {function}() (adapters/<command>.py, in an add-on hooks.py)")
        return found

    sessions = None
    facts = data.get("sessions", {})
    if "reader" in facts or facts.get("hook"):
        collect = ref(*READERS[facts["reader"]]) if "reader" in facts else hook("sessions")
        note_text, note_args = facts.get("note", ""), facts.get("note_args", [])
        sessions = Sessions(collect, lambda: _(note_text).format(*note_args) if note_text else "")
    update = data.get("update", {})
    skills = data.get("skills")
    mcp = data.get("mcp")
    llm = data.get("local_llm")
    security = data.get("security")
    simple = {key: data[key] for key in ("label", "full_name", "icon_font", "spec", "package", "package_only",
                                         "own_install_counts", "short_version", "offered", "short_model_flag",
                                         "fixed_provider", "self_served", "homepage") if key in data}
    if skills:
        listed = skills.get("listed")
        skills = Skills(tuple(skills["dirs"]), skills["own"], settings_list=skills.get("settings_list", False),
                        listed=hook("skills_listed") if listed == "hook" else _listed(listed) if listed else None,
                        settings_file=skills.get("settings_file", ""),
                        write=hook("skills_write") if skills.get("write") == "hook" else None)
    if mcp:
        write = hook("mcp_write") if mcp.get("write") == "hook" else \
            _mcp_write(mcp.get("key", "mcpServers"), mcp["defaults"]) if "defaults" in mcp else None
        mcp = Mcp(**{k: mcp[k] for k in ("path", "format", "key", "shape", "in_app") if k in mcp},
                  read=hook("mcp_read") if mcp.get("read") == "hook" else None, write=write,
                  native=hook("mcp_native") if mcp.get("native") == "hook" else None)
    if llm:
        llm = LocalLlm(tuple(llm["files"]), hook("local_llm_connected"), hook("local_llm_active"),
                       hook("local_llm_transform"))
    details = data.get("details", {})
    if details.get("hook"):
        details = hook("details")
    elif details.get("reader"):
        read = ref(*DETAIL_READERS[details["reader"]])
        folder = details.get("dir", "")
        details = (lambda d, read=read, folder=folder: read(folder, d)) if folder else read
    else:
        details = None
    models = data.get("models", {})
    if models.get("hook"):
        models = (hook("models_choices"), hook("models_plan"))
    elif models.get("reader"):
        module, choices, plan = MODEL_READERS[models["reader"]]
        models = (ref(module, choices), ref(module, plan))
    else:
        models = None
    security = security or {}
    return Harness(
        data["command"], data["name"], sessions=sessions, icon=_icon(data.get("icon", "")),
        access=_access(data["access"]) if "access" in data else None,
        security=(hook("security") if security.get("hook") else _security(security)) if security else None,
        sandbox_setting=security.get("sandbox", ""), asks_setting=security.get("asks", ""),
        home=data.get("home", ""), activity=tuple(data.get("activity", ())), details=details,
        program_dir=data.get("program_dir", ""), web_search=data.get("web_search", False),
        chat_app=data.get("chat_app", False), herdr=data.get("herdr", ""), start=tuple(data.get("start", ())),
        logout=tuple(data.get("access", {}).get("logout", ())),
        research=tuple(data["research"]["argv"]) if "research" in data else (),
        research_output=data["research"].get("output", "text") if "research" in data else "text",
        task=tuple(data["task"]["argv"]) if "task" in data else (),
        rewrite=tuple(data["rewrite"]["argv"]) if "rewrite" in data else (), models=models,
        tools_setting=(data["tools"]["file"], data["tools"]["key"]) if "tools" in data else (),
        replies=(data["replies"]["folder"], data["replies"].get("depth", 1), data["replies"]["format"],
                 tuple(sorted(data["replies"].get("contexts", {}).items()))) if "replies" in data else (),
        usage=ref(*USAGE[data["usage"]]) if "usage" in data else None,
        local_llm=llm, skills=skills, mcp=mcp,
        login_status=(tuple(data["login_status"]["argv"]), data["login_status"]["no"], data["login_status"]["yes"])
        if "login_status" in data else None,
        installer=_installer(data["installer"]) if "installer" in data else None,
        self_update=tuple(update.get("self", ())),
        latest=ref(*LATEST[update["latest"]]) if update.get("latest") else None,
        install=tuple(_way(w) for w in data.get("install", ())),
        ask=tuple(data["ask"]["argv"]) if "ask" in data else (),
        games=tuple(data["ask"].get("games", ())) if "ask" in data else (),
        effort=tuple((level, tuple(args)) for level, args in data["ask"].get("effort", {}).items())
        if "ask" in data else (),
        configure=tuple(harnesses.Configure(_(c["label"]), tuple(c["argv"]), _(c["note"]) if c.get("note") else "")
                        for c in data.get("configure", ())),
        data=tuple(data.get("data", ())), **simple)


def hooks_from(source: bytes, name: str):
    """A hook file's functions, from its bytes (an add-on's verified hooks.py; never read again)."""
    module = types.ModuleType(f"adapter_hooks_{name.replace('-', '_')}")
    module.__file__ = name
    exec(compile(source, name, "exec"), module.__dict__)
    return module


def hooks_module(command: str):
    """adapters/<command>.py of a built-in adapter, or None (tests reach its readers this way)."""
    path = ADAPTERS / f"{command}.py"
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location(f"adapter_hooks_{command.replace('-', '_')}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load(path: Path) -> Harness:
    """adapters/<command>.toml (and its hooks, adapters/<command>.py) → its Harness (AdapterError with
    the reason when it cannot be used)."""
    raw = path.read_bytes()
    if len(raw) > MAX_BYTES:
        raise AdapterError(f"{path.name}: larger than {MAX_BYTES // 1024} KiB")
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise AdapterError(f"{path.name}: {error}") from None
    return harness(data, path.name, hooks_module(path.stem))
