"""The best context size per model, and setting it.

The context a model can hold is limited by video memory: the model's file is
loaded whole, and every token of context needs KV-cache memory

    bytes per token = Σ over layers (KV heads × (key size + value size)) × bytes per cache element

read from the GGUF header (block_count, head_count_kv – a number or one per
layer –, key_length/value_length or embedding_length / head_count). Layers
with a sliding window (sliding_window_pattern, e.g. Gemma) keep only the last
`sliding_window` tokens: a fixed amount, whatever the context. Layers without
attention (recurrent, hybrid models: 0 KV heads) take nothing per token.
What is left after the model, those fixed amounts and a reserve (compute
buffers, the desktop) divided by the bytes per token gives the context,
rounded down to a multiple of 4096 and capped at what the model was trained for.

Setting it:
- llama.cpp: `ctx-size` in the model's section of ~/.config/llama.cpp/models.ini
- Ollama: `num_ctx`, through its API (/api/create from the model itself)
- LM Studio: `llm.load.contextLength` in the model's own defaults,
  ~/.lmstudio/.internal/user-concrete-model-default-config/<model id>.json (what
  LM Studio writes when the context is changed in its model settings; used when
  it loads the model, also just in time for a request)
"""

import json
import math
import os
import re
import struct
import tempfile
from dataclasses import dataclass
from pathlib import Path

GiB = 2 ** 30
RESERVE = int(1.5 * GiB)       # compute buffers and what the desktop keeps
STEP = 4096
MIN_CONTEXT = 4096
# Bytes per cache element (q4_0/q8_0 blocks of 32 carry a 2-byte scale).
CACHE_BYTES = {"f16": 2.0, "bf16": 2.0, "f32": 4.0, "q8_0": 34 / 32, "q4_0": 18 / 32, "q4_1": 20 / 32,
               "q5_0": 22 / 32, "q5_1": 24 / 32, "iq4_nl": 18 / 32}
MODELS_INI = Path.home() / ".config/llama.cpp/models.ini"
MAX_KV = 100_000
MAX_ARRAY = 4096            # entries of a kept per-layer array
MAX_LAYERS = MAX_ARRAY     # block_count: real models have at most a few hundred layers
MAX_DEPTH = 4              # arrays of arrays in skipped values
MAX_KEPT = 32              # header values kept: a real model has about a dozen that matter here
MAX_ARCH = 256             # bytes of general.architecture


@dataclass
class Fit:
    native: int              # what the model was trained for (0: unknown)
    best: int                # the context that fits (0: the model alone does not fit)
    per_token: int           # KV bytes per token
    model_size: int
    known: bool = True       # False: the header says nothing about attention (TTS, tokenizer models)
    fixed: int = 0           # bytes the sliding-window layers keep whatever the context


# --- GGUF header -----------------------------------------------------------------------------

def read_gguf(path: Path) -> dict:
    """The header values needed here; arrays only for the per-layer head counts."""
    meta = {}
    with open(path, "rb") as f:
        if f.read(4) != b"GGUF":
            raise ValueError("not a GGUF file")
        version = struct.unpack("<I", f.read(4))[0]
        _tensors, count = struct.unpack("<QQ", f.read(16)) if version >= 2 else struct.unpack("<II", f.read(8))
        if count > MAX_KV:
            raise ValueError("implausible header")

        def string(keep=True, limit=1 << 20):
            (length,) = struct.unpack("<Q", f.read(8))
            if length > limit:
                raise ValueError("implausible string")
            if not keep:
                f.seek(length, os.SEEK_CUR)
                return None
            return f.read(length).decode("utf-8", "replace")

        sizes = {0: ("<B", 1), 1: ("<b", 1), 2: ("<H", 2), 3: ("<h", 2), 4: ("<I", 4), 5: ("<i", 4),
                 6: ("<f", 4), 7: ("<?", 1), 10: ("<Q", 8), 11: ("<q", 8), 12: ("<d", 8)}

        def value(kind, keep=True, depth=0):
            if kind in sizes:
                fmt, size = sizes[kind]
                return struct.unpack(fmt, f.read(size))[0]
            if kind == 8:
                return string(False)      # the only string kept is the architecture, read below
            if kind == 9:
                sub, length = struct.unpack("<I", f.read(4))[0], struct.unpack("<Q", f.read(8))[0]
                if keep:
                    # The values kept are per-layer numbers: one flat array, one entry per layer.
                    if sub not in sizes or length > MAX_ARRAY:
                        raise ValueError("implausible array")
                    return [value(sub) for _ in range(length)]
                if sub in sizes:
                    f.seek(sizes[sub][1] * length, os.SEEK_CUR)     # skip big arrays (vocabularies)
                    return None
                if depth >= MAX_DEPTH:
                    raise ValueError("implausible array")
                for _ in range(length):                           # skipped one by one, nothing kept
                    value(sub, False, depth + 1)
                return None
            raise ValueError(f"unknown value type {kind}")

        wanted = (".context_length", ".block_count", ".head_count_kv", ".head_count", ".embedding_length",
                  ".key_length", ".value_length", ".key_length_swa", ".value_length_swa", ".sliding_window",
                  ".sliding_window_pattern")
        for _ in range(count):
            key = string()
            kind = struct.unpack("<I", f.read(4))[0]
            if key == "general.architecture" and kind == 8:
                meta[key] = string(limit=MAX_ARCH)
                continue
            # Only numbers and per-layer number arrays are kept, and only a few of them: a header
            # full of matching keys with big string values must not fill the memory.
            keep = key.endswith(wanted) and (kind in sizes or kind == 9)
            result = value(kind, keep)
            if keep:
                if len(meta) >= MAX_KEPT:
                    raise ValueError("implausible header")
                meta[key] = result
    return meta


UBATCH = 1024               # a sliding-window cache also holds one batch beyond the window


def kv_layout(meta: dict, cache: str = "f16") -> tuple[int, int]:
    """(bytes per token of context, fixed bytes of the sliding-window layers)."""
    arch = meta.get("general.architecture", "")
    get = lambda key: meta.get(f"{arch}.{key}")   # noqa: E731
    layers = get("block_count") or 0
    if type(layers) is not int or not 0 <= layers <= MAX_LAYERS:   # untrusted: per-layer lists are this long
        raise ValueError("implausible header")
    heads = get("attention.head_count")
    first = lambda v: (max(v) if v else 0) if isinstance(v, list) else v   # noqa: E731
    embed = int(get("embedding_length") or 0)
    head_dim = embed // int(first(heads)) if embed and first(heads) else 128
    k_dim = int(get("attention.key_length") or head_dim)
    v_dim = int(get("attention.value_length") or head_dim)
    k_swa = int(get("attention.key_length_swa") or k_dim)
    v_swa = int(get("attention.value_length_swa") or v_dim)
    kv = get("attention.head_count_kv")
    kv = heads if kv is None else kv
    per_layer_kv = [int(x or 0) for x in kv[:layers]] if isinstance(kv, list) else [int(kv or 0)] * layers
    window = int(get("attention.sliding_window") or 0)
    pattern = get("attention.sliding_window_pattern")
    if isinstance(pattern, list):
        swa = [bool(x) for x in pattern[:layers]] + [False] * max(0, layers - len(pattern))
    elif isinstance(pattern, int) and pattern > 1 and window:
        swa = [(i % pattern) < pattern - 1 for i in range(layers)]   # every pattern-th layer sees everything
    else:
        swa = [False] * layers
    element = CACHE_BYTES.get(cache, 2.0)
    per_token = fixed = 0.0
    for heads_kv, sliding in zip(per_layer_kv, swa):
        if sliding and window:
            fixed += heads_kv * (k_swa + v_swa) * element * (window + UBATCH)
        else:
            per_token += heads_kv * (k_dim + v_dim) * element
    return math.ceil(per_token), math.ceil(fixed)


def kv_bytes_per_token(meta: dict, cache: str = "f16") -> int:
    return kv_layout(meta, cache)[0]


def fit(model_file: Path, vram: int, cache: str = "f16", model_size: int | None = None) -> Fit:
    meta = read_gguf(model_file)
    arch = meta.get("general.architecture", "")
    native = int(meta.get(f"{arch}.context_length") or 0)
    size = model_size if model_size is not None else _size_of(model_file)
    per_token, fixed = kv_layout(meta, cache)
    if not per_token and not fixed:
        return Fit(native, native, 0, size, known=False)
    room = vram - RESERVE - size - fixed
    if room <= 0:
        return Fit(native, 0, per_token, size, fixed=fixed)
    best = room // per_token if per_token else (native or 0)
    best = (best // STEP) * STEP
    if native:
        best = min(best, native)
    return Fit(native, max(0, best if best >= MIN_CONTEXT else 0), per_token, size, fixed=fixed)


def _size_of(model_file: Path) -> int:
    """All parts of a split model (…-00001-of-00003.gguf)."""
    split = re.match(r"^(.*)-\d{5}-of-(\d{5})\.gguf$", model_file.name)
    if not split:
        return model_file.stat().st_size
    return sum(p.stat().st_size for p in model_file.parent.glob(f"{split.group(1)}-*-of-{split.group(2)}.gguf"))


def short_context(n: int) -> str:
    return f"{n // 1024}k" if n and n % 1024 == 0 else str(n)


# --- llama.cpp (models.ini) ---------------------------------------------------------------------

def _ini_sections(text: str) -> dict:
    sections, current = {}, None
    for line in text.splitlines():
        header = re.match(r"^\s*\[([^\]]+)\]\s*$", line)
        if header:
            current = header.group(1)
            sections.setdefault(current, {})
            continue
        pair = re.match(r"^\s*([A-Za-z0-9_-]+)\s*=\s*(.*?)\s*$", line)
        if current is not None and pair:
            sections[current][pair.group(1)] = pair.group(2)
    return sections


def llama_settings(name: str, ini: Path = MODELS_INI) -> tuple[int, str]:
    """(ctx-size, KV cache type) for a router model: its section, else [*]."""
    try:
        sections = _ini_sections(ini.read_text(encoding="utf-8"))
    except OSError:
        sections = {}
    own, shared = sections.get(name, {}), sections.get("*", {})
    ctx = own.get("ctx-size") or shared.get("ctx-size") or "0"
    cache = own.get("cache-type-k") or shared.get("cache-type-k") or "f16"
    return (int(ctx) if ctx.isdigit() else 0), cache


def llama_context_text(old: str, name: str, ctx: int) -> str:
    """models.ini with ctx-size set in the model's section (made if missing); the rest stays."""
    if not re.fullmatch(r"[^\[\]\x00-\x1f\x7f]{1,200}", name) or not 0 < ctx < 10_000_000:
        raise ValueError("bad model or context")
    old = old or "version = 1\n"
    lines = old.splitlines(keepends=True)
    start = next((i for i, l in enumerate(lines) if re.match(rf"^\s*\[{re.escape(name)}\]\s*$", l)), None)
    if start is None:
        return old.rstrip("\n") + f"\n\n[{name}]\nctx-size = {ctx}\n"
    end = next((i for i in range(start + 1, len(lines)) if re.match(r"^\s*\[", lines[i])), len(lines))
    for i in range(start + 1, end):
        if re.match(r"^\s*ctx-size\s*=", lines[i]):
            lines[i] = f"ctx-size = {ctx}\n"
            break
    else:
        lines.insert(start + 1, f"ctx-size = {ctx}\n")
    return "".join(lines)


def llama_model_file(models_dir: Path, name: str) -> Path | None:
    for candidate in (models_dir / f"{name}.gguf", *models_dir.glob(f"{name}*.gguf")):
        if candidate.exists():
            return Path(os.path.realpath(candidate))
    return None


# --- Ollama --------------------------------------------------------------------------------------

def ollama_manifest(models_root: Path, name: str) -> dict:
    """The manifest of "name:tag" (library) or "host/owner/name:tag"."""
    base, _sep, tag = name.partition(":")
    tag = tag or "latest"
    parts = base.split("/")
    if len(parts) == 1:
        parts = ["registry.ollama.ai", "library", parts[0]]
    elif len(parts) == 2:
        parts = ["registry.ollama.ai", *parts]
    if any(p in ("..", "") for p in parts) or ".." in tag or "/" in tag:
        raise ValueError("bad model name")
    path = models_root / "manifests" / Path(*parts) / tag
    if not path.is_file() and parts[0] == "registry.ollama.ai":
        # Newer Ollama keeps new pulls in manifests-v2/ollama.com/…
        v2 = models_root / "manifests-v2" / "ollama.com" / Path(*parts[1:]) / tag
        if v2.is_file():
            path = v2
    return json.loads(path.read_text(encoding="utf-8")[:1_000_000])


def ollama_model_file(models_root: Path, name: str) -> Path | None:
    manifest = ollama_manifest(models_root, name)
    for layer in manifest.get("layers", []):
        if layer.get("mediaType") == "application/vnd.ollama.image.model":
            digest = str(layer.get("digest", "")).replace(":", "-")
            if re.fullmatch(r"sha256-[0-9a-f]{64}", digest):
                return models_root / "blobs" / digest
    return None


def ollama_context(models_root: Path, name: str, default: int = 0) -> int:
    """num_ctx set on the model, else default (OLLAMA_CONTEXT_LENGTH)."""
    manifest = ollama_manifest(models_root, name)
    for layer in manifest.get("layers", []):
        if layer.get("mediaType") == "application/vnd.ollama.image.params":
            digest = str(layer.get("digest", "")).replace(":", "-")
            try:
                params = json.loads((models_root / "blobs" / digest).read_text(encoding="utf-8")[:100_000])
            except (OSError, ValueError):
                continue
            if isinstance(params, dict) and isinstance(params.get("num_ctx"), int):
                return params["num_ctx"]
    return default


def ollama_set_context(name: str, ctx: int, address: str):
    """Give the model num_ctx (Ollama keeps everything else, the weights are not copied)."""
    import models_ctl
    port = int(address.rsplit(":", 1)[-1])
    models_ctl.require_own_listener(port)
    body = json.dumps({"model": name, "from": name, "parameters": {"num_ctx": int(ctx)}, "stream": False}).encode()
    import netfetch
    try:
        raw = netfetch.fetch(f"http://127.0.0.1:{port}/api/create", max_bytes=65536, timeout=120, local=True,
                             headers={"Content-Type": "application/json"}, data=body, method="POST")
        answer = json.loads(raw or b"{}")
    except (netfetch.FetchError, ValueError) as error:
        raise RuntimeError(f"Ollama: {error}") from error
    if isinstance(answer, dict) and answer.get("error"):
        raise RuntimeError(str(answer["error"])[:300])


# --- LM Studio ---------------------------------------------------------------------------------

LMSTUDIO = Path.home() / ".lmstudio"
LMS_DEFAULTS = LMSTUDIO / ".internal/user-concrete-model-default-config"
LMS_CONTEXT_KEY = "llm.load.contextLength"
LMS_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+@-]*(/[A-Za-z0-9][A-Za-z0-9._+@-]*){0,4}$")
MAX_LMS_INDEX = 8 * 1024 * 1024


def lmstudio_entry(name: str, root: Path = LMSTUDIO) -> dict:
    """The model index entry LM Studio calls `name` (its defaultIdentifier), or {}."""
    path = root / ".internal/model-index-cache.json"
    try:
        if path.stat().st_size > MAX_LMS_INDEX:
            return {}
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return {}
    for entry in (data.get("models") if isinstance(data, dict) and isinstance(data.get("models"), list) else [])[:500]:
        if isinstance(entry, dict) and entry.get("defaultIdentifier") == name:
            return entry
    return {}


def lmstudio_defaults_file(entry: dict, defaults: Path = LMS_DEFAULTS) -> Path | None:
    """<defaults>/<publisher>/<repo>/<file>.json for an index entry; None for an id
    that is not a plain relative path (it becomes a file name)."""
    ident = entry.get("indexedModelIdentifier")
    if not isinstance(ident, str) or ".." in ident or not LMS_ID_RE.fullmatch(ident):
        return None
    return defaults / f"{ident}.json"


def lmstudio_model_file(entry: dict) -> Path | None:
    """The model's GGUF file, if it lies inside the models folder LM Studio names for it."""
    point = entry.get("entryPoint") if isinstance(entry.get("entryPoint"), dict) else {}
    path, root = point.get("absPath"), point.get("rootAbsolutePath")
    if not isinstance(path, str) or not isinstance(root, str) or not path.endswith(".gguf"):
        return None
    try:
        file, base = Path(path).resolve(), Path(root).resolve()
    except OSError:
        return None
    return file if file.is_relative_to(base) and file.is_file() else None


def _lms_fields(data: dict) -> list:
    load = data.get("load") if isinstance(data.get("load"), dict) else {}
    return load.get("fields") if isinstance(load.get("fields"), list) else []


def lmstudio_settings(entry: dict, defaults: Path = LMS_DEFAULTS) -> tuple[int, str]:
    """(context set for the model — 0: LM Studio's own default —, KV cache type)."""
    path = lmstudio_defaults_file(entry, defaults)
    try:
        data = json.loads(path.read_text()) if path else {}
    except (OSError, ValueError):
        data = {}
    ctx, cache = 0, "f16"
    for field in _lms_fields(data if isinstance(data, dict) else {}):
        if not isinstance(field, dict):
            continue
        if field.get("key") == LMS_CONTEXT_KEY and isinstance(field.get("value"), int):
            ctx = field["value"]
        if field.get("key") == "llm.load.llama.kCacheQuantizationType":
            value = field.get("value")
            value = value.get("value") if isinstance(value, dict) else value
            if isinstance(value, str) and value in CACHE_BYTES:
                cache = value
    return ctx, cache


def lmstudio_context_text(old: str, ctx: int) -> str:
    """The defaults file with its context set to ctx; every other field is kept."""
    try:
        data = json.loads(old) if old.strip() else {}
    except ValueError:
        raise ValueError("LM Studio's settings file for this model is not valid JSON")
    if not isinstance(data, dict):
        data = {}
    data.setdefault("preset", "")
    if not isinstance(data.get("operation"), dict):
        data["operation"] = {"fields": []}
    if not isinstance(data.get("load"), dict):
        data["load"] = {}
    fields = [f for f in _lms_fields(data) if not (isinstance(f, dict) and f.get("key") == LMS_CONTEXT_KEY)]
    data["load"]["fields"] = fields + [{"key": LMS_CONTEXT_KEY, "value": int(ctx)}]
    return json.dumps(data, indent=2) + "\n"


def lmstudio_auto_context(name: str, vram: int, root: Path = LMSTUDIO,
                          defaults: Path = LMS_DEFAULTS) -> tuple[int, Path | None, str, str]:
    """For connecting an agent to an LM Studio model: (its context, defaults file,
    old text, new text). LM Studio loads a model with 8192 tokens unless its
    defaults say otherwise, too few for an agent's prompt. When nothing is set
    yet, the best context for this card goes into the defaults (new != old);
    a context the user set is kept. (0, None, "", "") when it cannot be worked out."""
    entry = lmstudio_entry(name, root)
    path = lmstudio_defaults_file(entry, defaults) if entry else None
    if path is None:
        return 0, None, "", ""
    now, cache = lmstudio_settings(entry, defaults)
    try:
        old = path.read_text() if path.exists() else ""
    except OSError:
        return 0, None, "", ""
    if now:
        return now, path, old, old
    model_file = lmstudio_model_file(entry)
    if model_file is None or vram <= 0:
        return 0, None, "", ""
    try:
        best = fit(model_file, vram, cache).best
    except (OSError, ValueError, KeyError, struct.error):
        return 0, None, "", ""
    if best <= 0:
        return 0, None, "", ""
    return best, path, old, lmstudio_context_text(old, best)
