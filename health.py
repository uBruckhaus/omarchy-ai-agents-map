"""Health check: how well the AI setup on this PC works, and what would make it better.

Reads only what the window has collected already (agents, their settings' details,
the inventory of local LLMs, MCP servers and skills, the map's graph) plus a few
facts about the PC (graphics card, memory, free disk space: sysfs, /proc/meminfo,
statvfs). Nothing is changed, started or sent anywhere.

The report has
- areas (This PC, Agents, Local LLMs and models, Tools) with findings: ok, a tip
  ("hint") or something to fix ("warn"), each with what to do and, where the window
  can do it, an action;
- use cases (everyday help on the PC, vibe coding, images, voice, research and
  writing): what is there for it and the next step.
Use cases are judged by names (agents, models, MCP servers, skills) against keyword
lists: a guide, not a test of what the programs can really do.
"""

import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import desktop
from i18n import _, ngettext

GiB = 1024 ** 3

# Agents with their own web search (no MCP server needed for it).
WEB_AGENTS = {"claude", "codex", "gemini", "grok", "copilot", "cursor-agent"}
NOT_CODING = {"bionic"}            # chat apps among the agents
CODER = re.compile(r"cod(e|er|ing)|devstral|starcoder|codestral|swe", re.I)
VISION = re.compile(r"(^|[-_.\s])(vl|vision|llava|pixtral|minicpm-v|moondream)([-_.\s]|$)|gemma-?[34]|mmproj", re.I)
IMAGE_GEN = re.compile(r"flux|sdxl|stable-?diffusion|sd[-_]?3|hidream|qwen-?image", re.I)
IMAGE_TOOL = re.compile(r"photo|image|img|paint|draw|comfy|diffusion|gimp|krita|figma|canva|pixel|omaraw", re.I)
VOICE = re.compile(r"tts|whisper|stt|speech|voice|kokoro|piper|parakeet|narrat", re.I)
WEB_TOOL = re.compile(r"fetch|search|browser|chrome|playwright|puppeteer|web|tavily|brave|exa|firecrawl", re.I)
PC_TOOL = re.compile(r"desktop|file|filesystem|shell|hypr|omarchy|system|computer|home|notes", re.I)
IMAGE_APPS = ("comfyui", "sd", "sd-cli", "stable-diffusion", "krita", "gimp", "photocraft-cli", "omaraw")
VOICE_APPS = ("whisper", "whisper-cli", "piper", "kokoro", "espeak-ng")


@dataclass
class Finding:
    level: str                  # "ok", "hint" (a tip), "warn" (something to fix)
    title: str
    text: str = ""
    action: tuple = ()          # ("details", agent) ("runtime", key) ("models", key, query)
                                # ("addmcp", query) ("addskills",) ("reorganize",) ("install", key)


@dataclass
class Area:
    key: str
    title: str
    icon: str
    findings: list = field(default_factory=list)


@dataclass
class UseCase:
    key: str
    title: str
    icon: str
    blurb: str
    have: list = field(default_factory=list)      # what is there for it (short names)
    steps: list = field(default_factory=list)     # Findings: the next steps
    level: str = "missing"                         # "ready", "almost", "missing"


@dataclass
class Report:
    areas: list
    uses: list
    warn: int = 0
    hint: int = 0

    @property
    def level(self) -> str:
        return "warn" if self.warn else "hint" if self.hint else "ok"

    def summary(self) -> str:
        if self.warn:
            return ngettext("1 thing to fix", "{0} things to fix", self.warn).format(self.warn) + (
                " · " + ngettext("1 tip", "{0} tips", self.hint).format(self.hint) if self.hint else "")
        if self.hint:
            return _("Works well") + " · " + ngettext("1 tip", "{0} tips", self.hint).format(self.hint)
        return _("Everything set up and working")


@dataclass
class PC:
    vendor: str = "cpu"          # "amd", "nvidia", "cpu"
    gpu: str = ""
    vram: int = 0
    ram: int = 0
    cpus: int = 0
    free: dict = field(default_factory=dict)     # model folder -> free bytes there


def pc_info(model_dirs=()) -> PC:
    import install_ctl
    gpu = install_ctl.detect_gpu()
    ram = 0
    try:
        with open("/proc/meminfo", encoding="utf-8") as handle:
            for line in handle.read(8192).splitlines():
                if line.startswith("MemTotal:"):
                    ram = int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    free = {}
    for folder in model_dirs:
        path = Path(os.path.expanduser(folder))
        probe = path
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        try:
            free[folder] = shutil.disk_usage(probe).free
        except OSError:
            pass
    return PC(gpu.vendor, gpu.name, gpu.vram, ram, os.cpu_count() or 0, free)


def gb(size: int) -> str:
    return f"{size / GiB:.0f} GB" if size >= 10 * GiB else f"{size / GiB:.1f} GB"


def fits_text(vram: int) -> str:
    """The size of model file that runs well on this card (with room for context)."""
    usable = max(0, vram - int(2.5 * GiB))
    return gb(usable)


def matches(pattern, names) -> list:
    """Names that match, one per model (llama.cpp's file and Ollama's tag of the same model count once)."""
    found = {}
    for name in names:
        if name and pattern.search(name):
            found.setdefault(re.sub(r"[^a-z0-9]", "", name.lower().split(":")[0]), name)
    return sorted(found.values(), key=str.lower)


def card_name(pc: PC) -> str:
    """"Advanced Micro Devices, Inc. [AMD/ATI] Navi 31 [Radeon RX 7900 XT/7900 XTX/…] (rev c8)" -> "AMD Radeon RX 7900 XT"."""
    brackets = re.findall(r"\[([^\]]+)\]", pc.gpu or "")
    if brackets:
        model = brackets[-1].split("/")[0].strip()
        maker = {"amd": "AMD", "nvidia": "NVIDIA"}.get(pc.vendor, "")
        return f"{maker} {model}".strip() if maker and not model.upper().startswith(maker) else model
    return (pc.gpu or pc.vendor.upper())[:60]


# --- the checks ----------------------------------------------------------------------------------

def check(agent_list, details: dict, inventory, built, pc: PC, agent_checks: dict, runtime_checks: dict,
          failed_links: dict, path_has=shutil.which) -> Report:
    """agent_checks / runtime_checks: command or key -> [agent_status.Check] (made by the window);
    built: the map's graph (graph.build); failed_links: link_hints.load()."""
    import graph as graph_model
    installed = [a for a in agent_list if a.installed]
    names = {a.command: a.name for a in agent_list}
    runtimes = [rt for rt in inventory.runtimes if rt.installed]
    models = sorted({m for rt in runtimes for m in rt.models})
    mcp_names = [m.name + " " + (m.target or "") for m in inventory.mcp]
    used_mcp = [m for m in inventory.mcp if m.agents]
    skill_names = [Path(s.path).name + " " + " ".join(s.skills) for s in inventory.skill_sets]
    skill_count = sum(len(s.skills) for s in inventory.skill_sets)
    local_agents = {a for rt in runtimes for a in rt.agents}

    # This PC ------------------------------------------------------------------------------------
    area = Area("pc", _("This PC"), "\U000f0379")
    if pc.vendor == "cpu":
        area.findings.append(Finding("hint", _("No graphics card for local models"),
                                     _("Local models run on the processor: fine for small ones (up to about 4 GB), "
                                       "slow for bigger ones. Online models (Claude, Codex, Gemini) are not affected.")))
    elif pc.vram < 8 * GiB:
        area.findings.append(Finding("hint", _("{0} with {1} video memory").format(card_name(pc), gb(pc.vram)),
                                     _("Good for small local models (model files up to about {0}).").format(fits_text(pc.vram))))
    else:
        area.findings.append(Finding("ok", _("{0} with {1} video memory").format(card_name(pc), gb(pc.vram)),
                                     _("Local models with files up to about {0} run fully on the card.").format(fits_text(pc.vram))))
    if pc.ram:
        if pc.ram < 16 * GiB:
            area.findings.append(Finding("hint", _("{0} memory").format(gb(pc.ram)),
                                         _("Agents, a browser and a local model together get tight; close what you do not need.")))
        else:
            area.findings.append(Finding("ok", _("{0} memory").format(gb(pc.ram))))
    for folder, free in sorted(pc.free.items()):
        where = folder.replace(str(Path.home()), "~", 1)
        if free < 20 * GiB:
            area.findings.append(Finding("warn", _("Little free space for models: {0}").format(gb(free)),
                                         _("In {0}. A model needs 2–30 GB; remove models no agent uses.").format(where),
                                         ("reorganize",)))
        elif free < 60 * GiB:
            area.findings.append(Finding("hint", _("{0} free for models").format(gb(free)),
                                         _("In {0}: room for a few more models.").format(where)))
        else:
            area.findings.append(Finding("ok", _("{0} free for models").format(gb(free)), where))
    areas = [area]

    # Agents -------------------------------------------------------------------------------------
    area = Area("agents", _("Agents"), "\U000f06a9")
    if not installed:
        area.findings.append(Finding("warn", _("No agent installed"),
                                     desktop.text("install_agent_first"), ("setup",)))
    ready = []
    for agent in installed:
        checks = agent_checks.get(agent.command, [])
        missing = [c for c in checks if c.state == "missing"]
        warn = [c for c in checks if c.state == "warn"]
        if missing:
            area.findings.append(Finding("warn", f"{agent.name}: {missing[0].title}", missing[0].step or missing[0].detail,
                                         ("details", agent.command)))
        elif warn:
            area.findings.append(Finding("hint", f"{agent.name}: {warn[0].title}", warn[0].step or warn[0].detail,
                                         ("details", agent.command)))
        else:
            ready.append(agent.name)
    if ready:
        area.findings.insert(0, Finding("ok", ngettext("1 agent ready", "{0} agents ready", len(ready)).format(len(ready)),
                                        ", ".join(ready)))
    if runtimes and installed and not local_agents:
        area.findings.append(Finding("hint", _("No agent uses your local LLMs yet"),
                                     _("Draw a line on the map from an agent to {0}: it then works offline and free "
                                       "of charge as well.").format(runtimes[0].name)))
    areas.append(area)

    # Local LLMs and models ----------------------------------------------------------------------
    area = Area("local", _("Local LLMs and models"), "\U000f0b79")
    if not runtimes:
        area.findings.append(Finding("hint", _("No local LLM installed"),
                                     _("Optional: a local LLM (llama.cpp or Ollama) runs models on this PC, private "
                                       "and without costs. Right-click llama.cpp on the map → Install."),
                                     ("install", "llamacpp")))
    for rt in runtimes:
        checks = runtime_checks.get(rt.key, [])
        bad = [c for c in checks if c.state in ("missing", "warn")]
        if bad:
            level = "warn" if bad[0].state == "missing" else "hint"
            area.findings.append(Finding(level, f"{rt.name}: {bad[0].title}", bad[0].step or bad[0].detail,
                                         ("runtime", rt.key)))
        elif not rt.models:
            area.findings.append(Finding("hint", _("{0} has no models yet").format(rt.name),
                                         _("Download one that fits your card (files up to about {0}).").format(
                                             fits_text(pc.vram)) if pc.vram else "", ("models", rt.key, "")))
        else:
            area.findings.append(Finding("ok", rt.name, ngettext("1 model", "{0} models", len(rt.models)).format(
                len(rt.models)) + (" · " + _("running") if rt.running else "")))
        if rt.running and not rt.active_agents and rt.agents:
            area.findings.append(Finding("hint", _("{0} runs, but no agent uses it now").format(rt.name),
                                         _("It may hold video memory. With “Start local LLMs with their agents” on, "
                                           "it starts and stops with them."), ("runtime", rt.key)))
    # Agents on different models of one local LLM: it must keep them all loaded, and they must fit.
    import model_slots
    try:
        for need in model_slots.plan(runtimes, model_slots.models_in_use(built)):
            for hint in model_slots.hints(need, pc.vram):
                area.findings.append(Finding("warn", _("{0}: its agents use {1} different models")
                                             .format(need.name, need.count), hint["text"], ("runtime", need.key)))
    except (OSError, ValueError):
        pass
    idle = graph_model.idle_folders(built) if built is not None else {}
    if idle:
        folders = ", ".join(f.replace(str(Path.home()), "~", 1) for f in sorted(idle))
        area.findings.append(Finding("hint", ngettext("1 model folder no agent uses", "{0} model folders no agent uses",
                                                      len(idle)).format(len(idle)),
                                     _("{0}: connect an agent to them, or tidy up to free disk space.").format(folders),
                                     ("reorganize",)))
    seen = {}
    for rt in runtimes:
        for model in rt.models:
            seen.setdefault(re.sub(r"[^a-z0-9]", "", model.lower().split(":")[0]), set()).add(rt.name)
    doubled = [k for k, v in seen.items() if len(v) > 1]
    if doubled:
        area.findings.append(Finding("hint", ngettext("1 model is in more than one local LLM's folder",
                                                      "{0} models are in more than one local LLM's folder",
                                                      len(doubled)).format(len(doubled)),
                                     _("Reorganize puts them into one shared folder (no second copy, Undo possible)."),
                                     ("reorganize",)))
    areas.append(area)

    # Tools: MCP servers and skills ----------------------------------------------------------------
    area = Area("tools", _("Tools: MCP servers and skills"), "\U000f0841")
    for (source, target), hint in sorted(failed_links.items()) if isinstance(failed_links, dict) else []:
        area.findings.append(Finding("warn", _("A connection does not work: {0} → {1}").format(
            names.get(str(source).split(":", 1)[-1], str(source).split(":", 1)[-1]), str(target).split(":", 1)[-1]),
            _("Click its dotted red line on the map for what to do."), ("link", str(source), str(target))))
    unused = [m.name for m in inventory.mcp if not m.agents]
    if used_mcp:
        area.findings.append(Finding("ok", ngettext("1 MCP server in use", "{0} MCP servers in use", len(used_mcp))
                                     .format(len(used_mcp)), ", ".join(sorted(m.name for m in used_mcp))[:200]))
    else:
        area.findings.append(Finding("hint", _("No MCP servers yet"),
                                     _("MCP servers give agents tools: files, a browser, your apps. Add MCP server finds "
                                       "them on this PC and on the web."), ("addmcp", "")))
    if unused:
        area.findings.append(Finding("hint", ngettext("1 MCP server no agent uses", "{0} MCP servers no agent uses",
                                                      len(unused)).format(len(unused)),
                                     _("{0}: draw a line to an agent to use it.").format(", ".join(sorted(unused))[:200])))
    if skill_count:
        area.findings.append(Finding("ok", ngettext("1 skill", "{0} skills", skill_count).format(skill_count),
                                     ", ".join(sorted({Path(s.path).name for s in inventory.skill_sets}))[:200]))
    else:
        area.findings.append(Finding("hint", _("No skills yet"),
                                     _("Skills teach agents a task step by step (a PDF, a release, your house style)."),
                                     ("addskills",)))
    areas.append(area)

    # What you can do with it -----------------------------------------------------------------------
    uses = []
    coding = [a.name for a in installed if a.command not in NOT_CODING]
    online = [a.name for a in installed if any(not p.local for p in getattr(details.get(a.command), "providers", []) or [])
              or a.command in WEB_AGENTS]

    use = UseCase("pc", _("Everyday help on the PC"), "\U000f0379",
                  _("Ask questions, sort files, fix settings, explain errors."))
    use.have = [a.name for a in installed][:4] + matches(PC_TOOL, [m.name for m in inventory.mcp])[:3]
    if not installed:
        use.steps.append(Finding("warn", _("Install an agent"), desktop.text("install_agent_short")))
    elif not runtimes:
        use.steps.append(Finding("hint", _("Quick answers offline"),
                                 _("A small local model (2–4 GB) answers simple questions without internet or costs."),
                                 ("install", "llamacpp")))
    uses.append(use)

    use = UseCase("coding", _("Vibe coding"), "\U000f0169",
                  _("Describe what you want; the agent writes, runs and fixes the code."))
    coder_models = matches(CODER, models)
    use.have = coding[:4] + coder_models[:2] + ([ngettext("1 skill", "{0} skills", skill_count).format(skill_count)]
                                                if skill_count else [])
    if not coding:
        use.steps.append(Finding("warn", _("Install a coding agent"),
                                 desktop.text("install_coding_agent")))
    if runtimes and not coder_models and pc.vram >= 6 * GiB:
        rt = runtimes[0]
        use.steps.append(Finding("hint", _("A coding model for {0}").format(rt.name),
                                 _("Search “coder” and take one marked as fitting ({0} or smaller): code without "
                                   "costs or limits.").format(fits_text(pc.vram)), ("models", rt.key, "coder")))
    if coding and not skill_count:
        use.steps.append(Finding("hint", _("Add skills"), _("Ready-made workflows (tests, reviews, releases)."),
                                 ("addskills",)))
    uses.append(use)

    use = UseCase("images", _("Images"), "\U000f02e9",
                  _("Edit photos, cut out subjects, make and describe pictures."))
    image_tools = matches(IMAGE_TOOL, [m.name for m in inventory.mcp]) + [a for a in IMAGE_APPS if path_has(a)]
    vision = matches(VISION, models)
    makers = matches(IMAGE_GEN, models)
    use.have = image_tools[:3] + vision[:2] + makers[:2]
    if not image_tools:
        use.steps.append(Finding("hint", _("Add an image tool"),
                                 _("An MCP server for images lets agents edit pictures: Add MCP server, search “image”."),
                                 ("addmcp", "image")))
    if runtimes and not vision:
        use.steps.append(Finding("hint", _("A model that can see"),
                                 _("A vision model (names with VL, or Gemma) lets local agents read screenshots and photos."),
                                 ("models", runtimes[0].key, "vl")))
    uses.append(use)

    use = UseCase("voice", _("Voice"), "\U000f036c", _("Read texts aloud, write down what you say, subtitles."))
    voice_models = matches(VOICE, models)
    voice_tools = matches(VOICE, [m.name for m in inventory.mcp] + skill_names) + [a for a in VOICE_APPS if path_has(a)]
    use.have = voice_models[:3] + voice_tools[:3]
    if not use.have:
        use.steps.append(Finding("hint", _("A speech model"),
                                 _("Whisper (speech to text) or a TTS model (text to speech) run well locally."),
                                 ("models", runtimes[0].key, "whisper") if runtimes else ()))
    uses.append(use)

    use = UseCase("research", _("Research and writing"), "\U000f0349",
                  _("Search the web, read pages and documents, write and summarise."))
    web_tools = matches(WEB_TOOL, [m.name for m in inventory.mcp])
    use.have = sorted(set(online))[:3] + web_tools[:3]
    if not online and not web_tools:
        use.steps.append(Finding("hint", _("Web access"),
                                 _("Agents with online models search the web themselves; local ones need a "
                                   "search or browser MCP server."), ("addmcp", "search")))
    elif not web_tools and local_agents:
        use.steps.append(Finding("hint", _("Web access for local models"),
                                 _("A search or browser MCP server lets agents on local models look things up too."),
                                 ("addmcp", "search")))
    uses.append(use)

    for use in uses:
        use.have = list(dict.fromkeys(use.have))
        warn = any(s.level == "warn" for s in use.steps)
        use.level = "missing" if warn or not use.have else "almost" if use.steps else "ready"

    report = Report(areas, uses)
    report.warn = sum(1 for a in areas for f in a.findings if f.level == "warn")
    report.hint = sum(1 for a in areas for f in a.findings if f.level == "hint")
    return report
