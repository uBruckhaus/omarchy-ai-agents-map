"""Connect an agent to a local LLM runtime by writing the agent's own settings.

Drawn on the map (agent box → runtime box). Every change is planned first
(plan() returns the old and new text of each file), shown as a diff, then
written atomically with a backup (apply()), and can be undone (undo()).

Supported agents and what they get:

- Hermes   ~/.hermes/config.yaml   `providers: <key>:` (api, models) and, to
                                   use it now, the `model:` block
- Pi       ~/.pi/agent/models.json  `providers.<key>` and settings.json
                                   defaultProvider/defaultModel
- OpenCode ~/.config/opencode/opencode.json `provider.<key>` and `model`

The provider key is "<runtime>-local" (llama-local, ollama-local, …), the
names the tools already use. Local runtimes need no key; "local" stands in
where an agent insists on one.
"""

import datetime as dt
import difflib
import json
import os
import re
import shlex
import subprocess
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path

import harnesses
import providers

HOME = Path.home()
BACKUP_ROOT = HOME / ".local/state/ai-agents-map/backups"


def _rebuild():
    """Tables from the adapters (run again when add-ons add or remove some)."""
    global KEYS, LABELS, SUPPORTED, FIXED
    # The provider id and label agents get for each local LLM (providers.py).
    KEYS = providers.connect_keys()
    LABELS = providers.connect_labels()
    # Agents that can be pointed at a local LLM, and agents tied to one (nothing to set up, nothing
    # else possible) (harnesses.py).
    SUPPORTED = tuple(h.command for h in harnesses.all_harnesses() if h.local_llm)
    FIXED = {h.command: h.fixed_provider for h in harnesses.all_harnesses() if h.fixed_provider}


_rebuild()
providers.on_change(_rebuild)
harnesses.on_change(_rebuild)

MAX_BYTES = 2 * 1024 * 1024
RETIRED_DAYS = 7          # an app's old version, moved into a backup on update, is deleted after this


@dataclass
class Change:
    path: Path
    old: str                 # "" when the file does not exist yet
    new: str


@dataclass
class Plan:
    agent: str
    runtime: str             # runtime key (llamacpp, …)
    provider: str            # provider key written into the agent's settings
    changes: list = field(default_factory=list)
    connected: bool = False  # the agent already had this provider (or MCP server)
    commands: list = field(default_factory=list)       # argv lists run after the files are written
    undo_commands: list = field(default_factory=list)  # run by undo()
    secrets: list = field(default_factory=list)        # values never shown in the preview
    source: str = ""                                    # MCP: the agent the server was copied from
    links_add: list = field(default_factory=list)      # (link, target): symlinks to make (skills)
    links_remove: list = field(default_factory=list)   # (link, target): symlinks to delete
    notes: list = field(default_factory=list)          # what stays as it is (skills: folders read anyway)
    files_add: list = field(default_factory=list)      # (path, bytes, executable): new files (a fetched skill)
    retire: list = field(default_factory=list)         # folders moved into the backup (an app's old version)
    contexts: list = field(default_factory=list)       # (runtime, model, context) set through the local
                                                       # LLM's API after writing (Ollama; not undone)

    def diff(self) -> str:
        parts = []
        for change in self.changes:
            name = str(change.path).replace(str(HOME), "~", 1)
            parts.extend(difflib.unified_diff(change.old.splitlines(), change.new.splitlines(),
                                              name, name, n=2, lineterm=""))
        for link, target in self.links_remove:
            parts.append(f"- {tilde(link)} → {tilde(target)}")
        for link, target in self.links_add:
            parts.append(f"+ {tilde(link)} → {tilde(target)}")
        for path, data, _x in self.files_add:
            parts.append(f"+ {tilde(path)} ({len(data):,} B)")
        for argv in self.commands:
            parts.append("$ " + " ".join(shlex.quote(a) for a in [Path(argv[0]).name, *argv[1:]]))
        for path in self.retire:
            parts.append(f"- {tilde(path)}/ (kept with the backup for {RETIRED_DAYS} days)")
        text = "\n".join(parts)
        for secret in sorted(self.secrets, key=len, reverse=True):
            text = text.replace(secret, "•••")
        return text

    def empty(self) -> bool:
        return not self.changes and not self.commands and not self.links_add and not self.links_remove \
            and not self.files_add and not self.retire


def chat_models(models) -> list:
    """Models an agent can talk to: no speech, tokenizer or embedding models."""
    return [m for m in models if not re.search(r"tts|tokenizer|embed|whisper|rerank", m, re.IGNORECASE)]


def base_url(address: str) -> str:
    host, _sep, port = (address or "127.0.0.1:8080").rpartition(":")
    host = "127.0.0.1" if host in ("", "0.0.0.0", "::") else host
    return f"http://{host}:{port}/v1"


_STAGED = threading.local()     # chain(): texts planned so far, per file (this thread only)


def _read(path: Path) -> str:
    staged = getattr(_STAGED, "texts", None)
    if staged is not None and Path(path) in staged:
        return staged[Path(path)]
    try:
        limit = 8 * 1024 * 1024 if path.name == ".claude.json" else MAX_BYTES
        if path.stat().st_size > limit:
            raise ValueError(f"{path} is too large")
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def _json(text: str) -> dict:
    data = json.loads(text) if text.strip() else {}
    if not isinstance(data, dict):
        raise ValueError("not a JSON object")
    return data


def _dump(data: dict) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


# --- YAML edited as text (Hermes): find a top-level block, so only our block changes ------------

def _top_block(text: str, key: str):
    """(start, end) of a top-level `key:` block in YAML text, or None."""
    match = re.search(rf"^{re.escape(key)}:.*\n?", text, re.MULTILINE)
    if not match:
        return None
    end = match.end()
    for line in re.finditer(r"^.*\n?", text[end:], re.MULTILINE):
        if line.group(0) and not line.group(0).startswith((" ", "\t", "\n", "#")):
            return match.start(), end + line.start()
    return match.start(), len(text)


# --- plan / apply / undo -------------------------------------------------------------------------

def _llm(agent: str):
    h = harnesses.get(agent)
    return h.local_llm if h is not None else None


def paths_for(agent: str) -> list[Path]:
    """The settings files a connection to a local LLM is written to (harnesses.LocalLlm.files)."""
    llm = _llm(agent)
    return [HOME / f[2:] if f.startswith("~/") else Path(f) for f in llm.files] if llm is not None else []


def is_connected(agent: str, runtime: str) -> bool:
    provider = KEYS.get(runtime, "")
    llm = _llm(agent)
    if llm is None:
        return False
    try:
        return bool(llm.connected([_read(p) for p in paths_for(agent)], provider))
    except (OSError, ValueError):
        return False
    except Exception:          # a dsh patch PyYAML cannot read
        return False


def _transform(agent: str, texts: list, runtime, model: str, use_now: bool, remove: bool, context: dict) -> list:
    provider = KEYS[runtime.key]
    url = base_url(runtime.address)
    models = chat_models(runtime.models) or ([model] if model else [])
    # Without a model there is nothing to switch to: "<provider>/" would break the agent.
    use_now = use_now and bool(model)
    return list(_llm(agent).transform(texts, provider, LABELS[runtime.key], url, models, context, model, use_now,
                                      remove))


def is_active(agent: str, runtime: str) -> bool:
    """The agent uses this runtime's provider right now (its current model)."""
    provider = KEYS.get(runtime, "")
    if agent in FIXED:
        return FIXED[agent] == runtime
    llm = _llm(agent)
    if llm is None:
        return False
    try:
        return bool(llm.active([_read(p) for p in paths_for(agent)], provider))
    except (OSError, ValueError):
        return False
    except Exception:          # a dsh patch PyYAML cannot read
        return False


def plan(agent: str, runtime, model: str, use_now: bool, remove: bool = False, context: dict | None = None,
         replaces=None) -> Plan:
    """runtime: inventory.Runtime. replaces: a runtime whose provider goes in the
    same step (a connection moved on the map). Raises ValueError for an agent
    we cannot set up."""
    if agent not in SUPPORTED:
        raise ValueError(agent)
    result = Plan(agent, runtime.key, KEYS[runtime.key], connected=is_connected(agent, runtime.key))
    extra = []
    if model and not remove:
        # What the local LLM brings along (LM Studio: the model's context in its own settings, in
        # the same preview, backup and Undo, and handed to the agent too).
        import llm_engine
        more, extra, contexts = llm_engine.of(runtime).connect_extra(runtime, model)
        context = {**(context or {}), **more}
        result.contexts = contexts
        from i18n import _
        result.notes += [_("{0}: {1} gets a context of {2} tokens, the best for this graphics card")
                         .format(rt.name, name, ctx) for rt, name, ctx in contexts]
    files = paths_for(agent)
    old = [_read(p) for p in files]
    texts = list(old)
    if replaces is not None and replaces.key != runtime.key:
        texts = _transform(agent, texts, replaces, "", False, True, {})
    texts = _transform(agent, texts, runtime, model, use_now, remove, context or {})
    result.changes = [Change(p, o, n) for p, o, n in zip(files, old, texts) if o != n] + extra
    return result


def _write(path: Path, text: str):
    """Agent settings may carry API keys or MCP env/header secrets, so the file is
    always written owner-only: group and other bits of an existing file are dropped
    (a 0644 settings file becomes 0600), the owner's own bits are kept."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = path.stat().st_mode & 0o700 or 0o600
    except FileNotFoundError:
        mode = 0o600
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), mode)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def apply(result: Plan, backup_root: Path = BACKUP_ROOT) -> Path:
    """Back up every file, then write the new texts. Returns the backup folder.
    A file changed since the plan was made is not overwritten."""
    for change in result.changes:
        if _read(change.path) != change.old:
            raise RuntimeError(f"{change.path} changed in the meantime")
    replaced = {link for link, _target in result.links_remove}
    for link, target in result.links_add:
        if (link.exists() or link.is_symlink()) and link not in replaced:
            raise RuntimeError(f"{link} exists already")
    for link, target in result.links_remove:
        if not link.is_symlink() or Path(os.readlink(link)) != Path(target):
            raise RuntimeError(f"{link} changed in the meantime")
    for path, _data, _x in result.files_add:
        if path.exists() or path.is_symlink():
            raise RuntimeError(f"{path} exists already")
    for argv in result.commands:
        _run(argv)
    folder = backup_root / dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    folder.mkdir(parents=True, mode=0o700)
    manifest = []
    for index, change in enumerate(result.changes):
        copy = folder / f"{index}-{change.path.name}"
        if change.path.exists():
            copy.write_text(change.old, encoding="utf-8")
            os.chmod(copy, 0o600)
        manifest.append({"path": str(change.path), "copy": copy.name if change.path.exists() else None,
                         "written": change.new})
    links = {"added": [[str(l), str(t)] for l, t in result.links_add],
             "removed": [[str(l), str(t)] for l, t in result.links_remove]}
    import hashlib
    created_dirs = []
    new_files = [path for path, _data, _x in result.files_add] + \
        [change.path for change in result.changes if not change.path.exists()]
    for path in new_files:
        for parent in reversed(path.parents):
            if not parent.exists() and str(parent) not in created_dirs:
                created_dirs.append(str(parent))
    files = {"added": [[str(p), hashlib.sha256(d).hexdigest()] for p, d, _x in result.files_add],
             "dirs": created_dirs}
    retired = []
    for index, path in enumerate(result.retire):
        retired.append([str(path), f"retired-{index}-{Path(path).name}"])
    (folder / "manifest.json").write_text(json.dumps({"files": manifest, "undo_commands": result.undo_commands,
                                                      "links": links, "created": files, "retired": retired}),
                                          encoding="utf-8")
    os.chmod(folder / "manifest.json", 0o600)
    for change in result.changes:
        _write(change.path, change.new)
    for link, target in result.links_remove:
        link.unlink()
    for link, target in result.links_add:
        link.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(target, link)
    for path, data, executable in result.files_add:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o755 if executable else 0o644)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
    for path, name in retired:
        try:
            os.rename(path, folder / name)       # same file system: instant, nothing copied
        except OSError:
            pass                                 # elsewhere (or in use oddly): it stays where it is
    import llm_engine
    for runtime, model, ctx in result.contexts:
        try:
            llm_engine.of(runtime).set_context(runtime, model, ctx)
        except (OSError, ValueError, RuntimeError):
            pass                                 # the connection stands; the context stays as it was
    return folder


def prune_retired(backup_root: Path = BACKUP_ROOT, days: int = None):
    """Old app versions kept with a backup go after RETIRED_DAYS (Undo then
    puts the settings back, but not the folder)."""
    import shutil
    limit = dt.datetime.now() - dt.timedelta(days=RETIRED_DAYS if days is None else days)
    try:
        backups = list(backup_root.iterdir())
    except OSError:
        return
    for backup in backups:
        try:
            made = dt.datetime.strptime(backup.name, "%Y%m%d-%H%M%S-%f")     # when apply() ran
        except ValueError:
            continue
        if made >= limit:
            continue
        for retired in backup.glob("retired-*"):
            if retired.is_dir() and not retired.is_symlink():
                shutil.rmtree(retired, ignore_errors=True)


def undo(folder: Path) -> list[Path]:
    """Put back what apply() replaced, unless the file was changed again since."""
    restored = []
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    if isinstance(manifest, dict):       # since MCP: files and commands
        for argv in manifest.get("undo_commands", []):
            _run(argv)
        links = manifest.get("links", {})
        for link, target in links.get("added", []):
            link = Path(link)
            if link.is_symlink() and os.readlink(link) == target:
                link.unlink()
                restored.append(link)
        for link, target in links.get("removed", []):
            link = Path(link)
            if not link.exists() and not link.is_symlink():
                os.symlink(target, link)
                restored.append(link)
        import hashlib
        created = manifest.get("created", {})
        for path, digest in created.get("added", []):
            path = Path(path)
            try:
                if not path.is_symlink() and hashlib.sha256(path.read_bytes()).hexdigest() == digest:
                    path.unlink()
                    restored.append(path)
            except OSError:
                continue
        created_dirs = created.get("dirs", [])
        for path, name in manifest.get("retired", []):
            if not os.path.lexists(path) and (folder / name).is_dir():
                os.rename(folder / name, path)
                restored.append(Path(path))
        manifest = manifest.get("files", [])
    else:
        created_dirs = []
    for item in manifest:
        path = Path(item["path"])
        if _read(path) != item["written"]:
            continue            # changed by someone else since: leave it alone
        if item["copy"]:
            _write(path, (folder / item["copy"]).read_text(encoding="utf-8"))
        else:
            path.unlink(missing_ok=True)
        restored.append(path)
    # Folders apply() made for new files, deepest first, once they are empty again.
    for folder_path in sorted(created_dirs, key=len, reverse=True):
        try:
            Path(folder_path).rmdir()          # only when empty
        except OSError:
            pass
    return restored


def _run(argv: list):
    """A settings command (claude mcp …): no shell, short timeout, its error as the message."""
    import ai_core
    out, ok = ai_core.read_bounded_output(argv, max_bytes=64 * 1024, timeout=60, merge_stderr=True)
    if not ok:
        message = out.decode("utf-8", "replace").strip().splitlines()
        raise RuntimeError(f"{Path(argv[0]).name}: {message[-1] if message else 'failed'}")


def tilde(path) -> str:
    text, home = str(path), str(HOME)
    return "~" + text[len(home):] if text == home or text.startswith(home + "/") else text


def chain(builders) -> Plan:
    """Plans that may change the same files (several apps' updates all edit
    ~/.claude.json), each made on top of the ones before it: builders are
    called in order and read what the earlier ones planned. One preview, one
    backup, one Undo, as merge()."""
    plans, first = [], {}
    _STAGED.texts = {}
    try:
        for build in builders:
            item = build()
            for change in item.changes:
                first.setdefault(Path(change.path), change.old)
                _STAGED.texts[Path(change.path)] = change.new
            item.changes = []
            plans.append(item)
        texts = dict(_STAGED.texts)
    finally:
        _STAGED.texts = None
    if not plans:
        raise ValueError("nothing to do")
    plans[0].changes = [Change(path, first[path], text) for path, text in texts.items() if text != first[path]]
    return merge(plans)


def merge(plans: list) -> Plan:
    """Several plans as one (a new MCP server for several agents): one preview,
    one backup, one Undo."""
    if not plans:
        raise ValueError("nothing to do")
    result = Plan(",".join(p.agent for p in plans), "", plans[0].provider)
    for item in plans:
        for change in item.changes:
            if any(c.path == change.path for c in result.changes):
                raise ValueError(f"{change.path} twice")
            result.changes.append(change)
        result.commands += item.commands
        result.undo_commands = item.undo_commands + result.undo_commands
        result.secrets += item.secrets
        result.links_add += item.links_add
        result.links_remove += item.links_remove
        result.files_add += item.files_add
        result.retire += [path for path in item.retire if path not in result.retire]
        result.contexts += [c for c in item.contexts if c not in result.contexts]
        result.notes += [n for n in item.notes if n not in result.notes]
    return result
