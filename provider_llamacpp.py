"""llama.cpp: llama-server with its models folder (router mode) or one model."""

import os

import ai_core
import llm_engine
from harnesses import Way
from providers import Provider


def _detect(processes):
    import inventory
    return inventory.llamacpp(processes)


def _installer():
    import install_ctl
    return install_ctl.LlamaCpp()


class Engine(llm_engine.LlamaRouter):
    ports = (8080,)
    programs = ("llama-server",)
    start_command = "llama-server --models-dir <folder>"

    def owns_process(self, comm, argv):
        # Other apps bundle llama-server too; theirs claim it by their path (BY_PATH).
        return llm_engine.BY_NAME if comm.startswith("llama-") or llm_engine.basename(argv[0] if argv else "") \
            in self.programs else 0

    gguf_folder = True
    reorganize = True
    download = "file"
    removable = "file"
    context_settable = True
    context_effect = "restart"

    def context_now(self, rt, folder, name):
        import context_ctl
        # The context set now; what fits is sized for the KV cache "Set to" writes:
        # the one set, else q8_0 (task 029, rule 2).
        now, _cache = context_ctl.llama_settings(name)
        return now, context_ctl.llama_auto_cache(name), context_ctl.llama_model_file(folder, name)

    def auto_context_now(self, rt, folder, name):
        # Only the model's own section counts: "[*] ctx-size" is a fallback, not its context (task 029).
        # Sized for the KV cache set, else q8_0, which is then set along with the context.
        import context_ctl
        return (context_ctl.llama_own_context(name), context_ctl.llama_auto_cache(name),
                context_ctl.llama_model_file(folder, name))

    def auto_context_changes(self, rt, name, ctx):
        import connect
        import context_ctl
        before = connect._read(context_ctl.MODELS_INI)
        return [connect.Change(context_ctl.MODELS_INI, before, context_ctl.llama_sized_text(before, name, ctx))]

    def context_changes(self, rt, name, ctx):
        # Like a connection: written with a backup, and Undo puts it back.
        import connect
        import context_ctl
        before = connect._read(context_ctl.MODELS_INI)
        # As a context the app works out: q8_0 and flash attention where nothing is set (task 029).
        return [connect.Change(context_ctl.MODELS_INI, before, context_ctl.llama_sized_text(before, name, ctx))]

    # The router's per-model settings in models.ini ("*": every model); a restart makes them count.
    tuning = {
        "flash-attn": (r"on|off|auto", "flash attention: less memory for the context, often faster"),
        "cache-type-k": (r"f16|q8_0|q4_0", "the context's key cache: q8_0 halves its memory, q4_0 quarters it"),
        "cache-type-v": (r"f16|q8_0|q4_0", "the context's value cache (needs flash attention when quantized)"),
        "gpu-layers": (r"all|\d{1,3}", "how many layers run on the graphics card (all: the fastest)"),
        "parallel": (r"[1-8]", "requests served at once (each needs its own share of the context)"),
        "batch-size": (r"64|128|256|512|1024|2048|4096|8192", "prompt tokens read per step"),
        "ubatch-size": (r"64|128|256|512|1024|2048|4096", "the physical batch (at most batch-size)"),
        "threads": (r"[1-9]|[1-9]\d|1\d\d|2[0-5]\d", "CPU threads for what is not on the card"),
    }
    builds = (("rocm", "ROCm"), ("vulkan", "Vulkan"), ("cuda", "CUDA"), ("cpu", "CPU"))

    def tuning_now(self, rt):
        import context_ctl
        return {section: {k: v for k, v in values.items() if k in self.tuning}
                for section, values in context_ctl.ini_sections().items()}

    def tuning_changes(self, rt, section, key, value):
        import re
        import connect
        import context_ctl
        if key not in self.tuning or not re.fullmatch(self.tuning[key][0], value):
            raise ValueError(f"{key} = {value} is not a setting the app changes")
        before = connect._read(context_ctl.MODELS_INI)
        return [connect.Change(context_ctl.MODELS_INI, before, context_ctl.ini_text(before, section, key, value))]

    def build_now(self, rt):
        """Its own install's build ("b11658-rocm"), else what its devices say (any build, also one
        made by hand): "ROCm0: AMD Radeon …" → rocm."""
        import install_ctl
        version = install_ctl.installer("llamacpp").current_version()
        if "-" in version:
            return version.rsplit("-", 1)[-1]
        return self.devices(rt)[0]

    @staticmethod
    def devices(rt) -> tuple[str, str]:
        """(build, its device lines) from `llama-server --list-devices` (bounded, a few seconds)."""
        import re
        import shutil
        binary = shutil.which("llama-server")
        if binary is None:
            return "", ""
        out, _ok = ai_core.read_bounded_output([binary, "--list-devices"], max_bytes=8192, timeout=20,
                                               merge_stderr=True)
        lines = [l.strip() for l in out.decode("utf-8", "replace").splitlines()
                 if re.match(r"\s+(ROCm|Vulkan|CUDA|Metal|SYCL|CPU)\d*:", l)]
        first = lines[0].split(":", 1)[0].rstrip("0123456789").lower() if lines else ""
        return {"rocm": "rocm", "vulkan": "vulkan", "cuda": "cuda"}.get(first, "cpu" if out else ""), \
            "\n".join(lines[:4])

    def model_context(self, rt, name):
        import context_ctl
        return context_ctl.llama_settings(name)[0]

    def agent_context(self, rt):
        import inventory
        return inventory.llama_context()

    # How many models the router keeps loaded: --models-max, else LLAMA_ARG_MODELS_MAX (the env
    # file the app writes), else llama.cpp's default. A number on the start command wins over the
    # env file, so the app's own service passes none; one fixed in the user's own start script
    # (their wrapper in ~/.local/bin) is theirs and never changed here.
    slots_kind = "count"
    DEFAULT_SLOTS = 4

    def _router_argv(self):
        """The running router's command line (not a model it started, not LM Studio's engine)."""
        import inventory
        servers = {pid: argv for pid, comm, argv in inventory.own_processes()
                   if comm == "llama-server" and argv and "/.lmstudio/" not in argv[0]}
        return next((argv for pid, argv in servers.items() if inventory.parent_pid(pid) not in servers), None)

    def _unit(self, rt):
        from pathlib import Path
        if not rt.service:
            return None
        path = Path.home() / ".config/systemd/user" / f"{rt.service}.service"
        return path if path.is_file() else None

    def _start_lines(self, rt):
        """(where, text) of what starts it: its service file, and the program it starts when that
        is a script (the user's wrapper)."""
        import re
        import shlex
        from pathlib import Path
        unit = self._unit(rt)
        if unit is None:
            return []
        text = ai_core.read_head(unit, 200_000).decode("utf-8", "replace")
        found = [(unit, text)]
        for line in re.findall(r"^ExecStart=[-@:+!]*(.+)$", text, re.MULTILINE)[:3]:
            try:
                program = Path(os.path.expanduser(shlex.split(line)[0]))
            except (ValueError, IndexError):
                continue
            try:
                head = ai_core.read_head(program, 200_000)
            except OSError:
                continue
            if not head.startswith(b"\x7fELF"):
                found.append((program, head.decode("utf-8", "replace")))
        return found

    def _ours(self, rt) -> bool:
        unit = self._unit(rt)
        try:
            return unit is not None and any(mark in ai_core.read_head(unit, 4000).decode("utf-8", "replace") for mark in ("installed by AI Agents Map", "installed by Maprisma"))
        except OSError:
            return False

    def slots_fixed_by(self, rt):
        import re
        for where, text in self._start_lines(rt):
            if re.search(r"--models-max(?:[ =])", text) and not (where == self._unit(rt) and self._ours(rt)):
                return str(where).replace(os.path.expanduser("~"), "~", 1)
        return ""

    def slots(self, rt):
        import re
        import install_ctl
        number = llm_engine.arg_number(self._router_argv(), "--models-max") if rt.running else None
        if number is None:
            for _where, text in self._start_lines(rt):
                found = re.search(r"--models-max(?:\s+|=)(\d+)", text)
                if found:
                    number = int(found.group(1))
                    break
        if number is None:
            value = install_ctl.read_env(install_ctl.LlamaCpp.env_file).get("LLAMA_ARG_MODELS_MAX", "")
            number = int(value) if value.isdigit() else self.DEFAULT_SLOTS
        return number

    def slot_changes(self, rt, count):
        import re
        import install_ctl
        if self.slots_fixed_by(rt) or count < 1:
            return []
        env_file = install_ctl.LlamaCpp.env_file
        try:
            env_text = env_file.read_text(encoding="utf-8")
        except FileNotFoundError:
            env_text = ""
        except OSError:
            return []
        changes = [llm_engine.change(env_file, llm_engine.env_with(env_text, "LLAMA_ARG_MODELS_MAX", str(count)))]
        unit = self._unit(rt)
        if unit is not None and self._ours(rt):
            text = unit.read_text(encoding="utf-8")
            # Made by an older version with the number on the command line: it would win over the env file.
            changes.append(llm_engine.change(unit, re.sub(r" --models-max(?:\s+|=)\d+", "", text)))
        return [c for c in changes if c is not None]

    def after_slot_change(self, rt):
        llm_engine._run(["systemctl", "--user", "daemon-reload"])

    def model_size(self, rt, name):
        from pathlib import Path
        if not rt.models_dir:
            return 0
        folder = Path(os.path.expanduser(rt.models_dir))
        for candidate in (folder / f"{name}.gguf", *folder.glob("*.gguf")):
            # The named file may be missing: then one whose name differs only in case.
            if candidate.stem.lower() == name.lower() and candidate.is_file():
                try:
                    return candidate.stat().st_size
                except OSError:
                    return 0
        return 0


PROVIDER = Provider("llamacpp", "llama.cpp", "local", detect=_detect, installer=_installer, engine=Engine(),
                    connect_key="llama-local", connect_label="llama.cpp (local)",
                    install=(Way("installer"),),
                    data=("~/.cache/llama.cpp",),
                    homepage="https://github.com/ggml-org/llama.cpp")


def _discover_existing(folder, names, mount, device):
    from existing_ai import safe, program
    if any(part.lower() in ('ollama', '.lmstudio') for part in folder.parts):
        return []  # These engines belong to their enclosing app, not a standalone runtime.
    candidate = None
    for relative in ('current/llama-server', 'llama-server'):
        if relative.split('/')[0] in names:
            candidate = program(folder / relative, mount, device)
            if candidate:
                break
    if candidate is None:
        return []
    environment = {}
    dependencies = []
    # Portable ROCm trees often sit beside the program, outside the distro root.
    for parent in list(folder.parents)[:4]:
        if not parent.is_relative_to(mount):
            break
        runtime = parent / 'rocm-runtime/root'
        rocm = safe(runtime / 'opt/rocm', mount, device)
        libs = safe(runtime / 'usr/lib', mount, device)
        if rocm and libs:
            directories = [libs, rocm / 'lib', rocm / 'lib/llvm/lib']
            valid = [safe(p, mount, device) for p in directories]
            environment = {'ROCM_PATH': str(rocm), 'HIP_PATH': str(rocm),
                           'LD_LIBRARY_PATH': ':'.join(str(p) for p in valid if p),
                           'ROCBLAS_TENSILE_LIBPATH': str(rocm / 'lib/rocblas/library')}
            dependencies = [str(rocm), str(libs)]
            break
    return [{'kind': 'runtime', 'provider': PROVIDER.key, 'name': PROVIDER.name,
             **candidate, 'models': '', 'count': 0,
             'notes': ['GPU selection uses runtime defaults. Review device selection before loading a model.',
                       'A new model preset starts with GPU layers set to all. Existing tuning is not copied.'],
             'environment': environment, 'dependencies': dependencies}]


def _reconnect_existing(record, models):
    from pathlib import Path
    from flutter_existing import wrapper, service_files
    if models is None or models['kind'] != 'models':
        raise ValueError('Choose a discovered GGUF folder')
    args = ['--models-dir', models['path'], '--host', '127.0.0.1', '--port', '8080', '--models-max', '1',
            '--models-preset']
    text = wrapper(record, args, home_arguments=('.config/llama.cpp/models.ini',))
    env = 'LLAMA_ARG_MODELS_DIR="' + models['path'] + '"\n'
    # No old presets are copied: they may contain paths into the other distro's home.
    return service_files(record, 'llama-server', text, [record['mount']['path'], models['mount']['path']],
                         [(Path('.config/llama.cpp/server.env'), env),
                          (Path('.config/llama.cpp/models.ini'), '[*]\ngpu-layers = all\n')])


# Facts stay with this provider, including when its add-on has not been enabled yet.
import dataclasses as _dataclasses
PROVIDER = _dataclasses.replace(PROVIDER, discover_existing=_discover_existing, reconnect_existing=_reconnect_existing)


def _model_folder_changes(rt, folder):
    from pathlib import Path
    import shlex
    import connect
    import install_ctl
    from flutter_existing import HOME
    if any(c in str(folder) for c in ('\\','"','\n','\r')):
        raise ValueError('Unsupported model-folder quoting')
    inst=install_ctl.LlamaCpp()
    env=inst.env_file
    if env.is_symlink():raise ValueError('Environment file is a symbolic link')
    old=connect._read(env)
    if not old:raise ValueError('No editable provider environment')
    changes=[connect.Change(env,old,llm_engine.env_with(old,'LLAMA_ARG_MODELS_DIR',str(folder)))]
    link=HOME/'.local/bin/llama-server'
    if not link.is_symlink() and link.is_file():
        text=connect._read(link)
        if not any(m in text for m in ('# Connected by AI Agents Map', '# Connected by Maprisma', '# Connected by Asterism')):raise ValueError('Use AI help to review this custom wrapper')
        before='--models-dir '+shlex.quote(str(Path(rt.models_dir).expanduser()))
        if before not in text:raise ValueError('Wrapper model path changed')
        changes.append(connect.Change(link,text,text.replace(before,'--models-dir '+shlex.quote(str(folder)),1)))
    elif inst.installed_folder() is None:raise ValueError('Runtime is not managed here')
    return changes


PROVIDER = _dataclasses.replace(PROVIDER, model_folder_changes=_model_folder_changes)
