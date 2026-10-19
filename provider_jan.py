"""Jan: Jan's local API server and its models."""

from harnesses import Way
from providers import Provider


def _detect(processes):
    import inventory
    return inventory.jan(processes)


def _installer():
    import install_ctl
    return install_ctl.Jan()


PROVIDER = Provider("jan", "Jan", "local", detect=_detect, installer=_installer,
                    connect_key="jan-local", connect_label="Jan (local)",
                    install=(Way("installer"),),
                    homepage="https://jan.ai")
