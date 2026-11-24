"""Project access (Maprisma task 104; Maprisma's features/project_access.dart): what a plan needs,
what a team's access allows, and the steps a run left undone. Pure functions, GTK-free; the
project center calls them with its profile snapshot."""

from __future__ import annotations

import re

# The access levels a team member can have (adapter_data.PROJECT_ACCESS), and what an adapter
# declares per level (project-commands-push is project-commands with the project's Allow push).
PROJECT_ACCESS = ("read-only", "project-edit", "project-commands")
CAPABILITY_LEVELS = (*PROJECT_ACCESS, "project-commands-push")
CAPABILITIES = ("edit", "shell", "network", "outside_project", "git_commit", "git_push")

# The needs a plan can list, in display order, and the capability each one takes.
PROJECT_NEEDS = ("edit", "build", "test", "install", "commit", "push", "network")
NEED_CAPABILITY = {
    "edit": "edit",
    "build": "shell",
    "test": "shell",
    "install": "outside_project",
    "commit": "git_commit",
    "push": "git_push",
    "network": "network",
}

# "None", "nothing", "n/a" (any case, with a final period) mean an empty block.
_EMPTY_WORDS = {"none", "nothing", "n/a", "na", "-", "all done", "nothing left"}
_MARK = re.compile(r"^([-*•]|\d+[.)])\s*")

_KEYWORDS = {
    "edit": re.compile(r"\b(edit|change|modify|implement|add|fix|write|update|refactor|rename|remove)\b"),
    # Commands and phrases only: "build" and "make" alone are ordinary words ("build a feature",
    # "make it bold") and would ask for shell access.
    "build": re.compile(r"\b(compile|cargo build|flutter build|npm run build|make (build|install)|meson|ninja|cmake"
                        r"|makepkg|build (it|the (app|apps|project|package)))\b"),
    "test": re.compile(r"\b(cargo test|cargo clippy|clippy|flutter test|npm test|pytest|unittest|test-changed"
                       r"|run (the )?tests?|tests? pass)\b"),
    "install": re.compile(r"\binstall(s|ed|ing)?\b|install\.sh|\bpkexec\b"),
    "commit": re.compile(r"\bcommit(s|ted|ting)?\b"),
    "push": re.compile(r"\bpush(es|ed|ing)?\b"),
    "network": re.compile(r"\b(download|curl|wget|npm install|pip install)\b"),
}


def project_capabilities(project: dict) -> dict:
    """What an adapter's [project] table declares it can do per access level, every fact present:
    a level or fact it does not declare is False; project-commands exists only with run_commands,
    the push level only with run_commands_push (falling back to project-commands' facts)."""
    declared = project.get("capabilities") if isinstance(project.get("capabilities"), dict) else {}

    def level(name: str, fallback: str | None = None) -> dict:
        facts = declared.get(name)
        if not isinstance(facts, dict) and fallback is not None:
            facts = declared.get(fallback)
        facts = facts if isinstance(facts, dict) else {}
        return {fact: facts.get(fact) is True for fact in CAPABILITIES}

    levels = {"read-only": level("read-only"), "project-edit": level("project-edit")}
    if project.get("run_commands") is not None:
        levels["project-commands"] = level("project-commands")
    if project.get("run_commands_push") is not None:
        levels["project-commands-push"] = level("project-commands-push", "project-commands")
    return levels


def last_fenced_block(text: str, name: str) -> str | None:
    """The text of the last fenced block named `name` (```name ... ```), or None."""
    pattern = re.compile(r"^[ \t]*```[ \t]*" + re.escape(name) + r"[ \t]*\r?\n([\s\S]*?)^[ \t]*```",
                         re.MULTILINE | re.IGNORECASE)
    matches = list(pattern.finditer(text or ""))
    return matches[-1].group(1) if matches else None


def block_items(block: str, separators: str = "\n") -> list:
    """The lines of a block: one item per line (or per separator), list marks removed; the words
    for "nothing" (any case) are no item."""
    items = []
    for line in re.split(f"[{re.escape(separators)}]", block):
        item = _MARK.sub("", line.strip(), count=1).strip()
        if item and re.sub(r"[().!]", "", item.lower()).strip() not in _EMPTY_WORDS:
            items.append(item)
    return items


def plan_needs(plan: str, brief: str) -> tuple:
    """(needs, source) of carrying out `plan`: the plan's last fenced `needs` block wins (source
    "plan"); without one, words of the plan and `brief` ("keywords"); nothing to read: ([], "none")."""
    block = last_fenced_block(plan, "needs")
    if block is not None:
        named = {item.lower() for item in block_items(block, "\n,")}
        return [need for need in PROJECT_NEEDS if need in named], "plan"
    text = f"{plan}\n{brief}".lower()
    if not text.strip():
        return [], "none"
    return [need for need in PROJECT_NEEDS if _KEYWORDS[need].search(text)], "keywords"


def access_gaps(needs, members, allow_push: bool) -> list:
    """The needs no executing member covers at its level, each with the lowest level that would
    (`suggest_access`, None when no agent of the team can), the agents that would, and whether only
    Allow push makes it possible. members: [{agent, level, modes, capabilities{level: {fact: bool}}}]."""
    def covers(member: dict, level: str, need: str) -> bool:
        return ((member.get("capabilities") or {}).get(level) or {}).get(NEED_CAPABILITY[need]) is True

    gaps = []
    for need in needs:
        if any(covers(member, member["level"], need) for member in members):
            continue
        # The lowest level first; Allow push only when nothing else does.
        tries = [("project-edit", False), ("project-commands", allow_push)]
        if not allow_push:
            tries.append(("project-commands", True))
        for level, push in tries:
            able = []
            for member in members:
                if level not in (member.get("modes") or []):
                    continue
                at = "project-commands-push" if push and "project-commands-push" in (member.get("capabilities") or {}) \
                    else level
                if covers(member, at, need) and member["agent"] not in able:
                    able.append(member["agent"])
            if able:
                gaps.append({"need": need, "members": able, "suggest_access": level,
                             "needs_allow_push": push and not allow_push})
                break
        else:
            gaps.append({"need": need, "members": [], "suggest_access": None, "needs_allow_push": False})
    return gaps


def not_done_items(output: str, limit: int = 20) -> list:
    """The steps an agent says it left undone: its answer's last fenced `not_done` block, at most
    `limit` lines of at most 300 characters."""
    block = last_fenced_block(output, "not_done")
    if block is None:
        return []
    return [item if len(item) <= 300 else item[:299] + "…" for item in block_items(block)[:limit]]


def capability_text(facts: dict) -> str:
    """How an execution prompt names a level's capabilities."""
    def yes(fact: str) -> str:
        return "yes" if facts.get(fact) is True else "no"
    return (f"edit files: {yes('edit')}; run commands: {yes('shell')}; network: {yes('network')}; "
            f"install outside the project: {yes('outside_project')}; git commit: {yes('git_commit')}; "
            f"git push: {yes('git_push')}")
