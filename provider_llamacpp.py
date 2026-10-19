"""llama.cpp: llama-server with its models folder (router mode) or one model."""

from harnesses import Way
from providers import Provider


def _detect(processes):
    import inventory
    return inventory.llamacpp(processes)


def _installer():
    import install_ctl
    return install_ctl.LlamaCpp()


PROVIDER = Provider("llamacpp", "llama.cpp", "local", detect=_detect, installer=_installer,
                    connect_key="llama-local", connect_label="llama.cpp (local)",
                    install=(Way("installer"),),
                    homepage="https://github.com/ggml-org/llama.cpp")
