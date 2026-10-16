"""KoboldCpp's models as the router sees them: one .kcpps per GGUF.

KoboldCpp runs in router mode (--admin --admindir --routermode, see
install_ctl.KoboldCpp): its model ids are the .kcpps files in
inventory.KOBOLD_CONFIGS ("gemma-4-E2B-it-Q4_K_M.kcpps"), and a request naming
one loads it (the one loaded before is unloaded). A .kcpps holds the model
file, its context size and how to use the graphics card.

sync() makes one for every GGUF in the model folder (with the context that
fits the card) and removes the ones it made whose model file is gone. Files
the user made there are left alone. These are KoboldCpp settings AI Agents
Map keeps for itself (written without a preview, like its own state); a
context the user changes goes through a preview with backup and Undo
(context_text).
"""

import json
import os
import re
from pathlib import Path

import inventory

MARK = "ai_agents_map"            # the key telling a .kcpps we made (KoboldCpp ignores unknown keys)
DEFAULT_CONTEXT = 8192
MAX_FILE = 64 * 1024
SPLIT_RE = re.compile(r"-(\d{5})-of-(\d{5})\.gguf$", re.IGNORECASE)


def model_files(models_dir: Path) -> list[Path]:
    """GGUF models in the folder and one level below (no vision projectors, a
    split model by its first part)."""
    found = []
    try:
        entries = sorted(models_dir.iterdir())[:inventory.MAX_ITEMS * 2]
    except OSError:
        return found
    for entry in entries:
        candidates = [entry]
        if entry.is_dir() and not entry.name.startswith("."):
            try:
                candidates = sorted(entry.iterdir())[:50]
            except OSError:
                continue
        for path in candidates:
            name = path.name.lower()
            if not name.endswith(".gguf") or name.startswith("mmproj") or "tokenizer" in name:
                continue
            split = SPLIT_RE.search(path.name)
            if split and split.group(1) != "00001":
                continue
            found.append(path)
    return found[:inventory.MAX_ITEMS]


def config_name(model: Path) -> str:
    """"gemma-4-E2B-it-Q4_K_M.gguf" -> "gemma-4-E2B-it-Q4_K_M.kcpps" (split: without -00001-of-…)."""
    stem = SPLIT_RE.sub("", model.name)
    stem = re.sub(r"\.gguf$", "", stem, flags=re.IGNORECASE)
    return re.sub(r"[^A-Za-z0-9._+-]", "_", stem)[:120] + ".kcpps"


def read(path: Path) -> dict:
    try:
        if path.stat().st_size > MAX_FILE:
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def entry_for(model: Path, build: str, ctx: int) -> dict:
    entry = {"model_param": str(model), "contextsize": int(ctx), "gpulayers": -1, "flashattention": True,
             MARK: True}
    if build == "cuda":
        entry["usecuda"] = []
    elif build == "vulkan":
        entry["usevulkan"] = []
    else:
        entry["usecpu"] = True
    return entry


def best_context(model: Path, vram: int) -> int:
    import context_ctl
    try:
        best = context_ctl.fit(model, vram).best if vram > 0 else 0
    except (OSError, ValueError, KeyError, Exception):     # an unreadable or unusual GGUF
        best = 0
    return best or DEFAULT_CONTEXT


def sync(models_dir: Path, build: str, vram, configs: Path = None) -> list[str]:
    """One .kcpps per model file; ours for vanished files removed. Returns the names written.
    vram: bytes, or a function giving them (asked only when a new model needs a context)."""
    configs = configs or inventory.KOBOLD_CONFIGS
    configs.mkdir(parents=True, exist_ok=True)
    wanted = {config_name(m): m for m in model_files(models_dir)}
    written = []
    for name, model in wanted.items():
        path = configs / name
        if path.exists() or path.is_symlink():
            continue
        if callable(vram):
            vram = vram()
        _write(path, json.dumps(entry_for(model, build, best_context(model, vram)), indent=2) + "\n")
        written.append(name)
    for path in configs.glob("*.kcpps"):
        data = read(path)
        if data.get(MARK) and path.name not in wanted and not Path(str(data.get("model_param", ""))).exists():
            path.unlink(missing_ok=True)
    return written


def _write(path: Path, text: str):
    tmp = path.with_name(f".{path.name}.new")
    tmp.write_text(text, encoding="utf-8")
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def settings(name: str, configs: Path = None) -> tuple[int, Path | None]:
    """(context, model file) of a model id ("x.kcpps")."""
    configs = configs or inventory.KOBOLD_CONFIGS
    if "/" in name or not name.endswith(".kcpps"):
        return 0, None
    data = read(configs / name)
    ctx = data.get("contextsize") if isinstance(data.get("contextsize"), int) else 0
    model = Path(str(data.get("model_param", "")))
    return ctx, (Path(os.path.realpath(model)) if model.is_absolute() and model.exists() else None)


def config_path(name: str, configs: Path = None) -> Path | None:
    configs = configs or inventory.KOBOLD_CONFIGS
    if "/" in name or not name.endswith(".kcpps") or not (configs / name).is_file():
        return None
    return configs / name


def context_text(old: str, ctx: int) -> str:
    """The .kcpps with its context set; everything else stays."""
    if not 0 < ctx <= 262144:
        raise ValueError("KoboldCpp takes 256 to 262144 tokens")
    data = json.loads(old or "{}")
    if not isinstance(data, dict):
        raise ValueError("not a .kcpps file")
    data["contextsize"] = int(ctx)
    return json.dumps(data, indent=2) + "\n"
