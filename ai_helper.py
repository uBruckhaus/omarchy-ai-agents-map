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

import contextlib
import functools
import json
import os
import shutil
import threading

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

_watch = threading.local()          # this thread's AI help run (watched): its Cancel


@contextlib.contextmanager
def watched(cancel, entry=None):
    """Look-ups this thread makes inside the block end as soon as `cancel` (an Event) is set: the
    progress box's Cancel (ai_progress); each one's prompt and answer go into `entry` (ai_protocol)."""
    before = getattr(_watch, "cancel", None), getattr(_watch, "entry", None)
    _watch.cancel, _watch.entry = cancel, entry
    try:
        yield
    finally:
        _watch.cancel, _watch.entry = before


def cancelled() -> bool:
    cancel = getattr(_watch, "cancel", None)
    return cancel is not None and cancel.is_set()


def _bounded(argv, **kwargs):
    import ai_core
    # Our own look-up run: the provider watcher does not start the agent's local LLMs for it.
    kwargs.setdefault("env", {**os.environ, "AI_AGENTS_MAP_NO_PROVIDERS": "1"})
    kwargs.setdefault("cancel", getattr(_watch, "cancel", None))
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
    stdin = agent_profiles.research_input(command, prompt)        # the question on stdin, not in argv
    if stdin is not None:
        run = functools.partial(run, stdin=stdin)
    argv = agent_profiles.research_command(command, prompt, schema_file)
    entry = getattr(_watch, "entry", None)
    try:
        answer = plugin_ai.look_up_argv(argv, harness.research_output if harness else "text", plugin=plugin_id(),
                                        task=task, agent=command, run=run, cwd=str(work), result_of=result_of)
    except Exception as error:
        if entry is not None:
            entry.look_up(task, prompt, error=str(error), argv=argv)
        raise
    if entry is not None:
        entry.look_up(task, prompt, answer, argv=argv)
    return answer


def advise(candidate, task: str, prompt: str, schema: dict | None = None, result_of=None, run=_bounded) -> str:
    """One question to the local helper (local_helper.Candidate): its model answers offline, the
    prompt on stdin; the run goes into the shared log and the protocol like a look-up. `schema` is not
    given to it (no agent takes one offline): the prompts say which JSON to answer with."""
    import agent_profiles
    import local_helper
    argv, output = local_helper.advise_argv(candidate)
    work = agent_profiles.WORK_DIR                 # an empty folder of ours: no project files around it
    work.mkdir(parents=True, exist_ok=True, mode=0o700)
    entry = getattr(_watch, "entry", None)
    try:
        answer = plugin_ai.look_up_argv(argv, output, plugin=plugin_id(), task=task, agent=candidate.agent,
                                        run=functools.partial(run, stdin=prompt.encode("utf-8")), cwd=str(work),
                                        result_of=result_of)
    except Exception as error:
        if entry is not None:
            entry.look_up(task, prompt, error=str(error), argv=argv)
        raise
    if entry is not None:
        entry.look_up(task, prompt, answer, argv=argv)
    return answer


def advise_as_look_up(candidate):
    """advise() with look_up()'s arguments (agent, task, prompt, schema, result_of=…), for the modules
    that take a look_up function (smart_check.analyze, mcp_advice.ask)."""
    def look_up(_agent, task, prompt, schema=None, result_of=None, **_kwargs):
        return advise(candidate, task, prompt, schema, result_of=result_of)
    return look_up


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
    """The agent started with `prompt` as its first message (its task fact), closed in so it asks
    before each change (agent_guard.task; raises agent_guard.Refused); None when it has none."""
    import agent_guard
    import harnesses
    harness = harnesses.get(command)
    if harness is None or not harness.task:
        return None
    facts = {command: plugin_ai.AgentFacts(name_of(command), harness.research, harness.research_output,
                                           harness.task)}
    argv = plugin_ai.task_argv(command, prompt, facts=facts)
    return agent_guard.task(command, argv) if argv else argv


def task_agents(installed) -> list[str]:
    """Installed agents that can take a task in a tab, the chosen AI-check agent first."""
    import harnesses
    able: list[str] = [h.command for h in harnesses.all_harnesses() if h.task and h.closed and h.closed[1]
                       and h.command in installed
            and shutil.which(h.command)]
    first = chosen(installed)
    return ([first] if first in able else []) + [c for c in able if c != first]


def record_task(task: str, command: str, result: str = "started") -> None:
    plugin_ai.record_task(plugin_id(), task, command, result)
