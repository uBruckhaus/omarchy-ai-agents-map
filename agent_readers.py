"""The readers the app has for agents' own settings: each fills an agent_details.Details with the
agent's model and providers. An adapter names one (details.reader, adapter_data.DETAIL_READERS);
an agent whose settings none of them knows brings a details hook instead.

Only the keys named here are read; keys, tokens and other values stay where they are.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import agent_details as ad


def pi_like(config_dir, details: "ad.Details"):
    """pi and Oh My Pi: settings.json (default provider/model) and models.json (custom providers);
    Oh My Pi's own YAML (config.yml modelRoles.default, models.yml providers) where it has it."""
    config_dir = ad._expand(str(config_dir))
    settings = ad.read_json(config_dir / "settings.json")
    model, provider = ad._text(settings.get("defaultModel")), ad._text(settings.get("defaultProvider"), 40)
    role = _omp_default(ad._read(config_dir / "config.yml") or "")
    if role:
        provider, _slash, model = role.partition("/")
        model, provider = ad._text(model), ad._text(provider, 40)
    details.model = f"{model} ({provider})" if model and provider else model
    providers = ad.read_json(config_dir / "models.json").get("providers")
    if isinstance(providers, dict):
        for name, entry in list(providers.items())[:50]:
            if isinstance(entry, dict):
                details.providers.append(ad.provider_for_url(name, entry.get("baseUrl"),
                                                             ad._model_count(entry.get("models")),
                                                             active=not provider or name == provider))
    for name, url, count in _omp_providers(ad._read(config_dir / "models.yml") or ""):
        if not any(p.name == name for p in details.providers):
            details.providers.append(ad.provider_for_url(name, url, count, active=not provider or name == provider))


def _omp_default(text: str) -> str:
    """config.yml's `modelRoles: default: "<provider>/<model>"`, read with a pattern."""
    block = re.search(r"^modelRoles:[ \t]*\n((?:[ \t]+.*\n?)+)", text, re.MULTILINE)
    found = block and re.search(r"^[ \t]+default:[ \t]*['\"]?([^'\"\n#]*)", block.group(1), re.MULTILINE)
    value = found.group(1).strip() if found else ""
    return value if "/" in value else ""


def _omp_providers(text: str) -> list:
    """models.yml's `providers:` → [(name, baseUrl, number of models)], read with a pattern."""
    block = re.search(r"^providers:[ \t]*\n((?:[ \t]+.*\n?|\n)+)", text, re.MULTILINE)
    entries = re.split(r"^  (?=[A-Za-z0-9_.-]+:)", block.group(1), flags=re.MULTILINE) if block else []
    found = []
    for entry in entries[:50]:
        name = re.match(r"([A-Za-z0-9_.-]{1,64}):", entry)
        url = re.search(r"^    baseUrl:[ \t]*['\"]?([^'\"\s#]+)", entry, re.MULTILINE)
        if name and url:
            found.append((name.group(1), url.group(1), len(re.findall(r"^      - id:", entry, re.MULTILINE))))
    return found


def opencode(details: "ad.Details"):
    root = ad._expand("~/.config/opencode")
    config = ad.read_json(root / "opencode.json") or ad.read_json(root / "opencode.jsonc")
    details.model = ad._text(config.get("model"))
    providers = config.get("provider")
    if isinstance(providers, dict):
        for name, entry in list(providers.items())[:50]:
            if isinstance(entry, dict):
                options = entry.get("options") if isinstance(entry.get("options"), dict) else {}
                details.providers.append(ad.provider_for_url(
                       name, options.get("baseURL"), ad._model_count(entry.get("models")),
                       active=not details.model or details.model.startswith(name + "/")))


def claude(details: "ad.Details"):
    settings = ad.read_json(ad._expand("~/.claude/settings.json"))
    details.model = ad._text(settings.get("model"))
    env = settings.get("env") if isinstance(settings.get("env"), dict) else {}
    base = env.get("ANTHROPIC_BASE_URL")
    if isinstance(base, str) and base:
        details.providers.append(ad.provider_for_url("Anthropic API", base))


def codex(details: "ad.Details"):
    config = ad.read_toml(ad._expand("~/.codex/config.toml"))
    details.model = ad._text(config.get("model"))
    chosen = ad._text(config.get("model_provider"), 40)
    providers = config.get("model_providers")
    if isinstance(providers, dict):
        for name, entry in list(providers.items())[:50]:
            if isinstance(entry, dict):
                details.providers.append(ad.provider_for_url(ad._text(entry.get("name"), 40) or name,
                                                                entry.get("base_url"), active=name == chosen))
    if not details.model and not chosen:
        details.model = codex_session_model()          # none set: Codex's own default, as its last session ran it
    if chosen and details.model:
        details.model = f"{details.model} ({chosen})"


CODEX_MODEL = re.compile(rb'"type"\s*:\s*"turn_context".*?"model"\s*:\s*"([A-Za-z0-9._:-]{1,80})"')


def codex_session_model(root: str = "~/.codex/sessions") -> str:
    """The model of the last turn in Codex's newest session file (sessions/YYYY/MM/DD/rollout-….jsonl),
    "" when there is none. Only that key of its turn_context lines is read."""
    folder = ad._expand(root)
    for _level in range(3):
        last = ad._last_dirs(folder)
        if not last:
            return ""
        folder = Path(last[0])
    try:
        files = sorted((e.stat().st_mtime, e.path) for _i, e in zip(range(2000), os.scandir(folder))
                       if e.name.endswith(".jsonl") and e.is_file(follow_symlinks=False))
        if not files:
            return ""
        with open(files[-1][1], "rb") as handle:
            size = os.fstat(handle.fileno()).st_size
            handle.seek(max(0, size - 256 * 1024))
            found = CODEX_MODEL.findall(handle.read(256 * 1024))
            if not found:
                handle.seek(0)
                found = CODEX_MODEL.findall(handle.read(64 * 1024))
    except OSError:
        return ""
    return found[-1].decode("ascii") if found else ""


def copilot(details: "ad.Details"):
    config = ad.read_json(ad._expand("~/.copilot/config.json"))
    details.model = ad._text(config.get("model"))


def cursor(details: "ad.Details"):
    config = ad.read_json(ad._expand("~/.cursor/cli-config.json"))
    model = config.get("model")
    details.model = ad._text(model.get("displayName") or model.get("modelId")) if isinstance(model, dict) \
        else ad._text(model)


def hermes(details: "ad.Details"):
    """config.yaml's top-level `model:` block (default + provider + base_url),
    read with a pattern: the file is YAML and only these three keys matter."""
    text = ad._read(ad._expand("~/.hermes/config.yaml")) or ""
    block = re.search(r"^model:\s*\n((?:[ \t]+.*\n?)+)", text, re.MULTILINE)
    if not block:
        return
    values = dict(re.findall(r"^[ \t]+(default|provider|base_url):[ \t]*['\"]?([^'\"\n#]*)", block.group(1),
                             re.MULTILINE))
    model, provider = ad._text(values.get("default")), ad._text(values.get("provider"), 40)
    details.model = f"{model} ({provider})" if model and provider else model
    if values.get("base_url", "").strip():
        details.providers.append(ad.provider_for_url(provider or "custom", values["base_url"].strip()))
    # Its own providers (`providers:` → name: {api: URL, models: {id: …}}): the
    # active one is the model block's provider.
    block = re.search(r"^providers:[ \t]*\n((?:[ \t]+.*\n?|\n)+)", text, re.MULTILINE)
    entries = re.split(r"^  (?=[A-Za-z0-9_.-]+:)", block.group(1), flags=re.MULTILINE) if block else []
    for entry in entries[:50]:
        name = re.match(r"([A-Za-z0-9_.-]{1,64}):", entry)
        api = re.search(r"^    (?:api|base_url):[ \t]*['\"]?([^'\"\s#]+)", entry, re.MULTILINE)
        if not name or not api:
            continue
        models = re.search(r"^    models:[ \t]*\n((?:      .*\n?)+)", entry, re.MULTILINE)
        count = len(re.findall(r"^      [^ \s][^:]*:", models.group(1), re.MULTILINE)) if models else 0
        if not any(p.name == name.group(1) for p in details.providers):
            details.providers.append(ad.provider_for_url(name.group(1), api.group(1), count,
                                                         active=name.group(1) == provider))


GOOSE_LOCAL = {"ollama": "http://localhost:11434"}     # its default address when no <NAME>_HOST is set


def goose(details: "ad.Details"):
    """Goose's ~/.config/goose/config.yaml: active_provider and providers.<name>.model (older files:
    flat GOOSE_PROVIDER, GOOSE_MODEL); a provider's address from its <NAME>_HOST key, for local ones.
    API keys are not in this file (the keyring, or secrets.yaml): nothing else is read."""
    text = ad._read(ad._expand("~/.config/goose/config.yaml")) or ""
    try:
        import yaml
        data = yaml.safe_load(text) if text.strip() else {}
    except Exception:
        return
    if not isinstance(data, dict):
        return
    providers = data.get("providers") if isinstance(data.get("providers"), dict) else {}
    active = ad._text(data.get("active_provider") or data.get("GOOSE_PROVIDER"), 40)
    entry = providers.get(active) if isinstance(providers.get(active), dict) else {}
    model = ad._text(entry.get("model") or data.get("GOOSE_MODEL"))
    details.model = f"{model} ({active})" if model and active else model
    names = [n for n, v in list(providers.items())[:50] if isinstance(n, str) and isinstance(v, dict)
             and (v.get("enabled") or v.get("configured"))] or ([active] if active else [])
    for name in names:
        host = data.get(f"{name.upper()}_HOST")
        url = host if isinstance(host, str) else GOOSE_LOCAL.get(name)
        details.providers.append(ad.provider_for_url(name, url, active=name == active))


def dsh(details: "ad.Details"):
    """DeepSeek Harness: providers and default model from its web profile's patch."""
    import dsh_ctl
    text = ad._read(dsh_ctl.PATCH) or ""
    if not dsh_ctl.available() or not text:
        return
    provider, model = dsh_ctl.default_model(text)
    details.model = f"{ad._text(model)} ({ad._text(provider, 40)})" if model and provider else ad._text(model)
    for name, entry in list(dsh_ctl.providers(text).items())[:50]:
        if isinstance(entry, dict):
            details.providers.append(ad.provider_for_url(str(name), entry.get("baseURL"),
                                                         ad._model_count(entry.get("models")),
                                                         active=not provider or name == provider))


# OpenClaw's own local providers, used without an entry in models.providers (their usual address).
OPENCLAW_LOCAL = {"llama-cpp": "http://127.0.0.1:8080/v1", "ollama": "http://127.0.0.1:11434",
                  "lmstudio": "http://127.0.0.1:1234/v1"}


def openclaw(details: "ad.Details"):
    """OpenClaw: ~/.openclaw/openclaw.json, agents.defaults.model (a "provider/model" or
    {primary: …}) and models.providers (custom and local servers). JSON5 with comments is not read."""
    config = ad.read_json(ad._expand("~/.openclaw/openclaw.json"))
    agents = config.get("agents") if isinstance(config.get("agents"), dict) else {}
    defaults = agents.get("defaults") if isinstance(agents.get("defaults"), dict) else {}
    chosen = defaults.get("model")
    primary = ad._text(chosen.get("primary") if isinstance(chosen, dict) else chosen, 160)
    provider, _sep, model = primary.partition("/") if "/" in primary else ("", "", primary)
    provider = ad._text(provider, 40)
    details.model = f"{ad._text(model)} ({provider})" if model and provider else ad._text(model)
    models = config.get("models") if isinstance(config.get("models"), dict) else {}
    providers = p if isinstance(p := models.get("providers"), dict) else {}
    for name, entry in list(providers.items())[:50]:
        if isinstance(entry, dict):
            details.providers.append(ad.provider_for_url(name, entry.get("baseUrl"),
                                                         ad._model_count(entry.get("models")),
                                                         active=not provider or name == provider))
    if provider in OPENCLAW_LOCAL and provider not in providers:
        details.providers.append(ad.provider_for_url(provider, OPENCLAW_LOCAL[provider]))


def bionic(details: "ad.Details"):
    """LM Studio Bionic runs local models with LM Studio's engine and models
    (no providers to set up): always LM Studio."""
    details.providers.append(ad.Provider(name="LM Studio", local=True, runtime="LM Studio", active=True))
