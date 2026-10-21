"""Jan: Jan's local API server and its models."""

import llm_engine
from harnesses import Way
from providers import Provider


def _detect(processes):
    import inventory
    return inventory.jan(processes)


def _installer():
    import install_ctl
    return install_ctl.Jan()


class Engine(llm_engine.LlamaRouter):
    """Jan's engine (jan-llama-worker) answers like llama.cpp's router."""
    programs = ("jan-llama-worker", "jan-llama-worke")      # comm is cut to 15 characters

    def model_context(self, rt, name):
        import jan_ctl
        return jan_ctl.settings(name)[0]

    def add_model_step(self, rt):
        from i18n import _
        return _("add one in the Jan app (Hub), or right-click Jan on the map → Models….")


PROVIDER = Provider("jan", "Jan", "local", detect=_detect, installer=_installer, engine=Engine(),
                    connect_key="jan-local", connect_label="Jan (local)",
                    install=(Way("installer"),),
                    homepage="https://jan.ai")
