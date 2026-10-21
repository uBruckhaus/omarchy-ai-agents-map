"""The author's plugins in the Omarchy Plugin Marketplace, for the Information page's cards.

Read from the marketplace catalog (the file the marketplace site and My Plugins use) only when the
Information page is opened, at most once an hour, and only if it changed (its ETag: GitHub answers
"not changed" without the 15 MB file). Only the author's own entries are kept, in the cache. Without
the network, the cached list is used, else the built-in one (more_plugins.PLUGINS). A plugin the
app knows keeps its glyph and translated one-liner; a new one gets a generic glyph and the start
of its catalog description.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

import more_plugins

CATALOG_URL = "https://raw.githubusercontent.com/omacom/omarchy-plugin-marketplace/main/site/catalog.json"
OWNER = "ubruckhaus"                       # the repositories' owner on GitHub (the author's plugins)
MAX_AGE = 3600
MAX_BYTES = 40 * 1024 * 1024
MAX_PLUGINS = 60
CACHE = Path.home() / ".cache/ai-agents-map/plugin-catalog.json"
NEW_GLYPH = "\U000F03D6"                   # a package, for a plugin the app has no glyph for
REPO = re.compile(r"^https://github\.com/([A-Za-z0-9-]{1,39})/[A-Za-z0-9._-]{1,100}/?$")
ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,99}$")


def owner_of(repo) -> str:
    match = REPO.match(repo or "") if isinstance(repo, str) else None
    return match.group(1).lower() if match else ""


def parse(data) -> list:
    """The author's entries of a catalog: [{id, name, description, version, verified}]."""
    items = data.get("plugins") if isinstance(data, dict) else None
    found = []
    for entry in items if isinstance(items, list) else []:
        if not isinstance(entry, dict) or owner_of(entry.get("repo")) != OWNER:
            continue
        plugin_id, name = entry.get("id"), entry.get("name")
        if not isinstance(plugin_id, str) or not ID.match(plugin_id) or not isinstance(name, str):
            continue
        found.append({"id": plugin_id, "name": name.strip()[:60],
                      "description": str(entry.get("description") or "")[:600],
                      "version": str(entry.get("version") or "")[:30],
                      "verified": entry.get("verificationStatus") == "verified"})
        if len(found) >= MAX_PLUGINS:
            break
    return found


def load(path: Path = CACHE) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8")[:1_000_000])
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) and isinstance(data.get("plugins"), list) else {}


def _save(data: dict, path: Path = CACHE):
    import ai_core
    path.parent.mkdir(parents=True, exist_ok=True)
    ai_core.atomic_write(path, json.dumps(data, indent=1, ensure_ascii=False))


def split_response(raw: bytes) -> tuple[int, dict, bytes]:
    """curl --include output: (status, headers of the last response, body)."""
    status, headers, rest = 0, {}, raw
    while rest.startswith(b"HTTP/"):
        head, sep, body = rest.partition(b"\r\n\r\n")
        if not sep:
            break
        lines = head.decode("latin-1").split("\r\n")
        parts = lines[0].split()
        status = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        headers = {k.strip().lower(): v.strip() for k, _s, v in (line.partition(":") for line in lines[1:]) if k}
        rest = body
    return status, headers, rest


def refresh(fetch=None, path: Path = CACHE, now: float | None = None) -> list:
    """The author's plugins, read again when the cache is older than MAX_AGE (blocking: on a thread).
    Errors keep the cached list."""
    now = time.time() if now is None else now
    cached = load(path)
    if cached and now - float(cached.get("checked", 0) or 0) < MAX_AGE:
        return cached["plugins"]
    if fetch is None:
        import netfetch
        fetch = netfetch.fetch
    headers = {"If-None-Match": cached["etag"]} if cached.get("etag") else {}
    try:
        status, answer, body = split_response(fetch(CATALOG_URL, max_bytes=MAX_BYTES, timeout=40,
                                                    headers=headers, include=True))
    except Exception:          # offline, too large, refused: the cached list (or none)
        return cached.get("plugins", [])
    if status == 304 and cached:
        plugins = cached["plugins"]
    else:
        try:
            plugins = parse(json.loads(body.decode("utf-8", "replace")))
        except ValueError:
            return cached.get("plugins", [])
    try:
        _save({"checked": now, "etag": answer.get("etag", ""), "plugins": plugins}, path)
    except OSError:
        pass
    return plugins


def first_sentence(text: str, limit: int = 90) -> str:
    text = " ".join(text.split())
    cut = re.split(r"(?<=[.!?:])\s", text, maxsplit=1)[0]
    return cut if len(cut) <= limit else cut[:limit - 1].rsplit(" ", 1)[0] + "…"


def cards(plugins: list) -> list:
    """(id, name, glyph, text) for more_plugins.build_cards: known plugins in the built-in order
    with their glyph and one-liner (translated there), then new ones from the catalog."""
    if not plugins:
        return list(more_plugins.PLUGINS)
    known = {p[0]: p for p in more_plugins.PLUGINS}
    listed = {p["id"]: p for p in plugins}
    out = [(pid, listed[pid]["name"] or name, glyph, text) for pid, name, glyph, text in more_plugins.PLUGINS
           if pid in listed]
    out += [(p["id"], p["name"], NEW_GLYPH, first_sentence(p["description"]) or p["name"])
            for p in plugins if p["id"] not in known]
    return out
