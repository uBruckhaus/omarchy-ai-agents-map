"""Provider adapters: what the app knows about each LLM provider, one module per local LLM
(provider_<key>.py) and the online providers (provider_online.py), registered in a fixed list here.
Add-ons may add more later (addon_api); no outside code is loaded otherwise.

Each local provider module defines PROVIDER = Provider(kind="local", ...): how it is found
(detect(processes) -> inventory.Runtime), installed (an install_ctl.Installer), what agents write to
reach it (connect key and label) and how the provider watcher treats it. provider_online.py defines
ONLINE, a list of Provider(kind="online", ...): their display name, the ids agent settings use for
them and the agents that come with them.

The feature modules (inventory, install_ctl, connect, graph) build their tables from here, so a new
provider is added in one place. provider_watch.py runs isolated (stdlib only) and keeps a copy of
its few provider facts; tests/test_providers.py keeps that copy equal to this registry.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable


@dataclass(frozen=True)
class Provider:
    key: str                          # "llamacpp", "anthropic", …
    name: str
    kind: str                         # "local" | "online"
    # local providers
    detect: Callable | None = None    # detect(processes) -> inventory.Runtime
    installer: Callable | None = None  # installer() -> install_ctl.Installer (made once by install_ctl)
    connect_key: str = ""             # the provider id agents get in their settings ("llama-local")
    connect_label: str = ""           # its display name there ("llama.cpp (local)")
    stays_up: bool = False            # the watcher unloads its model instead of stopping it
    unload: tuple | None = None       # (path, JSON body) of its unload request
    install: tuple = ()               # ways to install / remove it (harnesses.Way), best first
    data: tuple = ()                  # "~/…" folders with its models and settings: never removed by us
    homepage: str = ""                # its web page (the Add-ons page's Homepage button)
    # online providers
    ids: tuple = ()                   # names used in agent settings ("anthropic", "openai-codex")
    agents: tuple = field(default=())  # agents that come with this provider ("claude")


_HIDDEN: set = set()       # built-in providers an installed add-on brings instead (hide_built_in)
LOCAL_ORDER = ("llamacpp", "ollama", "lmstudio", "vllm", "jan", "koboldcpp")


def hide_built_in(keys) -> None:
    """Asterism: these providers come as add-ons (see harnesses.hide_built_in)."""
    _HIDDEN.clear()
    _HIDDEN.update(keys)


def _built_in_local() -> tuple:
    return tuple(p for p in built_in_local() if p.key not in _HIDDEN)


def _built_in_online() -> tuple:
    return tuple(p for p in built_in_online() if p.key not in _HIDDEN)


def built_in_local() -> tuple:
    import provider_jan
    import provider_koboldcpp
    import provider_llamacpp
    import provider_lmstudio
    import provider_ollama
    import provider_vllm
    # Order: how the map and the cards list them.
    return (provider_llamacpp.PROVIDER, provider_ollama.PROVIDER, provider_lmstudio.PROVIDER,
            provider_vllm.PROVIDER, provider_jan.PROVIDER, provider_koboldcpp.PROVIDER)


def built_in_online() -> tuple:
    import provider_online
    return tuple(provider_online.ONLINE)


_ADDED: list = []          # [(addon id, Provider)] registered by loaded add-ons (addon_api.add_provider)
_LISTENERS: list = []


def local() -> tuple:
    added = [p for _o, p in _ADDED if p.kind == "local"]
    added.sort(key=lambda p: LOCAL_ORDER.index(p.key) if p.key in LOCAL_ORDER else len(LOCAL_ORDER))
    return _built_in_local() + tuple(added)


def online() -> tuple:
    return _built_in_online() + tuple(p for _o, p in _ADDED if p.kind == "online")


def on_change(listener) -> None:
    """listener() runs after add-ons added or removed providers."""
    _LISTENERS.append(listener)


def changed() -> None:
    for listener in list(_LISTENERS):
        listener()


def add(provider: Provider, owner: str) -> None:
    """An add-on's provider; refused when the key or one of its settings ids is taken."""
    if not isinstance(provider, Provider) or not provider.key or provider.kind not in ("local", "online"):
        raise ValueError("not a Provider")
    taken_ids = {pid for p in online() for pid in p.ids}
    if any(p.key == provider.key for p in all_providers()) or taken_ids & set(provider.ids):
        raise ValueError(f"the provider {provider.key} exists already")
    _ADDED.append((owner, provider))


def remove_added(owner: str | None = None) -> None:
    _ADDED[:] = [(o, p) for o, p in _ADDED if owner is not None and o != owner]


def all_providers() -> tuple:
    return local() + online()


def get(key: str) -> Provider | None:
    return next((p for p in all_providers() if p.key == key), None)


# --- tables the feature modules use ----------------------------------------------------------------

def connect_keys() -> dict:
    return {p.key: p.connect_key for p in local()}


def connect_labels() -> dict:
    return {p.key: p.connect_label for p in local()}


def cloud_of_agent() -> dict:
    """agent command -> its online provider's name (graph.CLOUD)."""
    return {agent: p.name for p in online() for agent in p.agents}


def online_names() -> dict:
    """provider id in agent settings -> display name (graph.PROVIDER_NAMES)."""
    return {pid: p.name for p in online() for pid in p.ids}
