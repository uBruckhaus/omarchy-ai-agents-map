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


def _inner_needs(item: dict, value: str) -> list:
    """The {placeholders} in a value such as "Bearer {api_key}": one field each (the registry's
    "variables" describe them)."""
    variables = item.get("variables") if isinstance(item.get("variables"), dict) else {}
    needs = []
    for name in dict.fromkeys(PLACEHOLDER.findall(value)):
        info = variables.get(name) if isinstance(variables.get(name), dict) else {}
        needs.append(Need(name, _s(info.get("description")) or _s(item.get("description")) or name,
                          info.get("isRequired") is True or item.get("isRequired") is True,
                          info.get("isSecret") is True or item.get("isSecret") is True,
                          _s(info.get("default") or "", 500)))
    return needs


def _needs_env(items) -> list:
    needs = []
    for item in items if isinstance(items, list) else []:
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
    """(args with placeholders, needs) from packageArguments."""
    args, needs = [], []
    for number, item in enumerate(items if isinstance(items, list) else []):
        if not isinstance(item, dict):
            continue
        value = _s(item.get("value") or item.get("default") or "", 500)
        name = _s(item.get("name"), 80)
        if not value and item.get("isRequired"):
            key = f"arg:{number}"
            needs.append(Need(key, _s(item.get("description")) or _s(item.get("valueHint")) or name, True, False,
                              hint=_s(item.get("valueHint"), 60)))
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
    args, arg_needs = _arguments(package.get("packageArguments"))
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
        runtime, runtime_needs = _arguments([a for a in package.get("runtimeArguments") or []
                                             if _env_flag(a)])
        arg_needs += runtime_needs
        base = ["run", "-i", "--rm"] + [x for n in env for x in ("-e", n)] + runtime + [image]
    else:
        return None
    entry = {"transport": "stdio", "command": command, "args": base + args, "env": env, "url": "", "headers": {}}
    label = f"{kind} · {ident}" + (f" {version}" if version else "")
    return Option(kind, label, entry, _flat(env_needs + arg_needs), shutil.which(command) is not None)


def option_from_remote(remote: dict) -> Option | None:
    url = _s(remote.get("url"), 500)
    kind = remote.get("type")
    if not url.startswith("https://") or kind not in ("streamable-http", "sse"):
        return None
    needs = _needs_env(remote.get("headers"))
    headers = {n.name: n.value or ("{" + n.name + "}") for n in needs}
    needs = _flat(needs)
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
        meta = item.get("_meta") if isinstance(item.get("_meta"), dict) else {}
        official = meta.get("io.modelcontextprotocol.registry/official")
        official = official if isinstance(official, dict) else {}
        website = _s(server.get("websiteUrl"), 300)
        servers.append(Server(server["name"][:200], short_name(server["name"]), _s(server.get("title"), 100),
                              _s(server.get("description"), 400), _s(server.get("version"), 60),
                              _s(repo.get("url"), 300), options, _s(official.get("updatedAt"), 40),
                              _s(official.get("status"), 20), website if website.startswith("https://") else ""))
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


def drop_unfilled(entry: dict, optional) -> dict:
    """Optional fields left empty: the values that still ask for them go (an env var, a header,
    an argument with the flag before it, e.g. -e TOKEN={token})."""
    optional = set(optional)

    def asks(text: str) -> bool:
        return any(m in optional for m in re.findall(r"\{([A-Za-z_][A-Za-z0-9_-]*|arg:\d+)\}", text))

    args = []
    for arg in entry["args"]:
        if asks(arg):
            if args and args[-1].startswith("-") and "=" not in args[-1]:
                args.pop()
            continue
        args.append(arg)
    return {**entry, "args": args, "env": {k: v for k, v in entry["env"].items() if not asks(v)},
            "headers": {k: v for k, v in entry["headers"].items() if not asks(v)}}


def unfilled(entry: dict) -> list[str]:
    text = json.dumps(entry)
    return re.findall(r"\{([A-Za-z_][A-Za-z0-9_-]*|arg:\d+)\}", text)
