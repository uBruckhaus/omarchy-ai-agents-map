import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ai_core  # noqa: E402
import terminal_pane  # noqa: E402

SESSION = {"CLAUDECODE": "1", "CLAUDE_CODE_CHILD_SESSION": "1", "CLAUDE_CODE_MESSAGING_TOKEN": "secret",
           "CLAUDE_CODE_SESSION_ID": "x", "CLAUDE_PID": "4", "CLAUDE_EFFORT": "high",
           "AI_AGENTS_MAP_NO_PROVIDERS": "1", "AI_AGENT": "claude-code_2-1-308_agent", "PI_CODING_AGENT": "true",
           "CODEX_THREAD_ID": "t", "OPENCODE": "1"}
KEEP = {"HOME": "/home/u", "PATH": "/usr/bin", "CLAUDE_CONFIG_DIR": "/home/u/.claude", "ANTHROPIC_MODEL": "opus",
        "OPENCODE_CONFIG": "/home/u/oc.json"}


class AgentSessionEnvTest(unittest.TestCase):
    """Started from inside an agent's session (a restart from its terminal), AI Agents Map must not
    pass that session's markers on: an agent started here would think it runs inside it."""

    def test_markers_are_dropped_and_the_rest_kept(self):
        env = {**SESSION, **KEEP}
        removed = ai_core.drop_agent_session_vars(env)
        self.assertEqual(set(removed), set(SESSION))
        self.assertEqual(env, KEEP)

    def test_built_in_terminal_starts_agents_without_them(self):
        child = dict(item.split("=", 1) for item in terminal_pane.child_env({**SESSION, **KEEP}))
        self.assertFalse(set(SESSION) & set(child))
        self.assertEqual(child["CLAUDE_CONFIG_DIR"], "/home/u/.claude")
        self.assertEqual(child["TERM"], "xterm-256color")


if __name__ == "__main__":
    unittest.main()
