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
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

HOME = Path.home()
BACKUP_ROOT = HOME / ".local/state/ai-agents-map/backups"
KEYS = {"llamacpp": "llama-local", "ollama": "ollama-local", "lmstudio": "lmstudio-local", "vllm": "vllm-local"}
LABELS = {"llamacpp": "llama.cpp (local)", "ollama": "Ollama (local)", "lmstudio": "LM Studio (local)",
          "vllm": "vLLM (local)"}
SUPPORTED = ("hermes", "pi", "opencode")
MAX_BYTES = 2 * 1024 * 1024


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
    connected: bool = False  # the agent already had this provider

    def diff(self) -> str:
        parts = []
        for change in self.changes:
            name = str(change.path).replace(str(HOME), "~", 1)
            parts.extend(difflib.unified_diff(change.old.splitlines(), change.new.splitlines(),
                                              name, name, n=2, lineterm=""))
        return "\n".join(parts)


def chat_models(models) -> list:
    """Models an agent can talk to: no speech, tokenizer or embedding models."""
    return [m for m in models if not re.search(r"tts|tokenizer|embed|whisper|rerank", m, re.IGNORECASE)]


def base_url(address: str) -> str:
    host, _sep, port = (address or "127.0.0.1:8080").rpartition(":")
    host = "127.0.0.1" if host in ("", "0.0.0.0", "::") else host
    return f"http://{host}:{port}/v1"


def _read(path: Path) -> str:
    try:
        if path.stat().st_size > MAX_BYTES:
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


# --- Hermes (YAML, edited as text: only our block changes) ---------------------------

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


def hermes_text(text: str, provider: str, label: str, url: str, models: list, context: dict,
                model: str, use_now: bool, remove: bool = False) -> str:
    entry = [f"  {provider}:", f"    name: {label}", f"    api: {url}", "    api_key: local",
             f"    default_model: {model}", "    discover_models: false", "    models:"]
    for name in models:
        if context.get(name):
            entry += [f"      {name}:", f"        context_length: {context[name]}"]
        else:
            entry.append(f"      {name}: {{}}")
    entry_text = "\n".join(entry) + "\n"

    block = _top_block(text, "providers")
    if block is None:
        if not remove:
            text = text + ("" if text.endswith("\n") or not text else "\n") + "providers:\n" + entry_text
    else:
        start, end = block
        body = text[start:end]
        header, _nl, rest = body.partition("\n")
        # Our entry: from "  <provider>:" to the next two-space key.
        rest = re.sub(rf"^  {re.escape(provider)}:.*\n(?:(?:    |\t\t).*\n?|\s*\n)*", "", rest, flags=re.MULTILINE)
        if not remove:
            rest = rest.rstrip("\n") + ("\n" if rest.strip() else "") + entry_text
        header = "providers:" if header.strip() in ("providers: {}", "providers:{}") else header
        new_body = header + "\n" + rest if rest.strip() else ("" if remove else header + "\n")
        text = text[:start] + new_body + text[end:]

    if use_now and not remove:
        block = _top_block(text, "model")
        lines = [f"  default: {model}", f"  provider: {provider}", "  base_url: ''"]
        if block is None:
            text = "model:\n" + "\n".join(lines) + "\n" + text
        else:
            start, end = block
            body = text[start:end]
            for line in lines:
                key = line.split(":", 1)[0].strip()
                if re.search(rf"^  {key}:", body, re.MULTILINE):
                    body = re.sub(rf"^  {key}:.*$", line, body, count=1, flags=re.MULTILINE)
                else:
                    body = body.rstrip("\n") + "\n" + line + "\n"
            text = text[:start] + body + text[end:]
    return text


# --- Pi (JSON) -----------------------------------------------------------------------------

def pi_texts(models_text: str, settings_text: str, provider: str, url: str, models: list, context: dict,
             model: str, use_now: bool, remove: bool = False) -> tuple[str, str]:
    data = _json(models_text)
    providers = data.setdefault("providers", {})
    if remove:
        providers.pop(provider, None)
    else:
        entries = []
        for name in models:
            item = {"id": name, "name": name}
            if context.get(name):
                item["contextWindow"] = context[name]
            entries.append(item)
        providers[provider] = {"baseUrl": url, "api": "openai-completions", "apiKey": "local", "models": entries}
    settings = _json(settings_text)
    if use_now and not remove:
        settings["defaultProvider"] = provider
        settings["defaultModel"] = model
    return _dump(data), (_dump(settings) if use_now and not remove else settings_text)


# --- OpenCode (JSON) ---------------------------------------------------------------------------

def opencode_text(text: str, provider: str, label: str, url: str, models: list, model: str,
                  use_now: bool, remove: bool = False) -> str:
    data = _json(text)
    providers = data.setdefault("provider", {})
    if remove:
        providers.pop(provider, None)
        if not providers:
            data.pop("provider")
    else:
        providers[provider] = {"npm": "@ai-sdk/openai-compatible", "name": label, "options": {"baseURL": url},
                               "models": {name: {"name": name} for name in models}}
        if use_now:
            data["model"] = f"{provider}/{model}"
    return _dump(data)


# --- plan / apply / undo -------------------------------------------------------------------------

def paths_for(agent: str) -> list[Path]:
    return {"hermes": [HOME / ".hermes/config.yaml"],
            "pi": [HOME / ".pi/agent/models.json", HOME / ".pi/agent/settings.json"],
            "opencode": [HOME / ".config/opencode/opencode.json"]}.get(agent, [])


def is_connected(agent: str, runtime: str) -> bool:
    provider = KEYS.get(runtime, "")
    try:
        if agent == "hermes":
            block = _top_block(_read(paths_for(agent)[0]), "providers")
            return bool(block) and re.search(rf"^  {re.escape(provider)}:", _read(paths_for(agent)[0])[block[0]:block[1]],
                                             re.MULTILINE) is not None
        if agent == "pi":
            return provider in _json(_read(paths_for(agent)[0])).get("providers", {})
        if agent == "opencode":
            return provider in _json(_read(paths_for(agent)[0])).get("provider", {})
    except (OSError, ValueError):
        return False
    return False


def plan(agent: str, runtime, model: str, use_now: bool, remove: bool = False, context: dict | None = None) -> Plan:
    """runtime: inventory.Runtime. Raises ValueError for an agent we cannot set up."""
    if agent not in SUPPORTED:
        raise ValueError(agent)
    provider = KEYS[runtime.key]
    url = base_url(runtime.address)
    models = chat_models(runtime.models) or ([model] if model else [])
    context = context or {}
    result = Plan(agent, runtime.key, provider, connected=is_connected(agent, runtime.key))
    files = paths_for(agent)
    old = [_read(p) for p in files]
    if agent == "hermes":
        new = [hermes_text(old[0], provider, LABELS[runtime.key], url, models, context, model, use_now, remove)]
    elif agent == "pi":
        new = list(pi_texts(old[0], old[1], provider, url, models, context, model, use_now, remove))
    else:
        new = [opencode_text(old[0], provider, LABELS[runtime.key], url, models, model, use_now, remove)]
    result.changes = [Change(p, o, n) for p, o, n in zip(files, old, new) if o != n]
    return result


def _write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = path.stat().st_mode & 0o777
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
    (folder / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    os.chmod(folder / "manifest.json", 0o600)
    for change in result.changes:
        _write(change.path, change.new)
    return folder


def undo(folder: Path) -> list[Path]:
    """Put back what apply() replaced, unless the file was changed again since."""
    restored = []
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    for item in manifest:
        path = Path(item["path"])
        if _read(path) != item["written"]:
            continue            # changed by someone else since: leave it alone
        if item["copy"]:
            _write(path, (folder / item["copy"]).read_text(encoding="utf-8"))
        else:
            path.unlink(missing_ok=True)
        restored.append(path)
    return restored
