"""Search the official MCP registry (registry.modelcontextprotocol.io) and
turn a result into an entry (mcp_ctl.normal shape) plus the fields the user
still has to fill in (API keys, folders).

How a result runs:
- npm package   -> npx -y <package>@<version>
- PyPI package  -> uvx <package>==<version>
- OCI image     -> docker run -i --rm [-e VAR …] <image>
- remote        -> its streamable-http (or sse) address and headers
Anyone can publish to the registry: the page shows where a server comes
from (package, image or address, and its repository) before it is added.

The registry's own search takes 10–30 s, its plain list well under a second a page: the list is
saved in the background when the app starts (refresh_list: at most every 12 hours, after the first
time only what changed since) and searched here. Until it is there, the registry is searched.
"""

import json
import os
import re
import shutil
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

API = "https://registry.modelcontextprotocol.io/v0/servers"
TIMEOUT = 45                # its search often takes 10 s or more
MAX_BYTES = 4 * 1024 * 1024


@dataclass
class Need:
    name: str                 # env var or header name, or "arg:<n>" for a package argument
    description: str = ""
    required: bool = False
    secret: bool = False
    value: str = ""           # default
    hint: str = ""            # shown in the empty field (valueHint, e.g. "folder")
    inner: list = field(default_factory=list)   # a value with {placeholders}: the fields for them


@dataclass
class Option:
    kind: str                 # "npm", "pypi", "oci", "remote"
    label: str                # "npm · @scope/pkg 1.2.0" / "remote · host"
    entry: dict               # mcp_ctl.normal() shape, with {VAR} placeholders where needs go
    needs: list = field(default_factory=list)
    runnable: bool = True     # npx/uvx/docker found on this PC


@dataclass
class Server:
    name: str                 # registry name (io.github.x/y)
    short: str                # suggested server key
    title: str
    description: str
    version: str
    repository: str
    options: list = field(default_factory=list)
    updated: str = ""         # ISO time the registry last changed it
    status: str = ""          # "active", "deprecated", "deleted"
    website: str = ""


def publisher(name: str) -> tuple[str, str]:
    """(kind, who) from a registry name. The registry checks the first part when a server is
    published: io.github.<user>/… by a GitHub login of <user>, a reversed domain
    (com.example/…) by that domain's DNS or website. kind: "github", "domain" or ""."""
    space = name.split("/", 1)[0]
    if space.lower().startswith("io.github.") and len(space) > 10:
        return "github", space[10:]
    parts = space.split(".")
    if len(parts) >= 2 and all(re.fullmatch(r"[A-Za-z0-9-]{1,63}", x) for x in parts):
        return "domain", ".".join(reversed(parts))
    return "", ""


def _s(value, limit=300) -> str:
    return " ".join(str(value).split())[:limit] if isinstance(value, (str, int, float)) else ""


def short_name(name: str) -> str:
    last = name.rsplit("/", 1)[-1].lower()
    last = re.sub(r"^(mcp[-_]server[-_]|mcp[-_]|server[-_])|([-_]mcp[-_]server|[-_]mcp|[-_]server)$", "", last)
    return re.sub(r"[^a-z0-9_.-]", "-", last).strip("-")[:64] or "server"


PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_-]{0,63})\}")

# What one registry answer may bring, before anything is built from it (a server becomes an option
# per package or remote, each a form with one field per need): servers per answer, packages and
# remotes per server, arguments and environment variables per package, placeholders per value and
# fields per option. An option over a budget is not offered (cut short, it would lack what it needs).
MAX_SERVERS = 100
MAX_OPTIONS = 6
MAX_ARGUMENTS = 40
MAX_PLACEHOLDERS = 10
MAX_FIELDS = 40


def _inner_needs(item: dict, value: str) -> list:
    """The {placeholders} in a value such as "Bearer {api_key}": one field each (the registry's
    "variables" describe them)."""
    variables = item.get("variables") if isinstance(item.get("variables"), dict) else {}
    needs = []
    for name in list(dict.fromkeys(PLACEHOLDER.findall(value)))[:MAX_PLACEHOLDERS]:
        info = variables.get(name) if isinstance(variables.get(name), dict) else {}
        needs.append(Need(name, _s(info.get("description")) or _s(item.get("description")) or name,
                          info.get("isRequired") is True or item.get("isRequired") is True,
                          info.get("isSecret") is True or item.get("isSecret") is True,
                          _s(info.get("default") or "", 500)))
    return needs


def _needs_env(items) -> list:
    needs = []
    for item in items[:MAX_ARGUMENTS] if isinstance(items, list) else []:
        if isinstance(item, dict) and re.match(r"^[A-Za-z_][A-Za-z0-9_-]{0,127}$", str(item.get("name", ""))):
            value = _s(item.get("default") or item.get("value") or "", 500)
            needs.append(Need(item["name"], _s(item.get("description")), item.get("isRequired") is True,
                              item.get("isSecret") is True, value, inner=_inner_needs(item, value)))
    return needs[:40]


def _flat(needs: list) -> list:
    """The fields to show: a value with {placeholders} asks for those, not for itself."""
    out = []
    for need in needs:
        out += need.inner or [need]
    return list({n.name: n for n in out}.values())


def _arguments(items) -> tuple[list, list]:
    """(args with placeholders, needs) from packageArguments; None when there are more than
    MAX_ARGUMENTS (the command would be cut short)."""
    if isinstance(items, list) and len(items) > MAX_ARGUMENTS:
        return None
    args, needs = [], []
    for number, item in enumerate(items if isinstance(items, list) else []):
        if not isinstance(item, dict):
            continue
        value = _s(item.get("value") or item.get("default") or "", 500)
        name = _s(item.get("name"), 80)
        if not value and item.get("isRequired"):
            key = f"arg:{number}"
            # isSecret kept: a secret field is masked, and the form refuses to put it on a command line.
            needs.append(Need(key, _s(item.get("description")) or _s(item.get("valueHint")) or name, True,
                              item.get("isSecret") is True, hint=_s(item.get("valueHint"), 60)))
            value = "{" + key + "}"
        elif PLACEHOLDER.search(value):
            needs += _inner_needs(item, value)
        elif not value:
            continue
        if item.get("type") == "named" and name:
            args += [name, value] if not name.endswith("=") else [name + value]
        else:
            args.append(value)
    return args, needs


def _env_flag(item) -> bool:
    """A docker runtime argument that only sets an environment variable."""
    if not isinstance(item, dict) or item.get("type") != "named" or item.get("name") not in ("-e", "--env"):
        return False
    value = str(item.get("value") or item.get("default") or "")
    return bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}(=[^\x00-\x1f\x7f]*)?", value))


def option_from_package(package: dict) -> Option | None:
    kind = package.get("registryType")
    ident = _s(package.get("identifier"), 200)
    version = _s(package.get("version"), 60)
    transport = (package.get("transport") or {}).get("type", "stdio")
    # Not "-…": npx, uvx and docker would read it as one of their own options.
    if not ident or ident.startswith("-") or transport != "stdio" or not re.match(r"^[@\w./:-]+$", ident):
        return None
    env_needs = _needs_env(package.get("environmentVariables"))
    arguments = _arguments(package.get("packageArguments"))
    runtime = package.get("runtimeArguments") if isinstance(package.get("runtimeArguments"), list) else []
    if arguments is None or len(runtime) > MAX_ARGUMENTS:
        return None
    args, arg_needs = arguments
    # Every variable gets its value or a {placeholder}; optional ones left empty go again (drop_unfilled).
    env = {n.name: n.value or ("{" + n.name + "}") for n in env_needs}
    if kind == "npm":
        command, base = "npx", ["-y", f"{ident}@{version}" if version and version != "latest" else ident]
    elif kind == "pypi":
        command, base = "uvx", [f"{ident}=={version}" if version and version != "latest" else ident]
    elif kind == "oci":
        command = "docker"
        image = ident if ":" in ident.rsplit("/", 1)[-1] or not version else f"{ident}:{version}"
        # docker's own arguments from runtimeArguments, before the image: only environment
        # variables (-e NAME[=value]). Anything else (-v, -p, --privileged, --network, …) would let a
        # registry entry reach into this PC, so it is left out.
        # -e NAME=value goes in as -e NAME with the value in the environment of docker: a value on the
        # command line (a token, say) would be readable by every user of this PC in /proc/<pid>/cmdline.
        passed = []                     # -e NAME alone: docker passes that variable on
        for item in runtime:
            if _env_flag(item):
                name, sep, value = str(item.get("value") or item.get("default")).partition("=")
                if not sep:
                    passed.append(name)
                    continue
                env.setdefault(name, value)
                arg_needs += _inner_needs(item, value)
        names = list(env) + [n for n in dict.fromkeys(passed) if n not in env]
        base = ["run", "-i", "--rm"] + [x for n in names for x in ("-e", n)] + [image]
    else:
        return None
    fields = _flat(env_needs + arg_needs)
    if len(fields) > MAX_FIELDS:
        return None
    entry = {"transport": "stdio", "command": command, "args": base + args, "env": env, "url": "", "headers": {}}
    label = f"{kind} · {ident}" + (f" {version}" if version else "")
    return Option(kind, label, entry, fields, shutil.which(command) is not None)


def option_from_remote(remote: dict) -> Option | None:
    url = _s(remote.get("url"), 500)
    kind = remote.get("type")
    if not url.startswith("https://") or kind not in ("streamable-http", "sse"):
        return None
    needs = _needs_env(remote.get("headers"))
    headers = {n.name: n.value or ("{" + n.name + "}") for n in needs}
    needs = _flat(needs)
    if len(needs) > MAX_FIELDS:
        return None
    entry = {"transport": "http" if kind == "streamable-http" else "sse", "command": "", "args": [], "env": {},
             "url": url, "headers": headers}
    host = urllib.parse.urlsplit(url).hostname or url
    return Option("remote", f"remote · {host}", entry, needs)


def parse(data: dict) -> list[Server]:
    servers = []
    items = data.get("servers") if isinstance(data, dict) else None
    for item in (items if isinstance(items, list) else [])[:MAX_SERVERS]:
        server = item.get("server") if isinstance(item, dict) else None
        if not isinstance(server, dict) or not isinstance(server.get("name"), str):
            continue
        packages = server.get("packages") if isinstance(server.get("packages"), list) else []
        remotes = server.get("remotes") if isinstance(server.get("remotes"), list) else []
        options = ([o for o in (option_from_package(p) for p in packages[:MAX_OPTIONS] if isinstance(p, dict)) if o]
                   + [o for o in (option_from_remote(r) for r in remotes[:MAX_OPTIONS] if isinstance(r, dict))
                      if o])[:MAX_OPTIONS]
        if not options:
            continue
        repo = server.get("repository") if isinstance(server.get("repository"), dict) else {}
        meta = item.get("_meta") if isinstance(item.get("_meta"), dict) else {}
        official = meta.get("io.modelcontextprotocol.registry/official")
        official = official if isinstance(official, dict) else {}
        website = _s(server.get("websiteUrl"), 300)
        servers.append(Server(server["name"][:200], short_name(server["name"]), _s(server.get("title"), 100),
                              _s(server.get("description"), 400), _s(server.get("version"), 60),
                              _s(repo.get("url"), 300), options, _s(official.get("updatedAt"), 40),
                              _s(official.get("status"), 20), website if website.startswith("https://") else ""))
    return servers


PAGE = 20                    # results shown at a time; "Next 20" / "Previous 20" page through them

# The saved list: {"fetched": ISO time the last download started, "checked": epoch, "servers": {name: item}}.
LIST_FILE = Path.home() / ".cache/ai-agents-map/mcp-registry.json"
LIST_MAX_AGE = 12 * 3600
LIST_MAX_PAGES = 1000         # 100 servers a page, per start (the registry had 30,000+ in 2026-10)
SAVE_EVERY = 10               # pages between saves (a download cut short goes on from there)
LIST_PAGE_TIMEOUT = 90        # some pages take the registry 50 s and more
LIST_MAX_FILE = 96 * 1024 * 1024
LIST_MAX_SERVERS = 100_000    # entries kept (48,000 in 2026-10)
LIST_MAX_ITEM = 128 * 1024    # bytes of one kept entry (as JSON; the largest real one 41 KB); a bigger one is left out
_list_lock = threading.Lock()
_list_cache = None           # (mtime, the loaded list)


SERVER_KEYS = ("name", "title", "description", "version", "repository", "websiteUrl", "packages", "remotes")


def _trim(value, depth: int = 0):
    """A copy cut to what parse() can use: strings to 500 characters (it reads at most that much),
    lists to MAX_ARGUMENTS + 1 items (one more, so a list too long is still refused), objects to
    MAX_FIELDS keys, four levels deep."""
    if isinstance(value, str):
        return value[:500]
    if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
        return value
    if depth >= 4:
        return None
    if isinstance(value, list):
        return [_trim(v, depth + 1) for v in value[:MAX_ARGUMENTS + 1]]
    if isinstance(value, dict):
        return {str(k)[:128]: _trim(v, depth + 1) for k, v in list(value.items())[:MAX_FIELDS]}
    return None


def _kept(item) -> dict | None:
    """Only what parse() reads of a list item, cut to what it uses (_trim); None when nothing is
    left to show or it is still over LIST_MAX_ITEM."""
    server = item.get("server") if isinstance(item, dict) else None
    if not isinstance(server, dict) or not isinstance(server.get("name"), str):
        return None
    server = {k: _trim(server[k][:MAX_OPTIONS] if k in ("packages", "remotes") and isinstance(server[k], list)
                       else server[k]) for k in SERVER_KEYS if k in server}
    server["name"] = server["name"][:200]
    if "repository" in server:
        repo = server["repository"]
        server["repository"] = {"url": repo["url"]} if isinstance(repo, dict) and isinstance(repo.get("url"), str) else {}
    meta = item.get("_meta") if isinstance(item.get("_meta"), dict) else {}
    official = meta.get("io.modelcontextprotocol.registry/official")
    kept = {"server": server, "_meta": {"io.modelcontextprotocol.registry/official": _trim(official)}} \
        if isinstance(official, dict) else {"server": server}
    return kept if _size(kept) <= LIST_MAX_ITEM else None


def _size(item) -> int:
    return len(json.dumps(item, ensure_ascii=False).encode())


def load_list(path: Path | None = None) -> dict | None:
    """The saved list, or None (not there yet, or not readable)."""
    global _list_cache
    path = path or LIST_FILE
    try:
        info = path.stat()
        if info.st_size > LIST_MAX_FILE:
            return None
        if _list_cache is not None and _list_cache[0] == (str(path), info.st_mtime_ns):
            return _list_cache[1]
        data = json.loads(path.read_bytes())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("servers"), dict):
        return None
    _list_cache = ((str(path), info.st_mtime_ns), data)
    return data


def _write_list(path: Path, data: dict):
    """Saved only up to LIST_MAX_FILE (what load_list reads back); OSError above it."""
    text = json.dumps(data, ensure_ascii=False).encode()
    if len(text) > LIST_MAX_FILE:
        raise OSError("the MCP server list is over LIST_MAX_FILE")
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + ".part")
    fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(text)
    os.replace(part, path)


def refresh_list(path: Path | None = None, now: float | None = None, fetch=None) -> bool:
    """Download the registry's list (or what changed since the last time) into path, when it is
    older than LIST_MAX_AGE; one at a time. Saved every SAVE_EVERY pages with where it stopped
    ("resume"), so a download cut short (the app closed, no connection) goes on at the next start.
    False: not needed, already running, or stopped early."""
    now = time.time() if now is None else now
    path = path or LIST_FILE
    if not _list_lock.acquire(blocking=False):
        return False
    try:
        old = load_list(path) or {}
        resume = old.get("resume") if isinstance(old.get("resume"), dict) else None
        if resume is None and old.get("fetched") and now - float(old.get("checked") or 0) < LIST_MAX_AGE:
            return False
        if fetch is None:
            import netfetch
            fetch = netfetch.fetch_json
        if resume is None:
            resume = {"since": str(old.get("fetched") or ""), "cursor": "",
                      "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))}
        # Entries saved by an older version are cut down the same way (_kept).
        kept_before = {k["server"]["name"]: k for item in (old.get("servers") or {}).values()
                       if (k := _kept(item)) is not None}
        data_out = {"fetched": old.get("fetched", ""), "checked": old.get("checked", 0),
                    "servers": kept_before, "resume": dict(resume)}
        servers, cursor = data_out["servers"], str(resume.get("cursor") or "")
        # The list in memory stays within what is saved and read back: LIST_MAX_SERVERS entries,
        # LIST_MAX_FILE bytes (the entries' sizes added up as they come; a full list ends the download).
        sizes = {name: _size(item) + len(name.encode()) + 8 for name, item in servers.items()}
        total, full = sum(sizes.values()), False
        for page in range(1, LIST_MAX_PAGES + 1):
            params = {"limit": 100, "version": "latest"}
            if resume.get("since"):
                params["updated_since"] = resume["since"]
            if cursor:
                params["cursor"] = cursor
            try:
                data = fetch(f"{API}?{urllib.parse.urlencode(params)}", max_bytes=MAX_BYTES, timeout=LIST_PAGE_TIMEOUT,
                             redirects=0, headers={"Accept": "application/json"})
            except Exception:                  # no connection, a timeout, an odd answer: on at the next start
                if page > 1:
                    _write_list(path, data_out)
                return False
            data = data if isinstance(data, dict) else {}
            items = data.get("servers") if isinstance(data.get("servers"), list) else []
            for item in items[:MAX_SERVERS]:           # 100 a page asked for
                kept = _kept(item)
                if kept is None:
                    continue
                name = kept["server"]["name"]
                size = _size(kept) + len(name.encode()) + 8          # with its key in the saved file
                grown = total - sizes.get(name, 0) + size
                if grown > LIST_MAX_FILE - LIST_MAX_ITEM or (name not in servers and len(servers) >= LIST_MAX_SERVERS):
                    full = True
                    break
                servers[name], sizes[name], total = kept, size, grown
            meta = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
            following = meta.get("nextCursor") if isinstance(meta.get("nextCursor"), str) else ""
            if full or not following or following == cursor or not items:
                break
            cursor = following[:300]
            data_out["resume"]["cursor"] = cursor
            if page % SAVE_EVERY == 0:
                _write_list(path, data_out)
        else:
            _write_list(path, data_out)      # LIST_MAX_PAGES this time: on at the next start
            return False
        _write_list(path, {"fetched": resume["started"], "checked": now, "servers": servers})
        return True
    except OSError:
        return False
    finally:
        _list_lock.release()


def search_list(query: str, offset: int = 0, limit: int = PAGE, path: Path | None = None, with_total: bool = False):
    """(servers, cursor for the next ones or "") from the saved list; None when there is none.
    Every word of the query in the name, title or description; names that hold the query first.
    with_total: (servers, cursor, how many match in all)."""
    data = load_list(path)
    if data is None or not data.get("fetched"):
        return None                          # not downloaded in full once yet: the registry is asked
    words = query.lower().split()
    query_text = query.strip().lower()
    matched = []
    for name, item in data["servers"].items():
        server = item.get("server") if isinstance(item, dict) else None
        if not isinstance(server, dict):
            continue
        text = " ".join(str(server.get(k) or "") for k in ("name", "title", "description")).lower()
        if all(word in text for word in words):
            meta = item.get("_meta") if isinstance(item.get("_meta"), dict) else {}
            meta = meta.get("io.modelcontextprotocol.registry/official")
            meta = meta if isinstance(meta, dict) else {}
            if _s(meta.get("status"), 20) not in ("", "active"):
                continue
            matched.append((query_text not in name.lower(), -_epoch(meta.get("updatedAt")), name, item))
    matched.sort(key=lambda m: m[:3])
    # Only the page asked for is built (parse() takes at most MAX_SERVERS at a time).
    page = parse({"servers": [m[3] for m in matched[offset:offset + limit]]})
    following = f"#{offset + limit}" if offset + limit < len(matched) else ""
    return (page, following, len(matched)) if with_total else (page, following)


def _epoch(text) -> float:
    try:
        return time.mktime(time.strptime(str(text)[:19], "%Y-%m-%dT%H:%M:%S"))
    except (ValueError, OverflowError):
        return 0.0


def search(query: str, limit: int = PAGE, base: str = "", cursor: str = "") -> tuple[list[Server], str]:
    """(servers, the cursor for the next page or ""). base: another registry with the same API
    (web_sources), else the official one; cursor: the one a previous page returned. The official
    registry is searched in the saved list when there is one ("#<offset>" cursors)."""
    return search_page(query, limit, base, cursor)[:2]


def search_page(query: str, limit: int = PAGE, base: str = "", cursor: str = "") -> tuple[list[Server], str, int | None]:
    """search() and how many servers match in all: known from the saved list, None from the
    registry itself (its API only says whether there are more)."""
    if not base and (not cursor or cursor.startswith("#")):
        offset = int(cursor[1:]) if cursor[1:].isdigit() else 0
        found = search_list(query, offset, limit, with_total=True)
        if found is not None:
            return found
        cursor = ""
    params = {"search": query.strip()[:100], "limit": limit, "version": "latest"}
    if cursor:
        params["cursor"] = cursor[:300]
    import netfetch
    api = f"{base}/v0/servers" if base else API
    try:
        data = netfetch.fetch_json(f"{api}?{urllib.parse.urlencode(params)}", max_bytes=MAX_BYTES, timeout=TIMEOUT,
                                   redirects=0, headers={"Accept": "application/json"})
    except netfetch.FetchError as error:
        raise ValueError(str(error)) from error
    data = data if isinstance(data, dict) else {}
    meta = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    following = meta.get("nextCursor") if isinstance(meta.get("nextCursor"), str) else ""
    return parse(data), following[:300] if following != cursor else "", None


def fill(entry: dict, values: dict) -> dict:
    """The entry with the {placeholders} replaced by what the user typed."""
    def put(text: str) -> str:
        return re.sub(r"\{([A-Za-z_][A-Za-z0-9_-]*|arg:\d+)\}", lambda m: values.get(m.group(1), m.group(0)), text)
    return {**entry, "args": [put(a) for a in entry["args"]],
            "env": {k: put(v) for k, v in entry["env"].items()},
            "headers": {k: put(v) for k, v in entry["headers"].items()}, "url": put(entry["url"])}


def drop_unfilled(entry: dict, optional) -> dict:
    """Optional fields left empty: the values that still ask for them go (an env var, a header,
    an argument with the flag before it, e.g. -e TOKEN={token})."""
    optional = set(optional)

    def asks(text: str) -> bool:
        return any(m in optional for m in re.findall(r"\{([A-Za-z_][A-Za-z0-9_-]*|arg:\d+)\}", text))

    dropped = {k for k, v in entry["env"].items() if asks(v)}
    args = []
    for arg in entry["args"]:
        if asks(arg) or (entry["command"] == "docker" and arg in dropped and args[-1:] == ["-e"]):
            if args and args[-1].startswith("-") and "=" not in args[-1]:
                args.pop()
            continue
        args.append(arg)
    return {**entry, "args": args, "env": {k: v for k, v in entry["env"].items() if k not in dropped},
            "headers": {k: v for k, v in entry["headers"].items() if not asks(v)}}


def unfilled(entry: dict) -> list[str]:
    text = json.dumps(entry)
    return re.findall(r"\{([A-Za-z_][A-Za-z0-9_-]*|arg:\d+)\}", text)


def secret_in_args(entry: dict, secrets) -> list[str]:
    """The secret fields an entry would put on the command line, where every user of this PC can read
    them (/proc/<pid>/cmdline). They belong in the environment or a header."""
    found = {m for arg in entry["args"] for m in re.findall(r"\{([A-Za-z_][A-Za-z0-9_-]*|arg:\d+)\}", arg)}
    return [name for name in secrets if name in found]
