import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import previews  # noqa: E402


class WidenTest(unittest.TestCase):
    def test_modes(self):
        start = previews.widen_frame(0.0, "workspace")
        self.assertEqual((start["slide"], start["grow"]), (0.0, 0.0))
        moved = previews.widen_frame(4.0, "workspace")
        self.assertEqual((moved["slide"], moved["grow"]), (1.0, 0.0))
        grown = previews.widen_frame(4.0, "maximize")
        self.assertEqual((grown["slide"], grown["grow"]), (0.0, 1.0))
        self.assertTrue(previews.widen_frame(1.4, "workspace")["click"])
        off = previews.widen_frame(4.0, "off")
        self.assertEqual((off["slide"], off["grow"], off["hint"], off["click"]), (0.0, 0.0, False, False))


class SetupTest(unittest.TestCase):
    def test_story(self):
        self.assertEqual(previews.setup_state(0)["caption"], 0)
        middle = previews.setup_state(1.5)
        self.assertEqual(middle["drawing"][:2], ("agent", "local"))
        self.assertEqual(previews.setup_state(4.8)["click"], "model_b")
        end = previews.setup_state(previews.DURATION)
        self.assertEqual(end["done"], [("agent", "local"), ("local", "model_b"), ("agent", "mcp"),
                                       ("agent", "skills")])
        self.assertEqual(end["caption"], 5)
        captions = [previews.setup_state(t / 2)["caption"] for t in range(int(previews.DURATION * 2) + 1)]
        self.assertEqual(captions, sorted(captions))          # the captions only move forward

    def test_every_box_known(self):
        for _s, _e, _what, src, dst, _c in previews.STEPS:
            for key in (src, dst):
                self.assertTrue(key is None or key in previews.BOXES)


if __name__ == "__main__":
    unittest.main()
