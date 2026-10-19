import ast
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import isolate  # noqa: E402,F401
import mcp_tools  # noqa: E402
import skill_update  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SKILL = "---\nname: photocraft\ndescription: Edit photos through the PhotoCraft MCP server.\n---\n\n# Use doc_open\n"
TOOLS = {"status": "ok", "server": "photocraft 0.10.0",
         "tools": [{"name": "doc_open", "description": "Open a file", "kind": "tool", "params": ["path"]},
                   {"name": "doc_place", "description": "Place a file", "kind": "tool", "params": ["path", "x"]}],
         "prompts": []}


class SkillUpdateTest(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.home, ignore_errors=True))
        for target, value in ((skill_update, "HOME"), (skill_update, "WORK_DIR"), (skill_update, "STATE_FILE")):
            path = {"HOME": self.home, "WORK_DIR": self.home / ".cache/work",
                    "STATE_FILE": self.home / "state.json"}[value]
            patcher = mock.patch.object(target, value, path)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.skills = self.home / ".agents/skills"
        (self.skills / "photocraft").mkdir(parents=True)
        (self.skills / "photocraft/SKILL.md").write_text(SKILL)
        (self.skills / "other").mkdir()
        (self.skills / "other/SKILL.md").write_text("---\nname: other\n---\nCall mcp__gridcraft__set_cell.\n")
        (self.skills / "plain").mkdir()
        (self.skills / "plain/SKILL.md").write_text("---\nname: plain\n---\nNothing about mcp__gridcraftish.\n")

    def test_based_on_by_name_or_tool_calls(self):
        folders = sorted(self.skills.iterdir())
        found = skill_update.based_on(folders, ["photocraft", "gridcraft", "bad name"])
        self.assertEqual(found["photocraft"], [self.skills / "photocraft"])
        self.assertEqual(found["gridcraft"], [self.skills / "other"])
        self.assertNotIn("bad name", found)

    def test_own_skill_file(self):
        link_dir = self.home / ".claude/skills"
        link_dir.mkdir(parents=True)
        (link_dir / "photocraft").symlink_to(self.skills / "photocraft")
        self.assertEqual(skill_update.own_skill_file(link_dir / "photocraft"),
                         (self.skills / "photocraft/SKILL.md").resolve())
        # outside the home folder (an app's /usr/share/<app>/skills): not offered
        outside = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(outside, ignore_errors=True))
        (outside / "s").mkdir()
        (outside / "s/SKILL.md").write_text(SKILL)
        (self.skills / "linked").symlink_to(outside / "s")
        self.assertIsNone(skill_update.own_skill_file(self.skills / "linked"))
        # a hard link to someone else's file could be rewritten in place: not offered
        os.link(self.skills / "photocraft/SKILL.md", self.home / "copy.md")
        self.assertIsNone(skill_update.own_skill_file(self.skills / "photocraft"))

    def test_status_follows_the_tools(self):
        file = self.skills / "photocraft/SKILL.md"
        self.assertEqual(skill_update.status(file, "photocraft", TOOLS), "unknown")
        skill_update.remember(file, "photocraft", TOOLS)
        self.assertEqual(skill_update.status(file, "photocraft", TOOLS), "current")
        changed = {**TOOLS, "tools": TOOLS["tools"][:1]}
        self.assertEqual(skill_update.status(file, "photocraft", changed), "changed")
        self.assertEqual(skill_update.status(file, "photocraft", None), "unknown")
        self.assertEqual(oct(skill_update.STATE_FILE.stat().st_mode & 0o777), "0o600")

    def test_check(self):
        self.assertEqual(skill_update.check("```markdown\n" + SKILL + "```", "photocraft"), SKILL)
        for bad in ("Here is the skill:\n" + SKILL, SKILL.replace("name: photocraft", "name: evil"),
                    SKILL + "x" * (skill_update.MAX_SKILL + 1), SKILL + "\x00"):
            with self.assertRaises(ValueError):
                skill_update.check(bad, "photocraft")

    def test_rewrite_has_no_tools_and_prompt_on_stdin(self):
        calls = []

        def run(argv, **kwargs):
            calls.append((argv, kwargs))
            return (SKILL.replace("doc_open", "doc_open and doc_place")).encode(), True

        with mock.patch.object(skill_update.shutil, "which", lambda c: f"/usr/bin/{c}"):
            new, writer = skill_update.rewrite(self.skills / "photocraft/SKILL.md", "photocraft", TOOLS,
                                               ["claude", "codex"], run=run)
        self.assertEqual(writer, "claude")
        self.assertIn("doc_place", new)
        argv, kwargs = calls[0]
        self.assertEqual(argv[argv.index("--tools") + 1], "")
        self.assertIn("--strict-mcp-config", argv)
        self.assertFalse(any("SKILL" in a or "doc_open" in a for a in argv))      # the skill only on stdin
        self.assertIn(b"doc_place(path, x)", kwargs["stdin"])

    def test_rewrite_falls_back_and_reports(self):
        answers = iter([(b"sorry", True), (b"", False)])
        with mock.patch.object(skill_update.shutil, "which", lambda c: f"/usr/bin/{c}"):
            with self.assertRaises(RuntimeError) as caught:
                skill_update.rewrite(self.skills / "photocraft/SKILL.md", "photocraft", TOOLS, ["claude", "codex"],
                                     run=lambda argv, **kw: next(answers))
        self.assertIn("claude:", str(caught.exception))
        self.assertIn("codex:", str(caught.exception))
        with self.assertRaises(RuntimeError):
            skill_update.rewrite(self.skills / "photocraft/SKILL.md", "photocraft", {"status": "failed"}, ["claude"])

    def test_tool_params_kept_short(self):
        items = [{"name": "t", "description": "d", "inputSchema": {"properties": {"path": {}, "x y": {}, "a" * 99: {}}}}]
        self.assertEqual(mcp_tools._clean(items, "tool")[0]["params"], ["path", "a" * 64])
        prompts = [{"name": "p", "arguments": [{"name": "topic"}, {"bad": 1}]}]
        self.assertEqual(mcp_tools._clean(prompts, "prompt")[0]["params"], ["topic"])

    def test_rewrite_only_after_a_click(self):
        tree = ast.parse((ROOT / "ai_popup.py").read_text())
        reaching = set()
        for cls in (n for n in tree.body if isinstance(n, ast.ClassDef)):
            for method in (n for n in cls.body if isinstance(n, ast.FunctionDef)):
                if any(isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "rewrite" for n in ast.walk(method)):
                    reaching.add(method.name)
        self.assertEqual(reaching, {"update_skill"})       # MCP server page › Update skill…


if __name__ == "__main__":
    unittest.main()
