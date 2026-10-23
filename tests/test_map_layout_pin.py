"""The pin below the map's zoom: the saved layout is used only while it is pinned."""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ai_core  # noqa: E402
import ai_popup  # noqa: E402

M = ai_popup.AgentsMap

LAYOUT = {"order": {"agents": ["agent:pi", "agent:claude"]}, "cards": {"mcp": [10, 20]}, "collapsed": ["local"]}


def settings(**stored):
    return lambda key: stored.get(key, {} if key == "map_layout" else None)


class LayoutPinTest(unittest.TestCase):
    def view(self, **stored):
        app = SimpleNamespace(side_map=SimpleNamespace(order={}, cards={}, collapsed=set()))
        with mock.patch.object(ai_core, "setting", side_effect=settings(**stored)):
            pinned = M.layout_pinned(app)
            app.layout_pinned = lambda: pinned
            M.load_map_layout(app)
        return pinned, app.side_map

    def test_pinned_restores_order_places_and_folded_cards(self):
        pinned, view = self.view(map_layout=LAYOUT, map_layout_pinned=True)
        self.assertTrue(pinned)
        self.assertEqual(view.cards, {"mcp": [10.0, 20.0]})
        self.assertEqual(view.collapsed, {"local"})
        self.assertEqual(view.order["agents"], ["agent:pi", "agent:claude"])

    def test_unpinned_starts_in_the_default_layout(self):
        pinned, view = self.view(map_layout=LAYOUT, map_layout_pinned=False)
        self.assertFalse(pinned)
        self.assertEqual((view.cards, view.collapsed, view.order), ({}, set(), {}))

    def test_unpinned_by_default_also_with_a_saved_layout(self):
        pinned, view = self.view(map_layout=LAYOUT)
        self.assertFalse(pinned)
        self.assertEqual((view.cards, view.collapsed, view.order), ({}, set(), {}))
        self.assertFalse(self.view()[0])

    def test_pinning_is_saved_so_it_stays_pinned(self):
        saved = {}
        app = SimpleNamespace(layout_pinned=lambda: saved.get("map_layout_pinned", False),
                              save_setting=lambda **v: saved.update(v), save_map_layout=mock.Mock(),
                              show_layout_pin=mock.Mock())
        M.toggle_layout_pin(app)
        self.assertEqual(saved, {"map_layout_pinned": True})        # read back on the next start
        app.save_map_layout.assert_called_once()                    # the layout on the map is the one kept
        M.toggle_layout_pin(app)
        self.assertEqual(saved, {"map_layout_pinned": False})

    def test_hostile_folded_card_lists_are_bounded(self):
        hostile = {"collapsed": [1, None, "", "x" * 600, {"a": 1}, "ok"] + [f"k{i}" for i in range(500)]}
        _pinned, view = self.view(map_layout=hostile, map_layout_pinned=True)
        self.assertIn("ok", view.collapsed)
        self.assertTrue(all(isinstance(k, str) and 0 < len(k) <= 500 for k in view.collapsed))
        self.assertLessEqual(len(view.collapsed), 100)
        _pinned, view = self.view(map_layout={"collapsed": "local"}, map_layout_pinned=True)
        self.assertEqual(view.collapsed, set())                     # not a list: nothing folded

    def test_refresh_resets_only_when_unpinned(self):
        for pinned, resets in ((True, 0), (False, 1)):
            side = mock.Mock()
            app = SimpleNamespace(side_map=side, layout_pinned=lambda p=pinned: p)
            M.reset_map_layout_unless_pinned(app)
            self.assertEqual(side.default_layout.call_count, resets)

    def test_refresh_itself_resets_only_when_unpinned(self):
        """Through the header's Refresh (reload_setup), not only the helper: unpinned it resets the
        layout before the map is drawn again, pinned it keeps it."""
        import setup_page
        for pinned, resets in ((True, 0), (False, 1)):
            app = mock.MagicMock()
            app.layout_pinned.return_value = pinned
            app.reset_map_layout_unless_pinned = lambda a=app: M.reset_map_layout_unless_pinned(a)
            app.with_addons.return_value = False
            setup_page.SetupPage.reload_setup(app)
            self.assertEqual(app.side_map.default_layout.call_count, resets)
            app.refresh_map.assert_called()


if __name__ == "__main__":
    unittest.main()
