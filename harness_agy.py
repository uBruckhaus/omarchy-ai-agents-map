"""Antigravity CLI (installed by this app: install_ctl.Agy)."""

from harnesses import Harness, Way


def _installer():
    import install_ctl
    return install_ctl.Agy()


HARNESS = Harness("agy", "Antigravity CLI", label="Antigravity CLI", icon="\U000f02ad", installer=_installer,
                  offered=False,
                  install=(Way("installer"),),
                  homepage="https://antigravity.google")
