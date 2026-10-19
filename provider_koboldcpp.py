"""KoboldCpp: a KoboldCpp server; stays up, its model is unloaded instead."""

from harnesses import Way
from providers import Provider


def _detect(processes):
    import inventory
    return inventory.koboldcpp(processes)


def _installer():
    import install_ctl
    return install_ctl.KoboldCpp()


PROVIDER = Provider("koboldcpp", "KoboldCpp", "local", detect=_detect, installer=_installer,
                    connect_key="koboldcpp-local", connect_label="KoboldCpp (local)",
                    # Kept running between agents (slow to start, no video memory without a model):
                    # the watcher unloads its model instead of stopping it.
                    stays_up=True, unload=("/api/admin/reload_config", {"filename": "unload_model"}),
                    install=(Way("installer"),),
                    homepage="https://github.com/LostRuins/koboldcpp")
