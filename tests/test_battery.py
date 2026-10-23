import datetime as dt
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import agent_details  # noqa: E402
from agent_details import Limit  # noqa: E402


class BatteryTest(unittest.TestCase):
    """An online agent's limits as a battery: the tightest one counts; yellow at a quarter or less
    left, red under a tenth (percent is the share used)."""

    def level(self, *used):
        return agent_details.battery([Limit(f"L{i}", u) for i, u in enumerate(used)])

    def test_no_limits_no_battery(self):
        self.assertIsNone(agent_details.battery([]))
        self.assertIsNone(agent_details.battery(None))

    def test_the_tightest_limit_counts(self):
        left, colour = self.level(0.23, 0.80, 0.0)        # 5-hour 23 % used, weekly 80 % used
        self.assertAlmostEqual(left, 0.20)
        self.assertEqual(colour, "yellow")

    def test_thresholds(self):
        self.assertEqual(self.level(0.74)[1], "green")    # 26 % left
        self.assertEqual(self.level(0.75)[1], "yellow")   # 25 % left: yellow
        self.assertEqual(self.level(0.89)[1], "yellow")   # 11 % left
        self.assertEqual(self.level(0.90)[1], "red")      # 10 % left: red from 90 % used
        self.assertEqual(self.level(1.0), (0.0, "red"))

    def test_own_levels(self):
        limits = lambda used: [Limit("Weekly", used)]
        levels = (0.6, 0.8)                                # Settings › Costs: yellow 60 %, red 80 %
        self.assertEqual(agent_details.battery(limits(0.59), levels=levels)[1], "green")
        self.assertEqual(agent_details.battery(limits(0.60), levels=levels)[1], "yellow")
        self.assertEqual(agent_details.battery(limits(0.80), levels=levels)[1], "red")
        self.assertEqual(agent_details.red_limits(limits(0.79), levels=levels), [])
        self.assertEqual(len(agent_details.red_limits(limits(0.80), levels=levels)), 1)

    def test_out_of_range_values_are_clamped(self):
        self.assertEqual(self.level(1.7), (0.0, "red"))
        self.assertEqual(self.level(-0.5), (1.0, "green"))

    def test_tip_lists_every_limit_with_its_reset(self):
        resets = dt.datetime(2026, 10, 22, 14, 0)
        tip = agent_details.battery_tip([Limit("Session (5-hour)", 0.23), Limit("Weekly (7-day)", 0.45, resets)],
                                        now=dt.datetime(2026, 10, 20, 9, 0).astimezone())
        self.assertEqual(tip.splitlines()[0], "Session (5-hour): 77 % left")
        self.assertTrue(tip.splitlines()[1].startswith("Weekly (7-day): 55 % left, resets Thu"))

    def test_limits_past_their_reset_do_not_count(self):
        now = dt.datetime(2026, 10, 23, 12, 0, tzinfo=dt.timezone.utc)
        passed = Limit("Session (5-hour)", 0.95, now - dt.timedelta(minutes=5))   # numbers from before the reset
        weekly = Limit("Weekly (7-day)", 0.40, now + dt.timedelta(days=3))
        self.assertEqual(agent_details.battery([passed, weekly], now=now), (0.6, "green"))
        self.assertEqual(agent_details.battery_tip([passed, weekly], now=now).splitlines()[0][:26],
                         "Weekly (7-day): 60 % left,")
        self.assertIsNone(agent_details.battery([passed], now=now))              # nothing current: no battery
        naive = Limit("Weekly (7-day)", 0.40, dt.datetime(2026, 10, 22, 14, 0))  # local time, no zone
        self.assertIsNone(agent_details.battery([naive], now=now))

    def test_gpu_battery_is_the_free_gpu_memory(self):
        gib = 1024 ** 3
        self.assertIsNone(agent_details.gpu_battery(0, 0))                       # no GPU, no battery
        self.assertEqual(agent_details.gpu_battery(12 * gib, 16 * gib)[:2], (0.75, "green"))
        self.assertEqual(agent_details.gpu_battery(4 * gib, 16 * gib)[:2], (0.25, "yellow"))
        self.assertEqual(agent_details.gpu_battery(gib, 16 * gib)[:2], (0.0625, "red"))
        self.assertEqual(agent_details.gpu_battery(-gib, 16 * gib)[:2], (0.0, "red"))
        tip = agent_details.gpu_battery(4 * gib, 16 * gib, 9 * gib)[2]
        self.assertEqual(tip.splitlines(), ["GPU memory: 4.0 of 16.0 GB free", "This local LLM uses 9.0 GB"])


if __name__ == "__main__":
    unittest.main()
