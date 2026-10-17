import os
import sys
import tempfile
import unittest
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


if __name__ == "__main__":
    unittest.main()
