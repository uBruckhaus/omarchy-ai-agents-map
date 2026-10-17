"""The sidebar as a tree (Settings › Sidebar: Tree): categories, their boxes, and what an
opened box holds (facts, models, skills, its buttons). This module is the plain part:
the nodes, which rows show for what is open, and which guide lines each row draws.
ai_popup draws the rows and runs the buttons.
"""

from dataclasses import dataclass, field


@dataclass
class TreeNode:
    id: str                      # "section:agents", "agent:claude", "agent:claude/mcp", ...
    title: str
    subtitle: str = ""
    kind: str = ""               # section, agent, runtime, mcp, skills, info, item, actions, more
    icon: str = ""               # a Nerd Font glyph (as on the cards)
    icon_font: str = ""
    status: str = ""             # running, stopped, missing: the dot
    badge: str = ""
    dim: bool = False
    value: str = ""              # info rows: the value next to the title
    actions: list = field(default_factory=list)   # actions rows: keys ai_popup knows
    children: list = field(default_factory=list)
    lazy: bool = False           # children are made when it opens (they read files)

    @property
    def expandable(self) -> bool:
        return bool(self.children) or self.lazy


@dataclass
class Row:
    node: TreeNode
    depth: int
    guides: tuple                # per ancestor level below the top: True when that level goes on below
    last: bool                   # the last child of its parent (└ instead of ├)
    parent: str = ""             # the parent's id ("" at the top)


def flatten(nodes: list, expanded: set, depth: int = 0, guides: tuple = (), parent: str = "") -> list:
    """The rows to show: every node, and the children of the open ones, in order."""
    rows = []
    for index, node in enumerate(nodes):
        last = index == len(nodes) - 1
        rows.append(Row(node, depth, guides, last, parent))
        if node.children and node.id in expanded:
            rows += flatten(node.children, expanded, depth + 1,
                            guides + ((not last),) if depth > 0 else guides, node.id)
    return rows


def parent_of(rows: list, node_id: str) -> str:
    return next((r.parent for r in rows if r.node.id == node_id), "")


def limited(items: list, most: int, more_text) -> list:
    """At most `most` nodes; the rest as one "+ N more" node (more_text(n) gives its title)."""
    if len(items) <= most:
        return items
    rest = len(items) - most
    return items[:most] + [TreeNode(f"{items[0].id}#more", more_text(rest), kind="more")]
