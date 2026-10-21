"""An agent's network traffic (traffic.py): reading ss, adding up per agent, destination and program."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import traffic  # noqa: E402

SS = """ESTAB 0      0     192.168.178.22:40048   160.79.104.10:443  users:(("claude",pid=100,fd=18)) ino:5001 sk:1
\t bbr rto:216 bytes_sent:5000 bytes_acked:5001 bytes_received:40000 segs_out:61
ESTAB 0      0     127.0.0.1:51000   127.0.0.1:8080  users:(("node",pid=101,fd=21)) ino:5002 sk:2
\t cubic bytes_sent:300 bytes_received:9000
ESTAB 0      0     127.0.0.1:8080   127.0.0.1:51000  users:(("llama-server",pid=300,fd=9)) ino:5003 sk:3
\t cubic bytes_sent:9000 bytes_received:300
ESTAB 0      0     192.168.178.22:46566   194.25.134.51:993  users:(("omamail",pid=200,fd=10)) ino:5004 sk:4
\t bbr bytes_sent:923 bytes_received:9429
"""


class Names(traffic.Names):
    """Without the network: one known API address, no reverse lookups."""

    def __init__(self):
        super().__init__()
        self.known = {"160.79.104.10": "Anthropic API"}
        self.known_at = 1.0

    def lookup_later(self, address):
        self.reverse[address] = ""


class TrafficTest(unittest.TestCase):
    def test_parse_ss(self):
        socks = traffic.parse_ss(SS)
        self.assertEqual([(s.comm, s.pid, s.sent, s.received, s.inode) for s in socks][:2],
                         [("claude", 100, 5000, 40000, "5001"), ("node", 101, 300, 9000, "5002")])
        self.assertEqual(len(socks), 4)
        self.assertEqual(traffic.split_address("[::ffff:1.2.3.4]:443"), ("1.2.3.4", 443))
        self.assertEqual(traffic.split_address("[::1]:8080"), ("::1", 8080))

    def test_the_agent_and_what_it_started(self):
        processes = [(100, "claude", ["/usr/bin/claude"]), (101, "node", ["/usr/bin/node", "server.js"]),
                     (300, "llama-server", ["/x/llama-server"]), (200, "omamail", ["/x/omamail"])]
        parents = {100: 1, 101: 100, 300: 1, 200: 1}
        owners = traffic.agent_processes({"claude": [100]}, processes, parents.get)
        self.assertEqual(owners, {100: ("claude", "claude"), 101: ("claude", "node")})

    def test_meter_adds_what_moved_since_the_last_look(self):
        meter = traffic.Meter(Names())
        owners = {100: ("claude", "claude"), 101: ("claude", "node")}
        llms = {8080: "llama.cpp"}
        socks = traffic.parse_ss(SS)
        meter.update(socks, owners, llms, now=10.0)
        stats = meter.stats["claude"]
        self.assertEqual((stats.received, stats.sent, stats.connections), (49000, 5300, 2))
        self.assertEqual(stats.places[("Anthropic API", "api")].received, 40000)
        self.assertEqual(stats.places[("llama.cpp", "local-llm")].received, 9000)
        self.assertEqual(stats.programs["node"].sent, 300)
        socks[0].received += 3000                      # 3 s later: 3 KB more from Anthropic
        meter.update(socks, owners, llms, now=13.0)
        self.assertEqual(stats.received, 52000)
        self.assertEqual(stats.rate, (1000.0, 0.0))
        meter.update([], owners, llms, now=16.0)      # the connections are gone: nothing more counted
        self.assertEqual((stats.received, stats.connections, stats.rate), (52000, 0, (0.0, 0.0)))

    def test_places(self):
        names = Names()
        self.assertEqual(names.place("127.0.0.1:8080", {8080: "llama.cpp"}), ("llama.cpp", "local-llm"))
        self.assertEqual(names.place("127.0.0.1:9000", {}), ("localhost:9000", "local"))
        self.assertEqual(names.place("192.168.178.63:8009", {}), ("192.168.178.63", "lan"))
        self.assertEqual(names.place("160.79.104.10:443", {}), ("Anthropic API", "api"))
        self.assertEqual(names.place("8.8.8.8:443", {}), ("8.8.8.8", "web"))

    def test_texts(self):
        self.assertEqual(traffic.size_text(1536), "1.5 KB")
        self.assertEqual(traffic.rate_text(0), "0 B/s")


if __name__ == "__main__":
    unittest.main()
