"""Models at once: agents on different models of one local LLM (model_slots, the engines' slots, the
map's colours)."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import graph  # noqa: E402
import inventory  # noqa: E402
import model_slots  # noqa: E402
import provider_llamacpp  # noqa: E402
from graph import Graph, Node  # noqa: E402

GIB = 2 ** 30


def two_agents(same: bool = False) -> Graph:
    """Pi and DeepSeek Harness on llama.cpp, each with its model (or both with the same one)."""
    g = Graph()
    for node in (Node("agent:pi", 0, "Pi", kind="agent"), Node("agent:dsh", 0, "DeepSeek Harness", kind="agent"),
                 Node("runtime:llamacpp", 1, "llama.cpp", kind="runtime"),
                 Node("model:runtime:llamacpp:big", 2, "big", kind="model"),
                 Node("model:runtime:llamacpp:small", 2, "small", kind="model")):
        g.add(node)
    for agent in ("agent:pi", "agent:dsh"):
        g.link(agent, "runtime:llamacpp", "uses")
    g.link("runtime:llamacpp", "model:runtime:llamacpp:big", "serves")
    g.link("runtime:llamacpp", "model:runtime:llamacpp:small", "serves")
    g.link("agent:pi", "model:runtime:llamacpp:big", "default")
    g.link("agent:dsh", "model:runtime:llamacpp:" + ("big" if same else "small"), "default")
    return g


class Engine:
    def __init__(self, kind="count", slots=1, fixed_by="", sizes=None):
        self.slots_kind, self._slots, self._fixed, self._sizes = kind, slots, fixed_by, sizes or {}

    def slots(self, rt):
        return self._slots

    def slots_fixed_by(self, rt):
        return self._fixed

    def model_size(self, rt, name):
        return self._sizes.get(name, 0)


RT = inventory.Runtime("llamacpp", "llama.cpp", True)


class PlanTest(unittest.TestCase):
    def test_models_in_use_per_local_llm(self):
        self.assertEqual(model_slots.models_in_use(two_agents()),
                         {"runtime:llamacpp": {"big": ["Pi"], "small": ["DeepSeek Harness"]}})
        self.assertEqual(model_slots.models_in_use(two_agents(same=True)),
                         {"runtime:llamacpp": {"big": ["Pi", "DeepSeek Harness"]}})

    def need(self, engine, same=False):
        return model_slots.plan([RT], model_slots.models_in_use(two_agents(same)), lambda rt: engine)[0]

    def test_the_app_raises_it_to_the_models_in_use(self):
        need = self.need(Engine(slots=1))
        self.assertEqual((need.count, need.change, need.swaps), (2, True, True))
        self.assertFalse(self.need(Engine(slots=2)).change)
        self.assertTrue(self.need(Engine(slots=2), same=True).change)     # back to one
        self.assertEqual(model_slots.hints(self.need(Engine(slots=1)), 24 * GIB), [])   # it is set, they fit

    def test_one_or_many_switch(self):
        self.assertTrue(self.need(Engine("switch", slots=1)).change)
        self.assertFalse(self.need(Engine("switch", slots=0)).change)
        self.assertFalse(self.need(Engine("switch", slots=1), same=True).change)

    def test_fixed_by_the_users_script_only_warns(self):
        need = self.need(Engine(slots=1, fixed_by="~/.local/bin/llama-server"))
        self.assertFalse(need.change)
        hints = model_slots.hints(need, 24 * GIB)
        self.assertEqual([h["level"] for h in hints], ["warning"])
        self.assertIn("~/.local/bin/llama-server", hints[0]["text"])
        self.assertIn("swaps", hints[0]["text"])

    def test_too_big_together_for_the_card(self):
        need = self.need(Engine(slots=2, sizes={"big": 16 * GIB, "small": 9 * GIB}))
        hints = model_slots.hints(need, 24 * GIB)
        self.assertEqual(len(hints), 1)
        self.assertIn("24 GB", hints[0]["text"])
        self.assertEqual(model_slots.hints(self.need(Engine(slots=2, sizes={"big": 16 * GIB, "small": 3 * GIB})),
                                           24 * GIB), [])


class StartCheckTest(unittest.TestCase):
    """Pi starts with "big" (16 GB) on a 24 GB card."""

    def usage(self, used, loaded=None, vram=0, other=0):
        import gpu_usage
        runtimes = {"llamacpp": gpu_usage.RuntimeUse(bool(loaded), vram, dict(loaded or {}))}
        if other:
            runtimes["ollama"] = gpu_usage.RuntimeUse(True, other, {"qwen3:8b": "loaded"})
        return gpu_usage.Usage(used, 24 * GIB, runtimes=runtimes)

    def check(self, usage, slots=1):
        engine = Engine(slots=slots, sizes={"big": 16 * GIB})
        rt = inventory.Runtime("llamacpp", "llama.cpp", True)
        ollama = inventory.Runtime("ollama", "Ollama", True)
        return model_slots.start_check("agent:pi", "Pi", two_agents(), [rt, ollama], usage, lambda r: engine)

    def test_too_little_free_memory(self):
        check = self.check(self.usage(14 * GIB, other=10 * GIB))
        self.assertFalse(check.fits)
        self.assertEqual(check.others, [("Ollama", 10 * GIB)])
        text = model_slots.start_text(check)
        self.assertIn("Pi uses big on llama.cpp", text)
        self.assertIn("Ollama 10 GB", text)

    def test_what_loading_it_unloads_counts_as_free(self):
        # llama.cpp holds "small" (9 GB) and keeps one model: loading "big" unloads it.
        self.assertTrue(self.check(self.usage(12 * GIB, {"small": "loaded"}, vram=9 * GIB)).fits)
        self.assertFalse(self.check(self.usage(12 * GIB, {"small": "loaded"}, vram=9 * GIB), slots=2).fits)

    def test_no_question_when_it_is_loaded_or_unknown(self):
        self.assertIsNone(self.check(self.usage(20 * GIB, {"big": "loaded"}, vram=18 * GIB)))
        self.assertIsNone(model_slots.start_check("agent:pi", "Pi", two_agents(), [], self.usage(1)))


class LlamaCppSlotsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        units = self.home / ".config/systemd/user"
        units.mkdir(parents=True)
        self.unit = units / "llama-server.service"
        self.env = self.home / "server.env"
        self.env.write_text("LLAMA_ARG_MODELS_DIR=/m\n")
        import install_ctl
        for patcher in (mock.patch.object(Path, "home", lambda: self.home),
                        mock.patch.object(install_ctl.LlamaCpp, "env_file", self.env),
                        mock.patch.object(os.path, "expanduser", lambda p: p.replace("~", str(self.home), 1))):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.rt = inventory.Runtime("llamacpp", "llama.cpp", True, False, service="llama-server")
        self.engine = provider_llamacpp.Engine()

    def test_the_apps_own_service_gets_the_number_in_its_env_file(self):
        self.unit.write_text("[Unit]\nDescription=llama.cpp model router (installed by AI Agents Map)\n[Service]\n"
                             "ExecStart=\"/x/llama-server\" --models-preset \"/p\" --models-max 1 --gpu-layers all\n")
        self.assertEqual((self.engine.slots(self.rt), self.engine.slots_fixed_by(self.rt)), (1, ""))
        changes = {c.path: c.new for c in self.engine.slot_changes(self.rt, 2)}
        self.assertIn("LLAMA_ARG_MODELS_MAX=2", changes[self.env])
        self.assertNotIn("--models-max", changes[self.unit])      # the command line would win over it

    def test_the_users_own_start_script_is_not_changed(self):
        script = self.home / "llama-server"
        script.write_text("#!/bin/sh\nexec /x/llama-server --models-max 1 --port 8080\n")
        self.unit.write_text(f"[Service]\nExecStart={script}\n")
        self.assertEqual(self.engine.slots(self.rt), 1)
        self.assertEqual(self.engine.slots_fixed_by(self.rt), str(script).replace(str(self.home), "~", 1))
        self.assertEqual(self.engine.slot_changes(self.rt, 2), [])


class TintsTest(unittest.TestCase):
    def test_different_models_one_violet_on_all_their_lines(self):
        import map_view
        g = two_agents()
        tints = map_view.MapView.model_tints(g)
        edge = {(e.source, e.target): i for i, e in enumerate(g.edges)}
        lines = [("runtime:llamacpp", "model:runtime:llamacpp:big"), ("runtime:llamacpp", "model:runtime:llamacpp:small"),
                 ("agent:pi", "runtime:llamacpp"), ("agent:dsh", "runtime:llamacpp")]
        self.assertEqual({tints[edge[line]] for line in lines}, {"magenta"})
        self.assertEqual(map_view.MapView.model_tints(two_agents(same=True)), {})


if __name__ == "__main__":
    unittest.main()
