"""Oh My Pi."""

from harnesses import Harness, Mcp, Skills, Way


# --- skills (skills_ctl) --------------------------------------------------------------------------

def _skills_listed():
    import skills_ctl
    import skills_ctl
    return skills_ctl._json(skills_ctl.HOME / ".omp/agent/settings.json").get("skills")


HARNESS = Harness("omp", "Oh My Pi", label="omp", full_name="Oh My Pi", icon="\ue903", icon_font="omarchy",
                  spec="github:can1357/oh-my-pi",
                  skills=Skills(("~/.omp/agent/skills",), "~/.omp/agent/skills", listed=_skills_listed),
                  mcp=Mcp("~/.omp/agent/mcp.json", shape="omp"),
                  install=(Way("mise", spec="github:can1357/oh-my-pi"),),
                  data=("~/.omp",),
                  homepage="https://github.com/can1357/oh-my-pi")
