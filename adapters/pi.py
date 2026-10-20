"""Pi's hooks (adapters/pi.toml): connecting a local LLM and writing its skill folder list."""


def llm_texts(models_text: str, settings_text: str, provider: str, url: str, models: list, context: dict,
              model: str, use_now: bool, remove: bool = False) -> tuple[str, str]:
    import connect
    data = connect._json(models_text)
    providers = data.setdefault("providers", {})
    if remove:
        providers.pop(provider, None)
    else:
        # What was set per model before (pictures, the thinking switch: AI Playground) is kept.
        before = {e["id"]: e for e in (providers.get(provider) or {}).get("models") or ()
                  if isinstance(e, dict) and isinstance(e.get("id"), str)}
        entries = []
        for name in models:
            item = {k: v for k, v in before.get(name, {}).items() if k in ("input", "reasoning", "compat")}
            item = {"id": name, "name": name, **item}
            if context.get(name):
                item["contextWindow"] = context[name]
            entries.append(item)
        providers[provider] = {"baseUrl": url, "api": "openai-completions", "apiKey": "local", "models": entries}
    settings = connect._json(settings_text)
    if remove:
        if settings.get("defaultProvider") != provider:
            return connect._dump(data), settings_text
        # The default would point at a provider that is gone.
        settings.pop("defaultProvider")
        settings.pop("defaultModel", None)
        return connect._dump(data), connect._dump(settings)
    if use_now:
        settings["defaultProvider"] = provider
        settings["defaultModel"] = model
    return connect._dump(data), (connect._dump(settings) if use_now else settings_text)


def local_llm_connected(texts, provider):
    import connect
    return provider in connect._json(texts[0]).get("providers", {})


def local_llm_active(texts, provider):
    import connect
    return connect._json(texts[1]).get("defaultProvider") == provider


def local_llm_transform(texts, provider, label, url, models, context, model, use_now, remove):
    return list(llm_texts(texts[0], texts[1], provider, url, models, context, model, use_now, remove))


def skills_write(text: str, folder: str, remove: bool) -> str:
    import connect
    import skills_ctl
    data = connect._json(text)
    dirs = data.get("skills") if isinstance(data.get("skills"), list) else []
    dirs = [d for d in dirs if not (isinstance(d, str) and str(skills_ctl.expand(d)) == folder)]
    if not remove:
        dirs.append(folder)
    if dirs:
        data["skills"] = dirs
    else:
        data.pop("skills", None)
    return connect._dump(data)
