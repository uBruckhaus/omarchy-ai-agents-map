"""Checks right after a connection was drawn and written: will it work?

Each check returns hints (link_hints): "error" (it will not work as it is),
"warning" (it may not work, or only after a step of the user's), "ok". All
blocking with short timeouts; the popup runs them on a thread.

- provider: the agent's settings have it; the local LLM is installed, lists
  the model, is running or can be started with the agent; when it runs, its
  port is this user's and its API offers the model; the model's context is
  big enough for an agent's prompt.
- mcp: the agent's settings have the server, and it answers (initialize +
  tools/list, as the details page's Tools: an stdio server is started for it).
- skills: the folder is there with skills in it, and the agent's own links
  to them are not broken.
"""

import os
import shutil
from pathlib import Path

import ai_core
import connect
import context_ctl
import gpu_usage
import install_ctl
import jan_ctl
import kobold_ctl
import mcp_ctl
import models_ctl
import runtime_ctl
import skills_ctl
from i18n import _

MIN_AGENT_CONTEXT = 16384      # an agent's system prompt and tools alone take 8–15k tokens


def hint(level: str, text: str) -> dict:
    return {"level": level, "text": text}


def model_context(rt, name: str) -> int:
    """The context the local LLM loads the model with (0: not known)."""
    folder = Path(os.path.expanduser(rt.models_dir)) if rt.models_dir else None
    try:
        if rt.key == "llamacpp":
            return context_ctl.llama_settings(name)[0]
        if rt.key == "jan":
            return jan_ctl.settings(name)[0]
        if rt.key == "koboldcpp":
            return kobold_ctl.settings(name)[0]
        if rt.key == "lmstudio":
            return context_ctl.lmstudio_settings(context_ctl.lmstudio_entry(name))[0]
        if rt.key == "ollama" and folder is not None:
            env = install_ctl.read_env(install_ctl.Ollama.env_file)
            return context_ctl.ollama_context(folder, name, int(env.get("OLLAMA_CONTEXT_LENGTH", "0") or 0))
    except (OSError, ValueError, KeyError):
        return 0
    return 0


def offered_models(rt) -> list | None:
    """Model ids the running local LLM's OpenAI API lists (lower case), None when it does not answer."""
    reply = gpu_usage.http_json(gpu_usage.port_of(rt), "/v1/models", timeout=3)
    rows = reply.get("data") if isinstance(reply, dict) else None
    if not isinstance(rows, list):
        return None
    return [str(r.get("id", "")).lower() for r in rows[:1000] if isinstance(r, dict)]


def check_provider(agent: str, agent_name: str, rt, model: str, with_agents: bool | None = None) -> list:
    hints = []
    if with_agents is None:
        with_agents = bool(ai_core.setting("providers_with_agents"))
    if not connect.is_connected(agent, rt.key):
        hints.append(hint("error", _("{0}'s settings do not have {1} after writing them: another program may "
                                     "have changed them at the same time. Draw the line again.").format(agent_name, rt.name)))
    if not rt.installed:
        hints.append(hint("error", _("{0} is not installed: install it (right-click it on the map).").format(rt.name)))
        return hints
    if model and model.lower() not in [m.lower() for m in rt.models]:
        hints.append(hint("error", _("{0} does not have the model {1} (any more).").format(rt.name, model)))
    if not rt.running:
        if not runtime_ctl.can_control(rt):
            hints.append(hint("error", _("{0} is not running and has no service to start it with {1}: start it "
                                         "yourself before working with {1}.").format(rt.name, agent_name)))
        elif not with_agents:
            hints.append(hint("warning", _("{0} is not running, and local LLMs are not started with their agents "
                                           "(Settings): start it before working with {1}.").format(rt.name, agent_name)))
    else:
        port = gpu_usage.port_of(rt)
        owners = models_ctl.loopback_listeners(port) if port else []
        if not owners:
            hints.append(hint("error", _("{0} runs, but nothing listens on {1}: check its address.")
                              .format(rt.name, rt.address)))
        elif any(uid != os.getuid() for uid in owners):
            hints.append(hint("error", _("Port {0} is taken by another account: {1} cannot be reached there safely.")
                              .format(port, rt.name)))
        else:
            offered = offered_models(rt)
            if offered is None:
                hints.append(hint("warning", _("{0} did not answer on {1}: it may still be starting. Test again "
                                               "in a moment.").format(rt.name, connect.base_url(rt.address))))
            elif model and offered and model.lower() not in offered:
                hints.append(hint("error", _("{0} runs but does not offer {1} on {2}.")
                                  .format(rt.name, model, connect.base_url(rt.address))))
    if model:
        ctx = model_context(rt, model)
        if 0 < ctx < MIN_AGENT_CONTEXT:
            hints.append(hint("warning", _("{0} loads {1} with {2} tokens of context; an agent's prompt needs at "
                                           "least {3}. Set a bigger one: right-click {0} → Models….")
                              .format(rt.name, model, ctx, MIN_AGENT_CONTEXT)))
    if not hints:
        hints.append(hint("ok", _("{0} can use {1} with {2}.").format(agent_name, rt.name, model or "?")))
    return hints


def check_mcp(agent: str, agent_name: str, name: str, ask=None) -> list:
    import mcp_tools
    ask = ask or mcp_tools.ask
    entry = mcp_ctl.normal(mcp_ctl.raw_entries(agent).get(name))
    if entry is None:
        return [hint("error", _("{0}'s settings do not have the MCP server {1} after writing them. Draw the line "
                                "again.").format(agent_name, name))]
    try:
        result = ask(entry)
    except (OSError, ValueError, RuntimeError, TimeoutError) as error:
        text = str(error).strip().splitlines()[-1][:200] if str(error).strip() else type(error).__name__
        hints = [hint("error", _("The MCP server {0} does not answer: {1}").format(name, text))]
        if entry["transport"] == "stdio" and not (os.path.isabs(entry["command"]) and os.path.exists(entry["command"])) \
                and not shutil.which(entry["command"]):
            hints.append(hint("error", _("Its program {0} is not found.").format(entry["command"])))
        return hints
    tools = len(result.get("tools", []))
    if not tools:
        return [hint("warning", _("The MCP server {0} answers but offers no tools.").format(name))]
    return [hint("ok", _("{0} answers with {1} tools.").format(name, tools))]


def check_skills(agent: str, agent_name: str, folder: str) -> list:
    path = Path(os.path.expanduser(folder))
    if not path.is_dir():
        return [hint("error", _("The folder {0} is not there (any more).").format(folder))]
    try:
        skills = [p for p in list(path.iterdir())[:500] if (p / "SKILL.md").is_file()]
    except OSError as error:
        return [hint("error", _("The folder {0} cannot be read: {1}").format(folder, error))]
    hints = []
    if not skills:
        hints.append(hint("warning", _("{0} holds no skills (folders with a SKILL.md).").format(folder)))
    broken = []
    for own in skills_ctl.skill_dirs(agent):
        try:
            for link in list(Path(own).iterdir())[:500]:
                if link.is_symlink() and not link.exists() and str(os.readlink(link)).startswith(str(path)):
                    broken.append(link.name)
        except OSError:
            continue
    if broken:
        hints.append(hint("error", _("{0}'s links into {1} are broken: {2}.").format(
            agent_name, folder, ", ".join(sorted(broken)[:5]))))
    if not hints:
        hints.append(hint("ok", _("{0} sees {1} skills in {2}.").format(agent_name, len(skills), folder)))
    return hints
