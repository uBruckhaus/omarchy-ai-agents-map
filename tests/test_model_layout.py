import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import model_layout as m  # noqa: E402


def make(root: Path):
    """A mixed folder like a real one: LM Studio layout, loose files, a router folder of links, Ollama's store."""
    (root / "pub/repo-GGUF").mkdir(parents=True)
    (root / "pub/repo-GGUF/a-Q4_K_M.gguf").write_bytes(b"a" * 10)
    (root / "lmstudio/pub2/b-GGUF").mkdir(parents=True)
    (root / "lmstudio/pub2/b-GGUF/b.gguf").write_bytes(b"b" * 10)
    (root / "loose-00001-of-00002.gguf").write_bytes(b"l")
    (root / "TTS").mkdir()
    (root / "TTS/tts.gguf").write_bytes(b"t")
    (root / "TTS/config.json").write_text("{}")
    (root / "router").mkdir()
    (root / "router/b.gguf").symlink_to(root / "lmstudio/pub2/b-GGUF/b.gguf")
    (root / "blobs").mkdir()
    (root / "blobs/sha256-1").write_bytes(b"x")
    (root / "manifests").mkdir()


class LayoutTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(os.path.realpath(self.tmp.name)) / "Models"
        self.root.mkdir()
        make(self.root)
        self.providers = [m.Provider("llamacpp", "llama.cpp", self.root / "router", False),
                          m.Provider("ollama", "Ollama", self.root, True),
                          m.Provider("lmstudio", "LM Studio", self.root, False)]
        self.journal = Path(self.tmp.name) / "journal.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_canonical_places(self):
        r = self.root
        self.assertEqual(m.canonical(r / "x/y/z/f.gguf", r, r), r / "y/z/f.gguf")
        self.assertEqual(m.canonical(r / "TTS/f.gguf", r, r), r / "local/TTS/f.gguf")
        self.assertEqual(m.canonical(r / "q-00001-of-00003.gguf", r, r), r / "local/q/q-00001-of-00003.gguf")

    def test_plan_apply_undo(self):
        plan = m.plan(self.root, self.providers)
        self.assertEqual(plan.problems, [])
        kinds = [(s.kind, Path(s.target).relative_to(self.root).as_posix() if s.target else "") for s in plan.steps]
        self.assertIn(("move", "pub2/b-GGUF/b.gguf"), kinds)
        self.assertIn(("move", "local/TTS/tts.gguf"), kinds)
        self.assertIn(("move", "ollama/blobs"), kinds)
        self.assertIn(("folder", "ollama"), kinds)
        self.assertNotIn(("move", "pub/repo-GGUF/a-Q4_K_M.gguf"), kinds)    # already in place
        settings = {}
        m.apply(plan, lambda key, path: settings.__setitem__(key, path), journal=self.journal)
        r = self.root
        self.assertTrue((r / "pub2/b-GGUF/b.gguf").is_file())
        self.assertEqual(os.path.realpath(r / "router/b.gguf"), str(r / "pub2/b-GGUF/b.gguf"))   # followed
        self.assertTrue((r / "TTS/tts.gguf").is_symlink())                                       # old path works
        self.assertEqual((r / "TTS/tts.gguf").read_bytes(), b"t")
        self.assertTrue((r / "TTS/config.json").exists())                                        # not a model: stays
        self.assertTrue((r / "ollama/blobs/sha256-1").exists())
        self.assertEqual(settings, {"ollama": r / "ollama"})
        self.assertTrue(m.plan(r, [m.Provider("llamacpp", "", r / "router", False),
                                   m.Provider("ollama", "", r / "ollama", True),
                                   m.Provider("lmstudio", "", r, False)]).empty)                 # nothing left to do
        problems = m.undo(lambda key, path: settings.__setitem__(key, path), journal=self.journal)
        self.assertEqual(problems, [])
        self.assertTrue((r / "lmstudio/pub2/b-GGUF/b.gguf").is_file())
        self.assertFalse((r / "lmstudio/pub2/b-GGUF/b.gguf").is_symlink())
        self.assertEqual(os.path.realpath(r / "router/b.gguf"), str(r / "lmstudio/pub2/b-GGUF/b.gguf"))
        self.assertTrue((r / "blobs/sha256-1").exists())
        self.assertFalse((r / "local").exists())
        self.assertEqual(settings["ollama"], r)
        self.assertFalse(self.journal.exists())

    def test_our_llamacpp_gets_the_flat_link_folder(self):
        providers = [m.Provider("llamacpp", "llama.cpp", self.root / "router", True)]
        plan = m.plan(self.root, providers, keep_old_paths=False)
        folder = [s for s in plan.steps if s.kind == "folder"]
        self.assertEqual([(s.provider, s.target) for s in folder], [("llamacpp", str(self.root / "llama.cpp"))])
        links = {Path(s.source).name for s in plan.steps if s.kind == "link"}
        self.assertTrue({"a-Q4_K_M.gguf", "b.gguf", "tts.gguf"} <= links)

    def test_refuses_home_and_conflicts(self):
        self.assertIn("root", m.plan(Path.home(), []).problems)
        (self.root / "local/TTS").mkdir(parents=True)
        (self.root / "local/TTS/tts.gguf").write_bytes(b"other")
        plan = m.plan(self.root, self.providers)
        self.assertTrue(any(p.startswith("conflict:") for p in plan.problems))
        with self.assertRaises(ValueError):
            m.apply(plan, lambda *_: None, journal=self.journal)


class HostileLayoutTest(unittest.TestCase):
    """Links, journals and left-over files someone else (or an earlier crash) put there."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(os.path.realpath(self.tmp.name))
        self.root = self.base / "Models"
        (self.root / "router").mkdir(parents=True)
        self.journal = self.base / "state/journal.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_only_own_files_are_moved(self):
        outside = self.base / "elsewhere"
        outside.mkdir()
        (outside / "x.gguf").write_bytes(b"x")
        (self.root / "router/x.gguf").symlink_to(outside / "x.gguf")
        providers = [m.Provider("llamacpp", "llama.cpp", self.root / "router", False)]
        own = m.plan(self.root, providers)
        self.assertTrue(any(s.kind == "move" and s.source == str(outside / "x.gguf") for s in own.steps))
        with mock.patch.object(m.os, "getuid", return_value=os.getuid() + 1):   # as if another account's file
            other = m.plan(self.root, providers)
        self.assertFalse([s for s in other.steps if s.kind == "move"])

    def test_planted_part_link_is_not_followed(self):
        source = self.base / "a.gguf"
        source.write_bytes(b"model")
        victim = self.base / "victim.txt"
        victim.write_text("keep")
        target = self.root / "local/a/a.gguf"
        target.parent.mkdir(parents=True)
        (target.parent / "a.gguf.part").symlink_to(victim)
        with self.assertRaises(FileExistsError):
            m._copy(source, target, lambda f, t: None, lambda: False, 0, 5)
        self.assertEqual(victim.read_text(), "keep")
        self.assertFalse(target.exists())

    def test_left_over_part_file_is_replaced(self):
        source = self.base / "a.gguf"
        source.write_bytes(b"model")
        target = self.root / "a.gguf"
        (self.root / "a.gguf.part").write_bytes(b"old half")
        m._copy(source, target, lambda f, t: None, lambda: False, 0, 5)
        self.assertEqual(target.read_bytes(), b"model")

    def test_journal_written_owner_only_without_predictable_temp(self):
        self.journal.parent.mkdir(parents=True)
        victim = self.base / "victim.txt"
        victim.write_text("keep")
        (self.journal.parent / "journal.json.tmp").symlink_to(victim)
        m._save_journal([m.Step("note", source="x")], self.journal)
        self.assertEqual(victim.read_text(), "keep")
        self.assertEqual(self.journal.stat().st_mode & 0o777, 0o600)
        self.assertEqual([s.source for s in m.load_journal(self.journal)], ["x"])

    def test_journal_that_is_not_plain_is_ignored(self):
        self.journal.parent.mkdir(parents=True)
        for steps in ([{"kind": "move", "source": {"x": 1}}], [{"kind": "rm -rf", "source": "/"}],
                      [{"kind": "move", "extra": "x"}], "steps"):
            self.journal.write_text(json.dumps({"version": 1, "steps": steps}))
            self.assertEqual(m.load_journal(self.journal), [])
        real = self.base / "real.json"
        real.write_text(json.dumps({"version": 1, "steps": [{"kind": "note"}]}))
        self.journal.unlink()
        self.journal.symlink_to(real)
        self.assertEqual(m.load_journal(self.journal), [])


    def test_root_that_could_break_a_settings_file_is_refused(self):
        for name in ("Models\nOLLAMA_HOST=0.0.0.0", "Models$(x)", 'Mo"dels', "Models%h"):
            plan = m.plan(self.base / name, [])
            self.assertEqual(plan.problems, ["root"])
            self.assertEqual(plan.steps, [])

    def test_set_models_folder_refuses_such_a_folder(self):
        import install_ctl
        inst = install_ctl.installer("llamacpp")
        with mock.patch.object(type(inst), "set_models_folder_value") as write:
            with self.assertRaises(ValueError):
                inst.set_models_folder(self.base / "Models\nX=1")
        write.assert_not_called()

if __name__ == "__main__":
    unittest.main()
