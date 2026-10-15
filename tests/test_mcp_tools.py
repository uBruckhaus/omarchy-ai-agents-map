import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import mcp_tools  # noqa: E402

SERVER = r'''
import json, os, sys
print("starting up...", flush=True)          # not JSON: must be skipped
for line in sys.stdin:
    msg = json.loads(line)
    if "id" not in msg:
        continue
    if msg["method"] == "initialize":
        result = {"serverInfo": {"name": "fake", "version": "1.0"}, "capabilities": {"tools": {}, "prompts": {}}}
    elif msg["method"] == "tools/list":
        if msg["params"].get("cursor"):
            result = {"tools": [{"name": "second", "description": os.environ.get("FAKE_KEY", "")}]}
        else:
            result = {"tools": [{"name": "first", "description": "One\n  tool"}], "nextCursor": "p2"}
    elif msg["method"] == "prompts/list":
        result = {"prompts": [{"name": "review"}]}
    print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}), flush=True)
'''


class ToolsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        (self.dir / "server.py").write_text(SERVER)
        patcher = mock.patch.object(mcp_tools, "CACHE_DIR", self.dir / "cache")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.entry = {"transport": "stdio", "command": sys.executable, "args": [str(self.dir / "server.py")],
                      "env": {"FAKE_KEY": "from env"}, "url": "", "headers": {}}

    def tearDown(self):
        self.tmp.cleanup()

    def test_pages_prompts_and_cache(self):
        result = mcp_tools.ask_and_keep("fake", self.entry)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["server"], "fake 1.0")
        self.assertEqual([(t["name"], t["description"]) for t in result["tools"]],
                         [("first", "One tool"), ("second", "from env")])
        self.assertEqual([p["name"] for p in result["prompts"]], ["review"])
        self.assertEqual(mcp_tools.cached("fake")["tools"][1]["name"], "second")
        self.assertEqual(os.stat(mcp_tools.cache_file("fake")).st_mode & 0o777, 0o600)

    def test_failure_is_kept(self):
        broken = dict(self.entry, args=["-c", "import sys; sys.exit(0)"])
        result = mcp_tools.ask_and_keep("broken", broken)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(mcp_tools.ask_and_keep("old", dict(self.entry, transport="sse"))["status"], "failed")
        with self.assertRaises(ValueError):
            mcp_tools.cache_file("../x")


if __name__ == "__main__":
    unittest.main()
