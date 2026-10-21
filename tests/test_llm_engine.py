"""Local LLMs through their provider's engine (llm_engine): a new provider is one add-on, the core
never asks for a provider by name."""

import ast
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import gpu_usage  # noqa: E402
import inventory  # noqa: E402
import link_check  # noqa: E402
import llm_engine  # noqa: E402
import providers  # noqa: E402
import runtime_ctl  # noqa: E402
from providers import Provider  # noqa: E402

# Core modules that work through engines only: no provider key may appear in them as a string.
# Later steps of moving provider knowledge into the providers add their modules here.
ENGINE_ONLY = ("llm_engine.py", "runtime_ctl.py", "gpu_usage.py", "link_check.py", "model_slots.py",
               "configure_ctl.py", "traffic.py", "connect.py", "connect_page.py", "model_folders.py",
               "model_move.py", "disk_usage.py", "health.py", "agent_status.py", "setup_page.py",
               "settings_pages.py", "models_page.py", "model_layout.py")


def string_constants(path: Path) -> set:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = {id(node.body[0].value) for node in ast.walk(tree)
                  if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef)) and node.body
                  and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant)}
    return {node.value for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings}


class NoProviderNamesInTheCore(unittest.TestCase):
    def test_engine_only_modules_name_no_provider(self):
        keys = {p.key for p in providers.built_in_local()}
        for name in ENGINE_ONLY:
            with self.subTest(name):
                self.assertEqual(string_constants(ROOT / name) & keys, set())


class FactsTest(unittest.TestCase):
    """The engines' facts say what the core's name lists said before (no change in behaviour)."""

    def keys(self, fact) -> set:
        return {p.key for p in providers.built_in_local() if getattr(llm_engine.of(p.key), fact)}

    def test_same_as_the_old_lists(self):
        self.assertEqual(self.keys("gguf_folder"), {"llamacpp", "lmstudio", "koboldcpp"})     # GGUF_RUNTIMES, MOVABLE
        self.assertEqual(self.keys("reorganize"), {"llamacpp", "lmstudio", "ollama", "koboldcpp"})
        self.assertEqual({p.key for p in providers.built_in_local()} - self.keys("models_page"), {"vllm"})
        self.assertEqual({p.key for p in providers.built_in_local()} - self.keys("own_model_folder"), {"jan"})
        self.assertEqual(self.keys("stop_to_move"), {"ollama"})
        self.assertEqual(self.keys("app_without_server"), {"lmstudio"})
        self.assertEqual(llm_engine.of("lmstudio").gguf_layout, "publisher")
        self.assertEqual(min(providers.built_in_local(), key=lambda p: llm_engine.of(p.key).models_root_rank).key,
                         "lmstudio")

    def test_models_page_same_as_before(self):
        of = llm_engine.of
        self.assertEqual({k for k in ("llamacpp", "lmstudio", "koboldcpp", "ollama", "jan", "vllm")
                          if of(k).download}, {"llamacpp", "lmstudio", "koboldcpp", "ollama"})
        self.assertEqual(of("ollama").download, "pull")
        self.assertEqual(of("ollama").removable, "api")
        self.assertEqual(self.keys("removable"), {"llamacpp", "lmstudio", "koboldcpp", "ollama"})
        self.assertEqual(self.keys("context_settable"), {"llamacpp", "lmstudio", "koboldcpp", "ollama", "jan"})
        self.assertEqual({k: of(k).context_effect for k in ("llamacpp", "jan", "lmstudio", "koboldcpp", "ollama")},
                         {"llamacpp": "restart", "jan": "restart", "lmstudio": "next-load",
                          "koboldcpp": "next-load", "ollama": ""})
        self.assertTrue(of("ollama").models_page_note(None))
        self.assertIsNone(of("vllm").context_now(None, None, "x"))

    def test_a_new_provider_gets_careful_defaults(self):
        engine = llm_engine.Engine()
        self.assertEqual((engine.models_page, engine.own_model_folder, engine.gguf_folder, engine.reorganize,
                          engine.stop_to_move), (True, True, False, False, False))
        self.assertEqual((engine.download, engine.removable, engine.context_settable), ("", "", False))
        self.assertIsNone(engine.context_changes(None, "x", 4096))
        with self.assertRaises(ValueError):
            engine.pull_name(None, "a/b", None, "")


class NewProviderTest(unittest.TestCase):
    """A provider the app has never heard of, as an add-on would bring it."""

    def setUp(self):
        self.addCleanup(providers.remove_added, "test")

    def runtime(self, key, **kw):
        return inventory.Runtime(key, key.title(), True, False, address="127.0.0.1:1", **kw)

    def test_without_an_engine_it_gets_the_defaults(self):
        providers.add(Provider("newllm", "NewLLM", "local", connect_key="newllm-local"), "test")
        rt = self.runtime("newllm", service="newllm")
        self.assertIs(llm_engine.of(rt), llm_engine.DEFAULT)
        self.assertTrue(runtime_ctl.can_control(rt))
        with mock.patch.object(llm_engine, "_run", return_value=(True, "")) as run:
            runtime_ctl.start(rt)
            runtime_ctl.unload(rt)           # cannot unload: stops it
        self.assertEqual([c.args[0] for c in run.call_args_list],
                         [["systemctl", "--user", "start", "newllm.service"],
                          ["systemctl", "--user", "stop", "newllm.service"]])
        with mock.patch.object(gpu_usage, "http_json", return_value={"data": [{"id": "Qwen3"}]}):
            self.assertEqual(gpu_usage.loaded_models(rt), {"qwen3": "loaded"})
        self.assertEqual(runtime_ctl.load(rt, "qwen3"), (False, "it cannot be asked to load a model"))

    def test_its_own_engine_is_asked(self):
        class Engine(llm_engine.LlamaRouter):
            programs = ("newllm-server",)
            start_command = "newllm-server --serve"

            def add_model_step(self, rt):
                return "pull one with newllm pull."

        providers.add(Provider("newllm", "NewLLM", "local", connect_key="newllm-local", engine=Engine()), "test")
        self.assertEqual(gpu_usage.runtime_of("newllm-server", ["/opt/newllm/newllm-server"]), "newllm")
        rt = self.runtime("newllm")
        with mock.patch.object(link_check.connect, "is_connected", lambda a, k: True):
            hints = link_check.check_provider("pi", "Pi", rt, "", with_agents=True)
        self.assertEqual([h["level"] for h in hints], ["error", "error"])
        self.assertIn("newllm pull", hints[0]["text"])
        self.assertIn("newllm-server --serve", hints[1]["text"])
        with mock.patch.object(gpu_usage, "http_json", return_value={}) as http, \
                mock.patch.object(llm_engine, "_port", return_value=1):
            runtime_ctl.unload_model(rt, "qwen3")
        http.assert_called_once_with(1, "/models/unload", "POST", {"model": "qwen3"}, timeout=30)

    def test_an_add_on_made_before_engines_keeps_the_apps_engine(self):
        built_in = next(p for p in providers.built_in_local() if p.key == "ollama")
        old = SimpleNamespace(key="ollama", engine=None)
        with mock.patch.object(providers, "get", return_value=old):
            self.assertIs(llm_engine.of("ollama"), built_in.engine)


class WhoseProcessTest(unittest.TestCase):
    def test_processes(self):
        cases = (("llama-server", ["/usr/bin/llama-server", "--port", "8080"], "llamacpp"),
                 ("llama-server", ["/home/u/.lmstudio/extensions/backends/x/llama-server"], "lmstudio"),
                 ("lm-studio", ["/opt/lm-studio/lm-studio", "--run-as-service"], "lmstudio"),
                 ("jan-llama-worke", ["/x/jan-llama-worker", "--port", "6767"], "jan"),
                 ("koboldcpp", ["/x/koboldcpp", "--admin"], "koboldcpp"),
                 ("ollama", ["/x/ollama", "serve"], "ollama"),
                 ("python3", ["/venv/bin/python3", "/venv/bin/vllm", "serve", "m"], "vllm"),
                 ("bash", ["/usr/bin/bash"], ""))
        for comm, argv, key in cases:
            with self.subTest(argv[0]):
                self.assertEqual(gpu_usage.runtime_of(comm, argv), key)


    def test_a_runner_belongs_to_the_llm_that_started_it(self):
        # pid 10 Ollama → 11 its runner (named like llama.cpp) → 12 a thread-like child; 20 a llama-server
        procs = [(10, "ollama", ["/x/ollama", "serve"]), (11, "llama-runner", ["/x/lib/llama-runner"]),
                 (12, "worker", ["/x/worker"]), (20, "llama-server", ["/usr/bin/llama-server"])]
        ppid = {10: 1, 11: 10, 12: 11, 20: 1}
        stat = lambda path, _n: f"{path} (x) S {ppid[int(path.split('/')[2])]} 0"  # noqa: E731
        with mock.patch.object(gpu_usage, "_read", stat):
            groups = gpu_usage.runtime_pids(procs)
        self.assertEqual(groups, {"ollama": {10, 11, 12}, "llamacpp": {20}})

if __name__ == "__main__":
    unittest.main()
