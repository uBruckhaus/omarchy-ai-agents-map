"""Updates for the local MCP servers, looked up on the web by an installed agent once a day
(Settings › Daily AI checks, off by default: it uses the agent's tokens; ai_helper records them).

What is checked: servers in the map's list (mcp_library) whose version can be told from how they
are started:
- an app unpacked in ~/Applications/<app>-<version>/ (the Craft apps; also through a link such as
  ~/.local/bin/photocraft-cli);
- an npm package pinned to a version (npx -y pkg@1.2.3); "@latest" and unpinned ones always run
  the newest, so they are left out;
- a PyPI package pinned to a version (uvx pkg==1.2.3, uvx pkg@1.2.3).

One prompt for all of them; the answer (JSON) comes from the web and is checked before it is
shown: a known server name, a plain version number newer than the installed one, an https link,
one short line of notes. Nothing in it is ever run or downloaded: Update opens the release page,
or "Do it with Claude…" gives the agent the task in a terminal tab, where it asks before each step.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit

import ai_core
import ai_helper
import mcp_scan
from agent_updates import newer

HOME = Path.home()
APPS = HOME / "Applications"
CACHE = HOME / ".cache/ai-agents-map/mcp-updates.json"
CHECK_EVERY = 24 * 3600
RETRY_FAILED = 6 * 3600
MAX_SERVERS = 40
_VERSION = re.compile(r"^\d+(?:\.\d+){1,3}$")
_PINNED_NPM = re.compile(r"^(?P<pkg>(?:@[a-z0-9][\w.-]*/)?[a-z0-9][\w.-]*)@(?P<version>\d+(?:\.\d+){1,3})$", re.I)
_PINNED_PYPI = re.compile(r"^(?P<pkg>[a-z0-9][\w.-]*)(?:==|@)(?P<version>\d+(?:\.\d+){1,3})$", re.I)

PROMPT = """For each of these locally installed MCP servers, find the newest released version, from the
project's official release page (GitHub releases, its homepage, npm or PyPI):
{lines}
Answer only with JSON: {{"servers": [{{"name": "<name as above>", "latest": "<version, digits and dots>",
"release_url": "<https link to that release or the download page>", "notes": "<one short sentence: what is new>"}}]}}.
Use null for latest and release_url when you cannot find them for sure. Do not guess."""

SCHEMA = {"type": "object", "additionalProperties": False, "required": ["servers"],
          "properties": {"servers": {"type": "array", "items": {
              "type": "object", "additionalProperties": False, "required": ["name", "latest", "release_url", "notes"],
              "properties": {"name": {"type": "string"}, "latest": {"type": ["string", "null"]},
                             "release_url": {"type": ["string", "null"]}, "notes": {"type": "string"}}}}}}


@dataclass
class Server:
    name: str            # its name in the map's list
    app: str             # the app or package
    version: str         # installed
    source: str          # "app", "npm", "pypi"
    program: str = ""    # the program the agents start (an app's)


@dataclass
class Found:
    name: str
    app: str
    current: str
    latest: str
    url: str
    notes: str = ""
    source: str = ""
    program: str = ""


def _app_of(path: str) -> tuple[str, str] | None:
    """(app, version) when the program lies in ~/Applications/<app>-<version>/ (links resolved)."""
    try:
        real = Path(path).expanduser().resolve()
    except (OSError, RuntimeError):
        return None
    if not real.is_relative_to(APPS) or real == APPS:
        return None
    found = mcp_scan.app_version(real.relative_to(APPS).parts[0])
    return (found[0], mcp_scan.version_text(found[1])) if found else None


def installed_servers(library: dict | None = None) -> list[Server]:
    if library is None:
        import mcp_library
        library = mcp_library.load()
    servers = []
    for name, entry in list(library.items())[:MAX_SERVERS]:
        if not isinstance(entry, dict) or not isinstance(entry.get("command"), str):
            continue
        command = entry["command"]
        args = [a for a in entry.get("args") or [] if isinstance(a, str)]
        tool = Path(command).name
        first = next((a for a in args if not a.startswith("-")), "")
        if tool in ("npx", "bunx", "pnpx"):
            hit = _PINNED_NPM.match(first)
            if hit:
                servers.append(Server(name, hit["pkg"], hit["version"], "npm"))
        elif tool in ("uvx", "pipx"):
            hit = _PINNED_PYPI.match(args[args.index("run") + 1] if tool == "pipx" and "run" in args[:-1] else first)
            if hit:
                servers.append(Server(name, hit["pkg"], hit["version"], "pypi"))
        elif "/" in command:
            app = _app_of(command)
            if app:
                servers.append(Server(name, app[0], app[1], "app", command))
    return servers


def prompt_for(servers) -> str:
    where = {"app": "an app unpacked from its AppImage", "npm": "npm package", "pypi": "PyPI package"}
    return PROMPT.format(lines="\n".join(f"- {s.name}: {s.app} {s.version} ({where[s.source]})" for s in servers))


def _answer_json(text: str) -> dict:
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", text):
        try:
            value, _end = decoder.raw_decode(text, match.start())
        except ValueError:
            continue
        if isinstance(value, dict) and isinstance(value.get("servers"), list):
            return value
    raise ValueError("no JSON answer")


def _safe_url(url) -> str:
    if not isinstance(url, str) or len(url) > 300 or any(c.isspace() or c in "\"'<>`\\" for c in url):
        return ""
    parts = urlsplit(url)
    return url if parts.scheme == "https" and "." in (parts.hostname or "") else ""


def _one_line(text, most: int = 160) -> str:
    text = " ".join(str(text or "").split())
    text = "".join(c for c in text if c.isprintable())
    return text if len(text) <= most else text[:most - 1].rstrip() + "…"


def validate(answer: dict, servers) -> list[Found]:
    """What the agent found, only where it holds up: a server asked about, a newer plain version, an
    https link."""
    by_name = {s.name: s for s in servers}
    found = []
    for item in answer.get("servers", [])[:MAX_SERVERS]:
        if not isinstance(item, dict) or item.get("name") not in by_name:
            continue
        server = by_name.pop(item["name"])
        latest = str(item.get("latest") or "").strip().lstrip("vV")
        url = _safe_url(item.get("release_url"))
        if not _VERSION.match(latest) or not url or not newer(latest, server.version):
            continue
        found.append(Found(server.name, server.app, server.version, latest, url, _one_line(item.get("notes")),
                           server.source, server.program))
    return found


def _read(cache: Path) -> dict:
    try:
        with open(cache, encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def cached(cache: Path = CACHE) -> list[Found]:
    found = []
    for item in _read(cache).get("found", []):
        try:
            found.append(Found(**{k: str(v) for k, v in item.items() if k in Found.__dataclass_fields__}))
        except TypeError:
            continue
    return found


def still_open(found, servers) -> list[Found]:
    """Hints still worth showing: the server is there in the version the answer was about."""
    now = {s.name: s.version for s in servers}
    return [f for f in found if now.get(f.name) == f.current and newer(f.latest, f.current)]


def due(now: float | None = None, cache: Path = CACHE) -> bool:
    saved = _read(cache)
    checked = saved.get("checked")
    if not isinstance(checked, (int, float)):
        return True
    wait = CHECK_EVERY if saved.get("ok") else RETRY_FAILED
    return not 0 <= (now if now is not None else time.time()) - checked < wait


def check(installed, now: float | None = None, cache: Path = CACHE, look_up=None,
          library: dict | None = None, force: bool = False) -> list[Found]:
    """The updates found, asking the chosen agent when a day has passed (Settings › Daily AI checks
    must be on; the caller checks that; force: ask now, Smart Check's Check now). Slow when it asks
    (a minute or more): call it in a thread."""
    now = now if now is not None else time.time()
    servers = installed_servers(library)
    if not servers:
        return []
    if not force and not due(now, cache):
        return still_open(cached(cache), servers)
    agent = ai_helper.chosen(installed)
    if agent is None:
        return still_open(cached(cache), servers)
    try:
        def result(text):
            count = len(validate(_answer_json(text), servers))
            return f"{count} update{'s' if count != 1 else ''} found"
        text = (look_up or ai_helper.look_up)(agent, "mcp-updates", prompt_for(servers), SCHEMA, result_of=result)
        found = validate(_answer_json(text), servers)
    except Exception as error:      # the web, the agent, its sign-in: keep the old answer, try later
        ai_core.atomic_write(cache, json.dumps({**_read(cache), "checked": now, "ok": False,
                                                "error": str(error)[:200]}))
        return still_open(cached(cache), servers)
    ai_core.atomic_write(cache, json.dumps({"checked": now, "ok": True, "agent": agent,
                                            "found": [asdict(f) for f in found]}, indent=1))
    return found


def task_prompt(update: Found) -> str:
    """What "Do it with <agent>…" asks the agent to do in its terminal tab."""
    if update.source == "app":
        return (f"Download the MCP server app \"{update.app}\" {update.latest} (I have {update.current}, in "
                f"{APPS / (update.app + '-' + update.current)}). Release: {update.url}\n"
                f"Get the Linux x86_64 AppImage of {update.latest} from the official release only, save it in "
                f"{APPS} under its release file name and check its checksum if the release gives one. Do not "
                f"unpack it, do not change any agent's settings and do not delete the old version: AI Agents "
                f"Map finds the new AppImage, unpacks it and moves the agents over, with Undo. Ask me before "
                f"each step that changes files.")
    manager = "npm" if update.source == "npm" else "PyPI"
    return (f"The MCP server \"{update.name}\" runs the {manager} package {update.app} pinned to {update.current}; "
            f"{update.latest} is out ({update.url}). Show me what changed and which agents' MCP settings pin "
            f"{update.app}@{update.current}, then, after I agree, change the pinned version to {update.latest}. "
            f"Ask me before each change.")
