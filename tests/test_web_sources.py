import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ai_core  # noqa: E402
import models_ctl  # noqa: E402
import skills_web  # noqa: E402
import web_sources as w  # noqa: E402


class SourcesTest(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(w.normalize("models", "hf-mirror.com/"), "https://hf-mirror.com")
        self.assertEqual(w.normalize("mcp", "https://reg.example.org/v0/servers"), "https://reg.example.org")
        self.assertEqual(w.normalize("skills", "https://github.com/anthropics/skills.git"),
                         "https://github.com/anthropics/skills")
        for kind, bad in (("models", "http://x.org"), ("models", "https://u:p@x.org"), ("mcp", "https://x.org/?q=1"),
                          ("models", "https://github.com/a/b"), ("skills", "https://x.org/a b")):
            with self.assertRaises(ValueError, msg=bad):
                w.normalize(kind, bad)
        self.assertEqual(w.github_repo("https://github.com/anthropics/skills"), "anthropics/skills")
        self.assertEqual(w.label("https://huggingface.co"), "Hugging Face")

    def test_added_sites_are_kept(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(ai_core, "settings_file", return_value=Path(tmp) / "settings.json"):
            self.assertEqual(w.sources("models"), ["https://huggingface.co"])
            w.add("models", "hf-mirror.com")
            self.assertEqual(w.sources("models"), ["https://huggingface.co", "https://hf-mirror.com"])
            self.assertEqual(w.chosen("models"), "https://hf-mirror.com")          # the one added last is picked
            w.choose("models", "https://huggingface.co")
            self.assertEqual(w.chosen("models"), "https://huggingface.co")
            w.remove("models", "https://hf-mirror.com")
            self.assertEqual(w.sources("models"), ["https://huggingface.co"])
            self.assertEqual(w.sources("mcp"), ["https://registry.modelcontextprotocol.io"])

    def test_skills_of_a_repository(self):
        tree = {"tree": [{"path": "skills/pdf/SKILL.md", "type": "blob"}, {"path": "skills/pdf/x.py", "type": "blob"},
                         {"path": "docx/SKILL.md", "type": "blob"}, {"path": "bad name/SKILL.md", "type": "blob"}]}
        self.assertEqual([s.skill_id for s in skills_web.skills_in_tree(tree, "a/b")], ["pdf", "docx"])
        self.assertEqual([s.skill_id for s in skills_web.skills_in_tree(tree, "a/b", "pdf")], ["pdf"])

    def test_skills_site_must_be_the_added_one(self):
        with self.assertRaises(ValueError):
            skills_web._get("https://evil.example/api/search", 10)


class LocalModelsTest(unittest.TestCase):
    def test_found_and_added_without_a_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(os.path.realpath(tmp))
            other, mine = tmp / "Downloads", tmp / "llama"
            other.mkdir()
            mine.mkdir()
            (other / "a.gguf").write_bytes(b"a")
            (other / "mmproj-a.gguf").write_bytes(b"m")
            (other / "s-00001-of-00002.gguf").write_bytes(b"1")
            (other / "s-00002-of-00002.gguf").write_bytes(b"2")
            found = models_ctl.local_ggufs([other, mine], models_ctl.seen_by("llamacpp", mine))
            self.assertEqual([f.path.name for f in found], ["a.gguf", "s-00001-of-00002.gguf"])
            models_ctl.add_local("llamacpp", mine, other / "s-00001-of-00002.gguf")
            self.assertTrue((mine / "s-00002-of-00002.gguf").exists())           # every part
            self.assertEqual([f.path.name for f in models_ctl.local_ggufs([other], models_ctl.seen_by("llamacpp", mine))], ["a.gguf"])
            models_ctl.add_local("lmstudio", tmp / "lms", other / "a.gguf")
            self.assertTrue((tmp / "lms/local/a/a.gguf").exists())
            self.assertTrue((tmp / "lms/local/a/mmproj-a.gguf").exists())         # its vision projector too
            self.assertEqual(os.stat(tmp / "lms/local/a/a.gguf").st_ino, os.stat(other / "a.gguf").st_ino)
            # LM Studio reads only <publisher>/<model>/: a loose file in its folder is still to add.
            (tmp / "lms/loose.gguf").write_bytes(b"x")
            names = [f.path.name for f in models_ctl.local_ggufs([tmp / "lms"], models_ctl.seen_by("lmstudio", tmp / "lms"))]
            self.assertEqual(names, ["loose.gguf"])


if __name__ == "__main__":
    unittest.main()
