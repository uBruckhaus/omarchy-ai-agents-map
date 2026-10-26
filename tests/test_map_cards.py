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
                 "visible_rows", "group_title", "default_layout", "card_header_at", "card_at",
                 "agent_slots", "agent_width", "text_width", "card_rows", "toggle_card", "make_room"):
        locals()[name] = M.__dict__[name]
    del name

    def __init__(self, built):
        self.graph, self.order, self.cards, self.group_titles = built, {}, {}, {"mcp": "MCP servers"}
        self.scroll_adjustment, self.zoom, self.saved = None, 1.0, 0
        self.agent_links = set()
        self.costs, self.text_widths, self.measure = {}, {}, None
        self.collapsed, self.folded = set(), set()
        self.view_columns = 0

    def relayout(self):
        pass

    @staticmethod
    def viewport_width():
        return 0

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

    def test_the_agents_column_fits_the_name_and_every_symbol(self):
        """The symbols' strip is always as wide as all of them; the name has its own room beside it,
        and a longer name makes the agent boxes and their card wider (not the others)."""
        view = Cards(built())
        view.layout(1200, 800)
        slots = view.agent_slots()
        bx, _by, bw, _bh = view.boxes["agent:pi"]
        name = view.text_width("Claude", "Sans Bold 9")
        self.assertGreaterEqual(bw, map_view.TILE_ROOM + name + map_view.TEXT_GAP + slots["strip"])
        mcp_w = view.boxes["mcp:alpha"][2]
        long = built()
        long.add(graph.Node("agent:long", 1, "An agent with a really rather long name indeed", "", "agent"))
        wide = Cards(long)
        wide.layout(1200, 800)
        self.assertGreater(wide.boxes["agent:pi"][2], bw)
        self.assertEqual(wide.boxes["agent:pi"][2], wide.boxes["agent:long"][2])   # one width per column
        self.assertEqual(wide.card_rects["agents"][2], wide.boxes["agent:pi"][2] + 2 * map_view.CARD_PAD)
        self.assertEqual(wide.boxes["mcp:alpha"][2], mcp_w)
        # Slots from the right: start, the outside stop, battery/cost, console, subagents, traffic.
        order = [slots[k] for k in ("start", "halt", "meter", "console", "subagents", "traffic", "strip")]
        self.assertEqual(order, sorted(order))

    def test_the_symbols_reach_the_right_border(self):
        """Right-aligned: the start button's slot begins just inside the pin's corner, not a
        tick's width further in (a selected agent's tick goes left of the strip)."""
        slots = Cards(built()).agent_slots()
        self.assertEqual(slots["start"], map_view.STRIP_RIGHT)
        self.assertEqual(map_view.STRIP_RIGHT, map_view.PIN_W + 4)

    def test_cards_are_open_by_default_and_fold_to_their_header(self):
        view = Cards(built(links=[("agent:pi", "mcp:alpha")]))
        view.layout(1200, 800)
        self.assertEqual(view.collapsed, set())
        open_h = view.card_rects["mcp"][3]
        self.assertEqual(view.boxes["mcp:alpha"][3], map_view.NODE_H)
        saved = view.saved
        view.toggle_card("mcp")
        self.assertEqual(view.saved, saved + 1)                     # saved with the map's layout
        view.layout(1200, 800)
        cx, cy, _cw, ch = view.card_rects["mcp"]
        self.assertEqual(ch, map_view.CARD_HEAD)
        self.assertLess(ch, open_h)
        # Its boxes sit on the header, not drawn: Pi's line still ends at the card.
        self.assertEqual(view.boxes["mcp:alpha"][1], cy)
        self.assertEqual(view.boxes["mcp:alpha"][3], map_view.CARD_HEAD)
        self.assertEqual(view.folded, {"mcp:alpha", "mcp:beta"})
        self.assertNotIn("agent:pi", view.folded)
        view.toggle_card("mcp")
        view.layout(1200, 800)
        self.assertEqual(view.card_rects["mcp"][3], open_h)
        self.assertEqual(view.folded, set())

    @staticmethod
    def folded_mcp(keys):
        """MCP folded and `keys` moved out of their columns first (that re-centres the columns),
        so MCP's folded card is measured where it stays while the others are placed around it."""
        view = Cards(built())
        view.collapsed.add("mcp")
        for key in keys:
            view.cards[key] = [900, 2000]
        view.layout(1200, 800)
        return view, view.card_rects["mcp"]

    @staticmethod
    def overlap(a, b) -> bool:
        return a[0] < b[0] + b[2] and b[0] < a[0] + a[2] and a[1] < b[1] + b[3] and b[1] < a[1] + a[3]

    def test_opening_a_card_moves_the_cards_it_would_cover_down(self):
        view, (mx, my, mw, _mh) = self.folded_mcp(("skills", "online", "local"))
        view.cards["skills"] = [mx, my + map_view.CARD_HEAD + 10]
        view.layout(1200, 800)
        sh = view.card_rects["skills"][3]
        view.cards["online"] = [mx, my + map_view.CARD_HEAD + 10 + sh + 10]     # right under Skills
        far = [mx + mw + 200, my + 5]
        view.cards["local"] = far                                             # beside it: stays
        view.layout(1200, 800)
        self.assertEqual(view.card_rects["mcp"][:2], (mx, my))               # MCP stays put meanwhile
        view.toggle_card("mcp")
        view.layout(1200, 800)
        mcp, skills, online = (view.card_rects[k] for k in ("mcp", "skills", "online"))
        self.assertFalse(self.overlap(mcp, skills))
        self.assertFalse(self.overlap(skills, online))                        # pushed on in turn
        self.assertFalse(self.overlap(mcp, online))
        self.assertEqual(view.cards["local"], far)

    def test_opening_a_card_that_grows_upwards_moves_a_card_above_out_of_the_way(self):
        """A column's card stays centred, so it can grow upwards into a card put above it by hand."""
        view, (mx, my, _mw, _mh) = self.folded_mcp(("skills",))
        sh = view.card_rects["skills"][3]
        view.cards["skills"] = [mx, my - sh - 5]                            # just above, apart
        view.layout(1200, 800)
        self.assertFalse(self.overlap(view.card_rects["skills"], view.card_rects["mcp"]))
        view.toggle_card("mcp")
        view.layout(1200, 800)
        self.assertLess(view.card_rects["mcp"][1], my)                       # it did grow upwards
        self.assertFalse(self.overlap(view.card_rects["skills"], view.card_rects["mcp"]))

    def test_no_overlap_left_after_opening_with_several_cards(self):
        view, (mx, my, _mw, mh) = self.folded_mcp(("skills", "online", "local"))
        for i, key in enumerate(("skills", "online", "local")):
            view.cards[key] = [mx, my + mh + 4 + 20 * i]                     # a heap under the folded card
        view.layout(1200, 800)
        view.toggle_card("mcp")
        view.layout(1200, 800)
        keys = ("mcp", "skills", "online", "local")
        for i, a in enumerate(keys):
            for b in keys[i + 1:]:
                self.assertFalse(self.overlap(view.card_rects[a], view.card_rects[b]), (a, b))

    def test_a_pushed_card_does_not_land_on_a_card_in_its_column(self):
        """Codex's case: MCP and Online placed by hand, Skills still in its column below them."""
        view = Cards(built())
        view.collapsed.add("mcp")
        view.cards = {"mcp": [4, 200], "online": [4, 230]}
        view.layout(1200, 800)
        before = dict(view.card_rects)
        self.assertFalse(any(self.overlap(before[a], before[b]) for a in before for b in before if a < b))
        view.toggle_card("mcp")
        view.layout(1200, 800)
        rects = view.card_rects
        for a in rects:
            for b in rects:
                if a < b:
                    self.assertFalse(self.overlap(rects[a], rects[b]), (a, b))
        self.assertNotIn("skills", view.cards)                         # the column's card stays in it

    def test_a_column_card_pushed_down_by_the_opening_does_not_land_on_a_placed_card(self):
        """Codex's case: opening MCP re-centres its column, Skills moves down onto Online (placed)."""
        view = Cards(built())
        view.collapsed.add("mcp")
        view.cards = {"online": [4, 466]}
        view.layout(1200, 800)
        before = dict(view.card_rects)
        self.assertFalse(any(self.overlap(before[a], before[b]) for a in before for b in before if a < b))
        view.toggle_card("mcp")
        view.layout(1200, 800)
        rects = view.card_rects
        for a in rects:
            for b in rects:
                if a < b:
                    self.assertFalse(self.overlap(rects[a], rects[b]), (a, b))

    def test_opening_never_lays_a_card_on_another_random_layouts(self):
        """Many random layouts (cards folded, some placed by hand anywhere): after opening a card no
        two cards lie on each other that did not before."""
        import random
        rng = random.Random(24)
        keys = ("agents", "online", "local", "mcp", "skills")
        for _case in range(400):
            view = Cards(built())
            view.collapsed = {k for k in keys if rng.random() < 0.6}
            if not view.collapsed:
                view.collapsed.add(rng.choice(keys))
            view.cards = {k: [rng.choice((4, 40, 300, 600)), rng.randrange(0, 700)]
                          for k in keys if rng.random() < 0.4}
            view.layout(1200, 800)
            before = dict(view.card_rects)
            key = rng.choice(sorted(view.collapsed))
            view.toggle_card(key)
            view.layout(1200, 800)
            after = view.card_rects
            for a in after:
                for b in after:
                    if a < b and self.overlap(after[a], after[b]):
                        self.assertTrue(self.overlap(before[a], before[b]),
                                        (_case, key, a, b, before, after, dict(view.cards)))

    def test_an_opened_card_placed_by_hand_moves_past_a_column_card_it_would_cover(self):
        view = Cards(built())
        view.collapsed.add("mcp")
        view.cards = {"mcp": [900, 2000]}                                # out of its column first
        view.layout(1200, 800)
        sx, sy, _sw, _sh = view.card_rects["skills"]                     # Skills where it now stays
        view.cards["mcp"] = [sx, sy - map_view.CARD_HEAD - 10]           # folded, just above Skills
        view.layout(1200, 800)
        self.assertEqual(view.card_rects["skills"][:2], (sx, sy))
        self.assertFalse(self.overlap(view.card_rects["mcp"], view.card_rects["skills"]))
        view.toggle_card("mcp")
        view.layout(1200, 800)
        self.assertFalse(self.overlap(view.card_rects["mcp"], view.card_rects["skills"]))

    def test_opening_a_card_leaves_cards_above_it_alone(self):
        view = Cards(built())
        view.collapsed.add("mcp")
        view.layout(1200, 800)
        mx, my, _mw, _mh = view.card_rects["mcp"]
        view.cards["skills"] = [mx, max(0, my - 300)]
        view.layout(1200, 800)
        before = list(view.cards["skills"])
        view.toggle_card("mcp")
        self.assertEqual(view.cards["skills"], before)

    def test_a_folded_card_has_no_hooks_and_no_drag_on_its_lines(self):
        """Its boxes share the header: a hook there could not say which line it is, so none shows,
        and a press there moves (or a click folds) the card instead."""
        g = built(links=[("agent:pi", "mcp:alpha"), ("agent:pi", "mcp:beta")])
        view = Cards(g)
        view.layout(1200, 800)
        view.selected, view.selected_edge = ["agent:pi"], ("agent:pi", "mcp:alpha")
        view.movable_edge = lambda edge: True
        hooked = M.hooked_edges(view)
        self.assertEqual(len(hooked), 2)
        view.collapsed.add("mcp")
        view.layout(1200, 800)
        self.assertEqual(M.hooked_edges(view), [])
        view.edge_distance = lambda edge, x, y: 0
        self.assertFalse(M.near_selected_edge(view, 0, 0))

    def test_a_folded_card_makes_its_column_shorter(self):
        view = Cards(built())
        tall = view.column_height(0)
        view.collapsed.add("mcp")
        self.assertLess(view.column_height(0), tall)

    def test_default_layout_opens_every_card(self):
        view = Cards(built())
        view.collapsed.add("mcp")
        view.default_layout()
        self.assertEqual(view.collapsed, set())

    def test_a_long_cost_label_widens_its_slot(self):
        view = Cards(built())
        narrow = view.agent_slots()
        view.costs = {"agent:pi": ("$1234.56 today", "", "fg")}
        wide = view.agent_slots()
        self.assertGreater(wide["console"] - wide["meter"], narrow["console"] - narrow["meter"])

    def test_columns_wider_than_the_view_scroll_sideways(self):
        long = built()
        long.add(graph.Node("agent:long", 1, "x" * 80, "", "agent"))
        view = Cards(long)
        view.layout(500, 800)
        self.assertGreater(view.min_width, 500)

    def test_agent_arrows_get_room_when_the_agents_card_comes_first(self):
        view = Cards(built())
        view.layout(1200, 800)
        with_mcp = view.card_rects["agents"][0]
        view.agent_links = {("agent:pi", "agent:claude")}
        view.layout(1200, 800)
        self.assertEqual(view.card_rects["agents"][0], with_mcp)       # the arrows run in the gap
        lone = Cards(built(mcp=()))
        lone.graph.nodes = [n for n in lone.graph.nodes if n.column != 0]
        lone.layout(1200, 800)
        first = lone.card_rects["agents"][0]
        lone.agent_links = {("agent:pi", "agent:claude")}
        lone.layout(1200, 800)
        self.assertGreaterEqual(lone.card_rects["agents"][0] - first, map_view.AGENT_LINK_ROOM / 2)

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


class NarrowColumnsTest(unittest.TestCase):
    """The narrow window: more columns than the view shares scroll sideways, not squeezed."""

    class View(Cards):
        @staticmethod
        def viewport_width():
            return 900

    def test_more_columns_than_the_view_shares_make_the_map_wider(self):
        view = self.View(built())                    # MCP servers · agents · providers: 3 columns
        view.layout(900, 800)
        squeezed = view.boxes["mcp:alpha"][2]
        self.assertLessEqual(view.min_width, 900)    # all in the view
        view.view_columns = 2
        view.layout(900, 800)
        self.assertGreater(view.boxes["mcp:alpha"][2], squeezed)
        self.assertGreater(view.min_width, 900)      # wider than the view: it scrolls sideways
        two = self.View(built(mcp=()))
        two.shown = lambda column: [] if column == 0 else two.graph.column(column)
        two.view_columns = 2
        two.layout(900, 800)
        # The providers' boxes are as wide with the MCP column shown as with only the two.
        self.assertEqual(two.boxes["cloud:anthropic"][2], view.boxes["cloud:anthropic"][2])


if __name__ == "__main__":
    unittest.main()
