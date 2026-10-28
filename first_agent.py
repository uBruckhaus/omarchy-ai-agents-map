"""The first-agent guide: for a PC with no working agent, the way to one that answers, online (a plan or an
API key) or on this PC (a local LLM, a starter model that fits the graphics card, an agent connected to it).

The routes and the starter models are data (data/first-agent.json, or a signed catalog add-on's copy of it,
read first): each route names its agent (and, for one on this PC, its local LLM) by key, what it needs
before (mise, Node) and how one gets access; the agents' own facts (install ways, sign-in, the ask command)
come from their adapters, so the core names no agent. Everything is checked when it is read (load); an
entry that does not hold up is left out.

The guide's steps, each with the app's own pieces (first_agent_page): what is missing first (mise through
install_ctl.Mise, Node through mise), the agent (its install ways, in a terminal), then sign-in or key
(signin.py) or the local LLM (install_ctl) with a starter model (the Models download) and the connection
(connect.plan, preview, backup, Undo), and last a test: one fixed question through the agent's ask command
(verify_argv), its answer shown. Only that marks the agent as working (settings "first_agent").
"""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
CATALOG = HERE / "data/first-agent.json"
MAX_CATALOG = 256 * 1024
ID = re.compile(r"[a-z0-9][a-z0-9.-]{0,47}")
KEY = re.compile(r"[a-z0-9][a-z0-9_-]{0,47}")
REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}/[A-Za-z0-9][A-Za-z0-9._-]{0,95}")
GGUF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,159}\.gguf")
NEEDS = ("mise", "node")
KINDS = ("online", "local")
MIN_CONTEXT = 16384               # what link_check asks of an agent's context
TEST_PROMPT = "Reply with one short sentence: are you ready?"


@dataclass(frozen=True)
class Route:
    id: str
    kind: str                     # online | local
    agent: str                    # the agent's command (its adapter)
    title: str
    access: str = ""
    pricing: str = ""             # https: what it costs, on the provider's own page
    runtime: str = ""             # local: the local LLM's key
    needs: tuple = ()             # tools before the agent ("mise", "node")
    name: str = ""                # the agent's own name (shown while its add-on is still off)


@dataclass(frozen=True)
class Model:
    id: str
    title: str
    repo: str                     # Hugging Face repo
    file: str                     # its GGUF file
    bytes: int
    min_vram: int                 # video memory it needs with its context, bytes
    license: str = ""
    context: int = 32768


@dataclass
class Catalog:
    routes: list = field(default_factory=list)
    models: list = field(default_factory=list)


def _text(value, most: int) -> str:
    return " ".join(str(value).split())[:most] if isinstance(value, str) else ""


def _int(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def parse(data) -> Catalog:
    """Only entries that hold up: known kinds and needs, plain ids, https pages, Hugging Face names."""
    catalog = Catalog()
    if not isinstance(data, dict):
        return catalog
    for item in data.get("routes") or []:
        if not isinstance(item, dict):
            continue
        rid, kind, agent = item.get("id"), item.get("kind"), item.get("agent")
        runtime = item.get("runtime") or ""
        needs = item.get("needs") or []
        pricing = item.get("pricing") or ""
        if not (isinstance(rid, str) and ID.fullmatch(rid) and kind in KINDS and isinstance(agent, str)
                and KEY.fullmatch(agent) and isinstance(needs, list) and all(n in NEEDS for n in needs)
                and (not runtime or (isinstance(runtime, str) and KEY.fullmatch(runtime)))
                and (kind != "local" or runtime)
                and (not pricing or (isinstance(pricing, str) and re.fullmatch(r"https://[A-Za-z0-9./_-]{1,200}", pricing)))):
            continue
        title = _text(item.get("title"), 60)
        if title:
            catalog.routes.append(Route(rid, kind, agent, title, _text(item.get("access"), 400), pricing,
                                        runtime, tuple(dict.fromkeys(needs)), _text(item.get("name"), 40) or title))
    for item in data.get("models") or []:
        if not isinstance(item, dict):
            continue
        repo, name = item.get("repo"), item.get("file")
        mid, size, vram = item.get("id"), _int(item.get("bytes")), _int(item.get("min_vram"))
        context = _int(item.get("context")) or 32768
        if not (isinstance(mid, str) and ID.fullmatch(mid) and isinstance(repo, str) and REPO.fullmatch(repo)
                and isinstance(name, str) and GGUF.fullmatch(name) and size and vram and context >= MIN_CONTEXT):
            continue
        title = _text(item.get("title"), 80)
        if title:
            catalog.models.append(Model(mid, title, repo, name, size, vram, _text(item.get("license"), 40), context))
    return catalog


def load(path: Path | None = None, loaded_addons=()) -> Catalog:
    """The catalog: a signed, switched-on add-on's first-agent.json (loaded_addons: the app's add-ons;
    their files were checked against the signature when they were loaded), else the app's own."""
    for candidate in ([path] if path else [*addon_catalogs(loaded_addons), CATALOG]):
        try:
            if candidate.is_symlink() or not candidate.is_file() or candidate.stat().st_size > MAX_CATALOG:
                continue
            found = parse(json.loads(candidate.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
        if found.routes:
            return found
    return Catalog()


def addon_catalogs(loaded_addons) -> list[Path]:
    """first-agent.json of the add-ons that are loaded (signed and switched on) and list it among
    their files, in their order: the first one wins."""
    return [Path(a.folder) / "first-agent.json" for a in loaded_addons
            if getattr(a, "state", "") == "loaded" and "first-agent.json" in (getattr(a, "files", None) or {})]


# --- what this PC has ---------------------------------------------------------------------------------

def has_tool(name: str) -> bool:
    if shutil.which(name):
        return True
    own = Path.home() / ".local/bin" / name           # installed by the guide, PATH not reread yet
    return own.is_file() and os.access(own, os.X_OK)


def node_ready() -> bool:
    """Node for agents mise installs from npm: on PATH, or mise's own (it finds it there)."""
    if has_tool("node"):
        return True
    return any((Path.home() / ".local/share/mise/installs/node").glob("*/bin/node"))


def missing(route: Route) -> list[str]:
    """What the route needs before its agent can be installed, in order ("mise", "node")."""
    out = []
    for need in route.needs:
        if need == "mise" and not has_tool("mise"):
            out.append("mise")
        elif need == "node" and not node_ready():
            out.append("node")
    return out


def blockers(route: Route) -> list[str]:
    """What the guide cannot install itself and the PC lacks (the page says how to get it)."""
    out = [tool for tool in ("curl", "tar") if not has_tool(tool)]
    if route.kind == "local" and not has_tool("systemctl"):
        out.append("systemctl")
    return out


def node_step() -> list[str]:
    """mise installs Node for the current user (no sudo): argv for a terminal tab."""
    mise = shutil.which("mise") or str(Path.home() / ".local/bin/mise")
    return [mise, "--yes", "use", "-g", "node@lts"]


# --- starter models --------------------------------------------------------------------------------

def fit(model: Model, vram: int) -> str:
    """"fits" (on the card with its context), "partly" (some layers in RAM: slower), "too large"."""
    if vram and vram >= model.min_vram:
        return "fits"
    if vram and vram >= model.bytes * 1.1:
        return "partly"
    return "too large" if vram else "unknown"


def suggested(models: list, vram: int) -> Model | None:
    """The largest one that fits; without a known card the smallest."""
    fitting = [m for m in models if fit(m, vram) == "fits"]
    if fitting:
        return max(fitting, key=lambda m: m.bytes)
    return min(models, key=lambda m: m.bytes) if models else None


# --- working: the test -------------------------------------------------------------------------------

def verify_argv(command: str) -> list[str] | None:
    """The agent's own one-question command (its adapter's ask fact: no tools, no session kept) with the
    fixed test prompt; None when it has none."""
    import harnesses
    harness = harnesses.get(command)
    argv = list(harness.ask) if harness is not None else []
    if not argv or "{prompt}" not in argv:
        return None
    binary = shutil.which(argv[0]) or argv[0]
    return [binary] + [TEST_PROMPT if a == "{prompt}" else a for a in argv[1:]]


def answer_ok(out: bytes, ok: bool) -> str:
    """The answer when the test worked (it ended well and said something), else ""."""
    text = out.decode("utf-8", "replace").strip()
    return text[-600:] if ok and len(text) >= 2 else ""


# --- state (settings "first_agent") ---------------------------------------------------------------------

def state() -> dict:
    import ai_core
    value = ai_core.setting("first_agent")
    return value if isinstance(value, dict) else {}


def working(installed) -> list[str]:
    """Agents the guide's test has seen answer, still installed."""
    return [c for c in state().get("verified", []) if isinstance(c, str) and c in installed]


def needs_guide(installed, signed_in) -> bool:
    """No agent works yet: none installed, or none signed in or connected (signed_in(command) -> bool),
    and none passed the test. An agent set up before the guide existed counts as working."""
    if working(installed):
        return False
    return not any(signed_in(c) for c in installed)
