"""The readers the app has for agents' own settings: each fills an agent_details.Details with the
agent's model and providers. An adapter names one (details.reader, adapter_data.DETAIL_READERS);
an agent whose settings none of them knows brings a details hook instead.

Only the keys named here are read; keys, tokens and other values stay where they are.
"""

from __future__ import annotations

import re

import agent_details as ad


def pi_like(config_dir, details: "ad.Details"):
    """pi and Oh My Pi: settings.json (default provider/model) and models.json (custom providers)."""
    config_dir = ad._expand(str(config_dir))
    settings = ad.read_json(config_dir / "settings.json")
    model, provider = ad._text(settings.get("defaultModel")), ad._text(settings.get("defaultProvider"), 40)
    details.model = f"{model} ({provider})" if model and provider else model
    providers = ad.read_json(config_dir / "models.json").get("providers")
    if isinstance(providers, dict):
        for name, entry in list(providers.items())[:50]:
            if isinstance(entry, dict):
                details.providers.append(ad.provider_for_url(name, entry.get("baseUrl"),
                                                             ad._model_count(entry.get("models")),
                                                             active=not provider or name == provider))


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
    if chosen and details.model:
        details.model = f"{details.model} ({chosen})"


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
    providers = models.get("providers") if isinstance(models.get("providers"), dict) else {}
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
