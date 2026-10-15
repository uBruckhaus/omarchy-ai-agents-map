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


# --- Ollama ---------------------------------------------------------------------------------------

class Ollama:
    key = "ollama"
    name = "Ollama"
    command = "ollama"
    service = "ollama"
    env_file = HOME / ".config/ollama/server.env"
    default_folder = HOME / ".local/share/ai-agents-map/ollama"
    address = "127.0.0.1:11434"

    def latest(self, gpu: Gpu) -> Release:
        data = _curl_json("https://api.github.com/repos/ollama/ollama/releases/latest")
        version = str(data.get("tag_name", ""))
        if not re.fullmatch(r"v\d+\.\d+\.\d+", version):
            raise RuntimeError("no release found")
        wanted = ["ollama-linux-amd64.tar.zst"] + (["ollama-linux-amd64-rocm.tar.zst"] if gpu.vendor == "amd" else [])
        assets = {a.get("name"): a for a in data.get("assets", []) if isinstance(a, dict)}
        if any(name not in assets for name in wanted):
            raise RuntimeError(f"{version} has no Linux build")
        return Release(version, [(n, assets[n]["browser_download_url"], int(assets[n].get("size", 0))) for n in wanted])

    def installed_folder(self) -> Path | None:
        """Our program folder, from the ~/.local/bin link (when we made it)."""
        link = BIN_DIR / self.command
        if not link.is_symlink():
            return None
        target = Path(os.readlink(link))
        folder = target.parent.parent.parent if target.parent.name == "bin" else None   # <folder>/current/bin/ollama
        return folder if folder and (folder / MARKER).exists() else None

    def models_folder(self) -> str:
        return read_env(self.env_file).get("OLLAMA_MODELS", "")

    def env_text(self, models: Path, gpu: Gpu) -> str:
        lines = ["# Written by AI Agents Map. Change the model folder there (right-click Ollama).",
                 f"OLLAMA_HOST={self.address}", f"OLLAMA_MODELS={models}"]
        if gpu.vendor == "amd" and gpu.index is not None:
            lines += ["# Only the card with the most video memory (not the integrated one).",
                      f"HIP_VISIBLE_DEVICES={gpu.index}", f"ROCR_VISIBLE_DEVICES={gpu.index}"]
        if gpu.vendor == "nvidia" and gpu.index is not None:
            lines.append(f"CUDA_VISIBLE_DEVICES={gpu.index}")
        return "\n".join(lines) + "\n"

    def unit_text(self) -> str:
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

    def install(self, folder: Path, models: Path, start_at_login: bool, start_now: bool, gpu: Gpu,
                release: Release, progress, cancelled):
        folder.mkdir(parents=True, exist_ok=True)
        (folder / MARKER).write_text(json.dumps({"runtime": self.key}) + "\n")
        models.mkdir(parents=True, exist_ok=True)
        version_dir = folder / release.version
        downloads = folder / "downloads"
        done = 0
        for name, url, size in release.assets:
            progress(done / max(1, release.size), f"download {name}")
            download(url, downloads / name, size, progress, cancelled, done, release.size)
            done += size
        if cancelled():
            raise Cancelled()
        progress(1.0, "unpack")
        partial = folder / f".{release.version}.partial"
        shutil.rmtree(partial, ignore_errors=True)
        for name, _url, _size in release.assets:
            extract(downloads / name, partial)
        if not (partial / "bin" / self.command).exists():
            raise RuntimeError("the archive has no ollama program")
        shutil.rmtree(version_dir, ignore_errors=True)
        os.replace(partial, version_dir)
        shutil.rmtree(downloads, ignore_errors=True)
        self.switch(folder, release.version)
        _write(self.env_file, self.env_text(models, gpu), 0o600)
        _write(UNIT_DIR / f"{self.service}.service", self.unit_text())
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
        if link.exists() and not link.is_symlink():
            raise RuntimeError(f"{link} is a file of its own; not replaced")
        new = BIN_DIR / f".{self.command}.new"
        new.unlink(missing_ok=True)
        new.symlink_to(current / "bin" / self.command)
        os.replace(new, link)

    def versions(self, folder: Path) -> list[str]:
        return sorted((p.name for p in folder.iterdir() if p.is_dir() and re.fullmatch(r"v\d+\.\d+\.\d+", p.name)),
                      key=lambda v: [int(x) for x in v[1:].split(".")])

    def current_version(self) -> str:
        folder = self.installed_folder()
        try:
            return os.readlink(folder / "current") if folder else ""
        except OSError:
            return ""

    def update(self, release: Release, gpu: Gpu, progress, cancelled):
        """New version folder, switch `current`, restart if it ran; the previous version stays as a fallback."""
        folder = self.installed_folder()
        if folder is None:
            raise RuntimeError("not installed here")
        running = _systemctl("is-active", "--quiet", f"{self.service}.service")[0]
        models = Path(self.models_folder() or folder / "models")
        login = _systemctl("is-enabled", "--quiet", f"{self.service}.service")[0]
        self.install(folder, models, login, running, gpu, release, progress, cancelled)
        for old in self.versions(folder)[:-2]:
            shutil.rmtree(folder / old, ignore_errors=True)

    def set_models_folder(self, models: Path):
        models.mkdir(parents=True, exist_ok=True)
        set_env_value(self.env_file, "OLLAMA_MODELS", str(models))
        if _systemctl("is-active", "--quiet", f"{self.service}.service")[0]:
            _systemctl("restart", f"{self.service}.service")

    def uninstall(self):
        """Program, link, service and its settings; never the models."""
        folder = self.installed_folder()
        if folder is None:
            raise RuntimeError("not installed by AI Agents Map; remove it the way it was installed")
        _systemctl("stop", f"{self.service}.service")
        _systemctl("disable", f"{self.service}.service")
        (UNIT_DIR / f"{self.service}.service").unlink(missing_ok=True)
        _systemctl("daemon-reload")
        (BIN_DIR / self.command).unlink(missing_ok=True)
        models = Path(self.models_folder()) if self.models_folder() else None
        self.env_file.unlink(missing_ok=True)
        for child in folder.iterdir():
            if models is not None and (child == models or models.is_relative_to(child)):
                continue            # the model folder was chosen inside the program folder: keep it
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
        if not any(folder.iterdir()):
            folder.rmdir()


INSTALLERS = {"ollama": Ollama()}


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
