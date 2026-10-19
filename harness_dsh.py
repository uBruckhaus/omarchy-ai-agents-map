"""DeepSeek Harness (installed by this app: install_ctl.Dsh)."""

from harnesses import Harness, LocalLlm, Mcp, Skills, Way, ref
from i18n import _


def _installer():
    import install_ctl
    return install_ctl.Dsh()


def _access():
    from agent_status import Access
    return Access(_("An API key from platform.deepseek.com, or a local LLM from the map"),
                  "dsh web", (".dsh/.credentials.yaml",), ("DEEPSEEK_API_KEY",), local_ok=True)


# --- pointed at a local LLM (connect.plan) --------------------------------------------------------

def _llm_connected(texts, provider):
    import dsh_ctl
    return provider in dsh_ctl.providers(texts[0])


def _llm_active(texts, provider):
    import dsh_ctl
    return dsh_ctl.default_model(texts[0])[0] == provider


def _llm_transform(texts, provider, label, url, models, context, model, use_now, remove):
    import dsh_ctl
    return [dsh_ctl.connect_text(texts[0], provider, label, url, models, context, model, use_now, remove)]


HARNESS = Harness("dsh", "DeepSeek Harness", label="DeepSeek Harness", icon="\U000f0e7b",
                  installer=_installer, offered=False, access=_access,
                  local_llm=LocalLlm(("~/.dsh/profiles/web/cordis.patch.yml",), _llm_connected, _llm_active,
                                     _llm_transform),
                  skills=Skills(("~/.dsh/skills", "~/.agents/skills"), "~/.dsh/skills"),
                  mcp=Mcp("~/.dsh/profiles/web/cordis.patch.yml", format="yaml", key="",
                          read=ref("dsh_ctl", "mcp_servers"), write=ref("dsh_ctl", "mcp_text"),
                          native=ref("dsh_ctl", "mcp_config")),
                  install=(Way("installer"),),
                  data=("~/.dsh",),
                  homepage="https://www.npmjs.com/package/@deepseek-ai/dsh")
