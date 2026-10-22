"""Smart Check: what the installed online agent did and found for this PC (a page of the app,
shown while an agent that can do look-ups is installed: ai_helper.helpers).

- analysis: on a click, the chosen agent reads a summary of this PC's AI setup (snapshot(): names,
  versions, models, counts and the setup check's findings; never keys, settings values, env or
  headers) and answers with a short summary and findings (problems, tips, what is good). It may use
  the web (is a version current?) and nothing else; the answer is checked (validate()) and kept in
  ~/.cache/ai-agents-map/smart-check.json. "Fix it with …" gives a finding to the agent in a
  terminal tab, with our own prompt (fix_prompt()), never the answer's words as a command.
- categories(): every AI run in the shared log (plugin_ai: this app's and the other plugins'),
  grouped by plugin and task, with starts, results and tokens.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

import ai_core
import ai_helper
import plugin_ai
from i18n import N_

HOME = Path.home()
CACHE = HOME / ".cache/ai-agents-map/smart-check.json"
MAX_SNAPSHOT = 12000
LEVELS = ("problem", "tip", "good")

PROMPT = """You are checking the AI setup of a Linux PC (Omarchy / Arch Linux). Here is what is installed
and how it is connected, as the app AI Agents Map sees it:

{snapshot}

Write a short summary (2-4 sentences, plain words, no markdown) of how this AI setup looks, then list
findings: real problems first (something broken, outdated or wasteful), then tips (worth doing), then
what is good. Base them on the facts above; you may look on the web whether versions are current.
Do not repeat a finding. Answer only with JSON:
{{"summary": "...", "findings": [{{"level": "problem|tip|good", "title": "<short>", "detail": "<one or two sentences: why and what to do>"}}]}}"""

SCHEMA = {"type": "object", "additionalProperties": False, "required": ["summary", "findings"],
          "properties": {"summary": {"type": "string"}, "findings": {"type": "array", "items": {
              "type": "object", "additionalProperties": False, "required": ["level", "title", "detail"],
              "properties": {"level": {"type": "string", "enum": list(LEVELS)}, "title": {"type": "string"},
                             "detail": {"type": "string"}}}}}}


@dataclass
class Finding:
    level: str
    title: str
    detail: str = ""


@dataclass
class Analysis:
    when: str
    agent: str
    summary: str
    findings: list = field(default_factory=list)

    def count(self, level: str) -> int:
        return sum(1 for f in self.findings if f.level == level)


# --- what the agent is shown -----------------------------------------------------------------

def snapshot(agent_list, details: dict, inventory, report=None, pc=None, updates=None) -> str:
    """This PC's AI setup in a few lines: names, versions, models, counts, the setup check's
    findings, the updates the daily AI checks found. Nothing secret (no keys, env, headers, URLs' paths or settings values)."""
    lines = []
    if pc is not None:
        lines.append(f"PC: GPU {pc.gpu or pc.vendor} with {round(pc.vram / 2**30)} GB video memory, "
                     f"{round(pc.ram / 2**30)} GB RAM, {pc.cpus} CPU threads")
    lines.append("Agents (installed):")
    for agent in agent_list:
        if not agent.installed:
            continue
        info = details.get(agent.command)
        model = f", model {info.model}" if info is not None and info.model else ""
        via = ", ".join(p.name + (" (local)" if p.local else "") for p in (info.providers if info else [])[:6])
        mcp = inventory.agent_mcp.get(agent.command, [])
        lines.append(f"- {agent.name} {agent.version or ''}{model}"
                     + (f", providers: {via}" if via else "") + (f", {len(mcp)} MCP servers" if mcp else ""))
    lines.append("Local LLM servers:")
    for rt in inventory.runtimes:
        if rt.installed:
            lines.append(f"- {rt.name}: {'running' if rt.running else 'stopped'}, {len(rt.models)} models"
                         + (f", used by {', '.join(rt.agents[:6])}" if rt.agents else ""))
    import mcp_updates
    versions = {s.name: f"{s.app} {s.version}" for s in mcp_updates.installed_servers()}
    newer = {u.name: u.latest for u in (mcp_updates.cached() if updates is None else updates)}
    lines.append("MCP servers:")
    for server in inventory.mcp[:40]:
        lines.append(f"- {server.name}" + (f" ({versions[server.name]})" if server.name in versions else "")
                     + (f", {newer[server.name]} is out" if server.name in newer else "")
                     + f", used by {len(server.agents)} agents")
    lines.append(f"Skills: {sum(len(s.skills) for s in inventory.skill_sets)} in {len(inventory.skill_sets)} folders")
    if report is not None:
        found = [(f.level, f.title, f.text) for area in report.areas for f in area.findings if f.level in ("warn", "hint")]
        if found:
            lines.append("The app's own setup check found:")
            lines += [f"- {'problem' if level == 'warn' else 'tip'}: {title}" + (f" ({text})" if text else "")
                      for level, title, text in found[:30]]
    text = "\n".join(lines)
    return text[:MAX_SNAPSHOT]


# --- the answer -----------------------------------------------------------------------------------

def _one_line(text, most: int) -> str:
    text = " ".join("".join(c for c in str(text or "") if c.isprintable() or c.isspace()).split())
    return text if len(text) <= most else text[:most - 1].rstrip() + "…"


def validate(answer: dict, agent: str, now: dt.datetime | None = None) -> Analysis:
    summary = _one_line(answer.get("summary"), 700)
    if not summary:
        raise ValueError("no summary")
    findings, seen = [], set()
    for item in answer.get("findings", [])[:20] if isinstance(answer.get("findings"), list) else []:
        if not isinstance(item, dict) or item.get("level") not in LEVELS:
            continue
        title = _one_line(item.get("title"), 100)
        if not title or title.lower() in seen:
            continue
        seen.add(title.lower())
        findings.append(Finding(item["level"], title, _one_line(item.get("detail"), 400)))
    findings.sort(key=lambda f: LEVELS.index(f.level))
    return Analysis((now or dt.datetime.now()).isoformat(timespec="seconds"), agent, summary, findings[:15])


def result_text(analysis: Analysis) -> str:
    return f"{analysis.count('problem')} problems, {analysis.count('tip')} tips"


def analyze(agent: str, setup: str, look_up=None, cache: Path = CACHE) -> Analysis:
    """Ask `agent` about the setup (minutes); the checked answer is kept. Raises on failure (the run
    is in the log either way)."""
    def result(text):
        return result_text(validate(plugin_ai.first_json(text, "findings"), agent))
    text = (look_up or ai_helper.look_up)(agent, "setup-analysis", PROMPT.format(snapshot=setup), SCHEMA,
                                          result_of=result)
    analysis = validate(plugin_ai.first_json(text, "findings"), agent)
    ai_core.atomic_write(cache, json.dumps(asdict(analysis), indent=1))
    return analysis


def last(cache: Path = CACHE) -> Analysis | None:
    try:
        with open(cache, encoding="utf-8") as handle:
            data = json.load(handle)
        return Analysis(str(data["when"]), str(data["agent"]), str(data["summary"]),
                        [Finding(str(f["level"]), str(f["title"]), str(f.get("detail", "")))
                         for f in data.get("findings", []) if f.get("level") in LEVELS])
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def fix_prompt(finding: Finding) -> str:
    """Our words, with the finding quoted as what was found (it came from the web-capable agent)."""
    return (f"A check of the AI setup on this Linux PC (Omarchy) found this {finding.level}:\n"
            f"\"{finding.title}\" - {finding.detail}\n"
            f"Look at this PC to see whether it is so, explain what you find, and fix it if it is. "
            f"Ask me before each step that changes anything.")


# --- what was done --------------------------------------------------------------------------------

# Task ids of this app; other plugins' tasks show with their own id.
TASKS = {"mcp-updates": N_("MCP server updates"), "agent-setup": N_("Looking up new agents"),
         "setup-analysis": N_("Setup analysis"), "mcp-update": N_("MCP server update tasks"),
         "fix": N_("Fixes after a failed install"), "smart-fix": N_("Fixes from Smart Check")}


@dataclass
class Category:
    plugin: str
    task: str
    runs: list            # newest first
    totals: plugin_ai.Totals


def categories(all_runs: list[dict], days: int = 30, now: dt.datetime | None = None) -> list[Category]:
    """Runs of the last `days` grouped by (plugin, task); the most recently used group first."""
    now = now or dt.datetime.now()
    groups = {}
    for run in all_runs:
        try:
            when = dt.datetime.fromisoformat(str(run.get("when")))
        except ValueError:
            continue
        if now - when <= dt.timedelta(days=days):
            groups.setdefault((str(run.get("plugin", "")), str(run.get("task", ""))), []).append(run)
    found = [Category(plugin, task, list(reversed(runs)),
                      plugin_ai.totals(runs, days=days, now=now)) for (plugin, task), runs in groups.items()]
    found.sort(key=lambda c: str(c.runs[0].get("when")), reverse=True)
    return found
