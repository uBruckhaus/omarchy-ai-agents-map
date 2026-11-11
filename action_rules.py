"""What may be done (Maprisma task 019, action_rules.dart): the user's allow and deny rules.

A rule is {effect: allow|deny, action, category, item, agent}; "*" matches anything. The most specific
matching rule decides (an item counts 8, an agent 4, a category 2, an action 1); on a tie allow wins; no
matching rule at all means no. The defaults allow everything. Every change the app makes asks first
(AgentsMap.rule_allows): what is drawn on the map, a menu, a page's button, and Ask AI's proposals.
"""

from __future__ import annotations

import os
import re

from i18n import N_, _
from settings_schema import RULE_ACTIONS, RULE_CATEGORIES, default_rules  # noqa: F401  (one source)

ACTION_NAMES = {"install": N_("Install"), "update": N_("Update"), "remove": N_("Remove"), "connect": N_("Connect"),
                "disconnect": N_("Disconnect"), "move": N_("Move"), "start": N_("Start"), "stop": N_("Stop")}
CATEGORY_NAMES = {"mcp": N_("MCP servers"), "skills": N_("Skills"), "agents": N_("Agents"),
                  "providers": N_("Providers"), "models": N_("Models")}


def score(rule: dict) -> int:
    return ((8 if rule["item"] != "*" else 0) + (4 if rule["agent"] != "*" else 0)
            + (2 if rule["category"] != "*" else 0) + (1 if rule["action"] != "*" else 0))


def normal(category: str, item: str) -> str:
    """An item as rules compare it: models without case, an MCP server without its "~2", folders with ~."""
    if category == "models":
        return item.lower()
    if category == "mcp":
        return re.sub(r"~\d+$", "", item)
    if category == "skills" and item:
        home = os.path.expanduser("~")
        path = os.path.expanduser(item)
        return "~" + path[len(home):] if path == home or path.startswith(home + "/") else path
    return item


def matches(rule: dict, action: str, category: str, item: str, agent: str) -> bool:
    def same(key, value):
        return rule[key] == "*" or rule[key] == value
    # "Everything for agent X" also covers agent X itself in the Agents category.
    by_agent = same("agent", agent) or (category == "agents" and rule["agent"] == item)
    by_item = rule["item"] == "*" or normal(category, rule["item"]) == normal(category, item)
    return same("action", action) and same("category", category) and by_item and by_agent


def deciding(rules: list, action: str, category: str, item: str = "", agent: str = "") -> dict | None:
    best = None
    for rule in rules:
        if not matches(rule, action, category, item, agent):
            continue
        if best is None or score(rule) > score(best) or (score(rule) == score(best) and rule["effect"] == "allow"):
            best = rule
    return best


def allows(rules: list, action: str, category: str, item: str = "", agent: str = "") -> bool:
    rule = deciding(rules, action, category, item, agent)
    return rule is not None and rule["effect"] == "allow"


def refusal(rules: list, hints: bool, action: str, category: str, item: str = "", agent: str = "") -> str | None:
    """None when the rules allow it, else what to say (Maprisma's RuleRefused): the action, category,
    item and agent, or only that it is not allowed (Settings: Say which rule refused something, off)."""
    if allows(rules, action, category, item, agent):
        return None
    if not hints:
        return _("This is not allowed here.")
    parts = [_(ACTION_NAMES.get(action, action)), _(CATEGORY_NAMES.get(category, category))]
    parts += [p for p in (item, agent) if p]
    return _("{0}: not allowed by your rules").format(", ".join(parts))


def node_check(node_id: str) -> tuple[str, str] | None:
    """(category, item) of a map node: mcp:x, skills:/p, runtime:k, cloud:k, agent:c, model:runtime:k:m."""
    kind, _sep, rest = node_id.partition(":")
    if kind == "mcp":
        return "mcp", rest
    if kind == "skills":
        return "skills", rest
    if kind in ("runtime", "cloud"):
        return "providers", rest
    if kind == "agent":
        return "agents", rest
    if kind == "model" and node_id.count(":") >= 3:
        return "models", node_id.split(":", 3)[3]
    return None


ADDON_STEPS = {"on": "install", "install": "install", "update": "update", "off": "remove", "remove": "remove"}


def addon_check(step: str, addon_id: str, kind: str = "") -> tuple | None:
    """(action, category, item) of an add-on change: switching on or installing counts as install,
    off or removing as remove; an agent's add-on is in Agents, the others (local LLMs, online
    providers) in Providers. None: not a change (details)."""
    action = ADDON_STEPS.get(step)
    if action is None:
        return None
    agent = kind == "agent" or (not kind and addon_id.startswith("agent-"))
    return action, "agents" if agent else "providers", addon_id


def candidate_checks(candidate: dict) -> list[tuple]:
    """(action, category, item, agent) checks of an Ask AI proposal (Maprisma's smartCandidateChecks)."""
    action = candidate.get("action") or {}
    kind = action.get("kind")
    if kind == "map_link":
        target = node_check(action["target"]) or ("providers", action["target"])
        return [("disconnect" if action.get("remove") else "connect", target[0], target[1],
                 action["source"].partition(":")[2])]
    if kind == "mcp_add":
        return [("install", "mcp", action["name"].split("/")[-1])]
    if kind == "model_download":
        return [("install", "models", action["repo"])]
    if kind == "model_remove":
        return [("remove", "models", action["model"])]
    if kind == "agent_task":
        return [("start", "agents", action["command"])]
    if kind == "agent_action":
        step = {"install": "install", "update": "update", "uninstall": "remove"}.get(action["step"], "connect")
        return [(step, "agents", action["command"])]
    if kind == "provider_action":
        step = action["step"]
        return [] if step == "performance" else [(step, "providers", action["key"])]
    if kind == "menu_action":
        area, step, key = action["area"], action["step"], action["key"]
        if area == "agents":
            return {"start": [("start", "agents", key)], "halt": [("stop", "agents", key)],
                    "security": [("update", "agents", key)], "key": [("connect", "agents", key)]}.get(step, [])
        if area == "addons":
            check = addon_check(step, key)
            return [check] if check else []
        if area == "models" and step == "move":
            return [("move", "models", "")]
    return []
