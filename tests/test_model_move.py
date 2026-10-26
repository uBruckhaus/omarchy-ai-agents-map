import os
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import model_move  # noqa: E402


class ModelMoveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.llama, self.kobold, self.lms = root / "llama", root / "kobold", root / "lms"
        for folder in (self.llama, self.kobold, self.lms):
            folder.mkdir()

    def gguf(self, path: Path, data: bytes = b"GGUF" + b"\0" * 60) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def test_flat_to_flat_and_back(self):
        self.gguf(self.llama / "qwen.gguf")
        move = model_move.plan("llamacpp", self.llama, "qwen", "koboldcpp", self.kobold)
        self.assertEqual([t for _s, t in move.steps], [self.kobold.resolve() / "qwen.gguf"])
        self.assertFalse(move.copy)
        done = model_move.apply(move)
        self.assertFalse((self.llama / "qwen.gguf").exists())
        self.assertTrue((self.kobold / "qwen.gguf").is_file())
        self.assertEqual(model_move.undo(move, done), [])
        self.assertTrue((self.llama / "qwen.gguf").is_file())

    def test_all_parts_of_a_split_model(self):
        for part in (1, 2):
            self.gguf(self.llama / f"big-0000{part}-of-00002.gguf")
        move = model_move.plan("llamacpp", self.llama, "big", "koboldcpp", self.kobold)
        self.assertEqual(len(move.steps), 2)

    def test_flat_to_lm_studio_gets_a_model_folder(self):
        self.gguf(self.llama / "qwen.gguf")
        move = model_move.plan("llamacpp", self.llama, "qwen", "lmstudio", self.lms)
        target = move.steps[0][1]
        self.assertTrue(target.is_relative_to(self.lms.resolve()))
        self.assertEqual(len(target.relative_to(self.lms.resolve()).parts), 3)    # local/<name>/<file>

    def test_a_link_is_not_moved(self):
        real = self.gguf(Path(self.tmp.name) / "elsewhere" / "qwen.gguf")
        (self.llama / "qwen.gguf").symlink_to(real)
        with self.assertRaises(ValueError):
            model_move.plan("llamacpp", self.llama, "qwen", "koboldcpp", self.kobold)

    def test_never_overwrites(self):
        self.gguf(self.llama / "qwen.gguf")
        self.gguf(self.kobold / "qwen.gguf", b"other")
        with self.assertRaises(ValueError):
            model_move.plan("llamacpp", self.llama, "qwen", "koboldcpp", self.kobold)
        self.assertEqual((self.kobold / "qwen.gguf").read_bytes(), b"other")

    def test_planted_link_at_the_target_is_not_followed(self):
        self.gguf(self.llama / "qwen.gguf")
        victim = Path(self.tmp.name) / "victim"
        (self.kobold / "qwen.gguf").symlink_to(victim)
        with self.assertRaises(ValueError):
            model_move.plan("llamacpp", self.llama, "qwen", "koboldcpp", self.kobold)
        self.assertFalse(victim.exists())

    def test_same_folder_ollama_and_hostile_names(self):
        self.gguf(self.llama / "qwen.gguf")
        for args in (("llamacpp", self.llama, "qwen", "koboldcpp", self.llama),
                     ("ollama", self.llama, "qwen", "koboldcpp", self.kobold),
                     ("llamacpp", self.llama, "../qwen", "koboldcpp", self.kobold),
                     ("llamacpp", self.llama, "q*", "koboldcpp", self.kobold)):
            with self.assertRaises(ValueError):
                model_move.plan(*args)

    def test_file_swapped_for_a_link_after_planning(self):
        path = self.gguf(self.llama / "qwen.gguf")
        move = model_move.plan("llamacpp", self.llama, "qwen", "koboldcpp", self.kobold)
        path.unlink()
        path.symlink_to(Path(self.tmp.name) / "secret")
        with self.assertRaises(ValueError):
            model_move.apply(move)
        self.assertFalse((self.kobold / "qwen.gguf").exists())

    def test_a_name_that_appears_at_the_target_is_not_overwritten(self):
        self.gguf(self.llama / "qwen.gguf")
        move = model_move.plan("llamacpp", self.llama, "qwen", "koboldcpp", self.kobold)
        (self.kobold / "qwen.gguf").write_bytes(b"theirs")           # after the check
        with self.assertRaises(OSError):
            model_move.apply(move)
        self.assertEqual((self.kobold / "qwen.gguf").read_bytes(), b"theirs")
        self.assertTrue((self.llama / "qwen.gguf").is_file())

    def test_copy_to_another_disk_path(self):
        self.gguf(self.llama / "qwen.gguf")
        move = model_move.plan("llamacpp", self.llama, "qwen", "koboldcpp", self.kobold)
        move.copy = True
        done = model_move.apply(move)
        self.assertTrue((self.kobold / "qwen.gguf").is_file())
        self.assertFalse((self.llama / "qwen.gguf").exists())
        self.assertEqual(list(self.kobold.glob(".*.part")), [])
        self.assertEqual(model_move.undo(move, done), [])
        self.assertTrue((self.llama / "qwen.gguf").is_file())

    def test_a_planted_folder_link_on_the_way_is_refused(self):
        self.gguf(self.llama / "qwen.gguf")
        move = model_move.plan("llamacpp", self.llama, "qwen", "lmstudio", self.lms)
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        (self.lms / "local").symlink_to(outside)
        with self.assertRaises(ValueError):
            model_move.apply(move)
        self.assertEqual(list(outside.rglob("*.gguf")), [])

    def swap_during_move(self, copy: bool):
        """Replace the source by a link to a private file right after the move has checked it."""
        source = self.gguf(self.llama / "qwen.gguf")
        secret = Path(self.tmp.name) / "secret"
        secret.write_bytes(b"SECRET")
        secret.chmod(0o600)
        move = model_move.plan("llamacpp", self.llama, "qwen", "koboldcpp", self.kobold)
        move.copy = copy
        walk = model_move._walk
        swapped = []

        def swap_then_walk(path, root, base, make=False):
            if make:                                 # the target folder: the source is open already
                source.unlink()
                source.symlink_to(secret)
                swapped.append(True)
            return walk(path, root, base, make)
        model_move._walk = swap_then_walk
        self.addCleanup(setattr, model_move, "_walk", walk)
        with self.assertRaises(ValueError):
            model_move.apply(move)
        self.assertEqual(swapped, [True])               # the swap happened with the source open
        leftovers = [p for p in self.kobold.rglob("*") if p.is_file() or p.is_symlink()]
        self.assertEqual(leftovers, [])
        self.assertEqual(secret.read_bytes(), b"SECRET")

    def test_source_swapped_for_a_link_during_a_copy(self):
        self.swap_during_move(copy=True)

    def test_source_swapped_for_a_link_during_a_link_move(self):
        self.swap_during_move(copy=False)

    def test_copy_is_owner_only_until_complete(self):
        source = self.gguf(self.llama / "qwen.gguf", b"GGUF" + b"\0" * 3_000_000)
        source.chmod(0o640)
        move = model_move.plan("llamacpp", self.llama, "qwen", "koboldcpp", self.kobold)
        move.copy = True
        modes = []

        def progress(_fraction, _name):
            modes.extend(p.stat().st_mode & 0o777 for p in self.kobold.glob(".*.part"))
        old_umask = os.umask(0)                      # the most permissive umask
        try:
            model_move.apply(move, progress)
        finally:
            os.umask(old_umask)
        self.assertTrue(modes)
        self.assertEqual(set(modes), {0o600})
        self.assertEqual((self.kobold / "qwen.gguf").stat().st_mode & 0o777, 0o640)

    def test_a_fifo_source_does_not_block(self):
        self.gguf(self.llama / "qwen.gguf")
        move = model_move.plan("llamacpp", self.llama, "qwen", "koboldcpp", self.kobold)
        (self.llama / "qwen.gguf").unlink()
        os.mkfifo(self.llama / "qwen.gguf")
        move.copy = True
        with self.assertRaises(ValueError):
            model_move.apply(move)

    def private_twin(self, name: str = "qwen.gguf") -> Path:
        """A private folder of 'another account' holding a file with the model's name (mode 0644)."""
        private = Path(self.tmp.name) / "private"
        private.mkdir()
        twin = private / name
        twin.write_bytes(b"VICTIM")
        twin.chmod(0o644)
        return private

    def swap_folder(self, folder: Path, to: Path):
        """Put a link to `to` where folder was (the folder itself is kept aside)."""
        folder.rename(folder.with_name(folder.name + ".old"))
        folder.symlink_to(to)

    def assert_victim_untouched(self, private: Path, *folders: Path):
        self.assertEqual((private / "qwen.gguf").read_bytes(), b"VICTIM")
        for folder in folders:
            self.assertEqual([p for p in folder.rglob("*") if p.is_file() or p.is_symlink()], [])

    def test_source_parent_swapped_after_the_preview(self):
        for copy in (False, True):
            with self.subTest(copy=copy):
                self.setUp()
                self.gguf(self.llama / "qwen.gguf")
                move = model_move.plan("llamacpp", self.llama, "qwen", "koboldcpp", self.kobold)
                move.copy = copy
                private = self.private_twin()
                self.swap_folder(self.llama, private)
                with self.assertRaises(ValueError):
                    model_move.apply(move)
                self.assert_victim_untouched(private, self.kobold)

    def test_higher_source_ancestor_swapped_after_the_preview(self):
        for copy in (False, True):
            with self.subTest(copy=copy):
                self.setUp()
                models = Path(self.tmp.name) / "models"
                deep = models / "llama"
                self.gguf(deep / "qwen.gguf")
                move = model_move.plan("llamacpp", deep, "qwen", "koboldcpp", self.kobold)
                move.copy = copy
                private = self.private_twin()
                (private / "llama").mkdir()
                (private / "llama" / "qwen.gguf").write_bytes(b"VICTIM")
                self.swap_folder(models, private)        # models/llama is now private/llama
                with self.assertRaises(ValueError):
                    model_move.apply(move)
                self.assertEqual((private / "llama" / "qwen.gguf").read_bytes(), b"VICTIM")
                self.assert_victim_untouched(private, self.kobold)

    def test_same_name_same_folder_but_another_file(self):
        path = self.gguf(self.llama / "qwen.gguf")
        move = model_move.plan("llamacpp", self.llama, "qwen", "koboldcpp", self.kobold)
        path.unlink()
        self.gguf(path, b"GGUF other file")            # a new inode under the planned name
        with self.assertRaises(ValueError):
            model_move.apply(move)
        self.assertEqual(path.read_bytes(), b"GGUF other file")
        self.assertFalse((self.kobold / "qwen.gguf").exists())

    def test_undo_refuses_a_swapped_target_folder(self):
        for copy in (False, True):
            with self.subTest(copy=copy):
                self.setUp()
                self.gguf(self.llama / "qwen.gguf")
                move = model_move.plan("llamacpp", self.llama, "qwen", "koboldcpp", self.kobold)
                move.copy = copy
                done = model_move.apply(move)
                private = self.private_twin()
                self.swap_folder(self.kobold, private)
                problems = model_move.undo(move, done)
                self.assertEqual(len(problems), 1)
                self.assertEqual((private / "qwen.gguf").read_bytes(), b"VICTIM")
                self.assertFalse((self.llama / "qwen.gguf").exists())

    def test_undo_refuses_a_swapped_higher_target_ancestor(self):
        self.gguf(self.llama / "qwen.gguf")
        move = model_move.plan("llamacpp", self.llama, "qwen", "lmstudio", self.lms)
        done = model_move.apply(move)
        moved = done[0][1]
        self.assertNotEqual(moved.parent, self.lms.resolve())  # local/<model>/ below the folder
        private = Path(self.tmp.name) / "private"
        (private / moved.parent.name).mkdir(parents=True)
        (private / moved.parent.name / "qwen.gguf").write_bytes(b"VICTIM")
        local = self.lms / "local"
        local.rename(self.lms / "local.old")
        local.symlink_to(private)
        self.assertEqual(len(model_move.undo(move, done)), 1)
        self.assertEqual((private / moved.parent.name / "qwen.gguf").read_bytes(), b"VICTIM")
        self.assertFalse((self.llama / "qwen.gguf").exists())

    def test_undo_after_a_copy_checks_the_copy_not_the_original(self):
        self.gguf(self.llama / "qwen.gguf")
        move = model_move.plan("llamacpp", self.llama, "qwen", "koboldcpp", self.kobold)
        move.copy = True
        done = model_move.apply(move)
        target = self.kobold / "qwen.gguf"
        target.unlink()
        self.gguf(target, b"GGUF replaced")              # another file under the moved name
        self.assertEqual(len(model_move.undo(move, done)), 1)
        self.assertEqual(target.read_bytes(), b"GGUF replaced")
        self.assertFalse((self.llama / "qwen.gguf").exists())

    def test_target_folder_swapped_after_the_preview(self):
        for copy in (False, True):
            with self.subTest(copy=copy):
                self.setUp()
                self.gguf(self.llama / "qwen.gguf")
                move = model_move.plan("llamacpp", self.llama, "qwen", "koboldcpp", self.kobold)
                move.copy = copy
                elsewhere = Path(self.tmp.name) / "elsewhere"
                elsewhere.mkdir()
                self.swap_folder(self.kobold, elsewhere)
                with self.assertRaises(ValueError):
                    model_move.apply(move)
                self.assertEqual(list(elsewhere.iterdir()), [])
                self.assertTrue((self.llama / "qwen.gguf").is_file())

    def test_higher_target_ancestor_swapped_after_the_preview(self):
        deep = Path(self.tmp.name) / "disk" / "kobold"
        deep.mkdir(parents=True)
        self.gguf(self.llama / "qwen.gguf")
        move = model_move.plan("llamacpp", self.llama, "qwen", "koboldcpp", deep)
        elsewhere = Path(self.tmp.name) / "elsewhere"
        (elsewhere / "kobold").mkdir(parents=True)
        self.swap_folder(deep.parent, elsewhere)
        with self.assertRaises(ValueError):
            model_move.apply(move)
        self.assertEqual(list((elsewhere / "kobold").iterdir()), [])
        self.assertTrue((self.llama / "qwen.gguf").is_file())

    def test_undo_refuses_a_swapped_restore_folder(self):
        self.gguf(self.llama / "qwen.gguf")
        move = model_move.plan("llamacpp", self.llama, "qwen", "koboldcpp", self.kobold)
        done = model_move.apply(move)
        elsewhere = Path(self.tmp.name) / "elsewhere"
        elsewhere.mkdir()
        self.swap_folder(self.llama, elsewhere)
        self.assertEqual(len(model_move.undo(move, done)), 1)
        self.assertEqual(list(elsewhere.iterdir()), [])
        self.assertTrue((self.kobold / "qwen.gguf").is_file())

    def test_a_folder_others_can_write_into_is_refused(self):
        for folder in ("llama", "kobold"):
            with self.subTest(folder=folder):
                self.setUp()
                self.gguf(self.llama / "qwen.gguf")
                move = model_move.plan("llamacpp", self.llama, "qwen", "koboldcpp", self.kobold)
                getattr(self, folder).chmod(0o777)
                with self.assertRaises(ValueError):
                    model_move.apply(move)
                self.assertTrue((self.llama / "qwen.gguf").is_file())
                self.assertFalse((self.kobold / "qwen.gguf").exists())

    def test_source_that_cannot_be_removed_is_not_moved(self):
        for copy in (False, True):
            with self.subTest(copy=copy):
                self.setUp()
                self.gguf(self.llama / "qwen.gguf")
                move = model_move.plan("llamacpp", self.llama, "qwen", "koboldcpp", self.kobold)
                move.copy = copy
                self.llama.chmod(0o555)
                self.addCleanup(self.llama.chmod, 0o755)
                with self.assertRaises(ValueError) as caught:
                    model_move.apply(move)
                self.assertEqual(caught.exception.done, [])
                self.assertTrue((self.llama / "qwen.gguf").is_file())
                self.assertEqual(list(self.kobold.iterdir()), [])

    def test_a_swapped_partial_copy_is_neither_published_nor_removed(self):
        self.gguf(self.llama / "qwen.gguf", b"GGUF" + b"\0" * 3_000_000)
        move = model_move.plan("llamacpp", self.llama, "qwen", "koboldcpp", self.kobold)
        move.copy = True
        partial, aside = self.kobold / ".qwen.gguf.part", self.kobold / "aside"
        swapped = []

        def progress(_fraction, _name):
            if not swapped:
                partial.rename(aside)
                partial.write_bytes(b"THEIRS")
                swapped.append(True)
        with self.assertRaises(ValueError):
            model_move.apply(move, progress)
        self.assertFalse((self.kobold / "qwen.gguf").exists())
        self.assertEqual(partial.read_bytes(), b"THEIRS")
        self.assertTrue((self.llama / "qwen.gguf").is_file())

    def test_target_folder_replaced_by_another_real_folder(self):
        for copy in (False, True):
            with self.subTest(copy=copy):
                self.setUp()
                self.gguf(self.llama / "qwen.gguf")
                self.kobold.chmod(0o700)
                move = model_move.plan("llamacpp", self.llama, "qwen", "koboldcpp", self.kobold)
                move.copy = copy
                self.kobold.rename(self.kobold.with_name("kobold.old"))
                self.kobold.mkdir(mode=0o755)               # this account's, readable by all
                with self.assertRaises(ValueError):
                    model_move.apply(move)
                self.assertEqual(list(self.kobold.iterdir()), [])
                self.assertTrue((self.llama / "qwen.gguf").is_file())

    def test_undo_refuses_a_restore_folder_replaced_by_another_real_folder(self):
        self.gguf(self.llama / "qwen.gguf")
        move = model_move.plan("llamacpp", self.llama, "qwen", "koboldcpp", self.kobold)
        done = model_move.apply(move)
        self.llama.rename(self.llama.with_name("llama.old"))
        self.llama.mkdir()
        self.assertEqual(len(model_move.undo(move, done)), 1)
        self.assertEqual(list(self.llama.iterdir()), [])
        self.assertTrue((self.kobold / "qwen.gguf").is_file())

    def test_a_group_writable_target_anchor_is_refused_for_nested_folders(self):
        self.gguf(self.llama / "qwen.gguf")
        move = model_move.plan("llamacpp", self.llama, "qwen", "lmstudio", self.lms)
        self.lms.chmod(0o775)
        with self.assertRaises(ValueError):
            model_move.apply(move)
        self.assertEqual(list(self.lms.rglob("*.gguf")), [])
        self.assertTrue((self.llama / "qwen.gguf").is_file())

    def test_without_proc_a_same_disk_move_copies(self):
        self.gguf(self.llama / "qwen.gguf")
        move = model_move.plan("llamacpp", self.llama, "qwen", "koboldcpp", self.kobold)
        real_isdir = os.path.isdir
        with unittest.mock.patch.object(model_move.os.path, "isdir",
                                        lambda p: False if p == "/proc/self/fd" else real_isdir(p)):
            done = model_move.apply(move)
            self.assertTrue((self.kobold / "qwen.gguf").is_file())
            self.assertFalse((self.llama / "qwen.gguf").exists())
            self.assertEqual(model_move.undo(move, done), [])
        self.assertTrue((self.llama / "qwen.gguf").is_file())


if __name__ == "__main__":
    unittest.main()
