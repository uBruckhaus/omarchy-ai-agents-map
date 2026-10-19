import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import icons  # noqa: E402

GOOD = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16"><path d="M0 0h16v16z"/></svg>'


class StoredIconsTest(unittest.TestCase):
    """The app brings its own icons: every glyph the cards and the map use is stored as an SVG."""

    def test_the_glyphs_in_use_are_stored(self):
        import glyphs
        from map_view import CONSOLE, PIN, PIN_OUTLINE, PLAY
        for char in list(glyphs.GLYPHS.values()) + [PIN, PIN_OUTLINE, PLAY, CONSOLE, "\U000F0493"]:
            name = icons.name_for(char)
            self.assertTrue(name and icons.path_for(name), f"U+{ord(char):X} has no stored icon")

    def test_the_ui_icons_are_in_the_apps_theme(self):
        for name in ("view-refresh-symbolic", "go-previous-symbolic", "am-chart-symbolic", "pan-end-symbolic"):
            self.assertTrue((icons.ACTIONS / f"{name}.svg").is_file(), name)
        self.assertIn("Inherits=Adwaita", (icons.THEME_DIR / "index.theme").read_text())

    def test_text_without_a_stored_icon(self):
        self.assertEqual(icons.name_for("ab"), "")
        self.assertEqual(icons.name_for("x"), "")


class AddonIconTest(unittest.TestCase):
    """An add-on's icon.svg: shapes only."""

    def test_shapes_are_fine(self):
        self.assertTrue(icons.safe_svg(GOOD))
        self.assertTrue(icons.safe_svg(b'<svg><defs><linearGradient id="a"/></defs>'
                                       b'<rect fill="url(#a)" width="4" height="4"/></svg>'))

    def test_anything_that_runs_or_loads_is_refused(self):
        for bad in (b'<svg><script>alert(1)</script></svg>', b'<svg><image href="file:///etc/passwd"/></svg>',
                    b'<svg><use href="http://x/a.svg#b"/></svg>', b'<svg><path onclick="x()" d="M0 0"/></svg>',
                    b'<svg><style>@import url(http://x)</style></svg>', b'<svg><rect fill="url(http://x)"/></svg>',
                    b'<!DOCTYPE svg [<!ENTITY x SYSTEM "file:///etc/passwd">]><svg>&x;</svg>',
                    b'<svg><foreignObject/></svg>', b"", b"<svg>" + b" " * icons.MAX_SVG + b"</svg>", b"\xff\xfe"):
            self.assertFalse(icons.safe_svg(bad), bad[:40])

    def test_it_is_kept_under_its_own_name(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(icons, "ADDON_DIR", Path(tmp)):
            name = icons.add_addon_icon("agent-x", GOOD)
            self.assertEqual(name, "am-addon-agent-x-symbolic")
            self.assertEqual(icons.name_for(name, "icon"), name)
            self.assertEqual(icons.add_addon_icon("../evil", GOOD), "")
            self.assertEqual(icons.add_addon_icon("agent-y", b"<svg><script/></svg>"), "")


if __name__ == "__main__":
    unittest.main()
