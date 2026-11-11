"""HANCORE review of 3452749: the map's model actions (click a model, drag a model line, attach
selected agents, switch an online model) obey the user's deny rules before anything is written."""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    import connect_page  # noqa: E402
except Exception:            # no GTK here
    connect_page = None
import action_rules  # noqa: E402

DENY_ALL = [{"effect": "deny", "action": "*", "category": "*", "item": "*", "agent": "*"}]
ALLOW_HERMES = DENY_ALL + [{"effect": "allow", "action": "connect", "category": "*", "item": "*", "agent": "hermes"}]


@unittest.skipIf(connect_page is None, "GTK not available")
class MapModelRulesTest(unittest.TestCase):
    def page(self, rules):
        cls = next(v for v in vars(connect_page).values()
                   if isinstance(v, type) and hasattr(v, "model_change_allowed"))
        page = cls.__new__(cls)
        page.toasts = []

        def rule_allows(action, category, item="", agent=""):
            text = action_rules.refusal(rules, True, action, category, item, agent)
            if text:
                page.toasts.append(text)
            return text is None
        page.rule_allows = rule_allows
        page.show_note = lambda *a: None
        page.agent_list = [SimpleNamespace(command="hermes", name="Hermes"), SimpleNamespace(command="pi", name="Pi")]
        page.inventory = SimpleNamespace(runtimes=[SimpleNamespace(key="llamacpp", name="llama.cpp", models=[],
                                                                   address="127.0.0.1:8080")])
        return page

    def test_deny_all_blocks_every_model_route(self):
        page = self.page(DENY_ALL)
        runtime = page.inventory.runtimes[0]
        with mock.patch.object(connect_page.connect, "apply") as apply, \
                mock.patch.object(connect_page.connect, "plan") as plan, \
                mock.patch.object(connect_page.connect, "SUPPORTED", {"hermes", "pi"}):
            page.on_map_pick_model("agent:hermes", "runtime:llamacpp", "qwen")
            page.attach_apply(page.agent_list, runtime, "qwen")
            page.remodel_apply(page.agent_list, runtime, "qwen", "Hermes, Pi", "old")
            page.remodel_apply(page.agent_list, runtime, None, "Hermes, Pi", "old")        # detach
            page.switch_online(page.agent_list, "gpt", "cloud:openai")
        plan.assert_not_called()
        apply.assert_not_called()
        self.assertEqual(len(page.toasts), 5)

    def test_batch_refused_when_one_agent_is_denied(self):
        page = self.page(ALLOW_HERMES)
        self.assertTrue(page.model_change_allowed(page.agent_list[:1], "runtime:llamacpp", "qwen"))
        self.assertFalse(page.model_change_allowed(page.agent_list, "runtime:llamacpp", "qwen"))
        self.assertFalse(page.model_change_allowed(page.agent_list[:1], "runtime:llamacpp", None))   # disconnect

    def test_moving_off_a_model_needs_its_disconnect_rule(self):
        rules = action_rules.default_rules() + [
            {"effect": "deny", "action": "disconnect", "category": "models", "item": "old", "agent": "hermes"}]
        page = self.page(rules)
        self.assertFalse(page.model_change_allowed(page.agent_list[:1], "runtime:llamacpp", "new", "old"))
        self.assertFalse(page.model_change_allowed(page.agent_list[:1], "runtime:llamacpp", None, "old"))
        self.assertTrue(page.model_change_allowed(page.agent_list[1:], "runtime:llamacpp", "new", "old"))

    def test_the_model_in_use_needs_its_disconnect_rule_on_every_route(self):
        rules = action_rules.default_rules() + [
            {"effect": "deny", "action": "disconnect", "category": "models", "item": "old", "agent": "hermes"}]
        page = self.page(rules)
        with mock.patch.object(type(page), "models_used_by", lambda self, agent: {"old"} if agent.command == "hermes" else set()):
            self.assertFalse(page.model_change_allowed(page.agent_list[:1], "runtime:llamacpp", "new"))
            self.assertTrue(page.model_change_allowed(page.agent_list[:1], "runtime:llamacpp", "old"))   # stays
            self.assertTrue(page.model_change_allowed(page.agent_list[1:], "runtime:llamacpp", "new"))

    def test_an_online_model_in_use_counts_too(self):
        import graph
        rules = action_rules.default_rules() + [
            {"effect": "deny", "action": "disconnect", "category": "models", "item": "gpt-5", "agent": "hermes"}]
        page = self.page(rules)
        g = graph.Graph()
        g.add(graph.Node("agent:hermes", 1, "Hermes", kind="agent"))
        g.add(graph.Node("cloud:openai", 2, "OpenAI", kind="cloud"))
        g.add(graph.Node("model:cloud:openai:gpt-5", 3, "gpt-5", kind="model"))
        g.link("agent:hermes", "model:cloud:openai:gpt-5", "default")
        page.side_map = SimpleNamespace(graph=g)
        self.assertEqual(page.models_used_by(page.agent_list[0]), {"gpt-5"})
        with mock.patch.object(connect_page.connect, "apply") as apply:
            page.switch_online(page.agent_list[:1], "gpt-5-mini", "cloud:openai")
        apply.assert_not_called()

    def test_a_backup_provider_leaves_nothing_a_move_leaves_the_old_provider(self):
        rules = action_rules.default_rules() + [
            {"effect": "deny", "action": "disconnect", "category": "models", "item": "old", "agent": "hermes"},
            {"effect": "deny", "action": "disconnect", "category": "providers", "item": "ollama", "agent": "hermes"}]
        page = self.page(rules)
        hermes = page.agent_list[:1]
        with mock.patch.object(type(page), "models_used_by", lambda self, agent: {"old"}):
            self.assertTrue(page.model_change_allowed(hermes, "runtime:llamacpp", "new", switches=False))
            self.assertFalse(page.model_change_allowed(hermes, "runtime:llamacpp", "new", switches=True))
            self.assertFalse(page.model_change_allowed(hermes, "runtime:llamacpp", "old", switches=False,
                                                       replaces="ollama"))

    def test_allowed_by_default_rules(self):
        page = self.page(action_rules.default_rules())
        self.assertTrue(page.model_change_allowed(page.agent_list, "runtime:llamacpp", "qwen"))
        self.assertTrue(page.model_change_allowed(page.agent_list, "runtime:llamacpp", None))


if __name__ == "__main__":
    unittest.main()
