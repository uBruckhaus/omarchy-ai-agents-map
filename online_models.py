"""The online models an agent can switch between on the map, and the switch itself as a
connect.Plan (backup, Undo).

Where each agent says which models it has (checked 2026-10-20), nothing is guessed:

- Claude Code: the aliases its own `claude --help` names for --model ('fable', 'opus', 'sonnet');
  the choice is "model" in ~/.claude/settings.json.
- Codex: ~/.codex/models_cache.json (Codex keeps it), the models it lists (visibility "list");
  the choice is the top-level `model = "…"` in ~/.codex/config.toml.

An agent's adapter names its switcher (models.reader, or the models_choices/models_plan hooks);
the readers above are the app's. The model in use is always among the choices, so the map never hides it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

HOME = Path.home()
CLAUDE_ALIASES = ("fable", "opus", "sonnet")
MAX_CHOICES = 12
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,99}")


def _switcher(command: str):
    """(choices(current, home), plan(model, home)) from the agent's adapter (its models fact)."""
    import harnesses
    harness = harnesses.get(command)
    return harness.models if harness is not None else None


def supported(command: str) -> bool:
    return _switcher(command) is not None


def _read(path: Path, limit: int = 4 * 1024 * 1024) -> str:
    try:
        with open(path, "rb") as handle:
            return handle.read(limit).decode("utf-8", "replace")
    except OSError:
        return ""


def codex_models(home: Path | None = None) -> list:
    data_text = _read((home or HOME) / ".codex/models_cache.json")
    try:
        data = json.loads(data_text) if data_text else {}
    except ValueError:
        return []
    rows = data.get("models") if isinstance(data, dict) else None
    return [r["slug"] for r in rows or () if isinstance(r, dict) and r.get("visibility") == "list"
            and isinstance(r.get("slug"), str) and NAME.fullmatch(r["slug"])][:MAX_CHOICES]


def claude_choices(current: str = "", home: Path | None = None) -> list:
    return list(CLAUDE_ALIASES)


def codex_choices(current: str = "", home: Path | None = None) -> list:
    return codex_models(home)


def choices(command: str, current: str = "", home: Path | None = None) -> list:
    """The models the agent can be switched to (the one in use first when it is not listed)."""
    switcher = _switcher(command)
    found = list(switcher[0](current, home))[:MAX_CHOICES] if switcher is not None else []
    if current and current not in found and NAME.fullmatch(current):
        found.insert(0, current)
    return found


def _codex_text(text: str, model: str) -> str:
    """config.toml with the top-level `model` set (before the first table, where TOML wants it)."""
    line = f'model = "{model}"'
    first_table = re.search(r"^\s*\[", text, re.MULTILINE)
    head, tail = (text[:first_table.start()], text[first_table.start():]) if first_table else (text, "")
    if re.search(r"^model\s*=.*$", head, re.MULTILINE):
        head = re.sub(r"^model\s*=.*$", line, head, count=1, flags=re.MULTILINE)
    else:
        head = line + "\n" + head
    return head + tail


def claude_plan(model: str, home: Path):
    """(file, old text, new text, provider): "model" in ~/.claude/settings.json."""
    import connect
    path = home / ".claude/settings.json"
    old = _read(path)
    data = connect._json(old) if old else {}
    data["model"] = model
    return path, old, connect._dump(data), "anthropic"


def codex_plan(model: str, home: Path):
    """(file, old text, new text, provider): the top-level model in ~/.codex/config.toml."""
    path = home / ".codex/config.toml"
    old = _read(path)
    return path, old, _codex_text(old, model), "openai"


def plan(command: str, model: str, home: Path | None = None):
    """The change that makes `model` the agent's model (a connect.Plan: preview, backup, Undo)."""
    import connect
    switcher = _switcher(command)
    if switcher is None or not NAME.fullmatch(model or ""):
        raise ValueError(model)
    path, old, new, provider = switcher[1](model, home or HOME)
    result = connect.Plan(command, "online", provider, connected=True)
    if new != old:
        result.changes.append(connect.Change(Path(path), old, new))
    return result
