"""Where the Add pages search the web: a default site per kind, and sites the
user added (kept in the settings, "web_sources"; the one picked last is
"web_source").

- models: Hugging Face, or a site with the same API (a mirror such as
  https://hf-mirror.com): <site>/api/models, <site>/<repo>/resolve/main/<file>;
- mcp:    the official MCP registry, or another registry with its API
  (<site>/v0/servers);
- skills: skills.sh, or a site with its API (<site>/api/search), or a GitHub
  repository (https://github.com/<owner>/<repo>): its folders with a SKILL.md.

Only https, no user:password@, no query or fragment; at most MAX_CUSTOM per kind.
"""

import re
from urllib.parse import urlsplit

import ai_core

KINDS = ("models", "mcp", "skills")
DEFAULTS = {"models": "https://huggingface.co", "mcp": "https://registry.modelcontextprotocol.io",
            "skills": "https://skills.sh"}
NAMES = {"https://huggingface.co": "Hugging Face", "https://registry.modelcontextprotocol.io": "MCP registry",
         "https://skills.sh": "skills.sh"}
MAX_CUSTOM = 12
_GITHUB = re.compile(r"^https://github\.com/([A-Za-z0-9][A-Za-z0-9_.-]{0,99}/[A-Za-z0-9][A-Za-z0-9_.-]{0,99})$")
_HOST = re.compile(r"^[A-Za-z0-9.-]{1,253}(:\d{1,5})?$")
_PATH = re.compile(r"^[A-Za-z0-9._~/-]{0,200}$")


def normalize(kind: str, text: str) -> str:
    """A site address as it is kept; raises ValueError with a short reason."""
    text = (text or "").strip()
    if kind not in KINDS:
        raise ValueError("unknown kind")
    if text and "://" not in text:
        text = "https://" + text
    parts = urlsplit(text)
    if parts.scheme != "https":
        raise ValueError("only https addresses")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("no user, query or # in the address")
    if not parts.netloc or not _HOST.match(parts.netloc) or not _PATH.match(parts.path):
        raise ValueError("not an address")
    path = parts.path.rstrip("/")
    if path.endswith(".git"):
        path = path[:-4]
    url = f"https://{parts.netloc.lower()}{path}"
    if kind == "mcp" and url.endswith("/v0/servers"):
        url = url[:-len("/v0/servers")]
    if kind == "skills" and url.endswith("/api/search"):
        url = url[:-len("/api/search")]
    if parts.netloc.lower() == "github.com" and not (kind == "skills" and _GITHUB.match(url)):
        raise ValueError("a GitHub address works for skills: https://github.com/<owner>/<repo>")
    return url


def github_repo(url: str) -> str:
    """"owner/repo" for a GitHub repository address, else ""."""
    match = _GITHUB.match(url or "")
    return match.group(1) if match else ""


def label(url: str) -> str:
    if url in NAMES:
        return NAMES[url]
    repo = github_repo(url)
    return f"github.com/{repo}" if repo else url[len("https://"):]


def _stored(settings: dict, key: str) -> dict:
    value = settings.get(key)
    return value if isinstance(value, dict) else {}


def custom(kind: str, settings: dict | None = None) -> list[str]:
    settings = ai_core.load_settings() if settings is None else settings
    found = []
    for item in _stored(settings, "web_sources").get(kind, [])[:MAX_CUSTOM] \
            if isinstance(_stored(settings, "web_sources").get(kind), list) else []:
        try:
            url = normalize(kind, str(item))
        except ValueError:
            continue
        if url != DEFAULTS[kind] and url not in found:
            found.append(url)
    return found


def sources(kind: str, settings: dict | None = None) -> list[str]:
    """The default first, then the user's own."""
    return [DEFAULTS[kind]] + custom(kind, settings)


def chosen(kind: str, settings: dict | None = None) -> str:
    settings = ai_core.load_settings() if settings is None else settings
    url = _stored(settings, "web_source").get(kind, "")
    return url if url in sources(kind, settings) else DEFAULTS[kind]


def add(kind: str, text: str) -> str:
    url = normalize(kind, text)
    settings = ai_core.load_settings()
    mine = custom(kind, settings)
    if url != DEFAULTS[kind] and url not in mine:
        if len(mine) >= MAX_CUSTOM:
            raise ValueError("too many sites")
        mine.append(url)
    all_sources = {**_stored(settings, "web_sources"), kind: mine}
    ai_core.update_settings(web_sources=all_sources,
                            web_source={**_stored(settings, "web_source"), kind: url})
    return url


def remove(kind: str, url: str):
    settings = ai_core.load_settings()
    mine = [u for u in custom(kind, settings) if u != url]
    picked = {**_stored(settings, "web_source")}
    if picked.get(kind) == url:
        picked.pop(kind)
    ai_core.update_settings(web_sources={**_stored(settings, "web_sources"), kind: mine}, web_source=picked)


def choose(kind: str, url: str):
    settings = ai_core.load_settings()
    if url in sources(kind, settings):
        ai_core.update_settings(web_source={**_stored(settings, "web_source"), kind: url})
