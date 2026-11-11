"""Add-ons for AI Agents Map, the same signed add-ons as Maprisma's: found, verified, loaded.

Only add-ons signed by Uwe Bruckhaus load. An add-on is a folder <id>/ with:
    addon.json        id, name, version, api, privacy, kind (feature | agent | provider), needs
                      ("maprisma >= x.y.z"), detect ({commands, paths}: is its program here),
                      capabilities, harnesses, reads, sends
    addon.py          register(api) (see addon_api.py); at import time only definitions (an agent or
                      provider adapter: its Harness / Provider objects, the same code the AI Agents
                      Map plugin has built in)
    translations/<language>.json   optional, the add-on's own strings
    MANIFEST.sha256   "<sha256>  <path>" for every other file
    SIGNATURE         `ssh-keygen -Y sign -n maprisma-addon` of MANIFEST.sha256 with Uwe's key

Before anything of it runs: the folder and every file must be the user's (or root's), regular, not
writable by others, no links, within size limits, only the files above; SIGNATURE must verify
MANIFEST.sha256 against the key below (OpenSSH's ssh-keygen); every file must match its hash and
no file may be missing from or added to the manifest. addon.py is then compiled from the very bytes
that were hashed (never read again from disk), and only when the user switched the add-on on.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import addon_api
import ai_core
import harnesses
import providers

# Uwe Bruckhaus's add-on signing key (fingerprint SHA256:e5cC634Ke3Our5LAeSc88qs22NKZEHTncBW5Ig3amWQ).
ALLOWED_SIGNERS = ('addons@maprisma namespaces="maprisma-addon" '
                   'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFfDZiRAvEhdsjhLIlYc6SfP80I533WGiSw6/tzqQtYA')
PRINCIPAL = "addons@maprisma"
NAMESPACE = "maprisma-addon"
# Add-ons signed for Asterism (this app's old name) with its old key: never loaded, but recognised, so the card
# can say what they are instead of "not signed" (they were copied into the user folder at the rename).
LEGACY_NAMESPACE = "asterism-addon"
LEGACY_SIGNERS = (f'{PRINCIPAL} namespaces="{LEGACY_NAMESPACE}" '
                  'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEIPZnfPIkyy+IX1J0jfB5g/U8U5jjJTapOogzlKrxlo')
LEGACY_REASON = "made for Asterism, this app's old name"

USER_DIR = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "ai-agents-map/addons"
# The add-ons that come with the app (signed like any other), next to its own files: /usr/lib/maprisma/
# addons from the package, inside the AppImage; a newer copy in USER_DIR (from the catalog) wins.
SYSTEM_DIR = Path(__file__).resolve().parent / "addons"

MAPRISMA_VERSION = "0.14.3"       # the Maprisma core this plugin follows: an add-on that "needs" a newer one is refused
KINDS = ("feature", "agent", "provider", "extension")
MAX_FILES = 64
MAX_FILE = 1024 * 1024
MAX_TOTAL = 4 * 1024 * 1024
MAX_MANIFEST = 64 * 1024
MAX_SIGNATURE = 8 * 1024
VERIFY_TIMEOUT = 10.0
_ID = re.compile(r"[a-z][a-z0-9-]{1,40}")
_VERSION = re.compile(r"\d{1,4}\.\d{1,4}\.\d{1,4}")
_LANG = re.compile(r"translations/[a-z]{2}\.json")
_LINE = re.compile(r"([0-9a-f]{64})  (\S{1,200})")
_TEXT_LIMIT = 300
_HOMEPAGE = re.compile(r"https://[A-Za-z0-9.-]{1,100}(/[A-Za-z0-9._~%/+@-]*)?")
_ICON = re.compile(r"[a-z0-9-]{1,60}")
_COMMAND = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}")
_NEEDS = re.compile(r"(?:maprisma\s*>=\s*)?(\d{1,4}\.\d{1,4}\.\d{1,4})")


@dataclass
class Addon:
    folder: Path
    id: str = ""
    name: str = ""
    version: str = ""
    api: int = 0
    privacy: int = 0
    harnesses: list = field(default_factory=list)
    reads: list = field(default_factory=list)
    sends: list = field(default_factory=list)
    capabilities: list = field(default_factory=list)    # "agent", "provider", "installer", "updater"
    kind: str = "feature"            # feature | agent | provider | extension
    extends: list = field(default_factory=list)         # extension: agent commands it adds facts to
    icon: str = ""                   # an icon name
    summary: str = ""
    description: str = ""
    homepage: str = ""               # https web page of what it brings
    needs: str = ""                  # the oldest Maprisma it works with
    provides: list = field(default_factory=list)        # agent commands / provider keys it brings
    commands: list = field(default_factory=list)        # detect hints: programs on PATH
    paths: list = field(default_factory=list)           # detect hints: "~/…" folders or files
    state: str = "refused"           # refused | verified | loaded | failed
    reason: str = ""
    files: dict = field(default_factory=dict, repr=False)    # path -> verified bytes

    @property
    def brings_code(self) -> bool:
        """An agent described only by agent.toml runs nothing of its own (the Add-ons page says so)."""
        return "addon.py" in self.files or "hooks.py" in self.files


def _refuse(addon: Addon, reason: str) -> Addon:
    addon.state, addon.reason, addon.files = "refused", reason, {}
    return addon


def _trusted(st: os.stat_result, uid: int) -> bool:
    return st.st_uid in (uid, 0) and not st.st_mode & (stat.S_IWGRP | stat.S_IWOTH)


def _read(path: Path, uid: int, limit: int) -> bytes | None:
    """A regular, trusted file's bytes (no links followed), or None."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return None
    with os.fdopen(fd, "rb") as handle:
        st = os.fstat(handle.fileno())
        if not stat.S_ISREG(st.st_mode) or not _trusted(st, uid) or st.st_size > limit:
            return None
        data = handle.read(limit + 1)
    return data if len(data) <= limit else None


def _files(folder: Path, uid: int) -> list | str:
    """Every file below `folder` as a relative path, or the reason it is refused."""
    found, stack = [], [(folder, "")]
    while stack:
        current, prefix = stack.pop()
        try:
            st = os.lstat(current)
        except OSError:
            return "cannot be read"
        if not stat.S_ISDIR(st.st_mode) or not _trusted(st, uid):
            return f"folder {prefix or '.'} is a link, not yours or writable by others"
        try:
            with os.scandir(current) as entries:
                items = list(entries)
        except OSError:
            return "cannot be read"
        for item in items:
            rel = prefix + item.name
            if item.is_symlink():
                return f"{rel} is a link"
            if item.is_dir(follow_symlinks=False):
                if prefix:
                    return f"{rel}: folders may only be one level deep"
                stack.append((Path(item.path), rel + "/"))
            else:
                found.append(rel)
            if len(found) > MAX_FILES:
                return "too many files"
    return sorted(found)


def version_tuple(version: str) -> tuple:
    try:
        return tuple(int(x) for x in version.split("."))
    except (AttributeError, ValueError):
        return ()


def detect_hints(data) -> tuple[list, list]:
    """(commands, paths) from a {commands, paths} object: plain program names and "~/…" paths."""
    detect = data if isinstance(data, dict) else {}
    commands, paths = detect.get("commands"), detect.get("paths")
    commands = [c for c in commands[:8] if isinstance(c, str) and _COMMAND.fullmatch(c)] \
        if isinstance(commands, list) else []
    paths = [p for p in paths[:8] if isinstance(p, str) and p.startswith("~/") and ".." not in p
             and len(p) <= 200 and not any(ord(c) < 32 for c in p)] if isinstance(paths, list) else []
    return commands, paths


def homepage(value) -> str:
    """A plain https address (no user, password or odd characters), else ""."""
    if not isinstance(value, str) or len(value) > 300 or not _HOMEPAGE.fullmatch(value):
        return ""
    return value


def signature_ok(manifest: bytes, signature: bytes, signers: str = ALLOWED_SIGNERS,
                 namespace: str = NAMESPACE) -> bool:
    """Does `signature` sign `manifest` by a key in `signers`? (ssh-keygen -Y verify)"""
    keygen = shutil.which("ssh-keygen")
    if keygen is None:
        return False
    with tempfile.TemporaryDirectory(prefix="ai-agents-map-addon-") as tmp:
        signers_file, signature_file = Path(tmp) / "allowed_signers", Path(tmp) / "SIGNATURE"
        signers_file.write_text(signers + "\n")
        signature_file.write_bytes(signature)
        _out, ok = ai_core.read_bounded_output(
            [keygen, "-Y", "verify", "-f", str(signers_file), "-I", PRINCIPAL, "-n", namespace,
             "-s", str(signature_file)], max_bytes=4096, timeout=VERIFY_TIMEOUT, stdin=manifest)
    return ok


def _texts(value, limit: int = 12) -> list:
    if not isinstance(value, list):
        return []
    return [" ".join(str(v).split())[:_TEXT_LIMIT] for v in value[:limit] if isinstance(v, str) and v.strip()]


def verify(folder: Path, signers: str = ALLOWED_SIGNERS, uid: int | None = None) -> Addon:
    """Check an add-on folder completely; state "verified" with its files, or "refused" with why."""
    uid = os.getuid() if uid is None else uid
    addon = Addon(folder=folder, id=folder.name)
    if not _ID.fullmatch(folder.name):
        return _refuse(addon, "the folder name is not an add-on id")
    names = _files(folder, uid)
    if isinstance(names, str):
        return _refuse(addon, names)
    allowed = {"addon.json", "addon.py", "agent.toml", "hooks.py", "MANIFEST.sha256", "SIGNATURE", "icon.svg"}
    strange = [n for n in names if n not in allowed and not _LANG.fullmatch(n)]
    if strange:
        return _refuse(addon, f"unexpected file {strange[0]}")
    if "SIGNATURE" not in names or "MANIFEST.sha256" not in names:
        return _refuse(addon, "not signed")
    manifest = _read(folder / "MANIFEST.sha256", uid, MAX_MANIFEST)
    signature = _read(folder / "SIGNATURE", uid, MAX_SIGNATURE)
    if manifest is None or signature is None:
        return _refuse(addon, "the signature files cannot be read safely")
    if not signature_ok(manifest, signature, signers):
        reason = "not signed by Uwe Bruckhaus" if shutil.which("ssh-keygen") else \
            "ssh-keygen (OpenSSH) is missing, so the signature cannot be checked"
        if shutil.which("ssh-keygen") and signature_ok(manifest, signature, LEGACY_SIGNERS, LEGACY_NAMESPACE):
            reason = LEGACY_REASON
        return _refuse(addon, reason)
    listed = {}
    for line in manifest.decode("utf-8", "replace").splitlines():
        m = _LINE.fullmatch(line)
        if m is None or m.group(2) in listed:
            return _refuse(addon, "the manifest is malformed")
        listed[m.group(2)] = m.group(1)
    if set(listed) != set(names) - {"MANIFEST.sha256", "SIGNATURE"}:
        return _refuse(addon, "files differ from the signed list")
    files, total = {}, 0
    for name, digest in listed.items():
        data = _read(folder / name, uid, MAX_FILE)
        if data is None:
            return _refuse(addon, f"{name} cannot be read safely")
        total += len(data)
        if total > MAX_TOTAL:
            return _refuse(addon, "too large")
        if hashlib.sha256(data).hexdigest() != digest:
            return _refuse(addon, f"{name} was changed")
        files[name] = data
    try:
        meta = json.loads(files["addon.json"])
    except (KeyError, ValueError):
        return _refuse(addon, "addon.json is missing or not JSON")
    if not isinstance(meta, dict) or meta.get("id") != folder.name:
        return _refuse(addon, "addon.json does not name this add-on")
    if "addon.py" not in files and "agent.toml" not in files and not _built_in_providers(meta):
        return _refuse(addon, "addon.py (or an agent's agent.toml) is missing")
    if "agent.toml" in files and meta.get("kind") != "agent":
        return _refuse(addon, "agent.toml belongs only in an agent add-on")
    if "hooks.py" in files and "agent.toml" not in files:
        return _refuse(addon, "hooks.py belongs beside an agent.toml")
    version, api, privacy = meta.get("version"), meta.get("api"), meta.get("privacy")
    if not isinstance(version, str) or not _VERSION.fullmatch(version):
        return _refuse(addon, "the version is not x.y.z")
    if api != addon_api.API_VERSION:
        return _refuse(addon, f"made for add-on API {api}, this AI Agents Map has {addon_api.API_VERSION}")
    if privacy not in addon_api.PRIVACY:
        return _refuse(addon, "unknown privacy level")
    addon.name = " ".join(str(meta.get("name") or folder.name).split())[:80]
    addon.version, addon.api, addon.privacy = version, api, addon_api.PRIVACY[privacy]
    kind, needs = meta.get("kind", "feature"), meta.get("needs", "")
    if kind not in KINDS:
        return _refuse(addon, "unknown kind")
    m = _NEEDS.fullmatch(needs.strip()) if isinstance(needs, str) and needs else None
    if needs and m is None:
        return _refuse(addon, "needs is not \"maprisma >= x.y.z\"")
    if m is not None and version_tuple(m.group(1)) > version_tuple(MAPRISMA_VERSION):
        return _refuse(addon, f"made for Maprisma {m.group(1)} or newer, this AI Agents Map follows {MAPRISMA_VERSION}")
    addon.kind, addon.needs = kind, m.group(1) if m else ""
    icon = meta.get("icon")
    addon.icon = icon if isinstance(icon, str) and _ICON.fullmatch(icon) else ""
    addon.homepage = homepage(meta.get("homepage"))
    addon.summary = " ".join(str(meta.get("summary") or "").split())[:200]
    addon.description = " ".join(str(meta.get("description") or "").split())[:2000]
    addon.commands, addon.paths = detect_hints(meta.get("detect"))
    provides = meta.get("provides", [])
    addon.provides = [p for p in provides[:40] if isinstance(p, str) and _COMMAND.fullmatch(p)] \
        if isinstance(provides, list) and kind in ("agent", "provider") else []
    extends = meta.get("extends", [])
    addon.extends = [c for c in extends[:40] if isinstance(c, str) and _COMMAND.fullmatch(c)] \
        if isinstance(extends, list) and kind == "extension" else []
    if kind == "extension" and not addon.extends:
        return _refuse(addon, "an extension names no agent in \"extends\"")
    capabilities = meta.get("capabilities", [])
    if not isinstance(capabilities, list) or not all(c in addon_api.CAPABILITIES for c in capabilities):
        return _refuse(addon, "unknown capabilities")
    addon.harnesses, addon.reads, addon.sends = (_texts(meta.get("harnesses")), _texts(meta.get("reads")),
                                                 _texts(meta.get("sends")))
    addon.capabilities = sorted(set(capabilities))
    if "icon.svg" in files:
        import icons
        if not icons.safe_svg(files["icon.svg"]):
            return _refuse(addon, "icon.svg may only hold shapes (no scripts, links, images or styles)")
    refusal = data_only_refusal(kind, files, addon.provides)
    if refusal:
        return _refuse(addon, refusal)
    addon.files, addon.state, addon.reason = files, "verified", ""
    return addon


def built_in_hooks() -> set:
    """The agents whose hooks this app has built in (adapters/<command>.py next to it)."""
    import adapter_data
    return {path.stem for path in adapter_data.ADAPTERS.glob("*.py")}


def data_only_refusal(kind: str, files: dict, provides: list) -> str:
    """Add-ons are data, as in Maprisma (addons.dart dataOnlyRefusal): no add-on code ever runs here. A
    provider add-on's addon.py is accepted only when it names providers the app has built in (they are
    used, its code is not), an agent's hooks.py only when the app has that agent's hooks built in (the
    built-in ones are used). "" when the add-on may load."""
    if "addon.py" not in files and "hooks.py" not in files:
        return ""
    known = {p.key for p in providers.built_in_local() + providers.built_in_online()}
    if kind == "provider" and "hooks.py" not in files and provides and all(p in known for p in provides):
        return ""
    if kind == "agent" and "addon.py" not in files and provides and all(p in built_in_hooks() for p in provides):
        return ""
    return "brings code; this AI Agents Map takes add-ons as data only: needs an update"


def strings(addon: Addon) -> dict:
    """The add-on's translations: language -> {English: translation} (only plain strings)."""
    found = {}
    for name, data in addon.files.items():
        if _LANG.fullmatch(name):
            try:
                table = json.loads(data)
            except ValueError:
                continue
            if isinstance(table, dict):
                found[name[13:15]] = {k: v for k, v in table.items() if isinstance(k, str) and (
                    isinstance(v, str) or isinstance(v, list) and all(isinstance(f, str) for f in v[:3]))}
    return found


def _built_in_providers(meta: dict) -> bool:
    """Maprisma's provider add-ons are data: they name providers the app has built in."""
    provides = meta.get("provides")
    if meta.get("kind") != "provider" or not isinstance(provides, list) or not provides:
        return False
    known = {p.key for p in providers.built_in_local() + providers.built_in_online()}
    return all(isinstance(key, str) and key in known for key in provides)


def load(addon: Addon, hooks: addon_api.Hooks = addon_api.HOOKS) -> Addon:
    """Make a verified add-on's data known: an agent's facts (agent.toml) with its built-in hooks, a
    provider add-on's built-in providers. No add-on code runs (data_only_refusal kept it out)."""
    if addon.state != "verified":
        return addon
    api = addon_api.AddonAPI(addon.id, addon.privacy, strings(addon), hooks, addon.capabilities, addon.kind,
                             addon.extends, icon=addon.icon if addon.icon.startswith("am-addon-") else "")
    try:
        if "agent.toml" in addon.files:
            # The agent's facts, from the verified bytes (never read again from disk): no code runs.
            import tomllib

            import adapter_data
            facts = tomllib.loads(addon.files["agent.toml"].decode("utf-8"))
            # Agent add-ons are data: the agent keeps the hooks the app has built in (an add-on's
            # hooks.py, when there is one, is never run).
            agent_hooks = adapter_data.hooks_module(str(facts.get("command", "")))
            api.add_harness(adapter_data.harness(facts, f"{addon.id}/agent.toml", agent_hooks))
        if addon.kind == "provider":
            # A data add-on: the built-in providers it names, shown through it (an addon.py is never run).
            built_in = {p.key: p for p in providers.built_in_local() + providers.built_in_online()}
            for key in addon.provides:
                providers.add(built_in[key], addon.id)
    except Exception as error:          # a broken add-on must not stop the app
        addon_api.failed(addon.id, error, hooks)
        addon.state, addon.reason = "failed", hooks.errors[addon.id]
        for registry in (hooks.details, hooks.help, hooks.settings):
            registry[:] = [entry for entry in registry if entry[0] != addon.id]
        for command in list(hooks.titles):
            hooks.titles[command] = [entry for entry in hooks.titles[command] if entry[0] != addon.id]
        harnesses.remove_added(addon.id)
        providers.remove_added(addon.id)
        return addon
    hooks.privacy[addon.id] = addon.privacy
    addon.state = "loaded"
    return addon


def find(dirs=(USER_DIR, SYSTEM_DIR)) -> list:
    """Add-on folders; an id in the user folder wins over the same id installed system-wide."""
    seen, found = set(), []
    for base in dirs:
        try:
            entries = sorted(p for p in Path(base).iterdir() if p.name not in seen)
        except OSError:
            continue
        for path in entries[:100]:
            seen.add(path.name)
            found.append(path)
    return found


def load_all(enabled, dirs=(USER_DIR, SYSTEM_DIR), signers: str = ALLOWED_SIGNERS,
             hooks: addon_api.Hooks = addon_api.HOOKS) -> list:
    """Verify every add-on found (to show its state) and load the ones switched on; then the
    modules rebuild their tables, so the add-ons' agents and providers appear everywhere."""
    hooks.clear()
    harnesses.remove_added()
    providers.remove_added()
    result = [verify(folder, signers) for folder in find(dirs)]
    for addon in result:
        # Its own icon (checked in verify): kept in the cache, used for its card and its agent.
        if addon.state == "verified" and "icon.svg" in addon.files:
            import icons
            addon.icon = icons.add_addon_icon(addon.id, addon.files["icon.svg"]) or addon.icon
    # An agent or provider that comes as an add-on is shown only through it (when switched on).
    harnesses.hide_built_in({p for a in result if a.state == "verified" and a.kind == "agent" for p in a.provides})
    providers.hide_built_in({p for a in result if a.state == "verified" and a.kind == "provider" for p in a.provides})
    for addon in result:
        if addon.state == "verified" and addon.id in enabled:
            load(addon, hooks)
    harnesses.changed()
    providers.changed()
    return result
