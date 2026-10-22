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


class CopiedSourceTest(unittest.TestCase):
    """Reorganize's copy (another file system): the planned source is opened once, never through a
    link, and must be this account's own file; a name swapped after the preview is not followed."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(os.path.realpath(self.tmp.name))
        self.shared, self.dest = base / "shared", base / "dest"
        self.shared.mkdir()
        self.dest.mkdir()
        private = base / "private"
        private.mkdir(mode=0o700)
        self.secret = private / "secret.txt"
        self.secret.write_text("private")
        self.secret.chmod(0o644)                   # readable, but only through its 0700 folder
        self.journal = base / "journal.json"

    def tearDown(self):
        self.tmp.cleanup()

    def move(self, source, target, progress=lambda f, t: None):
        return m._move(source, target, True, progress, lambda: False, 0, 10)

    def assert_nothing_exposed(self):
        for path in self.dest.rglob("*"):
            if path.is_file() and not path.is_symlink():
                self.assertNotEqual(path.read_bytes(), b"private", path)

    def test_source_swapped_for_a_link_after_the_preview(self):
        source = self.shared / "m.gguf"
        source.write_bytes(b"model")
        step = m.Step("move", str(source), str(self.dest / "m/m.gguf"), copy=True, size=5)
        plan = m.Plan(self.shared, [step], [], 5)
        source.unlink()
        source.symlink_to(self.secret)             # another user's swap between preview and apply
        with self.assertRaises(PermissionError):
            m.apply(plan, lambda *_: None, journal=self.journal)
        self.assertFalse((self.dest / "m/m.gguf").exists())
        self.assert_nothing_exposed()
        self.assertTrue(source.is_symlink())       # the name is left as it is

    def test_folder_swapped_for_a_link(self):
        folder = self.shared / "blobs"
        folder.symlink_to(self.secret.parent)
        with self.assertRaises(PermissionError):
            self.move(folder, self.dest / "blobs")
        self.assert_nothing_exposed()

    def test_link_inside_a_folder_stays_a_link(self):
        folder = self.shared / "blobs"
        folder.mkdir()
        (folder / "a").write_bytes(b"a")
        (folder / "b").symlink_to(self.secret)
        self.move(folder, self.dest / "blobs")
        self.assertTrue((self.dest / "blobs/b").is_symlink())
        self.assertEqual((self.dest / "blobs/a").read_bytes(), b"a")
        self.assert_nothing_exposed()

    def test_swapped_while_copying(self):
        source = self.shared / "m.gguf"
        source.write_bytes(b"model")

        def swap(_f, _t):
            if not source.is_symlink():
                source.unlink()
                source.symlink_to(self.secret)
        with self.assertRaises(PermissionError):
            self.move(source, self.dest / "m.gguf", progress=swap)
        self.assertFalse((self.dest / "m.gguf").exists())         # the copy of the old file goes too
        self.assertTrue(source.is_symlink())                      # and the swapped name is not removed
        self.assert_nothing_exposed()

    def test_another_accounts_file_is_not_copied(self):
        source = self.shared / "m.gguf"
        source.write_bytes(b"model")
        with mock.patch.object(m.os, "getuid", return_value=os.getuid() + 1):
            with self.assertRaises(PermissionError):
                self.move(source, self.dest / "m.gguf")
        self.assertTrue(source.exists())

    def test_part_file_owner_only_until_complete(self):
        source = self.shared / "m.gguf"
        source.write_bytes(b"model")
        source.chmod(0o644)
        modes = []
        self.move(source, self.dest / "m.gguf",
                  progress=lambda f, t: modes.append((self.dest / "m.gguf.part").stat().st_mode & 0o777))
        self.assertEqual(set(modes), {0o600})
        self.assertEqual((self.dest / "m.gguf").stat().st_mode & 0o777, 0o644)
        self.assertEqual((self.dest / "m.gguf").read_bytes(), b"model")
        self.assertFalse(source.exists())


class SwappedFolderTest(unittest.TestCase):
    """A folder on the way to a planned source swapped after the preview (another user who controls
    a shared folder): the path is walked without links and the file must be the one previewed."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(os.path.realpath(self.tmp.name))
        self.shared, self.dest = base / "shared", base / "dest"
        (self.shared / "pub").mkdir(parents=True)
        self.dest.mkdir()
        self.private = base / "private"
        self.private.mkdir(mode=0o700)
        (self.private / "m.gguf").write_bytes(b"private")  # this account's own model in its private folder
        self.source = self.shared / "pub/m.gguf"
        self.source.write_bytes(b"model")
        self.journal = base / "journal.json"

    def tearDown(self):
        self.tmp.cleanup()

    def planned(self, copy=True):
        step = m.Step("move", str(self.source), str(self.dest / "m/m.gguf"), 5, copy=copy,
                      ident=m._ident(self.source))
        return m.Plan(self.shared, [step], [], 5 if copy else 0)

    def swap_folder_for_link(self):
        (self.shared / "pub/m.gguf").unlink()
        (self.shared / "pub").rmdir()
        (self.shared / "pub").symlink_to(self.private)

    def assert_private_untouched(self):
        self.assertEqual((self.private / "m.gguf").read_bytes(), b"private")
        self.assertFalse((self.dest / "m/m.gguf").exists())

    def test_plan_records_the_identity(self):
        step = m.plan(self.dest, [m.Provider("x", "X", self.shared, True, layout="flat")]).steps[0]
        self.assertEqual(step.ident, m._ident(self.source))

    def test_folder_swapped_for_a_link_copy(self):
        plan = self.planned()
        self.swap_folder_for_link()
        with self.assertRaises(PermissionError):
            m.apply(plan, lambda *_: None, journal=self.journal)
        self.assert_private_untouched()

    def test_folder_swapped_for_a_link_rename(self):
        plan = self.planned(copy=False)
        self.swap_folder_for_link()
        with self.assertRaises(PermissionError):
            m.apply(plan, lambda *_: None, journal=self.journal)
        self.assert_private_untouched()

    def test_higher_folder_swapped_for_a_link(self):
        plan = self.planned()
        (self.private / "pub").mkdir()
        (self.private / "pub/m.gguf").write_bytes(b"private")
        moved = self.shared.with_name("old-shared")
        self.shared.rename(moved)
        self.shared.symlink_to(self.private)
        with self.assertRaises(PermissionError):
            m.apply(plan, lambda *_: None, journal=self.journal)
        self.assertEqual((self.private / "pub/m.gguf").read_bytes(), b"private")
        self.assertFalse((self.dest / "m/m.gguf").exists())

    def test_other_file_than_previewed(self):
        plan = self.planned()
        self.source.unlink()
        os.link(self.private / "m.gguf", self.source)      # a real folder, but not the previewed file
        with self.assertRaises(PermissionError):
            m.apply(plan, lambda *_: None, journal=self.journal)
        self.assert_private_untouched()

    def test_unchanged_source_is_moved(self):
        m.apply(self.planned(), lambda *_: None, journal=self.journal)
        self.assertEqual((self.dest / "m/m.gguf").read_bytes(), b"model")
        self.assertFalse(self.source.exists())

    def test_journal_keeps_the_identity(self):
        m.apply(self.planned(copy=False), lambda *_: None, journal=self.journal)
        self.assertEqual(m.load_journal(self.journal)[0].ident, m._ident(self.dest / "m/m.gguf"))


if __name__ == "__main__":
    unittest.main()
