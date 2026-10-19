"""OpenClaw (the openclaw pacman package)."""

from harnesses import Harness, Mcp, Skills, Way


# --- skills (skills_ctl) --------------------------------------------------------------------------

def _skills_listed():
    import skills_ctl
    import skills_ctl
    load = skills_ctl._json(skills_ctl.HOME / ".openclaw/openclaw.json").get("skills", {})
    return load.get("load", {}).get("extraDirs") if isinstance(load, dict) else None


# --- MCP servers (mcp_ctl) ------------------------------------------------------------------------

def _mcp_text(text: str, name: str, item: dict | None) -> str:
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


HARNESS = Harness("openclaw", "OpenClaw", label="OpenClaw", icon="\ue90c", icon_font="omarchy",
                  package="openclaw", package_only=True,
                  skills=Skills(("~/.openclaw/skills", "~/.agents/skills"), "~/.openclaw/skills",
                                listed=_skills_listed),
                  mcp=Mcp("~/.openclaw/openclaw.json", key="mcp.servers", shape="openclaw", write=_mcp_text),
                  install=(Way("command", argv=("omarchy-install-openclaw-cli", "--now"), sudo=True),
                           Way("package", packages=(("pacman", "openclaw"),))),
                  data=("~/.openclaw",),
                  homepage="https://github.com/openclaw/openclaw")
