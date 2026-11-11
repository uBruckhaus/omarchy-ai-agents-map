"""What the graphics card is driven with for AI work: Vulkan (the graphics driver alone: llama.cpp's Vulkan
build, Ollama), ROCm (AMD's compute stack: llama.cpp's ROCm build, faster on AMD), CUDA (NVIDIA), and the
PyTorch builds on this PC with the backend each was made for (ROCm, CUDA or the CPU only: speech, image
and training tools run on it). Only files are looked at (libraries, version files, PyTorch's version.py),
nothing is imported or started, except one bounded `pacman -Q` for a package's version.

Where the system's package manager has them (pacman: Arch, CachyOS, Omarchy), the app offers to install
what is missing (ways(): the usual install dialog, sudo in a terminal the user watches); elsewhere it names
the vendor's install page."""

from __future__ import annotations

import glob
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

ROCM_PAGE = "https://rocm.docs.amd.com/projects/install-on-linux/en/latest/"
PYTORCH_PAGE = "https://pytorch.org/get-started/locally/"
# name, pacman package, page: what the app may offer to install (install_dialog kind "tool")
PACKAGES = {
    "rocm": ("ROCm", "rocm-hip-runtime", ROCM_PAGE),
    "pytorch-rocm": ("PyTorch for ROCm", "python-pytorch-rocm", PYTORCH_PAGE),
    "vulkan-radeon": ("Vulkan for AMD cards", "vulkan-radeon", "https://docs.mesa3d.org/drivers/radv.html"),
}
MAX_FILE = 64 * 1024


@dataclass
class Torch:
    where: str                    # its folder (~ shortened)
    version: str
    backend: str                  # "ROCm 6.4", "CUDA 12.8", "CPU"


@dataclass
class Stack:
    vendor: str = ""              # amd | nvidia | ""
    vulkan: bool = False
    rocm: str = ""                # its version ("7.2.4"), "yes" when found without one, "" none
    cuda: str = ""
    torches: list = field(default_factory=list)

    def summary(self) -> list[str]:
        out = []
        if self.rocm:
            out.append("ROCm" + ("" if self.rocm == "yes" else f" {self.rocm}"))
        if self.vulkan:
            out.append("Vulkan")
        if self.cuda:
            out.append("CUDA" + ("" if self.cuda == "yes" else f" {self.cuda}"))
        return out


def _small(path: Path) -> str:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_FILE:
            return ""
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _tilde(path: str) -> str:
    home = str(Path.home())
    return "~" + path[len(home):] if path.startswith(home) else path


def rocm(environ=None, *, read_only=False) -> str:
    """ROCm's version, "yes" when its runtime is there without a version file, else ""."""
    environ = os.environ if environ is None else environ
    roots = [environ.get("ROCM_PATH", ""), *sorted(glob.glob("/opt/rocm*"), reverse=True)]
    for root in [r for r in roots if r]:
        version = _small(Path(root) / ".info/version").strip()
        if glob.glob(f"{root}/lib/libamdhip64.so*"):
            return version.split("-")[0] if re.fullmatch(r"\d+\.\d+(\.\d+)?(-\d+)?", version) else "yes"
    if glob.glob("/usr/lib/libamdhip64.so*") or glob.glob("/usr/lib64/libamdhip64.so*"):
        return ("yes" if read_only else package_version("rocm-hip-runtime")) or "yes"
    return ""


def package_version(name: str) -> str:
    import shutil
    import ai_core
    if not shutil.which("pacman") or not re.fullmatch(r"[a-z0-9][a-z0-9+._-]{0,63}", name):
        return ""
    out, ok = ai_core.read_bounded_output(["pacman", "-Q", name], max_bytes=4096, timeout=5)
    found = re.fullmatch(rf"{re.escape(name)} (\d+(?:\.\d+)+)\S*\s*", out.decode("utf-8", "replace")) if ok else None
    return found.group(1) if found else ""


def vulkan() -> bool:
    return bool(glob.glob("/usr/share/vulkan/icd.d/*.json") or glob.glob("/etc/vulkan/icd.d/*.json"))


def cuda() -> str:
    import shutil
    if glob.glob("/usr/lib/libcuda.so*") or glob.glob("/usr/lib64/libcuda.so*") or shutil.which("nvidia-smi"):
        return "yes"
    return ""


def torch_places() -> list[str]:
    """Where a PyTorch may be: the system's and this user's site-packages, mise's Pythons, and the
    virtual environments the AI tools keep in this user's ~/.local/share (one level)."""
    home = str(Path.home())
    patterns = ["/usr/lib/python3*/site-packages/torch", f"{home}/.local/lib/python3*/site-packages/torch",
                f"{home}/.local/share/mise/installs/python/*/lib/python3*/site-packages/torch",
                f"{home}/.local/share/*/venv/lib/python3*/site-packages/torch",
                f"{home}/.local/share/*/.venv/lib/python3*/site-packages/torch"]
    found = []
    for pattern in patterns:
        found += sorted(glob.glob(pattern))[:20]
    return found[:40]


def torch_at(folder: str) -> Torch | None:
    """A PyTorch's version and backend from its version.py (hip = '6.4…', cuda = '12.8'), not imported."""
    text = _small(Path(folder) / "version.py")
    version = re.search(r"^__version__\s*=\s*['\"]([^'\"]{1,40})['\"]", text, re.MULTILINE)
    if version is None:
        return None
    hip = re.search(r"^hip\s*(?::[^=\n]{0,40})?=\s*['\"](\d+\.\d+)", text, re.MULTILINE)
    cu = re.search(r"^cuda\s*(?::[^=\n]{0,40})?=\s*['\"](\d+\.\d+)", text, re.MULTILINE)
    backend = f"ROCm {hip.group(1)}" if hip else f"CUDA {cu.group(1)}" if cu else "CPU"
    return Torch(_tilde(str(Path(folder).parent.parent.parent.parent if "site-packages" in folder else folder)),
                 version.group(1).split("+")[0], backend)


def look(vendor: str = "", rocm_in_use: bool = False, *, read_only=False) -> Stack:
    """The stack now (blocking, a few milliseconds and at most one pacman -Q). rocm_in_use: a local LLM
    runs on ROCm (llama.cpp's devices say so), wherever that ROCm lives (a ROCM_PATH of its own)."""
    stack = Stack(vendor, vulkan(), (rocm(read_only=True) if read_only else rocm()) or ("yes" if rocm_in_use else ""), cuda())
    for folder in torch_places():
        found = torch_at(folder)
        if found is not None and found not in stack.torches:
            stack.torches.append(found)
    return stack


def wanted(stack: Stack) -> list[str]:
    """What an AMD card would want installed (keys of PACKAGES), in order: Vulkan to run at all when
    there is no ROCm, ROCm for speed, PyTorch for ROCm when PyTorch is there only for the CPU."""
    if stack.vendor != "amd":
        return []
    out = []
    if not stack.vulkan and not stack.rocm:
        out.append("vulkan-radeon")
    if not stack.rocm:
        out.append("rocm")
    if stack.torches and not any(t.backend.startswith("ROCm") for t in stack.torches):
        out.append("pytorch-rocm")
    return out


def ways(key: str) -> tuple:
    """The install ways for one of PACKAGES: the pacman package, then its page."""
    import harnesses
    name, package, page = PACKAGES[key]
    return (harnesses.Way("package", packages=(("pacman", package),)),
            harnesses.Way("manual", url=page, note=f"Install {name} as its vendor describes for your distribution."))
