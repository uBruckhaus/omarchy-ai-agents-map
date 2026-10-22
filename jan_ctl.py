"""Jan's model settings, for its engine run headless (install_ctl.Jan).

Jan keeps one model.yml per model (<data>/llamacpp/models/<id>/model.yml:
model_path relative to the data folder, ctx_size, …) and generates
<data>/llamacpp/router.preset.ini from them, which its engine
jan-llama-worker reads ([<id>] sections with model = <file>, ctx-size = N).
A context set here goes into both, so the engine uses it at once and the
Jan app keeps it when it writes the preset again.
"""

import os
import re
from pathlib import Path

import context_ctl
import inventory

ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,119}(/[A-Za-z0-9][A-Za-z0-9._+-]{0,119})?$")
MAX_FILE = 256 * 1024
EMPTY_PRESET = "[*]\nload-on-startup = false\n"


def model_yml(name: str, data: Path | None = None) -> Path | None:
    if not ID_RE.fullmatch(name or ""):
        return None
    data = data or inventory.jan_data_folder()
    path = data / "llamacpp/models" / name / "model.yml"
    return path if path.is_file() else None


def _read(path: Path) -> str:
    try:
        if path.stat().st_size > MAX_FILE:
            return ""
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def settings(name: str, data: Path | None = None) -> tuple[int, str, Path | None]:
    """(context, KV cache type, model file) of a Jan model: its preset
    section, else its model.yml."""
    data = data or inventory.jan_data_folder()
    preset = data / "llamacpp/router.preset.ini"
    sections = context_ctl._ini_sections(_read(preset))
    own, shared = sections.get(name, {}), sections.get("*", {})
    ctx = own.get("ctx-size") or shared.get("ctx-size") or ""
    cache = own.get("cache-type-k") or shared.get("cache-type-k") or "f16"
    model = own.get("model", "")
    yml = model_yml(name, data)
    text = _read(yml) if yml else ""
    if not ctx:
        found = re.search(r"^ctx_size:\s*(\d{1,8})\s*$", text, re.MULTILINE)
        ctx = found.group(1) if found else "0"
    if not model:
        found = re.search(r"^model_path:\s*['\"]?([^'\"\n]+?)['\"]?\s*$", text, re.MULTILINE)
        if found:
            model = found.group(1)
            model = model if os.path.isabs(model) else str(data / model)
    path = Path(os.path.realpath(model)) if model and Path(model).exists() else None
    return (int(ctx) if ctx.isdigit() else 0), cache, path


def yml_context_text(old: str, ctx: int) -> str:
    """model.yml with ctx_size set (added at the end if missing); the rest stays."""
    if not 0 < ctx < 10_000_000:
        raise ValueError("bad context")
    if re.search(r"^ctx_size:", old, re.MULTILINE):
        return re.sub(r"^ctx_size:.*$", f"ctx_size: {ctx}", old, count=1, flags=re.MULTILINE)
    return old.rstrip("\n") + f"\nctx_size: {ctx}\n"


def context_changes(name: str, ctx: int, data: Path | None = None) -> list:
    """connect.Change for the preset and the model.yml (preview, backup, Undo)."""
    import connect
    data = data or inventory.jan_data_folder()
    changes = []
    preset = data / "llamacpp/router.preset.ini"
    if preset.is_file():
        old = connect._read(preset)
        changes.append(connect.Change(preset, old, context_ctl.llama_context_text(old, name, ctx)))
    yml = model_yml(name, data)
    if yml is not None:
        old = connect._read(yml)
        changes.append(connect.Change(yml, old, yml_context_text(old, ctx)))
    if not changes:
        raise ValueError("Jan does not list this model")
    return changes


def ensure_preset(data: Path | None = None) -> Path:
    """The engine needs a preset to start: an empty one when Jan has not written
    its own yet (Jan replaces it once models are added in the app)."""
    data = data or inventory.jan_data_folder()
    preset = data / "llamacpp/router.preset.ini"
    if not preset.exists():
        preset.parent.mkdir(parents=True, exist_ok=True)
        preset.write_text(EMPTY_PRESET, encoding="utf-8")
    return preset
