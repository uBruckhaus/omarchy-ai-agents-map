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
"""

import json
import re
import shutil
import urllib.parse
from dataclasses import dataclass, field

API = "https://registry.modelcontextprotocol.io/v0/servers"
TIMEOUT = 20
MAX_BYTES = 4 * 1024 * 1024


@dataclass
class Need:
    name: str                 # env var or header name, or "arg:<n>" for a package argument
    description: str = ""
    required: bool = False
    secret: bool = False
    value: str = ""           # default


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


def _s(value, limit=300) -> str:
    return " ".join(str(value).split())[:limit] if isinstance(value, (str, int, float)) else ""


def short_name(name: str) -> str:
    last = name.rsplit("/", 1)[-1].lower()
    last = re.sub(r"^(mcp[-_]server[-_]|mcp[-_]|server[-_])|([-_]mcp[-_]server|[-_]mcp|[-_]server)$", "", last)
    return re.sub(r"[^a-z0-9_.-]", "-", last).strip("-")[:64] or "server"


def _needs_env(items) -> list:
    needs = []
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict) and re.match(r"^[A-Za-z_][A-Za-z0-9_-]{0,127}$", str(item.get("name", ""))):
            needs.append(Need(item["name"], _s(item.get("description")), item.get("isRequired") is True,
                              item.get("isSecret") is True, _s(item.get("default") or item.get("value") or "", 500)))
    return needs[:40]


def _arguments(items) -> tuple[list, list]:
    """(args with placeholders, needs) from packageArguments."""
    args, needs = [], []
    for number, item in enumerate(items if isinstance(items, list) else []):
        if not isinstance(item, dict):
            continue
        value = _s(item.get("value") or item.get("default") or "", 500)
        name = _s(item.get("name"), 80)
        if not value and item.get("isRequired"):
            key = f"arg:{number}"
            needs.append(Need(key, _s(item.get("description")) or _s(item.get("valueHint")) or name, True, False))
            value = "{" + key + "}"
        elif not value:
            continue
        if item.get("type") == "named" and name:
            args += [name, value] if not name.endswith("=") else [name + value]
        else:
            args.append(value)
    return args, needs


def option_from_package(package: dict) -> Option | None:
    kind = package.get("registryType")
    ident = _s(package.get("identifier"), 200)
    version = _s(package.get("version"), 60)
    transport = (package.get("transport") or {}).get("type", "stdio")
    if not ident or transport != "stdio" or not re.match(r"^[@\w./:-]+$", ident):
        return None
    env_needs = _needs_env(package.get("environmentVariables"))
    args, arg_needs = _arguments(package.get("packageArguments"))
    env = {n.name: n.value or ("{" + n.name + "}") for n in env_needs if n.required or n.value}
    if kind == "npm":
        command, base = "npx", ["-y", f"{ident}@{version}" if version and version != "latest" else ident]
    elif kind == "pypi":
        command, base = "uvx", [f"{ident}=={version}" if version and version != "latest" else ident]
    elif kind == "oci":
        command = "docker"
        image = ident if ":" in ident.rsplit("/", 1)[-1] or not version else f"{ident}:{version}"
        base = ["run", "-i", "--rm"] + [x for n in env for x in ("-e", n)] + [image]
    else:
        return None
    entry = {"transport": "stdio", "command": command, "args": base + args, "env": env, "url": "", "headers": {}}
    label = f"{kind} · {ident}" + (f" {version}" if version else "")
    return Option(kind, label, entry, env_needs + arg_needs, shutil.which(command) is not None)


def option_from_remote(remote: dict) -> Option | None:
    url = _s(remote.get("url"), 500)
    kind = remote.get("type")
    if not url.startswith("https://") or kind not in ("streamable-http", "sse"):
        return None
    needs = _needs_env(remote.get("headers"))
    headers = {n.name: n.value or ("{" + n.name + "}") for n in needs if n.required or n.value}
    entry = {"transport": "http" if kind == "streamable-http" else "sse", "command": "", "args": [], "env": {},
             "url": url, "headers": headers}
    host = urllib.parse.urlsplit(url).hostname or url
    return Option("remote", f"remote · {host}", entry, needs)


def parse(data: dict) -> list[Server]:
    servers = []
    for item in (data.get("servers") if isinstance(data, dict) else None) or []:
        server = item.get("server") if isinstance(item, dict) else None
        if not isinstance(server, dict) or not isinstance(server.get("name"), str):
            continue
        options = [o for o in (option_from_package(p) for p in server.get("packages") or [] if isinstance(p, dict))
                   if o] + [o for o in (option_from_remote(r) for r in server.get("remotes") or []
                                        if isinstance(r, dict)) if o]
        if not options:
            continue
        repo = server.get("repository") if isinstance(server.get("repository"), dict) else {}
        servers.append(Server(server["name"][:200], short_name(server["name"]), _s(server.get("title"), 100),
                              _s(server.get("description"), 400), _s(server.get("version"), 60),
                              _s(repo.get("url"), 300), options))
    return servers


def search(query: str, limit: int = 30, base: str = "") -> list[Server]:
    """base: another registry with the same API (web_sources), else the official one."""
    params = urllib.parse.urlencode({"search": query.strip()[:100], "limit": limit, "version": "latest"})
    import netfetch
    api = f"{base}/v0/servers" if base else API
    try:
        data = netfetch.fetch_json(f"{api}?{params}", max_bytes=MAX_BYTES, timeout=TIMEOUT, redirects=0,
                                   headers={"Accept": "application/json"})
    except netfetch.FetchError as error:
        raise ValueError(str(error)) from error
    return parse(data if isinstance(data, dict) else {})


def fill(entry: dict, values: dict) -> dict:
    """The entry with the {placeholders} replaced by what the user typed."""
    def put(text: str) -> str:
        return re.sub(r"\{([A-Za-z_][A-Za-z0-9_-]*|arg:\d+)\}", lambda m: values.get(m.group(1), m.group(0)), text)
    return {**entry, "args": [put(a) for a in entry["args"]],
            "env": {k: put(v) for k, v in entry["env"].items()},
            "headers": {k: put(v) for k, v in entry["headers"].items()}, "url": put(entry["url"])}


def unfilled(entry: dict) -> list[str]:
    text = json.dumps(entry)
    return re.findall(r"\{([A-Za-z_][A-Za-z0-9_-]*|arg:\d+)\}", text)
