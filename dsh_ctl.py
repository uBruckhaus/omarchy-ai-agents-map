"""DeepSeek Harness (dsh) settings: the profile patch ~/.dsh/profiles/web/cordis.patch.yml.

The file is a top-level YAML list of patch entries applied over dsh's bundles
(dsh's own web UI writes the same file, and dsh reloads it while it runs):

- an id-targeted entry replaces that entry's whole config:
    - id: llm-pi-ai                       providers (local LLMs among them)
      name: "@deepseek-ai/dsh-llm-pi-ai"
      config: {providers: {llama-local: {api: openai-completions, baseURL: …}}}
    - id: agent-default-model             the model new sessions use
      config: {provider: llama-local, model: gemma-4-E2B-it-Q4_K_M}
- an insert adds new entries (MCP servers):
    - insert:
        - id: mcp-gridcraft
          name: "@deepseek-ai/dsh-mcp-client"
          config: {serverName: gridcraft, transport: stdio, command: …, args: [mcp]}

Only those entries are rewritten; every other entry stays as written, byte
for byte, `!!js` expressions included (they are kept as tagged text, never run).
A local LLM needs no key, but dsh's OpenAI-compatible route refuses a request
without one, so a placeholder Authorization header goes with it (not a secret).
Credentials (~/.dsh/.credentials.yaml) are never read.
"""

import re
from pathlib import Path

HOME = Path.home()
PATCH = HOME / ".dsh/profiles/web/cordis.patch.yml"
SKILLS = HOME / ".dsh/skills"
LLM_ID, LLM_NAME = "llm-pi-ai", "@deepseek-ai/dsh-llm-pi-ai"
DEFAULT_ID, DEFAULT_NAME = "agent-default-model", "@deepseek-ai/dsh-agent-default-model"
MCP_NAME = "@deepseek-ai/dsh-mcp-client"
PLACEHOLDER_AUTH = "Bearer local"
HEADER = ("# Your patch layer for this dsh profile, applied after every bundle layer:\n"
          "# a top-level YAML array of loader patch entries (id-targeted config\n"
          "# overrides, disables, and insert lists; `!!js` expressions allowed).\n")
ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


class Js(str):
    """A `!!js` expression, kept as its text."""


def _yaml():
    import yaml

    class Loader(yaml.SafeLoader):
        pass

    class Dumper(yaml.SafeDumper):
        pass

    def js(loader: yaml.SafeLoader, node: yaml.ScalarNode) -> Js:
        return Js(loader.construct_scalar(node))

    Loader.add_constructor("tag:yaml.org,2002:js", js)
    Dumper.add_representer(Js, lambda dumper, value: dumper.represent_scalar("tag:yaml.org,2002:js", str(value)))
    return yaml, Loader, Dumper


def available() -> bool:
    try:
        import yaml  # noqa: F401
    except ImportError:
        return False
    return True


# --- the file as blocks ---------------------------------------------------------------------------

def split(text: str) -> tuple[str, list[str]]:
    """(the comments before the list, one text block per top-level entry)."""
    lines = (text or "").splitlines(keepends=True)
    header, blocks = [], []
    for line in lines:
        if line.startswith("- ") or line.rstrip() == "-":
            blocks.append(line)
        elif blocks:
            blocks[-1] += line
        elif line.strip() in ("[]", ""):
            continue                      # the empty list dsh starts with
        else:
            header.append(line)
    return "".join(header), blocks


def parse(block: str) -> dict:
    yaml, loader, _dumper = _yaml()
    data = yaml.load(block, Loader=loader)       # noqa: S506 (SafeLoader subclass)
    return data[0] if isinstance(data, list) and data and isinstance(data[0], dict) else {}


def dump(entry: dict) -> str:
    yaml, _loader, dumper = _yaml()
    return yaml.dump([entry], Dumper=dumper, sort_keys=False, default_flow_style=False, allow_unicode=True)


def join(header: str, blocks: list[str]) -> str:
    header = header or HEADER
    if not blocks:
        return header + "[]\n"
    return header + "".join(b if b.endswith("\n") else b + "\n" for b in blocks)


def _find(blocks: list[str], entry_id: str) -> int | None:
    for index, block in enumerate(blocks):
        if parse(block).get("id") == entry_id:
            return index
    return None


# --- providers ------------------------------------------------------------------------------------

def providers(text: str) -> dict:
    _header, blocks = split(text)
    index = _find(blocks, LLM_ID)
    config = parse(blocks[index]).get("config") if index is not None else None
    found = config.get("providers") if isinstance(config, dict) else None
    return found if isinstance(found, dict) else {}


def default_model(text: str) -> tuple[str, str]:
    _header, blocks = split(text)
    index = _find(blocks, DEFAULT_ID)
    config = parse(blocks[index]).get("config") if index is not None else None
    if not isinstance(config, dict):
        return "", ""
    return str(config.get("provider", "")), str(config.get("model", ""))


def provider_entry(label: str, url: str, models: list, context: dict) -> dict:
    rows = []
    for name in models:
        row = {"id": name}
        if context.get(name):
            row["contextWindow"] = int(context[name])
        rows.append(row)
    return {"displayName": label, "api": "openai-completions", "baseURL": url,
            "headers": {"Authorization": PLACEHOLDER_AUTH}, "models": rows}


def connect_text(text: str, provider: str, label: str, url: str, models: list, context: dict, model: str,
                 use_now: bool, remove: bool = False) -> str:
    """The patch with a local LLM's provider set (or removed), and the default
    model pointing at it (use_now) or away from it (remove)."""
    if not ID_RE.match(provider):
        raise ValueError("bad provider id")
    header, blocks = split(text)
    index = _find(blocks, LLM_ID)
    entry: dict = parse(blocks[index]) if index is not None else {"id": LLM_ID, "name": LLM_NAME}
    config = v if isinstance(v := entry.get("config"), dict) else {}
    found = v if isinstance(v := config.get("providers"), dict) else {}
    if remove:
        found.pop(provider, None)
    else:
        found[provider] = provider_entry(label, url, models, context)
    config["providers"] = found
    entry["config"] = config
    if index is None:
        if not remove:
            blocks.append(dump(entry))
    elif not found and set(config) == {"providers"}:
        blocks.pop(index)                 # nothing left of ours: dsh's own defaults again
    else:
        blocks[index] = dump(entry)
    current, _model = default_model(join(header, blocks))
    default_index = _find(blocks, DEFAULT_ID)
    if not remove and use_now and model:
        new = dump({"id": DEFAULT_ID, "name": DEFAULT_NAME, "config": {"provider": provider, "model": model}})
        if default_index is None:
            blocks.append(new)
        else:
            blocks[default_index] = new
    elif remove and current == provider and default_index is not None:
        blocks.pop(default_index)         # back to dsh's own default model
    return join(header, blocks)


# --- MCP servers ------------------------------------------------------------------------------------

def mcp_servers(text: str) -> dict:
    """serverName -> its config, for the MCP clients inserted in the patch."""
    _header, blocks = split(text)
    found = {}
    for block in blocks:
        rows = parse(block).get("insert")
        for row in rows if isinstance(rows, list) else []:
            if isinstance(row, dict) and row.get("name") == MCP_NAME and isinstance(row.get("config"), dict):
                name = str(row["config"].get("serverName", ""))
                if name:
                    found[name] = dict(row["config"])
    return found


def mcp_text(text: str, name: str, config: dict | None) -> str:
    """The patch with MCP server `name` set to config (an insert of its own), or removed."""
    header, blocks = split(text)
    kept = []
    for block in blocks:
        entry = parse(block)
        rows = entry.get("insert")
        if not isinstance(rows, list):
            kept.append(block)
            continue
        others = [r for r in rows if not (isinstance(r, dict) and r.get("name") == MCP_NAME
                                          and isinstance(r.get("config"), dict)
                                          and r["config"].get("serverName") == name)]
        if len(others) == len(rows):
            kept.append(block)            # untouched, as written
        elif others:
            kept.append(dump({**entry, "insert": others}))
    if config is not None:
        entry_id = "mcp-" + re.sub(r"[^a-z0-9_-]", "-", name.lower())[:60]
        kept.append(dump({"insert": [{"id": entry_id, "name": MCP_NAME, "config": {"serverName": name, **config}}]}))
    return join(header, kept)


def mcp_config(entry: dict) -> dict:
    """A normal() MCP entry (mcp_ctl) in dsh's form."""
    if entry["transport"] == "stdio":
        config = {"transport": "stdio", "command": entry["command"], "args": list(entry["args"])}
        if entry.get("env"):
            config["env"] = dict(entry["env"])
        return config
    config = {"transport": "streamable-http", "url": entry["url"]}
    if entry.get("headers"):
        config["headers"] = dict(entry["headers"])
    return config
