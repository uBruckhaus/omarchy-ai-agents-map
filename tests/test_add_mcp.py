import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import connect  # noqa: E402
import mcp_ctl  # noqa: E402
import mcp_registry  # noqa: E402
import mcp_scan  # noqa: E402

REGISTRY = {"servers": [
    {"server": {"name": "io.github.acme/mcp-server-notes", "description": "Notes", "version": "1.2.0",
                "repository": {"url": "https://github.com/acme/notes"},
                "packages": [{"registryType": "npm", "identifier": "@acme/notes-mcp", "version": "1.2.0",
                              "transport": {"type": "stdio"},
                              "environmentVariables": [{"name": "NOTES_KEY", "isRequired": True, "isSecret": True},
                                                       {"name": "NOTES_LOG", "isRequired": False}],
                              "packageArguments": [{"type": "named", "name": "--root", "isRequired": True,
                                                    "valueHint": "folder"}]}],
                "remotes": [{"type": "streamable-http", "url": "https://notes.example.com/mcp",
                             "headers": [{"name": "Authorization", "isRequired": True, "isSecret": True}]}]}},
    {"server": {"name": "bad/one", "packages": [{"registryType": "npm", "identifier": "x; rm -rf ~",
                                                "transport": {"type": "stdio"}}]}},
]}


class RegistryTest(unittest.TestCase):
    def test_package_and_remote(self):
        servers = mcp_registry.parse(REGISTRY)
        self.assertEqual(len(servers), 1)              # the bad identifier is dropped
        server = servers[0]
        self.assertEqual(server.short, "notes")
        npm, remote = server.options
        self.assertEqual(npm.entry["command"], "npx")
        self.assertEqual(npm.entry["args"], ["-y", "@acme/notes-mcp@1.2.0", "--root", "{arg:0}"])
        self.assertEqual(npm.entry["env"], {"NOTES_KEY": "{NOTES_KEY}"})
        self.assertEqual([n.name for n in npm.needs if n.required], ["NOTES_KEY", "arg:0"])
        filled = mcp_registry.fill(npm.entry, {"NOTES_KEY": "k-123", "arg:0": "/home/u/Notes"})
        self.assertEqual(filled["args"][-1], "/home/u/Notes")
        self.assertEqual(mcp_registry.unfilled(filled), [])
        self.assertEqual(remote.entry["transport"], "http")
        self.assertEqual(mcp_registry.unfilled(remote.entry), ["Authorization"])


class ScanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        for name, value in (("HOME", self.home), ("CACHE", self.home / "cache/appimages"),
                            ("SCAN_CACHE", self.home / "cache/scan.json"), ("_CACHE", None),
                            ("OPT", self.home / "opt")):
            patcher = mock.patch.object(mcp_scan, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.dict(os.environ, {"PATH": str(self.home / "nowhere")})
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def program(self, path: Path, body: bytes):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x7fELF" + body)
        os.chmod(path, 0o755)

    def test_finds_servers_not_clients(self):
        usage = (b"  photoapp-cli mcp [--bridge HOST] [--automation-read-root DIR]\n"
                 b"                    [--automation-write-root DIR]   MCP server on stdio\n")
        self.program(self.home / "Applications/photoapp-1.0/usr/bin/photoapp-cli", b"\0protocolVersion\0" + usage)
        self.program(self.home / "Applications/photoapp-1.0/usr/bin/photoapp", b"just a gui")
        self.program(self.home / ".local/bin/claude", b"protocolVersion tools/list notifications/initialized")
        self.program(self.home / ".local/bin/notes-mcp", b"modelcontextprotocol protocolVersion")
        found = {c.name: c for c in mcp_scan.scan()}
        self.assertEqual(set(found), {"photoapp", "notes"})
        photo = found["photoapp"]
        pictures = str(self.home / "Pictures")
        self.assertEqual(photo.command[1:], ["mcp", "--automation-read-root", pictures,
                                             "--automation-write-root", pictures])
        self.assertTrue(photo.sure)
        self.assertEqual(found["notes"].command, [str(self.home / ".local/bin/notes-mcp")])

    def test_names(self):
        self.assertEqual(mcp_scan.appimage_stem(Path("wordcraft-0.6.1-linux-x86_64.AppImage")), "wordcraft-0.6.1")
        self.assertEqual(mcp_scan.server_name(Path("mcp-server-github")), "github")
        self.assertEqual(mcp_scan.folder_for("soundcraft-cli"), str(self.home / "Music"))


class NewEntryTest(unittest.TestCase):
    def test_check_entry(self):
        good = mcp_ctl.check_entry({"transport": "stdio", "command": "npx", "args": ["-y", "x"],
                                    "env": {"KEY": "v"}, "headers": {"X": "y"}})
        self.assertEqual(good["headers"], {})
        for bad in ({"transport": "stdio", "command": ""}, {"transport": "http", "url": "file:///etc/passwd"},
                    {"transport": "stdio", "command": "x", "env": {"BAD NAME": "v"}}, {"transport": "ftp"}):
            with self.assertRaises(ValueError):
                mcp_ctl.check_entry(bad)

    def test_one_plan_for_several_agents(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            with mock.patch.object(connect, "HOME", home), mock.patch.object(mcp_ctl, "HOME", home):
                entry = {"transport": "stdio", "command": "/opt/x/x-cli", "args": ["mcp"], "env": {}}
                merged = connect.merge([mcp_ctl.plan(a, "x", entry=entry) for a in ("pi", "cursor-agent", "gemini")])
                backup = connect.apply(merged, home / "backups")
                for agent in ("pi", "cursor-agent", "gemini"):
                    self.assertIn("x", mcp_ctl.raw_entries(agent))
                connect.undo(backup)
                self.assertFalse(any(mcp_ctl.raw_entries(a) for a in ("pi", "cursor-agent", "gemini")))


if __name__ == "__main__":
    unittest.main()
