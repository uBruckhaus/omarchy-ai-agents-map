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

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

import ai_core
import harnesses
import netfetch
import providers

HOME = Path.home()
BIN_DIR = HOME / ".local/bin"
UNIT_DIR = HOME / ".config/systemd/user"
MARKER = ".ai-agents-map"
API_TIMEOUT = 20
MAX_DOWNLOAD = 8 * 1024 ** 3      # when a release does not name its size
MAX_UNPACKED = 20 * 1024 ** 3     # an unpacked release (Ollama with ROCm is a few GB)
NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


class Cancelled(Exception):
    pass


class DownloadError(RuntimeError):
    """The connection broke off: trying again resumes the download (jobs.retryable)."""


@dataclass
class Release:
    version: str
    assets: list = field(default_factory=list)   # [(name, url, size)]
    digests: dict = field(default_factory=dict)  # name -> SHA-256 (or SHA-512) hex the release lists for it

    @property
    def size(self) -> int:
        return sum(size for _name, _url, size in self.assets)

    def to_json(self) -> dict:
        return {"version": self.version, "assets": [list(a) for a in self.assets], "digests": dict(self.digests)}

    @classmethod
    def from_json(cls, data: dict) -> "Release":
        """A release kept by jobs.py (own 0600 file): checked again before use."""
        version = str(data.get("version", ""))
        if not NAME_RE.fullmatch(version):
            raise ValueError("bad release")
        assets = []
        for item in data.get("assets", [])[:8]:
            name, url, size = str(item[0]), str(item[1]), int(item[2])
            if not NAME_RE.fullmatch(name) or not url.startswith("https://") or size < 0:
                raise ValueError("bad release")
            assets.append((name, url, size))
        digests = {str(k): str(v) for k, v in (data.get("digests") or {}).items()
                   if re.fullmatch(r"[0-9a-f]{64}|[0-9a-f]{128}", str(v))}
        return cls(version, assets, digests)


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
        out = ai_core.read_bounded_output(["lspci"], max_bytes=256 * 1024, timeout=5)[0].decode("utf-8", "replace")
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


# Paths go into systemd units and EnvironmentFile lines: no specifiers (%), quotes,
# backslashes, $ or control characters there.
_UNIT_SAFE = re.compile(r"^[^\x00-\x1f\x7f%\"'\\$`]+$")


def check_env_value(value: str):
    if not _UNIT_SAFE.match(value or "x") or len(value) > 1024:
        raise ValueError("this path has characters a service file cannot hold (% \" ' \\ $ `)")


def unit_path(path) -> str:
    """A path as one argument of a service file: checked (no % " ' \\ $ ` or control
    characters), then in double quotes, so spaces stay inside it."""
    check_env_value(str(path))
    return f'"{path}"'


def check_folder(text: str) -> Path:
    """A folder the user typed: absolute (or ~), no control characters, ours to write."""
    text = (text or "").strip()
    if not text or len(text) > 1024 or not _UNIT_SAFE.match(text):
        raise ValueError("not a folder path (no % \" ' \\ $ ` or control characters)")
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
    try:
        data = netfetch.fetch_json(url, max_bytes=2_000_000, timeout=API_TIMEOUT)
    except netfetch.FetchError as error:
        raise RuntimeError(str(error)) from error
    if not isinstance(data, dict):
        raise RuntimeError("unexpected answer")
    return data


def github_asset(asset: dict) -> tuple:
    """(name, url, size, sha256) of a GitHub release asset. GitHub lists a SHA-256
    for every asset ("digest": "sha256:<hex>"); without one nothing is downloaded."""
    name = str(asset.get("name", ""))
    match = re.fullmatch(r"sha256:([0-9a-f]{64})", str(asset.get("digest") or ""))
    if not match:
        raise RuntimeError(f"{name}: the release lists no SHA-256 checksum")
    return name, asset["browser_download_url"], int(asset.get("size", 0)), match.group(1)


def github_release(version: str, assets: list) -> Release:
    picked = [github_asset(a) for a in assets]
    return Release(version, [(n, u, s) for n, u, s, _d in picked], {n: d for n, _u, _s, d in picked})


def _hasher(expected: str = ""):
    """The hash a release's checksum is in: SHA-512 for 128 hex digits, else SHA-256."""
    return hashlib.sha512() if len(expected) == 128 else hashlib.sha256()


def sha256_of(path: Path, expected: str = "") -> str:
    """The file's SHA-256 (or SHA-512 when `expected` is one), to compare with `expected`."""
    digest = _hasher(expected)
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def own_regular_file(path: Path) -> bool:
    """A regular file of this account with one name (not a link, not someone else's, not a hard
    link that another name could change): only such a file is resumed, kept or read as ours."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISREG(st.st_mode) and st.st_uid == os.getuid() and st.st_nlink == 1


def open_staging(partial: Path) -> int:
    """The download's staging file, never through a link: created new and exclusively, or, to
    resume, an existing one that is this account's own regular file with one name. New files get
    0644 within the umask, as curl gave them (a model folder may be shared on purpose)."""
    flags = os.O_RDWR | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        return os.open(partial, flags | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        pass
    try:
        fd = os.open(partial, flags)            # a link at that name fails here (ELOOP)
    except OSError as error:
        raise RuntimeError(f"{partial.name} is not a file this download can continue: {error}") from error
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_nlink != 1:
        os.close(fd)
        raise RuntimeError(f"{partial.name} in {partial.parent} is not this account's own download; "
                           "remove it and try again")
    return fd


def _sha256_of_fd(fd: int, expected: str = "") -> str:
    digest = _hasher(expected)
    offset = 0
    while block := os.pread(fd, 1024 * 1024, offset):
        digest.update(block)
        offset += len(block)
    return digest.hexdigest()


def download(url: str, target: Path, size: int, progress, cancelled, done_before: int = 0, total: int = 0,
             sha256: str = ""):
    """curl into target, reporting how far along (of total bytes) it is. With sha256
    the file only becomes target when its bytes match it (else it is deleted).

    The bytes go into a staging file `<target>.part` that this function opens itself (open_staging:
    exclusive and no-follow, or an own regular file to resume); curl writes to that open file on its
    standard output and never opens a path. The file is checked through the same descriptor and only
    renamed into place while the name still points at it."""
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".part")
    limit_bytes = size or MAX_DOWNLOAD
    fd = open_staging(partial)
    try:
        have = os.fstat(fd).st_size
        if have > limit_bytes:                  # more than announced: start again
            os.ftruncate(fd, 0)
            have = 0
        # The size from the release list caps the download (a wrong or endless body stops there).
        argv = ["curl", "-fsSL", "--proto", "=https", "--proto-redir", "=https", "--max-redirs", "5",
                "--max-filesize", str(limit_bytes)]
        if have:
            argv += ["-C", str(have)]           # resume at the checked size of our own file
        proc = subprocess.Popen([*argv, "--", url], stdin=subprocess.DEVNULL, stdout=fd, stderr=subprocess.PIPE)
        try:
            while proc.poll() is None:
                if cancelled():
                    proc.kill()
                    raise Cancelled()
                have = os.fstat(fd).st_size
                if have > limit_bytes:
                    proc.kill()
                    raise RuntimeError("the download is larger than announced")
                if total or size:
                    progress((done_before + have) / max(1, total or size), None)
                else:
                    # No size announced: a bar that keeps moving but never reaches the end, and the MB.
                    progress(have / (have + 200 * 1024 * 1024), f"{have // (1024 * 1024)} MB")
                time.sleep(0.4)
        finally:
            if proc.poll() is None:
                proc.kill()
        if proc.returncode != 0:
            raise DownloadError(f"download failed: {proc.stderr.read(400).decode('utf-8', 'replace').strip()}")
        st = os.fstat(fd)
        if size and st.st_size != size:
            raise DownloadError("download incomplete")
        if sha256 and _sha256_of_fd(fd, sha256) != sha256:
            os.ftruncate(fd, 0)
            _remove_if_same(partial, st)
            raise RuntimeError(f"{target.name} does not match the release's checksum; nothing was installed")
        os.fsync(fd)
        try:
            now = os.lstat(partial)
        except OSError:
            now = None
        if now is None or (now.st_dev, now.st_ino) != (st.st_dev, st.st_ino):
            raise RuntimeError(f"{partial.name} was replaced while downloading; nothing was kept")
        os.replace(partial, target)
    finally:
        os.close(fd)


def _remove_if_same(path: Path, st):
    try:
        now = os.lstat(path)
    except OSError:
        return
    if (now.st_dev, now.st_ino) == (st.st_dev, st.st_ino):
        path.unlink(missing_ok=True)


def extract(archive: Path, folder: Path):
    folder.mkdir(parents=True, exist_ok=True)
    flag = "--zstd" if archive.name.endswith(".zst") else "-z" if archive.name.endswith((".tgz", ".gz")) else ""
    argv = ["tar", "-x", "-f", str(archive), "-C", str(folder), "--no-same-owner", "--no-same-permissions"] \
        + ([flag] if flag else [])
    out, ok = ai_core.read_bounded_output(argv, max_bytes=64 * 1024, timeout=1800, merge_stderr=True)
    if not ok:
        raise RuntimeError(f"unpacking failed: {out.decode('utf-8', 'replace').strip()[:300]}")
    check_unpacked_size(folder)


# --- files and services ------------------------------------------------------------------------------

def _write(path: Path, text: str, mode: int = 0o644):
    if path.suffix in (".service", ".desktop") and ("%" in text.replace(" %U", "")
                                                   or any(ord(c) < 32 and c != "\n" for c in text)):
        raise ValueError(f"{path.name}: a path holds characters a service file cannot have")
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
    out, ok = ai_core.read_bounded_output(["systemctl", "--user", *args], max_bytes=64 * 1024, timeout=60,
                                          merge_stderr=True)
    return ok, out.decode("utf-8", "replace").strip()[:300]


def check_unpacked_size(folder: Path, limit: int = 0):
    """An unpacked release larger than MAX_UNPACKED (a crafted archive) is removed again."""
    limit = limit or MAX_UNPACKED
    total = 0
    for root, _dirs, files in os.walk(folder):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                continue
            if total > limit:
                shutil.rmtree(folder, ignore_errors=True)
                raise RuntimeError("the unpacked release is too large")


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
    check_env_value(value)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    line = f"{key}={value}"
    if re.search(rf"^\s*{key}=", text, re.MULTILINE):
        text = re.sub(rf"^\s*{key}=.*$", lambda _m: line, text, count=1, flags=re.MULTILINE)
    else:
        text = text.rstrip("\n") + ("\n" if text.strip() else "") + line + "\n"
    _write(path, text, 0o600)


# --- installers ------------------------------------------------------------------------------------

def _curl_json_any(url: str):
    try:
        return netfetch.fetch_json(url, max_bytes=5_000_000, timeout=API_TIMEOUT)
    except netfetch.FetchError as error:
        raise RuntimeError(str(error)) from error


def unpack_appimage(archive: Path, folder: Path):
    """An AppImage whose SHA-256 was checked against its release (Installer.install):
    its own --appimage-extract, so FUSE is not needed."""
    archive.chmod(0o755)
    _out, ok = ai_core.read_bounded_output([str(archive), "--appimage-extract"], max_bytes=64 * 1024,
                                           timeout=900, cwd=str(folder))
    if not ok:
        raise RuntimeError("unpacking the AppImage failed")
    check_unpacked_size(folder)


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
    login_default = False        # the install page's "Start at login" switch
    uses_models = True           # False: an agent (no model folder)
    builds: tuple = ()           # (key, label) choices, first = default for the card (see default_build)
    can_install = True           # False: no checksummed release to install from (only Uninstall of an earlier install)

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
        if not self.can_install:
            raise RuntimeError(f"{self.name} is not installed from here; use its own installer")
        missing = [name for name, _url, _size in release.assets if name not in release.digests]
        if missing:
            raise RuntimeError(f"{missing[0]}: no SHA-256 checksum to check it against; nothing was installed")
        folder.mkdir(parents=True, exist_ok=True)
        (folder / MARKER).write_text(json.dumps({"runtime": self.key}) + "\n")
        if models is not None and self.uses_models:
            models.mkdir(parents=True, exist_ok=True)
        downloads = folder / "downloads"
        done = 0
        for asset_name, url, size in release.assets:
            progress(done / max(1, release.size), f"download {asset_name}")
            target = downloads / asset_name
            # Continued after an interruption: a file already downloaded and checked is kept.
            if not (own_regular_file(target) and sha256_of(target, release.digests[asset_name])
                    == release.digests[asset_name]):
                download(url, target, size, progress, cancelled, done, release.size,
                         sha256=release.digests[asset_name])
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
        models = Path(self.models_folder() or folder / "models") if self.uses_models else None
        self.install(folder, models, login, running, gpu, release, progress, cancelled)
        for old in self.versions(folder)[:-2]:
            shutil.rmtree(folder / old, ignore_errors=True)

    def set_models_folder(self, models: Path):
        models = check_folder(str(models))      # it goes into the unit's environment file and settings
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
        if models is not None:
            # Its settings go now; a reinstall offers this folder first (models_suggestions).
            kept = ai_core.setting("removed_models_folders")
            kept = dict(kept) if isinstance(kept, dict) else {}
            kept[self.key] = str(models)
            ai_core.update_settings(removed_models_folders=kept)
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
        return github_release(version, [assets[n] for n in wanted])

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
ExecStart={unit_path(BIN_DIR / self.command)} serve
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
            return github_release(f"{version}-{build}", picked)
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
ExecStart={unit_path(BIN_DIR / self.command)} --models-preset {unit_path(preset)} --models-max 1 --gpu-layers all --flash-attn on
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
    """LM Studio publishes no checksum or signature for its AppImage, so it is
    not installed or updated from here (lmstudio.ai). Kept for a copy installed
    by an earlier version: its model folder, desktop entry and Uninstall."""
    key, name, command, service = "lmstudio", "LM Studio", "lm-studio", ""
    bin_rel = "squashfs-root/AppRun"
    has_service = False
    can_install = False
    site = "https://lmstudio.ai/download"
    settings = HOME / ".lmstudio/settings.json"
    desktop = HOME / ".local/share/applications/lm-studio-ai-agents-map.desktop"

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
            out, ok = ai_core.read_bounded_output(argv, max_bytes=1024 * 1024, timeout=3600, merge_stderr=True)
            if not ok:
                raise RuntimeError(out.decode("utf-8", "replace").strip()[-300:] or "pip failed")
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
ExecStart={unit_path(BIN_DIR / self.command)} serve ${{VLLM_MODEL}} --host {host} --port {port}
Restart=on-failure
KillMode=control-group

[Install]
WantedBy=default.target
"""


class KoboldCpp(Installer):
    """KoboldCpp's single-file Linux build as a model router: --admin with
    --routermode serves every .kcpps in ~/.config/koboldcpp/models (one per
    GGUF of the model folder, kobold_ctl.sync) and loads one on request."""
    key, name, command, service = "koboldcpp", "KoboldCpp", "koboldcpp", "koboldcpp"
    bin_rel = "koboldcpp"
    env_file = HOME / ".config/koboldcpp/server.env"
    address = "127.0.0.1:5001"
    builds = (("vulkan", "Vulkan"), ("cuda", "CUDA"), ("cpu", "CPU"))
    login_default = True         # stays up between agents (see provider_watch.STAYS_UP)
    ASSETS = {"vulkan": "koboldcpp-linux-x64-nocuda", "cuda": "koboldcpp-linux-x64", "cpu": "koboldcpp-linux-x64-nocuda"}

    def default_build(self, gpu: Gpu) -> str:
        return "cuda" if gpu.vendor == "nvidia" else "vulkan" if gpu.vendor == "amd" else "cpu"

    def latest(self, gpu: Gpu, build: str = "") -> Release:
        build = build or self.default_build(gpu)
        data = _curl_json("https://api.github.com/repos/LostRuins/koboldcpp/releases/latest")
        version = str(data.get("tag_name", ""))
        if not re.fullmatch(r"v\d+\.\d+(\.\d+)?", version):
            raise RuntimeError("no release found")
        assets = {a.get("name"): a for a in data.get("assets", []) if isinstance(a, dict)}
        wanted = self.ASSETS[build]
        if wanted not in assets:
            raise RuntimeError(f"{version} has no Linux build")
        asset = assets[wanted]
        return github_release(f"{version}-{build}", [asset])

    def unpack(self, archive: Path, folder: Path):
        """One program file: copied in as `koboldcpp`, made executable."""
        target = folder / self.bin_rel
        shutil.copyfile(archive, target)
        target.chmod(0o755)

    def build_of(self) -> str:
        build = read_env(self.env_file).get("KOBOLDCPP_BUILD", "")
        return build if build in self.ASSETS else "vulkan"

    def models_folder(self) -> str:
        return read_env(self.env_file).get("KOBOLDCPP_MODELS", "")

    def set_models_folder_value(self, models: Path):
        set_env_value(self.env_file, "KOBOLDCPP_MODELS", str(models))
        self.sync(models)

    def env_text(self, models: Path, gpu: Gpu) -> str:
        build = self.default_build(gpu)
        version = self.current_version()
        build = version.rsplit("-", 1)[-1] if version.rsplit("-", 1)[-1] in self.ASSETS else build
        lines = ["# Written by AI Agents Map. Change the model folder there (right-click KoboldCpp).",
                 f"KOBOLDCPP_MODELS={models}", f"KOBOLDCPP_BUILD={build}"]
        if gpu.vendor == "nvidia" and gpu.index is not None:
            lines.append(f"CUDA_VISIBLE_DEVICES={gpu.index}")
        return "\n".join(lines) + "\n"

    def unit_text(self, models: Path = None) -> str:
        import inventory
        host, _sep, port = self.address.rpartition(":")
        build = self.build_of()
        gpu_flag = {"cuda": "--usecuda", "vulkan": "--usevulkan", "cpu": "--usecpu"}[build]
        return f"""[Unit]
Description=KoboldCpp model router (installed by AI Agents Map)
After=network.target

[Service]
Type=exec
EnvironmentFile={self.env_file}
# Router mode: every .kcpps in {inventory.KOBOLD_CONFIGS} is a model, loaded on request.
ExecStart={unit_path(BIN_DIR / self.command)} --skiplauncher --nomodel --admin --admindir {unit_path(inventory.KOBOLD_CONFIGS)} --routermode --host {host} --port {port} {gpu_flag} --quiet
Restart=on-failure
RestartSec=3
# Stopping ends the model process too, so no video memory stays taken.
KillMode=control-group

[Install]
WantedBy=default.target
"""

    def sync(self, models: Path):
        import kobold_ctl
        check_env_value(str(models))
        kobold_ctl.sync(models, self.build_of(), lambda: detect_gpu().vram)

    def configure(self, models: Path):
        self.sync(models)

    def unconfigure(self):
        """The .kcpps files AI Agents Map made (the user's own stay)."""
        import inventory
        import kobold_ctl
        for path in inventory.KOBOLD_CONFIGS.glob("*.kcpps"):
            if kobold_ctl.read(path).get(kobold_ctl.MARK):
                path.unlink(missing_ok=True)
        for folder in (inventory.KOBOLD_CONFIGS, inventory.KOBOLD_CONFIGS.parent):
            try:
                folder.rmdir()             # only when empty
            except OSError:
                pass


class Jan(Installer):
    """Jan's AppImage, unpacked (as LM Studio's), with a desktop entry and
    `jan-desktop` in ~/.local/bin. For agents, Jan's own engine
    (jan-llama-worker, a llama.cpp router) runs headless as jan.service on
    inventory.JAN_PORT and serves the models of Jan's data folder with Jan's
    per-model settings (router.preset.ini). Models are added in the Jan app."""
    key, name, command, service = "jan", "Jan", "jan-desktop", "jan"
    bin_rel = "squashfs-root/AppRun"
    worker_rel = "squashfs-root/usr/lib/Jan/resources/bin"
    desktop = HOME / ".local/share/applications/jan-ai-agents-map.desktop"

    def latest(self, gpu: Gpu, build: str = "") -> Release:
        data = _curl_json("https://api.github.com/repos/janhq/jan/releases/latest")
        version = str(data.get("tag_name", ""))
        if not re.fullmatch(r"v\d+\.\d+\.\d+", version):
            raise RuntimeError("no release found")
        wanted = f"Jan_{version[1:]}_amd64.AppImage"
        asset = next((a for a in data.get("assets", []) if isinstance(a, dict) and a.get("name") == wanted), None)
        if asset is None:
            raise RuntimeError(f"{version} has no Linux AppImage")
        return github_release(version, [asset])

    def unpack(self, archive: Path, folder: Path):
        unpack_appimage(archive, folder)

    def models_folder(self) -> str:
        import inventory
        return str(inventory.jan_data_folder())

    def set_models_folder_value(self, models: Path):
        """Jan's data folder (models, threads, settings): only set before Jan has
        one; later Jan moves it itself (Settings › General › Data folder)."""
        import inventory
        settings = inventory.JAN_ROOT / "settings.json"
        try:
            data = json.loads(settings.read_text(encoding="utf-8")[:2_000_000])
        except FileNotFoundError:
            data = {}
        except (OSError, ValueError):
            raise RuntimeError("Jan's settings.json cannot be read; change the folder in Jan")
        if not isinstance(data, dict):
            data = {}
        current = inventory.jan_data_folder()
        if data.get("data_folder") and Path(os.path.expanduser(str(data["data_folder"]))) != models \
                and any(current.glob("llamacpp/models/*")):
            raise RuntimeError("Jan has models in its data folder already; move it in Jan (Settings › General)")
        data["data_folder"] = str(models)
        _write(settings, json.dumps(data, indent=2) + "\n", 0o644)

    def unit_text(self, models: Path = None) -> str:
        import inventory
        bin_dir = (self.installed_folder() or self.default_folder) / "current" / self.worker_rel
        preset = inventory.jan_preset()
        worker, preset_arg = unit_path(bin_dir / "jan-llama-worker"), unit_path(preset)
        library = unit_path(f"LD_LIBRARY_PATH={bin_dir}")
        return f"""[Unit]
Description=Jan's engine for agents (installed by AI Agents Map)
After=network.target

[Service]
Type=exec
Environment={library}
# jan-llama-worker ends when its input closes (the Jan app's way to stop it):
# `sleep infinity` keeps it open; systemd stops both. The paths reach the shell
# as arguments ($0, $1), never as part of its script.
ExecStart=/bin/sh -c 'sleep infinity | exec "$0" --preset "$1" --port {inventory.JAN_PORT} --models-max 1' {worker} {preset_arg}
Restart=on-failure
RestartSec=3
KillMode=control-group

[Install]
WantedBy=default.target
"""

    def configure(self, models: Path):
        import jan_ctl
        self.set_models_folder_value(models)
        jan_ctl.ensure_preset()
        folder = self.installed_folder() or self.default_folder
        icon = next(iter(sorted(folder.glob("current/squashfs-root/*.png"))), None)
        _write(self.desktop, f"""[Desktop Entry]
Type=Application
Name=Jan
Comment=Chat with local LLMs (installed by AI Agents Map)
Exec={BIN_DIR / self.command} %U
Icon={icon or "jan"}
Categories=Development;
Terminal=false
""")

    def unconfigure(self):
        self.desktop.unlink(missing_ok=True)


class Bionic(Installer):
    """LM Studio Bionic, LM Studio's agent app. Like LM Studio it has no
    checksum or signature, so it is not installed or updated from here; kept
    for a copy installed by an earlier version (desktop entry, Uninstall)."""
    key, name, command, service = "bionic", "LM Studio Bionic", "bionic", ""
    bin_rel = "squashfs-root/AppRun"
    has_service = False
    uses_models = False
    can_install = False
    site = "https://lmstudio.ai"
    desktop = HOME / ".local/share/applications/bionic-ai-agents-map.desktop"

    def configure(self, models: Path):
        folder = self.installed_folder() or self.default_folder
        icon = next(iter(sorted(folder.glob("current/squashfs-root/*.png"))), None)
        _write(self.desktop, f"""[Desktop Entry]
Type=Application
Name=LM Studio Bionic
Comment=Agent for open models (installed by AI Agents Map)
Exec={BIN_DIR / self.command} %U
Icon={icon or "bionic"}
Categories=Development;
Terminal=false
""")

    def unconfigure(self):
        self.desktop.unlink(missing_ok=True)


class Dsh(Installer):
    """DeepSeek Harness from npm into a version folder (npm install --prefix,
    install scripts off: no package code runs while installing), `dsh` in
    ~/.local/bin. Needs Node.js (mise or the system's)."""
    key, name, command, service = "dsh", "DeepSeek Harness", "dsh", ""
    bin_rel = "node_modules/.bin/dsh"
    has_service = False
    uses_models = False
    PACKAGE = "@deepseek-ai/dsh"

    def latest(self, gpu: Gpu, build: str = "") -> Release:
        data = _curl_json("https://registry.npmjs.org/@deepseek-ai%2fdsh")
        tags = data.get("dist-tags") if isinstance(data.get("dist-tags"), dict) else {}
        version = str(tags.get("latest", ""))
        if not re.fullmatch(r"\d+\.\d+\.\d+(-[0-9A-Za-z.]+)?", version):
            raise RuntimeError("no release found")
        return Release(f"v{version}", [])             # npm downloads it

    def install(self, folder: Path, models: Path, start_at_login: bool, start_now: bool, gpu: Gpu,
                release: Release, progress, cancelled):
        npm = shutil.which("npm")
        if not npm:
            raise RuntimeError("needs Node.js with npm (mise use -g node)")
        folder.mkdir(parents=True, exist_ok=True)
        (folder / MARKER).write_text(json.dumps({"runtime": self.key}) + "\n")
        partial = folder / f".{release.version}.partial"
        shutil.rmtree(partial, ignore_errors=True)
        partial.mkdir()
        (partial / "package.json").write_text('{"private": true}\n')
        progress(0.1, f"npm install {self.PACKAGE}@{release.version[1:]}")
        out, ok = ai_core.read_bounded_output(
            [npm, "install", "--prefix", str(partial), "--ignore-scripts", "--no-fund", "--no-audit", "--omit=dev",
             f"{self.PACKAGE}@{release.version[1:]}"], max_bytes=1024 * 1024, timeout=1800, merge_stderr=True)
        if cancelled():
            shutil.rmtree(partial, ignore_errors=True)
            raise Cancelled()
        if not ok or not (partial / self.bin_rel).exists():
            shutil.rmtree(partial, ignore_errors=True)
            raise RuntimeError(out.decode("utf-8", "replace").strip()[-300:] or "npm failed")
        check_unpacked_size(partial)
        version_dir = folder / release.version
        shutil.rmtree(version_dir, ignore_errors=True)
        os.replace(partial, version_dir)
        self.switch(folder, release.version)
        progress(1.0, "done")


class Agy(Installer):
    """Google's Antigravity CLI (`agy`): the release Google's own install script would take, from
    its manifest (version, a tarball on storage.googleapis.com and its SHA-512), checked before
    anything is unpacked; the binary goes into a version folder, `agy` into ~/.local/bin. Google's
    script is not run (its last step only adds ~/.local/bin to shell profiles)."""
    key, name, command, service = "agy", "Antigravity CLI", "agy", ""
    bin_rel = "antigravity"
    has_service = False
    uses_models = False
    site = "https://antigravity.google"
    MANIFEST = "https://antigravity-cli-auto-updater-974169037036.us-central1.run.app/manifests/linux_{0}.json"
    DOWNLOADS = "https://storage.googleapis.com/antigravity-public/antigravity-cli/"
    ARCHES = {"x86_64": "amd64", "aarch64": "arm64"}

    def latest(self, gpu: Gpu, build: str = "") -> Release:
        arch = self.ARCHES.get(os.uname().machine)
        if arch is None:
            raise RuntimeError(f"no Antigravity CLI for {os.uname().machine}")
        data = _curl_json(self.MANIFEST.format(arch))
        version, url, sha512 = (str(data.get(k) or "") for k in ("version", "url", "sha512"))
        name = url.rsplit("/", 1)[-1]
        if not re.fullmatch(r"\d+\.\d+\.\d+", version) or not re.fullmatch(r"[0-9a-f]{128}", sha512) \
                or not url.startswith(self.DOWNLOADS) or not name.endswith(".tar.gz") or not NAME_RE.fullmatch(name) \
                or any(c in url for c in "\"'\\ \n\r\t") or "/../" in url:
            raise RuntimeError("Google's release manifest is not in the expected form; nothing was installed")
        return Release(f"v{version}", [(name, url, 0)], {name: sha512})


# --- installs described as data (harnesses.Download), for agents and local LLMs from add-ons ----------

_REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}/[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
_WORD = re.compile(r"[A-Za-z0-9@:/._=+,{}-]{1,300}")
_ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
_PLACEHOLDERS = ("{binary}", "{host}", "{port}", "{models}", "{folder}")


def check_download(spec) -> None:
    """Refuse a Download that is not plain data of the expected form (raises ValueError): a GitHub
    repository, regexes that compile, a relative program path, plain-word service arguments with
    only the known placeholders, environment names in capitals."""
    if not isinstance(spec, harnesses.Download):
        raise ValueError("not a Download")
    if not _REPO.fullmatch(spec.repo or "") or not NAME_RE.fullmatch(spec.command or ""):
        raise ValueError("repo must be owner/name and command a plain file name")
    rel = Path(spec.binary or "")
    if not spec.binary or rel.is_absolute() or ".." in rel.parts or len(spec.binary) > 200:
        raise ValueError("binary must be a relative path inside the release")
    if not spec.assets or len(spec.assets) > 8:
        raise ValueError("assets: one to eight (build, regex) pairs")
    for build, pattern in list(spec.assets) + [("", spec.tag)]:
        if not isinstance(build, str) or len(build) > 32 or not isinstance(pattern, str) or len(pattern) > 200:
            raise ValueError("assets: (build, regex) pairs of short text")
        re.compile(pattern)
    for word in spec.service:
        if not isinstance(word, str) or not _WORD.fullmatch(word) or \
                any(part.startswith("{") and part not in _PLACEHOLDERS for part in re.findall(r"\{[^}]*\}", word)):
            raise ValueError(f"service: {word!r} is not a plain word")
    for name, value in spec.env:
        if not _ENV_NAME.fullmatch(str(name)) or not _WORD.fullmatch(str(value)):
            raise ValueError(f"env: {name!r} is not NAME=plain value")
    if spec.models_env and spec.models_env not in {n for n, _v in spec.env}:
        raise ValueError("models_env must be one of env's names")
    if spec.address and not re.fullmatch(r"127\.0\.0\.1:\d{2,5}", spec.address):
        raise ValueError("address must be 127.0.0.1:<port>")


class Declared(Installer):
    """An install an adapter describes as data (harnesses.Download): the newest GitHub release whose
    tag matches, the asset for the chosen build, its SHA-256 from the release checked before anything
    is unpacked (Installer.install), a version folder, `current`, the command in ~/.local/bin, and a
    user service when it has one. Nothing of the release runs while installing."""

    def __init__(self, key: str, name: str, spec, uses_models: bool):
        check_download(spec)
        self.spec = spec
        self.key, self.name, self.command = key, name, spec.command
        self.service = f"{key}-ai-agents-map" if spec.service else ""
        self.has_service = bool(spec.service)
        self.uses_models = uses_models
        self.bin_rel = spec.binary
        self.address = spec.address
        self.builds = tuple(spec.builds) or tuple((b, b or name) for b, _p in spec.assets)
        self.login_default = spec.login_default
        self.env_file = HOME / ".config/ai-agents-map/services" / f"{key}.env" if spec.service else HOME
        self.site = f"https://github.com/{spec.repo}"

    def default_build(self, gpu: Gpu) -> str:
        wanted = dict(self.spec.gpu_builds).get(getattr(gpu, "vendor", ""), "")
        known = [b for b, _p in self.spec.assets]
        return wanted if wanted in known else known[0]

    def latest(self, gpu: Gpu, build: str = "") -> Release:
        build = build or self.default_build(gpu)
        pattern = dict(self.spec.assets).get(build)
        if pattern is None:
            raise RuntimeError(f"no build {build!r}")
        releases = _curl_json_any(f"https://api.github.com/repos/{self.spec.repo}/releases?per_page=10")
        for data in releases if isinstance(releases, list) else []:
            tag = str(data.get("tag_name", "")) if isinstance(data, dict) else ""
            if data.get("draft") or data.get("prerelease") or not re.fullmatch(self.spec.tag, tag) \
                    or not NAME_RE.fullmatch(tag):
                continue
            assets = [a for a in data.get("assets", []) if isinstance(a, dict)
                      and re.fullmatch(pattern, str(a.get("name", "")))]
            if assets:
                suffix = f"-{build}" if build and len(self.spec.assets) > 1 else ""
                return github_release(f"{tag}{suffix}", assets[:1])
        raise RuntimeError(f"no release of {self.spec.repo} has a file for this PC")

    def unpack(self, archive: Path, folder: Path):
        if self.spec.single_file:
            target = folder / self.bin_rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(archive, target)
            target.chmod(0o755)
        else:
            extract(archive, folder)

    def _values(self, models: Path | None) -> dict:
        host, _sep, port = (self.spec.address or "127.0.0.1:0").rpartition(":")
        folder = self.installed_folder() or self.default_folder
        return {"{binary}": unit_path(BIN_DIR / self.command), "{host}": host, "{port}": port,
                "{models}": str(models or ""), "{folder}": str(folder)}

    def _fill(self, word: str, values: dict) -> str:
        for key, value in values.items():
            word = word.replace(key, value)
        return word

    def env_text(self, models: Path, gpu: Gpu) -> str:
        if not self.spec.env:
            return ""
        values = self._values(models)
        lines = [f"# Written by AI Agents Map for {self.name}."]
        lines += [f"{name}={self._fill(value, values)}" for name, value in self.spec.env]
        return "\n".join(lines) + "\n"

    def unit_text(self, models: Path = None) -> str:
        import shlex
        values = self._values(models or (Path(self.models_folder()) if self.models_folder() else None))
        argv = [self._fill(word, values) for word in self.spec.service]
        env = f"EnvironmentFile={self.env_file}\n" if self.spec.env else ""
        return f"""[Unit]
Description={self.name} (installed by AI Agents Map)
After=network.target

[Service]
Type=exec
{env}ExecStart={shlex.join(argv)}
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
"""

    def models_folder(self) -> str:
        return read_env(self.env_file).get(self.spec.models_env, "") if self.spec.models_env else ""

    def set_models_folder_value(self, models: Path):
        if not self.spec.models_env:
            raise RuntimeError(f"{self.name} has no model folder setting")
        set_env_value(self.env_file, self.spec.models_env, str(models))
        if self.has_service:
            _write(UNIT_DIR / f"{self.service}.service", self.unit_text(models))
            _systemctl("daemon-reload")


def _installer_of(adapter, uses_models: bool):
    """An adapter's installer: its own class (installer()), else one from its Download data."""
    if adapter.installer is not None:
        return adapter.installer()
    if getattr(adapter, "download", None) is not None:
        try:
            return Declared(getattr(adapter, "key", "") or adapter.command, adapter.name, adapter.download, uses_models)
        except (ValueError, re.error):
            return None          # not of the expected form: no installer (add_harness refused it already)
    return None


def _rebuild():
    """One installer per local LLM (providers.py) and per agent this app installs itself
    (harnesses.py), from their own class or their Download data; run again when add-ons change them."""
    global INSTALLERS, AGENT_INSTALLERS
    INSTALLERS = {p.key: inst for p in providers.local()
                  if (inst := _installer_of(p, True)) is not None}
    AGENT_INSTALLERS = {h.command: inst for h in harnesses.all_harnesses()
                        if (inst := _installer_of(h, False)) is not None}


_rebuild()
providers.on_change(_rebuild)
harnesses.on_change(_rebuild)


def installer(key: str):
    return INSTALLERS.get(key) or AGENT_INSTALLERS.get(key)


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


def models_suggestions(runtimes, program_folder: Path | None = None, key: str = "") -> list[Path]:
    """The runtime's own earlier model folder (still there), then model folders already in use (the
    shared one first), then one in the program folder."""
    roots = [_models_root(Path(os.path.expanduser(rt.models_dir))) for rt in runtimes if rt.models_dir]
    roots.sort(key=lambda r: (-roots.count(r), str(r).startswith(str(HOME) + "/.")))
    own = []
    if key:
        kept = ai_core.setting("removed_models_folders")
        before = kept.get(key) if isinstance(kept, dict) else None
        if isinstance(before, str) and before.startswith("/"):
            own.append(Path(before))
        # Where Reorganize (model_layout) puts the runtime's models in the shared root.
        sub = {"ollama": "ollama", "llamacpp": "llama.cpp", "koboldcpp": "llama.cpp"}.get(key)
        if sub:
            own += [r / sub for r in dict.fromkeys(roots)]
    found = [p for p in own if p.is_dir() and not p.is_symlink()] + roots
    if program_folder is not None:
        found.append(program_folder / "models")
    return list(dict.fromkeys(found))
