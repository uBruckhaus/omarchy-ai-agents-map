"""Download models for the local LLMs.

- Hugging Face GGUF models for llama.cpp and LM Studio: search, pick a file
  (quantisation), download it into the runtime's model folder.
- Ollama: `pull` through its own server (library names like "qwen3:8b", or
  Hugging Face GGUFs as "hf.co/<repo>:<quant>"), with progress. Before
  anything is sent to 127.0.0.1, the listening socket must belong to this
  user, so another account cannot pose as the local server.

Blocking; the popup runs it on a thread with progress(fraction, text) and
cancelled().
"""

import json
import time
import os
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import install_ctl

HF = "https://huggingface.co"
REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}/[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
OLLAMA_NAME_RE = re.compile(r"^(?:hf\.co/)?[a-z0-9][a-z0-9._/-]{0,127}(?::[A-Za-z0-9._-]{1,64})?$")
FILE_RE = re.compile(r"^[A-Za-z0-9._/-]{1,255}\.gguf$")
SPLIT_RE = re.compile(r"^(.*)-(\d{5})-of-(\d{5})\.gguf$")
QUANT_RE = re.compile(r"(IQ\d_[A-Z0-9_]+|Q\d_[A-Z0-9_]+|Q\d_\d|BF16|F16|F32|MXFP4|NVFP4)", re.IGNORECASE)


@dataclass
class Repo:
    id: str
    downloads: int = 0
    likes: int = 0


@dataclass
class GgufFile:
    name: str                # shown name (one entry for all parts of a split file)
    paths: list              # file paths in the repo
    size: int
    quant: str = ""
    fits: bool = False       # fits into the graphics card's memory with room for context
    recommended: bool = False


# --- Hugging Face ---------------------------------------------------------------------------

def search(query: str, limit: int = 20, base: str = HF) -> list[Repo]:
    """base: Hugging Face or a site with its API (web_sources)."""
    query = (query or "").strip()[:100]
    if not query:
        return []
    url = (f"{base}/api/models?search={urllib.parse.quote(query)}&filter=gguf&sort=downloads&direction=-1"
           f"&limit={int(limit)}")
    data = _json_list(url)
    repos = []
    for item in data[:limit]:
        repo = item.get("id") if isinstance(item, dict) else None
        if isinstance(repo, str) and REPO_RE.fullmatch(repo):
            repos.append(Repo(repo, int(item.get("downloads") or 0), int(item.get("likes") or 0)))
    return repos


def files(repo: str, vram: int = 0, base: str = HF) -> list[GgufFile]:
    """The GGUF files of a repository, split files as one entry; the best one
    for the card marked recommended (Q4_K_M if it fits, else the largest that fits)."""
    if not REPO_RE.fullmatch(repo):
        raise ValueError("not a Hugging Face model name")
    data = _json_list(f"{base}/api/models/{repo}/tree/main?recursive=1")
    groups: dict[str, GgufFile] = {}
    for item in data[:2000]:
        if not isinstance(item, dict) or item.get("type") != "file":
            continue
        path = item.get("path", "")
        if not isinstance(path, str) or not FILE_RE.fullmatch(path) or ".." in path:
            continue
        if "mmproj" in Path(path).name.lower():
            continue                 # vision add-on, not a model of its own
        size = int((item.get("lfs") or {}).get("size") or item.get("size") or 0)
        split = SPLIT_RE.match(path)
        key = split.group(1) if split else path[:-5]
        entry = groups.setdefault(key, GgufFile(Path(key).name, [], 0))
        entry.paths.append(path)
        entry.size += size
    found = sorted(groups.values(), key=lambda f: f.size)
    for entry in found:
        entry.paths.sort()
        quant = QUANT_RE.search(entry.name)
        entry.quant = quant.group(1).upper() if quant else ""
        entry.fits = bool(vram) and entry.size < vram * 0.8
    fitting = [f for f in found if f.fits]
    best = next((f for f in fitting if f.quant == "Q4_K_M"), fitting[-1] if fitting else None)
    if best is not None:
        best.recommended = True
    return found


def _json_list(url: str) -> list:
    import netfetch
    try:
        data = netfetch.fetch_json(url, max_bytes=5_000_000, timeout=20)
    except netfetch.FetchError as error:
        raise RuntimeError(f"{urllib.parse.urlsplit(url).hostname} did not answer") from error
    if not isinstance(data, list):
        raise RuntimeError(f"unexpected answer from {urllib.parse.urlsplit(url).hostname}")
    return data


# --- where a GGUF goes ----------------------------------------------------------------------

def gguf_target(runtime_key: str, models_dir: Path, repo: str) -> tuple[Path, Path | None]:
    """(folder to download into, folder to link it from or None).

    LM Studio keeps models as <folder>/<publisher>/<repo>/. A llama.cpp model
    folder that holds only links (a router folder, as llama-models-sync makes
    them) gets the file next to the files those links point at, and a link."""
    if runtime_key == "lmstudio":
        publisher, name = repo.split("/", 1)
        return models_dir / publisher / name, None
    try:
        entries = [p for p in models_dir.iterdir() if p.name.lower().endswith(".gguf")]
    except OSError:
        entries = []
    if entries and all(p.is_symlink() for p in entries):
        targets = {Path(os.path.realpath(p)).parent for p in entries}
        if len(targets) == 1:
            return targets.pop(), models_dir
    return models_dir, None


def download_gguf(runtime_key: str, models_dir: Path, repo: str, entry: GgufFile, progress, cancelled,
                  base: str = HF) -> Path:
    folder, link_dir = gguf_target(runtime_key, models_dir, repo)
    folder.mkdir(parents=True, exist_ok=True)
    done = 0
    first = None
    sizes = _part_sizes(repo, entry, base)
    for path, size in zip(entry.paths, sizes):
        target = folder / Path(path).name
        first = first or target
        # Downloaded before: only an own regular file of that size counts (not a link someone put there).
        if not (install_ctl.own_regular_file(target) and target.stat().st_size == size):
            install_ctl.download(f"{base}/{repo}/resolve/main/{urllib.parse.quote(path)}", target, size,
                                 progress, cancelled, done, entry.size)
        done += size
    if link_dir is not None:
        for path in entry.paths:
            link = link_dir / Path(path).name
            if not link.exists():
                link.symlink_to(folder / Path(path).name)
    return first


def _part_sizes(repo: str, entry: GgufFile, base: str = HF) -> list[int]:
    """Sizes of the parts of a split file (one total size is known; ask again for the parts)."""
    if len(entry.paths) == 1:
        return [entry.size]
    data = _json_list(f"{base}/api/models/{repo}/tree/main?recursive=1")
    sizes = {i.get("path"): int((i.get("lfs") or {}).get("size") or i.get("size") or 0)
             for i in data if isinstance(i, dict)}
    return [sizes.get(p, 0) for p in entry.paths]


# --- On this PC: GGUF files a local LLM does not have yet ----------------------------------

@dataclass
class LocalFile:
    path: Path
    size: int
    where: str               # the folder it was found in, for the list


def _identity(path: Path):
    try:
        st = path.stat()
    except OSError:
        return None
    return st.st_dev, st.st_ino


def seen_by(runtime_key: str, folder: Path) -> set:
    """The files a local LLM reads from its folder (as (device, inode)): LM Studio
    those at least two folders down (<publisher>/<model>/…), Ollama its blobs
    (hard links count), llama.cpp and KoboldCpp the folder and one level below."""
    folder = Path(folder)
    if runtime_key == "lmstudio":
        paths = folder.glob("*/*/**/*.gguf")
    elif runtime_key == "ollama":
        paths = (folder / "blobs").glob("sha256-*")
    else:
        paths = [*folder.glob("*.gguf"), *folder.glob("*/*.gguf")]
    found = set()
    for count, path in enumerate(paths):
        if count > 5000:
            break
        found.add(_identity(path))
    return found - {None}


def local_ggufs(roots: list, have: set) -> list[LocalFile]:
    """GGUF models under roots (real files and what links point at) that the local
    LLM does not read yet (have = seen_by(...): same file = same inode, so hard
    links and symlinks count as had). No vision projectors, a split model by its
    first part."""
    import model_layout
    had = set(have)
    found, seen = [], set()
    for root in roots:
        root = Path(root)
        if not root.is_dir():
            continue
        files, links = model_layout.walk_gguf(root, set())
        for path in files + [Path(os.path.realpath(l)) for l in links]:
            name = path.name.lower()
            ident = _identity(path)
            if ident is None or ident in had or ident in seen or "mmproj" in name or not path.is_file():
                continue
            split = SPLIT_RE.match(path.name)
            if split and split.group(2) != "00001":
                continue
            seen.add(ident)
            found.append(LocalFile(Path(os.path.realpath(path)), path.stat().st_size, str(root)))
    return sorted(found, key=lambda f: f.path.name.lower())


def add_local(runtime_key: str, models_dir: Path, file: Path) -> Path:
    """Make a GGUF on this PC a model of a local LLM without a second copy: LM
    Studio gets it as <folder>/local/<name>/ (with its vision projector), the
    others a link in their folder. A hard link on the same disk (every program
    reads it), else a symbolic one."""
    import model_layout
    file = Path(os.path.realpath(file))
    if not file.is_file() or not file.name.lower().endswith(".gguf"):
        raise ValueError("not a GGUF file")
    parts = [file]
    split = SPLIT_RE.match(file.name)
    if split:
        parts = sorted(file.parent.glob(f"{split.group(1)}-*-of-{split.group(3)}.gguf"))
    if runtime_key == "lmstudio":
        folder = models_dir / model_layout.LOCAL / model_layout.model_dir_name(file.name)
        parts += [p for p in file.parent.glob("*.gguf") if "mmproj" in p.name.lower()]
    else:
        folder, link_dir = gguf_target(runtime_key, models_dir, "local/x")
        folder = link_dir or folder
    folder.mkdir(parents=True, exist_ok=True)
    for part in parts:
        target = folder / part.name
        if target.exists() or target.is_symlink():
            continue
        try:
            os.link(part, target)
        except OSError:
            target.symlink_to(part)
    return folder / file.name


# --- Ollama --------------------------------------------------------------------------------

def loopback_listeners(port: int, proc: str = "/proc/net") -> list[int]:
    """UIDs of the sockets listening on a loopback-reachable address on this
    TCP port, from /proc/net/tcp{,6} (which shows every socket's owner)."""
    owners = []
    for name in ("tcp", "tcp6"):
        try:
            lines = Path(proc, name).read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            if len(fields) < 8 or fields[3] != "0A":     # 0A = LISTEN
                continue
            address, _sep, hex_port = fields[1].partition(":")
            if int(hex_port, 16) != port:
                continue
            if name == "tcp":
                reachable = address == "00000000" or address.endswith("7F")
            else:
                reachable = address in ("0" * 32, "0" * 24 + "01000000") or (
                    address.startswith("0" * 16 + "FFFF0000") and address.endswith("7F"))
            if reachable:
                owners.append(int(fields[7]))
    return owners


def require_own_listener(port: int):
    owners = loopback_listeners(port)
    if not owners:
        raise ConnectionError("not running")
    if any(uid != os.getuid() for uid in owners):
        raise PermissionError(f"port {port} belongs to another user account")


def ollama_create(file: Path, address: str) -> str:
    """A GGUF on this PC as an Ollama model (`ollama create`; Ollama keeps its own
    copy in its store). The name: the file's, lower case."""
    import shutil
    import subprocess
    import tempfile
    import ai_core
    file = Path(os.path.realpath(file))
    if not file.is_file() or not file.name.lower().endswith(".gguf"):
        raise ValueError("not a GGUF file")
    # The path goes into the Modelfile's FROM line: nothing that could end the quoted value or the line.
    if re.search(r'["\\\x00-\x1f\x7f]', str(file)):
        raise ValueError("the file's path has quotes, backslashes or control characters; rename it first")
    name = re.sub(r"[^a-z0-9._-]+", "-", SPLIT_RE.sub(r"\1", file.name).lower().removesuffix(".gguf")).strip("-._")
    name = (name or "model")[:100]
    host, _sep, port = address.rpartition(":")
    require_own_listener(int(port or 11434))
    binary = shutil.which("ollama")
    if binary is None:
        raise RuntimeError("ollama is not installed")
    with tempfile.TemporaryDirectory(prefix="ai-agents-map-") as tmp:
        modelfile = Path(tmp) / "Modelfile"
        modelfile.write_text(f"FROM {json.dumps(str(file))}\n", encoding="utf-8")
        out, ok = ai_core.read_bounded_output([binary, "create", name, "-f", str(modelfile)], max_bytes=64 * 1024,
                                              timeout=3600, merge_stderr=True,
                                              env={**os.environ, "OLLAMA_HOST": f"127.0.0.1:{int(port or 11434)}"})
    if not ok:
        raise RuntimeError(out.decode("utf-8", "replace").strip()[-300:] or "ollama create failed")
    return name


def ollama_name_for(repo: str, entry: GgufFile) -> str:
    """How Ollama pulls a Hugging Face GGUF: hf.co/<repo>:<quant>."""
    return f"hf.co/{repo}" + (f":{entry.quant}" if entry.quant else "")


PULL_DEADLINE = 12 * 3600


def ollama_pull(name: str, address: str, progress, cancelled):
    name = (name or "").strip()
    if not OLLAMA_NAME_RE.fullmatch(name) or ".." in name:
        raise ValueError("not a model name (e.g. qwen3:8b or hf.co/<user>/<repo>:Q4_K_M)")
    host, _sep, port = address.rpartition(":")
    port = int(port or 11434)
    require_own_listener(port)
    request = urllib.request.Request(f"http://127.0.0.1:{port}/api/pull",
                                     data=json.dumps({"model": name, "stream": True}).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=60) as response:
        # Our own Ollama (require_own_listener): still one line at most 64 KiB, at most 200000 lines,
        # and one overall deadline (a large model can take hours, not forever).
        deadline = time.monotonic() + PULL_DEADLINE
        for _count in range(200000):
            if time.monotonic() > deadline:
                raise RuntimeError("the download took too long")
            raw = response.readline(65537)
            if not raw:
                break
            if cancelled():
                raise install_ctl.Cancelled()
            if len(raw) > 65536:
                while raw and not raw.endswith(b"\n"):
                    raw = response.readline(65537)
                continue
            try:
                line = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(line, dict):
                continue
            if line.get("error"):
                raise RuntimeError(str(line["error"])[:300])
            total, completed = line.get("total"), line.get("completed")
            fraction = completed / total if isinstance(total, int) and total > 0 and isinstance(completed, int) else None
            progress(fraction if fraction is not None else 0.0, str(line.get("status", ""))[:120])
            if line.get("status") == "success":
                return
    raise install_ctl.DownloadError("the download stopped before it was finished")
