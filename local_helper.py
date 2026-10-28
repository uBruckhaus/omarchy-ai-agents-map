"""The local helper: an agent connected to a local LLM that answers AI help questions with one of that
LLM's models, on this PC, without the web and without tokens of a plan. Any agent whose adapter has
an advise fact (one question, offline, no tools, the model given by options) can be it; which agent,
provider and model is the user's choice (Settings › AI features), and the app suggests one: the
largest model the graphics card holds that is a chat model (not speech, embeddings or images).

AI help that needs the web (model advice, MCP server updates, looking up a new agent) stays with the
online agent; what works from what the app shows the agent (Smart Check's analysis, MCP server advice,
the local LLM help) goes to the local helper first when one is set up, else to the online agent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Model names that are not chat models: speech, embeddings, rerankers, tokenizers, vision parts.
NOT_CHAT = re.compile(r"tts|whisper|embed|rerank|tokenizer|mmproj|clip|vae|diffusion|flux|sdxl|"
                      r"stable-?diffusion|speech|asr|vision-encoder", re.IGNORECASE)
GPU_SHARE = 0.85            # of the card's memory a helper model may take (its context needs the rest)
MIN_BYTES = 2 * 1024 ** 3   # smaller models answer AI help questions too poorly


@dataclass(frozen=True)
class Candidate:
    agent: str              # the agent's command
    provider: str           # its name for the local LLM ("llama-local")
    runtime: str            # the local LLM's key ("llamacpp")
    model: str              # the model as the local LLM names it
    size: int = 0           # its file, bytes (0: not known)

    @property
    def key(self) -> dict:
        return {"agent": self.agent, "provider": self.provider, "model": self.model}


def advisers(installed) -> list[str]:
    """Installed agents that can be the local helper (an advise fact and what closes them in)."""
    import harnesses
    import shutil
    return [h.command for h in harnesses.all_harnesses()
            if h.advise and h.closed and h.command in installed and shutil.which(h.command)]


def candidates(installed, runtimes, details_for=None, size_of=None) -> list[Candidate]:
    """Every (agent, local LLM it is connected to, chat model of that LLM)."""
    import agent_details
    import llm_engine
    details_for = details_for or agent_details.details_for
    found = []
    for agent in advisers(installed):
        try:
            providers = details_for(agent).providers
        except Exception:
            continue
        for provider in providers:
            if not provider.local:
                continue
            rt = next((r for r in runtimes if r.installed and provider.runtime in (r.name, r.key)), None)
            if rt is None:
                continue
            for model in rt.models or ():
                name = str(model)
                if NOT_CHAT.search(name):
                    continue
                try:
                    size = size_of(rt, name) if size_of else llm_engine.of(rt).model_size(rt, name)
                except (OSError, ValueError):
                    size = 0
                found.append(Candidate(agent, provider.name, rt.key, name, size))
    return found


def suggest(found: list[Candidate], vram: int) -> Candidate | None:
    """The largest chat model that fits the card (with room for its context); without sizes or a card
    the first one."""
    if not found:
        return None
    limit = vram * GPU_SHARE if vram else 0
    sized = [c for c in found if c.size >= MIN_BYTES and (not limit or c.size <= limit)]
    if sized:
        return max(sized, key=lambda c: c.size)
    return found[0] if not any(c.size for c in found) else None


def chosen(found: list[Candidate], setting) -> Candidate | None:
    """The helper confirmed in Settings, while it is still there (its agent, provider and model)."""
    if not isinstance(setting, dict):
        return None
    return next((c for c in found if c.key == {k: setting.get(k) for k in ("agent", "provider", "model")}), None)


def advise_argv(candidate: Candidate) -> tuple[list[str], str]:
    """(argv, output format) for one question to the helper; the prompt goes on stdin."""
    import agent_guard
    import harnesses
    import shutil
    argv, model, output = harnesses.get(candidate.agent).advise
    fill: dict[str, str] = {"{model}": candidate.model, "{provider}": candidate.provider}
    full: list[str] = [shutil.which(candidate.agent) or candidate.agent] + [str(a) for a in argv[1:]] \
        + [fill.get(str(a), str(a)) for a in model]
    return agent_guard.background(candidate.agent, full), output


def label(candidate: Candidate, runtime_name: str = "") -> str:
    import ai_helper
    return " · ".join(filter(None, [ai_helper.name_of(candidate.agent), runtime_name or candidate.provider,
                                    candidate.model]))
