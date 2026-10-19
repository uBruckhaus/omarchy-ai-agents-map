"""Charm's Crush."""

from harnesses import Harness, Mcp, Skills, Way
from i18n import _


def _access():
    from agent_status import Access
    from signin import Login
    return Access(_("An API key for a provider, or a local LLM from the map"), "crush",
                  (".local/share/crush/crush.json",), ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"), local_ok=True,
                  logins=(Login(_("Inside Crush (it asks for a key)"), ("crush",)),))


# --- skills (skills_ctl) --------------------------------------------------------------------------

def _skills_listed():
    import skills_ctl
    import skills_ctl
    options = skills_ctl._json(skills_ctl.HOME / ".config/crush/crush.json").get("options")
    return options.get("skills_paths") if isinstance(options, dict) else None


HARNESS = Harness("crush", "Crush", label="Crush", icon="\U000f02d1", access=_access,
                  skills=Skills(("~/.config/crush/skills", "~/.config/agents/skills", "~/.agents/skills",
                                 "~/.claude/skills"),
                                "~/.config/crush/skills", listed=_skills_listed),
                  mcp=Mcp("~/.config/crush/crush.json", key="mcp", shape="crush"),
                  short_model_flag=True,
                  install=(Way("mise", spec="crush"),),
                  data=("~/.config/crush", "~/.local/share/crush"),
                  homepage="https://github.com/charmbracelet/crush")
