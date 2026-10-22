"""Move one model to another local LLM's model folder (the map's Move to… and
dragging a model box onto another folder).

Only GGUF folders take part: llama.cpp and KoboldCpp read a flat folder,
LM Studio <publisher>/<model>/<file>.gguf (a file from a flat folder goes to
local/<name>/ there, as Reorganize puts it). The files are the ones Remove
would take (models_ctl.removal: this account's own regular files inside the
source folder, all parts of a split model); a model that is only a link is
not moved. On one disk a move is a hard link at the new place and the old
name removed, to another a copy (O_EXCL, with progress) linked into place once
complete; a link never replaces an existing name, so nothing at the target is
ever overwritten, even if it appears after the check. Folders made on the way
must stay inside the target folder and be this account's. Undo moves them back.
"""

import os
import stat
from dataclasses import dataclass, field
from pathlib import Path

import llm_engine
import model_layout
import models_ctl



def movable(key: str) -> bool:
    """Its models can be moved between model folders: a local LLM that reads GGUF folders."""
    import llm_engine
    return llm_engine.of(key).gguf_folder


@dataclass
class Move:
    name: str
    steps: list = field(default_factory=list)     # (source Path, target Path)
    size: int = 0
    copy: bool = False                            # to another disk: copied, then the original removed
    root: Path = None                             # the target folder
    back: Path = None                             # the source folder (undo)


def _folder(path) -> Path:
    return Path(os.path.realpath(os.path.expanduser(str(path))))


def _own_dir(path: Path) -> bool:
    try:
        st = os.stat(path)
    except OSError:
        return False
    return os.path.isdir(path) and st.st_uid == os.getuid()


def plan(src_key: str, src_dir, name: str, dst_key: str, dst_dir) -> Move:
    """What moving model `name` from one local LLM's folder to another's does; ValueError
    (with the reason) when it cannot be done."""
    if not movable(src_key) or not movable(dst_key):
        raise ValueError("only llama.cpp, KoboldCpp and LM Studio models move between folders")
    if not src_dir or not dst_dir:
        raise ValueError("no model folder")
    source, target = _folder(src_dir), _folder(dst_dir)
    if source == target:
        raise ValueError("it is in that folder already")
    if not _own_dir(target):
        raise ValueError("the target folder is not a folder of this account")
    removal = models_ctl.removal(src_key, src_dir, name)
    if removal.links_only or any(p.is_symlink() for p in removal.paths):
        raise ValueError("only a link to the model is in this folder: move the file itself")
    move = Move(name, root=target, back=source)
    for path in removal.paths:
        real = Path(os.path.abspath(path))
        dest = model_layout.canonical(real, source, target) \
            if llm_engine.of(dst_key).gguf_layout == "publisher" else target / real.name
        if not dest.is_relative_to(target) or ".." in dest.relative_to(target).parts:
            raise ValueError("the target lies outside the model folder")
        if dest.exists() or dest.is_symlink():
            raise ValueError(f"{dest.name} is in the target folder already")
        move.steps.append((real, dest))
        move.size += real.stat().st_size
    move.copy = model_layout.device(source) != model_layout.device(target)
    if move.copy and model_layout.free_space(target) < move.size:
        raise ValueError("not enough free space in the target folder")
    return move


def _make_parents(target: Path, root: Path):
    """The folders above target, each inside root and this account's (never through another's link)."""
    folder = target.parent
    folder.mkdir(parents=True, exist_ok=True)
    real = Path(os.path.realpath(folder))
    if not real.is_relative_to(Path(os.path.realpath(root))) or not _own_dir(real):
        raise ValueError(f"{folder} is not a folder of this account inside the model folder")


def _open_own(source: Path):
    """(fd, stat) of source opened once, never through a link, and checked on that open
    file: this account's own regular file. Everything after reads and compares this fd,
    so swapping the name for a link or another file in between is noticed, not followed."""
    try:
        fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError as error:
        raise ValueError(f"{source.name} is not this account's own file") from error
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
        os.close(fd)
        raise ValueError(f"{source.name} is not this account's own file")
    return fd, st


def _same_file(path: Path, st) -> bool:
    try:
        now = os.stat(path, follow_symlinks=False)
    except OSError:
        return False
    return stat.S_ISREG(now.st_mode) and (now.st_dev, now.st_ino) == (st.st_dev, st.st_ino)


def _copy_to(src_fd: int, st, source: Path, target: Path, progress, cancelled, done: int, total: int) -> int:
    """Copy the already opened source into target: into an owner-only .part file first,
    which gets the source's mode and times only once it is complete."""
    partial = target.with_name(f".{target.name}.part")
    fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    copied = 0
    try:
        with os.fdopen(fd, "wb") as dst:
            while chunk := os.read(src_fd, model_layout.CHUNK):
                if cancelled():
                    raise model_layout.Cancelled()
                dst.write(chunk)
                copied += len(chunk)
                progress((done + copied) / max(1, total), source.name)
            dst.flush()
            os.fsync(dst.fileno())
            if os.fstat(dst.fileno()).st_size != st.st_size:
                raise OSError(f"{source.name}: the copy is incomplete")
            os.utime(dst.fileno(), ns=(st.st_atime_ns, st.st_mtime_ns))
            os.fchmod(dst.fileno(), stat.S_IMODE(st.st_mode) & 0o777)
        os.link(partial, target, follow_symlinks=False)       # fails if anything has that name now
    finally:
        os.unlink(partial)
    return copied


def move_file(source: Path, target: Path, root: Path, copy: bool, progress=lambda f, t: None,
              cancelled=lambda: False, done: int = 0, total: int = 1) -> int:
    """One file to its new name, never over an existing one; returns bytes copied."""
    src_fd, st = _open_own(source)
    try:
        _make_parents(target, root)
        if copy:
            copied = _copy_to(src_fd, st, source, target, progress, cancelled, done, total)
        else:
            os.link(source, target, follow_symlinks=False)
            copied = 0
        if not _same_file(source, st) or not copy and not _same_file(target, st):
            os.unlink(target)                                 # the name changed after the open
            raise ValueError(f"{source.name} changed while it was moved")
        os.unlink(source)
    finally:
        os.close(src_fd)
    return copied


def apply(move: Move, progress=lambda f, t: None, cancelled=lambda: False) -> list:
    """Do the move; returns the steps done (for undo), also when one fails on the way."""
    done, copied = [], 0
    try:
        for source, target in move.steps:
            copied += move_file(source, target, move.root, move.copy, progress, cancelled, copied, max(1, move.size))
            done.append((source, target))
    except (OSError, ValueError, model_layout.Cancelled) as error:
        error.done = done
        raise
    return done


def undo(move: Move, done: list, progress=lambda f, t: None) -> list:
    """Move the files back, last first; returns the problems (empty: all back)."""
    problems = []
    total = max(1, sum(t.stat().st_size for _s, t in done if t.exists()))
    for source, target in reversed(done):
        try:
            move_file(target, source, move.back, move.copy, progress, lambda: False, 0, total)
            if target.parent != move.root:
                model_layout._prune(target.parent, stop=2)
        except (OSError, ValueError) as error:
            problems.append(f"{target.name}: {error}")
    return problems
