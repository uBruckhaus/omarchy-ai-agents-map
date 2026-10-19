"""Nerd Font glyphs for the cards: the pick lists (ui_helpers) and the map's boxes (map_view)."""

GLYPHS = {
    "folder": "\U000F024B", "web": "\U000F059F", "chrome": "\U000F02AF", "screen": "\U000F0379",
    "search": "\U000F0349", "git": "\U000F02A2", "github": "\U000F02A4", "code": "\U000F0169",
    "docs": "\U000F05DA", "clock": "\U000F0150", "brain": "\U000F09D1", "steps": "\U000F027B",
    "test": "\U000F0668", "package": "\U000F03D6", "cloud": "\U000F015F", "docker": "\U000F0868",
    "python": "\U000F0320", "node": "\U000F0399", "model": "\U000F01A7", "file": "\U000F0214",
    "app": "\U000F0614", "skill": "\U000F140B", "chip": "\U000F061A",
    "robot": "\U000F06A9",
}
MCP_GLYPHS = {"filesystem": "folder", "git": "git", "fetch": "web", "playwright": "screen",
              "chrome-devtools": "chrome", "brave-search": "search", "github": "github", "context7": "docs",
              "time": "clock", "memory": "brain", "sequential-thinking": "steps", "everything": "test"}
MCP_GROUP_GLYPHS = {"files": "folder", "web": "web", "code": "code", "think": "brain"}
RUN_GLYPHS = {"npm": "node", "pypi": "python", "oci": "docker", "remote": "cloud"}


def glyph(name: str) -> str:
    return GLYPHS.get(name, GLYPHS["package"])
