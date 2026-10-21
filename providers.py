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

import dataclasses
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
    download: object = None           # harnesses.Download: what the app installs, as data (install_ctl.Declared)
    connect_key: str = ""             # the provider id agents get in their settings ("llama-local")
    connect_label: str = ""           # its display name there ("llama.cpp (local)")
    stays_up: bool = False            # the watcher unloads its model instead of stopping it
    unload: tuple | None = None       # (path, JSON body) of its unload request
    install: tuple = ()               # ways to install / remove it (harnesses.Way), best first
    data: tuple = ()                  # "~/…" folders with its models and settings: never removed by us
    homepage: str = ""                # its web page (the Add-ons page's Homepage button)
    usage: Callable | None = None     # usage(runtime) -> agent_status.Usage from its own logs or metrics
                                      # (else the app derives it from the agents' sessions)
    engine: object = None             # llm_engine.Engine: how the app starts, stops, asks and explains it
                                      # (None: llm_engine.Engine(), a user service + an OpenAI-compatible API)
    # online providers
    ids: tuple = ()                   # names used in agent settings ("anthropic", "openai-codex")
    agents: tuple = field(default=())  # agents that come with this provider ("claude")


_HIDDEN: set = set()       # built-in providers an installed add-on brings instead (hide_built_in)
LOCAL_ORDER = ("llamacpp", "ollama", "lmstudio", "vllm", "jan", "koboldcpp")


def hide_built_in(keys) -> None:
    """Asterism: these providers come as add-ons (see harnesses.hide_built_in)."""
    _HIDDEN.clear()
    _HIDDEN.update(keys)
    _forget()


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
_EXTENSIONS: list = []     # [(addon id, key, {field: value})] from extension add-ons (extend)
_LISTENERS: list = []
_CACHE: dict = {}          # "local" / "online" -> the merged tuple, made again after any change

# What an extension add-on may add to a provider another add-on (or the app) brings, as for agents
# (harnesses.EXTENDABLE): facts for its page and the map, never what installs or names it.
EXTENDABLE = ("usage", "unload", "data", "homepage", "agents")


def _forget() -> None:
    _CACHE.clear()


def _empty(provider: Provider, name: str) -> bool:
    f = next(f for f in dataclasses.fields(Provider) if f.name == name)
    default = f.default if f.default is not dataclasses.MISSING else f.default_factory()
    return getattr(provider, name) == default


def _extended(provider: Provider) -> Provider:
    """The provider with what extension add-ons add: only empty fields, the first one wins."""
    filled = {}
    for _owner, key, fields in _EXTENSIONS:
        if key != provider.key:
            continue
        for name, value in fields.items():
            if name not in filled and _empty(provider, name):
                filled[name] = value
    return dataclasses.replace(provider, **filled) if filled else provider


def local() -> tuple:
    if "local" not in _CACHE:
        added = [p for _o, p in _ADDED if p.kind == "local"]
        added.sort(key=lambda p: LOCAL_ORDER.index(p.key) if p.key in LOCAL_ORDER else len(LOCAL_ORDER))
        _CACHE["local"] = tuple(_extended(p) for p in _built_in_local() + tuple(added))
    return _CACHE["local"]


def online() -> tuple:
    if "online" not in _CACHE:
        _CACHE["online"] = tuple(_extended(p) for p in
                                 _built_in_online() + tuple(p for _o, p in _ADDED if p.kind == "online"))
    return _CACHE["online"]


def on_change(listener) -> None:
    """listener() runs after add-ons added or removed providers."""
    _LISTENERS.append(listener)


def changed() -> None:
    _forget()
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
    _forget()


def remove_added(owner: str | None = None) -> None:
    """Forget the providers and extensions of one add-on (or of all add-ons)."""
    _ADDED[:] = [(o, p) for o, p in _ADDED if owner is not None and o != owner]
    _EXTENSIONS[:] = [e for e in _EXTENSIONS if owner is not None and e[0] != owner]
    _forget()


def extend(key: str, fields: dict, owner: str) -> list:
    """An extension add-on adds facts to a provider (EXTENDABLE fields only); returns the fields
    refused. Used only where the provider left them empty; the first extension wins (conflicts)."""
    if not isinstance(key, str) or not key:
        raise ValueError("no provider key")
    refused = [name for name in fields if name not in EXTENDABLE]
    allowed = {name: value for name, value in fields.items() if name in EXTENDABLE and value is not None}
    if allowed:
        _EXTENSIONS.append((owner, key, allowed))
        _forget()
    return refused


def conflicts() -> dict:
    """addon id -> ["key.field", …] it added but that is not used (as harnesses.conflicts)."""
    base = {p.key: p for p in _built_in_local() + _built_in_online() + tuple(p for _o, p in _ADDED)}
    taken, found = set(), {}
    for owner, key, fields in _EXTENSIONS:
        provider = base.get(key)
        for name in fields:
            if provider is not None and (not _empty(provider, name) or (key, name) in taken):
                found.setdefault(owner, []).append(f"{key}.{name}")
            taken.add((key, name))
    return found


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
