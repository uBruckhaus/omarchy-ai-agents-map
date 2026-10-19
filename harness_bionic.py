"""LM Studio Bionic (installed by this app: install_ctl.Bionic)."""

from harnesses import Harness, Mcp, Skills, Way
from i18n import _


def _installer():
    import install_ctl
    return install_ctl.Bionic()


def _access():
    from agent_status import Access
    return Access(_("Nothing: it runs models on this computer"), "", (), (), local_ok=True)


HARNESS = Harness("bionic", "LM Studio Bionic", label="LM Studio Bionic", icon="\U000f09d1",
                  installer=_installer, short_version=True, offered=False, access=_access,
                  fixed_provider="lmstudio", self_served=True,
                  skills=Skills(("~/.lmstudio/skills",), "~/.lmstudio/skills"),
                  mcp=Mcp(in_app="Settings › Connected apps"),
                  install=(Way("installer"),),
                  data=("~/.lmstudio/apps/bionic",),
                  homepage="https://lmstudio.ai")
