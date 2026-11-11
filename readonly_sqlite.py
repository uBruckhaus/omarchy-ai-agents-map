"""Bounded own-UID numeric-reader SQLite snapshots, with no disk-side SQLite writes.

Session readers select only named metadata/numeric columns. WAL pages are merged
in memory following sqlite.org/fileformat2.html#walformat, never by opening a
write-capable SQLite connection to the agent's files.
"""
from __future__ import annotations
import os
from pathlib import Path
import sqlite3
import struct

MAX_BYTES = 512 * 1024 * 1024


def committed_image(base: bytes, wal: bytes = b'') -> bytes:
    if len(base) < 100 or base[:16] != b'SQLite format 3\0':
        raise ValueError('Invalid database')
    page = int.from_bytes(base[16:18], 'big')
    page = 65536 if page == 1 else page
    if not 512 <= page <= 65536 or page & (page - 1) or len(base) % page:
        raise ValueError('Invalid page size')
    image = bytearray(base)
    if wal:
        if len(wal) < 32:
            raise ValueError('Invalid WAL')
        magic, version, size, _, salt1, salt2, expected0, expected1 = struct.unpack('>8I', wal[:32])
        if magic not in (0x377f0682, 0x377f0683) or version != 3007000 or size != page:
            raise ValueError('Invalid WAL header')
        endian = '<' if magic == 0x377f0682 else '>'
        s0 = s1 = 0
        def checksum(data):
            nonlocal s0, s1
            for at in range(0, len(data), 8):
                a, b = struct.unpack_from(endian + 'II', data, at)
                s0 = (s0 + a + s1) & 0xffffffff
                s1 = (s1 + b + s0) & 0xffffffff
        checksum(wal[:24])
        if (s0, s1) != (expected0, expected1):
            raise ValueError('Invalid WAL checksum')
        frames, last, pages = [], 0, 0
        for at in range(32, len(wal) - 23 - page, 24 + page):
            number, commit, a, b, c0, c1 = struct.unpack_from('>6I', wal, at)
            if (a, b) != (salt1, salt2) or not number:
                break
            checksum(wal[at:at+8])
            checksum(wal[at+24:at+24+page])
            if (s0, s1) != (c0, c1):
                break
            frames.append((at, number))
            if commit:
                if commit > MAX_BYTES // page:
                    raise ValueError('WAL exceeds bound')
                last, pages = len(frames), commit
        if last:
            length = pages * page
            image = image[:length] + bytearray(max(0, length-len(image)))
            for at, number in frames[:last]:
                if number <= pages:
                    image[(number-1)*page:number*page] = wal[at+24:at+24+page]
    image[18:20] = b'\x01\x01'
    return bytes(image)


def connect(path: Path):
    import ai_core
    path = Path(path)
    if path.name in ('auth.json', '.credentials.json', 'credentials.json'):
        return None
    descriptors = []
    con = None
    try:
        resolved = ai_core._resolve_private_parent(Path(os.path.abspath(path)).parent)
        parent = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            for part in resolved.parts[1:]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
                os.close(parent)
                parent = child
                ai_core._trusted_private_directory(parent)
            def opened(name, optional=False):
                try:
                    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent)
                except FileNotFoundError:
                    if optional:
                        return None
                    raise
                descriptors.append(fd)
                import stat
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_size > MAX_BYTES:
                    raise ValueError('Unavailable database')
                return fd
            base_fd = opened(path.name)
            wal_fd = opened(path.name + '-wal', True)
            journal_fd = opened(path.name + '-journal', True)
            if journal_fd is not None and os.read(journal_fd, 8) == b'\xd9\xd5\x05\xf9\x20\xa1\x63\xd7':
                return None
            def stamp(fd):
                st = os.fstat(fd)
                return st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns
            def read(fd):
                if fd is None:
                    return b''
                with os.fdopen(fd, 'rb', closefd=False) as handle:
                    data = handle.read(MAX_BYTES + 1)
                if len(data) > MAX_BYTES:
                    raise ValueError('Database exceeds bound')
                return data
            before = [(fd, stamp(fd)) for fd in (base_fd, wal_fd) if fd is not None]
            base, wal = read(base_fd), read(wal_fd)
            if any(stamp(fd) != value for fd, value in before):
                return None
        finally:
            os.close(parent)
        con = sqlite3.connect(':memory:')
        con.deserialize(committed_image(base, wal))
        con.execute('PRAGMA temp_store=MEMORY')
        con.execute('PRAGMA query_only=ON')
        con.execute('PRAGMA trusted_schema=OFF')
        import time
        deadline = time.monotonic() + 2
        con.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        return con
    except (OSError, ValueError, sqlite3.Error, struct.error):
        if con is not None:
            con.close()
        return None
    finally:
        for fd in descriptors:
            os.close(fd)
