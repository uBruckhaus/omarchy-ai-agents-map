"""KoboldCpp: a KoboldCpp server; stays up, its model is unloaded instead."""

import llm_engine
from harnesses import Way
from providers import Provider


def _detect(processes):
    import inventory
    return inventory.koboldcpp(processes)


def _installer():
    import install_ctl
    return install_ctl.KoboldCpp()


class Engine(llm_engine.Engine):
    start_command = "koboldcpp --admin --routermode"

    def owns_process(self, comm, argv):
        exe = llm_engine.basename(argv[0] if argv else "")
        return llm_engine.BY_NAME if comm == "koboldcpp" or exe.startswith("koboldcpp") else 0

    def loaded_models(self, rt):
        # Router mode lists the loaded model first as "koboldcpp/<name>" ("inactive": none),
        # then the .kcpps model ids.
        rows = (llm_engine._http(rt, "/v1/models") or {}).get("data", [])[:500]
        first = str(rows[0].get("id", "")) if rows and isinstance(rows[0], dict) else ""
        return {first.split("/", 1)[1].lower() + ".kcpps": "loaded"} if first.startswith("koboldcpp/") else {}

    def unload_all(self, rt):
        if not self.loaded_models(rt):
            return True, ""
        reply = llm_engine._http(rt, "/api/admin/reload_config", "POST", {"filename": "unload_model"}, timeout=30)
        return (True, "") if isinstance(reply, dict) and reply.get("success") else (False, "KoboldCpp did not unload")

    def model_context(self, rt, name):
        import kobold_ctl
        return kobold_ctl.settings(name)[0]


PROVIDER = Provider("koboldcpp", "KoboldCpp", "local", detect=_detect, installer=_installer, engine=Engine(),
                    connect_key="koboldcpp-local", connect_label="KoboldCpp (local)",
                    # Kept running between agents (slow to start, no video memory without a model):
                    # the watcher unloads its model instead of stopping it.
                    stays_up=True, unload=("/api/admin/reload_config", {"filename": "unload_model"}),
                    install=(Way("installer"),),
                    homepage="https://github.com/LostRuins/koboldcpp")
