import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import graph  # noqa: E402
import map_view  # noqa: E402

M = map_view.MapView


class Cards:
    """MapView's cards, order and layout without the widget."""
    for name in ("group_key", "all_groups", "in_order", "keep_order", "groups", "siblings", "order_slot",
                 "reorder", "card_height", "column_height", "needed_height", "layout", "column_top",
                 "visible_rows", "group_title", "default_layout", "card_header_at", "card_at"):
        locals()[name] = M.__dict__[name]
    del name

    def __init__(self, built):
        self.graph, self.order, self.cards, self.group_titles = built, {}, {}, {"mcp": "MCP servers"}
        self.scroll_adjustment, self.zoom, self.saved = None, 1.0, 0

    def shown(self, column):
        return self.graph.column(column)

    def layout_changed(self):
        self.saved += 1


def built(mcp=("alpha", "beta"), links=()):
    g = graph.Graph()
    g.add(graph.Node("agent:claude", 1, "Claude", "", "agent"))
    g.add(graph.Node("agent:pi", 1, "Pi", "", "agent"))
    for name in mcp:
        g.add(graph.Node(f"mcp:{name}", 0, name, "", "mcp"))
    g.add(graph.Node("skills:~/s", 0, "~/s", "", "skills"))
    g.add(graph.Node("cloud:anthropic", 2, "Anthropic", "", "cloud"))
    g.add(graph.Node("runtime:ollama", 2, "Ollama", "", "runtime"))
    for source, target in links:
        g.link(source, target, "mcp")
    return g


class OrderTest(unittest.TestCase):
    def test_a_new_connection_moves_nothing(self):
        view = Cards(built(mcp=("alpha", "beta", "gamma")))
        view.keep_order()
        before = [n.id for n in view.groups(0)[0]]
        # Connecting Pi to gamma: the map's own order puts gamma first now (fewer crossings).
        again = built(mcp=("gamma", "alpha", "beta"), links=[("agent:pi", "mcp:gamma")])
        view.graph = again
        self.assertFalse(view.keep_order())
        self.assertEqual([n.id for n in view.groups(0)[0]], before)

    def test_new_boxes_go_last_and_come_back_in_place(self):
        view = Cards(built(mcp=("alpha", "beta")))
        view.keep_order()
        view.graph = built(mcp=("zeta", "alpha", "beta"))
        self.assertTrue(view.keep_order())
        self.assertEqual([n.label for n in view.groups(0)[0]], ["alpha", "beta", "zeta"])
        view.graph = built(mcp=("beta", "zeta"))                    # alpha gone for now
        view.keep_order()
        view.graph = built(mcp=("zeta", "beta", "alpha"))           # back: its old place
        self.assertEqual([n.label for n in view.groups(0)[0]], ["alpha", "beta", "zeta"])

    def test_groups_per_category(self):
        view = Cards(built())
        self.assertEqual([[n.kind for n in g] for g in view.groups(0)], [["mcp", "mcp"], ["skills"]])
        self.assertEqual([[n.kind for n in g] for g in view.groups(2)], [["cloud"], ["runtime"]])
        self.assertEqual(M.group_key(graph.Node("model:x", 3, "m", "", "model", group="~/Models")), "models:~/Models")

    def test_drag_inside_the_card_changes_the_order(self):
        view = Cards(built(mcp=("alpha", "beta", "gamma")))
        view.keep_order()
        view.layout(1200, 800)
        _x, y, _w, _h = view.boxes["mcp:alpha"]
        top = view.card_rects["mcp"][1]
        # alpha dragged below gamma: slot 2 of the two others.
        gx, gy, _gw, gh = view.boxes["mcp:gamma"]
        slot = view.order_slot("mcp:alpha", gx + 10, gy + gh - 2)
        self.assertEqual(slot, 2)
        view.reorder("mcp:alpha", slot)
        self.assertEqual([n.label for n in view.groups(0)[0]], ["beta", "gamma", "alpha"])
        self.assertEqual(view.saved, 1)
        # Over the header or outside the card: no place in the order (a line may be drawn there).
        self.assertIsNone(view.order_slot("mcp:alpha", gx + 10, top + 4))
        self.assertIsNone(view.order_slot("mcp:alpha", gx + 10, y + 2000))
        self.assertEqual(view.card_header_at(gx + 10, top + 4), "mcp")

    def test_cards_and_their_boxes(self):
        view = Cards(built())
        view.layout(1200, 800)
        cx, cy, cw, ch = view.card_rects["mcp"]
        bx, by, bw, bh = view.boxes["mcp:alpha"]
        self.assertEqual((bx - cx, by - cy), (map_view.CARD_PAD, map_view.GROUP_LABEL_H))
        self.assertEqual(cw, bw + 2 * map_view.CARD_PAD)
        self.assertEqual(ch, view.card_height(2))
        self.assertLess(cy + ch, view.card_rects["skills"][1])         # skills' card below, not on top

    def test_a_moved_card_stays_and_default_layout_puts_it_back(self):
        view = Cards(built())
        view.layout(1200, 800)
        home = view.card_rects["skills"]
        view.cards["skills"] = [700, 500]
        view.layout(1200, 800)
        self.assertEqual(view.card_rects["skills"][:2], (700, 500))
        self.assertEqual(view.boxes["skills:~/s"][:2], (700 + map_view.CARD_PAD, 500 + map_view.GROUP_LABEL_H))
        self.assertGreaterEqual(view.needed_height(), 500 + view.card_height(1))
        view.cards["skills"] = [5000, 20]                             # never off the map's side
        view.layout(1200, 800)
        self.assertEqual(view.card_rects["skills"][0], 1200 - view.card_rects["skills"][2])
        view.order["mcp"] = ["mcp:beta", "mcp:alpha"]
        view.default_layout()
        view.layout(1200, 800)
        self.assertEqual(view.cards, {})
        self.assertEqual(view.card_rects["skills"], home)
        self.assertEqual(view.order["mcp"], ["mcp:alpha", "mcp:beta"])


if __name__ == "__main__":
    unittest.main()
