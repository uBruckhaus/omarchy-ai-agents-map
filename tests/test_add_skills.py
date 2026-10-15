import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import connect  # noqa: E402
import corners  # noqa: E402
import skills_scan  # noqa: E402
import skills_web  # noqa: E402

TREE = {"tree": [
    {"path": "README.md", "type": "blob", "mode": "100644", "size": 10},
    {"path": "skills/pdf/SKILL.md", "type": "blob", "mode": "100644", "size": 100},
    {"path": "skills/pdf/scripts/fill.py", "type": "blob", "mode": "100755", "size": 50},
    {"path": "skills/pdf/link", "type": "blob", "mode": "120000", "size": 5},
    {"path": "skills/pdf/sub", "type": "commit", "mode": "160000"},
    {"path": "skills/pdf/../../evil", "type": "blob", "mode": "100644", "size": 5},
    {"path": "skills/docx/SKILL.md", "type": "blob", "mode": "100644", "size": 100},
]}


class WebTest(unittest.TestCase):
    def test_files_of_a_skill(self):
        folder, files = skills_web.files_of(TREE, "pdf")
        self.assertEqual(folder, "skills/pdf")
        self.assertEqual([(f[0], f[3]) for f in files], [("SKILL.md", False), ("scripts/fill.py", True)])
        with self.assertRaises(ValueError):
            skills_web.files_of(TREE, "xlsx")
        big = {"tree": [{"path": "a/SKILL.md", "type": "blob", "mode": "100644", "size": 100},
                        {"path": "a/model.bin", "type": "blob", "mode": "100644", "size": 10 ** 9}]}
        with self.assertRaises(ValueError):
            skills_web.files_of(big, "a")

    def test_search_results(self):
        found = skills_web.parse_search({"skills": [
            {"source": "anthropics/skills", "skillId": "pdf", "name": "pdf", "installs": 5},
            {"source": "../etc", "skillId": "x"}, {"source": "a/b", "skillId": "bad id"}]})
        self.assertEqual([(s.repo, s.skill_id, s.installs) for s in found], [("anthropics/skills", "pdf", 5)])


class ScanTest(unittest.TestCase):
    def test_skill_sets_from_apps(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for path in ("app/share/omaraw/skills/edit", "app/share/omaraw/skills/export",
                         "app/share/known/skills/one", "app/share/icons/skills/x"):
                (root / path).mkdir(parents=True)
                (root / path / "SKILL.md").write_text("x")
            with mock.patch.object(skills_scan, "ROOTS", [(root / "app", 4)]):
                found = skills_scan.scan(known=[str(root / "app/share/known/skills")])
        self.assertEqual([(s.origin, s.skills) for s in found], [("omaraw", ["edit", "export"])])


class FilesTest(unittest.TestCase):
    def test_new_files_and_undo(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            target = home / ".agents/skills/pdf"
            plan = connect.Plan("", "", "pdf")
            plan.files_add = [(target / "SKILL.md", b"---\nname: pdf\n---\n", False),
                              (target / "scripts/fill.py", b"print(1)\n", True)]
            self.assertIn("SKILL.md", plan.diff())
            backup = connect.apply(plan, home / "backups")
            self.assertTrue((target / "scripts/fill.py").stat().st_mode & 0o100)
            (target / "SKILL.md").write_text("changed by hand")
            connect.undo(backup)
            self.assertFalse((target / "scripts").exists())
            self.assertTrue((target / "SKILL.md").exists())      # changed since: kept
            with self.assertRaises(RuntimeError):
                connect.apply(plan, home / "backups")             # exists already


class CornersTest(unittest.TestCase):
    def test_radius(self):
        self.assertEqual(corners.radius("windows", 8), 8)
        self.assertEqual(corners.radius("rounded", 0), corners.ROUNDED)
        self.assertEqual(corners.radius("square", 8), 0)
        self.assertEqual(corners.parse_rounding('{"option": "decoration:rounding", "int": 10}'), 10)


if __name__ == "__main__":
    unittest.main()
