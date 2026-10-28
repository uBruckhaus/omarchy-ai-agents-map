"""DeepSeek Harness's hooks (adapters/dsh.toml): its profile is YAML, edited by dsh_ctl."""


def local_llm_connected(texts, provider):
    import dsh_ctl
    return provider in dsh_ctl.providers(texts[0])


def local_llm_active(texts, provider):
    import dsh_ctl
    return dsh_ctl.default_model(texts[0])[0] == provider


def local_llm_transform(texts, provider, label, url, models, context, model, use_now, remove):
    import dsh_ctl
    return [dsh_ctl.connect_text(texts[0], provider, label, url, models, context, model, use_now, remove)]


def mcp_read(text):
    import dsh_ctl
    return dsh_ctl.mcp_servers(text)


def mcp_write(text, name, item):
    import dsh_ctl
    return dsh_ctl.mcp_text(text, name, item)


def mcp_native(entry):
    import dsh_ctl
    return dsh_ctl.mcp_config(entry)


def models_choices(current="", home=None):
    """Its online models as "provider/model": the routes of its profile (their model lists) and its
    own DeepSeek route (dsh_ctl.builtin_choices); the map leaves out the local LLMs' (their boxes)."""
    import dsh_ctl
    from pathlib import Path
    try:
        text = _profile((home or Path.home()) / ".dsh/profiles/web/cordis.patch.yml")
    except (OSError, ValueError):
        return []                          # unreadable or too large: no choices, the map goes on
    found = []
    for provider, entry in dsh_ctl.providers(text).items():
        for row in (entry.get("models") if isinstance(entry, dict) else None) or []:
            if isinstance(row, dict) and isinstance(row.get("id"), str):
                found.append(f"{provider}/{row['id']}")
    import install_ctl
    folder = install_ctl.installer("dsh").installed_folder()
    if folder is not None:
        found += dsh_ctl.builtin_choices(folder / "current/node_modules/@deepseek-ai")
    return found


def models_plan(model, home):
    """(file, old text, new text, provider): its default model in the profile patch."""
    import dsh_ctl
    import online_models
    provider, name = online_models.split(model)
    path = home / ".dsh/profiles/web/cordis.patch.yml"
    old = _profile(path)
    return path, old, dsh_ctl.model_text(old, provider, name), provider


def _profile(path) -> str:
    """The profile patch: our own regular file, not through a link, at most connect.MAX_BYTES
    (larger raises); "" when there is none."""
    import ai_core
    import connect
    try:
        return ai_core.read_whole(path, connect.MAX_BYTES, follow=False).decode("utf-8", "replace")
    except FileNotFoundError:
        return ""
