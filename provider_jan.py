"""Jan: Jan's local API server and its models."""

import llm_engine
from harnesses import Configure, Way
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

    own_model_folder = False            # its models live in Jan's data folder; Jan moves it itself
    context_settable = True
    context_effect = "restart"

    def models_page_note(self, rt):
        from i18n import _
        return _("Jan adds models in its own app (Hub); here you set their context.")

    def context_now(self, rt, folder, name):
        import jan_ctl
        return jan_ctl.settings(name)

    def context_changes(self, rt, name, ctx):
        # Jan's preset (the engine) and the model's model.yml (the app), one backup.
        import jan_ctl
        return jan_ctl.context_changes(name, ctx)

    def data_folders(self, rt):
        import inventory
        return [str(inventory.jan_data_folder())]

    def add_model_step(self, rt):
        from i18n import _
        return _("add one in the Jan app (Hub), or right-click Jan on the map → Models….")


PROVIDER = Provider("jan", "Jan", "local", detect=_detect, installer=_installer, engine=Engine(),
                    connect_key="jan-local", connect_label="Jan (local)",
                    install=(Way("installer"),),
                    configure=(Configure("Open Jan", ("jan-desktop",), window=True),),
                    homepage="https://jan.ai")
