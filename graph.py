"""The map's nodes and edges, from agents, their details and the inventory.

Four columns, left to right:

    0 MCP servers and skill sets  ←  1 agents  →  2 LLM providers  →  3 models

Providers are the cloud services the agents talk to and the local runtimes
(llama.cpp, Ollama, …). Pure data: map_view.py draws it, tests check it.
"""

import re
from dataclasses import dataclass, field

import providers


def _rebuild():
    """Tables from the adapters (run again when add-ons add or remove some)."""
    global CLOUD, PROVIDER_NAMES
    # The online provider behind each agent unless its settings point elsewhere, and provider ids in
    # agent settings ("anthropic/claude-…", pi's defaultProvider, Hermes's provider): provider_online.py.
    CLOUD = providers.cloud_of_agent()
    PROVIDER_NAMES = providers.online_names()


_rebuild()
providers.on_change(_rebuild)


MAX_MODELS_PER_PROVIDER = 10

# What a model's name may carry besides the model itself: a publisher ("lmstudio-community/…"),
# the file format ("…-GGUF", "….gguf") and the quantisation ("…-Q4_K_M", "…-Q8_0", "…-IQ3_XS").
_QUANT = re.compile(r"[-_.](?:i?q\d(?:_[0-9a-z]+)*|f16|bf16|f32|fp16)$")


def model_key(name: str) -> str:
    """A model's name for matching across sources: an agent's settings name the id a runtime
    answers to ("gemma-4-12b-it-qat"), a runtime that is not running is listed by its folders
    ("gemma-4-12B-it-QAT-GGUF") or files ("gemma-4-12B-it-QAT-Q4_0")."""
    key = str(name or "").strip().lower().rsplit("/", 1)[-1]
    for suffix in (".gguf", "-gguf", "_gguf"):
        if key.endswith(suffix):
            key = key[: -len(suffix)]
    return _QUANT.sub("", key)

# Categories the map can show or hide, in toolbar order, and what each needs.
LAYERS = ("agents", "online", "local", "models", "mcp", "skills")
LAYER_OF = {"agent": "agents", "cloud": "online", "runtime": "local", "model": "models", "more": "models",
            "mcp": "mcp", "skills": "skills", "subagent": "agents"}
REQUIRES = {"models": ("online", "local"), "mcp": ("agents",), "skills": ("agents",)}   # any one of them


def effective_layers(enabled) -> set:
    """The categories actually shown: those switched on whose needs are met."""
    enabled = {layer for layer in enabled if layer in LAYERS}
    return {layer for layer in enabled
            if not REQUIRES.get(layer) or any(need in enabled for need in REQUIRES[layer])}


def hidden_by_layers(graph, enabled, hidden_ids=()) -> set:
    """Node ids the switched-off categories hide, plus hidden_ids (what the
    header's setup choice leaves out). A model also goes when none of the providers serving it is shown, an MCP server or
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
    return hidden


def idle_folders(graph) -> dict:
    """Model folders (of local LLMs) none of whose models an agent uses or runs:
    {folder: ids of its boxes, "+n more" included}."""
    folders = {}
    for node in graph.column(3):
        if node.group and node.kind in ("model", "more"):
            folders.setdefault(node.group, []).append(node)
    return {folder: {n.id for n in nodes} for folder, nodes in folders.items()
            if not any(n.active for n in nodes)}


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
    group: str = ""          # models: the folder they live in ("" = an online provider's); MCP servers:
                             # "online" for one at a URL (a card of its own on the map)
    active: bool = False     # models: an agent's model, or one an open agent runs


@dataclass
class Edge:
    source: str
    target: str
    kind: str                # uses / available (agent→provider), serves (provider→model), mcp, skills, default,
                             # failed (drawn but not written: only its hints are kept, link_hints)
    failed: str = ""         # "error" / "warning": the connection did not pass its check (dotted red / yellow)
    dangling: str = ""       # the model the agent is set to on a local LLM it has no provider for any more


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
    """"running", "noserver" (the app runs, its API server does not), "stopped" or "missing"."""
    if rt.running:
        return "noserver" if getattr(rt, "server_off", False) else "running"
    return "stopped" if rt.installed else "missing"


def runtime_detail(rt, labels: dict) -> str:
    return " · ".join([rt.address, labels["server_off"]]) if runtime_status(rt) == "noserver" else rt.address


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
          running: dict | None = None, available=()) -> Graph:
    """Only what is set up: installed agents and local LLMs, the MCP servers and skill
    sets agents use, models. agent_list: agents.Agent (installed ones are drawn); details: command ->
    agent_details.Details; labels: functions n -> translated text ("skills", "more");
    running: command -> the model of each open instance ("" = its settings');
    available: (kind, ident, label, detail) of MCP servers and skill sets on this PC that no agent
    uses yet: a faded box without lines, so a line drawn to it sets it up."""
    labels = {"skills": "{0} skills".format, "more": "+{0} more".format, "running": lambda n: "running" if n == 1
              else f"{n} running", "in_use": "in use · {0}".format,
              "server_off": "server off", **(labels or {})}
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
                graph.add(Node(rid, 2, rt.name, runtime_detail(rt, labels), "runtime", status=runtime_status(rt)))
                graph.link(aid, rid, "uses" if provider.active else "available")

        # A model that points at a local LLM's provider the agent no longer has (removed by hand, or by
        # an older disconnect): not an online service, a broken line to that local LLM.
        dangling = next((rt for rt in inventory.runtimes if rt.installed and model_provider
                         and providers.connect_keys().get(rt.key) == model_provider
                         and model_provider not in local_names), None)
        if dangling is not None:
            rid = f"runtime:{dangling.key}"
            graph.add(Node(rid, 2, dangling.name, runtime_detail(dangling, labels), "runtime",
                           status=runtime_status(dangling)))
            edge = Edge(aid, rid, "failed", "error")
            edge.dangling = model
            graph.edges.append(edge)

        # The cloud service: the configured provider when it is not local, else the agent's own.
        cloud = ""
        if model_provider and model_provider not in local_names and dangling is None:
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
            # The other models it can be switched to on the map (online_models: the agent's own list).
            import online_models
            for other in online_models.choices(agent.command, model):
                if other != model:
                    oid = f"model:{cid}:{other.lower()}"
                    graph.add(Node(oid, 3, other, "", "model"))
                    if not any(e.source == cid and e.target == oid for e in graph.edges):
                        graph.link(cid, oid, "serves")
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
            graph.add(Node(mid, 0, name, server.target if server else "", "mcp",
                           group="online" if server and server.kind == "url" else ""))
            graph.link(aid, mid, "mcp")
        for path in inventory.agent_skills.get(agent.command, []):
            skill_set = next((s for s in inventory.skill_sets if s.path == path), None)
            sid = f"skills:{path}"
            count = len(skill_set.skills) if skill_set else 0
            graph.add(Node(sid, 0, path, labels["skills"](count), "skills"))
            graph.link(aid, sid, "skills")

    for kind, ident, label, detail in available:
        if kind in ("mcp", "skills"):
            server = next((s for s in inventory.mcp if s.name == ident), None) if kind == "mcp" else None
            graph.add(Node(f"{kind}:{ident}", 0, label, detail, kind, dim=True,
                           group="online" if server and server.kind == "url" else ""))

    # Every installed local runtime (something to connect agents to), with its models.
    for rt in inventory.runtimes:
        if not rt.installed:
            continue
        rid = f"runtime:{rt.key}"
        graph.add(Node(rid, 2, rt.name, runtime_detail(rt, labels), "runtime", status=runtime_status(rt)))
        shown = rt.models[:MAX_MODELS_PER_PROVIDER]
        for model in shown:
            mid = f"model:{rid}:{model.lower()}"
            graph.add(Node(mid, 3, model, "", "model", group=rt.models_dir or rt.name))
            graph.link(rid, mid, "serves")
        if len(rt.models) > len(shown):
            mid = f"more:{rid}"
            graph.add(Node(mid, 3, labels["more"](len(rt.models) - len(shown)), "", "more", dim=True,
                           group=rt.models_dir or rt.name))
            graph.link(rid, mid, "serves")

    # A link to a runtime's model under another spelling of its name (folders while LM Studio is
    # not running, files in llama.cpp's folder): to the model the runtime lists.
    by_key = {}
    for node in graph.nodes:
        if node.kind == "model" and node.id.startswith("model:runtime:"):
            runtime = node.id.split(":", 3)[2]
            by_key.setdefault((runtime, model_key(node.label)), node.id)
    known = {n.id for n in graph.nodes}

    def listed(model_id: str) -> str:
        if model_id in known or not model_id.startswith("model:runtime:"):
            return model_id
        _m, _r, runtime, name = model_id.split(":", 3)
        return by_key.get((runtime, model_key(name)), model_id)

    for edge in graph.edges:
        edge.target = listed(edge.target)
    for model_id in list(live):
        found = listed(model_id)
        if found != model_id:
            live.setdefault(found, [])
            live[found] += [n for n in live.pop(model_id) if n not in live[found]]

    for node in graph.nodes:
        if node.id in live:
            node.status = "running"
            node.detail = labels["in_use"](", ".join(live[node.id]))
    chosen = {e.target for e in graph.edges if e.kind == "default"}
    for node in graph.nodes:
        node.active = node.kind == "model" and (node.id in chosen or node.id in live)

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
    left = sorted(graph.column(0), key=lambda n: n.kind != "mcp")
    graph.nodes = [n for n in graph.nodes if n.column != 0] + left
    place(2, 1)
    place(3, 2)
