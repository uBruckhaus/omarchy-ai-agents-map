"""Add MCP server: Test is on only for a server picked from the list (By hand: a filled-in form)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import gi  # noqa: E402
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402
import mcp_pages  # noqa: E402


class FormOnly:
    update_add_test = mcp_pages.McpPages.update_add_test

    def __init__(self, tab, pages=(), form_ok=True):
        self.add_tab, self.add_pages, self.form_ok = tab, dict.fromkeys(pages, (None, None)), form_ok
        self.add_test = Gtk.Button()

    def new_mcp_entry(self):
        if not self.form_ok:
            raise ValueError("No command")
        return {"command": "x"}


class TestButtonTest(unittest.TestCase):
    def test_off_until_a_server_is_picked(self):
        for tab in ("picks", "web", "local"):
            with self.subTest(tab=tab):
                view = FormOnly(tab)
                view.update_add_test()
                self.assertFalse(view.add_test.get_sensitive())
                self.assertEqual(view.add_test.get_tooltip_text(), "Pick a server from the list first")
                view.add_pages[tab] = (None, None)
                view.update_add_test()
                self.assertTrue(view.add_test.get_sensitive())

    def test_by_hand_needs_a_filled_form(self):
        view = FormOnly("hand", form_ok=False)
        view.update_add_test()
        self.assertFalse(view.add_test.get_sensitive())
        self.assertEqual(view.add_test.get_tooltip_text(), "No command")
        view.form_ok = True
        view.update_add_test()
        self.assertTrue(view.add_test.get_sensitive())

    def test_picked_on_another_tab_does_not_count(self):
        view = FormOnly("web", pages=("picks",))
        view.update_add_test()
        self.assertFalse(view.add_test.get_sensitive())


if __name__ == "__main__":
    unittest.main()
