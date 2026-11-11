"""Bounded, user-requested discovery on mounted local data disks. No execution."""
from __future__ import annotations

import os
import json
from collections import deque
from pathlib import Path
import re
import stat
import time

LOCAL_FILESYSTEMS = frozenset(('ext4', 'ext3', 'ext2', 'btrfs', 'xfs', 'f2fs', 'zfs', 'ntfs3', 'exfat', 'vfat'))
EXCLUDED = ('/boot', '/proc', '/sys', '/dev', '/run', '/tmp', '/var', '/usr', '/etc')
MAX_ENTRIES = 12000
MAX_DEPTH = 5
MAX_RESULTS = 128
MAX_SECONDS = 5.0


def mounts(text: str | None = None) -> list[dict]:
    if text is None:
        text = Path('/proc/self/mountinfo').read_text()
    result = {}
    for line in text.splitlines():
        fields = line.split()
        try:
            sep = fields.index('-')
            kind = fields[sep + 1]
            path = re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), fields[4])
            root = Path(path)
            if kind not in LOCAL_FILESYSTEMS or path == '/' or not root.is_absolute() \
                    or any(root == Path(p) or root.is_relative_to(p) for p in EXCLUDED):
                continue
            result[path] = {'path': path, 'filesystem': kind, 'identity': fields[0] + ':' + fields[2]}
        except (ValueError, IndexError):
            continue
    return list(result.values())


def root_for(path: str, available: list[dict]) -> dict:
    if not isinstance(path, str) or not path or len(path) > 4096 or any(ord(c) < 32 for c in path):
        raise ValueError('Choose a mounted data folder')
    root = Path(path)
    if not root.is_absolute() or root.resolve() != root:
        raise ValueError('Choose a real absolute folder')
    found = [row for row in available if root.is_relative_to(row['path'])]
    if not found or not root.is_dir():
        raise ValueError('Folder is not on a supported mounted local disk')
    return max(found, key=lambda row: len(row['path']))


def stamp(path: Path) -> list[int]:
    st = path.stat()
    return [st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns]


def safe(path: Path, mount: Path, device: int, *, executable=False, regular=False) -> Path | None:
    try:
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(mount):
            return None
        st = resolved.stat()
        if st.st_dev != device or st.st_uid != os.getuid():
            return None
        if executable:
            if resolved.suffix.lower()=='.json' or 'credentials' in resolved.name.lower():
                return None
            if not stat.S_ISREG(st.st_mode) or not os.access(resolved, os.X_OK):
                return None
            fd = os.open(resolved, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, 'rb') as handle:
                if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode) or handle.read(4) != b'\x7fELF':
                    return None
        elif regular and not stat.S_ISREG(st.st_mode):
            return None
        elif not regular and not stat.S_ISDIR(st.st_mode):
            return None
        return resolved
    except (OSError, RuntimeError):
        return None


def provider_rules():
    import providers
    # Built-in discovery facts also work before a provider's signed add-on is enabled.
    found = {p.key: p for p in providers.built_in_local() if p.discover_existing}
    found.update({p.key: p for p in providers.local() if p.discover_existing})
    return found


def link_stamp(path: Path) -> list:
    st = path.lstat()
    return [st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, os.readlink(path)]


def program(path: Path, mount: Path, device: int) -> dict | None:
    """Keep the launch alias while validating its native target and link components."""
    target = safe(path, mount, device, executable=True)
    if target is None:
        return None
    links = {}
    try:
        pending = deque(path.relative_to(mount).parts)
        current = mount
        followed = 0
        while pending:
            name = pending.popleft()
            if name == '..':
                current = current.parent
                if not current.is_relative_to(mount):
                    return None
                continue
            if name == '.':
                continue
            part = current / name
            if part.is_symlink():
                st = part.lstat()
                followed += 1
                if st.st_uid != os.getuid() or st.st_dev != device or followed > 40:
                    return None
                links[str(part)] = link_stamp(part)
                direct = Path(os.readlink(part))
                if direct.is_absolute():
                    if not direct.is_relative_to(mount):
                        return None
                    current = mount
                    parts = direct.relative_to(mount).parts
                else:
                    parts = direct.parts
                pending.extendleft(reversed(parts))
            else:
                current = part
        if current != target:
            return None
    except (OSError, ValueError):
        return None
    return {'path': str(target), 'launch_path': str(path), 'links': links}


def scan(path: str, progress=lambda fraction, text='': None) -> dict:
    available = mounts()
    mounted = root_for(path, available)
    root, mount = Path(path), Path(mounted['path'])
    device = mount.stat().st_dev
    rules = provider_rules()
    deadline = time.monotonic() + MAX_SECONDS
    pending = deque([(root, 0)])
    visited: set[Path] = set()
    count = 0
    results: dict[tuple, dict] = {}
    limited = False
    limits: set[str] = set()
    notices: set[str] = set()
    foreign: set[str] = set()
    program_names = {name for p in rules.values() for name in getattr(p.engine, 'programs', ())}
    while pending:
        if count >= MAX_ENTRIES or time.monotonic() >= deadline or len(results) >= MAX_RESULTS:
            limits.add('entries' if count >= MAX_ENTRIES else 'time' if time.monotonic() >= deadline else 'results')
            limited = True
            break
        folder, depth = pending.popleft()
        if folder in visited:
            continue
        visited.add(folder)
        if folder != mount and safe(folder, mount, device) is None:
            try:
                if folder.stat().st_uid != os.getuid():
                    notices.add('ownership')
                if folder.stat().st_dev != device:
                    notices.add('filesystem')
            except OSError:
                pass
            continue
        names = []
        try:
            with os.scandir(folder) as entries:
                for entry in entries:
                    count += 1
                    if count >= MAX_ENTRIES or time.monotonic() >= deadline:
                        limits.add('entries' if count >= MAX_ENTRIES else 'time')
                        limited = True
                        break
                    names.append(entry.name)
                    if entry.name in program_names or entry.name.lower().endswith('.gguf'):
                        try:
                            if entry.stat().st_uid != os.getuid():
                                foreign.add(entry.path)
                        except OSError:
                            pass
                    if depth >= MAX_DEPTH and not entry.name.startswith('.') and entry.is_dir():
                        limits.add('depth')
                        limited = True
                    if depth < MAX_DEPTH and not entry.name.startswith('.') and entry.is_dir():
                        info = entry.stat()
                        if info.st_uid != os.getuid():
                            notices.add('ownership')
                        if info.st_dev != device:
                            notices.add('filesystem')
                        target = safe(Path(entry.path), mount, device)
                        # Mounted subtrees are scanned independently, never traversed implicitly.
                        if target and not any(target.is_relative_to(row['path']) for row in available
                                              if row['path'] != mounted['path']):
                            pending.append((target, depth + 1))
                        elif target:
                            notices.add('filesystem')
        except OSError:
            continue
        ggufs = []
        targets = []
        for name in names:
            if name.lower().endswith('.gguf'):
                item = folder / name
                try:
                    st = item.stat(follow_symlinks=False)
                    target = safe(item, mount, device, regular=True)
                    if target and st.st_uid == os.getuid() and st.st_dev == device:
                        ggufs.append(name)
                        targets.append(str(target))
                except OSError:
                    pass
        if ggufs:
            row = {'kind': 'models', 'provider': '', 'name': 'GGUF', 'path': str(folder),
                   'count': len(ggufs), 'models': '', 'environment': {}, 'dependencies': sorted(set(targets))}
            results[('models', str(folder))] = row
        for provider in rules.values():
            for row in provider.discover_existing(folder, names, mount, device):
                key = ('runtime', row['provider'], row['path'])
                previous = results.get(key)
                if previous is None or (row.get('links') and not previous.get('links')):
                    results[key] = row
        progress(min(0.95, count / MAX_ENTRIES), str(folder))
    records = []
    size = 0
    for row in list(results.values())[:MAX_RESULTS]:
        try:
            tracked = [row['path'], *row['dependencies']]
            row.update({'mount': mounted, 'stamps': {p: stamp(Path(p)) for p in tracked}})
            size += len(json.dumps(row).encode())
            if size > 700 * 1024:
                limits.add('response')
                limited = True
                break
            records.append(row)
        except OSError:
            continue
    return {'records': records, 'limited': limited, 'visited': count, 'limits': sorted(limits),
            'depth': MAX_DEPTH, 'notices': sorted(notices), 'foreign': sorted(foreign)[:16]}


def validate(record: dict) -> None:
    mounted = root_for(str(Path(record['path']).parent) if record['kind'] == 'runtime' else record['path'], mounts())
    if mounted != record['mount']:
        raise ValueError('The mount changed; scan again')
    device = Path(mounted['path']).stat().st_dev
    if record['kind'] == 'runtime':
        rule=provider_rules().get(record['provider'])
        launch_path=Path(record.get('launch_path',record['path']))
        if rule is None or launch_path.name not in getattr(rule.engine,'programs',()):
            raise ValueError('Unknown runtime launch path')
        candidates=rule.discover_existing(launch_path.parent,[launch_path.name],Path(mounted['path']),device)
        fresh=next((r for r in candidates if r['kind']=='runtime' and r['path']==record['path']),None)
        if fresh is None or fresh['environment']!=record['environment'] or fresh['dependencies']!=record['dependencies']:
            raise ValueError('Runtime environment changed; scan again')
        launch = program(Path(record.get('launch_path', record['path'])), Path(mounted['path']), device)
        if launch is None or launch['path'] != record['path'] or launch['links'] != record.get('links', {}):
            raise ValueError('The launch link changed; scan again')
    for path, identity in record['stamps'].items():
        is_program = path == record['path'] and record['kind'] == 'runtime'
        if stamp(Path(path)) != identity or safe(Path(path), Path(mounted['path']), device,
                executable=is_program, regular=not is_program and Path(path).is_file()) is None:
            raise ValueError('The discovered installation changed; scan again')
