"""The map's nodes and edges, from agents, their details and the inventory.

Four columns, left to right:

    0 MCP servers and skill sets  ←  1 agents  →  2 LLM providers  →  3 models

Providers are the cloud services the agents talk to and the local runtimes
(llama.cpp, Ollama, …). Pure data: map_view.py draws it, tests check it.
"""

import os
import re
from dataclasses import dataclass, field

import harnesses
import providers
from i18n import _

MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}")


def _last_model(command: str) -> str:
    import usage_box
    return usage_box.last_model(command)


# The model of an online agent's latest session, for one whose settings name none (tests set None).
LAST_MODEL = _last_model


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
                             # "online" for one at a URL (a card of its own on the map); agents: "local"
                             # for one made for a local LLM (its adapter has local_llm)
    active: bool = False     # models: an agent's model, or one an open agent runs
    own_provider: str = ""   # agents: the id of their own online provider (CLOUD), the one drawing the
                             # agent onto signs in to; "" for none
    own_login: bool = False  # agents: the app can start their sign-in there (access.logins); a drop onto
                             # it is refused without


@dataclass
class Edge:
    source: str
    target: str
    kind: str                # uses / available (agent→provider), serves (provider→model), mcp, skills, default,
                             # failed (drawn but not written: only its hints are kept, link_hints)
    failed: str = ""         # "error" / "warning": the connection did not pass its check (dotted red / yellow)
    fixed: str = ""          # why this line cannot be taken away here (skills reached through another folder)
    dangling: str = ""       # the model the agent is set to on a local LLM it has no provider for any more
    provider: str = ""       # a moved local LLM's line: the agent's own name for that entry (Repair rewrites it)


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


_ways = {}
_ways_of = None
_ways_in = None


def cached_skill_way(inventory, command: str, path: str) -> str:
    """Reading folders per line is for a new inventory, not each scan."""
    global _ways_of, _ways_in
    language = _("through a folder it reads")   # the language they were made in
    if inventory is not _ways_of or language != _ways_in:
        _ways_of, _ways_in = inventory, language
        _ways.clear()
    if (command, path) not in _ways:
        _ways[command, path] = skill_way(command, path)
    return _ways[command, path]


def skill_way(command: str, path: str) -> str:
    """Why an agent keeps a skill folder that nothing here can take away, or ""."""
    import connect
    import skills_ctl
    folder = skills_ctl.expand(path)
    reach = skills_ctl.reach(command, folder)
    if reach.listed or reach.links or not reach.native:
        return ""
    other = [d for d in reach.native if skills_ctl._real(d) != skills_ctl._real(folder)]
    if other:
        return f"{_('through a folder it reads')}: {connect.tilde(other[0])}"
    return _('reads the folder by itself')


def runtime_status(rt) -> str:
    """"running", "noserver" (the app runs, its API server does not), "stopped" or "missing"."""
    if rt.running:
        return "noserver" if getattr(rt, "server_off", False) else "running"
    return "stopped" if rt.installed else "missing"


def runtime_detail(rt, labels: dict) -> str:
    return " · ".join([rt.address, labels["server_off"]]) if runtime_status(rt) == "noserver" else rt.address


def signed_in(command: str) -> bool:
    """An agent's sign-in to its own online provider is there: a key in the environment or a sign-in
    file (a stat, never a read). An agent whose access facts name neither sign-in files nor keys cannot
    tell: signed in."""
    import agent_status
    access = agent_status.access_table().get(command)
    if access is None or not access.files and not access.env or any(os.environ.get(n) for n in access.env):
        return True
    import agent_details      # the files where it keeps them (a moved home), or a keyring its program tells
    return agent_details.plan_limits_signed_in(command, agent_status.HOME)


def provider_name(raw: str) -> str:
    raw = (raw or "").strip()
    return PROVIDER_NAMES.get(raw.lower(), raw)


def online_choices(graph, agent, aid: str, model: str, model_provider: str, local_names: dict, runtimes: dict):
    """A harness with several providers (Hermes, DeepSeek Harness, Pi, …): each online provider its
    own settings (or its own built-in routes) offer, with those models, joined to it by an "offer" line
    while it uses another provider: not a connection (nothing to disconnect), only what it can switch
    to; the map draws it quietly while the agent is selected or hovered; a click or a line to one of
    the models switches it."""
    import online_models
    local_keys = {key.lower() for key in providers.connect_keys().values()} | {n.lower() for n in local_names}
    current = f"{model_provider}/{model}" if model and model_provider else ""
    for choice in online_models.choices(agent.command, current):
        provider, name = online_models.split(choice)
        if not provider or provider.lower() in local_keys:
            continue                      # a local LLM's models: its own box shows them
        service = provider_name(provider)
        cid = f"cloud:{service.lower()}"
        graph.add(Node(cid, 2, service, "", "cloud"))
        if not any(e.source == aid and e.target == cid for e in graph.edges):
            graph.link(aid, cid, "offer")
        mid = f"model:{cid}:{name.lower()}"
        graph.add(Node(mid, 3, name, "", "model"))
        if not any(e.source == cid and e.target == mid for e in graph.edges):
            graph.link(cid, mid, "serves")


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
          running: dict | None = None, available=(), provider_models: dict | None = None) -> Graph:
    """Only what is set up: installed agents and local LLMs, the MCP servers and skill
    sets agents use, models. agent_list: agents.Agent (installed ones are drawn); details: command ->
    agent_details.Details; labels: functions n -> translated text ("skills", "more");
    running: command -> the model of each open instance ("" = its settings');
    available: (kind, ident, label, detail) of MCP servers and skill sets on this PC that no agent
    uses yet: a faded box without lines, so a line drawn to it sets it up; provider_models: Settings ›
    provider_models (provider id -> its chosen model's id), drawn even past the first models."""
    labels = {"skills": "{0} skills".format, "more": "+{0} more".format, "running": lambda n: "running" if n == 1
              else f"{n} running", "in_use": "in use · {0}".format,
              "server_off": "server off", **(labels or {})}
    running = running or {}
    graph = Graph()
    runtimes = {rt.name: rt for rt in inventory.runtimes}
    live = {}                # model node id -> names of the agents running with it now
    model_labels = {}        # a local model an agent is set to -> its name as the agent has it

    for agent in agent_list:
        if not agent.installed:
            continue
        aid = f"agent:{agent.command}"
        instances = running.get(agent.command, [])
        # Made for a local LLM (its adapter connects one): the map's Local agents card.
        harness = harnesses.get(agent.command)
        local = "local" if harness and harness.local_llm else ""
        own = CLOUD.get(agent.command, "")
        own_id = f"cloud:{own.lower()}" if own else ""
        access = harness.access() if harness and harness.access else None
        own_login = bool(own_id and access is not None and access.logins)
        if instances:
            graph.add(Node(aid, 1, agent.name, " · ".join(filter(None, [agent.version, labels["running"](len(instances))])),
                           "agent", agent.icon, agent.icon_font, status="running", group=local, own_provider=own_id,
                           own_login=own_login))
        else:
            graph.add(Node(aid, 1, agent.name, agent.version, "agent", agent.icon, agent.icon_font, group=local,
                           own_provider=own_id, own_login=own_login))
        info = details.get(agent.command)
        model, model_provider = split_model(info.model if info else "")

        # Local runtimes the agent's providers point at.
        local_names = {}
        stale = {}  # a local provider set to a port its LLM no longer listens on
        # Active providers first, so an agent's line to a runtime is solid when any is in use.
        import inventory as inventory_mod
        for provider in sorted(info.providers if info else [], key=lambda p: not p.active):
            # Joined by its configured port to the runtime detected there.
            rt = inventory_mod.local_runtime(provider, inventory.runtimes)
            if rt is not None:
                local_names[provider.name] = rt.name
                rid = f"runtime:{rt.key}"
                graph.add(Node(rid, 2, rt.name, runtime_detail(rt, labels), "runtime", status=runtime_status(rt)))
                graph.link(aid, rid, "uses" if provider.active else "available")
            elif (moved := inventory_mod.moved_runtime(provider, inventory.runtimes)) is not None:
                stale[provider.name] = moved

        # A model with no provider of its own, while the agent's one provider is a local LLM (Claude
        # Code with a local ANTHROPIC_BASE_URL): that LLM's model, not one of its own online provider.
        active = [p for p in (info.providers if info else []) if p.active]
        if not model_provider and len(active) == 1 and (active[0].name in local_names or active[0].name in stale):
            model_provider = active[0].name

        # A model that points at a local LLM's provider the agent no longer has (removed by hand, or by
        # an older disconnect): not an online service, a broken line to that local LLM.
        # Also a provider whose LLM moved to another port: a broken line to it.
        dangling = stale.get(model_provider) or next((rt for rt in inventory.runtimes if rt.installed and model_provider
                         and providers.connect_keys().get(rt.key) == model_provider
                         and model_provider not in local_names), None)
        # An agent with no local-LLM planner (Claude Code, Codex: the address is in its own settings)
        # cannot be repaired or disconnected here: the line says where to change it instead.
        def unfixable(rt):
            return "" if harness and harness.local_llm else \
                _("its address is set in its own settings: change it there to {0}").format(rt.address)
        if dangling is not None:
            rid = f"runtime:{dangling.key}"
            graph.add(Node(rid, 2, dangling.name, runtime_detail(dangling, labels), "runtime",
                           status=runtime_status(dangling)))
            edge = Edge(aid, rid, "failed", "error", fixed=unfixable(dangling))
            edge.dangling = model
            edge.provider = model_provider if stale.get(model_provider) is not None else ""
            graph.edges.append(edge)
        # Every other local provider whose LLM moved (an inactive one too): a broken line, no default;
        # Repair rewrites its address.
        for name, moved in stale.items():
            rid = f"runtime:{moved.key}"
            if moved is dangling or any(e.source == aid and e.target == rid for e in graph.edges):
                continue
            graph.add(Node(rid, 2, moved.name, runtime_detail(moved, labels), "runtime",
                           status=runtime_status(moved)))
            graph.edges.append(Edge(aid, rid, "failed", "error", fixed=unfixable(moved), provider=name))

        # The cloud service: the configured provider when it is not local, else the agent's own.
        cloud = ""
        if model_provider and model_provider not in local_names and dangling is None:
            cloud = provider_name(model_provider)
        elif not model_provider:
            cloud = CLOUD.get(agent.command, "")
        if agent.command in CLOUD and not local_names and not stale:
            cloud = cloud or CLOUD[agent.command]
        if cloud:
            cid = f"cloud:{cloud.lower()}"
            graph.add(Node(cid, 2, cloud, "", "cloud"))
            # Its own provider, while it is not signed in: one it can use (drawing the agent onto it
            # signs in), not one it uses.
            graph.link(aid, cid, "available" if cloud == own and not signed_in(agent.command) else "uses")
            implicit = False
            if not model and LAST_MODEL is not None:
                # Its settings name no model: it uses its own default, which its latest session says.
                found = LAST_MODEL(agent.command)
                if found and MODEL_NAME.fullmatch(found):
                    model, implicit = found, True
            if model:
                mid = f"model:{cid}:{model.lower()}"
                graph.add(Node(mid, 3, model, _("its default (last session)") if implicit else "", "model"))
                graph.link(cid, mid, "serves")
                graph.link(aid, mid, "default")
            # The other models it can be switched to on the map (online_models: the agent's own list;
            # "provider/model" choices of a harness with several providers come below).
            import online_models
            for other in online_models.choices(agent.command, model):
                if other != model and not online_models.split(other)[0]:
                    oid = f"model:{cid}:{other.lower()}"
                    graph.add(Node(oid, 3, other, "", "model"))
                    if not any(e.source == cid and e.target == oid for e in graph.edges):
                        graph.link(cid, oid, "serves")
        elif model and model_provider in local_names:
            rt = runtimes[local_names[model_provider]]
            mid = f"model:runtime:{rt.key}:{model.lower()}"
            model_labels[mid] = model
            graph.link(aid, mid, "default")
        online_choices(graph, agent, aid, model, model_provider, local_names | {n: rt.name for n, rt in stale.items()}, runtimes)

        # The model each open instance runs with: its own --model, else the settings'.
        for chosen in instances:
            name, provider = split_model(chosen) if chosen else (model, model_provider)
            if not name:
                continue
            provider = provider or model_provider     # `--model qwen` names none: the agent's configured one
            if provider in local_names:
                mid = f"model:runtime:{runtimes[local_names[provider]].key}:{name.lower()}"
            elif provider in stale or (dangling is not None and provider == model_provider):
                continue    # its local LLM moved (or its provider is gone): only the broken line
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
            # This agent's server of that name (another of the same name is another box, name~2).
            server = next((s for s in inventory.mcp if s.name == name and agent.command in s.agents),
                          next((s for s in inventory.mcp if s.name == name), None))
            mid = f"mcp:{server.id if server else name}"
            graph.add(Node(mid, 0, name, server.target if server else "", "mcp",
                           group="online" if server and server.kind == "url" else ""))
            graph.link(aid, mid, "mcp")
        for path in inventory.agent_skills.get(agent.command, []):
            skill_set = next((s for s in inventory.skill_sets if s.path == path), None)
            sid = f"skills:{path}"
            count = len(skill_set.skills) if skill_set else 0
            graph.add(Node(sid, 0, path, labels["skills"](count), "skills"))
            graph.link(aid, sid, "skills")
            for edge in graph.edges:
                if edge.source == aid and edge.target == sid:
                    edge.fixed = cached_skill_way(inventory, agent.command, path)

    for kind, ident, label, detail in available:
        if kind in ("mcp", "skills"):
            server = next((s for s in inventory.mcp if s.name == ident), None) if kind == "mcp" else None
            graph.add(Node(f"{kind}:{ident}", 0, label, detail, kind, dim=True,
                           group="online" if server and server.kind == "url" else ""))

    # A known server without agents stays, to draw a line to it again.
    for server in inventory.mcp:
        if not any(n.id == f"mcp:{server.id}" for n in graph.nodes):
            graph.add(Node(f"mcp:{server.id}", 0, server.name, server.target, "mcp", dim=True,
                           group="online" if server.kind == "url" else ""))

    # What an agent is set to or runs, and each provider's chosen model: drawn even past the first ones.
    wanted = {e.target for e in graph.edges if e.kind == "default"} | set(live) | set((provider_models or {}).values())
    # Every installed local runtime (something to connect agents to), with its models.
    for rt in inventory.runtimes:
        if not rt.installed:
            continue
        rid = f"runtime:{rt.key}"
        graph.add(Node(rid, 2, rt.name, runtime_detail(rt, labels), "runtime", status=runtime_status(rt)))
        keys = {model_key(i[len(f"model:{rid}:"):]) for i in wanted if i.startswith(f"model:{rid}:")}
        shown = rt.models[:MAX_MODELS_PER_PROVIDER]
        shown += [m for m in rt.models[MAX_MODELS_PER_PROVIDER:] if model_key(m) in keys]
        for model in shown:
            mid = f"model:{rid}:{model.lower()}"
            graph.add(Node(mid, 3, model, "", "model", group=rt.models_dir or rt.name))
            graph.link(rid, mid, "serves")
        if len(rt.models) > len(shown):
            mid = f"more:{rid}"
            graph.add(Node(mid, 3, labels["more"](len(rt.models) - len(shown)), "", "more", dim=True,
                           group=rt.models_dir or rt.name))
            graph.link(rid, mid, "serves")
    # Reused model folders no local LLM lists: a folder card of their own.
    for path, models in getattr(inventory, "model_folders", []):
        for model in models[:MAX_MODELS_PER_PROVIDER]:
            graph.add(Node(f"model:folder:{path}:{model.lower()}", 3, model, "", "model", group=path))
        if len(models) > MAX_MODELS_PER_PROVIDER:
            graph.add(Node(f"more:folder:{path}", 3, labels["more"](len(models) - MAX_MODELS_PER_PROVIDER), "",
                           "more", dim=True, group=path))

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
    # An agent set to a model its local LLM no longer has: the model stays, marked missing, so its line
    # and the way to repair it stay too.
    for edge in [e for e in graph.edges if e.kind == "default"]:
        if edge.target in known or not edge.target.startswith("model:runtime:"):
            continue
        rt = next((r for r in inventory.runtimes if r.key == edge.target.split(":")[2]), None)
        if rt is None or f"runtime:{rt.key}" not in known:
            continue
        graph.add(Node(edge.target, 3, model_labels.get(edge.target, edge.target.split(":", 3)[3]),
                       _("This model is no longer on this PC."), "model", status="missing",
                       group=rt.models_dir or rt.name))
        graph.link(f"runtime:{rt.key}", edge.target, "serves")
        known.add(edge.target)
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
