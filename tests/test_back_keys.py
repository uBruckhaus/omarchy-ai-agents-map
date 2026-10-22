"""Esc and Backspace on a page with a Back button: back, unless a field on it is being edited."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import gi  # noqa: E402
gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gtk  # noqa: E402
import ai_popup  # noqa: E402


class Window:
    def __init__(self, focus):
        self.focus = focus

    def get_focus(self):
        return self.focus


class Field:
    """Stands in for a focused Gtk.Text (Gtk.Editable)."""
    def get_editable(self):
        return True


class KeysOnly:
    on_key = ai_popup.AgentsMap.on_key
    back_actions = ai_popup.AgentsMap.back_actions

    def __init__(self, page, focus=None):
        self.page, self.window, self.selection, self.closed = page, Window(focus), [], []
        for name in ("close_help", "close_info", "close_addons", "close_reorganize", "close_settings",
                     "close_connect", "close_install", "close_models", "close_mcp_details", "close_status",
                     "close_health", "close_traffic", "close_smart_check", "close_add_mcp", "close_add_skills",
                     "close_setup", "close_subagents"):
            setattr(self, name, lambda name=name: self.closed.append(name))

    def page_name(self):
        return self.page

    def editing(self):
        return isinstance(self.window.focus, Field)


class BackKeysTest(unittest.TestCase):
    def test_every_back_page_goes_back_with_both_keys(self):
        for page in KeysOnly("main").back_actions():
            for key in (Gdk.KEY_Escape, Gdk.KEY_BackSpace):
                with self.subTest(page=page, key=key):
                    view = KeysOnly(page)
                    self.assertTrue(view.on_key(None, key, 0, 0))
                    self.assertEqual(len(view.closed), 1)

    def test_keys_stay_in_a_field_being_edited(self):
        for page in ("models", "addmcp", "settings", "addons"):
            for key in (Gdk.KEY_Escape, Gdk.KEY_BackSpace):
                with self.subTest(page=page, key=key):
                    view = KeysOnly(page, Field())
                    self.assertFalse(view.on_key(None, key, 0, 0))
                    self.assertEqual(view.closed, [])

    def test_terminal_keeps_its_keys(self):
        view = KeysOnly("terminal")
        self.assertFalse(view.on_key(None, Gdk.KEY_Escape, 0, 0))
        self.assertEqual(view.closed, [])

    def test_editing_sees_real_fields(self):
        self.assertTrue(ai_popup.AgentsMap.editing(type("V", (), {"window": Window(Gtk.Text())})()))
        self.assertFalse(ai_popup.AgentsMap.editing(type("V", (), {"window": Window(Gtk.Button())})()))
        self.assertFalse(ai_popup.AgentsMap.editing(type("V", (), {"window": Window(None)})()))


if __name__ == "__main__":
    unittest.main()
