"""Pi."""

from harnesses import Harness, LocalLlm, Mcp, Skills, Way, ref
from i18n import _


def _access():
    from agent_status import Access
    from signin import Login
    return Access(_("Sign in inside Pi (/login: Claude, ChatGPT, Copilot …), an API key, or a local LLM from the map"),
                  "pi  →  /login", (".pi/agent/auth.json",), ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"), local_ok=True,
                  logins=(Login(_("Inside Pi"), ("pi",), _("Type /login in it.")),))


# --- pointed at a local LLM (connect.plan) --------------------------------------------------------

def llm_texts(models_text: str, settings_text: str, provider: str, url: str, models: list, context: dict,
             model: str, use_now: bool, remove: bool = False) -> tuple[str, str]:
    import connect
    data = connect._json(models_text)
    providers = data.setdefault("providers", {})
    if remove:
        providers.pop(provider, None)
    else:
        entries = []
        for name in models:
            item = {"id": name, "name": name}
            if context.get(name):
                item["contextWindow"] = context[name]
            entries.append(item)
        providers[provider] = {"baseUrl": url, "api": "openai-completions", "apiKey": "local", "models": entries}
    settings = connect._json(settings_text)
    if remove:
        if settings.get("defaultProvider") != provider:
            return connect._dump(data), settings_text
        # The default would point at a provider that is gone.
        settings.pop("defaultProvider")
        settings.pop("defaultModel", None)
        return connect._dump(data), connect._dump(settings)
    if use_now:
        settings["defaultProvider"] = provider
        settings["defaultModel"] = model
    return connect._dump(data), (connect._dump(settings) if use_now else settings_text)


def _llm_connected(texts, provider):
    import connect
    return provider in connect._json(texts[0]).get("providers", {})


def _llm_active(texts, provider):
    import connect
    return connect._json(texts[1]).get("defaultProvider") == provider


def _llm_transform(texts, provider, label, url, models, context, model, use_now, remove):
    return list(llm_texts(texts[0], texts[1], provider, url, models, context, model, use_now, remove))


# --- skills (skills_ctl) --------------------------------------------------------------------------

def _skills_listed():
    import skills_ctl
    import skills_ctl
    return skills_ctl._json(skills_ctl.HOME / ".pi/agent/settings.json").get("skills")


def _skills_text(text: str, folder: str, remove: bool) -> str:
    import connect
    import skills_ctl
    data = connect._json(text)
    dirs = data.get("skills") if isinstance(data.get("skills"), list) else []
    dirs = [d for d in dirs if not (isinstance(d, str) and str(skills_ctl.expand(d)) == folder)]
    if not remove:
        dirs.append(folder)
    if dirs:
        data["skills"] = dirs
    else:
        data.pop("skills", None)
    return connect._dump(data)


# --- security settings (agent_security) ----------------------------------------------------------

PI_FULL = ["read", "bash", "edit", "write", "grep", "find", "ls"]
PI_READ = ["read", "grep", "find", "ls"]


def _security(report):
    """Its security settings (agent_security.report_for)."""
    from agent_security import Choice, HOME, Setting, _json_file
    path = HOME / ".pi/agent/settings.json"
    data = _json_file(path)
    report.files.append(path)
    report.note = _("Pi never asks before it acts, by design: the tools it has are its permissions. Run it in a "
                    "container or VM, or give it read-only tools, when it works on something you do not trust.")
    tools = data.get("defaultTools")
    report.settings.append(Setting("defaultTools", _("Tools"), _("What Pi can do at all."),
        [Choice(PI_FULL, _("Read, write and run commands"), _(
            "Edits files and runs any shell command, without asking."), 3),
         Choice(PI_READ, _("Read only"), _("Reads and searches files; changes nothing, runs nothing."), 0),
         Choice(None, _("Pi's default"), _("read, bash, edit, write: changes files and runs commands."), 3)],
        tools if isinstance(tools, list) else None, None, path, terms=("tools", "approval"),
        recommended=PI_READ))


HARNESS = Harness("pi", "Pi", label="Pi", full_name="Pi", icon="\ue901", icon_font="omarchy",
                  security=_security, usage=ref("agent_status", "pi"), access=_access,
                  local_llm=LocalLlm(("~/.pi/agent/models.json", "~/.pi/agent/settings.json"),
                                     _llm_connected, _llm_active, _llm_transform),
                  skills=Skills(("~/.pi/agent/skills", "~/.agents/skills"), "~/.pi/agent/skills",
                                settings_list=True, listed=_skills_listed,
                                settings_file="~/.pi/agent/settings.json", write=_skills_text),
                  mcp=Mcp("~/.pi/agent/mcp.json"),
                  install=(Way("mise", spec="pi"),),
                  data=("~/.pi",),
                  homepage="https://github.com/badlogic/pi-mono")
