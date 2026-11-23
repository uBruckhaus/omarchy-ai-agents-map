"""Settings › Keyboard shortcuts: a section of its own, one group per part of the window, every key
with what it does; and every key listed is one the window really handles."""

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import gi  # noqa: E402
gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gdk, Gtk  # noqa: E402,F401  (Gtk opens the display)

import settings_pages  # noqa: E402

S = settings_pages.SettingsPages


class TableTest(unittest.TestCase):
    def test_a_section_of_its_own_before_about(self):
        keys = [key for key, _name in S.SECTIONS]
        self.assertIn("shortcuts", keys)
        self.assertEqual(keys[-2:], ["shortcuts", "about"])
        for title, _keys in S.SHORTCUTS:
            self.assertEqual(S.SECTION_OF[title], "shortcuts", title)
        self.assertEqual(S.SECTION_OF["Keyboard shortcuts"], "shortcuts")

    def test_every_key_has_a_description_and_is_handled(self):
        """Each listed key appears in the handler of its part (no shortcut promised that does nothing)."""
        sources = {name: (ROOT / name).read_text() for name in
                   ("ai_popup.py", "projects_page.py", "askai_page.py", "terminal_pane.py", "map_view.py")}
        handled = {
            "Ctrl+1": ("ai_popup.py", "Gdk.KEY_1"), "Ctrl+2": ("ai_popup.py", "Gdk.KEY_2"),
            "Ctrl+,": ("ai_popup.py", "Gdk.KEY_comma"), "F1": ("ai_popup.py", "Gdk.KEY_F1"),
            "F5": ("ai_popup.py", "Gdk.KEY_F5"), "F12": ("ai_popup.py", "Gdk.KEY_F12"),
            "Esc": ("ai_popup.py", "Gdk.KEY_Escape"), "Backspace": ("ai_popup.py", "Gdk.KEY_BackSpace"),
            "Ctrl++": ("ai_popup.py", "Gdk.KEY_plus"), "Ctrl+−": ("ai_popup.py", "Gdk.KEY_minus"),
            "Ctrl+0": ("ai_popup.py", "Gdk.KEY_0"), "Shift+click": ("map_view.py", "shift"),
            "Ctrl+↑ / Ctrl+↓": ("projects_page.py", "Gdk.KEY_Up"),
            "Ctrl+Home / Ctrl+End": ("projects_page.py", "Gdk.KEY_Home"),
            "Enter": ("askai_page.py", "Gdk.KEY_Return"), "Ctrl+Enter": ("askai_page.py", "CONTROL_MASK"),
            "Ctrl+Shift+C": ("terminal_pane.py", "Gdk.KEY_c"), "Ctrl+Shift+V": ("terminal_pane.py", "Gdk.KEY_v"),
            "Shift+Insert": ("terminal_pane.py", "Gdk.KEY_Insert"),
        }
        listed = [combo for _title, keys in S.SHORTCUTS for combo, _what in keys]
        self.assertEqual(sorted(set(listed)), sorted(handled))
        for _title, keys in S.SHORTCUTS:
            for combo, what in keys:
                self.assertTrue(what and not what.endswith("."), combo)
                source, mark = handled[combo]
                self.assertIn(mark, sources[source], combo)


@unittest.skipIf(Gdk.Display.get_default() is None, "real GTK widgets need a display (without one GTK crashes)")
class SectionTest(unittest.TestCase):
    def test_the_groups_and_rows(self):
        from gi.repository import Adw
        Adw.init()

        class Host:
            fill_shortcut_settings = S.fill_shortcut_settings
            SHORTCUTS = S.SHORTCUTS

            def __init__(self):
                self.groups = []

            def preference_group(self, _box, title):
                group = Adw.PreferencesGroup(title=title)
                self.groups.append(group)
                return group

        host = Host()
        host.fill_shortcut_settings(Gtk.Box())
        titles = [g.get_title() for g in host.groups]
        self.assertEqual(titles, ["Keyboard shortcuts", "Window", "AI Map", "Projects", "Ask AI", "Terminal"])
        self.assertIn("Omarchy", host.groups[0].get_description())

        def rows(group):
            found, stack = [], [group]
            while stack:
                widget = stack.pop()
                if isinstance(widget, Adw.ActionRow):
                    found.append(widget)
                child = widget.get_first_child()
                while child is not None:
                    stack.append(child)
                    child = child.get_next_sibling()
            return found

        def labels(widget):
            found, stack = [], [widget]
            while stack:
                item = stack.pop()
                if isinstance(item, Gtk.Label):
                    found.append(item.get_label())
                child = item.get_first_child()
                while child is not None:
                    stack.append(child)
                    child = child.get_next_sibling()
            return found

        window = rows(host.groups[1])
        self.assertEqual(len(window), 8)
        first = next(r for r in window if r.get_title() == "Projects tab")
        self.assertIn("Ctrl+1", labels(first))
        zoom = rows(host.groups[2])
        self.assertTrue(any(r.get_title() == "Back to 100 %" and "Ctrl+0" in labels(r) for r in zoom))
        self.assertFalse(re.search(r"Move up|Move down", " ".join(r.get_title() for g in host.groups for r in rows(g))))


if __name__ == "__main__":
    unittest.main()
