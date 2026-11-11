"""Measure and tune a llama.cpp model (Maprisma task 029, llama_tune.dart).

llama-bench, next to the llama-server that runs, tries three micro-batch sizes on this graphics card with
the model's KV cache type and flash attention: the fastest prompt processing wins (within 3 % the
smaller one, it needs less memory). A short second run with the winner measures the video memory it
takes; the context is what the card leaves after that and a margin, in steps of 4096, at most what the
model was trained for. Nothing is written here: tuned_ini() is the models.ini text the app shows and
writes with a backup and Undo (as every change).
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import threading
from pathlib import Path

import context_ctl
import gpu_usage

UBATCHES = (512, 1024, 2048)
BATCH, PROMPT, GEN, MEM_GEN = 2048, 2048, 64, 16
MARGIN = 512 * 1024 ** 2          # video memory kept free for the desktop and growth
STEP, MIN_CONTEXT = 4096, 4096
SPEED_TIMEOUT, MEMORY_TIMEOUT = 600, 300
MAX_OUTPUT = 1024 * 1024


def trusted(path: str | None) -> bool:
    """An executable owned by you or root that nobody else can change, nor any folder above it up to
    `/` (so no other local user can swap it or a folder on its way between this check and the
    start): one another user could have put there is never run."""
    if not path:
        return False
    uid = os.getuid()
    real = os.path.realpath(path)
    try:
        step = real
        while True:
            st = os.stat(step)
            # A folder others may write is safe only when sticky (/tmp): they cannot replace what is ours.
            open_to_others = st.st_mode & 0o022 and not (step != real and st.st_mode & stat.S_ISVTX)
            if st.st_uid not in (0, uid) or open_to_others:
                return False
            if step == "/":
                break
            step = os.path.dirname(step)
        return os.path.isfile(real) and os.access(real, os.X_OK)
    except OSError:
        return False


def server_binary() -> str | None:
    """The llama-server that runs now (one of your own processes), else the one on PATH."""
    uid = os.getuid()
    try:
        for entry in os.scandir("/proc"):
            if not entry.name.isdigit():
                continue
            try:
                if os.stat(f"/proc/{entry.name}").st_uid != uid:
                    continue                  # another user's process: never where a program is taken from
                if Path(f"/proc/{entry.name}/comm").read_text().strip() == "llama-server":
                    exe = os.readlink(f"/proc/{entry.name}/exe")
                    if "/.lmstudio/" not in exe and trusted(exe):
                        return exe
            except OSError:
                continue
    except OSError:
        pass
    found = shutil.which("llama-server")
    return found if trusted(os.path.realpath(found) if found else None) else None


def real_server(path: str) -> str | None:
    """A wrapper script's llama-server (the first existing one it names), else the binary itself."""
    import re
    try:
        with open(path, "rb") as handle:
            head = handle.read(64 * 1024)
    except OSError:
        return None
    if not head.startswith(b"#!"):
        return os.path.realpath(path) if trusted(os.path.realpath(path)) else None
    for found in re.findall(r"(/[^\s\"'$]+/llama-server)\b", head.decode("utf-8", "replace")):
        if trusted(os.path.realpath(found)):
            return os.path.realpath(found)
    return None


def bench_binary() -> str | None:
    server = server_binary()
    server = real_server(server) if server else None
    if server is None:
        return None
    bench = str(Path(server).with_name("llama-bench"))
    if trusted(bench):
        return bench
    found = shutil.which("llama-bench")
    return os.path.realpath(found) if found and trusted(os.path.realpath(found)) else None


def _rows(text: str) -> list:
    start = text.find("[")
    if start < 0:
        return []
    try:
        data = json.loads(text[start:])
    except ValueError:
        return []
    return [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []


def _run(argv: list, timeout: int, cancel=None) -> tuple[str, bool]:
    """argv (no shell, its own process group), output read while it runs with a 1 MiB cap and one
    deadline; Cancel or the cap ends it and its children."""
    import ai_core
    out, ok = ai_core.read_bounded_output(argv, max_bytes=MAX_OUTPUT, timeout=timeout, cancel=cancel)
    if cancel is not None and cancel.is_set():
        return "", False
    return out.decode("utf-8", "replace"), ok


def measure(name: str, model_file: str, ini_text: str = "", cancel=None, run=_run, totals=gpu_usage.gpu_totals) -> dict:
    """One model: {name, ctx, ubatch, batch, pp, tg, used, cache, flash} or {name, reason}."""
    bench = bench_binary()
    if bench is None:
        return {"name": name, "reason": "llama-bench is not next to the llama.cpp that runs"}
    have = context_ctl.llama_effective(ini_text, name) if ini_text else {}
    cache_k = have.get("cache-type-k") or "q8_0"
    cache_v = have.get("cache-type-v") or cache_k
    flash = have.get("flash-attn") or "on"
    meta = context_ctl.read_gguf(Path(model_file))
    arch = meta.get("general.architecture", "")
    trained = meta.get(f"{arch}.context_length")
    per_token, _layers = context_ctl.kv_layout(meta, cache_k)
    if per_token <= 0:
        return {"name": name, "reason": "its memory per token is not known"}
    used_now, total, _busy = totals()       # (used, total, busy %)
    if not total:
        return {"name": name, "reason": "no graphics card found"}

    def argv(ubatches, gen, repeats):
        return [bench, "-m", model_file, "-ngl", "99", "-fa", "0" if flash == "off" else "1", "-ctk", cache_k,
                "-ctv", cache_v, "-b", str(BATCH), "-ub", ",".join(map(str, ubatches)), "-p", str(PROMPT),
                "-n", str(gen), "-r", str(repeats), "-o", "json"]
    text, done = run(argv(UBATCHES, GEN, 2), SPEED_TIMEOUT, cancel)
    if not done:
        return {"name": name, "reason": "llama-bench did not finish"}
    rows = _rows(text)

    def rate(ub, prompt):
        return next((float(r.get("avg_ts", 0)) for r in rows if r.get("n_ubatch") == ub
                     and (r.get("n_prompt") if prompt else r.get("n_gen"))), 0.0)
    winner = 0
    for ub in UBATCHES:
        if rate(ub, True) > 0 and (winner == 0 or rate(ub, True) > rate(winner, True) * 1.03):
            winner = ub
    if not winner:
        return {"name": name, "reason": "llama-bench failed"}
    # Memory: one short run with the winner, the card's use sampled from just before it.
    before = totals()[0] or used_now
    peak = [before]
    stop = threading.Event()

    def sample():
        while not stop.is_set():
            now = totals()[0]
            if now and now > peak[0]:
                peak[0] = now
            stop.wait(0.2)
    sampler = threading.Thread(target=sample, daemon=True)
    sampler.start()
    try:
        _text, done = run(argv([winner], MEM_GEN, 1), MEMORY_TIMEOUT, cancel)
    finally:
        stop.set()
        sampler.join(timeout=1)
    if not done:
        return {"name": name, "reason": "llama-bench did not finish"}
    after = totals()[0]                       # once more right after it (a run shorter than a sample)
    if after and after > peak[0]:
        peak[0] = after
    used = peak[0] - before
    room = total - before - MARGIN - used
    ctx = (room // per_token + PROMPT + MEM_GEN) if room > 0 else 0
    ctx = ctx // STEP * STEP
    if isinstance(trained, int) and trained > 0 and ctx > trained:
        ctx = trained
    if used <= 0 or ctx < MIN_CONTEXT:
        return {"name": name, "reason": "it does not fit on the card"}
    return {"name": name, "ctx": int(ctx), "ubatch": winner, "batch": BATCH, "pp": round(rate(winner, True)),
            "tg": round(rate(winner, False)), "used": used, "cache": cache_k, "flash": flash}


def tuned_ini(old: str, measured: dict) -> str:
    """models.ini with a measured model's section: ctx-size, the batch sizes; the rest stays."""
    name = measured["name"]
    text = context_ctl.llama_sized_text(old, name, measured["ctx"])
    text = context_ctl.ini_text(text, name, "ubatch-size", str(measured["ubatch"]))
    return context_ctl.ini_text(text, name, "batch-size", str(measured["batch"]))
