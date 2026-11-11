"""Add-ons from Maprisma's catalog (shared with the AI Agents Map plugin): find, install, update, switch on/off, remove.

- The catalog is catalog.json in github.com/maprisma/maprisma-addons, with catalog.sig beside it.
  It is fetched only when Settings › Look for add-ons online is on, at most every CHECK_EVERY, and
  used only when catalog.sig verifies it with the author's built-in key (addons.signature_ok). Each
  entry is checked again here (ids, versions, download address on that repository's releases,
  sizes, hashes, detect hints).
- Installing downloads the zip (size cap), compares its SHA-256 with the signed catalog, unpacks it
  safely into a temporary folder beside the add-ons folder (only "<id>/…" names, no "..", no links,
  file count and sizes capped), and then hands the folder to addons.verify: the add-on's own
  signature and every file's hash. Only a verified folder replaces an installed one, whose old copy
  goes to the trash. A downloaded zip can be installed from a file the same way.
- Removing moves the folder to the trash. Switching on/off is the settings list addons_enabled.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import stat
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import addon_api
import addons
import ai_core

REPO = "https://github.com/maprisma/maprisma-addons"
CATALOG_URL = "https://raw.githubusercontent.com/maprisma/maprisma-addons/main/catalog.json"
SIGNATURE_URL = "https://raw.githubusercontent.com/maprisma/maprisma-addons/main/catalog.sig"
DOWNLOADS = REPO + "/releases/download/"
CACHE = Path.home() / ".cache/ai-agents-map/catalog.json"
CHECK_EVERY = 6 * 3600
MAX_CATALOG = 1024 * 1024
MAX_ZIP = 8 * 1024 * 1024
MAX_ENTRIES = 200
KINDS = ("feature", "agent", "provider")
_ID = re.compile(r"[a-z][a-z0-9-]{1,40}")
_VERSION = re.compile(r"\d{1,4}\.\d{1,4}\.\d{1,4}")
_SHA = re.compile(r"[0-9a-f]{64}")
_ICON = re.compile(r"[a-z0-9-]{1,64}")


@dataclass
class Entry:
    id: str
    name: str
    version: str
    kind: str = "feature"
    icon: str = ""
    summary: str = ""
    description: str = ""
    privacy: int = addon_api.PRIVACY["numbers"]
    capabilities: list = field(default_factory=list)
    harnesses: list = field(default_factory=list)
    reads: list = field(default_factory=list)
    sends: list = field(default_factory=list)
    commands: list = field(default_factory=list)        # detect hints: programs on PATH
    paths: list = field(default_factory=list)           # detect hints: "~/…" folders or files
    homepage: str = ""
    url: str = ""
    size: int = 0
    sha256: str = ""
    needs: str = ""                  # the oldest Maprisma it works with ("" = any)


def _text(value, limit: int) -> str:
    return " ".join(str(value).split())[:limit] if isinstance(value, str) else ""


def _texts(value, limit: int = 12) -> list:
    return [_text(v, 300) for v in value[:limit] if isinstance(v, str) and v.strip()] if isinstance(value, list) else []


def entry_from(data) -> Entry | None:
    """A checked catalog entry, or None when anything about it is off."""
    if not isinstance(data, dict):
        return None
    aid, version = data.get("id"), data.get("version")
    if not isinstance(aid, str) or not _ID.fullmatch(aid) or not isinstance(version, str) \
            or not _VERSION.fullmatch(version) or data.get("api") != addon_api.API_VERSION:
        return None
    url, sha, size = data.get("url"), data.get("sha256"), data.get("size")
    if not isinstance(url, str) or url != f"{DOWNLOADS}{aid}-{version}/{aid}-{version}.zip" \
            or not isinstance(sha, str) or not _SHA.fullmatch(sha) \
            or not isinstance(size, int) or not 0 < size <= MAX_ZIP:
        return None
    caps = data.get("capabilities", [])
    if not isinstance(caps, list) or not all(c in addon_api.CAPABILITIES for c in caps):
        return None
    needs = data.get("needs", "")
    m = addons._NEEDS.fullmatch(needs.strip()) if isinstance(needs, str) else None
    if needs and m is None:
        return None
    needs = m.group(1) if m else ""
    commands, paths = addons.detect_hints(data.get("detect"))
    icon = data.get("icon") if isinstance(data.get("icon"), str) and _ICON.fullmatch(data.get("icon")) else ""
    return Entry(aid, _text(data.get("name"), 80) or aid, version,
                 data.get("kind") if data.get("kind") in KINDS else "feature", icon,
                 _text(data.get("summary"), 200), _text(data.get("description"), 2000),
                 addon_api.PRIVACY.get(data.get("privacy"), addon_api.PRIVACY["numbers"]), sorted(set(caps)),
                 _texts(data.get("harnesses")), _texts(data.get("reads")), _texts(data.get("sends")),
                 commands, paths, addons.homepage(data.get("homepage")), url, size, sha, needs)


def parse_catalog(text: bytes, signature: bytes, signers: str = addons.ALLOWED_SIGNERS) -> list | None:
    """The catalog's entries when its signature verifies; None when it does not. Per add-on the newest
    version this Maprisma can load: the catalog keeps older versions for older Maprisma releases (which
    take the first entry of an add-on, so those come first)."""
    if len(text) > MAX_CATALOG or not addons.signature_ok(text, signature, signers):
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("catalog") != 1 or not isinstance(data.get("addons"), list):
        return None
    found = {}
    this = addons.version_tuple(addons.MAPRISMA_VERSION)
    for raw in data["addons"][:MAX_ENTRIES]:
        entry = entry_from(raw)
        if entry is None or (entry.needs and addons.version_tuple(entry.needs) > this):
            continue
        known = found.get(entry.id)
        if known is None or addons.version_tuple(entry.version) > addons.version_tuple(known.version):
            found[entry.id] = entry
    return list(found.values())            # in the order the add-ons first appear


def catalog(online: bool, fetch=None, cache: Path = CACHE, now: float | None = None,
            signers: str = addons.ALLOWED_SIGNERS) -> list:
    """The catalog's entries: from the cache when it is recent (or when looking online is off),
    else fetched again. [] when there is none that verifies."""
    now = time.time() if now is None else now
    saved = _load_cache(cache)
    if saved and (not online or now - saved.get("checked", 0) < CHECK_EVERY):
        entries = parse_catalog(saved["text"].encode(), saved["sig"].encode(), signers)
        return entries or []
    if not online:
        return []
    import netfetch
    fetch = fetch or netfetch.fetch
    try:
        text = fetch(CATALOG_URL, max_bytes=MAX_CATALOG, timeout=15, redirects=0)
        sig = fetch(SIGNATURE_URL, max_bytes=8 * 1024, timeout=15, redirects=0)
    except netfetch.FetchError:
        return parse_catalog(saved["text"].encode(), saved["sig"].encode(), signers) or [] if saved else []
    entries = parse_catalog(text, sig, signers)
    if entries is None:
        return []
    try:
        cache.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        ai_core.atomic_write(cache, json.dumps({"checked": now, "text": text.decode(), "sig": sig.decode()}))
    except (OSError, UnicodeDecodeError):
        pass
    return entries


def _load_cache(cache: Path) -> dict | None:
    try:
        data = json.loads(ai_core.read_whole(cache, MAX_CATALOG * 3, follow=False))   # regular file only, never blocks
    except (OSError, ValueError):
        return None
    ok = isinstance(data, dict) and isinstance(data.get("text"), str) and isinstance(data.get("sig"), str) \
        and isinstance(data.get("checked"), (int, float))
    return data if ok else None


# --- is its harness on this PC? ----------------------------------------------------------------------

def present(entry) -> bool:
    """The detect hints of a catalog entry or an add-on find its program on PATH or its folder
    (read-only checks)."""
    if any(shutil.which(c) for c in entry.commands):
        return True
    home = Path.home()
    return any((home / p[2:]).exists() for p in entry.paths)


# --- unpacking safely ----------------------------------------------------------------------------------

class InstallError(Exception):
    pass


def unpack(data: bytes, target: Path) -> str:
    """Unzip an add-on into `target` (which must be empty); the add-on's id. Every name must be
    "<id>/…" (one id), relative, without ".." or links; counts and sizes are capped."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as error:
        raise InstallError("not a zip file") from error
    infos = zf.infolist()
    if not infos or len(infos) > addons.MAX_FILES + 4:
        raise InstallError("too many files")
    ids, total = set(), 0
    for info in infos:
        name = info.filename
        parts = name.split("/")
        if name.startswith("/") or "\\" in name or any(p in ("", ".", "..") for p in parts[:-1]) \
                or parts[-1] in (".", "..") or len(parts) > 3:
            raise InstallError(f"bad name in the zip: {name[:80]}")
        if stat.S_ISLNK(info.external_attr >> 16):
            raise InstallError("links are not allowed")
        ids.add(parts[0])
        total += info.file_size
        if info.file_size > addons.MAX_FILE or total > addons.MAX_TOTAL:
            raise InstallError("too large")
    if len(ids) != 1 or not _ID.fullmatch(next(iter(ids))):
        raise InstallError("not one add-on folder")
    aid = next(iter(ids))
    for info in infos:
        if info.is_dir():
            continue
        path = target / info.filename
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        with zf.open(info) as source:
            body = source.read(addons.MAX_FILE + 1)
        if len(body) > addons.MAX_FILE:
            raise InstallError("too large")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
        with os.fdopen(fd, "wb") as handle:
            handle.write(body)
    for folder in [target / aid, *(p for p in (target / aid).rglob("*") if p.is_dir())]:
        os.chmod(folder, 0o755)
    return aid


def install_bytes(data: bytes, user_dir: Path = addons.USER_DIR, signers: str = addons.ALLOWED_SIGNERS,
                  expect: str | None = None, trash=None, allow=None, described=None) -> addons.Addon:
    """Unpack, verify and put an add-on into the user's add-ons folder; the verified Addon.
    `allow(id, kind, replacing)`: asked once it is verified and before anything is changed; a text
    refuses it (Settings › What may be done). Raises InstallError (nothing is changed then)."""
    user_dir.mkdir(parents=True, exist_ok=True, mode=0o755)
    staging = Path(tempfile.mkdtemp(prefix=".install-", dir=user_dir))
    try:
        os.chmod(staging, 0o755)
        aid = unpack(data, staging)
        if expect is not None and aid != expect:
            raise InstallError(f"the zip holds {aid}, not {expect}")
        addon = addons.verify(staging / aid, signers)
        if addon.state != "verified":
            raise InstallError(addon.reason)
        if described is not None and not same_description(addon, described):
            # What the confirmation showed (the catalog) must be what is installed and switched on.
            raise InstallError("the add-on does not match its catalog description")
        final = user_dir / aid
        if allow is not None:
            refused = allow(aid, addon.kind, final.exists() or final.is_symlink())
            if refused:
                raise InstallError(refused)
        old = None
        if final.exists() or final.is_symlink():
            import secrets
            old = user_dir / f".old-{aid}-{time.time_ns()}-{secrets.token_hex(4)}"
            os.replace(final, old)
        try:
            os.replace(staging / aid, final)
        except OSError:
            if old is not None:
                os.replace(old, final)                # the installed one comes back as it was
            raise
        if old is not None:
            (trash or _trash)(old)
        addon.folder = final
        return addon
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def install(entry: Entry, fetch=None, **kwargs) -> addons.Addon:
    """Download a catalog entry's zip, check its SHA-256 against the signed catalog, install it."""
    import netfetch
    fetch = fetch or netfetch.fetch
    try:
        data = fetch(entry.url, max_bytes=min(entry.size, MAX_ZIP), timeout=60, redirects=3)
    except netfetch.FetchError as error:
        raise InstallError(str(error)) from error
    if len(data) != entry.size or hashlib.sha256(data).hexdigest() != entry.sha256:
        raise InstallError("the download does not match the catalog")
    return install_bytes(data, expect=entry.id, described=entry, **kwargs)


def same_description(addon, entry) -> bool:
    """The verified add-on declares what its catalog entry says: kind, privacy level, what it reads and
    sends, its capabilities (the Install confirmation is built from the entry)."""
    return (addon.kind == entry.kind and addon.privacy == entry.privacy
            and all(sorted(map(str, getattr(addon, f))) == sorted(map(str, getattr(entry, f)))
                    for f in ("reads", "sends", "capabilities")))


def install_file(path: Path, **kwargs) -> addons.Addon:
    """Install a zip downloaded by hand (same checks, its own signature decides)."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)   # a FIFO never blocks
        with os.fdopen(fd, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise InstallError("not a file")
            data = handle.read(MAX_ZIP + 1)
    except OSError as error:
        raise InstallError("cannot be read") from error
    if len(data) > MAX_ZIP:
        raise InstallError("too large")
    return install_bytes(data, **kwargs)


def _trash(path: Path):
    """To the trash (Gio), else removed."""
    try:
        from gi.repository import Gio
        Gio.File.new_for_path(str(path)).trash(None)
    except Exception:
        shutil.rmtree(path, ignore_errors=True)


def remove(aid: str, user_dir: Path = addons.USER_DIR, trash=None) -> bool:
    """Move an add-on of the user's folder to the trash (system-wide ones are the package's)."""
    folder = user_dir / aid
    if not _ID.fullmatch(aid) or not folder.is_dir() or folder.is_symlink():
        return False
    set_enabled(aid, False)
    (trash or _trash)(folder)
    return True


# --- switched on ---------------------------------------------------------------------------------------

def enabled() -> set:
    value = ai_core.setting("addons_enabled")
    return {v for v in value if isinstance(v, str)} if isinstance(value, list) else set()


def switched_off() -> set:
    """The add-ons the user switched off (addons_off): never switched on again by enable_found."""
    value = ai_core.setting("addons_off")
    return {v for v in value if isinstance(v, str)} if isinstance(value, list) else set()


def set_enabled(aid: str, on: bool) -> None:
    """The user switches an add-on on or off (as Maprisma's addons.set_enabled: off is remembered)."""
    now = enabled()
    now = now | {aid} if on else now - {aid}
    off = switched_off() - {aid} if on else switched_off() | {aid}
    ai_core.update_settings(addons_enabled=sorted(now), addons_off=sorted(off))


def enable_found(ids) -> list:
    """Maprisma's addons.enable_found (task 055): the add-ons of programs found on this PC are switched
    on at the start, except the ones the user switched off. Never switches one off. The ids switched on."""
    now, off = enabled(), switched_off()
    new = sorted(i for i in ids if isinstance(i, str) and i not in now and i not in off)
    if new:
        ai_core.update_settings(addons_enabled=sorted(now | set(new)))
    return new


def online() -> bool:
    """Settings › Look for add-ons online (on unless switched off)."""
    return ai_core.setting("addons_online") is not False


def newer(a: str, b: str) -> bool:
    """Version a is newer than b (x.y.z)."""
    ta, tb = addons.version_tuple(a), addons.version_tuple(b)
    return bool(ta and tb) and ta > tb
