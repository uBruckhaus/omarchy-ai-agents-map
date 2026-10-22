import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import mcp_updates  # noqa: E402


class McpUpdatesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.apps = root / "Applications"
        program = self.apps / "photocraft-0.11.1/usr/bin/photocraft-cli"
        program.parent.mkdir(parents=True)
        program.write_text("")
        (root / "bin").mkdir()
        (root / "bin/photocraft-cli").symlink_to(program)
        patcher = mock.patch.object(mcp_updates, "APPS", self.apps)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.cache = root / "cache.json"
        self.library = {
            "photocraft": {"command": str(root / "bin/photocraft-cli"), "args": ["mcp"]},
            "pinned": {"command": "npx", "args": ["-y", "@scope/some-mcp@1.2.3"]},
            "newest": {"command": "npx", "args": ["-y", "chrome-devtools-mcp@latest"]},
            "loose": {"command": "npx", "args": ["-y", "some-mcp-server"]},
            "py": {"command": "uvx", "args": ["mcp-server-time==0.6.2"]},
            "remote": {"url": "https://mcp.example.com/mcp"},
        }
        self.servers = mcp_updates.installed_servers(self.library)

    def tearDown(self):
        self.tmp.cleanup()

    def test_versions_told_from_how_servers_start(self):
        self.assertEqual([(s.name, s.app, s.version, s.source) for s in self.servers],
                         [("photocraft", "photocraft", "0.11.1", "app"), ("pinned", "@scope/some-mcp", "1.2.3", "npm"),
                          ("py", "mcp-server-time", "0.6.2", "pypi")])

    def test_only_answers_that_hold_up(self):
        answer = {"servers": [
            {"name": "photocraft", "latest": "v0.12.0", "release_url": "https://github.com/storytold/photocraft/releases",
             "notes": "Content-Aware\nMove " + "x" * 300},
            {"name": "pinned", "latest": "1.2.3", "release_url": "https://npmjs.com/x", "notes": ""},       # not newer
            {"name": "py", "latest": "0.7.0", "release_url": "http://pypi.org/x", "notes": ""},             # not https
            {"name": "unknown", "latest": "9.0", "release_url": "https://x.org", "notes": ""},
            {"name": "photocraft", "latest": "1.0", "release_url": "https://x.org", "notes": ""},           # twice
            {"name": "py", "latest": "0.7.0; rm -rf ~", "release_url": "https://pypi.org/x", "notes": ""}]}
        found = mcp_updates.validate(answer, self.servers)
        self.assertEqual([(f.name, f.latest) for f in found], [("photocraft", "0.12.0")])
        self.assertNotIn("\n", found[0].notes)
        self.assertLessEqual(len(found[0].notes), 160)

    def test_asked_once_a_day_and_kept_on_failure(self):
        calls = []

        def look_up(agent, task, prompt, schema, result_of=None):
            calls.append((agent, task))
            self.assertEqual(result_of('{"servers": []}'), "0 updates found")
            self.assertIn("photocraft 0.11.1", prompt)
            return "Sure: " + json.dumps({"servers": [{"name": "photocraft", "latest": "0.12.0",
                                                       "release_url": "https://example.org/r", "notes": "new"}]})

        with mock.patch("ai_helper.chosen", lambda installed: "claude"):
            first = mcp_updates.check({"claude"}, now=1000.0, cache=self.cache, look_up=look_up, library=self.library)
            again = mcp_updates.check({"claude"}, now=2000.0, cache=self.cache, look_up=look_up, library=self.library)
            self.assertEqual(len(calls), 1)
            self.assertEqual([f.latest for f in first], ["0.12.0"])
            self.assertEqual(first, again)

            def fails(*_a, **_k):
                raise RuntimeError("signed out")
            later = 1000.0 + mcp_updates.CHECK_EVERY + 1
            kept = mcp_updates.check({"claude"}, now=later, cache=self.cache, look_up=fails, library=self.library)
            self.assertEqual(kept, first)                     # the old answer stays
            self.assertFalse(mcp_updates.due(later + 60, self.cache))
            self.assertTrue(mcp_updates.due(later + mcp_updates.RETRY_FAILED + 1, self.cache))

    def test_a_hint_goes_once_the_new_version_is_in(self):
        found = [mcp_updates.Found("photocraft", "photocraft", "0.11.1", "0.12.0", "https://x.org")]
        self.assertEqual(mcp_updates.still_open(found, self.servers), found)
        moved = [mcp_updates.Server("photocraft", "photocraft", "0.12.0", "app")]
        self.assertEqual(mcp_updates.still_open(found, moved), [])

    def test_the_task_downloads_only(self):
        update = mcp_updates.Found("photocraft", "photocraft", "0.11.1", "0.12.0", "https://x.org/r", source="app")
        prompt = mcp_updates.task_prompt(update)
        self.assertIn("Do not unpack it", prompt)
        self.assertIn("https://x.org/r", prompt)


if __name__ == "__main__":
    unittest.main()
