"""Start and stop local LLM runtimes (right-click on the map).

Only this user's own means are used, so no password is ever needed:

- a runtime with a user service (llama-server.service, ollama.service, …):
  `systemctl --user start|stop|enable|disable <unit>`
- LM Studio without one: its own CLI, `lms server start|stop`

Everything runs with a timeout; callers run it on a thread.
"""

import re
import shutil
import subprocess
from pathlib import Path

TIMEOUT = 30


def lms_binary() -> str | None:
    found = shutil.which("lms")
    if found:
        return found
    candidate = Path.home() / ".lmstudio/bin/lms"
    return str(candidate) if candidate.is_file() else None


def _run(argv: list[str]) -> tuple[bool, str]:
    import ai_core
    out, ok = ai_core.read_bounded_output(argv, max_bytes=64 * 1024, timeout=TIMEOUT, merge_stderr=True)
    message = out.decode("utf-8", "replace").strip().splitlines()
    return ok, (message[-1] if message else "")[:300]


def can_control(rt) -> bool:
    return bool(rt.installed and (rt.service or (rt.key == "lmstudio" and lms_binary())))


def start_at_login(rt) -> bool | None:
    """True/False for a runtime with a user service, None when there is none."""
    if not rt.service:
        return None
    ok, _out = _run(["systemctl", "--user", "is-enabled", "--quiet", f"{rt.service}.service"])
    return ok


def start(rt) -> tuple[bool, str]:
    if rt.service:
        return _run(["systemctl", "--user", "start", f"{rt.service}.service"])
    if rt.key == "lmstudio" and lms_binary():
        return _run([lms_binary(), "server", "start"])
    return False, "no way to start it"


def stop(rt) -> tuple[bool, str]:
    if rt.service:
        return _run(["systemctl", "--user", "stop", f"{rt.service}.service"])
    if rt.key == "lmstudio" and lms_binary():
        return _run([lms_binary(), "server", "stop"])
    return False, "no way to stop it"


def set_start_at_login(rt, on: bool) -> tuple[bool, str]:
    if not rt.service:
        return False, "no user service"
    return _run(["systemctl", "--user", "enable" if on else "disable", f"{rt.service}.service"])


def load(rt, model: str, wait: float = 30) -> tuple[bool, str]:
    """Load one model so that it is ready (a line drawn on the map from the local LLM to its model,
    with no agent or with some): the local LLM is started first when it is not running. Only on
    this user's own server (gpu_usage.http_json checks the listener)."""
    if not MODEL_KEY.fullmatch(model or ""):
        return False, "not a model name"
    import time
    import gpu_usage
    import loopback
    port = gpu_usage.port_of(rt)
    if not port:
        return False, "no address"
    if not loopback.loopback_listeners(port):
        ok, why = start(rt)
        if not ok:
            return False, why
        until = time.monotonic() + wait
        while not loopback.loopback_listeners(port):
            if time.monotonic() > until:
                return False, "it did not start"
            time.sleep(0.5)
    if rt.key in ("llamacpp", "jan"):
        reply = gpu_usage.http_json(port, "/models/load", "POST", {"model": model}, timeout=300)
        return (True, "") if reply is not None else (False, "it did not load the model")
    if rt.key == "ollama":
        reply = gpu_usage.http_json(port, "/api/generate", "POST", {"model": model, "keep_alive": "30m"}, timeout=300)
        return (True, "") if reply is not None else (False, "it did not load the model")
    if rt.key == "lmstudio":
        binary = lms_binary()
        return _run([binary, "load", "--", model]) if binary else (False, "lms not found")
    return False, "it cannot be asked to load a model"


# A model name given to `lms` (load/unload) or a local LLM's API: never an option (a folder name in
# a shared models folder could start with "-"), one plain word.
MODEL_KEY = re.compile(r"[A-Za-z0-9][\w.:/@+-]{0,199}")


def unload_model(rt, model: str) -> tuple[bool, str]:
    """Unload one model (a loaded model's line dropped into empty space on the map); the local LLM
    keeps running. Only on this user's own server."""
    if not MODEL_KEY.fullmatch(model or ""):
        return False, "not a model name"
    import gpu_usage
    port = gpu_usage.port_of(rt)
    if rt.key in ("llamacpp", "jan"):
        reply = gpu_usage.http_json(port, "/models/unload", "POST", {"model": model}, timeout=30)
        return (True, "") if reply is not None else (False, "it did not unload the model")
    if rt.key == "ollama":
        reply = gpu_usage.http_json(port, "/api/generate", "POST", {"model": model, "keep_alive": 0}, timeout=30)
        return (True, "") if reply is not None else (False, "it did not unload the model")
    if rt.key == "lmstudio":
        binary = lms_binary()
        return _run([binary, "unload", "--", model]) if binary else (False, "lms not found")
    return unload(rt)                      # KoboldCpp, vLLM: one model at a time anyway


def unload(rt) -> tuple[bool, str]:
    """Free the GPU memory without stopping the server: unload every loaded model
    (the next request loads it again). vLLM cannot unload, so it is stopped."""
    import gpu_usage
    port = gpu_usage.port_of(rt)
    if rt.key == "vllm":
        return stop(rt) if can_control(rt) else (False, "no way to stop it")
    if rt.key == "lmstudio":
        binary = lms_binary()
        return _run([binary, "unload", "--all"]) if binary else (False, "lms not found")
    names = [name for name, state in gpu_usage.loaded_models(rt).items() if state in ("loaded", "loading", "sleeping")]
    if rt.key == "koboldcpp":
        if not names:
            return True, ""
        reply = gpu_usage.http_json(port, "/api/admin/reload_config", "POST", {"filename": "unload_model"}, timeout=30)
        return (True, "") if isinstance(reply, dict) and reply.get("success") else (False, "KoboldCpp did not unload")
    if rt.key in ("llamacpp", "jan"):
        rows = (gpu_usage.http_json(port, "/models") or {}).get("data", [])
        ids = [str(r.get("id")) for r in rows if isinstance(r, dict) and str(r.get("id", "")).lower() in names]
        failed = [i for i in ids if gpu_usage.http_json(port, "/models/unload", "POST", {"model": i}, timeout=30) is None]
    elif rt.key == "ollama":
        rows = (gpu_usage.http_json(port, "/api/ps") or {}).get("models", [])
        ids = [str(r.get("name")) for r in rows if isinstance(r, dict) and r.get("name")]
        failed = [i for i in ids if gpu_usage.http_json(port, "/api/generate", "POST",
                                                        {"model": i, "keep_alive": 0}, timeout=30) is None]
    else:
        return False, "unknown runtime"
    if failed:
        return False, f"could not unload {', '.join(failed)[:200]}"
    return True, ""
