"""LM Studio: LM Studio's server (lms) and its models folder."""

import json
import shutil
import time
from pathlib import Path

import llm_engine
from harnesses import Configure, Mcp, Way
from providers import Provider


def _detect(processes):
    import inventory
    return inventory.lmstudio(processes)


def _installer():
    import install_ctl
    return install_ctl.LmStudio()


def lms_binary() -> str | None:
    found = shutil.which("lms")
    if found:
        return found
    candidate = Path.home() / ".lmstudio/bin/lms"
    return str(candidate) if candidate.is_file() else None


LMS_EVERY = 20           # seconds between two `lms ps` runs
_lms_cache = [0.0, {}]
MAX_REPLY = 1024 * 1024        # as gpu_usage.MAX_REPLY


def lms_loaded(runner=None) -> dict:
    """LM Studio's loaded models from its own CLI (`lms ps --json`), for when its server is off;
    asked at most every LMS_EVERY seconds, since the CLI takes a moment to start."""
    now = time.monotonic()
    if runner is None and _lms_cache[0] and now - _lms_cache[0] < LMS_EVERY:
        return dict(_lms_cache[1])
    import ai_core
    found, binary = {}, lms_binary()
    if binary:
        out, ok = (runner or ai_core.read_bounded_output)([binary, "ps", "--json"], max_bytes=MAX_REPLY, timeout=5)
        try:
            rows = json.loads(out) if ok and out else []
        except ValueError:
            rows = []
        for row in (rows if isinstance(rows, list) else [])[:100]:
            if isinstance(row, dict) and row.get("type") == "llm" and isinstance(row.get("identifier"), str):
                found[row["identifier"].lower()[:200]] = "loaded"
    if runner is None:
        _lms_cache[:] = [now, found]
    return found


class Engine(llm_engine.Engine):
    """LM Studio has no user service: its own CLI (lms) starts and stops its server and loads models."""
    can_load = True
    app_without_server = True
    gguf_folder = True
    gguf_layout = "publisher"           # publisher/repo/file, as LM Studio downloads them
    reorganize = True
    models_root_rank = 0                # its folder first, when the app looks for one folder of all models
    mcp = Mcp(path="~/.lmstudio/mcp.json")      # its chat's MCP servers
    download = "file"
    removable = "file"
    context_settable = True
    context_effect = "next-load"

    def context_now(self, rt, folder, name):
        import context_ctl
        entry = context_ctl.lmstudio_entry(name)
        now, cache = context_ctl.lmstudio_settings(entry)
        return now, cache, context_ctl.lmstudio_model_file(entry)

    def context_changes(self, rt, name, ctx):
        # LM Studio's own per-model defaults file (backup, Undo).
        import connect
        import context_ctl
        from i18n import _
        path = context_ctl.lmstudio_defaults_file(context_ctl.lmstudio_entry(name))
        if path is None:
            raise ValueError(_("LM Studio does not list this model"))
        before = connect._read(path)
        return [connect.Change(path, before, context_ctl.lmstudio_context_text(before, ctx))]

    def connect_extra(self, rt, model):
        """LM Studio loads a model with 8192 tokens unless told otherwise: too few for an agent. Its
        context is set with the connection (same preview, backup and Undo) and handed to the agent
        too, unless the user set one in LM Studio already."""
        import context_ctl
        import gpu_usage
        if not model:
            return {}, []
        ctx, path, old_text, new_text = context_ctl.lmstudio_auto_context(model, gpu_usage.gpu_totals()[1])
        import connect
        changes = [connect.Change(path, old_text, new_text)] if path is not None and new_text != old_text else []
        return ({model: ctx} if ctx else {}), changes

    def owns_process(self, comm, argv):
        exe = argv[0] if argv else ""
        if ".lmstudio" in exe or comm.lower().startswith("lm studio") \
                or llm_engine.basename(exe).lower() in ("lm-studio", "lm studio"):
            return llm_engine.BY_PATH       # also its bundled llama-server
        return 0

    def can_control(self, rt):
        return bool(rt.installed and (rt.service or lms_binary()))

    def _lms(self, *args):
        binary = lms_binary()
        return llm_engine._run([binary, *args]) if binary else (False, "lms not found")

    def start(self, rt):
        return super().start(rt) if rt.service else self._lms("server", "start")

    def stop(self, rt):
        return super().stop(rt) if rt.service else self._lms("server", "stop")

    def loaded_models(self, rt):
        import gpu_usage
        if not gpu_usage._own_port(gpu_usage.port_of(rt)):
            return lms_loaded()            # the app is open without its server
        found = {}
        for row in (llm_engine._http(rt, "/api/v0/models") or {}).get("data", [])[:500]:
            if isinstance(row, dict) and row.get("state") == "loaded":
                found[str(row.get("id", "")).lower()] = "loaded"
        return found

    def load(self, rt, model):
        return self._lms("load", "--", model)

    def unload_model(self, rt, model):
        return self._lms("unload", "--", model)

    def unload_all(self, rt):
        return self._lms("unload", "--all")

    # LM Studio keeps one model loaded on request ("Only keep last JIT loaded model",
    # developer.unloadPreviousJITModelOnLoad in its settings.json), or every one asked for until
    # its idle time ends: one, or as many as fit. It reads the setting when it starts.
    slots_kind = "switch"
    SETTINGS = Path.home() / ".lmstudio/settings.json"

    def _settings(self) -> dict:
        try:
            data = json.loads(self.SETTINGS.read_text(encoding="utf-8")[:2_000_000])
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def slots(self, rt):
        developer = self._settings().get("developer")
        one = developer.get("unloadPreviousJITModelOnLoad", True) if isinstance(developer, dict) else True
        return 1 if one is not False else 0

    def slots_fixed_by(self, rt):
        return "" if self.SETTINGS.is_file() else "LM Studio"

    def slot_changes(self, rt, count):
        if self.slots_fixed_by(rt) or count < 1:
            return []
        data = self._settings()
        developer = data.get("developer") if isinstance(data.get("developer"), dict) else {}
        if developer.get("unloadPreviousJITModelOnLoad", True) is not False and count > 1 or \
                developer.get("unloadPreviousJITModelOnLoad", True) is False and count == 1:
            data["developer"] = {**developer, "unloadPreviousJITModelOnLoad": count == 1}
            found = llm_engine.change(self.SETTINGS, json.dumps(data, indent=2) + "\n")
            return [found] if found is not None else []
        return []

    def model_size(self, rt, name):
        import context_ctl
        point = context_ctl.lmstudio_entry(name).get("entryPoint")
        path = point.get("absPath") if isinstance(point, dict) else None
        try:
            return Path(path).stat().st_size if isinstance(path, str) else 0
        except OSError:
            return 0

    def model_context(self, rt, name):
        import context_ctl
        return context_ctl.lmstudio_settings(context_ctl.lmstudio_entry(name))[0]

    def add_model_step(self, rt):
        from i18n import _
        return _("download one in LM Studio (Discover), or right-click LM Studio on the map → Models….")

    def start_step(self, rt, agent_name):
        from i18n import _
        return _("open LM Studio and turn on its server (Developer → Start server) before working with {0}.") \
            .format(agent_name)


PROVIDER = Provider("lmstudio", "LM Studio", "local", detect=_detect, installer=_installer, engine=Engine(),
                    connect_key="lmstudio-local", connect_label="LM Studio (local)",
                    install=(Way("installer"), Way("manual", url="https://lmstudio.ai/download")), data=("~/.lmstudio",),
                    configure=(Configure("Open LM Studio", ("lm-studio",), window=True),
                               Configure("Its server and loaded models", ("lms", "status"))),
                    homepage="https://lmstudio.ai")
