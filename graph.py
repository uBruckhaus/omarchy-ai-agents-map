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


@dataclass
class Node:
    id: str
    column: int
    label: str
    detail: str = ""
    kind: str = ""           # agent, cloud, runtime, model, mcp, skills
    icon: str = ""
    icon_font: str = ""
    dim: bool = False        # e.g. a runtime that is not running


@dataclass
class Edge:
    source: str
    target: str
    kind: str                # uses (agent→provider), serves (provider→model), mcp, skills


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


def build(agent_list, details: dict, inventory, labels: dict | None = None) -> Graph:
    """agent_list: agents.Agent (installed ones are drawn); details: command ->
    agent_details.Details; labels: functions n -> translated text ("skills", "more")."""
    labels = {"skills": "{0} skills".format, "more": "+{0} more".format, **(labels or {})}
    graph = Graph()
    runtimes = {rt.name: rt for rt in inventory.runtimes}

    for agent in agent_list:
        if not agent.installed:
            continue
        aid = f"agent:{agent.command}"
        graph.add(Node(aid, 1, agent.name, agent.version, "agent", agent.icon, agent.icon_font))
        info = details.get(agent.command)
        model, model_provider = split_model(info.model if info else "")

        # Local runtimes the agent's providers point at.
        local_names = {}
        for provider in (info.providers if info else []):
            if provider.local and provider.runtime in runtimes:
                local_names[provider.name] = provider.runtime
                rt = runtimes[provider.runtime]
                rid = f"runtime:{rt.key}"
                graph.add(Node(rid, 2, rt.name, rt.address, "runtime", dim=not rt.running))
                graph.link(aid, rid, "uses")

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

    # Models of every local runtime in the map (and of running ones nobody uses yet).
    for rt in inventory.runtimes:
        rid = f"runtime:{rt.key}"
        if rt.running and not any(n.id == rid for n in graph.nodes):
            graph.add(Node(rid, 2, rt.name, rt.address, "runtime"))
        if not any(n.id == rid for n in graph.nodes):
            continue
        shown = rt.models[:MAX_MODELS_PER_PROVIDER]
        for model in shown:
            mid = f"model:{rid}:{model.lower()}"
            graph.add(Node(mid, 3, model, "", "model", dim=not rt.running))
            graph.link(rid, mid, "serves")
        if len(rt.models) > len(shown):
            mid = f"more:{rid}"
            graph.add(Node(mid, 3, labels["more"](len(rt.models) - len(shown)), "", "more", dim=True))
            graph.link(rid, mid, "serves")

    # A default model that points at a model the runtime does not list: drop the link.
    ids = {n.id for n in graph.nodes}
    graph.edges = [e for e in graph.edges if e.source in ids and e.target in ids]
    order_columns(graph)
    return graph


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
    place(2, 1)
    place(3, 2)
