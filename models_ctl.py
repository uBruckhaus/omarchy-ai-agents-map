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
    updated: float = 0.0         # last change (epoch seconds), 0 unknown
    pipeline: str = ""           # Hugging Face's task: text-generation, image-text-to-text, ...
    params: int = 0              # parameters (all of them, for a mixture of experts)
    vision: bool = False
    tools: bool = False
    reasoning: bool = False
    mtp: bool = False

    @property
    def author(self) -> str:
        return self.id.split("/", 1)[0]

    @property
    def name(self) -> str:
        return self.id.split("/", 1)[-1]


@dataclass
class ModelInfo(Repo):
    """One model's page: Repo's numbers and what its card and GGUF metadata say."""
    license: str = ""
    base_model: str = ""
    architecture: str = ""
    context: int = 0             # tokens it was trained for
    gated: bool = False          # needs accepting terms on Hugging Face first
    mmproj: bool = False         # has a vision projector file


@dataclass
class GgufFile:
    name: str                # shown name (one entry for all parts of a split file)
    paths: list              # file paths in the repo
    size: int
    quant: str = ""
    fits: bool = False       # fits into the graphics card's memory with room for context
    recommended: bool = False


# --- Hugging Face ---------------------------------------------------------------------------

CHAT_TASKS = {"text-generation", "image-text-to-text", "", None}     # not image, audio or embedding models
EXPAND = "".join(f"&expand[]={k}" for k in ("downloads", "likes", "lastModified", "pipeline_tag", "tags", "gguf"))
MTP_RE = re.compile(r"(^|[-_.])MTP([-_.]|$)", re.IGNORECASE)


def _count(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value < 10 ** 15 else 0


def _epoch(value) -> float:
    from datetime import datetime
    if not isinstance(value, str) or len(value) > 40:
        return 0.0
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def capabilities(repo_id: str, tags, pipeline, template) -> tuple[bool, bool, bool, bool]:
    """(vision, tools, reasoning, mtp) from the task, the tags and the chat template (only looked
    for words in, never run)."""
    tags = [t for t in tags if isinstance(t, str)] if isinstance(tags, list) else []
    template = template if isinstance(template, str) else ""
    vision = pipeline == "image-text-to-text" or "vision" in tags
    tools = "tools" in template or "tool_call" in template
    reasoning = any(w in template for w in ("<think>", "enable_thinking", "reasoning_content", "<|channel|>"))
    mtp = bool(MTP_RE.search(repo_id.split("/", 1)[-1])) or "mtp" in tags
    return vision, tools, reasoning, mtp


def _repo_from(item) -> Repo | None:
    repo = item.get("id") if isinstance(item, dict) else None
    if not isinstance(repo, str) or not REPO_RE.fullmatch(repo):
        return None
    gguf = item.get("gguf") if isinstance(item.get("gguf"), dict) else {}
    pipeline = item.get("pipeline_tag") if isinstance(item.get("pipeline_tag"), str) else ""
    vision, tools, reasoning, mtp = capabilities(repo, item.get("tags"), pipeline, gguf.get("chat_template"))
    return Repo(repo, _count(item.get("downloads")), _count(item.get("likes")), _epoch(item.get("lastModified")),
                pipeline[:40], _count(gguf.get("total")), vision, tools, reasoning, mtp)


def search(query: str, limit: int = 20, base: str = HF) -> list[Repo]:
    """GGUF models for query, most downloaded first; with no query the trending ones (chat and
    vision models only). base: Hugging Face or a site with its API (web_sources)."""
    query = (query or "").strip()[:100]
    sort = "downloads" if query else "trendingScore"
    url = (f"{base}/api/models?filter=gguf&sort={sort}&direction=-1&limit={int(limit) * 2}{EXPAND}"
           + (f"&search={urllib.parse.quote(query)}" if query else ""))
    repos = []
    for item in _json_list(url)[:limit * 2]:
        repo = _repo_from(item)
        if repo is not None and repo.pipeline in CHAT_TASKS:
            repos.append(repo)
    return repos[:limit]


def details(repo: str, base: str = HF) -> ModelInfo:
    """What the model's page says: numbers, license, base model, architecture, context, capabilities."""
    if not REPO_RE.fullmatch(repo):
        raise ValueError("not a Hugging Face model name")
    import netfetch
    try:
        data = netfetch.fetch_json(f"{base}/api/models/{repo}", max_bytes=5_000_000, timeout=20)
    except netfetch.FetchError as error:
        raise RuntimeError(f"{urllib.parse.urlsplit(base).hostname} did not answer") from error
    if not isinstance(data, dict):
        raise RuntimeError("unexpected answer")
    found = _repo_from({**data, "id": repo})
    if found is None:
        raise RuntimeError("unexpected answer")
    card = data.get("cardData") if isinstance(data.get("cardData"), dict) else {}
    gguf = data.get("gguf") if isinstance(data.get("gguf"), dict) else {}
    base_model = card.get("base_model")
    base_model = base_model[0] if isinstance(base_model, list) and base_model else base_model
    siblings = data.get("siblings") if isinstance(data.get("siblings"), list) else []
    mmproj = any(isinstance(s, dict) and "mmproj" in str(s.get("rfilename", "")).lower() for s in siblings[:3000])
    info = ModelInfo(**found.__dict__)
    info.license = _plain(card.get("license"), 60)
    info.base_model = _plain(base_model, 120) if isinstance(base_model, str) and REPO_RE.fullmatch(base_model) else ""
    info.architecture = _plain(gguf.get("architecture"), 60)
    info.context = _count(gguf.get("context_length"))
    info.gated = bool(data.get("gated"))
    info.mmproj = mmproj
    info.vision = info.vision or mmproj
    return info


def _plain(value, limit: int) -> str:
    """A short value from a web answer as plain text (no control characters)."""
    if not isinstance(value, str):
        return ""
    return "".join(c for c in value if c.isprintable())[:limit]


MAX_README = 200_000


def readme(repo: str, base: str = HF, limit: int = 12_000) -> str:
    """The model card's text as plain text (front matter, HTML tags and images taken out), shortened."""
    if not REPO_RE.fullmatch(repo):
        raise ValueError("not a Hugging Face model name")
    import netfetch
    try:
        raw = netfetch.fetch(f"{base}/{repo}/raw/main/README.md", max_bytes=MAX_README, timeout=20)
    except netfetch.FetchError:
        return ""
    return plain_readme(raw.decode("utf-8", "replace"), limit)


def plain_readme(text: str, limit: int = 12_000) -> str:
    text = text.replace("\r\n", "\n")
    if text.startswith("---"):
        end = text.find("\n---", 3)
        text = text[end + 4:] if end > 0 else text
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)                 # images
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)             # links: their text
    text = re.sub(r"<[^>]{0,2000}>", "", text)                        # HTML tags
    text = "".join(c for c in text if c.isprintable() or c in "\n\t")
    lines = [line.strip() for line in text.split("\n")]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return text[:limit] + ("…" if len(text) > limit else "")


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


# --- does it fit ----------------------------------------------------------------------------

def system_ram() -> int:
    """This computer's memory in bytes (0 when unknown)."""
    try:
        with open("/proc/meminfo", "rb") as handle:
            text = handle.read(8192).decode("ascii", "replace")
    except OSError:
        return 0
    match = re.search(r"^MemTotal:\s+(\d+)\s+kB", text, re.MULTILINE)
    return int(match.group(1)) * 1024 if match else 0


def fit(size: int, vram: int, ram: int = 0) -> str:
    """"fits" (in the graphics card with room for context), "partly" (part of it in the
    computer's memory: works, slower) or "too_large"."""
    if vram and size <= vram * 0.8:
        return "fits"
    if size <= vram * 0.8 + ram * 0.5:
        return "partly"
    return "too_large"


# --- where a GGUF goes ----------------------------------------------------------------------

def gguf_target(runtime_key: str, models_dir: Path, repo: str) -> tuple[Path, Path | None]:
    """(folder to download into, folder to link it from or None).

    LM Studio keeps models as <folder>/<publisher>/<repo>/. A llama.cpp model
    folder that holds only links (a router folder, as llama-models-sync makes
    them) gets the file next to the files those links point at, and a link; when
    they point into several folders, into <their common folder>/<publisher>/<repo>/
    (LM Studio's layout). Never as a real file among the links: a tool that keeps
    such a folder (llama-models-sync) may take every name it does not know away."""
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
        common = Path(os.path.commonpath([str(t) for t in targets]))
        router = Path(os.path.realpath(models_dir))
        if len(common.parts) > 1 and common != router and router not in common.parents:
            publisher, name = repo.split("/", 1)
            return common / publisher / name, models_dir
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


# --- removing a model (to the trash) ----------------------------------------------------------

@dataclass
class Removal:
    paths: list              # what goes to the trash: files, or links in the model folder
    size: int                # bytes freed (0 for links)
    links_only: bool = False # only links go: the files stay where they are
    shared: list = None      # other local LLMs that read the same file


def _own_entry(path: Path, inside: Path) -> bool:
    """A file or link of this account directly inside the model folder tree (never through a link)."""
    try:
        st = os.lstat(path)
        rel = Path(os.path.abspath(path)).relative_to(Path(os.path.abspath(inside)))
    except (OSError, ValueError):
        return False
    return st.st_uid == os.getuid() and (os.path.islink(path) or os.path.isfile(path)) and ".." not in rel.parts


def removal(runtime_key: str, models_dir: Path, name: str, others: dict | None = None) -> Removal:
    """What removing model `name` from a local LLM moves to the trash: llama.cpp and KoboldCpp
    its .gguf (all parts of a split one) in the model folder, or only the link when it is one;
    LM Studio the model's files in its <publisher>/<model> folder, with the vision projector
    when nothing else is left there. others: {local LLM name: (its key, its model folder)} to warn when
    one of them reads the same file. Ollama removes its own models (ollama_delete)."""
    import context_ctl
    models_dir = Path(os.path.expanduser(str(models_dir)))
    if runtime_key == "lmstudio":
        file = context_ctl.lmstudio_model_file(context_ctl.lmstudio_entry(name))
        if file is None:
            raise ValueError("LM Studio does not list this model in its folder")
        folder = file.parent
        split = SPLIT_RE.match(file.name)
        paths = sorted(folder.glob(f"{split.group(1)}-*-of-{split.group(3)}.gguf")) if split else [file]
        rest = [p for p in folder.glob("*.gguf") if p not in paths and "mmproj" not in p.name.lower()]
        if not rest:
            paths += sorted(p for p in folder.glob("*.gguf") if "mmproj" in p.name.lower())
        root = models_dir
    elif runtime_key in ("llamacpp", "koboldcpp"):
        exact = models_dir / f"{name}.gguf"
        if exact.exists() or exact.is_symlink():
            paths = [exact]
        else:
            paths = sorted(p for p in models_dir.glob(f"{glob_escape(name)}-*-of-*.gguf") if SPLIT_RE.match(p.name))
        root = models_dir
    else:
        raise ValueError("this local LLM removes models itself")
    if not paths or not all(_own_entry(p, root) for p in paths):
        raise ValueError("the model's files are not this account's own files in its model folder")
    links_only = all(p.is_symlink() for p in paths)
    size = 0 if links_only else sum(p.stat().st_size for p in paths if not p.is_symlink())
    shared = []
    if not links_only:
        # Another local LLM loses the model only through a symbolic link to a file that goes;
        # a hard link (Ollama's blobs, an added model) keeps its own name for the same data.
        gone = {os.path.realpath(p) for p in paths if not p.is_symlink()}
        for other, (key, folder) in (others or {}).items():
            if folder and links_into(Path(os.path.expanduser(folder)), gone):
                shared.append(other)
    return Removal(paths, size, links_only, shared)


def links_into(folder: Path, targets: set, limit: int = 5000) -> bool:
    """Whether a .gguf link in folder (up to three levels down) points at one of targets."""
    for count, path in enumerate(p for pattern in ("*.gguf", "*/*.gguf", "*/*/*.gguf", "*/*/*/*.gguf")
                                 for p in folder.glob(pattern)):
        if count > limit:
            break
        if path.is_symlink() and os.path.realpath(path) in targets:
            return True
    return False


def glob_escape(text: str) -> str:
    return re.sub(r"([*?\[])", r"[\1]", text)


def trash(paths: list) -> None:
    """Move each to the desktop's trash (freedesktop.org, as the file manager does): restorable."""
    import gi
    gi.require_version("Gio", "2.0")
    from gi.repository import Gio, GLib
    for path in paths:
        try:
            Gio.File.new_for_path(str(path)).trash(None)
        except GLib.Error as error:
            raise RuntimeError(f"{Path(path).name} could not be moved to the trash: {error.message}") from error


def ollama_delete(name: str, address: str):
    """Ollama removes one of its models (its own store has no trash)."""
    if not OLLAMA_NAME_RE.fullmatch(name):
        raise ValueError("not an Ollama model name")
    port = int(address.rsplit(":", 1)[-1] or 11434)
    require_own_listener(port)
    import netfetch
    try:
        netfetch.fetch(f"http://127.0.0.1:{port}/api/delete", max_bytes=65536, timeout=60, local=True,
                       headers={"Content-Type": "application/json"},
                       data=json.dumps({"model": name}).encode(), method="DELETE")
    except netfetch.FetchError as error:
        raise RuntimeError(f"Ollama: {error}") from error


# --- Ollama --------------------------------------------------------------------------------

from loopback import loopback_listeners, require_own_listener  # noqa: E402,F401  (callers still ask models_ctl)


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
