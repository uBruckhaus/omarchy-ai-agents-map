"""The window class (ai_popup.AgentsMap) is put together from many parts (mixins). A method name
defined in two of them silently hides one: the settings page once called the layout kit's
button_row instead of its own and did not open. Every name only once."""

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def methods(cls: ast.ClassDef) -> list:
    return [n.name for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]


class WindowNamesTest(unittest.TestCase):
    def test_no_method_in_two_parts(self):
        tree = ast.parse((ROOT / "ai_popup.py").read_text())
        window = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "AgentsMap")
        bases = {b.id for b in window.bases if isinstance(b, ast.Name)}
        where = {name: ["AgentsMap"] for name in methods(window)}
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.module and (ROOT / f"{node.module}.py").is_file():
                for alias in node.names:
                    if alias.name in bases:
                        part = ast.parse((ROOT / f"{node.module}.py").read_text())
                        cls = next(n for n in part.body if isinstance(n, ast.ClassDef) and n.name == alias.name)
                        for name in methods(cls):
                            where.setdefault(name, []).append(alias.name)
        twice = {name: parts for name, parts in where.items() if len(parts) > 1}
        self.assertEqual(twice, {})


if __name__ == "__main__":
    unittest.main()
