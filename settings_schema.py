"""Every key of the app's settings.json in one place: its default, the shape a stored value must
have, and what it is for. ai_core.setting() reads through it (a value of the wrong shape counts as
not set), ai_core.update_settings() refuses keys not declared here, and migrate() turns earlier
keys into today's and drops retired ones on start. tests/test_settings_schema.py checks that the
code reads and writes only declared keys."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable

MAP_LAYERS = ("agents", "online", "local", "models", "mcp", "skills")
SYSTEM_MAP_COLUMNS = ("pc", "disks", "programs", "tools", "folders")
MAP_SCAN_INTERVALS = (0, 6, 15, 30, 60, 120, 300)  # 0: startup/manual Refresh only
MAP_SCAN_TARGETS = ('activity', 'agents', 'inventory')
MAP_ANIMATIONS = ("all", "agents", "off")
USAGE_BOX_ITEMS = ("rate", "context", "used")     # as usage_box.ITEMS
USAGE_BOX_SIZES = (7, 8, 9, 10, 12)                 # text size in pt, as usage_box.SIZES (7: the default)
SUBAGENT_MINUTES = (10, 30, 60, 120)                # how long finished subagents stay listed (30: the default)
SUBAGENT_COUNTS = (5, 10, 20, 40)                   # how many finished ones at most (10: the default)
COST_CURRENCIES = ("usd", "eur")                    # what agents paid per token cost is shown in
COST_LIMITS = (10, 25, 50, 100, 200, 500)           # a day, in that currency (100: the default)
WARN_YELLOW = (50, 60, 70, 75, 80, 85)              # % of a day's limit or of a plan's limit used: yellow (75)
WARN_RED = (80, 85, 90, 95, 100)                    # … red (90); always above yellow (agent_details.warn_levels)
LOOKS = ("omarchy", "adwaita")


RULE_ACTIONS = ("install", "update", "remove", "connect", "disconnect", "move", "start", "stop")
RULE_CATEGORIES = ("mcp", "skills", "agents", "providers", "models")


def default_rules() -> list:
    return [{"effect": "allow", "action": action, "category": "*", "item": "*", "agent": "*"}
            for action in RULE_ACTIONS]


def _rules(v) -> bool:
    return isinstance(v, list) and len(v) <= 200 and all(
        isinstance(r, dict) and set(r) == {"effect", "action", "category", "item", "agent"}
        and all(isinstance(r[k], str) and r[k] for k in ("action", "category", "item", "agent"))
        and r["effect"] in ("allow", "deny")
        and (r["action"] == "*" or r["action"] in RULE_ACTIONS)
        and (r["category"] == "*" or r["category"] in RULE_CATEGORIES)
        and (r["item"] == "*" or r["category"] != "*")
        for r in v)


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


def _text_mapping(v) -> bool:
    return isinstance(v, dict) and all(isinstance(k, str) and isinstance(p, str) for k, p in v.items())


def _history_mapping(v) -> bool:
    return isinstance(v, dict) and all(isinstance(k, str) and _texts(paths) for k, paths in v.items())


ASK_PROMPTS_COLUMNS = 16                            # Ask AI cards with own suggestions, at most
ASK_PROMPTS_ITEMS = 12                              # suggestions per card, at most
ASK_PROMPT_CHARS = 2000                             # characters of one suggestion, at most


def _ask_prompts(v) -> bool:
    """Ask AI's suggestions per card column: an int keeps that built-in example (its index),
    a string is the user's own (or edited) request; a column left out shows the built-ins."""
    def item(x) -> bool:
        if isinstance(x, bool):
            return False
        if isinstance(x, int):
            return 0 <= x <= 199
        return isinstance(x, str) and x == x.strip() and 0 < len(x) <= ASK_PROMPT_CHARS
    return isinstance(v, dict) and len(v) <= ASK_PROMPTS_COLUMNS and all(
        isinstance(k, str) and re.fullmatch(r"[a-z_]{1,32}", k) is not None
        and isinstance(items, list) and len(items) <= ASK_PROMPTS_ITEMS
        and all(item(x) for x in items) and len({repr(x) for x in items}) == len(items)
        for k, items in v.items())


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
    "flutter_theme_mode": Setting("system", _one_of("system", "light", "dark"), "Flutter Settings › Theme"),
    "window_mode": Setting("auto", _one_of("auto", "popup", "window"),
                           "Flutter Settings › Window: auto = a popup under the bar on Omarchy, else a window"),
    "map_light_fps": Setting(20, lambda v: type(v) is int and v in (20, 30, 60), "Flutter map-light frame rate: 20, 30 or 60 fps"),
    "look": Setting("omarchy", _one_of(*LOOKS), "Settings › Look on Omarchy: its theme or GNOME's Adwaita"),
    "choice_text": Setting("bright", _one_of("bright", "dim"),
                           "Settings › Text in choice boxes: bright (white on dark themes) or dimmed"),
    "corners": Setting("windows", _text, "Settings › Corners: \"windows\", \"slight\", \"rounded\" or \"square\" (corners.choice)"),
    # The window
    "window_size": Setting(None, _size, "the window's size when it was last closed: [width, height]"),
    "last_view": Setting("main", _text, "the view open when the window was last closed"),
    "terminal_tabs": Setting(True, _bool, "Settings › Terminal tabs above the map"),
    "screenshot_key": Setting(True, _bool, "Settings › F12 saves a screenshot"),
    "action_rules": Setting(default_rules(), _rules, "what may be done, task 019: the most specific rule decides, on a tie allow"),
    "action_rule_hints": Setting(True, _bool, "a refusal names the rule; off: it only says it is not allowed"),
    "smart_helper": Setting(None, _text, "Ask AI: the chosen helper's command, for all four cards"),
    "ask_prompts": Setting({}, _ask_prompts, "Ask AI: the suggestions per card column, own and kept built-in ones"),
    "dictation": Setting(True, _bool, "Settings › Dictation: microphone in Ask AI"),
    "dictation_engine": Setting("auto", _one_of("auto", "omarchy", "whisper", "local", "online"),
                                "Settings › Dictation: chosen backend"),
    "dictation_whisper_model": Setting("", _text, "Settings › Dictation: chosen local Whisper model file name"),
    "screenshot_folder": Setting(None, _text, "Settings › Screenshots go to: empty is ~/Pictures/AI Agents Map"),
    "launcher_entry": Setting(None, _bool, "Settings › In the app launcher (None: not asked yet)"),
    "agent_folder": Setting(None, _text, "Settings › Agents start in: the folder agents start in"),
    "agent_projects": Setting({}, _text_mapping, "Start: the optional project folder pinned as each agent's default"),
    "provider_models": Setting({}, _text_mapping, "Map: the model each provider uses, drawn provider to model; an agent connected to the provider later gets it"),
    "agent_project_history": Setting({}, _history_mapping, "Project folders used to start each agent, newest first"),
    # The map
    "flutter_map_layout": Setting({}, lambda v: __import__("flutter_map_editor").valid_layout(v),
                                  "Flutter map card positions/order/folds and node pins (bounded schema)"),
    "map_layers": Setting(list(MAP_LAYERS), _texts, "the map's Show switches of the full-size window"),
    "system_map_columns": Setting(list(SYSTEM_MAP_COLUMNS),
                                  lambda v: _texts(v) and set(v) <= set(SYSTEM_MAP_COLUMNS),
                                  "the System Map's column filters"),
    "map_scan_interval": Setting(6, lambda v: type(v) is int and v in MAP_SCAN_INTERVALS,
                                 "Automatic map scan interval in seconds; 0: manual only"),
    "map_scan_pause_unfocused": Setting(True, _bool, "Pause automatic map scans while the window is unfocused"),
    "map_scan_targets": Setting(list(MAP_SCAN_TARGETS), lambda v: _texts(v) and set(v) <= set(MAP_SCAN_TARGETS),
                                "Background scan groups; startup/manual Refresh always refresh everything"),
    "map_animations": Setting("all", _one_of(*MAP_ANIMATIONS), "Settings › Map animations"),
    "usage_box": Setting(True, _bool, "Settings › Usage statistics box: shown on a working agent's line"),
    "usage_box_items": Setting(list(USAGE_BOX_ITEMS), lambda v: _texts(v) and set(v) <= set(USAGE_BOX_ITEMS),
                               "Settings › Usage statistics box: what it shows (usage_box.ITEMS)"),
    "usage_box_size": Setting(7, lambda v: v in USAGE_BOX_SIZES and not isinstance(v, bool),
                              "Settings › Usage statistics box: its text size in pt"),
    "subagents_minutes": Setting(30, lambda v: v in SUBAGENT_MINUTES and not isinstance(v, bool),
                                 "Settings › Subagents: minutes a finished one stays on the subagents page"),
    "subagents_kept": Setting(10, lambda v: v in SUBAGENT_COUNTS and not isinstance(v, bool),
                              "Settings › Subagents: how many finished ones it keeps at most"),
    "cost_currency": Setting("usd", _one_of(*COST_CURRENCIES),
                             "Settings › Costs: US dollars, or euros at the ECB's daily rate (plugin_costs.usd_per_eur)"),
    "cost_limit": Setting(100, lambda v: v in COST_LIMITS and not isinstance(v, bool),
                          "Settings › Costs: a day's limit for an agent paid per token (its cost label's colour)"),
    "warn_yellow": Setting(75, lambda v: v in WARN_YELLOW and not isinstance(v, bool),
                           "Settings › Costs: yellow from this % of a day's limit (API key) or of a plan's limit used"),
    "warn_red": Setting(90, lambda v: v in WARN_RED and not isinstance(v, bool),
                        "Settings › Costs: red from this % (the same two kinds)"),
    "warn_notify": Setting(True, _bool, "Settings › Costs: a desktop notification when an agent turns red"),
    "map_zoom": Setting(1.0, _number, "the map's zoom"),
    "map_layout": Setting({}, _mapping, "the map's cards: {\"order\": {card: [box ids]}, \"cards\": {card: [x, y]}, "
                          "\"collapsed\": [card]} (drag a box inside its card, a card by its header, click a header to "
                          "fold it; Default layout clears it)"),
    "map_layout_pinned": Setting(False, _bool, "the pin right of the map's zoom: keep map_layout after Refresh and a "
                                 "restart (unpinned, the default: the map's default layout)"),
    # Pages
    "setup_done": Setting(False, _bool, "AI Agents Map's welcome page was finished once"),
    "first_connection_done": Setting(False, _bool, "the card above the map that shows the first connection: done or closed"),
    "setup_collapsed": Setting([], _texts, "Setup: sections folded (closed-by-default ones as \"<key>:open\")"),
    "details_open": Setting([], _texts, "details pages: group cards opened (open-by-default ones as \"<key>:closed\")"),
    "model_folders": Setting([], _texts, "Setup › Models: folders added by hand (Add folder, Change folder), ~-paths"),
    "mcp_folders": Setting([], _texts, "Setup › MCP servers: folders added by hand (Add folder), where programs are looked for"),
    "skill_folders": Setting([], _texts, "Setup › Skills: folders added by hand (Add folder), where skill sets are looked for"),
    "add_where": Setting({}, _mapping, "the add pages' last tab: {\"mcp\": …, \"models\": …, \"skills\": …}"),
    "seen_found": Setting({}, _mapping, "programs and skill sets already shown as new on this PC"),
    "header_order": Setting([], _texts, "the order of the header's symbols (hold the left mouse button + drag)"),
    "web_sources": Setting({}, _mapping, "the user's own search sites per kind (web_sources.py)"),
    "web_source": Setting({}, _mapping, "the search site picked last per kind (web_sources.py)"),
    # Local LLMs and models
    "providers_with_agents": Setting(True, _bool, "Settings › Start local LLMs with their agents"),
    "models_root": Setting(None, _text, "Settings › Models folder"),
    "removed_models_folders": Setting({}, _mapping, "{local LLM: its models folder} of local LLMs uninstalled here"),
    "look_up_new_agents": Setting(True, _bool, "Settings › Look up new agents"),
    "ai_checks": Setting(False, _bool, "Settings › Daily AI checks (an agent looks up MCP server updates; uses tokens)"),
    "model_advice_count": Setting(10, lambda v: v in (5, 10, 15, 20) and not isinstance(v, bool),
                                  "Models › Ask an agent: how many models it suggests (model_advice.MODEL_COUNTS)"),
    "mcp_advice_count": Setting(10, lambda v: v in (5, 10, 15, 20) and not isinstance(v, bool),
                                "Add MCP server › Ask an agent: how many servers it suggests (mcp_advice.SERVER_COUNTS)"),
    "ai_checks_agent": Setting("", _text, "Settings › Agent for AI checks (its command; \"\": the first that can)"),
    "first_agent": Setting({}, _mapping, "the first-agent guide: {\"later\": true when put off, \"verified\": [agents whose test answered]} (first_agent.py)"),
    "ai_local_helper": Setting({}, _mapping, "Settings › Local helper: {agent, provider, model} confirmed by the user; {}: none (local_helper.py)"),
    "mcp_list": Setting(True, _bool, "Settings › Keep the MCP server list (mcp_registry.refresh_list on start)"),
    # Add-ons (AI Agents Map)
    "addons_enabled": Setting([], _texts, "the add-ons switched on"),
    "addons_off": Setting([], _texts, "the add-ons the user switched off: never switched on again at the start"),
    "addons_online": Setting(True, _bool, "Settings › Look for add-ons online"),
    "installs_in_window": Setting(True, _bool, "add-on page: installs run in a terminal inside the window"),
}

# Earlier keys and what they became: old key -> (new key, value converter or None to just drop it).
RENAMED: dict[str, tuple[str, Callable[[Any], Any]]] = {
    "animate_lines": ("map_animations", lambda v: "off" if v is False else "all"),   # "Animate active lines"
}
# Keys nothing reads any more (Hide, the column lists, the overview's Show list, Setup · + Available).
RETIRED = ("plugin_migration", "launcher_entry",   # the AI Agents Map plugin's import and launcher
           "widen",                         # "Maximize for more room" in the header (removed)
           "hidden_items", "hidden_nodes", "hidden_providers", "card_hidden", "start_scope", "show_more_hint",
           "setup_mode",                          # Setup's All/Installed switch
           "sidebar", "sidebar_width",            # the list beside the map
           "health_line",                         # the health check's line (now the header's !)
           "chess_window",                        # where chess opened (chess moved to AI Playground)
           # AI Playground's (a plugin of its own now, with its own settings)
           "chess_thinking", "chess_strength", "chess_rival_strength", "chess_board", "chess_pieces",
           "chess_coordinates", "playground_tab", "playground_agents", "duel_scores")


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
