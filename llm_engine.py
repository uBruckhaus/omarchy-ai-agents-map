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


def change(path, new_text: str):
    """A connect.Change of one settings file (its text now, then new_text); None when it stays."""
    import connect
    from pathlib import Path
    path = Path(path)
    try:
        old_text = path.read_text(encoding="utf-8")[:1_000_000]
    except FileNotFoundError:
        old_text = ""
    except OSError:
        return None
    return connect.Change(path, old_text, new_text) if new_text != old_text else None


def env_with(text: str, name: str, value: str) -> str:
    """An environment file's text with NAME=value set (replaced in place, else added at the end)."""
    import re
    line = f"{name}={value}"
    if re.search(rf"^\s*(?:export\s+)?{re.escape(name)}=.*$", text, re.MULTILINE):
        return re.sub(rf"^\s*(?:export\s+)?{re.escape(name)}=.*$", lambda _m: line, text, count=1, flags=re.MULTILINE)
    return text + ("" if text.endswith("\n") or not text else "\n") + line + "\n"


def arg_number(argv, flag: str) -> int | None:
    """The number after --flag (or --flag=N) in a command line, else None."""
    for i, arg in enumerate(argv or []):
        value = argv[i + 1] if arg == flag and i + 1 < len(argv) else arg.split("=", 1)[1] \
            if arg.startswith(flag + "=") else None
        if value is not None and value.strip().lstrip("-").isdigit():
            return int(value)
    return None


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

    # --- how many models it keeps loaded at once ----------------------------------------------
    # "count": a number the app can set; "switch": one, or as many as fit (LM Studio); "": fixed.
    slots_kind = ""

    def slots(self, rt) -> int:
        """The models it keeps loaded at once (0: no limit)."""
        return 1

    def slots_fixed_by(self, rt) -> str:
        """Where the user fixed it themselves (their own start script), so the app does not change
        it; "" when the app may."""
        return ""

    def slot_changes(self, rt, count: int) -> list:
        """The settings changes (connect.Change) that let it keep `count` models loaded; [] when
        nothing needs to change or it cannot be changed here."""
        return []

    def after_slot_change(self, rt):
        """What else a slot change needs (systemd reading a changed unit)."""

    def model_size(self, rt, name: str) -> int:
        """The model file's size in bytes (0: not known), for the GPU memory warning."""
        return 0

    # --- its models on disk, and what the app's pages offer for it --------------------------------
    models_page = True          # Models… (search, download, context); False: its model is set elsewhere (vLLM)
    own_model_folder = True     # a model folder the app chooses and changes; False: its app's data folder (Jan)
    gguf_folder = False         # reads a folder of GGUF files: model folders, moving models between them
    gguf_layout = "flat"        # how it keeps them: "flat" files, "publisher" (publisher/repo/file), or
                                # "blobs" (a store of its own: its parts in store_parts)
    store_parts: tuple = ()     # "blobs": the folders of its store, moved together
    reorganize = False          # the Reorganize page may move its model folder
    stop_to_move = False        # must be stopped while its model folder moves (it holds the files open)
    models_root_rank = 9        # where the app looks first for one folder of all models (lower: earlier)

    def data_folders(self, rt) -> list:
        """Its own folders of models and settings ("~/…"), for disk usage: its provider's data."""
        import providers
        provider = providers.get(rt.key)
        built_in = next((p for p in providers.built_in_local() if p.key == rt.key), None)
        return list(getattr(provider, "data", ()) or getattr(built_in, "data", ()))

    def agent_context(self, rt) -> dict:
        """model -> the context it loads it with, for the agents' settings (llama.cpp's models.ini)."""
        return {}

    def connect_extra(self, rt, model: str) -> tuple:
        """(context, changes) a connection to `model` brings along (LM Studio: the model's context,
        set in its own settings, in the same preview, backup and Undo)."""
        return {}, []

    def gpu_build(self, vendor: str) -> str:
        """The graphics interface its build uses on this card ("ROCm", "CUDA"), for the install page."""
        return ""

    def refresh(self, rt):
        """When the app opens, and after a model was added: bring its own state up to date
        (KoboldCpp: one entry per model file)."""

    # How the provider watcher starts and stops it when it has no user service: one of the watcher's
    # own controllers (provider_watch.CONTROLS: "lms"), never a command line.
    watch_control = ""

    # Its usual ports: an agent's provider at this machine on one of them is this local LLM.
    ports: tuple = ()

    # Its own chat uses MCP servers (harnesses.Mcp): read and shown with the agents', never written.
    mcp = None

    # --- the Models page: adding, removing and tuning its models ----------------------------------
    download = ""               # "file": GGUF files into its folder; "pull": its own download; "": not here
    removable = ""              # "file": its files to the trash; "api": its own delete; "": in its own app
    context_settable = False    # the page sets a model's context for it
    context_effect = ""         # when a new context counts: "restart", "next-load", "" (at once)

    def models_page_note(self, rt) -> str:
        """A line above its model lists (where its models come from, what happens on a download)."""
        return ""

    def context_now(self, rt, folder, name: str):
        """(context now, KV cache type, model file) of an installed model; None: not known here."""
        return None

    def context_changes(self, rt, name: str, ctx: int):
        """The settings changes (connect.Change) that give `name` the context ctx (backup, Undo);
        None when it is set another way (set_context). Raises ValueError when it cannot."""
        return None

    def set_context(self, rt, name: str, ctx: int):
        raise ValueError("the context is set in the local LLM itself")

    def pull_name(self, rt, repo: str, entry, base: str) -> str:
        """download "pull": the name it downloads a Hugging Face file under; ValueError: it cannot."""
        raise ValueError("no download of its own")

    def pull(self, rt, name: str, progress, cancelled):
        """download "pull": its own download of `name` (started first when it does not run)."""
        raise ValueError("no download of its own")

    def add_file(self, rt, path):
        """A GGUF file on this PC becomes one of its models: into its folder (links where it can)."""
        import os
        from pathlib import Path
        import models_ctl
        if not rt.models_dir:
            raise ValueError(f"{rt.name} has no model folder yet")
        models_ctl.add_local(rt.key, Path(os.path.expanduser(rt.models_dir)), path)
        self.refresh(rt)

    def remove(self, rt, name: str):
        """removable "api": its own delete of `name`."""
        raise ValueError("not removed here")

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


def layout(rt_or_key) -> str:
    """How a local LLM keeps its model files: "flat" (GGUF files in its folder), "publisher"
    (publisher/repo/file), "blobs" (a store of its own), or "" (not in a folder the app manages)."""
    engine = of(rt_or_key)
    if engine.gguf_folder:
        return "publisher" if engine.gguf_layout == "publisher" else "flat"
    return "blobs" if engine.gguf_layout == "blobs" else ""


def runtime_of(comm: str, argv: list) -> str:
    """Which local LLM a process belongs to ("" for none): the surest claim wins."""
    import providers
    best, best_key = 0, ""
    for provider in providers.local():
        claim = of(provider.key).owns_process(comm, argv)
        if claim > best:
            best, best_key = claim, provider.key
    return best_key
