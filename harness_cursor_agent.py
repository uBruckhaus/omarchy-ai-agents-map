"""Cursor CLI."""

from harnesses import Harness, Mcp, Skills, Way
from i18n import _


def _access():
    from agent_status import Access
    from signin import Login
    return Access(_("A Cursor account (cursor.com)"), "cursor-agent login",
                  (".config/cursor/auth.json",), ("CURSOR_API_KEY",),
                  logins=(Login(_("Cursor account"), ("cursor-agent", "login")),))


# --- security settings (agent_security) ----------------------------------------------------------

def _security(report):
    """Its security settings (agent_security.report_for)."""
    from agent_security import Choice, HOME, Setting, _dig, _json_file, _strs
    path = HOME / ".config/cursor/cli-config.json"
    data = _json_file(path)
    report.files.append(path)
    report.settings += [
        Setting("approvalMode", _("Approval"), _("When Cursor CLI asks before running a command."),
            [Choice("allowlist", _("Ask unless allowed"), _(
                "Asks unless the command is in your allow list."), 1),
             Choice("auto-review", _("Automatic review"), _(
                 "A classifier on Cursor's servers runs safe calls and asks about the rest."), 2),
             Choice("unrestricted", _("Run everything"), _("Runs every command without asking."), 3)],
            data.get("approvalMode"), "allowlist", path, terms=("approval", "classifier", "bypass"),
            recommended="allowlist"),
        Setting("sandbox.mode", _("Sandbox"), _("Runs commands fenced in to the workspace."),
            [Choice("enabled", _("On"), _("Commands are fenced in."), 0),
             Choice("disabled", _("Off"), _("Commands run with all your rights."), 1)],
            _dig(data, "sandbox.mode"), "disabled", path, terms=("sandbox",), recommended="enabled"),
        Setting("sandbox.networkAccess", _("Network in the sandbox"), _(
            "Which sites commands in the sandbox may reach."),
            [Choice("user_config_only", _("Only your list"), _("Only the sites you listed."), 0),
             Choice("user_config_with_defaults", _("Your list + common sites"), _(
                 "Your sites plus package registries and similar."), 1),
             Choice("allow_all", _("Everything"), _("Any site."), 2)],
            _dig(data, "sandbox.networkAccess"), "user_config_with_defaults", path, terms=("network",),
            recommended="user_config_with_defaults"),
        Setting("autoAcceptWebSearch", _("Web search without asking"), _(
            "Whether the agent may search the web without asking each time."),
            [Choice(False, _("Ask"), _("Asks before searching."), 0),
             Choice(True, _("Allowed"), _("Searches on its own; web pages can carry hidden instructions."), 1)],
            data.get("autoAcceptWebSearch"), False, path, terms=("injection",), recommended=False),
    ]
    for key, title in (("allow", _("Allowed without asking")), ("deny", _("Never allowed"))):
        entries = _strs(_dig(data, f"permissions.{key}"))
        if entries:
            report.rules.append((title, "permissions." + key, entries))


HARNESS = Harness("cursor-agent", "Cursor CLI", label="Cursor CLI", icon="\ue90d", icon_font="omarchy",
                  security=_security, access=_access,
                  login_status=(("cursor-agent", "status"), "not logged in", "logged in"),
                  skills=Skills(("~/.cursor/skills", "~/.agents/skills", "~/.claude/skills", "~/.codex/skills"),
                                "~/.cursor/skills"),
                  mcp=Mcp("~/.cursor/mcp.json"),
                  install=(Way("mise", spec="cursor-agent"),),
                  data=("~/.cursor", "~/.config/cursor"),
                  homepage="https://cursor.com/cli")
