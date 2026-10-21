"""vLLM: a vLLM server for one model."""

import llm_engine
from harnesses import Way
from providers import Provider


def _detect(processes):
    import inventory
    return inventory.vllm(processes)


def _installer():
    import install_ctl
    return install_ctl.Vllm()


class Engine(llm_engine.Engine):
    """One model per server, no unloading: freeing GPU memory stops it (the default)."""
    start_command = "vllm serve <model>"
    models_page = False                 # its one model is VLLM_MODEL in its env file

    def owns_process(self, comm, argv):
        if comm.startswith("VLLM") or (len(argv) > 1 and any(llm_engine.basename(a) == "vllm" for a in argv[:2])
                                       and "serve" in argv[1:3]):
            return llm_engine.BY_NAME
        return 0

    def add_model_step(self, rt):
        import install_ctl
        from i18n import _
        return _("vLLM serves one Hugging Face model: write its id into {0} (VLLM_MODEL=…), then start vLLM.") \
            .format(install_ctl.Vllm.env_file)


PROVIDER = Provider("vllm", "vLLM", "local", detect=_detect, installer=_installer, engine=Engine(),
                    connect_key="vllm-local", connect_label="vLLM (local)",
                    install=(Way("installer"),),
                    data=("~/.cache/vllm",),
                    homepage="https://docs.vllm.ai")
