"""Install, update and uninstall local LLM runtimes in a folder of the user's
choice, without a password (the "user folder" install).

Layout, the same for every runtime this module installs:

    <program folder>/v0.40.4/…        one folder per version
    <program folder>/current -> v0.40.4
    <program folder>/.ai-agents-map   marker: installed here, may be updated/removed
    ~/.local/bin/<command> -> <program folder>/current/bin/<command>
    ~/.config/<runtime>/server.env    address, model folder, GPU choice
    ~/.config/systemd/user/<runtime>.service

Models never live in the program folder unless the user picks that, and
uninstalling never removes the model folder.

Everything here is blocking; the popup runs it on a thread and passes a
progress(fraction, text) callback and a cancelled() check.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

HOME = Path.home()
BIN_DIR = HOME / ".local/bin"
UNIT_DIR = HOME / ".config/systemd/user"
MARKER = ".ai-agents-map"
API_TIMEOUT = 20
NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


class Cancelled(Exception):
    pass


@dataclass
class Release:
    version: str
    assets: list = field(default_factory=list)   # [(name, url, size)]

    @property
    def size(self) -> int:
        return sum(size for _name, _url, size in self.assets)


@dataclass
class Gpu:
    vendor: str = "cpu"      # "amd", "nvidia", "cpu"
    name: str = ""
    index: int | None = None # HIP/CUDA device index of the largest card, when there are several
    count: int = 0
    vram: int = 0            # bytes, of the chosen card


# --- what is on this machine --------------------------------------------------------------

def detect_gpu() -> Gpu:
    """The graphics card a runtime should use: the one with the most video memory."""
    cards = []
    for device in sorted(Path("/sys/class/drm").glob("card[0-9]*/device")):
        try:
            vendor = (device / "vendor").read_text().strip()
            vram = int((device / "mem_info_vram_total").read_text().strip()) if (device / "mem_info_vram_total").exists() else 0
            slot = device.resolve().name
        except (OSError, ValueError):
            continue
        if vendor in ("0x1002", "0x10de") and not any(c[2] == slot for c in cards):
            cards.append((vendor, vram, slot))
    if not cards:
        return Gpu()
    cards.sort(key=lambda c: c[2])            # ROCm and CUDA number cards in PCI order
    vendor, vram, _slot = max(cards, key=lambda c: c[1])
    same = [c for c in cards if c[0] == vendor]
    gpu = Gpu("amd" if vendor == "0x1002" else "nvidia", count=len(same), vram=vram)
    gpu.index = same.index((vendor, vram, _slot)) if len(same) > 1 else None
    try:
        out = subprocess.run(["lspci"], capture_output=True, text=True, timeout=5).stdout
        names = [l.split(": ", 1)[-1] for l in out.splitlines() if re.search(r"VGA|Display|3D", l)]
        gpu.name = next((n for n in names if ("AMD" in n) == (gpu.vendor == "amd")), "")[:120]
    except (OSError, subprocess.SubprocessError):
        pass
    return gpu


def free_bytes(folder: Path) -> int:
    """Free space where folder is (or would be created)."""
    probe = folder
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        return shutil.disk_usage(probe).free
    except OSError:
        return 0


def check_folder(text: str) -> Path:
    """A folder the user typed: absolute (or ~), no control characters, ours to write."""
    text = (text or "").strip()
    if not text or len(text) > 1024 or any(ord(c) < 32 for c in text):
        raise ValueError("not a folder path")
    path = Path(os.path.expanduser(text))
    if not path.is_absolute():
        raise ValueError("the folder must be an absolute path")
    if path in (Path("/"), HOME) or str(path).startswith(("/usr", "/etc", "/bin", "/sbin", "/boot", "/proc", "/sys", "/dev")):
        raise ValueError("choose a folder of your own")
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    if not os.access(probe, os.W_OK):
        raise ValueError(f"{probe} is not writable")
    return path


# --- downloads ------------------------------------------------------------------------------------

def _curl_json(url: str) -> dict:
    out = subprocess.run(["curl", "-fsSL", "--max-time", str(API_TIMEOUT), "--proto", "=https", url],
                         capture_output=True, timeout=API_TIMEOUT + 5)
    if out.returncode != 0:
        raise RuntimeError(f"could not reach {url.split('/')[2]}")
    data = json.loads(out.stdout[:2_000_000])
    if not isinstance(data, dict):
        raise RuntimeError("unexpected answer")
    return data


def download(url: str, target: Path, size: int, progress, cancelled, done_before: int = 0, total: int = 0):
    """curl into target, reporting how far along (of total bytes) it is."""
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".part")
    proc = subprocess.Popen(["curl", "-fsSL", "--proto", "=https", "--proto-redir", "=https", "-C", "-",
                             "-o", str(partial), url], stdin=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        while proc.poll() is None:
            if cancelled():
                proc.kill()
                raise Cancelled()
            have = partial.stat().st_size if partial.exists() else 0
            progress((done_before + have) / max(1, total or size), None)
            time.sleep(0.4)
    finally:
        if proc.poll() is None:
            proc.kill()
    if proc.returncode != 0:
        raise RuntimeError(f"download failed: {proc.stderr.read(400).decode('utf-8', 'replace').strip()}")
    if size and partial.stat().st_size != size:
        raise RuntimeError("download incomplete")
    os.replace(partial, target)


def extract(archive: Path, folder: Path):
    folder.mkdir(parents=True, exist_ok=True)
    flag = "--zstd" if archive.name.endswith(".zst") else "-z" if archive.name.endswith((".tgz", ".gz")) else ""
    argv = ["tar", "-x", "-f", str(archive), "-C", str(folder), "--no-same-owner"] + ([flag] if flag else [])
    result = subprocess.run(argv, capture_output=True, text=True, timeout=1800)
    if result.returncode != 0:
        raise RuntimeError(f"unpacking failed: {result.stderr.strip()[:300]}")


# --- files and services ------------------------------------------------------------------------------

def _write(path: Path, text: str, mode: int = 0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), mode)
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _systemctl(*args) -> tuple[bool, str]:
    try:
        result = subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as error:
        return False, str(error)
    return result.returncode == 0, (result.stderr or result.stdout).strip()[:300]


def read_env(path: Path) -> dict:
    values = {}
    try:
        text = path.read_text(encoding="utf-8")[:65536]
    except OSError:
        return values
    for key, value in re.findall(r"^\s*([A-Z_][A-Z0-9_]*)=(.*)$", text, re.MULTILINE):
        values[key] = value.strip().strip("'\"")
    return values


def set_env_value(path: Path, key: str, value: str):
    """Change (or add) one KEY=value line, keeping everything else in the file."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    line = f"{key}={value}"
    if re.search(rf"^\s*{key}=", text, re.MULTILINE):
        text = re.sub(rf"^\s*{key}=.*$", line, text, count=1, flags=re.MULTILINE)
    else:
        text = text.rstrip("\n") + ("\n" if text.strip() else "") + line + "\n"
    _write(path, text, 0o600)


# --- installers ------------------------------------------------------------------------------------

def _curl_json_any(url: str):
    out = subprocess.run(["curl", "-fsSL", "--max-time", str(API_TIMEOUT), "--proto", "=https", url],
                         capture_output=True, timeout=API_TIMEOUT + 5)
    if out.returncode != 0:
        raise RuntimeError(f"could not reach {url.split('/')[2]}")
    return json.loads(out.stdout[:5_000_000])


class Installer:
    """What all user-folder installs share: version folders, `current`, the
    command in ~/.local/bin, a settings file, a user service, uninstall
    without the models. Subclasses say where releases come from and how they
    start."""
    key = name = command = service = ""
    bin_rel = ""                 # the command inside a version folder
    env_file: Path = HOME
    address = ""
    has_service = True
    builds: tuple = ()           # (key, label) choices, first = default for the card (see default_build)

    @property
    def default_folder(self) -> Path:
        return HOME / ".local/share/ai-agents-map" / self.key

    def default_build(self, gpu: Gpu) -> str:
        return self.builds[0][0] if self.builds else ""

    # where releases come from
    def latest(self, gpu: Gpu, build: str = "") -> Release:
        raise NotImplementedError

    # how a downloaded asset becomes a version folder
    def unpack(self, archive: Path, folder: Path):
        extract(archive, folder)

    def env_text(self, models: Path, gpu: Gpu) -> str:
        return ""

    def unit_text(self, models: Path) -> str:
        return ""

    def configure(self, models: Path):
        """Anything besides the settings file and the service (LM Studio: its own settings)."""

    def models_folder(self) -> str:
        return ""

    def set_models_folder_value(self, models: Path):
        raise NotImplementedError

    # ---------------------------------------------------------------------------------------------

    def installed_folder(self) -> Path | None:
        """Our program folder, from the ~/.local/bin link (when we made it)."""
        link = BIN_DIR / self.command
        if not link.is_symlink():
            return None
        target = Path(os.readlink(link))
        for parent in target.parents:
            if parent.name == "current":
                folder = parent.parent
                return folder if (folder / MARKER).exists() else None
        return None

    def current_version(self) -> str:
        folder = self.installed_folder()
        try:
            return os.readlink(folder / "current") if folder else ""
        except OSError:
            return ""

    def versions(self, folder: Path) -> list[str]:
        found = [p for p in folder.iterdir() if p.is_dir() and not p.is_symlink() and not p.name.startswith(".")
                 and p.name not in ("downloads", "models", "current")]
        return [p.name for p in sorted(found, key=lambda p: p.stat().st_mtime)]

    def install(self, folder: Path, models: Path, start_at_login: bool, start_now: bool, gpu: Gpu,
                release: Release, progress, cancelled):
        folder.mkdir(parents=True, exist_ok=True)
        (folder / MARKER).write_text(json.dumps({"runtime": self.key}) + "\n")
        models.mkdir(parents=True, exist_ok=True)
        downloads = folder / "downloads"
        done = 0
        for asset_name, url, size in release.assets:
            progress(done / max(1, release.size), f"download {asset_name}")
            download(url, downloads / asset_name, size, progress, cancelled, done, release.size)
            done += size
        if cancelled():
            raise Cancelled()
        progress(1.0, "unpack")
        partial = folder / f".{release.version}.partial"
        shutil.rmtree(partial, ignore_errors=True)
        partial.mkdir()
        for asset_name, _url, _size in release.assets:
            self.unpack(downloads / asset_name, partial)
        if not (partial / self.bin_rel).exists():
            flatten(partial)         # llama-b11658/llama-server → llama-server
        if not (partial / self.bin_rel).exists():
            raise RuntimeError(f"the download has no {self.bin_rel}")
        version_dir = folder / release.version
        shutil.rmtree(version_dir, ignore_errors=True)
        os.replace(partial, version_dir)
        shutil.rmtree(downloads, ignore_errors=True)
        self.switch(folder, release.version)
        if self.env_file != HOME:
            _write(self.env_file, self.env_text(models, gpu), 0o600)
        self.configure(models)
        if self.has_service:
            _write(UNIT_DIR / f"{self.service}.service", self.unit_text(models))
            _systemctl("daemon-reload")
            _systemctl("enable" if start_at_login else "disable", f"{self.service}.service")
            if start_now:
                ok, message = _systemctl("restart", f"{self.service}.service")
                if not ok:
                    raise RuntimeError(f"installed, but it did not start: {message}")

    def switch(self, folder: Path, version: str):
        current = folder / "current"
        tmp = folder / ".current.new"
        tmp.unlink(missing_ok=True)
        tmp.symlink_to(version)
        os.replace(tmp, current)
        BIN_DIR.mkdir(parents=True, exist_ok=True)
        link = BIN_DIR / self.command
        if (link.exists() or link.is_symlink()) and self.installed_folder() not in (None, folder):
            raise RuntimeError(f"{link} belongs to another install; not replaced")
        if link.exists() and not link.is_symlink():
            raise RuntimeError(f"{link} is a file of its own; not replaced")
        new = BIN_DIR / f".{self.command}.new"
        new.unlink(missing_ok=True)
        new.symlink_to(current / self.bin_rel)
        os.replace(new, link)

    def update(self, release: Release, gpu: Gpu, progress, cancelled):
        """New version folder, switch `current`, restart if it ran; the previous version stays as a fallback."""
        folder = self.installed_folder()
        if folder is None:
            raise RuntimeError("not installed here")
        running = self.has_service and _systemctl("is-active", "--quiet", f"{self.service}.service")[0]
        login = self.has_service and _systemctl("is-enabled", "--quiet", f"{self.service}.service")[0]
        models = Path(self.models_folder() or folder / "models")
        self.install(folder, models, login, running, gpu, release, progress, cancelled)
        for old in self.versions(folder)[:-2]:
            shutil.rmtree(folder / old, ignore_errors=True)

    def set_models_folder(self, models: Path):
        models.mkdir(parents=True, exist_ok=True)
        self.set_models_folder_value(models)
        if self.has_service and _systemctl("is-active", "--quiet", f"{self.service}.service")[0]:
            _systemctl("restart", f"{self.service}.service")

    def uninstall(self):
        """Program, link, service and its settings; never the models."""
        folder = self.installed_folder()
        if folder is None:
            raise RuntimeError("not installed by AI Agents Map; remove it the way it was installed")
        if self.has_service:
            _systemctl("stop", f"{self.service}.service")
            _systemctl("disable", f"{self.service}.service")
            (UNIT_DIR / f"{self.service}.service").unlink(missing_ok=True)
            _systemctl("daemon-reload")
        (BIN_DIR / self.command).unlink(missing_ok=True)
        models = Path(self.models_folder()) if self.models_folder() else None
        if self.env_file != HOME:
            self.env_file.unlink(missing_ok=True)
        self.unconfigure()
        for child in folder.iterdir():
            if models is not None and (child == models or models.is_relative_to(child)):
                continue            # the model folder was chosen inside the program folder: keep it
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
        if not any(folder.iterdir()):
            folder.rmdir()

    def unconfigure(self):
        """Undo configure() (LM Studio: its desktop entry)."""


def flatten(folder: Path):
    """An archive with one top folder (llama-b11658/…): its content moves up."""
    entries = [p for p in folder.iterdir()]
    if len(entries) == 1 and entries[0].is_dir() and not entries[0].is_symlink():
        inner = entries[0]
        temp = folder / f".{inner.name}.flatten"
        os.replace(inner, temp)
        for child in temp.iterdir():
            os.replace(child, folder / child.name)
        temp.rmdir()


class Ollama(Installer):
    key, name, command, service = "ollama", "Ollama", "ollama", "ollama"
    bin_rel = "bin/ollama"
    env_file = HOME / ".config/ollama/server.env"
    address = "127.0.0.1:11434"

    def latest(self, gpu: Gpu, build: str = "") -> Release:
        data = _curl_json("https://api.github.com/repos/ollama/ollama/releases/latest")
        version = str(data.get("tag_name", ""))
        if not re.fullmatch(r"v\d+\.\d+\.\d+", version):
            raise RuntimeError("no release found")
        wanted = ["ollama-linux-amd64.tar.zst"] + (["ollama-linux-amd64-rocm.tar.zst"] if gpu.vendor == "amd" else [])
        assets = {a.get("name"): a for a in data.get("assets", []) if isinstance(a, dict)}
        if any(name not in assets for name in wanted):
            raise RuntimeError(f"{version} has no Linux build")
        return Release(version, [(n, assets[n]["browser_download_url"], int(assets[n].get("size", 0))) for n in wanted])

    def models_folder(self) -> str:
        return read_env(self.env_file).get("OLLAMA_MODELS", "")

    def set_models_folder_value(self, models: Path):
        set_env_value(self.env_file, "OLLAMA_MODELS", str(models))

    def env_text(self, models: Path, gpu: Gpu) -> str:
        lines = ["# Written by AI Agents Map. Change the model folder there (right-click Ollama).",
                 f"OLLAMA_HOST={self.address}", f"OLLAMA_MODELS={models}"]
        if gpu.vendor == "amd" and gpu.index is not None:
            lines += ["# Only the card with the most video memory (not the integrated one).",
                      f"HIP_VISIBLE_DEVICES={gpu.index}", f"ROCR_VISIBLE_DEVICES={gpu.index}"]
        if gpu.vendor == "nvidia" and gpu.index is not None:
            lines.append(f"CUDA_VISIBLE_DEVICES={gpu.index}")
        return "\n".join(lines) + "\n"

    def unit_text(self, models: Path = None) -> str:
        return f"""[Unit]
Description=Ollama (installed by AI Agents Map)
After=network.target

[Service]
Type=exec
EnvironmentFile={self.env_file}
ExecStart={BIN_DIR / self.command} serve
Restart=on-failure
RestartSec=3
# Stopping also ends the model runners, so no video memory stays taken.
KillMode=control-group

[Install]
WantedBy=default.target
"""


class LlamaCpp(Installer):
    """The official llama.cpp builds as a model router: llama-server serves
    every GGUF in the model folder and loads one on demand; per-model settings
    come from ~/.config/llama.cpp/models.ini (the context size, see context_ctl)."""
    key, name, command, service = "llamacpp", "llama.cpp", "llama-server", "llama-server"
    bin_rel = "llama-server"
    env_file = HOME / ".config/llama.cpp/server.env"
    address = "127.0.0.1:8080"
    builds = (("vulkan", "Vulkan"), ("rocm", "ROCm"), ("cuda", "CUDA"), ("cpu", "CPU"))

    def default_build(self, gpu: Gpu) -> str:
        if gpu.vendor == "nvidia":
            return "cuda"
        if gpu.vendor == "amd":
            # The ROCm build needs ROCm installed; Vulkan runs on the graphics driver alone.
            return "rocm" if Path("/opt/rocm/lib/libamdhip64.so").exists() else "vulkan"
        return "cpu"

    def latest(self, gpu: Gpu, build: str = "") -> Release:
        build = build or self.default_build(gpu)
        data = _curl_json_any("https://api.github.com/repos/ggml-org/llama.cpp/releases?per_page=5")
        pattern = {"vulkan": r"llama-(b\d+)-bin-ubuntu-vulkan-x64\.tar\.gz", "rocm": r"llama-(b\d+)-bin-ubuntu-rocm-[\d.]+-x64\.tar\.gz",
                   "cuda": r"llama-(b\d+)-bin-ubuntu-cuda-12[\d.]*-x64\.tar\.gz", "cpu": r"llama-(b\d+)-bin-ubuntu-x64\.tar\.gz"}[build]
        for release in data if isinstance(data, list) else []:
            assets = [a for a in release.get("assets", []) if isinstance(a, dict)]
            main = next((a for a in assets if re.fullmatch(pattern, str(a.get("name")))), None)
            if main is None:
                continue
            version = re.fullmatch(pattern, main["name"]).group(1)
            picked = [main]
            if build == "cuda":
                runtime = next((a for a in assets if re.fullmatch(rf"cudart-llama-{version}-bin-ubuntu-cuda-12[\d.]*-x64\.tar\.gz",
                                                                    str(a.get("name")))), None)
                if runtime:
                    picked.append(runtime)
            return Release(f"{version}-{build}", [(a["name"], a["browser_download_url"], int(a.get("size", 0))) for a in picked])
        raise RuntimeError(f"no {build} build for Linux found")

    def models_folder(self) -> str:
        return read_env(self.env_file).get("LLAMA_ARG_MODELS_DIR", "")

    def set_models_folder_value(self, models: Path):
        set_env_value(self.env_file, "LLAMA_ARG_MODELS_DIR", str(models))

    def env_text(self, models: Path, gpu: Gpu) -> str:
        host, _sep, port = self.address.rpartition(":")
        lines = ["# Written by AI Agents Map. Change the model folder there (right-click llama.cpp).",
                 f"LLAMA_ARG_MODELS_DIR={models}", f"LLAMA_ARG_HOST={host}", f"LLAMA_ARG_PORT={port}"]
        if gpu.vendor == "amd" and gpu.index is not None:
            lines += [f"HIP_VISIBLE_DEVICES={gpu.index}", f"GGML_VK_VISIBLE_DEVICES={gpu.index}"]
        if gpu.vendor == "nvidia" and gpu.index is not None:
            lines.append(f"CUDA_VISIBLE_DEVICES={gpu.index}")
        return "\n".join(lines) + "\n"

    def unit_text(self, models: Path = None) -> str:
        preset = HOME / ".config/llama.cpp/models.ini"
        return f"""[Unit]
Description=llama.cpp model router (installed by AI Agents Map)
After=network.target

[Service]
Type=exec
EnvironmentFile={self.env_file}
# Every GGUF in the model folder, one loaded at a time; per-model settings in models.ini.
ExecStart={BIN_DIR / self.command} --models-preset {preset} --models-max 1 --gpu-layers all --flash-attn on
Restart=on-failure
RestartSec=3
KillMode=control-group

[Install]
WantedBy=default.target
"""

    def configure(self, models: Path):
        preset = HOME / ".config/llama.cpp/models.ini"
        if not preset.exists():
            _write(preset, "version = 1\n\n; Settings for every model; a [model name] section overrides them.\n"
                           "[*]\nctx-size = 8192\n", 0o644)


class LmStudio(Installer):
    """LM Studio's AppImage, unpacked (so FUSE is not needed), with a desktop
    entry and `lm-studio` in ~/.local/bin. Its server is `lms server`."""
    key, name, command, service = "lmstudio", "LM Studio", "lm-studio", ""
    bin_rel = "squashfs-root/AppRun"
    has_service = False
    settings = HOME / ".lmstudio/settings.json"
    desktop = HOME / ".local/share/applications/lm-studio-ai-agents-map.desktop"

    def latest(self, gpu: Gpu, build: str = "") -> Release:
        out = subprocess.run(["curl", "-fsSI", "--max-time", str(API_TIMEOUT), "--proto", "=https",
                              "https://lmstudio.ai/download/latest/linux/x64"], capture_output=True, text=True,
                             timeout=API_TIMEOUT + 5)
        location = re.search(r"^location:\s*(https://installers\.lmstudio\.ai/linux/x64/([\w.-]+)/[\w.-]+\.AppImage)\s*$",
                             out.stdout, re.MULTILINE | re.IGNORECASE)
        if out.returncode != 0 or not location:
            raise RuntimeError("could not find the latest LM Studio")
        url, version = location.group(1), location.group(2)
        head = subprocess.run(["curl", "-fsSIL", "--max-time", str(API_TIMEOUT), "--proto", "=https", url],
                              capture_output=True, text=True, timeout=API_TIMEOUT + 5)
        sizes = re.findall(r"^content-length:\s*(\d+)", head.stdout, re.MULTILINE | re.IGNORECASE)
        return Release(f"v{version}", [(Path(url).name, url, int(sizes[-1]) if sizes else 0)])

    def unpack(self, archive: Path, folder: Path):
        archive.chmod(0o755)
        result = subprocess.run([str(archive), "--appimage-extract"], cwd=folder, capture_output=True, timeout=900)
        if result.returncode != 0:
            raise RuntimeError("unpacking the AppImage failed")

    def models_folder(self) -> str:
        try:
            value = json.loads(self.settings.read_text(encoding="utf-8")[:2_000_000]).get("downloadsFolder", "")
        except (OSError, ValueError, AttributeError):
            value = ""
        return value if isinstance(value, str) else ""

    def set_models_folder_value(self, models: Path):
        try:
            data = json.loads(self.settings.read_text(encoding="utf-8")[:2_000_000])
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        data["downloadsFolder"] = str(models)
        _write(self.settings, json.dumps(data, indent=2) + "\n", 0o644)

    def configure(self, models: Path):
        self.set_models_folder_value(models)
        icon = next(iter(sorted((self.installed_folder() or HOME).glob("current/squashfs-root/*.png"))), None)
        _write(self.desktop, f"""[Desktop Entry]
Type=Application
Name=LM Studio
Comment=Run local LLMs (installed by AI Agents Map)
Exec={BIN_DIR / self.command} %U
Icon={icon or "lm-studio"}
Categories=Development;
Terminal=false
""")

    def unconfigure(self):
        self.desktop.unlink(missing_ok=True)


class Vllm(Installer):
    """vLLM in its own Python environment (uv), for NVIDIA cards. On AMD, vLLM
    needs ROCm builds that ship as containers; that is not offered here."""
    key, name, command, service = "vllm", "vLLM", "vllm", "vllm"
    bin_rel = "venv/bin/vllm"
    env_file = HOME / ".config/vllm/server.env"
    address = "127.0.0.1:8000"

    def latest(self, gpu: Gpu, build: str = "") -> Release:
        if gpu.vendor != "nvidia":
            raise RuntimeError("vLLM can be installed here for NVIDIA cards only; on AMD use llama.cpp or Ollama")
        data = _curl_json("https://pypi.org/pypi/vllm/json")
        version = str((data.get("info") or {}).get("version", ""))
        if not re.fullmatch(r"\d+\.\d+\.\d+", version):
            raise RuntimeError("no vLLM release found")
        return Release(f"v{version}", [])          # pip downloads it (several GB)

    def install(self, folder: Path, models: Path, start_at_login: bool, start_now: bool, gpu: Gpu,
                release: Release, progress, cancelled):
        uv = shutil.which("uv")
        if not uv:
            raise RuntimeError("needs uv (pacman -S uv)")
        folder.mkdir(parents=True, exist_ok=True)
        (folder / MARKER).write_text(json.dumps({"runtime": self.key}) + "\n")
        partial = folder / f".{release.version}.partial"
        shutil.rmtree(partial, ignore_errors=True)
        progress(0.05, "python environment")
        for argv in ([uv, "venv", str(partial / "venv"), "--python", "3.12"],
                     [uv, "pip", "install", "--python", str(partial / "venv/bin/python"), f"vllm=={release.version[1:]}"]):
            if cancelled():
                raise Cancelled()
            progress(0.1 if "venv" in argv[1] else 0.3, " ".join(argv[1:3]))
            result = subprocess.run(argv, capture_output=True, text=True, timeout=3600)
            if result.returncode != 0:
                raise RuntimeError(result.stderr.strip()[-300:])
        version_dir = folder / release.version
        shutil.rmtree(version_dir, ignore_errors=True)
        os.replace(partial, version_dir)
        self.switch(folder, release.version)
        models.mkdir(parents=True, exist_ok=True)
        _write(self.env_file, f"HF_HOME={models}\nVLLM_MODEL=\n", 0o600)
        _write(UNIT_DIR / f"{self.service}.service", self.unit_text(models))
        _systemctl("daemon-reload")
        _systemctl("enable" if start_at_login else "disable", f"{self.service}.service")

    def models_folder(self) -> str:
        return read_env(self.env_file).get("HF_HOME", "")

    def set_models_folder_value(self, models: Path):
        set_env_value(self.env_file, "HF_HOME", str(models))

    def unit_text(self, models: Path = None) -> str:
        host, _sep, port = self.address.rpartition(":")
        return f"""[Unit]
Description=vLLM (installed by AI Agents Map)
After=network.target

[Service]
Type=exec
EnvironmentFile={self.env_file}
# Serves the model named in VLLM_MODEL (a Hugging Face id).
ExecStart=/bin/sh -c 'exec {BIN_DIR / self.command} serve "$VLLM_MODEL" --host {host} --port {port}'
Restart=on-failure
KillMode=control-group

[Install]
WantedBy=default.target
"""


INSTALLERS = {"ollama": Ollama(), "llamacpp": LlamaCpp(), "lmstudio": LmStudio(), "vllm": Vllm()}


def installer(key: str):
    return INSTALLERS.get(key)


def _models_root(path: Path) -> Path:
    """/mnt/ai/Models/llama.cpp-router -> /mnt/ai/Models (cut after the part named models)."""
    parts = path.parts
    for index, part in enumerate(parts):
        if part.lower() == "models":
            return Path(*parts[:index + 1])
    return path


def folder_suggestions(key: str, runtimes) -> list[Path]:
    """Where to put a program: beside the shared model folder (/mnt/ai/Models -> /mnt/ai/<key>),
    else the default. Hidden folders in home (~/.ollama) are not suggested."""
    found = []
    for rt in runtimes:
        if rt.models_dir:
            base = _models_root(Path(os.path.expanduser(rt.models_dir))).parent
            if base not in (HOME, Path("/")) and not str(base).startswith(str(HOME) + "/."):
                found.append(base / key)
    inst = installer(key)
    found.append(inst.default_folder if inst else HOME / ".local/share/ai-agents-map" / key)
    return list(dict.fromkeys(found))


def models_suggestions(runtimes, program_folder: Path | None = None) -> list[Path]:
    """Model folders already in use (the shared one first), then one in the program folder."""
    roots = [_models_root(Path(os.path.expanduser(rt.models_dir))) for rt in runtimes if rt.models_dir]
    roots.sort(key=lambda r: (-roots.count(r), str(r).startswith(str(HOME) + "/.")))
    found = list(roots)
    if program_folder is not None:
        found.append(program_folder / "models")
    return list(dict.fromkeys(found))
