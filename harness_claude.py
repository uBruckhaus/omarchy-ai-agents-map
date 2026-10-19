"""Claude Code: identity, session numbers (claude_sessions reads ~/.claude/projects), security settings,
what it needs to sign in, skills, MCP servers (~/.claude.json) and its own updater."""

import claude_sessions
from harnesses import Harness, Mcp, Sessions, Skills, Way, ref
from i18n import _


def _note() -> str:
    return _("Numbers from Claude Code's session files (no text is read). The API value is what the "
             "tokens would cost at API prices, not what a plan costs.")


def _access():
    from agent_status import Access
    from signin import Login
    return Access(_("A Claude Pro or Max subscription (claude.ai), or an API key from console.anthropic.com"),
                  "claude auth login", (".claude/.credentials.json",), ("ANTHROPIC_API_KEY",),
                  logins=(Login(_("Claude subscription"), ("claude", "auth", "login", "--claudeai")),
                          Login(_("API billing (Anthropic Console)"), ("claude", "auth", "login", "--console"))))


# --- security settings (agent_security) ----------------------------------------------------------

def _security(report):
    """Its security settings (agent_security.report_for)."""
    from agent_security import Choice, HOME, Setting, _dig, _json_file, _strs
    path = HOME / ".claude/settings.json"
    data = _json_file(path)
    report.files.append(path)
    ask = _("Asks before it edits a file or runs a command.")
    report.settings += [
        Setting("permissions.defaultMode", _("Permission mode"), _(
            "How much Claude Code may do without asking. It can be changed for one session with Shift+Tab."),
            [Choice("default", _("Ask first"), ask, 1),
             Choice("plan", _("Plan only"), _("Reads and plans; changes nothing until you agree."), 0),
             Choice("dontAsk", _("Only what is allowed"), _(
                 "Never asks: does only what your allow rules permit and refuses the rest."), 0),
             Choice("acceptEdits", _("Edit without asking"), _(
                 "Edits files in the project on its own, asks before commands."), 2),
             Choice("auto", _("Automatic review"), _(
                 "A classifier lets safe actions through and asks about the rest."), 2),
             Choice("bypassPermissions", _("Bypass all checks"), _(
                 "Does everything without asking. Only for a throwaway machine."), 3)],
            _dig(data, "permissions.defaultMode"), "default", path, terms=("approval", "classifier", "bypass"),
            recommended="default"),
        Setting("sandbox.enabled", _("Sandbox for commands"), _(
            "Runs shell commands in a sandbox (bubblewrap on Linux): writes only in the project, network only to "
            "allowed sites."),
            [Choice(True, _("On"), _("Commands are fenced in."), 0),
             Choice(False, _("Off"), _("Commands run with all your rights."), 1)],
            _dig(data, "sandbox.enabled"), False, path, terms=("sandbox", "network"), recommended=True),
        Setting("permissions.disableBypassPermissionsMode", _("Block bypass mode"), _(
            "Makes it impossible to start Claude Code with --dangerously-skip-permissions, even by mistake."),
            [Choice("disable", _("Blocked"), _("Bypass mode cannot be switched on."), 0),
             Choice(None, _("Allowed"), _("Bypass mode can be switched on with a flag."), 1)],
            _dig(data, "permissions.disableBypassPermissionsMode"), None, path, terms=("bypass",),
            recommended="disable"),
    ]
    for key, title in (("allow", _("Allowed without asking")), ("ask", _("Always ask")),
                       ("deny", _("Never allowed"))):
        entries = _strs(_dig(data, f"permissions.{key}"))
        if entries:
            report.rules.append((title, "permissions." + key, entries))


HARNESS = Harness("claude", "Claude Code", sessions=Sessions(claude_sessions.collect_by_id, _note),
                  label="Claude", full_name="Claude Code", icon="\U000f06c4", security=_security,
                  access=_access, skills=Skills(("~/.claude/skills",), "~/.claude/skills"),
                  mcp=Mcp("~/.claude.json", format="claude", shape="claude"),
                  self_update=("claude", "update"), latest=ref("agent_updates", "claude_channel_latest"),
                  install=(Way("mise", spec="claude"),),
                  data=("~/.claude", "~/.claude.json"),
                  homepage="https://github.com/anthropics/claude-code")
