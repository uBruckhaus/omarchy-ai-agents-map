"""Every global name a function uses is defined in its module (imported, assigned, def or class) or
is a builtin: a forgotten import after moving code between modules (ai_popup.py and its mixins)
fails here instead of as a NameError when somebody opens that page."""

import builtins
import symtable
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Names a module gets in other ways than an assignment the symbol table sees.
ALLOWED = {"__file__", "__name__", "__doc__", "__spec__", "__builtins__", "__class__", "__annotations__"}


def module_names(table: symtable.SymbolTable) -> set:
    """Assigned or imported at module level, or assigned by a function that declares it global
    (agents.py fills SPECS and the others from the adapters that way)."""
    names = {s.get_name() for s in table.get_symbols() if s.is_assigned() or s.is_imported()}
    stack = list(table.get_children())
    while stack:
        child = stack.pop()
        names |= {s.get_name() for s in child.get_symbols() if s.is_declared_global() and s.is_assigned()}
        stack.extend(child.get_children())
    return names


def used_globals(table: symtable.SymbolTable):
    """(scope name, global name) for every global a function, lambda, comprehension or class body
    reads (a class body runs on import: N_() in a class constant fails before any page opens)."""
    for child in table.get_children():
        kind = str(child.get_type()).lower()
        for symbol in child.get_symbols():
            if not symbol.is_referenced():
                continue
            if "function" in kind and symbol.is_global():
                yield child.get_name(), symbol.get_name()
            elif "class" in kind and not (symbol.is_assigned() or symbol.is_imported()):
                yield child.get_name(), symbol.get_name()
        yield from used_globals(child)


class ModuleNamesTest(unittest.TestCase):
    def test_no_undefined_globals(self):
        known = set(dir(builtins)) | ALLOWED
        problems = []
        for path in sorted(ROOT.glob("*.py")) + sorted(ROOT.glob("adapters/*.py")):     # the hook files too
            table = symtable.symtable(path.read_text(), str(path), "exec")
            defined = module_names(table) | known
            for function, name in used_globals(table):
                if name not in defined:
                    problems.append(f"{path.name}: {function}() uses {name}, which the module does not define")
        self.assertEqual(problems, [], "\n" + "\n".join(problems))


if __name__ == "__main__":
    unittest.main()
