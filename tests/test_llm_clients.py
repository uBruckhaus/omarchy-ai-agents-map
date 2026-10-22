import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import llm_clients  # noqa: E402
import palette  # noqa: E402

HEADER = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"


def tcp_line(local_port: int, remote_port: int, state: str, uid: int, inode: str) -> str:
    return (f"   0: 0100007F:{local_port:04X} 0100007F:{remote_port:04X} {state} 00000000:00000000 "
            f"00:00000000 00000000 {uid:5d}        0 {inode} 1 0000000000000000 20 4 30 10 -1\n")


class LlmClientsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.net, self.proc, self.plugins = root / "net", root / "proc", root / "plugins"
        for folder in (self.net, self.proc, self.plugins):
            folder.mkdir()
        for name, value in (("PLUGINS", str(self.plugins)), ("APP_DIR", str(root / "app"))):
            patcher = mock.patch.object(llm_clients, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        llm_clients._manifests.clear()
        self.uid = os.getuid()

    def process(self, pid: int, argv, parent: int = 1, sockets=()):
        folder = self.proc / str(pid)
        (folder / "fd").mkdir(parents=True)
        (folder / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
        (folder / "stat").write_text(f"{pid} (x) S {parent} 0 0")
        for number, inode in enumerate(sockets):
            os.symlink(f"socket:[{inode}]", folder / "fd" / str(number + 3))

    def plugin(self, plugin_id: str, name: str):
        (self.plugins / plugin_id).mkdir()
        (self.plugins / plugin_id / "manifest.json").write_text(json.dumps({"name": name}))

    def test_only_own_established_connections_to_the_port(self):
        (self.net / "tcp").write_text(HEADER + tcp_line(51000, 8080, "01", self.uid, "111")
                                      + tcp_line(51001, 8080, "06", self.uid, "0")           # TIME_WAIT
                                      + tcp_line(51002, 8080, "01", self.uid + 1, "222")     # another user
                                      + tcp_line(51003, 9999, "01", self.uid, "333")         # another port
                                      + tcp_line(8080, 0, "0A", self.uid, "444"))            # the listener
        self.assertEqual(llm_clients.client_inodes([8080], str(self.net), self.uid), {8080: {"111"}})

    def test_plugin_named_by_manifest_also_for_its_helper(self):
        self.plugin("ubruckhaus.video-trans", "Video Trans")
        backend = f"{self.plugins}/ubruckhaus.video-trans/backend.py"
        self.process(50, ["python3", "-u", backend])
        self.process(51, ["curl", "-s", "http://127.0.0.1:8080/health"], parent=50)
        self.assertEqual(llm_clients.app_name(50, proc=str(self.proc)), "Video Trans")
        self.assertEqual(llm_clients.app_name(51, proc=str(self.proc)), "Video Trans")

    def test_an_app_is_not_named_after_its_launcher(self):
        self.plugin("ubruckhaus.simple-launcher", "App Manager")
        self.process(60, ["python3", f"{self.plugins}/ubruckhaus.simple-launcher/launcher.py"])
        self.process(61, ["/usr/bin/open-webui", "serve"], parent=60)
        self.assertEqual(llm_clients.app_name(61, proc=str(self.proc)), "open-webui")

    def test_agents_monitors_and_this_app(self):
        self.plugin("ubruckhaus.llm-usage", "LLM Monitor")
        self.process(70, ["node", "/home/u/.local/bin/hermes"])
        self.process(71, ["python3", f"{self.plugins}/ubruckhaus.llm-usage/scripts/llm-monitor.py"])
        self.process(72, ["python3", f"{llm_clients.APP_DIR}/provider_watch.py"])
        self.assertEqual(llm_clients.app_name(70, {"hermes": "Hermes"}, str(self.proc)), "Hermes")
        self.assertIsNone(llm_clients.app_name(71, proc=str(self.proc)))
        self.assertIsNone(llm_clients.app_name(72, proc=str(self.proc)))

    def test_clients_remembered_then_forgotten(self):
        self.plugin("ubruckhaus.video-trans", "Video Trans")
        self.process(50, ["python3", f"{self.plugins}/ubruckhaus.video-trans/backend.py"], sockets=["111"])
        (self.net / "tcp").write_text(HEADER + tcp_line(51000, 8080, "01", self.uid, "111"))
        clients = llm_clients.Clients()
        clients.update({"llamacpp": 8080}, net=str(self.net), proc=str(self.proc), now=100.0)
        (self.net / "tcp").write_text(HEADER)          # the request is over
        clients.update({"llamacpp": 8080}, net=str(self.net), proc=str(self.proc), now=103.0)
        self.assertEqual(clients.recent("llamacpp", now=103.0), [("Video Trans", 3.0)])
        self.assertTrue(clients.active("llamacpp", now=103.0))
        self.assertFalse(clients.active("llamacpp", now=100.0 + llm_clients.ACTIVE + 1))
        clients.update({"llamacpp": 8080}, net=str(self.net), proc=str(self.proc), now=100.0 + llm_clients.KEEP + 1)
        self.assertEqual(clients.recent("llamacpp"), [])

    def test_outside_colour_is_cyan(self):
        # Magenta (violet) marks agents on different models of one local LLM (map_view.model_tints).
        colors = {"accent": "#f38d70", "green": "#adda78", "yellow": "#f9cc6c", "red": "#fd6883",
                  "magenta": "#a8a9eb", "cyan": "#85dacc"}
        self.assertEqual(palette.outside_color(colors), "#85dacc")
        self.assertEqual(palette.outside_color({}), palette.DEFAULT_COLORS["cyan"])

    def test_plugin_ids_are_plain(self):
        self.assertEqual(llm_clients._plugin_of([f"{self.plugins}/../../.ssh/x"]), "")
        self.assertEqual(llm_clients._plugin_of([f"{self.plugins}/../x.py"]), "")
        self.assertEqual(llm_clients._plugin_of([f"{self.plugins}/ubruckhaus.video-trans/b.py"]),
                         "ubruckhaus.video-trans")

    def test_names_per_runtime_capped(self):
        clients = llm_clients.Clients()
        with clients.lock:
            clients.seen["llamacpp"] = {f"app{i}": 100.0 + i for i in range(llm_clients.MAX_NAMES)}
        for i in range(5):
            self.process(80 + i, [f"/usr/bin/extra{i}"], sockets=[str(500 + i)])
        (self.net / "tcp").write_text(HEADER + "".join(tcp_line(52000 + i, 8080, "01", self.uid, str(500 + i))
                                                      for i in range(5)))
        clients.update({"llamacpp": 8080}, net=str(self.net), proc=str(self.proc), now=120.0)
        names = [n for n, _age in clients.recent("llamacpp", now=120.0)]
        self.assertEqual(len(names), llm_clients.MAX_NAMES)
        self.assertTrue({f"extra{i}" for i in range(5)} <= set(names))


if __name__ == "__main__":
    unittest.main()


class BusyGateTest(unittest.TestCase):
    """Connections are only read while the GPU is busy, or after one was seen in this update."""

    process = LlmClientsTest.process

    def setUp(self):
        LlmClientsTest.setUp(self)
        self.reads = 0
        original = llm_clients.client_inodes

        def counted(*args, **kwargs):
            self.reads += 1
            return original(*args, **kwargs)
        patcher = mock.patch.object(llm_clients, "client_inodes", counted)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.process(50, ["myapp"], sockets=["111"])
        (self.net / "tcp").write_text(HEADER + tcp_line(51000, 8080, "01", self.uid, "111"))

    def update(self, busy_values, gate=20):
        values = iter(busy_values)
        clients = llm_clients.Clients()
        peak = clients.update({"llamacpp": 8080}, net=str(self.net), proc=str(self.proc), now=100.0,
                              samples=len(busy_values), interval=0, probe=lambda: next(values), busy=gate)
        return clients, peak

    def test_idle_gpu_reads_no_connections(self):
        clients, peak = self.update([0, 3, 5, 0])
        self.assertEqual(self.reads, 0)
        self.assertEqual(peak, 5)                      # the GPU's peak is still reported
        self.assertEqual(clients.recent("llamacpp", now=100.0), [])

    def test_busy_gpu_reads_and_keeps_reading_after_a_find(self):
        clients, _peak = self.update([0, 60, 0, 0])
        self.assertEqual(self.reads, 3)                # from the busy sample on, also once it falls quiet
        self.assertEqual(clients.recent("llamacpp", now=100.0), [("myapp", 0.0)])

    def test_without_a_gate_or_a_gpu_reading_every_sample_reads(self):
        self.update([0, 0, 0], gate=None)
        self.assertEqual(self.reads, 3)
        self.reads = 0
        self.update([None, None], gate=20)             # no sysfs busy percent (another GPU)
        self.assertEqual(self.reads, 2)
