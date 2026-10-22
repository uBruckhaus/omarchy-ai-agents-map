"""One models folder for every local LLM, and "Reorganize models" (a button
in Settings, with a preview first): every GGUF the local LLMs know goes to

    <root>/<publisher>/<repo>/<file>.gguf    (LM Studio's own layout)
    <root>/local/<name>/<file>.gguf          (a file without a publisher)
    <root>/ollama/                           (Ollama's blobs and manifests)
    <root>/llama.cpp/                        (links to every GGUF: llama.cpp and KoboldCpp
                                              installed here read this flat folder)

and each local LLM is pointed there ("back reflection"): LM Studio's
downloadsFolder, and the model folder of the ones AI Agents Map installed.
A local LLM installed by hand keeps its folder: its links are pointed at the
new places, and a file moved out of its folder leaves a link behind.

Moves on one disk are renames (instant, open files keep working); to another
disk they are copies with progress, the original removed once the copy has
its size. Every step is written to a journal first, so Undo puts it all back.
Pure planning (plan) and doing (apply, undo); the popup shows the plan.
"""

import json
import os
import re
import shutil
import stat
from dataclasses import dataclass, field, asdict
from pathlib import Path

import ai_core

HOME = Path.home()
JOURNAL = HOME / ".local/state/ai-agents-map/reorganize.json"
FLAT = "llama.cpp"
LOCAL = "local"
MAX_DEPTH = 5
MAX_FILES = 5000
SPLIT_RE = re.compile(r"-\d{5}-of-\d{5}$", re.IGNORECASE)
_NAME_RE = re.compile(r"[^A-Za-z0-9._ -]+")
_SAFE_ROOT = re.compile(r"[^\x00-\x1f\x7f%\"'\\$`]{1,1024}")   # it ends up in local LLMs' settings and unit files
CHUNK = 8 * 1024 * 1024


@dataclass
class Provider:
    """A local LLM as the plan sees it. layout and store come from its engine when not given:
    "flat" GGUF files (llama.cpp, KoboldCpp), "publisher" folders (LM Studio), "blobs" (a store
    of its own, moved whole: Ollama, its parts in `store`)."""
    key: str
    name: str
    folder: Path | None      # its model folder now
    ours: bool               # installed by AI Agents Map: its folder setting can be changed
    running: bool = False
    layout: str = ""
    store: tuple = ()

    def __post_init__(self):
        if not self.layout:
            import llm_engine
            self.layout = llm_engine.layout(self.key)
            self.store = self.store or tuple(llm_engine.of(self.key).store_parts)


@dataclass
class Step:
    kind: str                # "move", "link" (old place -> new), "relink", "folder" (a provider's setting), "note"
    source: str = ""
    target: str = ""
    size: int = 0
    provider: str = ""
    old: str = ""            # "relink": the link's old target; "folder": the old setting
    copy: bool = False       # "move" to another disk
    ident: str = ""          # "move": "<st_dev>:<st_ino>" of the source when the preview was made


@dataclass
class Plan:
    root: Path
    steps: list = field(default_factory=list)
    problems: list = field(default_factory=list)     # (text) the plan cannot be applied while there are any
    copy_bytes: int = 0
    free: int = 0

    def count(self, kind: str) -> int:
        return sum(1 for s in self.steps if s.kind == kind)

    @property
    def empty(self) -> bool:
        return not any(s.kind != "note" for s in self.steps)


def device(path: Path) -> int:
    """st_dev of the path or of its nearest existing parent."""
    for candidate in (path, *path.parents):
        try:
            return candidate.stat().st_dev
        except OSError:
            continue
    return -1


def free_space(path: Path) -> int:
    for candidate in (path, *path.parents):
        try:
            return shutil.disk_usage(candidate).free
        except OSError:
            continue
    return 0


def model_dir_name(file_name: str) -> str:
    """local/<this> for a loose file: its name without .gguf and a split suffix."""
    stem = file_name[:-5] if file_name.lower().endswith(".gguf") else file_name
    stem = SPLIT_RE.sub("", stem)
    return (_NAME_RE.sub("-", stem).strip(" .-") or "model")[:120]


def walk_gguf(folder: Path, skip: set, depth: int = 0, found=None) -> tuple[list, list]:
    """(real .gguf files, symlinks to .gguf) under folder; skip = folders left out."""
    files, links = found if found is not None else ([], [])
    if depth > MAX_DEPTH or len(files) + len(links) > MAX_FILES:
        return files, links
    try:
        entries = sorted(folder.iterdir())
    except OSError:
        return files, links
    for entry in entries:
        if entry.name.startswith(".") or entry in skip:
            continue
        if entry.is_symlink():
            if entry.name.lower().endswith(".gguf"):
                links.append(entry)
            continue
        if entry.is_dir():
            walk_gguf(entry, skip, depth + 1, (files, links))
        elif entry.is_file() and entry.name.lower().endswith(".gguf"):
            files.append(entry)
    return files, links


def _own_file(path: Path) -> bool:
    """A regular file of this account: only those are moved (a link in a shared models
    folder may point at anyone's file, or at one of this account's outside it)."""
    try:
        st = os.stat(path, follow_symlinks=False)
    except OSError:
        return False
    return stat.S_ISREG(st.st_mode) and st.st_uid == os.getuid()


def canonical(file: Path, base: Path, root: Path) -> Path:
    """Where a GGUF belongs under root, from where it is under base (a model folder)."""
    try:
        dirs = file.parent.relative_to(base).parts
    except ValueError:
        dirs = (file.parent.name,) if file.parent != file.parent.parent else ()
    if len(dirs) >= 2:
        return root / dirs[-2] / dirs[-1] / file.name
    if len(dirs) == 1:
        return root / LOCAL / dirs[0] / file.name
    return root / LOCAL / model_dir_name(file.name) / file.name


def _ident(path: Path) -> str:
    """"<st_dev>:<st_ino>" of path itself (not where a link points); "" when it is gone."""
    try:
        st = os.stat(path, follow_symlinks=False)
    except OSError:
        return ""
    return f"{st.st_dev}:{st.st_ino}"


def _base_of(file: Path, bases: list) -> Path:
    """The deepest model folder holding the file (the root itself when none does)."""
    holding = [b for b in bases if file.is_relative_to(b)]
    return max(holding, key=lambda b: len(b.parts)) if holding else file.parent


def plan(root: Path, providers: list, keep_old_paths: bool = True) -> Plan:
    root = Path(os.path.realpath(os.path.expanduser(str(root))))
    result = Plan(root)
    if not root.is_absolute() or root == Path("/") or root == HOME or not _SAFE_ROOT.fullmatch(str(root)):
        result.problems.append("root")
        return result
    gguf_providers = [p for p in providers if p.layout in ("flat", "publisher") and p.folder]
    stores = [p for p in providers if p.layout == "blobs"]
    flat = root / FLAT

    # Folders left out of the walk: the stores (Ollama's), our flat link folder.
    skip = {flat, *(root / p.key for p in stores)}
    for p in stores:
        if p.folder:
            skip |= {Path(os.path.realpath(p.folder)) / name for name in p.store}
    bases = [Path(os.path.realpath(p.folder)) for p in gguf_providers] + [root]
    files, links = [], []
    for base in dict.fromkeys(bases):
        if base.is_dir():
            walk_gguf(base, skip, 0, (files, links))
    # Files the links point at (a link folder's models can live anywhere).
    for link in links:
        target = Path(os.path.realpath(link))
        if target.is_file() and target.name.lower().endswith(".gguf") and not target.is_symlink():
            files.append(target)
    files = [f for f in dict.fromkeys(Path(os.path.realpath(f)) for f in files) if _own_file(f)]

    moved = {}                                   # old real path -> new path
    targets = {}
    for file in sorted(files):
        if file.name.endswith(".part"):
            continue
        target = canonical(file, _base_of(file, bases), root)
        if target == file:
            continue
        if target in targets or (target.exists() and not target.is_symlink()):
            result.problems.append(f"conflict:{target}")
            continue
        targets[target] = file
        try:
            size = file.stat().st_size
        except OSError:
            continue
        copy = device(file) != device(root)
        result.steps.append(Step("move", str(file), str(target), size, copy=copy, ident=_ident(file)))
        if copy:
            result.copy_bytes += size
        moved[file] = target

    # Links that pointed at a moved file follow it (a hand-installed llama.cpp's router folder, …).
    for link in dict.fromkeys(links):
        if link.parent == flat:
            continue
        real = Path(os.path.realpath(link))
        if real in moved:
            result.steps.append(Step("relink", str(link), str(moved[real]), old=os.readlink(link)))

    # The old place keeps working: always inside a hand-installed LLM's own folder, else when asked.
    fixed = [Path(os.path.realpath(p.folder)) for p in gguf_providers if not p.ours]
    for old, new in moved.items():
        if keep_old_paths or any(old.parent == f for f in fixed):
            result.steps.append(Step("link", str(old), str(new)))

    # A store of its own (Ollama): whole into <root>/<key>, when we can point it there.
    for store in stores:
        if not store.folder:
            continue
        current, store_dir = Path(os.path.realpath(store.folder)), root / store.key
        if current == store_dir:
            continue
        if store.ours:
            for name in store.store:
                item = current / name
                if item.is_dir() and not item.is_symlink():
                    size = _tree_size(item)
                    copy = device(item) != device(root)
                    result.steps.append(Step("move", str(item), str(store_dir / name), size, copy=copy,
                                             ident=_ident(item)))
                    if copy:
                        result.copy_bytes += size
            result.steps.append(Step("folder", provider=store.key, target=str(store_dir), old=str(store.folder)))
        else:
            result.steps.append(Step("note", provider=store.key, source=str(store.folder)))

    # The GGUF local LLMs.
    final = sorted({*(moved.values()), *(f for f in files if f not in moved)})
    models = [f for f in final if "mmproj" not in f.name.lower() and f.is_relative_to(root)]
    for p in gguf_providers:
        current = Path(os.path.realpath(p.folder))
        if p.layout == "publisher":
            if current != root:
                result.steps.append(Step("folder", provider=p.key, target=str(root), old=str(p.folder)))
                if p.running:
                    result.problems.append(f"running:{p.name}")      # it writes its settings when it closes
        elif p.ours and current != flat:
            result.steps.append(Step("folder", provider=p.key, target=str(flat), old=str(p.folder)))
        elif not p.ours:
            result.steps.append(Step("note", provider=p.key, source=str(p.folder)))
    if any(s.kind == "folder" and s.target == str(flat) for s in result.steps) or flat.is_dir():
        have = {Path(os.path.realpath(l)) for l in flat.glob("*.gguf")} if flat.is_dir() else set()
        for model in models:
            link = flat / model.name
            if model not in have and not link.exists() and not link.is_symlink():
                result.steps.append(Step("link", str(link), str(model)))

    result.free = free_space(root)
    if result.copy_bytes and result.copy_bytes > result.free - 1024 ** 3:
        result.problems.append("space")
    return result


def _tree_size(folder: Path) -> int:
    total = 0
    for dirpath, _dirs, names in os.walk(folder):
        for name in names:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                pass
    return total


# --- doing it ---------------------------------------------------------------------------------

class Cancelled(Exception):
    pass


MAX_JOURNAL = 8 * 1024 * 1024
_STEP_KINDS = ("move", "link", "relink", "folder", "note")


def _save_journal(done: list, journal: Path):
    ai_core.atomic_write(journal, json.dumps({"version": 1, "steps": [asdict(s) for s in done]}, indent=1))


def _step(item) -> Step | None:
    """A journal entry, only with the fields and types a Step has."""
    if not isinstance(item, dict) or set(item) - set(Step.__dataclass_fields__) or item.get("kind") not in _STEP_KINDS:
        return None
    strings = ("kind", "source", "target", "provider", "old", "ident")
    if not all(isinstance(item.get(k, ""), str) for k in strings) or not isinstance(item.get("size", 0), int) \
            or not isinstance(item.get("copy", False), bool):
        return None
    return Step(**item)


def load_journal(journal: Path = JOURNAL) -> list:
    """The steps of the last Reorganize (its own 0600 file); [] when it is missing or not plain."""
    try:
        fd = os.open(journal, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return []
    try:
        with os.fdopen(fd, "rb") as handle:
            st = os.fstat(handle.fileno())
            if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
                return []
            data = json.loads(handle.read(MAX_JOURNAL + 1)[:MAX_JOURNAL] or b"{}")
    except (OSError, ValueError):
        return []
    steps = data.get("steps") if isinstance(data, dict) else None
    found = [_step(s) for s in steps] if isinstance(steps, list) else []
    return [] if None in found else found


def _open_own(name, dir_fd=None):
    """(fd, stat) of a file or folder of this account, opened once and never through a link (a
    name inside a folder: relative to that folder's own descriptor). Everything after reads this fd,
    so a name swapped for a link or another file after the preview is not followed."""
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    try:
        fd = os.open(name, flags, dir_fd=dir_fd)
    except OSError as error:
        raise PermissionError(f"{Path(name).name} is not this account's own file") from error
    st = os.fstat(fd)
    if st.st_uid != os.getuid() or not (stat.S_ISREG(st.st_mode) or stat.S_ISDIR(st.st_mode)):
        os.close(fd)
        raise PermissionError(f"{Path(name).name} is not this account's own file")
    return fd, st


def _copy(source: Path, target: Path, progress, cancelled, done_bytes: int, total: int,
          dir_fd=None, expect=None) -> int:
    """Copy source (a file or a folder) to target, reading only what _open_own opened. expect:
    (st_dev, st_ino) the source must still have."""
    fd, st = _open_own(source.name if dir_fd is not None else source, dir_fd)
    try:
        if expect is not None and (st.st_dev, st.st_ino) != expect:
            raise PermissionError(f"{source.name} changed after the preview")
        if stat.S_ISDIR(st.st_mode):
            copied = 0
            target.mkdir(parents=True, exist_ok=True)
            for name in sorted(os.listdir(fd)):
                if stat.S_ISLNK(os.stat(name, dir_fd=fd, follow_symlinks=False).st_mode):
                    (target / name).symlink_to(os.readlink(name, dir_fd=fd))
                else:
                    copied += _copy(source / name, target / name, progress, cancelled, done_bytes + copied, total,
                                    dir_fd=fd)
            return copied
        return _copy_file(fd, st, source, target, progress, cancelled, done_bytes, total)
    finally:
        os.close(fd)


def _copy_file(src_fd: int, st, source: Path, target: Path, progress, cancelled, done_bytes: int, total: int) -> int:
    partial = target.with_name(target.name + ".part")
    if partial.is_symlink() or partial.exists():
        if partial.is_symlink() or not partial.is_file():
            raise FileExistsError(str(partial))
        partial.unlink()                          # our own left-over from a stopped copy
    copied = 0
    # Owner-only until it is complete; then the source's mode and times.
    fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    with os.fdopen(fd, "wb") as dst:
        while chunk := os.read(src_fd, CHUNK):
            if cancelled():
                dst.close()
                partial.unlink(missing_ok=True)
                raise Cancelled()
            dst.write(chunk)
            copied += len(chunk)
            progress((done_bytes + copied) / max(1, total), source.name)
        dst.flush()
        os.fsync(dst.fileno())
        if os.fstat(dst.fileno()).st_size != st.st_size:
            dst.close()
            partial.unlink(missing_ok=True)
            raise OSError(f"{source.name}: the copy is incomplete")
        os.utime(dst.fileno(), ns=(st.st_atime_ns, st.st_mtime_ns))
        os.fchmod(dst.fileno(), stat.S_IMODE(st.st_mode) & 0o777)
    os.replace(partial, target)
    return copied


def _open_parent(path: Path) -> int:
    """fd of path's folder, walked from / one name at a time and never through a link. The plan
    holds real paths (realpath), so a link anywhere on the way was put there after the preview."""
    if not path.is_absolute() or path.name in ("", ".", ".."):
        raise PermissionError(f"{path} is not a planned path")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for name in path.parent.parts[1:]:
            next_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            os.close(fd)
            fd = next_fd
    except OSError as error:
        os.close(fd)
        raise PermissionError(f"{path.name}: a folder on its way changed after the preview") from error
    return fd


def _expected(ident: str):
    """(st_dev, st_ino) from a Step's ident, or None."""
    dev, _, ino = ident.partition(":")
    return (int(dev), int(ino)) if dev.isdigit() and ino.isdigit() else None


def _move(source: Path, target: Path, copy: bool, progress, cancelled, done_bytes: int, total: int,
          expect=None) -> int:
    """Move source (reached only through _open_parent's folder descriptor) to target. expect:
    (st_dev, st_ino) it had in the preview; it must still be that file."""
    if target.exists() or target.is_symlink():
        raise FileExistsError(str(target))
    parent = _open_parent(source)
    try:
        try:
            st = os.stat(source.name, dir_fd=parent, follow_symlinks=False)
        except OSError as error:
            raise PermissionError(f"{source.name} is not this account's own file") from error
        before = (st.st_dev, st.st_ino)
        if stat.S_ISLNK(st.st_mode) or st.st_uid != os.getuid():
            raise PermissionError(f"{source.name} is not this account's own file")
        if expect is not None and before != expect:
            raise PermissionError(f"{source.name} changed after the preview")
        target.parent.mkdir(parents=True, exist_ok=True)
        if not copy:
            os.rename(source.name, target, src_dir_fd=parent)
            return 0
        copied = _copy(source, target, progress, cancelled, done_bytes, total, dir_fd=parent, expect=before)
        try:
            after = os.stat(source.name, dir_fd=parent, follow_symlinks=False)
        except OSError:
            after = None
        if after is None or (after.st_dev, after.st_ino) != before:   # swapped while copied: the copy goes, the name stays
            shutil.rmtree(target) if target.is_dir() and not target.is_symlink() else target.unlink(missing_ok=True)
            raise PermissionError(f"{source.name} changed while it was copied")
        if stat.S_ISDIR(after.st_mode):
            shutil.rmtree(source.name, dir_fd=parent)
        else:
            os.unlink(source.name, dir_fd=parent)
        return copied
    finally:
        os.close(parent)


def _relink(link: Path, target: str):
    tmp = link.with_name(f".{link.name}.relink")
    tmp.unlink(missing_ok=True)
    tmp.symlink_to(target)
    os.replace(tmp, link)


def apply(plan_: Plan, set_folder, progress=lambda f, t: None, cancelled=lambda: False,
          journal: Path = JOURNAL) -> list:
    """Do the plan: moves first, then links, then the providers' folders
    (set_folder(provider key, Path)). Returns the steps done (also in the journal)."""
    if plan_.problems:
        raise ValueError("the plan has problems")
    done = []
    total = max(1, plan_.copy_bytes)
    copied = 0
    order = {"move": 0, "relink": 1, "link": 2, "folder": 3}
    try:
        for step in sorted((s for s in plan_.steps if s.kind in order), key=lambda s: order[s.kind]):
            if cancelled():
                raise Cancelled()
            if step.kind == "move":
                progress(copied / total if plan_.copy_bytes else len(done) / max(1, len(plan_.steps)),
                         Path(step.source).name)
                copied += _move(Path(step.source), Path(step.target), step.copy, progress, cancelled, copied, total,
                                expect=_expected(step.ident))
            elif step.kind == "relink":
                _relink(Path(step.source), step.target)
            elif step.kind == "link":
                link = Path(step.source)
                if link.exists() or link.is_symlink():
                    continue
                link.parent.mkdir(parents=True, exist_ok=True)
                link.symlink_to(step.target)
            elif step.kind == "folder":
                Path(step.target).mkdir(parents=True, exist_ok=True)
                set_folder(step.provider, Path(step.target))
            done.append(step)
            _save_journal(done, journal)
    finally:
        if done:
            _save_journal(done, journal)
    return done


def undo(set_folder, journal: Path = JOURNAL, progress=lambda f, t: None):
    """Everything in the journal back, last step first; the journal goes when it worked."""
    steps = load_journal(journal)
    problems = []
    for index, step in enumerate(reversed(steps)):
        progress(index / max(1, len(steps)), Path(step.source or step.target).name)
        try:
            if step.kind == "folder":
                set_folder(step.provider, Path(step.old))
            elif step.kind == "link":
                link = Path(step.source)
                if link.is_symlink():
                    link.unlink()
            elif step.kind == "relink":
                _relink(Path(step.source), step.old)
            elif step.kind == "move":
                source, target = Path(step.source), Path(step.target)
                if source.is_symlink():
                    source.unlink()
                _move(target, source, step.copy, progress, lambda: False, 0, max(1, step.size))
                _prune(target.parent, stop=3)
        except OSError as error:
            problems.append(f"{step.kind} {step.source}: {error}")
    if not problems:
        journal.unlink(missing_ok=True)
    return problems


def _prune(folder: Path, stop: int):
    """Remove folders the undo left empty (a few levels up)."""
    for _ in range(stop):
        try:
            folder.rmdir()
        except OSError:
            return
        folder = folder.parent
