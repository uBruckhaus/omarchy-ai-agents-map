"""Find skills on the web (the skills.sh directory) and fetch one into a
local skills folder, without running anything.

- search: skills.sh/api/search -> skill name, its GitHub repository
  ("anthropics/skills") and how often it was installed;
- one snapshot: the repository's HEAD is resolved to a full commit once
  (api.github.com …/commits/HEAD); the file list and every file come from that
  commit, and each file's bytes must have the Git blob id the list names, so an
  install never mixes files of two versions;
- the skill's folder in that repository: GitHub's file list (one request),
  the folder whose name is the skill's and holds a SKILL.md;
- its files: raw.githubusercontent.com, at most MAX_FILES files and
  MAX_TOTAL bytes, no links, no paths leaving the folder.

The files land through connect.Plan.files_add (preview, backup, Undo).
Skills can hold scripts an agent may run later: the page says so and shows
the repository before anything is fetched.
"""

import hashlib
import json
import re
import urllib.parse
from dataclasses import dataclass

SEARCH = "https://skills.sh/api/search"
COMMIT = "https://api.github.com/repos/{repo}/commits/HEAD"
TREE = "https://api.github.com/repos/{repo}/git/trees/{ref}?recursive=1"
RAW = "https://raw.githubusercontent.com/{repo}/{ref}/{path}"
_SHA = re.compile(r"^[0-9a-f]{40}$")
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
    commit: str = ""          # the commit its files were fetched from (fetch)


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
        # fullmatch: match's $ would let a trailing newline through; a bool is not a count.
        if _REPO.fullmatch(repo) and _ID.fullmatch(skill_id):
            installs = v if isinstance(v := item.get("installs"), int) and not isinstance(v, bool) else 0
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
        if not _ID.fullmatch(skill_id) or not all(w in path.lower() for w in words):
            continue
        if all(s.skill_id != skill_id for s in found):
            found.append(Skill(skill_id, skill_id, repo))
    return found[:limit]


def repo_skills(repo: str, query: str = "", limit: int = 30) -> list[Skill]:
    if not _REPO.fullmatch(repo):
        raise ValueError("not a GitHub repository")
    tree = json.loads(_get(TREE.format(repo=repo, ref="HEAD"), 8 * 1024 * 1024, "application/vnd.github+json"))
    return skills_in_tree(tree, repo, query, limit)


def files_of(tree: dict, skill_id: str) -> tuple[str, list]:
    """(folder in the repository, [(path inside the skill, repo path, size, executable, blob id)]) of
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
        if any(p in ("", ".", "..") or not _PART.fullmatch(p) for p in parts) or len(parts) > 8:
            continue
        size = v if isinstance(v := entry.get("size"), int) and not isinstance(v, bool) else MAX_FILE + 1
        if size > MAX_FILE:
            raise ValueError(f"{inner} is too large")
        blob = entry.get("sha")
        if not isinstance(blob, str) or not _SHA.fullmatch(blob):
            raise ValueError(f"{inner}: the file list names no Git blob id")
        total += size
        files.append((inner, path, size, entry.get("mode") == "100755", blob))
    if len(files) > MAX_FILES or total > MAX_TOTAL:
        raise ValueError("the skill is too large")
    if not any(f[0] == "SKILL.md" for f in files):
        raise ValueError("no SKILL.md")
    return folder, files


def blob_id(data: bytes) -> str:
    """Git's id of a file's bytes (SHA-1 of "blob <size>\\0" and the bytes)."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data, usedforsecurity=False).hexdigest()


def resolve(repo: str) -> str:
    """The full commit the repository's HEAD is now."""
    sha = _get(COMMIT.format(repo=repo), 1024, "application/vnd.github.sha").decode("ascii", "replace").strip()
    if not _SHA.fullmatch(sha):
        raise ValueError("GitHub did not name the current commit")
    return sha


def fetch(skill: Skill) -> list:
    """[(path inside the skill, bytes, executable)] of the skill's files, all from one commit (set as
    skill.commit), each checked against its Git blob id before anything is returned."""
    commit = resolve(skill.repo)
    tree = json.loads(_get(TREE.format(repo=skill.repo, ref=commit), 8 * 1024 * 1024, "application/vnd.github+json"))
    if isinstance(tree, dict) and tree.get("truncated") is True:
        raise ValueError("the repository's file list is too long")
    _folder, files = files_of(tree, skill.skill_id)
    result = []
    for inner, path, size, executable, blob in files:
        url = RAW.format(repo=skill.repo, ref=commit, path=urllib.parse.quote(path))
        data = _get(url, max(size, 1) + 1024, "*/*")
        if blob_id(data) != blob:
            raise ValueError(f"{inner} is not the file of commit {commit[:12]}; nothing was added")
        result.append((inner, data, executable))
    skill.commit = commit
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
