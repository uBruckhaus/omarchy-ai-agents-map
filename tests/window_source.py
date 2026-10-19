"""The window's source for tests that read it: ai_popup.py and every module AgentsMap takes a base
class (a mixin) from, found from the class itself, so code moved into a new mixin is still read."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def window_modules() -> list[Path]:
    tree = ast.parse((ROOT / "ai_popup.py").read_text())
    window = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "AgentsMap")
    bases = {b.id for b in window.bases if isinstance(b, ast.Name)}
    found = [ROOT / "ai_popup.py"]
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and any(a.name in bases for a in node.names):
            path = ROOT / f"{node.module}.py"
            if path.is_file() and path not in found:      # addons_page.py: Asterism only
                found.append(path)
    return found


def window_methods():
    """Every method of the window's classes: (module path, ast.FunctionDef)."""
    for path in window_modules():
        for cls in (n for n in ast.parse(path.read_text()).body if isinstance(n, ast.ClassDef)):
            for method in (n for n in cls.body if isinstance(n, ast.FunctionDef)):
                yield path, method
