"""Work an installed online agent does for the map: web look-ups in the background (a new agent's
setup, the daily AI checks, Smart Check's setup analysis) and, on a click, a task in a terminal tab
("Do it with Claude…").

The shared part is plugin_ai (vendored from ~/Projects/omarchy-plugin-ai, also used by other
plugins): running a look-up, its token use, and one log of every plugin's runs
(~/.local/state/omarchy-plugin-ai/runs.json), which Smart Check shows. Here: which agents can, from
the app's adapters (their research and task facts), and the agent chosen in Settings › Agent for AI
checks. Nothing an agent answers is ever run by the app.
"""

from __future__ import annotations

import json
import os
import shutil

import plugin_ai
from plugin_ai import Usage, parse_output  # noqa: F401  (the app's modules use them from here)


def plugin_id() -> str:
    """This app's name in the shared log."""
    import desktop
    return "ai-agents-map" if desktop.NAME == "omarchy" else "asterism"


# --- which agent ---------------------------------------------------------------------------------

def helpers(installed) -> list[str]:
    """Installed agents that can look things up on the web without a prompt, in the agent list's order."""
    import agent_profiles
    return agent_profiles.researchers(installed)


def chosen(installed) -> str | None:
    """The agent for AI checks: the one picked in Settings when it can, else the first that can."""
    import ai_core
    found = helpers(installed)
    picked = ai_core.setting("ai_checks_agent")
    return picked if picked in found else (found[0] if found else None)


def in_order(installed) -> list[str]:
    """Every helper, the chosen one first (the others when it fails)."""
    first = chosen(installed)
    return ([first] if first else []) + [c for c in helpers(installed) if c != first]


def name_of(command: str) -> str:
    import harnesses
    harness = harnesses.get(command)
    return (harness.full_name or harness.name) if harness is not None else command


# --- one look-up -----------------------------------------------------------------------------------

def _bounded(argv, **kwargs):
    import ai_core
    # Our own look-up run: the provider watcher does not start the agent's local LLMs for it.
    kwargs.setdefault("env", {**os.environ, "AI_AGENTS_MAP_NO_PROVIDERS": "1"})
    return ai_core.read_bounded_output(argv, **kwargs)


def look_up(command: str, task: str, prompt: str, schema: dict, run=_bounded, result_of=None) -> str:
    """One web look-up by `command` (an agent with a research fact); its answer text. The run, its
    tokens and its result (result_of(answer) -> short text) go into the shared log. Raises on failure."""
    import ai_core
    import agent_profiles
    import harnesses
    harness = harnesses.get(command)
    work = agent_profiles.WORK_DIR
    work.mkdir(parents=True, exist_ok=True, mode=0o700)
    schema_file = work / f"schema-{task}.json"
    ai_core.atomic_write(schema_file, json.dumps(schema))
    return plugin_ai.look_up_argv(agent_profiles.research_command(command, prompt, schema_file),
                                  harness.research_output if harness else "text", plugin=plugin_id(), task=task,
                                  agent=command, run=run, cwd=str(work), result_of=result_of)


def billing(command: str, environ=None) -> tuple[str, str]:
    """How this agent's runs are paid: ("key", "") when its API key variable is set (the agent then
    uses the key, at API prices), ("plan", <plan>) when it is signed in to a subscription (the runs
    count against its limits, no extra charge), else ("unknown", "")."""
    import agent_details
    import agent_status
    environ = os.environ if environ is None else environ
    access = agent_status.access_table().get(command)
    if access is not None and any(environ.get(name) for name in access.env):
        return "key", ""
    plan = agent_details.details_for(command).plan
    return ("plan", plan) if plan else ("unknown", "")


# --- what was done ---------------------------------------------------------------------------------

def runs() -> list[dict]:
    return plugin_ai.runs()


def totals(command: str, days: int = 30) -> plugin_ai.Totals:
    """This agent's background look-ups (not its task tabs: those are sessions of its own)."""
    return plugin_ai.totals(runs(), days=days, agent=command, kind="look-up")


# --- a task in a terminal tab ----------------------------------------------------------------------

def task_argv(command: str, prompt: str) -> list[str] | None:
    """The agent started with `prompt` as its first message (its task fact); None when it has none."""
    import harnesses
    harness = harnesses.get(command)
    if harness is None or not harness.task:
        return None
    facts = {command: plugin_ai.AgentFacts(name_of(command), harness.research, harness.research_output,
                                           harness.task)}
    return plugin_ai.task_argv(command, prompt, facts=facts)


def task_agents(installed) -> list[str]:
    """Installed agents that can take a task in a tab, the chosen AI-check agent first."""
    import harnesses
    able: list[str] = [h.command for h in harnesses.all_harnesses() if h.task and h.command in installed
            and shutil.which(h.command)]
    first = chosen(installed)
    return ([first] if first in able else []) + [c for c in able if c != first]


def record_task(task: str, command: str, result: str = "started") -> None:
    plugin_ai.record_task(plugin_id(), task, command, result)
