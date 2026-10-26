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

The preview keeps each file's (st_dev, st_ino). Moving reaches both folders only by walking from /
one name at a time without following links (_walk) and works relative to those pinned folders, and
the file opened must be the one the preview saw: a folder above it swapped for a link to somewhere
else after the preview is refused, not followed (Undo the same, with the identity at the new place).
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
    root: Path | None = None                      # the target folder
    back: Path | None = None                      # the source folder (undo)
    idents: dict = field(default_factory=dict)    # str(source) -> its (st_dev, st_ino) in the preview
    anchors: dict = field(default_factory=dict)   # str(root or back) -> anchor() of it in the preview

    def anchor(self, folder: Path | None):
        if folder is None or str(folder) not in self.anchors:
            raise ValueError("the move has no preview of its folders")
        return self.anchors[str(folder)]


def _folder(path) -> Path:
    return Path(os.path.realpath(os.path.expanduser(str(path))))


def _own_dir(path: Path) -> bool:
    try:
        st = os.stat(path)
    except OSError:
        return False
    return os.path.isdir(path) and st.st_uid == os.getuid()


def anchor(folder: Path) -> tuple[Path, tuple]:
    """(the deepest folder of `folder` that exists, folder itself if it does; its (st_dev, st_ino)),
    taken in the preview: _walk refuses to go on when that folder is another one by then."""
    probe = folder
    while True:
        try:
            st = os.stat(probe, follow_symlinks=False)
        except FileNotFoundError:
            if probe.parent == probe:
                raise ValueError(f"{folder} cannot be reached")
            probe = probe.parent
            continue
        if not stat.S_ISDIR(st.st_mode):
            raise ValueError(f"{probe} is not a folder")
        return probe, (st.st_dev, st.st_ino)


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
    move.anchors = {str(target): anchor(target), str(source): anchor(source)}
    for path in removal.paths:
        real = _folder(path.parent) / path.name       # real folders: _walk refuses any link on the way
        if not real.is_relative_to(source):
            raise ValueError(f"{path.name} lies outside the model folder")
        st = os.stat(real, follow_symlinks=False)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
            raise ValueError(f"{path.name} is not this account's own file")
        dest = model_layout.canonical(real, source, target) \
            if llm_engine.of(dst_key).gguf_layout == "publisher" else target / real.name
        if not dest.is_relative_to(target) or ".." in dest.relative_to(target).parts:
            raise ValueError("the target lies outside the model folder")
        if dest.exists() or dest.is_symlink():
            raise ValueError(f"{dest.name} is in the target folder already")
        move.steps.append((real, dest))
        move.idents[str(real)] = (st.st_dev, st.st_ino)
        move.size += st.st_size
    move.copy = model_layout.device(source) != model_layout.device(target)
    if move.copy and model_layout.free_space(target) < move.size:
        raise ValueError("not enough free space in the target folder")
    return move


def _walk(path: Path, root: Path, base, make: bool = False) -> int:
    """fd of folder `path` (inside root; real paths from the preview), walked from / one name at a
    time and never through a link. base: anchor() of root in the preview; the folder reached there
    must still be that very folder, so a link or another folder swapped in above or at it after
    the preview is refused, not followed. Below base, folders are made when missing (make; each
    through the descriptor just opened). Base and every folder below it must be this account's,
    with no other account allowed to write into them, nor into `path` itself: nothing but this account can then rename or
    replace a folder or a name in it while the file moves."""
    top, ident = base
    if not path.is_absolute() or not path.is_relative_to(root) or not root.is_relative_to(top) \
            or ".." in path.parts:
        raise ValueError(f"{path} is not inside {root}")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        if top == Path("/") and (os.fstat(fd).st_dev, os.fstat(fd).st_ino) != tuple(ident):
            raise ValueError(f"{top} changed after the preview")
        for depth, name in enumerate(path.parts[1:], start=2):
            below = depth > len(top.parts)
            mine = depth >= len(top.parts)            # the anchor and every folder below it
            if make and below:
                try:
                    os.mkdir(name, 0o755, dir_fd=fd)
                except FileExistsError:
                    pass
            next_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            os.close(fd)
            fd = next_fd
            st = os.fstat(fd)
            if depth == len(top.parts) and (st.st_dev, st.st_ino) != tuple(ident):
                raise ValueError(f"{top} changed after the preview")
            if mine and (st.st_uid != os.getuid() or st.st_mode & 0o022):
                raise ValueError(f"{path} is not a folder of this account alone inside the model folder")
        st = os.fstat(fd)
        if st.st_uid != os.getuid():
            raise ValueError(f"{path} is not a folder of this account")
        if st.st_mode & 0o022:
            raise ValueError(f"other accounts can write into {path}: make it this account's alone (chmod go-w)")
    except OSError as error:
        os.close(fd)
        raise ValueError(f"{path.name}: a folder on its way is a link or changed after the preview") from error
    except ValueError:
        os.close(fd)
        raise
    return fd


def _open_own(parent: int, name: str, expect):
    """(fd, stat) of `name` in the pinned folder, opened once, never through a link, and checked on
    that open file: this account's own regular file, and the very file the preview saw (expect:
    its (st_dev, st_ino)). Everything after reads this fd and compares names with expect."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent)
    except OSError as error:
        raise ValueError(f"{name} is not this account's own file") from error
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
        os.close(fd)
        raise ValueError(f"{name} is not this account's own file")
    if expect is None or (st.st_dev, st.st_ino) != tuple(expect):
        os.close(fd)
        raise ValueError(f"{name} changed after the preview")
    return fd, st


def _is(parent: int, name: str, ident) -> bool:
    """`name` in the pinned folder is still the regular file ident ((st_dev, st_ino))."""
    try:
        now = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except OSError:
        return False
    return stat.S_ISREG(now.st_mode) and (now.st_dev, now.st_ino) == tuple(ident)


def _copy_to(src_fd: int, st, name: str, folder: int, progress, cancelled, done: int, total: int):
    """Copy the already opened source as `name` into the pinned target folder: into an owner-only
    .part file first, which gets the source's mode and times only once it is complete. Returns
    (bytes copied, (st_dev, st_ino) of the copy)."""
    partial = f".{name}.part"
    fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600,
                 dir_fd=folder)
    made = os.fstat(fd)
    copied = 0
    try:
        with os.fdopen(fd, "wb") as dst:
            while chunk := os.read(src_fd, model_layout.CHUNK):
                if cancelled():
                    raise model_layout.Cancelled()
                dst.write(chunk)
                copied += len(chunk)
                progress((done + copied) / max(1, total), name)
            dst.flush()
            os.fsync(dst.fileno())
            if os.fstat(dst.fileno()).st_size != st.st_size:
                raise OSError(f"{name}: the copy is incomplete")
            os.utime(dst.fileno(), ns=(st.st_atime_ns, st.st_mtime_ns))
            os.fchmod(dst.fileno(), stat.S_IMODE(st.st_mode) & 0o777)
        if not _is(folder, partial, (made.st_dev, made.st_ino)):
            raise ValueError(f"{name}: the partial copy changed")
        os.link(partial, name, src_dir_fd=folder, dst_dir_fd=folder, follow_symlinks=False)  # fails if the name exists now
    finally:
        _remove(folder, partial, (made.st_dev, made.st_ino))
    return copied, (made.st_dev, made.st_ino)


def _remove(folder: int, name: str, ident) -> bool:
    """Remove `name` from the pinned folder, but only while it is still the file ident."""
    if not _is(folder, name, ident):
        return False
    os.unlink(name, dir_fd=folder)
    return True


def move_file(source: Path, target: Path, src_root: Path, dst_root: Path, copy: bool, expect,
              src_base, dst_base, progress=lambda f, t: None, cancelled=lambda: False,
              done: int = 0, total: int = 1):
    """One file to its new name, never over an existing one. source lies below src_root, target
    below dst_root (real paths from the preview, never resolved again); both folders are reached
    by _walk only (src_base, dst_base: their anchor() from the preview), and the file must be
    `expect` (its (st_dev, st_ino) in the preview). Returns (bytes copied, (st_dev, st_ino) at the new name)."""
    src_dir = _walk(source.parent, src_root, src_base)
    try:
        src_fd, st = _open_own(src_dir, source.name, expect)
        try:
            dst_dir = _walk(target.parent, dst_root, dst_base, make=True)
            try:
                if copy or not os.path.isdir("/proc/self/fd"):   # no /proc: the checked copy instead
                    copied, ident = _copy_to(src_fd, st, target.name, dst_dir, progress, cancelled, done, total)
                else:
                    # the open file itself, not whatever its name is now (linkat through /proc)
                    try:
                        os.link(f"/proc/self/fd/{src_fd}", target.name, dst_dir_fd=dst_dir, follow_symlinks=True)
                    except FileNotFoundError as error:
                        if os.fstat(src_fd).st_nlink == 0:       # its last name went after the open
                            raise ValueError(f"{source.name} changed while it was moved") from error
                        raise
                    copied, ident = 0, (st.st_dev, st.st_ino)
                if not _is(src_dir, source.name, (st.st_dev, st.st_ino)) or not _is(dst_dir, target.name, ident):
                    _remove(dst_dir, target.name, ident)        # a name changed after the open
                    raise ValueError(f"{source.name} changed while it was moved")
                try:
                    os.unlink(source.name, dir_fd=src_dir)
                except OSError as error:                        # the new name goes again: not moved
                    _remove(dst_dir, target.name, ident)
                    raise ValueError(f"{source.name} cannot be removed from its folder: {error}") from error
            finally:
                os.close(dst_dir)
        finally:
            os.close(src_fd)
    finally:
        os.close(src_dir)
    return copied, ident


def apply(move: Move, progress=lambda f, t: None, cancelled=lambda: False) -> list:
    """Do the move; returns the steps done (source, target, (st_dev, st_ino) at the target) for
    undo, also when one fails on the way."""
    done, copied = [], 0
    assert move.root is not None and move.back is not None     # plan() sets both
    try:
        for source, target in move.steps:
            size, ident = move_file(source, target, move.back, move.root, move.copy, move.idents.get(str(source)),
                                    move.anchor(move.back), move.anchor(move.root),
                                    progress, cancelled, copied, max(1, move.size))
            copied += size
            done.append((source, target, ident))
    except (OSError, ValueError, model_layout.Cancelled) as error:
        setattr(error, "done", done)
        raise
    return done


def undo(move: Move, done: list, progress=lambda f, t: None) -> list:
    """Move the files back, last first; returns the problems (empty: all back)."""
    problems = []
    assert move.root is not None and move.back is not None     # plan() sets both
    total = max(1, sum(os.lstat(t).st_size for _s, t, _i in done if os.path.lexists(t)))
    for source, target, ident in reversed(done):
        try:
            move_file(target, source, move.root, move.back, move.copy, ident,
                      move.anchor(move.root), move.anchor(move.back), progress, lambda: False, 0, total)
            if target.parent != move.root:
                model_layout._prune(target.parent, stop=2)
        except (OSError, ValueError) as error:
            problems.append(f"{target.name}: {error}")
    return problems
