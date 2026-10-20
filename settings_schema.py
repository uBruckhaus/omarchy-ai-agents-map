"""Every key of the app's settings.json in one place: its default, the shape a stored value must
have, and what it is for. ai_core.setting() reads through it (a value of the wrong shape counts as
not set), ai_core.update_settings() refuses keys not declared here, and migrate() turns earlier
keys into today's and drops retired ones on start. tests/test_settings_schema.py checks that the
code reads and writes only declared keys."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

MAP_LAYERS = ("agents", "online", "local", "models", "mcp", "skills")
MAP_ANIMATIONS = ("all", "agents", "off")
LOOKS = ("omarchy", "adwaita")


def _bool(v) -> bool:
    return isinstance(v, bool)


def _text(v) -> bool:
    return isinstance(v, str)


def _number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _texts(v) -> bool:
    return isinstance(v, list) and all(isinstance(x, str) for x in v)


def _mapping(v) -> bool:
    return isinstance(v, dict)


def _one_of(*choices) -> Callable[[Any], bool]:
    return lambda v: v in choices


def _size(v) -> bool:
    return isinstance(v, list) and len(v) == 2 and all(isinstance(x, int) and x > 0 for x in v)


@dataclass(frozen=True)
class Setting:
    default: Any
    valid: Callable[[Any], bool]
    about: str


SCHEMA: dict[str, Setting] = {
    # Look and language
    "language": Setting("auto", _text, "Settings › Language: \"auto\" (the system's) or a language code"),
    "look": Setting("omarchy", _one_of(*LOOKS), "Settings › Look on Omarchy: its theme or GNOME's Adwaita"),
    "choice_text": Setting("bright", _one_of("bright", "dim"),
                           "Settings › Text in choice boxes: bright (white on dark themes) or dimmed"),
    "corners": Setting("windows", _text, "Settings › Corners: \"windows\", \"rounded\" or \"square\" (corners.choice)"),
    # The window
    "window_size": Setting(None, _size, "the window's size when it was last closed: [width, height]"),
    "last_view": Setting("main", _text, "the view open when the window was last closed"),
    "widen": Setting("workspace", _text, "Settings › Too narrow for the map: what Maximize for more room does"),
    "chess_thinking": Setting("normal", _one_of("quick", "normal", "deep", "max"), "AI Playground › Chess › Thinking time"),
    "chess_strength": Setting("club", _one_of("beginner", "casual", "club", "master", "max"), "AI Playground › Chess › Strength"),
    "chess_board": Setting("theme", _one_of("theme", "wood", "green", "blue", "grey"), "AI Playground › Chess › Board and pieces › Board"),
    "chess_pieces": Setting("classic", _one_of("classic", "flat", "theme"), "AI Playground › Chess › Board and pieces › Pieces"),
    "chess_coordinates": Setting(True, _bool, "AI Playground › Chess › Board and pieces › Coordinates"),
    # AI Playground (its own settings file; the same schema)
    "playground_tab": Setting("chess", _text, "AI Playground: the tab open when it was last closed"),
    "playground_agents": Setting([], _texts, "AI Playground › Speed and cost: the agents ticked"),
    "duel_scores": Setting({}, _mapping, "AI Playground › Head to head: {\"a|b\": [a wins, ties, b wins]}"),
    "terminal_tabs": Setting(True, _bool, "Settings › Terminal tabs above the map"),
    "launcher_entry": Setting(None, _bool, "Settings › In the app launcher (None: not asked yet)"),
    "agent_folder": Setting(None, _text, "Settings › Agents start in: the folder agents start in"),
    # The map
    "map_layers": Setting(list(MAP_LAYERS), _texts, "the map's Show switches of the full-size window"),
    "map_animations": Setting("all", _one_of(*MAP_ANIMATIONS), "Settings › Map animations"),
    "map_zoom": Setting(1.0, _number, "the map's zoom"),
    # Pages
    "setup_done": Setting(False, _bool, "Asterism's welcome page was finished once"),
    "first_connection_done": Setting(False, _bool, "the card above the map that shows the first connection: done or closed"),
    "setup_collapsed": Setting([], _texts, "Setup: sections folded (closed-by-default ones as \"<key>:open\")"),
    "details_open": Setting([], _texts, "details pages: group cards opened (open-by-default ones as \"<key>:closed\")"),
    "add_where": Setting({}, _mapping, "the add pages' last tab: {\"mcp\": …, \"models\": …, \"skills\": …}"),
    "seen_found": Setting({}, _mapping, "programs and skill sets already shown as new on this PC"),
    "web_sources": Setting({}, _mapping, "the user's own search sites per kind (web_sources.py)"),
    "web_source": Setting({}, _mapping, "the search site picked last per kind (web_sources.py)"),
    # Local LLMs and models
    "providers_with_agents": Setting(True, _bool, "Settings › Start local LLMs with their agents"),
    "models_root": Setting(None, _text, "Settings › Models folder"),
    "removed_models_folders": Setting({}, _mapping, "{local LLM: its models folder} of local LLMs uninstalled here"),
    "look_up_new_agents": Setting(True, _bool, "Settings › Look up new agents"),
    # Add-ons (Asterism)
    "addons_enabled": Setting([], _texts, "the add-ons switched on"),
    "addons_online": Setting(True, _bool, "Settings › Look for add-ons online"),
    "installs_in_window": Setting(True, _bool, "add-on page: installs run in a terminal inside the window"),
}

# Earlier keys and what they became: old key -> (new key, value converter or None to just drop it).
RENAMED: dict[str, tuple[str, Callable[[Any], Any]]] = {
    "animate_lines": ("map_animations", lambda v: "off" if v is False else "all"),   # "Animate active lines"
}
# Keys nothing reads any more (Hide, the column lists, the overview's Show list, Setup · + Available).
RETIRED = ("hidden_items", "hidden_nodes", "hidden_providers", "card_hidden", "start_scope", "show_more_hint",
           "setup_mode",                          # Setup's All/Installed switch
           "sidebar", "sidebar_width",            # the list beside the map
           "health_line",                         # the health check's line (now the header's !)
           "chess_window")                        # where chess opened (chess moved to AI Playground)


def value(settings: dict, key: str):
    """The stored value when it has the declared shape, else the default (a fresh copy)."""
    setting = SCHEMA[key]
    stored = settings.get(key)
    if stored is not None and setting.valid(stored):
        return stored
    default = setting.default
    return list(default) if isinstance(default, list) else dict(default) if isinstance(default, dict) else default


def migrate(settings: dict) -> dict:
    """{key: new value or None (remove)} turning an earlier settings file into today's."""
    changes = {}
    for old, (new, convert) in RENAMED.items():
        if old in settings:
            if new not in settings:
                changes[new] = convert(settings[old])
            changes[old] = None
    for key in RETIRED:
        if key in settings:
            changes[key] = None
    return changes
