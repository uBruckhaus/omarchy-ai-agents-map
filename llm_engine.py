"""How the app works with a local LLM, as one object per provider (Provider.engine).

The core never asks "is this Ollama?": it asks the provider's engine. A provider add-on
that sets no engine gets Engine(), which fits any server with a user service and an
OpenAI-compatible API (/v1/models); one that works like a known family subclasses it:

- Engine: start/stop through the user service, models from /v1/models (all loaded),
  freeing GPU memory stops it (nothing finer is known)
- LlamaRouter: llama.cpp's router API (/models, /models/load, /models/unload)

A provider with its own ways (Ollama's /api/ps, LM Studio's lms, KoboldCpp's admin API)
overrides the methods in its own module (provider_<key>.py), which is its add-on.

Everything that talks to a server goes through gpu_usage.http_json (127.0.0.1, our own
listener only, bounded reply) or a bounded command; callers run it on a thread.
"""

from __future__ import annotations

import os
import re

# A model name given to a local LLM's API or CLI: never an option (a folder name in a shared
# models folder could start with "-"), one plain word.
MODEL_KEY = re.compile(r"[A-Za-z0-9][\w.:/@+-]{0,199}")

# How sure an engine is that a process is its own (owns_process): the highest claim wins, so
# LM Studio's bundled llama-server (by its path) is LM Studio's, not llama.cpp's (by its name).
BY_NAME, BY_PATH = 1, 2


def _run(argv: list) -> tuple[bool, str]:
    import ai_core
    out, ok = ai_core.read_bounded_output(argv, max_bytes=64 * 1024, timeout=30, merge_stderr=True)
    message = out.decode("utf-8", "replace").strip().splitlines()
    return ok, (message[-1] if message else "")[:300]


def _port(rt) -> int:
    import gpu_usage
    return gpu_usage.port_of(rt)


def _http(rt, path: str, method: str = "GET", body: dict | None = None, timeout: float = 1.5):
    import gpu_usage
    return gpu_usage.http_json(_port(rt), path, method, body, timeout=timeout)


def basename(path: str) -> str:
    return os.path.basename(path or "")


class Engine:
    """The defaults: a user service and an OpenAI-compatible server."""

    programs: tuple = ()          # program names (comm or argv[0] basename) that are this LLM
    start_command = ""            # how a user starts it by hand ("ollama serve"), for hints
    can_load = False              # it loads a model on request (a line LLM → model on the map)
    app_without_server = False    # an app that can be open with its API server off (LM Studio)

    # --- its processes -------------------------------------------------------------------------

    def owns_process(self, comm: str, argv: list) -> int:
        """0, BY_NAME or BY_PATH: how sure it is that this process is its own."""
        exe = basename(argv[0]) if argv else ""
        return BY_NAME if comm in self.programs or exe in self.programs else 0

    # --- start and stop ------------------------------------------------------------------------

    def can_control(self, rt) -> bool:
        return bool(rt.installed and rt.service)

    def start(self, rt) -> tuple[bool, str]:
        if rt.service:
            return _run(["systemctl", "--user", "start", f"{rt.service}.service"])
        return False, "no way to start it"

    def stop(self, rt) -> tuple[bool, str]:
        if rt.service:
            return _run(["systemctl", "--user", "stop", f"{rt.service}.service"])
        return False, "no way to stop it"

    # --- its models through its API ------------------------------------------------------------

    def loaded_models(self, rt) -> dict:
        """lower-case model name -> "loaded" / "loading" / "sleeping"."""
        found = {}
        for row in (_http(rt, "/v1/models") or {}).get("data", [])[:20]:
            if isinstance(row, dict) and row.get("id"):
                found[str(row["id"]).lower()] = "loaded"
        return found

    def load(self, rt, model: str) -> tuple[bool, str]:
        return False, "it cannot be asked to load a model"

    def unload_model(self, rt, model: str) -> tuple[bool, str]:
        return self.unload_all(rt)            # one model at a time anyway

    def unload_all(self, rt) -> tuple[bool, str]:
        """Free the GPU memory; the default cannot unload, so it stops the server."""
        return self.stop(rt) if self.can_control(rt) else (False, "no way to stop it")

    def model_context(self, rt, name: str) -> int:
        """The context it loads the model with (0: not known)."""
        return 0

    # --- what the user is told -----------------------------------------------------------------

    def add_model_step(self, rt) -> str:
        """Where its models come from, as one step ("right-click … → Models…")."""
        from i18n import _
        return _("right-click {0} on the map → Models… and download one that fits your card.").format(rt.name)

    def start_step(self, rt, agent_name: str) -> str:
        """How the user gets it running when the app cannot start it."""
        from i18n import _
        if self.start_command:
            return _("start it yourself ({0}) before working with {1}.").format(self.start_command, agent_name)
        return _("start it yourself before working with {0}.").format(agent_name)


class LlamaRouter(Engine):
    """llama.cpp's router API: every model listed with its state, loaded and unloaded on request."""

    can_load = True

    def loaded_models(self, rt) -> dict:
        found = {}
        for row in (_http(rt, "/models") or {}).get("data", [])[:500]:
            state = (row.get("status") or {}).get("value", "") if isinstance(row, dict) else ""
            if state in ("loaded", "loading", "sleeping"):
                found[str(row.get("id", "")).lower()] = state
        return found

    def load(self, rt, model: str) -> tuple[bool, str]:
        reply = _http(rt, "/models/load", "POST", {"model": model}, timeout=300)
        return (True, "") if reply is not None else (False, "it did not load the model")

    def unload_model(self, rt, model: str) -> tuple[bool, str]:
        reply = _http(rt, "/models/unload", "POST", {"model": model}, timeout=30)
        return (True, "") if reply is not None else (False, "it did not unload the model")

    def unload_all(self, rt) -> tuple[bool, str]:
        loaded = self.loaded_models(rt)
        rows = (_http(rt, "/models") or {}).get("data", [])
        ids = [str(r.get("id")) for r in rows if isinstance(r, dict) and str(r.get("id", "")).lower() in loaded]
        failed = [i for i in ids if _http(rt, "/models/unload", "POST", {"model": i}, timeout=30) is None]
        return (False, f"could not unload {', '.join(failed)[:200]}") if failed else (True, "")


DEFAULT = Engine()


def of(rt_or_key) -> Engine:
    """The engine of a runtime (or provider key). A provider that brings none (an add-on made
    before engines existed) gets the engine of the app's own module for that key, if there is
    one, else Engine()."""
    import providers
    key = rt_or_key if isinstance(rt_or_key, str) else rt_or_key.key
    provider = providers.get(key)
    engine = getattr(provider, "engine", None) if provider is not None else None
    if isinstance(engine, Engine):
        return engine
    built_in = next((p for p in providers.built_in_local() if p.key == key), None)
    engine = getattr(built_in, "engine", None)
    return engine if isinstance(engine, Engine) else DEFAULT


def runtime_of(comm: str, argv: list) -> str:
    """Which local LLM a process belongs to ("" for none): the surest claim wins."""
    import providers
    best, best_key = 0, ""
    for provider in providers.local():
        claim = of(provider.key).owns_process(comm, argv)
        if claim > best:
            best, best_key = claim, provider.key
    return best_key
