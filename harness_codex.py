"""OpenAI's Codex CLI."""

from harnesses import Harness, Mcp, Skills, Way
from i18n import _


def _access():
    from agent_status import Access
    from signin import KeyStep, Login
    return Access(_("A ChatGPT plan (Plus, Pro, Business), or an API key from platform.openai.com"),
                  "codex login", (".codex/auth.json",), ("OPENAI_API_KEY",), local_ok=True,
                  logins=(Login(_("ChatGPT plan"), ("codex", "login")),),
                  key=KeyStep("OPENAI_API_KEY", "https://platform.openai.com/api-keys",
                              stdin=("codex", "login", "--with-api-key")))


# --- security settings (agent_security) ----------------------------------------------------------

def _security(report):
    """Its security settings (agent_security.report_for)."""
    import tomllib

    from agent_security import Choice, HOME, Setting, _dig, _read, _strs
    path = HOME / ".codex/config.toml"
    try:
        data = tomllib.loads(_read(path) or "")
    except tomllib.TOMLDecodeError:
        data = {}
    report.files.append(path)
    report.settings += [
        Setting("approval_policy", _("When Codex asks"), _(
            "When Codex stops and asks before running a command."),
            [Choice("untrusted", _("Ask for everything but safe reads"), _(
                "Asks before every command that is not a known read-only one."), 0),
             Choice("on-request", _("When it needs more"), _(
                 "Works inside the sandbox on its own and asks when it needs to leave it."), 1),
             Choice("on-failure", _("After a failure (old)"), _(
                 "Asks only when a sandboxed command failed. Replaced by “When it needs more”."), 1),
             Choice("never", _("Never"), _("Never asks; what the sandbox allows simply happens."), 2)],
            data.get("approval_policy"), "on-request", path, "toml", ("approval",), recommended="on-request"),
        Setting("sandbox_mode", _("Sandbox"), _("What commands Codex runs may touch."),
            [Choice("read-only", _("Read only"), _("Commands may read, not write or go online."), 0),
             Choice("workspace-write", _("Project folder"), _(
                 "Commands may write in the project folder (and temp), nowhere else."), 1),
             Choice("danger-full-access", _("No sandbox"), _("Commands may do anything you can do."), 3)],
            data.get("sandbox_mode"), "workspace-write", path, "toml", ("sandbox",),
            recommended="workspace-write"),
        Setting("sandbox_workspace_write.network_access", _("Network in the sandbox"), _(
            "Whether commands in the project-folder sandbox may reach the internet."),
            [Choice(False, _("Off"), _("No internet for commands (web search of Codex itself still works)."), 0),
             Choice(True, _("On"), _("Commands may download and upload."), 2)],
            _dig(data, "sandbox_workspace_write.network_access"), False, path, "toml", ("network",),
            recommended=False),
    ]
    trusted = [p for p, v in (data.get("projects") or {}).items()
               if isinstance(v, dict) and v.get("trust_level") == "trusted"]
    if trusted:
        report.rules.append((_("Trusted folders"), "projects", _strs(trusted)))


HARNESS = Harness("codex", "Codex", label="Codex", icon="\ue905", icon_font="omarchy", security=_security,
                  access=_access,
                  skills=Skills(("~/.agents/skills", "~/.codex/skills"), "~/.codex/skills"),
                  mcp=Mcp("~/.codex/config.toml", format="toml", key="mcp_servers", shape="codex"),
                  short_model_flag=True,
                  install=(Way("mise", spec="codex"),),
                  data=("~/.codex",),
                  homepage="https://github.com/openai/codex")
