"""Meta's Muse Code."""

from harnesses import Harness, Mcp, Skills, Way


# --- MCP servers (mcp_ctl) ------------------------------------------------------------------------

def _mcp_text(text: str, name: str, item: dict | None) -> str:
    import connect
    import mcp_ctl
    new_text = mcp_ctl._json_text(text, "mcp_servers", name, item)
    data = connect._json(new_text)
    if "schema_version" not in data:      # Muse refuses a settings file without it
        new_text = connect._dump({"schema_version": 1, **data})
    return new_text


HARNESS = Harness("muse", "Muse Code", label="Muse Code", icon="\U000f06e4",
                  spec="http:muse[url=https://api.meta.ai/muse-launcher.sh,bin=muse]",
                  skills=Skills(("~/.config/muse/skills", "~/.agents/skills", "~/.claude/skills",
                                 "~/.codex/skills"),
                                "~/.config/muse/skills"),
                  mcp=Mcp("~/.config/muse/settings.json", key="mcp_servers", shape="muse", write=_mcp_text),
                  install=(Way("mise", spec="http:muse[url=https://api.meta.ai/muse-launcher.sh,bin=muse]"),),
                  data=("~/.config/muse",))
