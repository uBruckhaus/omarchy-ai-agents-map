"""Get help from an agent: an installed, signed-in online agent (Claude Code, Codex) installs, sets up
or removes an agent, a provider or an MCP server, in a terminal tab of the window (Setup and the
"!" page start it).

Here: which agent can (it takes a task and its sign-in or API key is there: only whether a file
exists or a variable is set, nothing is read) and its first message, with what is on this PC now
(names only, no settings, no keys). The agent asks before each step that changes the system; the
app runs nothing it answers.
"""

from __future__ import annotations

from i18n import N_

KINDS = (("agent", N_("An AI agent")), ("provider", N_("A provider (local LLM or online)")),
         ("mcp", N_("An MCP server")))
ACTIONS = (("install", N_("Install")), ("setup", N_("Set up or repair")), ("remove", N_("Remove")))
KIND_WORDS = {"agent": "an AI coding agent", "provider": "a model provider (a local LLM program or an online "
              "provider an agent uses)", "mcp": "an MCP server"}
ACTION_WORDS = {"install": "install and set up", "setup": "set up or repair", "remove": "remove"}
MAX_TEXT = 400


def ready_agent(task_agents, signed_in) -> str | None:
    """The first agent that can take a task and is signed in (signed_in(command) -> bool)."""
    return next((command for command in task_agents if signed_in(command)), None)


def signed_in(command: str) -> bool:
    """Its sign-in or API key is there (a stat or an environment look-up, never a read); an agent
    without known access facts counts as set up."""
    import agent_status
    access = agent_status.access_table().get(command)
    return access is None or agent_status.credentials_present(access)


def _clip(text: str) -> str:
    return " ".join(str(text or "").split())[:MAX_TEXT]


def prompt(kind: str, action: str, name: str, details: str, app: str, agents, runtimes, mcp) -> str:
    """The agent's first message: the job, what is here now and the rules."""
    if kind not in KIND_WORDS or action not in ACTION_WORDS:
        raise ValueError("unknown kind or action")
    name, details = _clip(name), _clip(details)
    what = f"{KIND_WORDS[kind]}" + (f" called \"{name}\"" if name else "")
    lines = [f"Please help me {ACTION_WORDS[action]} {what} on this Linux PC."]
    if not name:
        lines.append("I have not picked one yet: first ask me what I need it for, suggest two or three that fit, "
                     "and let me choose.")
    if details:
        lines.append(f"My notes: {details}")
    here = []
    if agents:
        here.append("AI agents: " + ", ".join(agents))
    if runtimes:
        here.append("local LLMs: " + ", ".join(runtimes))
    if mcp:
        here.append("MCP servers: " + ", ".join(mcp))
    lines.append("What is set up now (from " + app + "): " + ("; ".join(here) if here else "nothing yet") + ".")
    if kind == "mcp" and action != "remove":
        lines.append("Add the server to the agents I name (ask me which), in each agent's own MCP settings.")
    if action == "remove":
        lines.append("Remove it from every agent's settings that names it too, and tell me what you leave behind.")
    lines.append("Rules: use the official install way of this Linux distribution or of the project; tell me the "
                 "plan first, then ask me before each step that changes the system or a settings file; back up a "
                 "settings file before you change it; never print, copy or store an API key or password: when "
                 "one is needed, tell me where to put it and let me type it myself. When you are done, say what "
                 f"changed; {app} shows it on its map by itself.")
    return "\n".join(lines)
