"""xAI's Grok CLI."""

from harnesses import Harness, Mcp, Skills, Way
from i18n import _


def _access():
    from agent_status import Access
    return Access(_("An API key from console.x.ai"), "grok", (), ("XAI_API_KEY",))


# --- skills (skills_ctl) --------------------------------------------------------------------------

def _skills_listed():
    import tomllib

    import connect
    import skills_ctl
    return tomllib.loads(connect._read(skills_ctl.HOME / ".grok/config.toml")).get("skills", {}).get("paths")


HARNESS = Harness("grok", "Grok", label="Grok", icon="\ue904", icon_font="omarchy",
                  spec="npm:@xai-official/grok", access=_access,
                  skills=Skills(("~/.grok/skills",), "~/.grok/skills", listed=_skills_listed),
                  mcp=Mcp("~/.grok/config.toml", format="toml", key="mcp_servers"),
                  install=(Way("mise", spec="npm:@xai-official/grok"),),
                  data=("~/.grok",),
                  homepage="https://www.npmjs.com/package/@xai-official/grok")
