import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import agent_details as ad  # noqa: E402
import agents  # noqa: E402
import extra_folders  # noqa: E402
import graph  # noqa: E402
import inventory as invmod  # noqa: E402
import map_view  # noqa: E402
import mcp_scan  # noqa: E402
import skills_scan  # noqa: E402

MCP_BYTES = b"\x7fELF....protocolVersion....notifications/initialized....tools/list...."


class AvailableOnTheMapTest(unittest.TestCase):
    """What is on this PC but not connected: a faded box without lines, to draw a line to."""

    def build(self, available):
        claude = agents.Agent(command="claude", name="Claude Code", icon="c", installed=True)
        inv = invmod.Inventory(
            mcp=[invmod.McpServer("github", "command", "gh", ["claude"]),
                 invmod.McpServer("docs", "url", "example.com", [])],
            agent_mcp={"claude": ["github"]})
        return graph.build([claude], {"claude": ad.Details(model="opus")}, inv, available=available)

    def test_faded_boxes_without_lines(self):
        g = self.build([("mcp", "photocraft", "photocraft", "not connected"),
                        ("mcp", "docs", "docs", "not connected"),
                        ("mcp", "github", "github", "not connected"),       # connected: stays as it is
                        ("skills", "/data/Skills", "/data/Skills", "3 skills")])
        nodes = {n.id: n for n in g.nodes}
        self.assertTrue(nodes["mcp:photocraft"].dim)
        self.assertTrue(nodes["skills:/data/Skills"].dim)
        self.assertFalse(nodes["mcp:github"].dim)
        self.assertEqual(nodes["mcp:photocraft"].column, 0)
        lined = {e.source for e in g.edges} | {e.target for e in g.edges}
        self.assertNotIn("mcp:photocraft", lined)
        self.assertNotIn("skills:/data/Skills", lined)

    def test_online_servers_get_their_own_card(self):
        g = self.build([("mcp", "docs", "docs", "not connected"), ("mcp", "photocraft", "photocraft", "")])
        nodes = {n.id: n for n in g.nodes}
        self.assertEqual(map_view.MapView.group_key(nodes["mcp:docs"]), "mcp-online")
        self.assertEqual(map_view.MapView.group_key(nodes["mcp:photocraft"]), "mcp")
        self.assertEqual(map_view.MapView.group_key(nodes["mcp:github"]), "mcp")

    def test_unused_layers_keep_unconnected_boxes(self):
        g = self.build([("mcp", "photocraft", "photocraft", "")])
        self.assertNotIn("mcp:photocraft", graph.hidden_by_layers(g, graph.LAYERS))


class AddedFoldersTest(unittest.TestCase):
    """Setup's Add folder…: the scans look in the folders added there too."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def added(self, kind_folders: dict):
        patcher = mock.patch.object(extra_folders, "folders", lambda kind: kind_folders.get(kind, []))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_programs_of_unpacked_apps_in_an_added_folder(self):
        shared = self.root / "McpServers"
        program = shared / "photocraft-0.16.0/usr/bin/photocraft-cli"
        program.parent.mkdir(parents=True)
        program.write_bytes(MCP_BYTES)
        program.chmod(0o755)
        self.added({"mcp": [shared]})
        self.assertIn(program, mcp_scan.own_programs())
        self.added({})
        self.assertNotIn(program, mcp_scan.own_programs())

    def test_skill_sets_in_an_added_folder(self):
        shared = self.root / "Skills"
        for name in ("json", "python"):
            (shared / name).mkdir(parents=True)
            (shared / name / "SKILL.md").write_text("---\nname: x\n---\n")
        with mock.patch.object(skills_scan, "ROOTS", []):
            self.added({"skills": [shared]})
            found = skills_scan.scan()
            self.assertEqual([(s.path, s.skills) for s in found], [(str(shared), ["json", "python"])])
            # A folder an agent reads already is not offered again.
            self.assertEqual(skills_scan.scan(known=[str(shared)]), [])

    def test_with_change_adds_once_and_drops(self):
        with mock.patch.object(extra_folders, "saved", lambda kind: ["/data/a"]):
            self.assertEqual(extra_folders.with_change("mcp", add=["/data/a", "/data/b"]), ["/data/a", "/data/b"])
            self.assertEqual(extra_folders.with_change("mcp", drop=["/data/a"]), [])

    def test_missing_folder_is_left_out(self):
        with mock.patch.object(extra_folders, "saved", lambda kind: [str(self.root), str(self.root / "gone")]):
            self.assertEqual(extra_folders.folders("skills"), [self.root])


class StartCommandTest(unittest.TestCase):
    def test_headless_when_the_default_is_a_running_app(self):
        command, sure = mcp_scan.command_for(Path("/x/vectorcraft-cli"),
                                             "vectorcraft-cli mcp [--connect ADDR | --headless]")
        self.assertTrue(sure)
        self.assertEqual(command[1:], ["mcp", "--headless"])

    def test_usage_glued_to_the_string_before_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            program = Path(tmp) / "deckcraft-cli"
            program.write_bytes(b"\x7fELF protocolVersion '{}'mcpmcp [--connect PORT] [--automation-read-root DIR]\n")
            with mock.patch.object(mcp_scan, "_save_cache"):
                count, usage = mcp_scan.scan_file(program, keep=False)
        self.assertEqual(count, 1)
        self.assertIn(b"--automation-read-root", usage)


if __name__ == "__main__":
    unittest.main()
