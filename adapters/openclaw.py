"""OpenClaw's hooks (adapters/openclaw.toml): what its facts cannot say."""


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
