import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import isolate  # noqa: E402
import agent_profiles  # noqa: E402
import ai_helper  # noqa: E402
import plugin_ai  # noqa: E402


class AiHelperTest(unittest.TestCase):
    """The app's side of plugin_ai (its own tests are in ~/Projects/omarchy-plugin-ai): which agent,
    from the app's adapters and Settings, and its runs in the shared log under the app's name."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        isolate.desktop_in(self, self.home)
        patcher = mock.patch.object(agent_profiles, "WORK_DIR", self.home / "work")
        patcher.start()
        self.addCleanup(patcher.stop)
        which = mock.patch("shutil.which", lambda name: f"/usr/bin/{name}")
        which.start()
        self.addCleanup(which.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def test_chosen_agent_from_settings_else_the_first(self):
        with mock.patch("ai_core.setting", lambda key: "codex" if key == "ai_checks_agent" else None):
            self.assertEqual(ai_helper.chosen({"claude", "codex"}), "codex")
            self.assertEqual(ai_helper.in_order({"claude", "codex"}), ["codex", "claude"])
            self.assertEqual(ai_helper.chosen({"claude"}), "claude")
        self.assertIsNone(ai_helper.chosen(set()))

    def test_look_up_goes_into_the_shared_log_under_the_apps_name(self):
        def run(argv, **_kw):
            return json.dumps({"type": "result", "result": "ok", "usage": {"input_tokens": 10,
                                                                           "output_tokens": 5}}).encode(), True
        self.assertEqual(ai_helper.look_up("claude", "mcp-updates", "PROMPT", {}, run=run,
                                           result_of=lambda a: "2 updates found"), "ok")
        last = plugin_ai.runs()[-1]
        self.assertTrue(str(plugin_ai.log_file()).startswith(str(self.home)))
        self.assertEqual((last["plugin"], last["task"], last["result"]), (ai_helper.plugin_id(), "mcp-updates",
                                                                          "2 updates found"))
        self.assertEqual(ai_helper.totals("claude").tokens, 15)

    def test_who_pays(self):
        import types
        access = {"claude": types.SimpleNamespace(env=("ANTHROPIC_API_KEY",))}
        with mock.patch("agent_status.access_table", lambda: access), \
                mock.patch("agent_details.details_for", lambda c: types.SimpleNamespace(plan="Max 5x" if c == "claude" else "")):
            self.assertEqual(ai_helper.billing("claude", {}), ("plan", "Max 5x"))
            self.assertEqual(ai_helper.billing("claude", {"ANTHROPIC_API_KEY": "sk-x"}), ("key", ""))   # the key wins
            self.assertEqual(ai_helper.billing("codex", {}), ("unknown", ""))

    def test_task_argv_puts_the_prompt_in_as_one_argument(self):
        self.assertEqual(ai_helper.task_argv("claude", "do; rm -rf x"), ["/usr/bin/claude", "do; rm -rf x"])
        self.assertIsNone(ai_helper.task_argv("pi", "x"))         # no task fact


if __name__ == "__main__":
    unittest.main()
