"""The map's nodes and edges, from agents, their details and the inventory.

Four columns, left to right:

    0 MCP servers and skill sets  ←  1 agents  →  2 LLM providers  →  3 models

Providers are the cloud services the agents talk to and the local runtimes
(llama.cpp, Ollama, …). Pure data: map_view.py draws it, tests check it.
"""

from dataclasses import dataclass, field

# The cloud service behind each agent unless its settings point elsewhere.
CLOUD = {
    "claude": "Anthropic", "codex": "OpenAI", "copilot": "GitHub Copilot", "cursor-agent": "Cursor",
    "gemini": "Google", "grok": "xAI", "muse": "Meta", "openclaw": "OpenClaw",
}
# Provider ids in agent settings ("anthropic/claude-…", pi's defaultProvider, Hermes's provider).
PROVIDER_NAMES = {
    "anthropic": "Anthropic", "openai": "OpenAI", "openai-codex": "OpenAI", "google": "Google",
    "gemini": "Google", "github-copilot": "GitHub Copilot", "xai": "xAI", "nous": "Nous Research",
    "openrouter": "OpenRouter", "groq": "Groq", "mistral": "Mistral", "deepseek": "DeepSeek",
    "fireworks": "Fireworks", "together": "Together AI", "opencode": "OpenCode Zen", "zai": "Z.ai",
}
MAX_MODELS_PER_PROVIDER = 10

# Categories the map can show or hide, in toolbar order, and what each needs.
LAYERS = ("agents", "online", "local", "models", "mcp", "skills")
LAYER_OF = {"agent": "agents", "cloud": "online", "runtime": "local", "model": "models", "more": "models",
            "mcp": "mcp", "add-mcp": "mcp", "skills": "skills", "add-skills": "skills", "add-model": "models",
            "add-folders": "models"}
REQUIRES = {"models": ("online", "local"), "mcp": ("agents",), "skills": ("agents",)}   # any one of them


def effective_layers(enabled) -> set:
    """The categories actually shown: those switched on whose needs are met."""
    enabled = {layer for layer in enabled if layer in LAYERS}
    return {layer for layer in enabled
            if not REQUIRES.get(layer) or any(need in enabled for need in REQUIRES[layer])}


def hidden_by_layers(graph, enabled, hidden_ids=()) -> set:
    """Node ids the switched-off categories hide, plus hidden_ids (boxes
    unticked in a column's list). A model (or a folder's "+ Model" box) also
    goes when none of the providers serving it is shown, an MCP server or
    skill set when none of the agents using it is (unused ones stay)."""
    shown = effective_layers(enabled)
    hidden = {n.id for n in graph.nodes if LAYER_OF.get(n.kind) not in shown or n.id in hidden_ids}
    for layer, kinds, side in (("models", ("serves",), "source"), ("mcp", ("mcp",), "source"),
                               ("skills", ("skills",), "source")):
        for node in graph.nodes:
            if LAYER_OF.get(node.kind) == layer and node.id not in hidden:
                linked = [getattr(e, side) for e in graph.edges if e.target == node.id and e.kind in kinds]
                if linked and all(p in hidden for p in linked):
                    hidden.add(node.id)
    for node in graph.nodes:
        if node.kind == "add-model" and node.owners and all(o in hidden for o in node.owners):
            hidden.add(node.id)
    return hidden


SCOPES = ("mine", "all")    # My setup · + Available


def scope_extras(agent_list, inventory) -> set:
    """Card and box ids "My setup" leaves out: agents and local LLMs that are not
    installed, MCP servers no agent uses (all of them stay one click away)."""
    return ({f"agent:{a.command}" for a in agent_list if not a.installed}
            | {f"runtime:{rt.key}" for rt in inventory.runtimes if not rt.installed}
            | {f"mcp:{m.name}" for m in inventory.mcp if not m.agents})


FOLDERS_BOX = "add:folders"


def idle_folders(graph) -> dict:
    """Model folders (of local LLMs) none of whose models an agent uses or runs:
    {folder: ids of its boxes, "+ Model" and "+n more" included}."""
    folders = {}
    for node in graph.column(3):
        if node.group and node.kind in ("model", "more", "add-model"):
            folders.setdefault(node.group, []).append(node)
    return {folder: {n.id for n in nodes} for folder, nodes in folders.items()
            if not any(n.active for n in nodes)}


def listable(graph, column: int) -> list:
    """The boxes a column's heading list can tick: everything but the "+" boxes."""
    return [n for n in graph.column(column) if not n.kind.startswith("add-")]


@dataclass
class Node:
    id: str
    column: int
    label: str
    detail: str = ""
    kind: str = ""           # agent, cloud, runtime, model, mcp, skills
    icon: str = ""
    icon_font: str = ""
    dim: bool = False        # drawn faded
    status: str = ""         # local runtimes: "running", "stopped" (installed) or "missing"
    group: str = ""          # models: the folder they live in ("" = an online provider's)
    owners: tuple = ()       # "+ Model" boxes: the local LLM node ids using that folder
    active: bool = False     # models: an agent's model, or one an open agent runs


@dataclass
class Edge:
    source: str
    target: str
    kind: str                # uses / available (agent→provider), serves (provider→model), mcp, skills, default,
                             # failed (drawn but not written: only its hints are kept, link_hints)
    failed: str = ""         # "error" / "warning": the connection did not pass its check (dotted red / yellow)


@dataclass
class Graph:
    nodes: list = field(default_factory=list)
    edges: list = field(default_factory=list)

    def add(self, node: Node) -> Node:
        for existing in self.nodes:
            if existing.id == node.id:
                return existing
        self.nodes.append(node)
        return node

    def link(self, source: str, target: str, kind: str):
        if not any(e.source == source and e.target == target for e in self.edges):
            self.edges.append(Edge(source, target, kind))

    def column(self, index: int) -> list:
        return [n for n in self.nodes if n.column == index]


def runtime_status(rt) -> str:
    return "running" if rt.running else "stopped" if rt.installed else "missing"


def provider_name(raw: str) -> str:
    raw = (raw or "").strip()
    return PROVIDER_NAMES.get(raw.lower(), raw)


def split_model(model: str) -> tuple[str, str]:
    """"qwen (llama-local)" -> ("qwen", "llama-local"); "anthropic/claude" -> ("claude", "anthropic")."""
    model = (model or "").strip()
    if model.endswith(")") and " (" in model:
        name, _sep, provider = model[:-1].rpartition(" (")
        return name, provider
    if "/" in model and not model.startswith("/"):
        provider, _sep, name = model.partition("/")
        return name, provider
    return model, ""


def build(agent_list, details: dict, inventory, labels: dict | None = None, failures: dict | None = None,
          running: dict | None = None) -> Graph:
    """agent_list: agents.Agent (installed ones are drawn); details: command ->
    agent_details.Details; labels: functions n -> translated text ("skills", "more");
    running: command -> the model of each open instance ("" = its settings')."""
    labels = {"skills": "{0} skills".format, "more": "+{0} more".format, "add_mcp": "+ MCP server",
              "add_skills": "+ Skills", "add_model": "+ Model", "running": lambda n: "running" if n == 1
              else f"{n} running", "in_use": "in use · {0}".format,
              **(labels or {})}
    running = running or {}
    graph = Graph()
    runtimes = {rt.name: rt for rt in inventory.runtimes}
    live = {}                # model node id -> names of the agents running with it now

    for agent in agent_list:
        if not agent.installed:
            continue
        aid = f"agent:{agent.command}"
        instances = running.get(agent.command, [])
        if instances:
            graph.add(Node(aid, 1, agent.name, " · ".join(filter(None, [agent.version, labels["running"](len(instances))])),
                           "agent", agent.icon, agent.icon_font, status="running"))
        else:
            graph.add(Node(aid, 1, agent.name, agent.version, "agent", agent.icon, agent.icon_font))
        info = details.get(agent.command)
        model, model_provider = split_model(info.model if info else "")

        # Local runtimes the agent's providers point at.
        local_names = {}
        # Active providers first, so an agent's line to a runtime is solid when any is in use.
        for provider in sorted(info.providers if info else [], key=lambda p: not p.active):
            if provider.local and provider.runtime in runtimes:
                local_names[provider.name] = provider.runtime
                rt = runtimes[provider.runtime]
                rid = f"runtime:{rt.key}"
                graph.add(Node(rid, 2, rt.name, rt.address, "runtime", status=runtime_status(rt)))
                graph.link(aid, rid, "uses" if provider.active else "available")

        # The cloud service: the configured provider when it is not local, else the agent's own.
        cloud = ""
        if model_provider and model_provider not in local_names:
            cloud = provider_name(model_provider)
        elif not model_provider:
            cloud = CLOUD.get(agent.command, "")
        if agent.command in CLOUD and not local_names:
            cloud = cloud or CLOUD[agent.command]
        if cloud:
            cid = f"cloud:{cloud.lower()}"
            graph.add(Node(cid, 2, cloud, "", "cloud"))
            graph.link(aid, cid, "uses")
            if model:
                mid = f"model:{cid}:{model.lower()}"
                graph.add(Node(mid, 3, model, "", "model"))
                graph.link(cid, mid, "serves")
                graph.link(aid, mid, "default")
        elif model and model_provider in local_names:
            rt = runtimes[local_names[model_provider]]
            graph.link(aid, f"model:runtime:{rt.key}:{model.lower()}", "default")

        # The model each open instance runs with: its own --model, else the settings'.
        for chosen in instances:
            name, provider = split_model(chosen) if chosen else (model, model_provider)
            if not name:
                continue
            if provider in local_names:
                mid = f"model:runtime:{runtimes[local_names[provider]].key}:{name.lower()}"
            else:
                service = provider_name(provider) if provider else cloud
                if not service:
                    continue
                cid = f"cloud:{service.lower()}"
                graph.add(Node(cid, 2, service, "", "cloud"))
                mid = f"model:{cid}:{name.lower()}"
                graph.add(Node(mid, 3, name, "", "model"))
                graph.link(cid, mid, "serves")
            if agent.name not in live.setdefault(mid, []):
                live[mid].append(agent.name)

        for name in inventory.agent_mcp.get(agent.command, []):
            server = next((s for s in inventory.mcp if s.name == name), None)
            mid = f"mcp:{name}"
            graph.add(Node(mid, 0, name, server.target if server else "", "mcp"))
            graph.link(aid, mid, "mcp")
        for path in inventory.agent_skills.get(agent.command, []):
            skill_set = next((s for s in inventory.skill_sets if s.path == path), None)
            sid = f"skills:{path}"
            count = len(skill_set.skills) if skill_set else 0
            graph.add(Node(sid, 0, path, labels["skills"](count), "skills"))
            graph.link(aid, sid, "skills")

    # Every local runtime, installed or not (something to connect agents to),
    # with the models of the installed ones.
    for rt in inventory.runtimes:
        rid = f"runtime:{rt.key}"
        graph.add(Node(rid, 2, rt.name, rt.address if rt.installed else "", "runtime", status=runtime_status(rt)))
        shown = rt.models[:MAX_MODELS_PER_PROVIDER] if rt.installed else []
        for model in shown:
            mid = f"model:{rid}:{model.lower()}"
            graph.add(Node(mid, 3, model, "", "model", group=rt.models_dir or rt.name))
            graph.link(rid, mid, "serves")
        if len(rt.models) > len(shown):
            mid = f"more:{rid}"
            graph.add(Node(mid, 3, labels["more"](len(rt.models) - len(shown)), "", "more", dim=True,
                           group=rt.models_dir or rt.name))
            graph.link(rid, mid, "serves")

    # A box per model folder to download a model into it (the local LLMs sharing it pick one).
    folders = {}
    for rt in inventory.runtimes:
        if rt.installed:
            folders.setdefault(rt.models_dir or rt.name, []).append(f"runtime:{rt.key}")
    for folder, owners in folders.items():
        graph.add(Node(f"add:model:{folder}", 3, labels["add_model"], folder, "add-model", dim=True,
                       group=folder, owners=tuple(owners)))

    # MCP servers no agent uses (any more): still listed, to connect again.
    for server in inventory.mcp:
        if not server.agents:
            graph.add(Node(f"mcp:{server.name}", 0, server.name, labels.get("unused", "not used"), "mcp", dim=True))

    # A box to add a new MCP server (on the map: opens the add page).
    graph.add(Node("add:mcp", 0, labels["add_mcp"], "", "add-mcp", dim=True))
    graph.add(Node("add:skills", 0, labels["add_skills"], "", "add-skills", dim=True))

    for node in graph.nodes:
        if node.id in live:
            node.status = "running"
            node.detail = labels["in_use"](", ".join(live[node.id]))
    chosen = {e.target for e in graph.edges if e.kind == "default"}
    for node in graph.nodes:
        node.active = node.kind == "model" and (node.id in chosen or node.id in live)
    idle = idle_folders(graph)
    if idle and "folders" in labels:
        # My setup folds these away behind one box (ai_popup.scope_hidden); a click unfolds them.
        graph.add(Node(FOLDERS_BOX, 3, labels["folders"](len(idle)), "", "add-folders", dim=True, group=FOLDERS_BOX))

    # A default model that points at a model the runtime does not list: drop the link.
    ids = {n.id for n in graph.nodes}
    graph.edges = [e for e in graph.edges if e.source in ids and e.target in ids]
    mark_failures(graph, failures or {})
    order_columns(graph)
    return graph


def mark_failures(graph: Graph, failures: dict):
    """Lines with hints (link_hints): a written one is marked; one that could
    not be written is drawn anyway (kind "failed") until it is removed by hand."""
    import link_hints
    ids = {n.id for n in graph.nodes}
    for link_key, entry in failures.items():
        source, target = link_hints.split(link_key)
        level = link_hints.level(entry)
        if level == "ok" or source not in ids or target not in ids:
            continue
        edge = next((e for e in graph.edges if e.source == source and e.target == target), None)
        if edge is None:
            edge = Edge(source, target, "failed")
            graph.edges.append(edge)
        edge.failed = level


def order_columns(graph: Graph):
    """Fewer crossing lines: each side column follows the order of what it is
    joined to (agents stay in their order; models follow their providers)."""
    def place(column: int, neighbour: int):
        index = {n.id: i for i, n in enumerate(graph.column(neighbour))}
        def key(item):
            position, node = item
            linked = [index[e.source] for e in graph.edges if e.target == node.id and e.source in index and e.kind != "default"]
            linked += [index[e.target] for e in graph.edges if e.source == node.id and e.target in index and e.kind != "default"]
            return (sum(linked) / len(linked) if linked else len(index), position)
        ordered = [node for _position, node in sorted(enumerate(graph.column(column)), key=key)]
        others = [n for n in graph.nodes if n.column != column]
        graph.nodes = others + ordered

    place(0, 1)
    # MCP servers above skill sets: two groups (the map leaves a gap between them).
    left = sorted(graph.column(0), key=lambda n: {"mcp": 0, "add-mcp": 1, "add-skills": 3}.get(n.kind, 2))
    graph.nodes = [n for n in graph.nodes if n.column != 0] + left
    place(2, 1)
    place(3, 2)
