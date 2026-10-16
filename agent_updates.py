"""Updates for the installed agents and local LLMs, in the hint area below the cards.

Each is checked the way it was installed, so Update installs what was found:
- mise (most agents): `mise outdated --json`, one call for all; Update runs
  `mise upgrade <tool>` in a terminal of its own (MISE_USE_VERSIONS_HOST=0: from
  the tool's own releases, as mise's version service can lag behind).
- Claude Code: also its release channel (`autoUpdatesChannel` in
  ~/.claude/settings.json, "latest" or "stable") from npm's dist-tags, as its own
  "Update available!" message; Update runs `claude update` (its own installer)
  or `mise upgrade claude`.
- installed by AI Agents Map (install_ctl: local LLMs, DeepSeek Harness): the
  installer's latest release; Update opens the Update page (SHA-256 checked there).
- system packages are left to the system's own updates.

Checked in the background on open, at most every CHECK_EVERY seconds (the
answers are cached owner-only). Nothing is installed or run here except
`mise outdated`: an update runs only when the user clicks Update.
"""

import json
import re
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import ai_core
import netfetch

TAGS_URL = "https://registry.npmjs.org/-/package/@anthropic-ai/claude-code/dist-tags"
CACHE = Path.home() / ".cache/ai-agents-map/updates.json"
CLAUDE_SETTINGS = Path.home() / ".claude/settings.json"
CHECK_EVERY = 6 * 3600
CHANNELS = ("latest", "stable")
MAX_OUTPUT = 256 * 1024
_VERSION = re.compile(r"\d{1,6}(\.\d{1,6}){1,3}")
_TOOL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@-]{0,99}")
_SHOWN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}")      # a version as shown (also b6500, 2026.10.16-e3cb207)


@dataclass
class Update:
    kind: str                  # "agent" or "runtime"
    key: str                   # agent command or runtime key
    name: str
    current: str
    available: str
    command: list = field(default_factory=list)   # run in a terminal (mise, claude update)
    page: bool = False         # True: AI Agents Map's own Update page


def parse(version: str) -> tuple | None:
    version = str(version or "").strip().lstrip("v").split("-")[0]
    return tuple(int(p) for p in version.split(".")) if _VERSION.fullmatch(version) else None


def newer(available: str, installed: str) -> bool:
    a, b = parse(available), parse(installed)
    return a is not None and b is not None and a > b


def channel(settings: Path = CLAUDE_SETTINGS) -> str:
    value = ai_core.load_settings(settings).get("autoUpdatesChannel")
    return value if value in CHANNELS else "latest"


# --- the sources --------------------------------------------------------------------------------

def claude_channel_latest(chan: str, fetch=None) -> str | None:
    try:
        tags = (fetch or netfetch.fetch_json)(TAGS_URL, max_bytes=16 * 1024, timeout=10, redirects=0)
    except netfetch.FetchError:
        return None
    version = tags.get(chan) if isinstance(tags, dict) else None
    return version if isinstance(version, str) and parse(version) else None


def mise_outdated(run=None) -> dict:
    """mise tool name -> (current, latest), from `mise outdated --json`."""
    if not shutil.which("mise"):
        return {}
    out, ok = (run or ai_core.read_bounded_output)(["mise", "outdated", "--json"], max_bytes=MAX_OUTPUT, timeout=60)
    if not ok:
        return {}
    try:
        data = json.loads(out.decode("utf-8", "replace") or "{}")
    except ValueError:
        return {}
    found = {}
    for name, item in (data.items() if isinstance(data, dict) else []):
        if not isinstance(item, dict) or not _TOOL.fullmatch(str(name)):
            continue
        current, latest = str(item.get("current") or ""), str(item.get("latest") or "")
        if _SHOWN.fullmatch(current) and _SHOWN.fullmatch(latest) and current != latest:
            found[name] = (current, latest)
    return found


def mise_command_of(tool: str) -> str:
    """The agent command a mise tool name stands for: "http:muse" -> "muse"."""
    return tool.rsplit(":", 1)[-1].rsplit("/", 1)[-1]


def installer_latest(inst, gpu) -> str | None:
    """The newest release of one of our installs, for the build that is installed."""
    current = inst.current_version()
    build = next((key for key, _label in inst.builds if current.endswith(key)), "")
    try:
        release = inst.latest(gpu, build)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, netfetch.FetchError):
        return None
    version = getattr(release, "version", "")
    return version if _SHOWN.fullmatch(version or "") else None


# --- all of them ------------------------------------------------------------------------------

def check(agent_list, runtimes, now: float | None = None, cache: Path = CACHE, sources=None) -> list[Update]:
    """What can be updated now; the web is asked at most every CHECK_EVERY seconds."""
    import install_ctl
    now = time.time() if now is None else now
    saved = ai_core.load_settings(cache)
    if isinstance(saved.get("checked"), (int, float)) and 0 <= now - saved["checked"] < CHECK_EVERY \
            and isinstance(saved.get("updates"), list):
        found = [Update(**u) for u in saved["updates"] if _usable(u)]
        return still_open(found, agent_list, runtimes)
    sources = sources or {}
    found = []
    installed = {a.command: a for a in agent_list if a.installed}
    # mise
    mise_agents = {a.command for a in installed.values() if a.source == "mise"}
    if mise_agents:
        for tool, (current, latest) in (sources.get("mise") or mise_outdated)().items():
            agent = installed.get(mise_command_of(tool))
            if agent is not None and agent.command in mise_agents:
                found.append(Update("agent", agent.command, agent.name, current, latest,
                                    command=["mise", "upgrade", tool]))
    # Claude Code: its own channel, as its "Update available!" message (mise's version list can lag
    # behind it for hours; the mise upgrade then reads GitHub directly, see run_in_terminal).
    claude = installed.get("claude")
    command = ["claude", "update"] if claude is not None and claude.source == "own" and shutil.which("claude") \
        else ["mise", "upgrade", "claude"] if claude is not None and claude.source == "mise" else None
    if command and not any(u.key == "claude" for u in found):
        latest = (sources.get("claude") or claude_channel_latest)(channel())
        if latest and newer(latest, claude.version):
            found.append(Update("agent", "claude", claude.name, claude.version, latest, command=command))
    # ours: local LLMs and agents AI Agents Map installed
    ours = [(k, a.name, "agent") for k, a in installed.items() if k in install_ctl.AGENT_INSTALLERS] + \
           [(rt.key, rt.name, "runtime") for rt in runtimes if rt.installed]
    gpu = None
    for key, name, kind in ours:
        inst = install_ctl.installer(key)
        if inst is None or not inst.can_install or inst.installed_folder() is None:
            continue
        if gpu is None:
            gpu = (sources.get("gpu") or install_ctl.detect_gpu)()
        latest = (sources.get("installer") or installer_latest)(inst, gpu)
        current = inst.current_version()
        if latest and current and latest != current and not newer(current, latest):
            found.append(Update(kind, key, name, current, latest, page=True))
    try:
        ai_core.atomic_write(cache, json.dumps({"checked": now, "updates": [asdict(u) for u in found]}))
    except OSError:
        pass
    return found


def _usable(u) -> bool:
    if not isinstance(u, dict) or set(u) != set(Update.__dataclass_fields__):
        return False
    command = u.get("command")
    return u["kind"] in ("agent", "runtime") and all(isinstance(u[k], str) for k in ("key", "name", "current", "available")) \
        and isinstance(command, list) and (command == [] or command[:2] in (["mise", "upgrade"], ["claude", "update"]))


def still_open(found: list[Update], agent_list, runtimes) -> list[Update]:
    """Cached updates not installed since (the installed version moved on)."""
    versions = {a.command: a.version for a in agent_list if a.installed}
    left = []
    for u in found:
        if u.kind == "agent" and u.command and versions.get(u.key) not in (None, u.current):
            continue
        if u.kind == "runtime" and not any(rt.key == u.key and rt.installed for rt in runtimes):
            continue
        if u.page:
            import install_ctl
            inst = install_ctl.installer(u.key)
            if inst is None or inst.current_version() != u.current:
                continue
        left.append(u)
    return left


def forget(cache: Path = CACHE):
    """After an update was started: ask again on the next open."""
    try:
        cache.unlink(missing_ok=True)
    except OSError:
        pass


def run_in_terminal(argv: list[str]) -> bool:
    """argv in a terminal window of its own that waits at the end (desktop.run_in_terminal)."""
    if not argv or argv[:2] not in (["mise", "upgrade"], ["claude", "update"]) \
            or not all(_TOOL.fullmatch(a) for a in argv):
        return False
    # mise's version service can lag behind a release; read the versions from the source (still checksummed).
    import desktop
    return desktop.run_in_terminal(argv, {"MISE_USE_VERSIONS_HOST": "0"} if argv[0] == "mise" else None)
