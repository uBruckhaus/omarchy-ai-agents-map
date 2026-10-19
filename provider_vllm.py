"""vLLM: a vLLM server for one model."""

from harnesses import Way
from providers import Provider


def _detect(processes):
    import inventory
    return inventory.vllm(processes)


def _installer():
    import install_ctl
    return install_ctl.Vllm()


PROVIDER = Provider("vllm", "vLLM", "local", detect=_detect, installer=_installer,
                    connect_key="vllm-local", connect_label="vLLM (local)",
                    install=(Way("installer"),),
                    homepage="https://docs.vllm.ai")
