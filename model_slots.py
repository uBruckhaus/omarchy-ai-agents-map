"""How many models a local LLM must keep loaded for its agents, and whether they fit.

When agents use different models of one local LLM (Pi one, DeepSeek Harness another), it must keep
that many loaded, or it swaps them on every request while both work. The app sets the number
itself after a connection or a model change, where the local LLM lets it (its engine's
slots_kind: llama.cpp and Ollama a number, LM Studio one or many), and says so; a number the user
fixed in their own start script is theirs, and only a warning says what to change. Either way it
warns when the models together are bigger than the graphics card.
"""

from __future__ import annotations

from dataclasses import dataclass, field

OVERHEAD = 1.15          # a model's file plus its context cache and the runtime, roughly


@dataclass
class Need:
    key: str                              # the local LLM
    name: str
    models: dict = field(default_factory=dict)    # model -> [agent names] that use it
    kind: str = ""                        # its engine's slots_kind
    slots: int = 1                        # models it keeps loaded now (0: no limit)
    fixed_by: str = ""                    # the user's own start script that fixes the number
    sizes: dict = field(default_factory=dict)     # model -> bytes (0: not known)

    @property
    def count(self) -> int:
        return len(self.models)

    @property
    def change(self) -> bool:
        """The app should set it (a number other than the models in use; LM Studio's one or many)."""
        if not self.kind or self.fixed_by or not self.models:
            return False
        if self.kind == "switch":
            return (self.slots == 1) != (self.count == 1)
        return self.slots != self.count

    @property
    def swaps(self) -> bool:
        """More models in use than it keeps loaded: they are swapped while the agents work."""
        return self.slots != 0 and self.count > self.slots


def models_in_use(graph) -> dict:
    """runtime id -> {model label: [agent labels]}: the agents whose provider now is that local LLM
    (a "uses" line) and whose model is one of its models."""
    if graph is None:
        return {}
    nodes = {n.id: n for n in graph.nodes}
    served = {}
    for edge in graph.edges:
        if edge.kind == "serves" and edge.source.startswith("runtime:"):
            served.setdefault(edge.target, set()).add(edge.source)
    found = {}
    for edge in graph.edges:
        if edge.kind != "default" or edge.target not in served:
            continue
        agent, model = edge.source, edge.target
        for runtime in served[model]:
            if any(e.source == agent and e.target == runtime and e.kind == "uses" for e in graph.edges):
                label = nodes[model].label if model in nodes else model
                who = nodes[agent].label if agent in nodes else agent
                found.setdefault(runtime, {}).setdefault(label, []).append(who)
    return found


def plan(runtimes, in_use: dict, engine_of=None) -> list:
    """A Need for every local LLM some agent uses, with what its engine says."""
    if engine_of is None:
        import llm_engine
        engine_of = llm_engine.of
    needs = []
    for rt in runtimes:
        models = in_use.get(f"runtime:{rt.key}")
        if not models:
            continue
        engine = engine_of(rt)
        need = Need(rt.key, rt.name, dict(models), engine.slots_kind)
        try:
            need.slots = engine.slots(rt)
            need.fixed_by = engine.slots_fixed_by(rt)
            need.sizes = {m: engine.model_size(rt, m) for m in models}
        except (OSError, ValueError):
            pass
        needs.append(need)
    return needs


def gib(size: float) -> str:
    return f"{size / 2 ** 30:.0f}" if size >= 10 * 2 ** 30 else f"{size / 2 ** 30:.1f}"


def hints(need: Need, vram: int) -> list:
    """Warnings (link_check hint dicts) for a local LLM's models in use."""
    from i18n import _
    out = []
    if need.count < 2:
        return out
    who = "; ".join(f"{', '.join(agents)}: {model}" for model, agents in sorted(need.models.items()))
    if need.swaps and not need.change:
        text = _("Its agents use {0} different models ({1}), but {2} keeps {3} loaded at a time: while they "
                 "work at the same time it swaps them, which is slow.").format(need.count, who, need.name, need.slots)
        if need.fixed_by:
            text += " " + _("The number is set in {0} (--models-max): raise it there, or give the agents the "
                            "same model.").format(need.fixed_by)
        out.append({"level": "warning", "text": text})
    known = [s for s in need.sizes.values() if s]
    total = sum(known) * OVERHEAD
    if vram and total > vram:
        out.append({"level": "warning", "text": _(
            "The {0} models its agents use need about {1} GB of video memory together; the graphics card has "
            "{2} GB. They cannot all stay loaded: choose smaller models, or the same one for several agents.")
            .format(need.count, gib(total), gib(vram))})
    return out


# --- starting an agent ---------------------------------------------------------------------------

@dataclass
class StartCheck:
    """Whether an agent's model still fits in the graphics card's memory when it starts."""
    agent: str
    model: str
    runtime: str                          # the local LLM's name
    key: str
    need: int                             # bytes its model takes once loaded (about)
    free: int                             # bytes free for it: free now plus what loading it unloads
    others: list = field(default_factory=list)    # [(local LLM name, bytes)] other local LLMs hold
    apps: int = 0                         # bytes the desktop and other programs hold

    @property
    def fits(self) -> bool:
        return self.need <= self.free


def agent_model(graph, agent_id: str):
    """(runtime id, model label) of the local LLM model the agent uses now, else None."""
    if graph is None:
        return None
    nodes = {n.id: n for n in graph.nodes}
    for edge in graph.edges:
        if edge.kind != "default" or edge.source != agent_id:
            continue
        for serves in graph.edges:
            if serves.kind == "serves" and serves.target == edge.target and serves.source.startswith("runtime:") \
                    and any(e.source == agent_id and e.target == serves.source and e.kind == "uses"
                            for e in graph.edges):
                return serves.source, nodes[edge.target].label if edge.target in nodes else edge.target
    return None


def start_check(agent_id: str, agent_name: str, graph, runtimes, usage, engine_of=None):
    """A StartCheck for an agent about to start, or None (no local model, already loaded, sizes or
    the card not known). usage: gpu_usage.Usage taken just now."""
    found = agent_model(graph, agent_id)
    if found is None or usage is None or not usage.vram_total:
        return None
    runtime_id, model = found
    rt = next((r for r in runtimes if f"runtime:{r.key}" == runtime_id), None)
    if rt is None:
        return None
    if engine_of is None:
        import llm_engine
        engine_of = llm_engine.of
    engine = engine_of(rt)
    use = usage.runtimes.get(rt.key)
    loaded = dict(use.models) if use is not None else {}
    if model.lower() in loaded:
        return None                           # it is in memory already
    size = engine.model_size(rt, model)
    if not size:
        return None
    try:
        slots = engine.slots(rt)
    except (OSError, ValueError):
        slots = 1
    # Loading it where the local LLM is full unloads what it holds now (one model at a time: all of it).
    frees = use.vram if use is not None and loaded and slots and len(loaded) >= slots else 0
    names = {r.key: r.name for r in runtimes}
    others = [(names.get(key, key), u.vram) for key, u in usage.runtimes.items() if u.vram and key != rt.key]
    held = sum(u.vram for u in usage.runtimes.values())
    return StartCheck(agent_name, model, rt.name, rt.key, int(size * OVERHEAD),
                      max(0, usage.vram_total - usage.vram_used) + frees,
                      others, max(0, usage.vram_used - held))


def start_text(check: StartCheck) -> str:
    from i18n import _
    text = _("{0} uses {1} on {2}: loading it takes about {3} GB of video memory, but only {4} GB are free.") \
        .format(check.agent, check.model, check.runtime, gib(check.need), gib(check.free))
    held = [f"{name} {gib(size)} GB" for name, size in check.others]
    if check.apps >= 2 ** 30:
        held.append(_("the desktop and other programs {0} GB").format(gib(check.apps)))
    if held:
        text += " " + _("In use: {0}.").format(", ".join(held))
    text += " " + _("Started anyway, the model partly runs on the processor (slow) or does not load.")
    return text
