"""Setup's model folders: one card per folder holding model files, and Change folder moving a folder's
models with what pointed at them (router links, models.ini) following, and Undo putting all back."""

import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import model_folders  # noqa: E402
import model_move  # noqa: E402


def rt(key, folder):
    return types.SimpleNamespace(key=key, models_dir=str(folder), installed=True)


class ModelFoldersTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.models = self.root / "Models"                    # LM Studio's folder: <publisher>/<model>/
        self.gemma = self.models / "unsloth" / "gemma-GGUF"
        self.gemma.mkdir(parents=True)
        (self.gemma / "gemma-Q4.gguf").write_bytes(b"g" * 100)
        (self.gemma / "mmproj-BF16.gguf").write_bytes(b"m" * 10)
        self.router = self.models / "router"                   # llama.cpp: links only
        self.router.mkdir()
        (self.router / "gemma-Q4.gguf").symlink_to(self.gemma / "gemma-Q4.gguf")
        self.ini = self.root / "models.ini"
        self.ini.write_text(f"[gemma-Q4]\nmmproj = {self.gemma / 'mmproj-BF16.gguf'}\nctx-size = 1\n")
        self.runtimes = [rt("llamacpp", self.router), rt("lmstudio", self.models)]
        patcher = mock.patch.object(model_folders, "BACKUPS", self.root / "backups")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_one_card_per_real_folder_not_for_the_links(self):
        found = model_folders.discover(self.runtimes)
        self.assertEqual([f.path for f in found], [self.gemma.resolve()])
        self.assertEqual(found[0].models, ["gemma-Q4"])                    # the vision file is no model
        self.assertEqual(sorted(found[0].readers), ["llamacpp", "lmstudio"])
        self.assertEqual(len(found[0].files), 2)

    def test_a_file_reached_only_by_another_llms_link_is_not_read_by_lm_studio(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "qwen.gguf").write_bytes(b"q")
        (self.router / "qwen.gguf").symlink_to(outside / "qwen.gguf")       # the router inside LM Studio's
        found = {f.path: f for f in model_folders.discover(self.runtimes)}
        self.assertEqual(found[outside.resolve()].readers, ["llamacpp"])

    def test_an_added_folder_shows_even_empty(self):
        empty = self.root / "Chess"
        empty.mkdir()
        found = model_folders.discover(self.runtimes, [str(empty)])
        self.assertIn(empty.resolve(), [f.path for f in found])
        self.assertTrue(next(f for f in found if f.path == empty.resolve()).added)

    def test_change_folder_moves_and_fixes_links_and_ini_then_undo(self):
        folder = model_folders.discover(self.runtimes)[0]
        new = self.models / "chess"
        change = model_folders.plan(folder, new, self.runtimes, ini=self.ini)
        self.assertEqual(len(change.move.steps), 2)
        self.assertEqual([l[0].name for l in change.links], ["gemma-Q4.gguf"])
        self.assertTrue(change.ini)
        self.assertEqual(change.lost, [])                                  # LM Studio: still below its folder
        done = model_move.apply(change.move)
        self.assertEqual(model_folders.fix(change, ini=self.ini), [])
        self.assertFalse((self.gemma / "gemma-Q4.gguf").exists())
        self.assertEqual((self.router / "gemma-Q4.gguf").resolve(), (new / "gemma-Q4.gguf").resolve())
        self.assertIn(str(new / "mmproj-BF16.gguf"), self.ini.read_text())
        # Undo: the files, the link and models.ini as before.
        self.assertEqual(model_move.undo(change.move, done), [])
        self.assertEqual(model_folders.fix(change, ini=self.ini, back=True), [])
        self.assertTrue((self.gemma / "gemma-Q4.gguf").is_file())
        self.assertEqual((self.router / "gemma-Q4.gguf").resolve(), (self.gemma / "gemma-Q4.gguf").resolve())
        self.assertIn(str(self.gemma / "mmproj-BF16.gguf"), self.ini.read_text())

    def test_what_will_not_see_the_models_is_said_first(self):
        folder = model_folders.discover(self.runtimes)[0]
        outside = self.root / "elsewhere"                                   # outside LM Studio's folder
        change = model_folders.plan(folder, outside, self.runtimes, ini=self.ini)
        self.assertEqual(change.lost, ["lmstudio"])                         # llama.cpp follows by its link

    def test_a_folder_that_is_a_local_llms_own_is_repointed_where_it_can_be(self):
        kobold = self.root / "kobold"
        kobold.mkdir()
        (kobold / "m.gguf").write_bytes(b"x")
        runtimes = [rt("koboldcpp", kobold)]
        folder = model_folders.discover(runtimes)[0]
        self.assertEqual(model_folders.plan(folder, self.root / "k2", runtimes, lambda k: True).repoint,
                         ["koboldcpp"])
        self.assertEqual(model_folders.plan(folder, self.root / "k2", runtimes, lambda k: False).lost,
                         ["koboldcpp"])

    def test_refused_moves(self):
        folder = model_folders.discover(self.runtimes)[0]
        for bad in (self.gemma, self.gemma / "inside"):
            with self.assertRaises(ValueError, msg=bad):
                model_folders.plan(folder, bad, self.runtimes, ini=self.ini)
        taken = self.root / "taken"
        taken.mkdir()
        (taken / "gemma-Q4.gguf").write_bytes(b"other")
        with self.assertRaises(ValueError):                                 # never over an existing file
            model_folders.plan(folder, taken, self.runtimes, ini=self.ini)


if __name__ == "__main__":
    unittest.main()
