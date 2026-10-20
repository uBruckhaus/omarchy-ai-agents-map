"""Model folders on Setup: one card per folder that really holds GGUF model files, with the local
LLMs that read it, plus the folders added by hand (Add folder; setting model_folders), even empty.

A local LLM reads a folder when the folder is its model folder or inside it (LM Studio:
<publisher>/<model>/), or when its model folder holds links to files there (a llama.cpp router
folder, as llama-models-sync makes it: that folder of links is no card of its own).

Change folder moves a folder's model files (its own regular *.gguf files, vision files included)
into another folder with model_move (a hard link on the same disk, a checked copy to another one;
never over an existing name; Undo moves them back), then fixes what pointed at them:

- links in a local LLM's model folder that led to a moved file now lead to its new place;
- paths in llama.cpp's models.ini (mmproj = …) name the new place (a copy is kept first);
- a local LLM whose model folder this was, and whose folder setting AI Agents Map keeps
  (installed here), is pointed at the new folder (and restarted when it runs).

What nothing points at afterwards (LM Studio reading only below its own folder) is said in the
preview, before anything moves. Blocking (files): call it off the main thread.
"""

from __future__ import annotations

import errno
import os
import re
import secrets
import shutil
import stat
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

import model_move

GGUF_RUNTIMES = ("llamacpp", "lmstudio", "koboldcpp")      # the local LLMs that read GGUF folders
MAX_DEPTH = 3                 # folders looked into below a model folder
MAX_FILES = 20_000
SPLIT = re.compile(r"-\d{5}-of-\d{5}$")
HOME = Path.home()
MODELS_INI = HOME / ".config/llama.cpp/models.ini"
BACKUPS = HOME / ".local/state/ai-agents-map/backups"


@dataclass
class Folder:
    path: Path                                    # the real folder
    models: list = field(default_factory=list)    # model names (split files once, no vision files)
    files: list = field(default_factory=list)     # its own *.gguf files (vision files included)
    readers: list = field(default_factory=list)   # runtime keys that read it
    added: bool = False                           # added by hand (Add folder)


def real(path) -> Path:
    return Path(os.path.realpath(os.path.expanduser(str(path))))


def _model_name(file: Path) -> str:
    return SPLIT.sub("", file.name[:-5])


def _ggufs(folder: Path, depth: int = MAX_DEPTH):
    """(path as found, its real file) of every .gguf below folder, links followed to their files."""
    found = 0
    base = len(folder.parts)
    for root, dirs, names in os.walk(folder, followlinks=False):
        if len(Path(root).parts) - base >= depth:
            dirs[:] = []
        for name in names:
            if not name.lower().endswith(".gguf"):
                continue
            path = Path(root, name)
            target = real(path)
            if target.is_file():
                yield path, target
            found += 1
            if found >= MAX_FILES:
                return


def discover(runtimes, added=()) -> list[Folder]:
    """The folders holding model files of the GGUF local LLMs, and the ones added by hand."""
    folders: dict[Path, Folder] = {}

    def folder_of(path: Path) -> Folder:
        return folders.setdefault(path, Folder(path))

    for rt in runtimes:
        if rt.key not in GGUF_RUNTIMES or not getattr(rt, "installed", True) or not rt.models_dir:
            continue
        root = real(rt.models_dir)
        if not root.is_dir():
            continue
        for path, target in _ggufs(root):
            if path.is_symlink() and path.parent != root:
                continue                       # another LLM's router folder inside this one's
            entry = folder_of(target.parent)
            if rt.key not in entry.readers:
                entry.readers.append(rt.key)
    for path in added:
        path = real(path)
        if path.is_dir():
            folder_of(path).added = True
            for _found, target in _ggufs(path, depth=1):
                folder_of(target.parent)
    for entry in folders.values():
        try:
            own = sorted(p for p in entry.path.iterdir() if p.name.lower().endswith(".gguf")
                         and not p.is_symlink() and p.is_file())
        except OSError:
            own = []
        entry.files = own
        entry.models = sorted({_model_name(p) for p in own if "mmproj" not in p.name.lower()}, key=str.lower)
    # A folder of links only (a router folder) is no card: its files are shown where they are.
    return sorted((f for f in folders.values() if f.files or f.added), key=lambda f: str(f.path).lower())


# --- Change folder ---------------------------------------------------------------------------------

@dataclass
class Change:
    move: model_move.Move
    links: list = field(default_factory=list)     # (link, old target, new target)
    ini: bool = False                             # models.ini names a moved file
    repoint: list = field(default_factory=list)   # runtime keys whose folder setting follows
    lost: list = field(default_factory=list)      # runtime keys that will not see the models any more


def _same_disk(a: Path, b: Path) -> bool:
    probe = b
    while not probe.exists():
        probe = probe.parent
    return os.stat(a).st_dev == os.stat(probe).st_dev


def plan(folder: Folder, new, runtimes, repointable=lambda key: False, ini: Path = MODELS_INI) -> Change:
    """What moving this folder's model files to `new` does. Raises ValueError when it cannot."""
    source, target = folder.path, Path(os.path.abspath(os.path.expanduser(str(new))))
    if real(target) == source:
        raise ValueError("that is this folder already")
    if real(target).is_relative_to(source):
        raise ValueError("the new folder is inside this one")
    if target.exists() and not target.is_dir():
        raise ValueError(f"{target} is not a folder")
    steps, size = [], 0
    for path in folder.files:
        st = os.stat(path, follow_symlinks=False)
        if st.st_uid != os.getuid():
            raise ValueError(f"{path.name} belongs to another account")
        dest = target / path.name
        if os.path.lexists(dest):
            raise ValueError(f"{dest} exists already")
        steps.append((path, dest))
        size += st.st_size
    if not steps:
        raise ValueError("this folder holds no model files of its own")
    move = model_move.Move(name=source.name, steps=steps, size=size, copy=not _same_disk(source, target),
                           root=target, back=source)
    change = Change(move)
    moved = {str(s): d for s, d in steps}
    for rt in runtimes:
        if rt.key not in GGUF_RUNTIMES or not rt.models_dir:
            continue
        root = real(rt.models_dir)
        relinked = False
        for path, now in _ggufs(root) if root.is_dir() else ():
            if path.is_symlink() and str(now) in moved:
                if not any(link == path for link, _o, _n in change.links):
                    change.links.append((path, now, moved[str(now)]))
                # Only a link right in its own folder is how it reads (a router folder); links deeper
                # (another LLM's router folder inside LM Studio's) are not.
                relinked = relinked or path.parent == root
        if rt.key not in folder.readers or relinked:
            continue                                  # not its files, or its links follow them
        if root == source:                            # its own model folder: its setting must follow
            (change.repoint if repointable(rt.key) else change.lost).append(rt.key)
        elif not real(target).is_relative_to(root):   # it reads only below its folder (LM Studio)
            change.lost.append(rt.key)
    try:
        change.ini = any(str(s) in ini.read_text(encoding="utf-8", errors="replace") for s in moved)
    except OSError:
        change.ini = False
    return change


def _relink(link: Path, to: Path):
    """The link now leads to `to` (a new link put in its place in one step). Only a link of this
    user's own: a models folder may be shared; the temporary name is random and only ever ours."""
    info = os.lstat(link)
    if not stat.S_ISLNK(info.st_mode) or info.st_uid != os.getuid():
        raise OSError(errno.EPERM, "not a link of your own")
    tmp = link.with_name(f".{link.name}.{secrets.token_hex(6)}.relink")
    os.symlink(to, tmp)                           # fails when the name exists: never someone else's
    try:
        os.replace(tmp, link)
    except OSError:
        os.unlink(tmp)
        raise


def _backup(path: Path) -> Path:
    folder = BACKUPS / time.strftime("%Y%m%d-%H%M%S")
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    shutil.copy2(path, folder / path.name)
    return folder / path.name


def fix(change: Change, ini: Path = MODELS_INI, back: bool = False) -> list:
    """Links and models.ini to the new place (back=True: to the old one again). Returns problems."""
    problems = []
    for link, old, new in change.links:
        try:
            _relink(link, old if back else new)
        except OSError as error:
            problems.append(f"{link.name}: {error}")
    if change.ini:
        try:
            text = ini.read_text(encoding="utf-8")
            pairs = [(str(d), str(s)) if back else (str(s), str(d)) for s, d in change.move.steps]
            new = text
            for a, b in pairs:
                new = new.replace(a, b)
            if new != text:
                _backup(ini)
                mode = ini.stat().st_mode & 0o777
                fd, tmp = tempfile.mkstemp(dir=ini.parent, prefix=f".{ini.name}.")   # O_EXCL, a fresh name
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as handle:
                        os.fchmod(handle.fileno(), mode)
                        handle.write(new)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(tmp, ini)
                except BaseException:
                    if os.path.lexists(tmp):
                        os.unlink(tmp)
                    raise
        except OSError as error:
            problems.append(f"{ini.name}: {error}")
    return problems
