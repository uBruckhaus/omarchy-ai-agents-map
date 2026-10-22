"""Find skills on the web (the skills.sh directory) and fetch one into a
local skills folder, without running anything.

- search: skills.sh/api/search -> skill name, its GitHub repository
  ("anthropics/skills") and how often it was installed;
- the skill's folder in that repository: GitHub's file list (one request),
  the folder whose name is the skill's and holds a SKILL.md;
- its files: raw.githubusercontent.com, at most MAX_FILES files and
  MAX_TOTAL bytes, no links, no paths leaving the folder.

The files land through connect.Plan.files_add (preview, backup, Undo).
Skills can hold scripts an agent may run later: the page says so and shows
the repository before anything is fetched.
"""

import json
import re
import urllib.parse
from dataclasses import dataclass

SEARCH = "https://skills.sh/api/search"
TREE = "https://api.github.com/repos/{repo}/git/trees/HEAD?recursive=1"
RAW = "https://raw.githubusercontent.com/{repo}/HEAD/{path}"
TIMEOUT = 20
MAX_FILES = 150
MAX_FILE = 5 * 1024 * 1024
MAX_TOTAL = 30 * 1024 * 1024
_REPO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
_PART = re.compile(r"^[A-Za-z0-9_.@+ -]{1,120}$")


@dataclass
class Skill:
    name: str
    skill_id: str
    repo: str                 # "owner/name" on GitHub
    installs: int = 0


def _get(url: str, limit: int, accept: str = "application/json", site: str = "") -> bytes:
    """Only the three fixed hosts (and a site the user added, web_sources); no redirects."""
    import netfetch
    allowed = ("https://skills.sh/api/", "https://api.github.com/repos/", "https://raw.githubusercontent.com/")
    if site:
        allowed += (site.rstrip("/") + "/api/",)
    if not url.startswith(allowed):
        raise ValueError("not an address this page fetches")
    try:
        return netfetch.fetch(url, max_bytes=limit, timeout=TIMEOUT, redirects=0, headers={"Accept": accept})
    except netfetch.FetchError as error:
        raise ValueError(str(error)) from error


def parse_search(data: dict) -> list[Skill]:
    found = []
    for item in (data.get("skills") if isinstance(data, dict) else None) or []:
        if not isinstance(item, dict):
            continue
        repo, skill_id = str(item.get("source", "")), str(item.get("skillId", ""))
        if _REPO.match(repo) and _ID.match(skill_id):
            installs = v if isinstance(v := item.get("installs"), int) else 0
            found.append(Skill(str(item.get("name") or skill_id)[:100], skill_id, repo, installs))
    return found


def search(query: str, limit: int = 30, base: str = "") -> list[Skill]:
    """base: a site with skills.sh's API, or a GitHub repository (web_sources)."""
    import web_sources
    repo = web_sources.github_repo(base)
    if repo:
        return repo_skills(repo, query, limit)
    params = urllib.parse.urlencode({"q": query.strip()[:100], "limit": limit})
    if base and base != "https://skills.sh":
        return parse_search(json.loads(_get(f"{base}/api/search?{params}", 2 * 1024 * 1024, site=base)))
    return parse_search(json.loads(_get(f"{SEARCH}?{params}", 2 * 1024 * 1024)))


def skills_in_tree(tree: dict, repo: str, query: str = "", limit: int = 30) -> list[Skill]:
    """The skills of a repository's file list: each folder with a SKILL.md (an
    empty query lists them all)."""
    entries = tree.get("tree") if isinstance(tree, dict) else None
    found, words = [], query.lower().split()
    for entry in entries if isinstance(entries, list) else []:
        path = entry.get("path") if isinstance(entry, dict) else None
        if not isinstance(path, str) or entry.get("type") != "blob" or not path.endswith("SKILL.md"):
            continue
        skill_id = path.rsplit("/", 2)[-2] if "/" in path else repo.split("/")[1]
        if not _ID.match(skill_id) or not all(w in path.lower() for w in words):
            continue
        if all(s.skill_id != skill_id for s in found):
            found.append(Skill(skill_id, skill_id, repo))
    return found[:limit]


def repo_skills(repo: str, query: str = "", limit: int = 30) -> list[Skill]:
    if not _REPO.match(repo):
        raise ValueError("not a GitHub repository")
    tree = json.loads(_get(TREE.format(repo=repo), 8 * 1024 * 1024, "application/vnd.github+json"))
    return skills_in_tree(tree, repo, query, limit)


def files_of(tree: dict, skill_id: str) -> tuple[str, list]:
    """(folder in the repository, [(path inside the skill, repo path, size)]) of
    the skill named skill_id; raises ValueError when it is not there or too big."""
    entries = tree.get("tree") if isinstance(tree, dict) else None
    if not isinstance(entries, list):
        raise ValueError("no file list")
    folders = [e["path"][:-len("/SKILL.md")] for e in entries
               if isinstance(e, dict) and isinstance(e.get("path"), str) and e.get("type") == "blob"
               and e["path"].endswith("/SKILL.md") and e["path"].rsplit("/", 2)[-2] == skill_id]
    if not folders and any(isinstance(e, dict) and e.get("path") == "SKILL.md" for e in entries):
        folders = [""]                       # a repository that is one skill
    if not folders:
        raise ValueError(f"no folder {skill_id} with a SKILL.md")
    folder = min(folders, key=len)
    prefix = folder + "/" if folder else ""
    files, total = [], 0
    for entry in entries:
        path = entry.get("path", "") if isinstance(entry, dict) else ""
        if not path.startswith(prefix) or entry.get("type") != "blob":
            continue
        if entry.get("mode") not in ("100644", "100755"):
            continue                         # links and submodules stay out
        inner = path[len(prefix):]
        parts = inner.split("/")
        if any(p in ("", ".", "..") or not _PART.match(p) for p in parts) or len(parts) > 8:
            continue
        size = v if isinstance(v := entry.get("size"), int) else MAX_FILE + 1
        if size > MAX_FILE:
            raise ValueError(f"{inner} is too large")
        total += size
        files.append((inner, path, size, entry.get("mode") == "100755"))
    if len(files) > MAX_FILES or total > MAX_TOTAL:
        raise ValueError("the skill is too large")
    if not any(f[0] == "SKILL.md" for f in files):
        raise ValueError("no SKILL.md")
    return folder, files


def fetch(skill: Skill) -> list:
    """[(path inside the skill, bytes, executable)] of the skill's files."""
    tree = json.loads(_get(TREE.format(repo=skill.repo), 8 * 1024 * 1024, "application/vnd.github+json"))
    _folder, files = files_of(tree, skill.skill_id)
    result = []
    for inner, path, size, executable in files:
        url = RAW.format(repo=skill.repo, path=urllib.parse.quote(path))
        result.append((inner, _get(url, max(size, 1) + 1024, "*/*"), executable))
    return result


def description(files: list) -> str:
    """The SKILL.md description of fetched files."""
    import skills_ctl
    import tempfile
    from pathlib import Path
    text = next((data for inner, data, _x in files if inner == "SKILL.md"), b"")
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "SKILL.md").write_bytes(text[:16384])
        return skills_ctl.description(Path(tmp))
