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
    ports = (11434,)
    programs = ("ollama",)
    start_command = "ollama serve"
    can_load = True

    def loaded_models(self, rt):
        found = {}
        for row in llm_engine.rows(llm_engine._http(rt, "/api/ps"), "models", 100):
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
        listed = llm_engine.rows(llm_engine._http(rt, "/api/ps"), "models", 100)
        ids = [str(r.get("name")) for r in listed if isinstance(r, dict) and r.get("name")]
        failed = [i for i in ids if not self._keep(rt, i, 0, 30)]
        return (False, f"could not unload {', '.join(failed)[:200]}") if failed else (True, "")

    # How many models it keeps loaded: OLLAMA_MAX_LOADED_MODELS (per graphics card) in the env file
    # the app writes for its own install; without it, Ollama's own default.
    slots_kind = "count"
    DEFAULT_SLOTS = 3
    reorganize = True
    stop_to_move = True                 # it holds its blobs open
    gguf_layout = "blobs"               # its own store in OLLAMA_MODELS
    store_parts = ("blobs", "manifests", "manifests-v2", "metadata")
    models_root_rank = 1
    download = "pull"
    removable = "api"
    context_settable = True

    def models_page_note(self, rt):
        from i18n import _
        return _("Ollama is started for a download if it is not running.")

    def context_now(self, rt, folder, name):
        import context_ctl
        import install_ctl
        env = install_ctl.read_env(install_ctl.Ollama.env_file)
        now = context_ctl.ollama_context(folder, name, int(env.get("OLLAMA_CONTEXT_LENGTH", "0") or 0))
        return now, env.get("OLLAMA_KV_CACHE_TYPE", "f16"), context_ctl.ollama_model_file(folder, name)

    def set_context(self, rt, name, ctx):
        import context_ctl
        context_ctl.ollama_set_context(name, ctx, rt.address)

    def pull_name(self, rt, repo, entry, base):
        import models_ctl
        from i18n import _
        if (base or models_ctl.HF) != models_ctl.HF:
            raise ValueError(_("Ollama downloads Hugging Face models only from huggingface.co."))
        return models_ctl.ollama_name_for(repo, entry)

    def pull(self, rt, name, progress, cancelled):
        import time
        import models_ctl
        import runtime_ctl
        from i18n import _
        port = int(rt.address.rsplit(":", 1)[-1])
        if not models_ctl.loopback_listeners(port):
            progress(0.0, _("Starting Ollama…"))
            runtime_ctl.start(rt)
            for _attempt in range(40):
                if models_ctl.loopback_listeners(port):
                    break
                time.sleep(0.5)
        models_ctl.ollama_pull(name, rt.address, progress, cancelled)

    def add_file(self, rt, path):
        import models_ctl
        models_ctl.ollama_create(path, rt.address)

    def remove(self, rt, name):
        import models_ctl
        models_ctl.ollama_delete(name, rt.address)

    # Its server's settings in the app's env file (only when the app installed it): for every model;
    # a restart makes them count.
    tuning = {
        "OLLAMA_FLASH_ATTENTION": (r"0|1", "flash attention (1): less memory for the context, often faster"),
        "OLLAMA_KV_CACHE_TYPE": (r"f16|q8_0|q4_0", "the context's cache: q8_0 halves its memory (needs flash attention)"),
        "OLLAMA_NUM_PARALLEL": (r"[1-8]", "requests per model served at once (each needs its own context)"),
        "OLLAMA_MAX_LOADED_MODELS": (r"[1-8]", "models kept loaded at once"),
    }
    tuning_per_model = False

    def tuning_now(self, rt):
        import install_ctl
        if self.slots_fixed_by(rt):
            return {}
        env = install_ctl.read_env(install_ctl.Ollama.env_file)
        return {"*": {k: v for k, v in env.items() if k in self.tuning}}

    def tuning_changes(self, rt, section, key, value):
        import re
        import install_ctl
        if self.slots_fixed_by(rt):
            raise ValueError("not the app's own Ollama service: change it where it was set up")
        if section != "*" or key not in self.tuning or not re.fullmatch(self.tuning[key][0], value):
            raise ValueError(f"{key} = {value} is not a setting the app changes")
        env_file = install_ctl.Ollama.env_file
        found = llm_engine.change(env_file, llm_engine.env_with(env_file.read_text(encoding="utf-8"), key, value))
        return [found] if found is not None else []

    def gpu_build(self, vendor):
        return {"amd": "ROCm", "nvidia": "CUDA"}.get(vendor, "")

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


def _discover_existing(folder, names, mount, device):
    from existing_ai import safe, program
    rows = []
    candidate = None
    for relative in ('current/bin/ollama', 'current/ollama', 'ollama'):
        if relative.split('/')[0] in names:
            candidate = program(folder / relative, mount, device)
            if candidate:
                break
    if candidate:
        rows.append({'kind': 'runtime', 'provider': PROVIDER.key, 'name': PROVIDER.name,
                     **candidate, 'models': '', 'count': 0, 'environment': {}, 'dependencies': []})
    if ('manifests' in names or 'manifests-v2' in names) and 'blobs' in names:
        if safe(folder / 'blobs', mount, device) and any(safe(folder / n, mount, device)
                                                       for n in ('manifests', 'manifests-v2')):
            rows.append({'kind': 'store', 'provider': PROVIDER.key, 'name': PROVIDER.name,
                         'path': str(folder), 'models': '', 'count': 0, 'environment': {}, 'dependencies': []})
    return rows


def _reconnect_existing(record, models):
    from pathlib import Path
    from flutter_existing import wrapper, service_files
    if models is None or models['kind'] != 'store' or models['provider'] != PROVIDER.key:
        raise ValueError('Choose a discovered model store for this provider')
    record = {**record, 'environment': {**record['environment'], 'OLLAMA_MODELS': models['path'],
                                      'OLLAMA_HOST': '127.0.0.1:11434'}}
    env = 'OLLAMA_MODELS="' + models['path'] + '"\nOLLAMA_HOST=127.0.0.1:11434\n'
    return service_files(record, 'ollama', wrapper(record, ['serve']),
                         [record['mount']['path'], models['mount']['path']],
                         [(Path('.config/ollama/server.env'), env)])


import dataclasses as _dataclasses
PROVIDER = _dataclasses.replace(PROVIDER, discover_existing=_discover_existing, reconnect_existing=_reconnect_existing)


def _model_folder_changes(rt, folder):
    import shlex
    import connect
    import install_ctl
    from flutter_existing import HOME
    if any(c in str(folder) for c in ('\\','"','\n','\r')):raise ValueError('Unsupported model-folder quoting')
    if not (folder/'blobs').is_dir() or not any((folder/name).is_dir() for name in ('manifests','manifests-v2')):raise ValueError('Choose an Ollama model store')
    inst=install_ctl.Ollama()
    env=inst.env_file
    if env.is_symlink():raise ValueError('Environment file is a symbolic link')
    old=connect._read(env)
    if not old:raise ValueError('No editable provider environment')
    changes=[connect.Change(env,old,llm_engine.env_with(old,'OLLAMA_MODELS',str(folder)))]
    link=HOME/'.local/bin/ollama'
    if not link.is_symlink() and link.is_file():
        text=connect._read(link)
        if not any(m in text for m in ('# Connected by AI Agents Map', '# Connected by Maprisma', '# Connected by Asterism')):raise ValueError('Use AI help to review this custom wrapper')
        before='export OLLAMA_MODELS='+shlex.quote(rt.models_dir)
        if before not in text:raise ValueError('Wrapper model path changed')
        changes.append(connect.Change(link,text,text.replace(before,'export OLLAMA_MODELS='+shlex.quote(str(folder)),1)))
    elif inst.installed_folder() is None:raise ValueError('Runtime is not managed here')
    return changes


PROVIDER = _dataclasses.replace(PROVIDER, model_folder_changes=_model_folder_changes)
