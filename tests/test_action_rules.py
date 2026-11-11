"""What may be done (action_rules, Maprisma task 019): the most specific rule decides, a tie allows."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import action_rules as r  # noqa: E402


def rule(effect, action="*", category="*", item="*", agent="*"):
    return {"effect": effect, "action": action, "category": category, "item": item, "agent": agent}


class RulesTest(unittest.TestCase):
    def test_defaults_allow_and_nothing_means_no(self):
        self.assertTrue(r.allows(r.default_rules(), "install", "models", "x"))
        self.assertFalse(r.allows([], "install", "models", "x"))

    def test_the_most_specific_rule_decides(self):
        rules = r.default_rules() + [rule("deny", "connect", "mcp", "fetch"),
                                     rule("allow", "connect", "mcp", "fetch", "claude")]
        self.assertFalse(r.allows(rules, "connect", "mcp", "fetch", "codex"))
        self.assertTrue(r.allows(rules, "connect", "mcp", "fetch", "claude"))
        self.assertTrue(r.allows(rules, "connect", "mcp", "fetch~2", "claude"))     # a second server of a name
        self.assertTrue(r.allows(rules, "connect", "mcp", "other", "codex"))

    def test_a_tie_allows_and_an_agent_rule_covers_the_agent(self):
        self.assertTrue(r.allows([rule("deny", "start"), rule("allow", "start")], "start", "agents", "pi"))
        rules = r.default_rules() + [rule("deny", agent="pi")]
        self.assertFalse(r.allows(rules, "update", "agents", "pi"))                  # the agent itself
        self.assertFalse(r.allows(rules, "connect", "mcp", "fetch", "pi"))

    def test_models_without_case(self):
        rules = r.default_rules() + [rule("deny", "remove", "models", "Qwen3:8B")]
        self.assertFalse(r.allows(rules, "remove", "models", "qwen3:8b"))

    def test_what_a_refusal_says(self):
        rules = r.default_rules() + [rule("deny", "install", "models")]
        self.assertIsNone(r.refusal(rules, True, "start", "agents", "pi"))
        self.assertIn("not allowed by your rules", r.refusal(rules, True, "install", "models", "x"))
        self.assertEqual(r.refusal(rules, False, "install", "models", "x"), "This is not allowed here.")

    def test_ask_ai_proposals_are_checked_as_what_they_do(self):
        link = {"action": {"kind": "map_link", "source": "agent:codex", "target": "mcp:fetch", "remove": True}}
        self.assertEqual(r.candidate_checks(link), [("disconnect", "mcp", "fetch", "codex")])
        download = {"action": {"kind": "model_download", "runtime": "ollama", "repo": "org/Q"}}
        self.assertEqual(r.candidate_checks(download), [("install", "models", "org/Q")])


if __name__ == "__main__":
    unittest.main()
