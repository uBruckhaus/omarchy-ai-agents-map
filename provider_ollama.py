"""Ollama: the ollama service and its model store."""

import llm_engine
from harnesses import Configure, Way
from providers import Provider


def _detect(processes):
    import inventory
    return inventory.ollama(processes)


def _installer():
    import install_ctl
    return install_ctl.Ollama()


class Engine(llm_engine.Engine):
    programs = ("ollama",)
    start_command = "ollama serve"
    can_load = True

    def loaded_models(self, rt):
        found = {}
        for row in (llm_engine._http(rt, "/api/ps") or {}).get("models", [])[:100]:
            if isinstance(row, dict) and row.get("name"):
                found[str(row["name"]).lower()] = "loaded"
        return found

    def _keep(self, rt, model, keep_alive, timeout):
        reply = llm_engine._http(rt, "/api/generate", "POST", {"model": model, "keep_alive": keep_alive}, timeout=timeout)
        return reply is not None

    def load(self, rt, model):
        return (True, "") if self._keep(rt, model, "30m", 300) else (False, "it did not load the model")

    def unload_model(self, rt, model):
        return (True, "") if self._keep(rt, model, 0, 30) else (False, "it did not unload the model")

    def unload_all(self, rt):
        rows = (llm_engine._http(rt, "/api/ps") or {}).get("models", [])
        ids = [str(r.get("name")) for r in rows if isinstance(r, dict) and r.get("name")]
        failed = [i for i in ids if not self._keep(rt, i, 0, 30)]
        return (False, f"could not unload {', '.join(failed)[:200]}") if failed else (True, "")

    # How many models it keeps loaded: OLLAMA_MAX_LOADED_MODELS (per graphics card) in the env file
    # the app writes for its own install; without it, Ollama's own default.
    slots_kind = "count"
    DEFAULT_SLOTS = 3

    def slots(self, rt):
        import install_ctl
        value = install_ctl.read_env(install_ctl.Ollama.env_file).get("OLLAMA_MAX_LOADED_MODELS", "")
        return int(value) if value.isdigit() else self.DEFAULT_SLOTS

    def slots_fixed_by(self, rt):
        import install_ctl
        # Not the app's service (a system or hand install): its settings are not the app's to change.
        return "" if install_ctl.Ollama.env_file.is_file() else "OLLAMA_MAX_LOADED_MODELS"

    def slot_changes(self, rt, count):
        import install_ctl
        if self.slots_fixed_by(rt) or count < 1:
            return []
        env_file = install_ctl.Ollama.env_file
        text = env_file.read_text(encoding="utf-8")
        found = llm_engine.change(env_file, llm_engine.env_with(text, "OLLAMA_MAX_LOADED_MODELS", str(count)))
        return [found] if found is not None else []

    def model_size(self, rt, name):
        import os
        from pathlib import Path
        import context_ctl
        if not rt.models_dir:
            return 0
        try:
            blob = context_ctl.ollama_model_file(Path(os.path.expanduser(rt.models_dir)), name)
            return blob.stat().st_size if blob is not None else 0
        except (OSError, ValueError):
            return 0

    def model_context(self, rt, name):
        import os
        from pathlib import Path
        import context_ctl
        import install_ctl
        if not rt.models_dir:
            return 0
        env = install_ctl.read_env(install_ctl.Ollama.env_file)
        return context_ctl.ollama_context(Path(os.path.expanduser(rt.models_dir)), name,
                                          int(env.get("OLLAMA_CONTEXT_LENGTH", "0") or 0))


PROVIDER = Provider("ollama", "Ollama", "local", detect=_detect, installer=_installer, engine=Engine(),
                    connect_key="ollama-local", connect_label="Ollama (local)",
                    install=(Way("installer"), Way("package", packages=(("pacman", "ollama"),))),
                    data=("~/.ollama",),
                    configure=(Configure("Ollama's menu", ("ollama", "launch")),),
                    homepage="https://ollama.com")
