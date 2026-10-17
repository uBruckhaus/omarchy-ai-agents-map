import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import side_tree  # noqa: E402
from side_tree import TreeNode as T  # noqa: E402


def sample():
    return [T("section:agents", "Agents", kind="section", children=[
                T("agent:claude", "Claude", kind="agent", children=[
                    T("agent:claude/Model", "Model", kind="info", value="opus"),
                    T("agent:claude/actions", "", kind="actions", actions=["start", "details"])]),
                T("agent:pi", "Pi", kind="agent", lazy=True)]),
            T("section:mcp", "MCP", kind="section", children=[T("mcp:x", "x", kind="mcp", lazy=True)])]


class TreeTest(unittest.TestCase):
    def test_only_open_nodes_show_their_children(self):
        rows = side_tree.flatten(sample(), {"section:agents"})
        self.assertEqual([r.node.id for r in rows], ["section:agents", "agent:claude", "agent:pi", "section:mcp"])
        rows = side_tree.flatten(sample(), {"section:agents", "agent:claude", "section:mcp"})
        self.assertEqual([r.node.id for r in rows], ["section:agents", "agent:claude", "agent:claude/Model",
                                                     "agent:claude/actions", "agent:pi", "section:mcp", "mcp:x"])

    def test_guides_depth_last_and_parent(self):
        rows = {r.node.id: r for r in side_tree.flatten(sample(), {"section:agents", "agent:claude"})}
        claude, model, actions, pi = (rows[k] for k in ("agent:claude", "agent:claude/Model", "agent:claude/actions",
                                                        "agent:pi"))
        self.assertEqual((claude.depth, claude.guides, claude.last), (1, (), False))
        self.assertEqual((model.depth, model.guides, model.last), (2, (True,), False))   # │ for Claude: Pi follows
        self.assertTrue(actions.last)
        self.assertEqual((pi.last, pi.parent), (True, "section:agents"))
        self.assertEqual(side_tree.parent_of(list(rows.values()), "agent:claude/Model"), "agent:claude")

    def test_lazy_nodes_can_open_and_long_lists_are_cut(self):
        self.assertTrue(T("a", "a", lazy=True).expandable)
        self.assertFalse(T("a", "a").expandable)
        items = [T(f"m{i}", f"m{i}", kind="item") for i in range(20)]
        cut = side_tree.limited(items, 12, lambda n: f"+ {n} more")
        self.assertEqual((len(cut), cut[-1].title, cut[-1].kind), (13, "+ 8 more", "more"))
        self.assertEqual(side_tree.limited(items[:5], 12, str), items[:5])


if __name__ == "__main__":
    unittest.main()
