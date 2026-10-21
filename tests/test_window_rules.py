import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import window_rules as rules  # noqa: E402

HALF = 941            # a half-screen tile on a 1920 px wide (logical) monitor
FULL = 1882
MONITOR = 1920


class SizeTest(unittest.TestCase):
    def test_no_width_yet_is_neither_narrow_nor_wide(self):
        # Before the first allocation the window has no width; split is still the monitor's guess.
        self.assertFalse(rules.narrow(0))
        self.assertIsNone(rules.layer_situation(0, agent_running=True))

    def test_half_screen_is_narrow_full_screen_is_not(self):
        self.assertTrue(rules.narrow(HALF))
        self.assertFalse(rules.full_size(HALF))
        self.assertFalse(rules.narrow(FULL))
        self.assertTrue(rules.full_size(rules.SPLIT_WIDTH))

    def test_a_tiled_half_screen_window_is_not_maximized(self):
        # Hyprland reports every tiled window as maximized.
        self.assertFalse(rules.really_maximized(True, HALF, MONITOR))
        self.assertTrue(rules.really_maximized(True, FULL, MONITOR))
        self.assertFalse(rules.really_maximized(None, FULL, MONITOR))
        self.assertTrue(rules.really_maximized(True, HALF, None))      # no monitor known: trust it


class LayersTest(unittest.TestCase):
    SAVED = ["online", "local", "models", "mcp", "skills"]

    def test_narrow_with_an_agent_running_shows_agents_and_providers(self):
        situation = rules.layer_situation(HALF, agent_running=True)
        self.assertEqual(situation, ("narrow", True))
        self.assertEqual(rules.layers_for(situation, None, self.SAVED), {"agents", "online", "local"})

    def test_narrow_without_agents_shows_providers_and_models(self):
        situation = rules.layer_situation(HALF, agent_running=False)
        self.assertEqual(rules.layers_for(situation, None, self.SAVED), {"online", "local", "models"})

    def test_wide_first_time_is_the_saved_choice_with_agents(self):
        situation = rules.layer_situation(FULL, agent_running=False)
        self.assertEqual(situation, ("wide",))
        self.assertEqual(rules.layers_for(situation, None, self.SAVED), set(self.SAVED) | {"agents"})

    def test_wide_again_is_what_the_full_size_window_had(self):
        kept = {"agents", "online"}
        self.assertEqual(rules.layers_for(("wide",), kept, self.SAVED), kept)


if __name__ == "__main__":
    unittest.main()
