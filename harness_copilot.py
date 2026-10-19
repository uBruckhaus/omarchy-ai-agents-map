"""GitHub Copilot CLI."""

from harnesses import Harness, Mcp, Skills, Way
from i18n import _


def _access():
    from agent_status import Access
    from signin import Login
    return Access(_("A GitHub Copilot subscription (github.com/features/copilot)"),
                  "copilot  →  /login", (".copilot/config.json",), ("COPILOT_GITHUB_TOKEN", "GH_TOKEN"),
                  logins=(Login(_("GitHub account"), ("copilot",), _("Type /login in it.")),))


# --- security settings (agent_security) ----------------------------------------------------------

def _security(report):
    """Its security settings (agent_security.report_for)."""
    from agent_security import Choice, HOME, Setting, _json_file, _strs
    path = HOME / ".copilot/settings.json"
    data = _json_file(path)
    managed = _json_file(HOME / ".copilot/config.json")
    report.files.append(path)
    report.settings.append(Setting("defaultPermissionMode", _("Permission mode"), _(
        "How new interactive Copilot sessions start."),
        [Choice("manual", _("Ask first"), _("Asks before writes and commands; reading is allowed."), 1),
         Choice("assisted", _("Automatic review"), _(
             "An AI review approves what it judges safe and asks about the rest."), 2),
         Choice("allow-all", _("Allow everything"), _("Approves everything, like --allow-all."), 3)],
        data.get("defaultPermissionMode"), "manual", path, terms=("approval", "classifier", "bypass"),
        recommended="manual"))
    for key, title in (("trustedFolders", _("Trusted folders")), ("allowedUrls", _("Sites without asking"))):
        entries = _strs(data.get(key)) or _strs(managed.get(key))
        if entries:
            report.rules.append((title, key, entries))


HARNESS = Harness("copilot", "GitHub Copilot", label="Copilot", full_name="GitHub Copilot", icon="\uf4b8",
                  security=_security, access=_access,
                  skills=Skills(("~/.copilot/skills", "~/.agents/skills"), "~/.copilot/skills"),
                  mcp=Mcp("~/.copilot/mcp-config.json", shape="copilot"),
                  install=(Way("mise", spec="copilot"),),
                  data=("~/.copilot",),
                  homepage="https://github.com/github/copilot-cli")
