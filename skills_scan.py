"""Find skill sets on this PC that apps brought along (folders whose
sub-folders hold a SKILL.md), such as /usr/share/<app>/skills, without
running anything. The agents' own skill folders are not offered again.

Where it looks (a few levels deep, skipping big folders that never hold
skills): /usr/share, /usr/local/share, /opt, ~/Applications (extracted
AppImages), ~/.local/share, the global npm folder, Claude plugins, and
the folders added on Setup › Skills (extra_folders).
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

HOME = Path.home()
ROOTS = [(Path("/usr/share"), 4), (Path("/usr/local/share"), 4), (Path("/opt"), 5), (HOME / "Applications", 6),
         (HOME / ".local/share", 4), (HOME / ".local/lib/node_modules", 4), (Path("/usr/lib/node_modules"), 4),
         (HOME / ".claude/plugins", 6)]
SKIP = {"icons", "locale", "man", "doc", "fonts", "themes", "mime", "zoneinfo", "help", "i18n", "info",
        "backgrounds", "wallpapers", "sounds", "pixmaps", "applications", "Trash", "Steam", "flatpak",
        "containers", "mise", "pnpm", "uv", "pipx", "node_modules_cache", "baloo", "akonadi", "gvfs-metadata",
        "recently-used.xbel", "keyrings", "nvim", "tracker3", "ai-agents-map", "kxmlgui5", "gnome-shell",
        "omarchy-plugin-i18n", ".git", "__pycache__", "marketplaces", "site-packages", "dist-packages", "ollama", "lm-studio"}
MAX_DIRS = 20000
MAX_ENTRIES = 2000       # names read from one folder at most


@dataclass
class SkillSet:
    path: str                 # the folder (~ for home)
    skills: list = field(default_factory=list)
    origin: str = ""          # the app it seems to belong to ("omaraw")


def _tilde(path) -> str:
    text, home = str(path), str(HOME)
    return "~" + text[len(home):] if text == home or text.startswith(home + "/") else text


def _skills_in(folder: Path) -> list[str]:
    import ai_core
    try:
        return sorted(e.name for e in ai_core.scandir_head(folder, MAX_ENTRIES)
                      if not e.name.startswith(".") and e.is_dir() and os.path.isfile(os.path.join(e.path, "SKILL.md")))[:200]
    except OSError:
        return []


def origin_of(folder: Path) -> str:
    """The app a skills folder belongs to: the folder above it, or above usr/share."""
    parts = [p for p in folder.parts if p not in ("/", "usr", "share", "local", "opt", "skills", "lib")]
    return parts[-1] if parts else folder.name


def scan(known=()) -> list[SkillSet]:
    """Skill sets not among `known` (real paths of folders agents read already)."""
    import ai_core
    known = {os.path.realpath(os.path.expanduser(k)) for k in known}
    found, seen, visited = [], set(), 0
    try:
        import extra_folders
        added = [(folder, 3) for folder in extra_folders.folders("skills")]
    except (OSError, ValueError):
        added = []
    for root, depth in added + ROOTS:
        stack = [(root, 0)]
        while stack and visited < MAX_DIRS:
            folder, level = stack.pop()
            visited += 1
            try:
                # Bounded: a folder filled with millions of names costs MAX_ENTRIES at most.
                entries = [e for e in ai_core.scandir_head(folder, MAX_ENTRIES) if e.is_dir(follow_symlinks=False)]
            except OSError:
                continue
            names = [e.name for e in entries]
            holds = [n for n in names if os.path.isfile(os.path.join(folder, n, "SKILL.md"))]
            if holds:
                real = os.path.realpath(folder)
                if real not in known and real not in seen:
                    seen.add(real)
                    found.append(SkillSet(_tilde(folder), _skills_in(Path(folder)), origin_of(Path(folder))))
                continue          # a skill set: its skills are not sets of their own
            if level >= depth:
                continue
            for entry in entries:
                if visited + len(stack) >= MAX_DIRS:
                    break             # the queue never holds more than the folders still allowed
                if entry.name in SKIP or entry.name.startswith(".") and entry.name not in (".claude",):
                    continue
                stack.append((Path(entry.path), level + 1))
    return sorted(found, key=lambda s: s.path)
