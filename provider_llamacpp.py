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
        now, cache = context_ctl.llama_settings(name)
        return now, cache, context_ctl.llama_model_file(folder, name)

    def context_changes(self, rt, name, ctx):
        # Like a connection: written with a backup, and Undo puts it back.
        import connect
        import context_ctl
        before = connect._read(context_ctl.MODELS_INI)
        return [connect.Change(context_ctl.MODELS_INI, before, context_ctl.llama_context_text(before, name, ctx))]

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
            return unit is not None and "installed by AI Agents Map" in ai_core.read_head(unit, 4000).decode("utf-8", "replace")
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
            if candidate.stem.lower() == name.lower():
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
