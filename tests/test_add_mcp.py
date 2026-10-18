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
import mcp_catalog  # noqa: E402
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
        self.assertEqual(npm.entry["env"], {"NOTES_KEY": "{NOTES_KEY}", "NOTES_LOG": "{NOTES_LOG}"})
        # An optional variable left empty goes again; one filled in stays.
        optional = [n.name for n in npm.needs if not n.required]
        self.assertEqual(mcp_registry.drop_unfilled(npm.entry, optional)["env"], {"NOTES_KEY": "{NOTES_KEY}"})
        self.assertEqual(mcp_registry.drop_unfilled(mcp_registry.fill(npm.entry, {"NOTES_LOG": "1"}), optional)
                         ["env"]["NOTES_LOG"], "1")
        self.assertEqual([n.name for n in npm.needs if n.required], ["NOTES_KEY", "arg:0"])
        filled = mcp_registry.fill(npm.entry, {"NOTES_KEY": "k-123", "arg:0": "/home/u/Notes"})
        self.assertEqual(filled["args"][-1], "/home/u/Notes")
        self.assertEqual(mcp_registry.unfilled(mcp_registry.drop_unfilled(filled, ["NOTES_LOG"])), [])
        self.assertEqual(remote.entry["transport"], "http")
        self.assertEqual(mcp_registry.unfilled(remote.entry), ["Authorization"])


class CatalogTest(unittest.TestCase):
    def pick(self, key):
        return next(p for p in mcp_catalog.PICKS if p.key == key)

    def test_packages_pinned_or_unpinned_offline(self):
        server = mcp_catalog.resolve(self.pick("filesystem"), ask=lambda kind, name: "1.2.3")
        option = server.options[0]
        self.assertEqual(option.entry["args"], ["-y", "@modelcontextprotocol/server-filesystem@1.2.3", "{arg:0}"])
        self.assertEqual([(n.name, n.required, n.hint) for n in option.needs], [("arg:0", True, "~/Documents")])
        offline = mcp_catalog.resolve(self.pick("git"), ask=lambda kind, name: "")
        self.assertEqual(offline.options[0].entry["args"], ["mcp-server-git"])

    def test_placeholder_inside_a_header(self):
        remote = mcp_catalog.resolve(self.pick("github"), ask=lambda *_: "").options[0]
        self.assertEqual([(n.name, n.required, n.secret) for n in remote.needs], [("GITHUB_TOKEN", True, True)])
        filled = mcp_registry.fill(remote.entry, {"GITHUB_TOKEN": "ghp_x"})
        self.assertEqual(filled["headers"], {"Authorization": "Bearer ghp_x"})
        self.assertEqual(mcp_registry.unfilled(filled), [])

    def test_optional_key_left_empty_goes(self):
        for option in mcp_catalog.resolve(self.pick("context7"), ask=lambda *_: "4.0.0").options:
            entry = mcp_registry.drop_unfilled(option.entry, ["CONTEXT7_API_KEY"])
            self.assertEqual((entry["env"], entry["headers"]), ({}, {}))
            self.assertEqual(mcp_registry.unfilled(entry), [])

    def test_every_pick_resolves(self):
        for pick in mcp_catalog.PICKS:
            server = mcp_catalog.resolve(pick, ask=lambda *_: "")
            self.assertTrue(server.options, pick.key)
            self.assertIn(pick.group, dict(mcp_catalog.GROUPS))

    def test_docker_runtime_arguments_and_optional_flag(self):
        package = {"registryType": "oci", "identifier": "ghcr.io/acme/srv:2.0", "transport": {"type": "stdio"},
                   "runtimeArguments": [{"type": "named", "name": "-e", "value": "TOKEN={token}",
                                         "variables": {"token": {"isSecret": True}}},
                                        # a registry entry must not reach into this PC through docker:
                                        {"type": "named", "name": "-v", "value": "/:/host"},
                                        {"type": "positional", "value": "--privileged"},
                                        {"type": "named", "name": "--network", "value": "host"},
                                        {"type": "named", "name": "-e", "value": "X=1 --privileged\n"}]}
        option = mcp_registry.option_from_package(package)
        # The value goes into docker's environment, not its command line (/proc/<pid>/cmdline).
        self.assertEqual(option.entry["args"], ["run", "-i", "--rm", "-e", "TOKEN", "ghcr.io/acme/srv:2.0"])
        self.assertEqual(option.entry["env"], {"TOKEN": "{token}"})
        self.assertEqual([(n.name, n.required, n.secret) for n in option.needs], [("token", False, True)])
        filled = mcp_registry.fill(option.entry, {"token": "s3cret"})
        self.assertNotIn("s3cret", " ".join(filled["args"]))
        self.assertEqual(filled["env"], {"TOKEN": "s3cret"})
        dropped = mcp_registry.drop_unfilled(option.entry, ["token"])
        self.assertEqual(dropped["args"], ["run", "-i", "--rm", "ghcr.io/acme/srv:2.0"])
        self.assertEqual(dropped["env"], {})

    def test_docker_bare_env_flag_passes_variable_on(self):
        package = {"registryType": "oci", "identifier": "ghcr.io/acme/srv:2.0", "transport": {"type": "stdio"},
                   "runtimeArguments": [{"type": "named", "name": "-e", "value": "HOME_DIR"},
                                        {"type": "named", "name": "--env", "value": "LEVEL=debug"}]}
        option = mcp_registry.option_from_package(package)
        self.assertEqual(option.entry["args"],
                         ["run", "-i", "--rm", "-e", "LEVEL", "-e", "HOME_DIR", "ghcr.io/acme/srv:2.0"])
        self.assertEqual(option.entry["env"], {"LEVEL": "debug"})

    def test_secret_on_command_line_found(self):
        package = {"registryType": "npm", "identifier": "@acme/srv", "transport": {"type": "stdio"},
                   "packageArguments": [{"type": "named", "name": "--api-key", "value": "{key}",
                                         "variables": {"key": {"isSecret": True, "isRequired": True}}}]}
        option = mcp_registry.option_from_package(package)
        self.assertEqual(mcp_registry.secret_in_args(option.entry, ["key"]), ["key"])
        oci = mcp_registry.option_from_package(
            {"registryType": "oci", "identifier": "ghcr.io/acme/srv:2.0", "transport": {"type": "stdio"},
             "runtimeArguments": [{"type": "named", "name": "-e", "value": "TOKEN={token}",
                                   "variables": {"token": {"isSecret": True}}}]})
        self.assertEqual(mcp_registry.secret_in_args(oci.entry, ["token"]), [])

    def test_required_secret_argument_without_value_stays_secret(self):
        # A required package argument marked isSecret with no value/default: its field is secret, so
        # the form's guard refuses it on the command line (it would be readable in /proc/<pid>/cmdline).
        package = {"registryType": "npm", "identifier": "@acme/srv", "transport": {"type": "stdio"},
                   "packageArguments": [{"type": "named", "name": "--api-key", "isRequired": True, "isSecret": True,
                                         "description": "API key"},
                                        {"type": "positional", "isRequired": True, "valueHint": "folder"}]}
        option = mcp_registry.option_from_package(package)
        self.assertEqual(option.entry["args"], ["-y", "@acme/srv", "--api-key", "{arg:0}", "{arg:1}"])
        self.assertEqual([(n.name, n.required, n.secret) for n in option.needs],
                         [("arg:0", True, True), ("arg:1", True, False)])
        secrets = [n.name for n in option.needs if n.secret]
        self.assertEqual(mcp_registry.secret_in_args(option.entry, secrets), ["arg:0"])

    def test_option_like_identifier_refused(self):
        for ident in ("--registry=https://evil.example", "-p"):
            self.assertIsNone(mcp_registry.option_from_package(
                {"registryType": "npm", "identifier": ident, "transport": {"type": "stdio"}}))

    def test_publisher(self):
        self.assertEqual(mcp_registry.publisher("io.github.acme/notes"), ("github", "acme"))
        self.assertEqual(mcp_registry.publisher("com.microsoft/playwright"), ("domain", "microsoft.com"))
        self.assertEqual(mcp_registry.publisher("weird name/x"), ("", ""))


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
