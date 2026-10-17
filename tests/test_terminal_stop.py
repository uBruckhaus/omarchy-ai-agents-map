import os
import signal
import subprocess
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    import terminal_pane  # noqa: E402
except Exception:            # no GTK here
    terminal_pane = None


@unittest.skipIf(terminal_pane is None, "GTK not available")
class StopTest(unittest.TestCase):
    def test_stop_ends_the_agent_and_what_it_started(self):
        # An "agent" leading its own session, with a child of its own (as pi with its MCP servers).
        agent = subprocess.Popen(["sh", "-c", "sleep 60 & echo $!; wait"], stdout=subprocess.PIPE,
                                 start_new_session=True, text=True)
        child = int(agent.stdout.readline())
        terminal_pane.TerminalPane.signal_group(agent.pid, signal.SIGTERM)
        agent.wait(timeout=5)
        for _ in range(50):
            try:
                os.kill(child, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            self.fail("the agent's child is still running")


if __name__ == "__main__":
    unittest.main()
