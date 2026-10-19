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
