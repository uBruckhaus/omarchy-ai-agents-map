import ast
import unittest
from pathlib import Path

from window_source import window_methods

ROOT = Path(__file__).resolve().parent.parent
# Everything that runs an AppImage (its own --appimage-extract).
RUNNERS = {"look_inside", "unpack_bin", "unpack", "unpack_appimage"}
# The only window methods that may reach them: each runs after the user's click.
CLICKED = {
    "look_inside_appimages",   # + MCP server › Look inside…
    "check_mcp_updates",       # + MCP server › On this PC › Check for updates
    "test_new_mcp",            # Test on the add page
    "apply_new_mcp",           # Add on the add page (a server that lives in an AppImage)
    "apply_connect",           # Update… of an app's MCP entries, after its preview
}


class NoRunOnOpenTest(unittest.TestCase):
    """Opening the window (or any background check) never runs an AppImage:
    a newer file next to a known app is only named (mcp_scan.new_versions_found)."""

    def test_only_click_handlers_run_appimages(self):
        reaching = set()
        for _path, method in window_methods():     # ai_popup.py and every mixin of the window
            for node in ast.walk(method):          # nested work()/done() included
                if isinstance(node, ast.Call) and getattr(node.func, "attr", "") in RUNNERS:
                    reaching.add(method.name)
        self.assertEqual(reaching, CLICKED)

    def test_check_new_apps_only_lists(self):
        method = next(m for _path, m in window_methods() if m.name == "check_new_apps")
        called = {getattr(n.func, "attr", "") for n in ast.walk(method) if isinstance(n, ast.Call)}
        self.assertIn("new_versions_found", called)
        self.assertFalse(called & RUNNERS)


if __name__ == "__main__":
    unittest.main()
