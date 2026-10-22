"""Connections that failed, with their hints, kept until the line is removed by hand.

A line drawn on the map that could not be written ("applied": false: the
agent's settings stay as they were), or was written but did not pass the
check right after it (link_check: "applied": true), is drawn dotted: red with
an error among its hints, yellow with warnings only. Its hints stay with the
line (click it) until the user removes the connection on the map, or a new
test of it passes.

failed-links.json in desktop.config_dir() (~/.config/omarchy/ai-agents-map on Omarchy, 0600):
{"agent:hermes|runtime:jan": {"kind": "provider", "applied": true, "when": "…",
  "target": "gemma-4-e2b", "hints": [{"level": "error", "text": "…"}]}}
Only the plugin's own words and capped error messages, never settings values.
"""

import datetime as dt
import json
import os
import re
from pathlib import Path

HOME = Path.home()
KINDS = ("provider", "mcp", "skills")
LEVELS = ("error", "warning", "ok")
MAX_LINKS = 200
MAX_HINTS = 12
MAX_TEXT = 400
ID_RE = re.compile(r"^[a-z]+:[^|\x00-\x1f]{1,300}$")


def store_file() -> Path:
    import desktop
    return desktop.config_dir() / "failed-links.json"


def key(source: str, target: str) -> str:
    return f"{source}|{target}"


def split(link_key: str) -> tuple[str, str]:
    source, _sep, target = link_key.partition("|")
    return source, target


def _clean_hints(hints) -> list:
    found = []
    for hint in hints if isinstance(hints, list) else []:
        if isinstance(hint, dict) and hint.get("level") in LEVELS and isinstance(hint.get("text"), str):
            found.append({"level": hint["level"], "text": hint["text"][:MAX_TEXT]})
    return found[:MAX_HINTS]


def load(path: Path | None = None) -> dict:
    path = path or store_file()
    try:
        with open(path, "rb") as handle:
            data = json.loads(handle.read(1024 * 1024))
    except (OSError, ValueError):
        return {}
    links = {}
    for link_key, entry in list(data.items())[:MAX_LINKS] if isinstance(data, dict) else ():
        source, target = split(str(link_key))
        if not (ID_RE.match(source) and ID_RE.match(target)) or not isinstance(entry, dict):
            continue
        hints = _clean_hints(entry.get("hints"))
        if entry.get("kind") in KINDS and hints:
            links[link_key] = {"kind": entry["kind"], "applied": bool(entry.get("applied")),
                               "when": str(entry.get("when", ""))[:40], "target": str(entry.get("target", ""))[:200],
                               "hints": hints}
    return links


def _save(links: dict, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f".{path.name}.new")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(links, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    os.replace(tmp, path)


def remember(link_key: str, kind: str, applied: bool, hints: list, target: str = "", path: Path | None = None):
    """Keep a failed line's hints (replacing what it had)."""
    path = path or store_file()
    hints = _clean_hints(hints)
    if kind not in KINDS or not hints:
        return
    links = load(path)
    links[link_key] = {"kind": kind, "applied": bool(applied), "target": target[:200],
                       "when": dt.datetime.now().isoformat(timespec="seconds"), "hints": hints}
    _save(dict(list(links.items())[-MAX_LINKS:]), path)


def forget(link_key: str, path: Path | None = None) -> bool:
    """The line was removed by hand (or a new test passed). True when it had hints."""
    path = path or store_file()
    links = load(path)
    if link_key not in links:
        return False
    links.pop(link_key)
    _save(links, path)
    return True


def level(entry: dict) -> str:
    """"error" when any hint is one, else "warning" (or "ok")."""
    levels = {h["level"] for h in entry.get("hints", [])}
    return "error" if "error" in levels else "warning" if "warning" in levels else "ok"


def failing(hints: list) -> bool:
    """Hints that mark the line: an error or a warning."""
    return any(h.get("level") in ("error", "warning") for h in hints)
