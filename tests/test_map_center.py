import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import map_view  # noqa: E402

H, P = map_view.HEADER_H, map_view.PAD


class Adjustment:
    def __init__(self, value, page):
        self.value, self.page = value, page

    def get_value(self):
        return self.value

    def get_page_size(self):
        return self.page


class Centring:
    """MapView's column placement without the widget."""
    visible_rows = map_view.MapView.visible_rows
    column_top = map_view.MapView.column_top

    def __init__(self, value, page, zoom=1.0):
        self.scroll_adjustment = Adjustment(value, page)
        self.zoom = zoom


class ColumnTopTest(unittest.TestCase):
    def test_short_column_sits_in_the_middle_of_the_view(self):
        view = Centring(0, 600)
        self.assertEqual(view.column_top(100, 2000), H + (600 - P - H - 100) / 2)

    def test_it_follows_scrolling(self):
        view = Centring(800, 600)
        self.assertEqual(view.column_top(100, 2000), 800 + P + (600 - 2 * P - 100) / 2)

    def test_never_below_the_map(self):
        view = Centring(1800, 600)       # scrolled past the end: stays inside the content
        self.assertEqual(view.column_top(300, 2000), 2000 - P - 300)

    def test_tall_column_starts_under_its_heading(self):
        self.assertEqual(Centring(800, 600).column_top(900, 2000), H)

    def test_zoom(self):
        view = Centring(800, 600, zoom=2.0)
        self.assertEqual(view.column_top(100, 2000), 400 + P + (300 - 2 * P - 100) / 2)


    def test_tall_agents_column_follows_the_view(self):
        self.assertEqual(Centring(0, 600).column_top(800, 2000), H)
        self.assertEqual(Centring(0, 600).column_top(800, 2000, hub=True), H)      # top: under the heading
        middle = Centring(700, 600).column_top(800, 2000, hub=True)                 # scrolled: centred on the view
        self.assertAlmostEqual(middle + 400, 700 + 300, delta=1)
        self.assertEqual(Centring(1400, 600).column_top(800, 2000, hub=True), 2000 - P - 800)   # end: inside the map


if __name__ == "__main__":
    unittest.main()
