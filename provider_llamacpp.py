"""llama.cpp: llama-server with its models folder (router mode) or one model."""

import llm_engine
from harnesses import Way
from providers import Provider


def _detect(processes):
    import inventory
    return inventory.llamacpp(processes)


def _installer():
    import install_ctl
    return install_ctl.LlamaCpp()


class Engine(llm_engine.LlamaRouter):
    programs = ("llama-server",)
    start_command = "llama-server --models-dir <folder>"

    def owns_process(self, comm, argv):
        # Other apps bundle llama-server too; theirs claim it by their path (BY_PATH).
        return llm_engine.BY_NAME if comm.startswith("llama-") or llm_engine.basename(argv[0] if argv else "") \
            in self.programs else 0

    def model_context(self, rt, name):
        import context_ctl
        return context_ctl.llama_settings(name)[0]


PROVIDER = Provider("llamacpp", "llama.cpp", "local", detect=_detect, installer=_installer, engine=Engine(),
                    connect_key="llama-local", connect_label="llama.cpp (local)",
                    install=(Way("installer"),),
                    homepage="https://github.com/ggml-org/llama.cpp")
