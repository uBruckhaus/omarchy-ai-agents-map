"""OpenClaw's hooks (adapters/openclaw.toml): what its facts cannot say.

A local LLM goes into ~/.openclaw/openclaw.json as a custom provider (docs.openclaw.ai, Configuration
— provider examples): models.providers.<key> = {baseUrl, apiKey, api: "openai-completions", models:
[{id, name, contextWindow}]} with models.mode "merge" (its other providers stay), and "use it now"
sets agents.defaults.model.primary = "<key>/<model>" (fallbacks kept). A file with JSON5 (comments,
unquoted keys) is left alone: `openclaw configure` is the way then."""


def mcp_write(text: str, name: str, item: dict | None) -> str:
    """mcp.servers in ~/.openclaw/openclaw.json. The file may be JSON5 (comments):
    then it is left alone and `openclaw mcp set` is the way."""
    import json

    import connect
    import mcp_ctl
    try:
        data = connect._json(text)
    except ValueError as error:
        raise ValueError("~/.openclaw/openclaw.json is JSON5: use `openclaw mcp set`") from error
    mcp = data.setdefault("mcp", {})
    if not isinstance(mcp, dict):
        raise ValueError("mcp is not an object")
    servers_text = mcp_ctl._json_text(json.dumps({"servers": mcp.get("servers", {})}), "servers", name, item)
    servers = json.loads(servers_text).get("servers")
    if servers:
        mcp["servers"] = servers
    else:
        mcp.pop("servers", None)
        if not mcp:
            data.pop("mcp")
    return connect._dump(data)


def _config(text: str) -> dict:
    import connect
    if not text.strip():
        return {}
    try:
        data = connect._json(text)
    except ValueError as error:
        raise ValueError("~/.openclaw/openclaw.json is JSON5: use `openclaw configure`") from error
    if not isinstance(data, dict):
        raise ValueError("~/.openclaw/openclaw.json is not an object")
    return data


def _primary(data: dict) -> str:
    chosen = ((data.get("agents") or {}).get("defaults") or {}).get("model") \
        if isinstance(data.get("agents"), dict) and isinstance(data["agents"].get("defaults"), dict) else None
    value = chosen.get("primary") if isinstance(chosen, dict) else chosen
    return value if isinstance(value, str) else ""


def local_llm_connected(texts, provider):
    models = _config(texts[0]).get("models")
    return isinstance(models, dict) and isinstance(models.get("providers"), dict) and provider in models["providers"]


def local_llm_active(texts, provider):
    return _primary(_config(texts[0])).startswith(provider + "/")


def local_llm_transform(texts, provider, label, url, models, context, model, use_now, remove):
    import connect
    data = _config(texts[0])
    section = data.setdefault("models", {})
    if not isinstance(section, dict):
        raise ValueError("models is not an object")
    providers = section.setdefault("providers", {})
    if not isinstance(providers, dict):
        raise ValueError("models.providers is not an object")
    agents = data.setdefault("agents", {})
    defaults = agents.setdefault("defaults", {}) if isinstance(agents, dict) else None
    if not isinstance(defaults, dict):
        raise ValueError("agents.defaults is not an object")
    if remove:
        providers.pop(provider, None)
        if _primary(data).startswith(provider + "/"):
            chosen = defaults.get("model")
            if isinstance(chosen, dict):
                chosen.pop("primary", None)
                if not chosen:
                    defaults.pop("model")
            else:
                defaults.pop("model", None)
    else:
        section.setdefault("mode", "merge")
        # What was set per model before is kept (its own compat, input, maxTokens …).
        before = {e.get("id"): e for e in (providers.get(provider) or {}).get("models") or ()
                  if isinstance(e, dict)}
        entries = []
        for name in models:
            item = {k: v for k, v in before.get(name, {}).items() if k not in ("id", "name")}
            item = {"id": name, "name": name, **item}
            if context.get(name):
                item["contextWindow"] = context[name]
            entries.append(item)
        providers[provider] = {"baseUrl": url, "apiKey": "local", "api": "openai-completions", "models": entries}
        if use_now and model:
            chosen = defaults.get("model")
            if isinstance(chosen, dict):
                chosen["primary"] = f"{provider}/{model}"
            else:
                defaults["model"] = {"primary": f"{provider}/{model}"}
    # Nothing left behind that this added: empty tables go (a lone "mode" too).
    if not providers:
        section.pop("providers")
    if set(section) <= {"mode"}:
        data.pop("models")
    if not defaults:
        agents.pop("defaults")
    if not agents:
        data.pop("agents")
    return [connect._dump(data)]
