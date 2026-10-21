"""Start and stop local LLM runtimes (right-click on the map), load and unload their models.

What each local LLM needs is its provider's engine (llm_engine): by default its user service
(`systemctl --user start|stop <unit>`, only this user's own means, so no password is ever
needed); LM Studio's engine uses its own CLI instead. This module keeps the calls the pages use
and what is the same for all: checking a model name, starting before a load, start at login.

Everything runs with a timeout; callers run it on a thread.
"""

import llm_engine
from llm_engine import MODEL_KEY  # noqa: F401  (pages check names with it)

TIMEOUT = 30


def lms_binary() -> str | None:
    """LM Studio's CLI (kept for the pages that still ask for it; its engine has its own)."""
    import provider_lmstudio
    return provider_lmstudio.lms_binary()


def _run(argv: list[str]) -> tuple[bool, str]:
    return llm_engine._run(argv)


def can_control(rt) -> bool:
    return llm_engine.of(rt).can_control(rt)


def start_at_login(rt) -> bool | None:
    """True/False for a runtime with a user service, None when there is none."""
    if not rt.service:
        return None
    ok, _out = _run(["systemctl", "--user", "is-enabled", "--quiet", f"{rt.service}.service"])
    return ok


def start(rt) -> tuple[bool, str]:
    return llm_engine.of(rt).start(rt)


def stop(rt) -> tuple[bool, str]:
    return llm_engine.of(rt).stop(rt)


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
    engine = llm_engine.of(rt)
    if not engine.can_load:
        return False, "it cannot be asked to load a model"
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
    return engine.load(rt, model)


def unload_model(rt, model: str) -> tuple[bool, str]:
    """Unload one model (a loaded model's line dropped into empty space on the map); the local LLM
    keeps running. Only on this user's own server."""
    if not MODEL_KEY.fullmatch(model or ""):
        return False, "not a model name"
    return llm_engine.of(rt).unload_model(rt, model)


def unload(rt) -> tuple[bool, str]:
    """Free the GPU memory without stopping the server where it can: unload every loaded model
    (the next request loads it again); an engine that cannot unload stops the server."""
    return llm_engine.of(rt).unload_all(rt)
