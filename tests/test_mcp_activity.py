import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import mcp_activity  # noqa: E402
import traffic  # noqa: E402

STDIO = {"transport": "stdio", "command": "/opt/pdfcraft/bin/pdfcraft-cli", "args": ["mcp", "--root", "/home/u/Docs"]}
NPX = {"transport": "stdio", "command": "npx", "args": ["-y", "chrome-devtools-mcp@latest"]}
HTTP = {"transport": "http", "url": "https://mcp.example.com/mcp"}


class MatchTest(unittest.TestCase):
    def test_configured_command_is_found_by_its_arguments(self):
        self.assertTrue(mcp_activity.matches(STDIO, ["/opt/pdfcraft/bin/pdfcraft-cli", "mcp", "--root", "/home/u/Docs"]))
        self.assertFalse(mcp_activity.matches(STDIO, ["/opt/pdfcraft/bin/pdfcraft-cli", "gui"]))
        self.assertFalse(mcp_activity.matches(STDIO, ["/usr/bin/other", "mcp", "--root", "/home/u/Docs"]))
        # npm rewrites its title into one string; npx itself is not in it.
        self.assertTrue(mcp_activity.matches(NPX, ["npm exec chrome-devtools-mcp@latest"]))
        self.assertTrue(mcp_activity.matches({"command": "srv", "args": []}, ["/usr/bin/srv"]))
        self.assertFalse(mcp_activity.matches({"command": "srv", "args": []}, []))

    def test_server_processes_below_each_instance(self):
        pdf = ["/opt/pdfcraft/bin/pdfcraft-cli", "mcp", "--root", "/home/u/Docs"]
        processes = [(10, "claude", ["claude"]), (11, "pdfcraft-cli", pdf),
                     (12, "worker", ["pdfcraft-worker"]),
                     (13, "sh", ["sh", "-c", "npx -y chrome-devtools-mcp@latest"]),
                     (14, "npm", ["npm exec chrome-devtools-mcp@latest"]),
                     (20, "claude", ["claude"]), (21, "pdfcraft-cli", pdf), (30, "pdfcraft-cli", pdf)]
        parents = {10: 1, 11: 10, 12: 11, 13: 10, 14: 13, 20: 1, 21: 20, 30: 1}
        found = mcp_activity.server_processes([10, 20], {"pdf": STDIO, "chrome": NPX, "web": HTTP},
                                              processes, parents.get)
        self.assertEqual(sorted(map(sorted, found["pdf"])), [[11, 12], [21]])   # not 30: not this agent's
        self.assertEqual(found["chrome"], [[14]])
        self.assertNotIn("web", found)


class BusyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.proc = Path(self.tmp.name)

    def process(self, pid, ticks=0, chars=0, stdin="pipe:[1]"):
        folder = self.proc / str(pid)
        (folder / "fd").mkdir(parents=True, exist_ok=True)
        fields = ["S", "1"] + ["0"] * 9 + [str(ticks), "0", "0", "0"] + ["0"] * 10
        (folder / "stat").write_text(f"{pid} (srv) " + " ".join(fields))
        (folder / "io").write_text(f"rchar: {chars}\nwchar: 0\nsyscr: 1\nsyscw: 0\n")
        link = folder / "fd" / "0"
        if link.is_symlink():
            link.unlink()
        os.symlink(stdin, link)

    def test_in_use_while_it_talks_on_its_pipe_or_works(self):
        key = ("claude", "pdf")
        activity = mcp_activity.Activity()
        self.process(11, ticks=5, chars=100)
        self.process(12, ticks=0, chars=7, stdin="pipe:[2]")         # a helper with its own pipe
        with mock.patch.object(mcp_activity.agent_activity, "TICKS", 100):
            self.assertEqual(activity.stdio_busy({key: [[11, 12]]}, 0.0, str(self.proc)), set())   # first reading
            self.assertEqual(activity.stdio_busy({key: [[11, 12]]}, 2.0, str(self.proc)), set())   # idle
            self.process(12, ticks=0, chars=99, stdin="pipe:[2]")     # the helper's own I/O: not a call
            self.assertEqual(activity.stdio_busy({key: [[11, 12]]}, 4.0, str(self.proc)), set())
            self.process(11, ticks=5, chars=180)                       # request read, answer written
            self.assertEqual(activity.stdio_busy({key: [[11, 12]]}, 6.0, str(self.proc)), {key})
            self.process(12, ticks=20, chars=99, stdin="pipe:[2]")    # working on it: 15 ticks in 2 s
            self.assertEqual(activity.stdio_busy({key: [[11, 12]]}, 8.0, str(self.proc)), {key})
            self.assertEqual(activity.stdio_busy({key: [[11, 12]]}, 10.0, str(self.proc)), set())

    def test_url_server_in_use_while_its_connection_moves_data(self):
        key = ("claude", "web")
        activity = mcp_activity.Activity(resolve=lambda host, port, proto: [(0, 0, 0, "", ("203.0.113.5", 443))])
        owners = {50: ("claude", "claude"), 60: ("codex", "codex")}

        def sock(pid, remote, moved):
            return traffic.Sock(local="10.0.0.2:5000", remote=remote, pid=pid, comm="x", sent=moved, inode=str(pid))
        urls = {key: HTTP["url"]}
        self.assertEqual(activity.url_busy(urls, owners, [sock(50, "203.0.113.5:443", 10)], 0.0), set())
        self.assertEqual(activity.url_busy(urls, owners, [sock(50, "203.0.113.5:443", 10)], 2.0), set())
        self.assertEqual(activity.url_busy(urls, owners, [sock(50, "203.0.113.5:443", 90)], 4.0), {key})
        # Another address, another port or another agent's connection: not this server.
        for pid, remote in ((50, "198.51.100.1:443"), (50, "203.0.113.5:8443"), (60, "203.0.113.5:443")):
            activity.url_busy(urls, owners, [sock(pid, remote, 500)], 6.0)
            self.assertEqual(activity.url_busy(urls, owners, [sock(pid, remote, 900)], 8.0), set(), remote)
        self.assertEqual(mcp_activity.url_place("http://localhost:3000/mcp"), ("localhost", 3000))
        self.assertEqual(mcp_activity.url_place("not a url"), ("", 0))



class MapLightTest(unittest.TestCase):
    def test_light_runs_on_the_line_to_a_server_in_use(self):
        import graph
        import map_view
        g = graph.Graph()
        for node in (graph.Node("agent:claude", 1, "Claude", "", "agent"), graph.Node("mcp:pdf", 0, "pdf", "", "mcp"),
                     graph.Node("mcp:web", 0, "web", "", "mcp")):
            g.add(node)
        g.link("agent:claude", "mcp:pdf", "mcp")
        g.link("agent:claude", "mcp:web", "mcp")

        class View:
            mcp_paths = map_view.MapView.mcp_paths
        view = View()
        view.graph, view.mcp_busy = g, {("claude", "pdf")}
        paths = view.mcp_paths({0, 1})
        self.assertEqual([(key, [e.target for e in edges]) for key, edges in paths], [("mcp:claude:pdf", ["mcp:pdf"])])
        self.assertEqual(view.mcp_paths({1}), [])          # its line not drawn (MCP layer hidden)


if __name__ == "__main__":
    unittest.main()
