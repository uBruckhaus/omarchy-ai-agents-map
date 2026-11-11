import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import isolate  # noqa: E402
import agent_details as ad  # noqa: E402
import agents  # noqa: E402
import graph  # noqa: E402
import connect  # noqa: E402
import inventory as invmod  # noqa: E402
import mcp_ctl  # noqa: E402
import mcp_library  # noqa: E402
import skills_ctl  # noqa: E402


class InventoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        isolate.desktop_in(self, self.home)
        expand = lambda p: str(p).replace("~", str(self.home), 1)  # noqa: E731
        for target, value in ((invmod, "HOME"), (ad, "HOME"), (connect, "HOME"), (mcp_ctl, "HOME"),
                              (skills_ctl, "HOME"), (mcp_library, "HOME")):
            patcher = mock.patch.object(target, value, self.home)
            patcher.start()
            self.addCleanup(patcher.stop)
        for patcher in (mock.patch.object(ad, "_expand", lambda p: Path(expand(p))),
                        mock.patch("os.path.expanduser", expand),
                        mock.patch.object(invmod, "own_processes", lambda: []),
                        mock.patch("shutil.which", lambda name: None)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def write(self, rel, text):
        path = self.home / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def agent(self, command, name):
        return agents.Agent(command=command, name=name, icon="x", installed=True)

    def test_ollama_models_from_unit_env_file(self):
        models = self.home / "models"
        (models / "manifests/registry.ollama.ai/library/qwen3").mkdir(parents=True)
        (models / "manifests/registry.ollama.ai/library/qwen3/8b").write_text("{}")
        (models / "manifests/hf.co/someone/model-gguf").mkdir(parents=True)
        (models / "manifests/hf.co/someone/model-gguf/Q4").write_text("{}")
        self.write("ollama.env", f"OLLAMA_MODELS={models}\nOLLAMA_HOST=127.0.0.1:11500\nSECRET_TOKEN=x\n")
        self.write(".config/systemd/user/ollama.service",
                   f"[Service]\nEnvironmentFile={self.home}/ollama.env\nExecStart=/usr/bin/ollama serve\n")
        rt = invmod.ollama([])
        self.assertTrue(rt.installed)
        self.assertEqual(rt.service, "ollama")
        self.assertEqual(rt.address, "127.0.0.1:11500")
        self.assertEqual(sorted(rt.models), ["hf.co/someone/model-gguf:Q4", "qwen3:8b"])

    def test_ollama_quoted_exec_start_and_manifests_v2(self):
        # The installer writes ExecStart="<path>" serve; newer Ollama pulls into manifests-v2.
        models = self.home / "models"
        (models / "manifests/registry.ollama.ai/library/qwen3").mkdir(parents=True)
        (models / "manifests/registry.ollama.ai/library/qwen3/8b").write_text("{}")
        (models / "manifests-v2/ollama.com/library/gemma4").mkdir(parents=True)
        (models / "manifests-v2/ollama.com/library/gemma4/latest").write_text("{}")
        (models / "manifests-v2/ollama.com/library/qwen3").mkdir(parents=True)
        (models / "manifests-v2/ollama.com/library/qwen3/8b").write_text("{}")
        self.write("ollama.env", f"OLLAMA_MODELS={models}\n")
        self.write(".config/systemd/user/ollama.service",
                   f'[Service]\nEnvironmentFile={self.home}/ollama.env\nExecStart="{self.home}/bin/ollama" serve\n')
        rt = invmod.ollama([])
        self.assertEqual(rt.service, "ollama")
        self.assertEqual(rt.models_dir, invmod.tilde(models))
        self.assertEqual(sorted(rt.models), ["gemma4:latest", "qwen3:8b"])

    def test_every_installers_service_is_found_while_stopped(self):
        # A provider freshly installed from the map is stopped: the map must still find its
        # service (to start it with an agent) and its model folder (to list its models).
        import install_ctl
        real = str(install_ctl.HOME)
        models = self.home / "models"
        (models / "manifests/registry.ollama.ai/library/qwen3").mkdir(parents=True)
        (models / "manifests/registry.ollama.ai/library/qwen3/8b").write_text("{}")
        (models / "Qwen-7B-Q4_K_M.gguf").write_text("")
        gpu = install_ctl.Gpu("amd", index=0)
        cases = ((install_ctl.Ollama(), invmod.ollama, True), (install_ctl.LlamaCpp(), invmod.llamacpp, True),
                 (install_ctl.Vllm(), invmod.vllm, False), (install_ctl.KoboldCpp(), invmod.koboldcpp, True),
                 (install_ctl.Jan(), invmod.jan, False))
        for installer, detect, has_folder in cases:
            with self.subTest(installer.key):
                unit = installer.unit_text(models).replace(real, str(self.home))
                self.write(f".config/systemd/user/{installer.service}.service", unit)
                env = installer.env_text(models, gpu)
                if env and installer.env_file != install_ctl.HOME:
                    self.write(str(installer.env_file.relative_to(install_ctl.HOME)), env)
                rt = detect([])
                self.assertEqual(rt.service, installer.service)
                self.assertTrue(rt.installed)
                self.assertFalse(rt.running)
                if has_folder:
                    self.assertEqual(rt.models_dir, invmod.tilde(models))

    def test_llamacpp_folder_and_presets_deduplicated(self):
        folder = self.home / "gguf"
        folder.mkdir()
        for name in ("Qwen-7B-Q4_K_M.gguf", "mmproj-Qwen.gguf", "notes.txt"):
            (folder / name).write_text("")
        self.write(".config/llama.cpp/models.ini", "[*]\nx=1\n[qwen-7b-q4_k_m]\n[other]\n")
        self.write(".config/systemd/user/llama-server.service",
                   f"[Service]\nEnvironment=LLAMA_ARG_MODELS_DIR={folder}\nExecStart=%h/.local/bin/llama-server\n")
        rt = invmod.llamacpp([])
        self.assertEqual(rt.models, ["Qwen-7B-Q4_K_M", "other"])
        self.assertEqual(rt.service, "llama-server")

    def test_mcp_never_takes_env_or_headers(self):
        self.write(".claude.json", json.dumps({"mcpServers": {
            "web": {"command": "npx", "args": ["-y", "some-mcp@1"], "env": {"API_KEY": "secret"}},
            "remote": {"url": "https://mcp.example.com/sse?token=secret", "headers": {"Authorization": "secret"}},
            "off": {"command": "x", "disabled": True}}}))
        found = invmod.mcp_for("claude")
        self.assertEqual([f[:3] for f in found],
                         [("web", "command", "npx some-mcp@1"), ("remote", "url", "mcp.example.com")])
        self.assertNotIn("secret", repr(found))

    def test_skill_sets_follow_links(self):
        shared = self.home / ".agents/skills"
        (shared / "one").mkdir(parents=True)
        (shared / "one/SKILL.md").write_text("x")
        (self.home / ".claude/skills").mkdir(parents=True)
        os.symlink(shared / "one", self.home / ".claude/skills/one")
        (self.home / ".claude/skills/own").mkdir()
        (self.home / ".claude/skills/own/SKILL.md").write_text("x")
        (self.home / ".hermes/skills/cat/two").mkdir(parents=True)
        (self.home / ".hermes/skills/cat/two/SKILL.md").write_text("x")
        inv = invmod.collect([self.agent("claude", "Claude Code"), self.agent("hermes", "Hermes")])
        sets = {s.path: (s.skills, s.agents) for s in inv.skill_sets}
        self.assertEqual(sets["~/.agents/skills"], (["one"], ["claude"]))
        self.assertEqual(sets["~/.claude/skills"], (["own"], ["claude"]))
        self.assertEqual(sets["~/.hermes/skills"], (["two"], ["hermes"]))


class GraphTest(unittest.TestCase):
    def test_columns_and_links(self):
        pi = agents.Agent(command="pi", name="Pi", icon="p", installed=True)
        claude = agents.Agent(command="claude", name="Claude Code", icon="c", installed=True)
        missing = agents.Agent(command="grok", name="Grok", icon="g")
        details = {
            "pi": ad.Details(model="qwen (local)", providers=[ad.Provider("local", True, "llama.cpp", 2)]),
            "claude": ad.Details(model="opus"),
        }
        inv = invmod.Inventory(
            runtimes=[invmod.Runtime("llamacpp", "llama.cpp", True, True, address="127.0.0.1:8080",
                                     models=["qwen", "gemma"])],
            mcp=[invmod.McpServer("photo", "command", "photo-cli", ["claude", "pi"])],
            skill_sets=[invmod.SkillSet("~/.agents/skills", ["a", "b"], ["claude"])],
            agent_mcp={"claude": ["photo"], "pi": ["photo"]}, agent_skills={"claude": ["~/.agents/skills"]})
        g = graph.build([pi, claude, missing], details, inv)
        self.assertEqual([n.label for n in g.column(1)], ["Pi", "Claude Code"])
        self.assertEqual({n.label for n in g.column(2)}, {"llama.cpp", "Anthropic"})
        # Claude Code's other models (online_models) stand beside the one in use, to switch to on the map.
        self.assertEqual({n.label for n in g.column(3)}, {"opus", "fable", "sonnet", "qwen", "gemma"})
        self.assertNotIn(("agent:claude", "model:cloud:anthropic:sonnet", "default"),
                         {(e.source, e.target, e.kind) for e in g.edges})
        self.assertEqual({n.label for n in g.column(0)}, {"photo", "~/.agents/skills"})
        links = {(e.source, e.target, e.kind) for e in g.edges}
        self.assertIn(("agent:pi", "runtime:llamacpp", "uses"), links)
        self.assertIn(("agent:pi", "model:runtime:llamacpp:qwen", "default"), links)
        self.assertIn(("cloud:anthropic", "model:cloud:anthropic:opus", "serves"), links)
        self.assertNotIn("agent:grok", {n.id for n in g.nodes})
        ids = {n.id for n in g.nodes}
        self.assertTrue(all(e.source in ids and e.target in ids for e in g.edges))
        # Models know their folder (the list groups them by it); online ones have none.
        inv.runtimes[0].models_dir = "~/Models"
        g = graph.build([pi, claude, missing], details, inv)
        groups = {n.label: n.group for n in g.column(3)}
        self.assertEqual(groups, {"opus": "", "fable": "", "sonnet": "", "qwen": "~/Models", "gemma": "~/Models"})

    def test_only_what_is_set_up(self):
        """The map has no places to add things: no local LLM that is not installed, no "+" boxes.
        A known MCP server without agents stays as a faded box, to draw a line to it again (task 018)."""
        claude = agents.Agent(command="claude", name="Claude Code", icon="c", installed=True)
        inv = invmod.Inventory(
            runtimes=[invmod.Runtime("ollama", "Ollama", True, False, models=["gemma"], models_dir="/m"),
                      invmod.Runtime("jan", "Jan", False, False)],
            mcp=[invmod.McpServer("github", "command", "gh", ["claude"]), invmod.McpServer("old", "command", "x", [])],
            agent_mcp={"claude": ["github"]})
        nodes = {n.id: n for n in graph.build([claude], {"claude": ad.Details(model="opus")}, inv).nodes}
        ids = set(nodes)
        self.assertIn("runtime:ollama", ids)
        self.assertFalse(nodes["mcp:github"].dim)
        self.assertNotIn("runtime:jan", ids)
        self.assertTrue(nodes["mcp:old"].dim)
        self.assertFalse([i for i in ids if i.startswith("add:")])

    def test_running_instances(self):
        """An open agent gets a green dot; the model it runs with says who uses it now."""
        pi = agents.Agent(command="pi", name="Pi", icon="p", installed=True)
        claude = agents.Agent(command="claude", name="Claude Code", icon="c", installed=True, version="2.1")
        details = {
            "pi": ad.Details(model="qwen (local)", providers=[ad.Provider("local", True, "llama.cpp", 2)]),
            "claude": ad.Details(model="opus"),
        }
        inv = invmod.Inventory(
            runtimes=[invmod.Runtime("llamacpp", "llama.cpp", True, True, address="127.0.0.1:8080",
                                     models=["qwen", "gemma"])])
        g = graph.build([pi, claude], details, inv, running={"claude": [""], "pi": ["gemma (local)", ""]})
        nodes = {n.id: n for n in g.nodes}
        self.assertEqual(nodes["agent:claude"].status, "running")
        self.assertEqual(nodes["agent:claude"].detail, "2.1 · running")
        self.assertEqual(nodes["agent:pi"].detail, "2 running")
        self.assertEqual(nodes["model:cloud:anthropic:opus"].status, "running")
        self.assertEqual(nodes["model:cloud:anthropic:opus"].detail, "in use · Claude Code")
        self.assertEqual(nodes["model:runtime:llamacpp:qwen"].status, "running")
        self.assertEqual(nodes["model:runtime:llamacpp:gemma"].status, "running")
        # Nothing open: no dots.
        g = graph.build([pi, claude], details, inv)
        self.assertFalse(any(n.status == "running" for n in g.nodes if n.kind in ("agent", "model")))
        # An instance started with another online model adds that model.
        g = graph.build([pi, claude], details, inv, running={"claude": ["anthropic/sonnet"]})
        self.assertEqual({n.id: n for n in g.nodes}["model:cloud:anthropic:sonnet"].status, "running")

    def test_a_model_under_another_spelling_still_gets_its_line(self):
        """LM Studio not running lists its models by folder ("gemma-4-12B-it-QAT-GGUF"); the agent
        names the id ("gemma-4-12b-it-qat"). Same model: the default line and the "in use" mark."""
        hermes = agents.Agent(command="hermes", name="Hermes", icon="h", installed=True)
        details = {"hermes": ad.Details(model="gemma-4-12b-it-qat (lmstudio-local)",
                                        providers=[ad.Provider("lmstudio-local", True, "LM Studio", 2, active=True)])}
        inv = invmod.Inventory(runtimes=[invmod.Runtime("lmstudio", "LM Studio", True, False, address="127.0.0.1:1234",
                                                        models=["Qwen3.8-27B-GGUF", "gemma-4-12B-it-QAT-GGUF"])])
        g = graph.build([hermes], details, inv, running={"hermes": [""]})
        folder = "model:runtime:lmstudio:gemma-4-12b-it-qat-gguf"
        self.assertIn(("agent:hermes", folder, "default"), {(e.source, e.target, e.kind) for e in g.edges})
        nodes = {n.id: n for n in g.nodes}
        self.assertTrue(nodes[folder].active)
        self.assertEqual(nodes[folder].status, "running")
        self.assertFalse(nodes["model:runtime:lmstudio:qwen3.8-27b-gguf"].active)
        # Another model is not taken for it: the one it is set to is missing (review 063 F6).
        details["hermes"] = ad.Details(model="gemma-4-12b-it (lmstudio-local)", providers=details["hermes"].providers)
        g = graph.build([hermes], details, inv)
        self.assertEqual([e.target for e in g.edges if e.source == "agent:hermes" and e.kind == "default"],
                         ["model:runtime:lmstudio:gemma-4-12b-it"])
        self.assertEqual({n.id: n for n in g.nodes}["model:runtime:lmstudio:gemma-4-12b-it"].status, "missing")

    def test_a_provider_that_is_gone_is_a_broken_line_not_a_cloud(self):
        """Hermes set to a model on "llama-local", but no llama-local provider any more: a red
        line to llama.cpp (click: repair or remove), never an online service named llama-local."""
        hermes = agents.Agent(command="hermes", name="Hermes", icon="h", installed=True)
        details = {"hermes": ad.Details(model="gemma-4-26B (llama-local)", providers=[])}
        inv = invmod.Inventory(runtimes=[invmod.Runtime("llamacpp", "llama.cpp", True, False,
                                                        address="127.0.0.1:8080", models=["gemma-4-26B"])])
        g = graph.build([hermes], details, inv)
        broken = [e for e in g.edges if e.source == "agent:hermes" and e.failed]
        self.assertEqual([(e.target, e.failed, e.dangling) for e in broken],
                         [("runtime:llamacpp", "error", "gemma-4-26B")])
        self.assertFalse([n for n in g.nodes if n.kind == "cloud"])

    def test_models_stamp_follows_the_index_and_the_folders(self):
        with tempfile.TemporaryDirectory() as home:
            index = Path(home) / ".lmstudio/.internal/model-index-cache.json"
            index.parent.mkdir(parents=True)
            index.write_text("{}")
            folder = Path(home) / "models"
            folder.mkdir()
            runtimes = [invmod.Runtime("lmstudio", "LM Studio", True, True),
                        invmod.Runtime("llamacpp", "llama.cpp", True, True, models_dir=str(folder))]
            with mock.patch.object(invmod, "HOME", Path(home)):
                first = invmod.models_stamp(runtimes)
                self.assertEqual(first, invmod.models_stamp(runtimes))
                os.utime(index, ns=(1, 10 ** 18))
                second = invmod.models_stamp(runtimes)
                self.assertNotEqual(first, second)
                (folder / "new-model").mkdir()
                os.utime(folder, ns=(1, 2 * 10 ** 18))
                self.assertNotEqual(second, invmod.models_stamp(runtimes))

    def test_idle_folders_folded(self):
        """A model folder none of whose models an agent uses or runs is idle (the health check
        names it); one whose model an open agent runs is not."""
        pi = agents.Agent(command="pi", name="Pi", icon="p", installed=True)
        details = {"pi": ad.Details(model="qwen (local)", providers=[ad.Provider("local", True, "llama.cpp", 2)])}
        inv = invmod.Inventory(runtimes=[
            invmod.Runtime("llamacpp", "llama.cpp", True, True, address="127.0.0.1:8080", models=["qwen"],
                           models_dir="/m/llama"),
            invmod.Runtime("ollama", "Ollama", True, False, address="127.0.0.1:11434", models=["gemma"],
                           models_dir="/m/ollama")])
        g = graph.build([pi], details, inv)
        self.assertEqual(graph.idle_folders(g), {"/m/ollama": {"model:runtime:ollama:gemma"}})
        # An open Pi runs gemma from Ollama right now: nothing idle.
        details["pi"].providers.append(ad.Provider("ollama", True, "Ollama", 1))
        g = graph.build([pi], details, inv, running={"pi": ["gemma (ollama)"]})
        self.assertEqual(graph.idle_folders(g), {})

    def test_split_model(self):
        self.assertEqual(graph.split_model("qwen (llama-local)"), ("qwen", "llama-local"))
        self.assertEqual(graph.split_model("anthropic/claude-x"), ("claude-x", "anthropic"))
        self.assertEqual(graph.split_model("opus"), ("opus", ""))


if __name__ == "__main__":
    unittest.main()


class LayerTest(unittest.TestCase):
    def setUp(self):
        self.g = graph.Graph()
        for node in (graph.Node("agent:a", 1, "A", kind="agent"), graph.Node("cloud:x", 2, "X", kind="cloud"),
                     graph.Node("runtime:r", 2, "R", kind="runtime"), graph.Node("model:x:m", 3, "m", kind="model"),
                     graph.Node("model:r:n", 3, "n", kind="model"), graph.Node("mcp:s", 0, "s", kind="mcp")):
            self.g.add(node)
        self.g.link("cloud:x", "model:x:m", "serves")
        self.g.link("runtime:r", "model:r:n", "serves")

    def test_needs(self):
        self.assertEqual(graph.effective_layers({"models"}), set())
        self.assertEqual(graph.effective_layers({"models", "local"}), {"models", "local"})
        self.assertEqual(graph.effective_layers({"mcp", "online"}), {"online"})

    def test_models_follow_their_providers(self):
        hidden = graph.hidden_by_layers(self.g, {"agents", "local", "models"})
        self.assertEqual(hidden, {"cloud:x", "model:x:m", "mcp:s"})

    def test_unticked_boxes_take_what_hangs_on_them(self):
        self.g.add(graph.Node("mcp:unused", 0, "u", kind="mcp"))
        self.g.link("agent:a", "mcp:s", "mcp")
        hidden = graph.hidden_by_layers(self.g, set(graph.LAYERS), {"agent:a", "runtime:r"})
        # The agent's MCP server and the runtime's model go with them; an unused server stays.
        self.assertEqual(hidden, {"agent:a", "mcp:s", "runtime:r", "model:r:n"})
        self.assertEqual(graph.hidden_by_layers(self.g, set(graph.LAYERS), {"model:x:m"}), {"model:x:m"})


class VllmServiceTest(unittest.TestCase):
    def test_installer_service_found(self):
        import tempfile
        from pathlib import Path
        import inventory
        with tempfile.TemporaryDirectory() as root:
            env = Path(root, "server.env")
            env.write_text("HF_HOME=/m\nVLLM_MODEL=Qwen/Qwen3-8B\n")
            unit = Path(root, "vllm.service")
            unit.write_text(f"[Service]\nEnvironmentFile={env}\n"
                            "ExecStart=/home/u/.local/bin/vllm serve ${VLLM_MODEL} --host 127.0.0.1 --port 8000\n")
            old = inventory.unit_files
            inventory.unit_files = lambda: [unit]
            try:
                rt = inventory.vllm([])
            finally:
                inventory.unit_files = old
        self.assertEqual(rt.service, "vllm")
        self.assertTrue(rt.installed)
        self.assertFalse(rt.running)
        self.assertEqual(rt.models, ["Qwen/Qwen3-8B"])


class LmStudioIdsTest(unittest.TestCase):
    def test_ids_from_model_index(self):
        import json
        import tempfile
        from pathlib import Path
        import inventory
        with tempfile.TemporaryDirectory() as root:
            index = Path(root, ".internal")
            index.mkdir()
            (index / "model-index-cache.json").write_text(json.dumps({"models": [
                {"domain": "llm", "defaultIdentifier": "gemma-4-12b-it-qat", "containingDirSubpath": "x/gemma-4-12B-it-QAT-GGUF"},
                {"domain": "embedding", "defaultIdentifier": "text-embedding-nomic"},
                {"domain": "llm", "defaultIdentifier": "<b>bad</b>\n"},
                {"domain": "llm"},
                # moved to the trash: LM Studio's cache still lists it
                {"domain": "llm", "defaultIdentifier": "gone", "entryPoint": {"absPath": f"{root}/gone.gguf"}}]}))
            self.assertEqual(inventory.lmstudio_identifiers(Path(root)), ["gemma-4-12b-it-qat"])
            self.assertEqual(inventory.lmstudio_identifiers(Path(root, "missing")), [])


class ReviewChainTest(unittest.TestCase):
    """Review 063 (Codex): the agent → provider → model chain."""
    setUp, write, agent = InventoryTest.setUp, InventoryTest.write, InventoryTest.agent

    def edges(self, g):
        return {(e.source, e.target, e.kind) for e in g.edges}

    def test_a_configured_port_finds_the_runtime_detected_there(self):
        """F4: Pi set to llama.cpp on port 9090 (not its usual 8080)."""
        pi = self.agent("pi", "Pi")
        provider = ad.provider_for_url("llama", "http://127.0.0.1:9090/v1")
        self.assertEqual((provider.runtime, provider.port), ("", 9090))
        inv = invmod.Inventory(runtimes=[invmod.Runtime("llamacpp", "llama.cpp", True, True,
                                                        address="127.0.0.1:9090", models=["qwen"])])
        g = graph.build([pi], {"pi": ad.Details(model="qwen (llama)", providers=[provider])}, inv)
        self.assertIn(("agent:pi", "runtime:llamacpp", "uses"), self.edges(g))
        self.assertIn(("agent:pi", "model:runtime:llamacpp:qwen", "default"), self.edges(g))
        # The usual port still finds it when no runtime is detected on the configured one.
        usual = ad.provider_for_url("llama", "http://127.0.0.1:8080/v1")
        inv.runtimes[0].address = ""
        self.assertIs(invmod.local_runtime(usual, inv.runtimes), inv.runtimes[0])

    def test_an_unqualified_model_on_a_local_endpoint_is_that_runtimes(self):
        """F5 and F7: Claude Code with ANTHROPIC_BASE_URL on localhost, model qwen (also as --model)."""
        claude = self.agent("claude", "Claude Code")
        provider = ad.provider_for_url("Anthropic API", "http://localhost:8080")
        inv = invmod.Inventory(runtimes=[invmod.Runtime("llamacpp", "llama.cpp", True, True,
                                                        address="127.0.0.1:8080", models=["qwen", "gemma"])])
        g = graph.build([claude], {"claude": ad.Details(model="qwen", providers=[provider])}, inv,
                        running={"claude": ["gemma"]})
        self.assertIn(("agent:claude", "model:runtime:llamacpp:qwen", "default"), self.edges(g))
        self.assertFalse([n for n in g.nodes if n.kind == "cloud"])
        self.assertEqual({n.id: n for n in g.nodes}["model:runtime:llamacpp:gemma"].status, "running")

    def test_the_models_in_use_are_drawn_past_the_first_ten(self):
        """F6: the eleventh model, used; a model no longer there stays, missing."""
        pi = self.agent("pi", "Pi")
        provider = ad.Provider("llama", True, "llama.cpp")
        models = [f"m{i:02}" for i in range(12)]
        inv = invmod.Inventory(runtimes=[invmod.Runtime("llamacpp", "llama.cpp", True, True,
                                                        address="127.0.0.1:8080", models=models)])
        g = graph.build([pi], {"pi": ad.Details(model="m10 (llama)", providers=[provider])}, inv,
                        provider_models={"runtime:llamacpp": "model:runtime:llamacpp:m11"})
        nodes = {n.id: n for n in g.nodes}
        self.assertIn(("agent:pi", "model:runtime:llamacpp:m10", "default"), self.edges(g))
        self.assertIn("model:runtime:llamacpp:m11", nodes)
        self.assertNotIn("more:runtime:llamacpp", nodes)
        g = graph.build([pi], {"pi": ad.Details(model="gone (llama)", providers=[provider])}, inv)
        nodes = {n.id: n for n in g.nodes}
        self.assertEqual(nodes["model:runtime:llamacpp:gone"].status, "missing")
        self.assertEqual(nodes["model:runtime:llamacpp:gone"].label, "gone")
        self.assertIn(("runtime:llamacpp", "model:runtime:llamacpp:gone", "serves"), self.edges(g))
        self.assertIn(("agent:pi", "model:runtime:llamacpp:gone", "default"), self.edges(g))
        self.assertEqual(nodes["more:runtime:llamacpp"].label, "+2 more")

    def test_an_own_provider_without_sign_in_is_available(self):
        """F17: Codex signed out: OpenAI is a provider it can use, not one it uses."""
        import agent_status
        codex = self.agent("codex", "Codex")
        with mock.patch.object(agent_status, "HOME", self.home), mock.patch.dict(os.environ, {}, clear=True):
            g = graph.build([codex], {"codex": ad.Details(model="gpt-6.1-sol")}, invmod.Inventory())
            self.assertIn(("agent:codex", "cloud:openai", "available"), self.edges(g))
            self.assertEqual({n.id: n for n in g.nodes}["agent:codex"].own_provider, "cloud:openai")
            self.write(".codex/auth.json", "{}")
            g = graph.build([codex], {"codex": ad.Details(model="gpt-6.1-sol")}, invmod.Inventory())
            self.assertIn(("agent:codex", "cloud:openai", "uses"), self.edges(g))

    def test_a_moved_codex_home_is_where_its_sign_in_is(self):
        """Review 063 round 3 #11: with CODEX_HOME set, ~/.codex/auth.json is no sign-in; the line and
        the battery go by the same files."""
        import agent_status
        codex = self.agent("codex", "Codex")
        moved = self.home / "codex-home"
        moved.mkdir()
        self.write(".codex/auth.json", "{}")
        with mock.patch.object(agent_status, "HOME", self.home), \
                mock.patch.dict(os.environ, {"CODEX_HOME": str(moved)}, clear=True):
            g = graph.build([codex], {"codex": ad.Details(model="gpt-6.1-sol")}, invmod.Inventory())
            self.assertIn(("agent:codex", "cloud:openai", "available"), self.edges(g))
            self.assertFalse(ad.plan_limits_signed_in("codex", self.home))
            (moved / "auth.json").write_text("{}")
            g = graph.build([codex], {"codex": ad.Details(model="gpt-6.1-sol")}, invmod.Inventory())
            self.assertIn(("agent:codex", "cloud:openai", "uses"), self.edges(g))

    def test_an_unset_key_is_no_sign_in_and_no_login_no_drop(self):
        """Round 2 #16: Grok names only XAI_API_KEY: without it xAI is available, not used; and the app
        can start no sign-in for it (own_login), as Ask AI refuses."""
        import agent_status
        grok, codex = self.agent("grok", "Grok"), self.agent("codex", "Codex")
        with mock.patch.object(agent_status, "HOME", self.home), mock.patch.dict(os.environ, {}, clear=True):
            g = graph.build([grok, codex], {"grok": ad.Details(model="grok-5")}, invmod.Inventory())
            self.assertIn(("agent:grok", "cloud:xai", "available"), self.edges(g))
            nodes = {n.id: n for n in g.nodes}
            self.assertEqual((nodes["agent:grok"].own_provider, nodes["agent:grok"].own_login), ("cloud:xai", False))
            self.assertTrue(nodes["agent:codex"].own_login)
            os.environ["XAI_API_KEY"] = "set"
            g = graph.build([grok], {"grok": ad.Details(model="grok-5")}, invmod.Inventory())
            self.assertIn(("agent:grok", "cloud:xai", "uses"), self.edges(g))

    def test_servers_of_one_name_but_other_commands_stay_two(self):
        """F2: Claude's and Codex's "filesystem" run different programs."""
        self.write(".claude.json", json.dumps({"mcpServers": {"filesystem": {"command": "fs-a"}}}))
        self.write(".codex/config.toml", '[mcp_servers.filesystem]\ncommand = "fs-b"\n')
        claude, codex = self.agent("claude", "Claude Code"), self.agent("codex", "Codex")
        with mock.patch.object(ad, "details_for", lambda c: ad.Details()):
            inv = invmod.collect([claude, codex], remember=False)
        servers = {s.id: (s.name, s.target, s.agents) for s in inv.mcp}
        self.assertEqual(servers["filesystem"], ("filesystem", "fs-a", ["claude"]))
        self.assertEqual(servers["filesystem~2"], ("filesystem", "fs-b", ["codex"]))
        g = graph.build([claude, codex], {}, inv)
        self.assertIn(("agent:claude", "mcp:filesystem", "mcp"), self.edges(g))
        self.assertIn(("agent:codex", "mcp:filesystem~2", "mcp"), self.edges(g))
        self.assertEqual({n.id: n.label for n in g.nodes}["mcp:filesystem~2"], "filesystem")

    def test_servers_are_told_apart_by_what_they_are(self):
        """063e #4: same host, other path or port, or the same program with other arguments,
        are other servers; so are other env or headers (R3 #1), kept out of every label and id.
        The label stays short."""
        self.write(".claude.json", json.dumps({"mcpServers": {
            "web": {"type": "http", "url": "https://example.test/a"},
            "port": {"type": "http", "url": "https://example.test:8443/mcp"},
            "tool": {"command": "node", "args": ["/x/server.js"]},
            "same": {"command": "same-cli", "env": {"ROOT": "/data/one"}},
            "desk": {"type": "http", "url": "https://example.test/desk", "headers": {"X-Workspace": "alpha"}},
            "twin": {"command": "twin-cli", "env": {"A": "1", "B": "2"}}}}))
        self.write(".codex/config.toml", '[mcp_servers.web]\nurl = "https://example.test/b"\n'
                   '[mcp_servers.port]\nurl = "https://example.test:9443/mcp"\n'
                   '[mcp_servers.tool]\ncommand = "node"\nargs = ["/y/server.js"]\n'
                   '[mcp_servers.same]\ncommand = "same-cli"\nenv = { ROOT = "/data/two" }\n'
                   '[mcp_servers.desk]\nurl = "https://example.test/desk"\nhttp_headers = { X-Workspace = "beta" }\n'
                   '[mcp_servers.twin]\ncommand = "twin-cli"\nenv = { B = "2", A = "1" }\n')
        claude, codex = self.agent("claude", "Claude Code"), self.agent("codex", "Codex")
        with mock.patch.object(ad, "details_for", lambda c: ad.Details()):
            inv = invmod.collect([claude, codex], remember=False)
        servers = {s.id: s for s in inv.mcp}
        for name in ("web", "port", "tool"):
            self.assertEqual(servers[f"{name}~2"].agents, ["codex"], name)
            self.assertEqual(servers[f"{name}~2"].target, servers[name].target)
        self.assertEqual((servers["web"].target, servers["tool"].target), ("example.test", "node server.js"))
        for name in ("same", "desk"):
            self.assertEqual(servers[f"{name}~2"].agents, ["codex"], name)
            self.assertEqual(servers[f"{name}~2"].target, servers[name].target)
        self.assertEqual(servers["twin"].agents, ["claude", "codex"])
        self.assertNotIn("twin~2", servers)
        for secret in ("/data/", "alpha", "beta"):
            self.assertNotIn(secret, repr(inv.mcp))

    def test_a_saved_server_keeps_its_key(self):
        """R3 #4: a saved, unused filesystem keeps its key when an agent's other filesystem comes;
        a saved server with env (from before env took part) still is that server."""
        a = {"transport": "stdio", "command": "fs-a", "args": [], "env": {}, "url": "", "headers": {}}
        rooted = dict(a, env={"ROOT": "/data/a"})
        self.write(".config/omarchy/ai-agents-map/mcp-servers.json", json.dumps({"servers": {"filesystem": a, "rooted": rooted}}))
        self.write(".claude.json", json.dumps({"mcpServers": {"filesystem": {"command": "fs-b"},
                                                              "rooted": {"command": "fs-a", "env": {"ROOT": "/data/a"}}}}))
        claude = self.agent("claude", "Claude Code")
        with mock.patch.object(ad, "details_for", lambda c: ad.Details()):
            invmod.collect([claude])
        known = mcp_library.load()
        self.assertEqual(sorted(known), ["filesystem", "filesystem~2", "rooted"])
        self.assertEqual((known["filesystem"], known["filesystem~2"]["command"], known["rooted"]),
                         (a, "fs-b", rooted))

    @unittest.skip("Maprisma Flutter route; the GTK map gets it in parity phase 2")
    def test_the_second_server_of_a_name_stays_known(self):
        """063e #5: the list keeps every server of a name (an old list's plain name stays),
        so the second one's definition stays when its last agent leaves."""
        import flutter_connections
        old = {"transport": "stdio", "command": "fs-a", "args": [], "env": {}, "url": "", "headers": {}}
        self.write(".config/omarchy/ai-agents-map/mcp-servers.json", json.dumps({"servers": {"filesystem": old}}))
        self.write(".claude.json", json.dumps({"mcpServers": {"filesystem": {"command": "fs-a"}}}))
        self.write(".codex/config.toml", '[mcp_servers.filesystem]\ncommand = "fs-b"\n')
        claude, codex = self.agent("claude", "Claude Code"), self.agent("codex", "Codex")
        with mock.patch.object(ad, "details_for", lambda c: ad.Details()):
            invmod.collect([claude, codex])
            known = mcp_library.load()
            self.assertEqual((known["filesystem"]["command"], known["filesystem~2"]["command"]), ("fs-a", "fs-b"))
            self.write(".codex/config.toml", "")
            inv = invmod.collect([claude, codex])
            self.assertEqual(sorted(mcp_library.load()), ["filesystem", "filesystem~2"])
            unused = {s.id: s for s in inv.mcp}["filesystem~2"]
            self.assertEqual((unused.name, unused.target, unused.agents, unused.library),
                             ("filesystem", "fs-b", [], "filesystem~2"))
            # Drawing the line again sets that very server up; forgetting takes only it.
            conn = flutter_connections.Connections(self.home / "backups")
            with mock.patch.object(flutter_connections.agents, "list_agents", lambda: [claude, codex]):
                again = conn.preview({"kind": "mcp", "command": "codex", "item": "filesystem~2", "remove": False})
                text = "".join(c.new for c in conn.plans[again["plan"]][1].changes)
                self.assertIn("fs-b", text)
                self.assertNotIn("fs-a", text)
                forget = conn.forget_preview({"name": "filesystem~2"})
                self.assertEqual(list(json.loads(conn.plans[forget["plan"]][1].changes[0].new)["servers"]),
                                 ["filesystem"])
                with self.assertRaises(ValueError):
                    conn.forget_preview({"name": "filesystem"})

    def test_lm_studio_and_jan_on_other_ports(self):
        """F8: LM Studio's server port from its settings, Jan's from its service or its router."""
        self.write(".lmstudio/.internal/http-server-config.json", json.dumps({"port": 4321}))
        self.assertEqual(invmod.lmstudio([]).address, "127.0.0.1:4321")
        self.write(".lmstudio/.internal/http-server-config.json", "{}")
        self.assertEqual(invmod.lmstudio([]).address, "127.0.0.1:1234")
        self.assertEqual(invmod.jan([]).address, "127.0.0.1:6767")
        self.write(".config/systemd/user/jan.service", "[Service]\nExecStart=/bin/sh -c 'exec w --port 7001'\n")
        self.assertEqual((invmod.jan([]).address, invmod.jan([]).running), ("127.0.0.1:7001", False))
        worker = [(9, "jan-llama-worke", ["jan-llama-worker", "--preset", "p.ini", "--port", "7002"])]
        rt = invmod.jan(worker)
        self.assertEqual((rt.address, rt.running), ("127.0.0.1:7002", True))

    def test_a_reused_model_folder_is_on_the_map(self):
        """F11: a folder saved by Reuse model folder that no local LLM lists."""
        folder = self.home / "Old/models"
        self.write("Old/models/qwen-7b-Q4_K_M.gguf", "")
        llama = invmod.Runtime("llamacpp", "llama.cpp", True, False, models=["served"])
        found = invmod.reused_folders([llama], [str(folder)])
        self.assertEqual(found, [("~/Old/models", ["qwen-7b-Q4_K_M"])])
        g = graph.build([], {}, invmod.Inventory(runtimes=[llama], model_folders=found))
        node = {n.id: n for n in g.nodes}["model:folder:~/Old/models:qwen-7b-q4_k_m"]
        self.assertEqual((node.kind, node.group), ("model", "~/Old/models"))
        # A folder a runtime already lists its models from is that runtime's.
        llama.models_dir = "~/Old/models"
        self.assertEqual(invmod.reused_folders([llama], [str(folder)]), [])

    def test_a_reused_file_is_hidden_only_when_the_runtime_serves_that_file(self):
        """Round 2 #11: another quantization of the same model stays; the same file reached by a link goes."""
        for name in ("Old/models/qwen-Q8_0.gguf", "Old/models/gemma.gguf", "llama/qwen-Q4_K_M.gguf", "llama/gemma.gguf"):
            self.write(name, "")
        (self.home / "Old/models/same.gguf").symlink_to(self.home / "llama/gemma.gguf")
        llama = invmod.Runtime("llamacpp", "llama.cpp", True, False, models_dir="~/llama",
                               models=["qwen-Q4_K_M", "gemma"])
        found = invmod.reused_folders([llama], [str(self.home / "Old/models"), str(self.home / "llama")])
        self.assertEqual(found, [("~/Old/models", ["gemma", "qwen-Q8_0"])])
        ids = {n.id for n in graph.build([], {}, invmod.Inventory(runtimes=[llama], model_folders=found)).nodes}
        self.assertLessEqual({"model:folder:~/Old/models:qwen-q8_0", "model:folder:~/Old/models:gemma",
                              "model:runtime:llamacpp:qwen-q4_k_m"}, ids)

    def test_a_runtime_detected_on_another_port_is_a_broken_line(self):
        """Round 2 #9: llama.cpp on 9090, Pi still set to 8080."""
        pi = self.agent("pi", "Pi")
        provider = ad.provider_for_url("llama", "http://127.0.0.1:8080/v1")
        provider.active = True
        self.assertEqual((provider.runtime, provider.port), ("llama.cpp", 8080))
        inv = invmod.Inventory(runtimes=[invmod.Runtime("llamacpp", "llama.cpp", True, True,
                                                        address="127.0.0.1:9090", models=["qwen"])])
        self.assertIsNone(invmod.local_runtime(provider, inv.runtimes))
        self.assertIs(invmod.moved_runtime(provider, inv.runtimes), inv.runtimes[0])
        g = graph.build([pi], {"pi": ad.Details(model="qwen (llama)", providers=[provider])}, inv)
        self.assertIn(("agent:pi", "runtime:llamacpp", "failed"), self.edges(g))
        self.assertNotIn(("agent:pi", "runtime:llamacpp", "uses"), self.edges(g))
        self.assertFalse([e for e in g.edges if e.source == "agent:pi" and e.kind == "default"])
        self.assertFalse([n for n in g.nodes if n.kind == "cloud"])
        self.assertEqual([e.dangling for e in g.edges if e.kind == "failed"], ["qwen"])
        claude = self.agent("claude", "Claude Code")
        local = ad.provider_for_url("Anthropic API", "http://localhost:8080")
        local.active = True
        g = graph.build([claude], {"claude": ad.Details(model="qwen", providers=[local])}, inv)
        self.assertIn(("agent:claude", "runtime:llamacpp", "failed"), self.edges(g))
        self.assertFalse([n for n in g.nodes if n.kind == "cloud"])
        for address in ("127.0.0.1:8080", ""):
            inv.runtimes[0].address = address
            self.assertIs(invmod.local_runtime(provider, inv.runtimes), inv.runtimes[0])
            self.assertIsNone(invmod.moved_runtime(provider, inv.runtimes))

    def moved_llama(self):
        provider = ad.provider_for_url("llama", "http://127.0.0.1:8080/v1")
        return provider, invmod.Runtime("llamacpp", "llama.cpp", True, True, address="127.0.0.1:9090",
                                        models=["qwen"])

    def test_r3_a_file_a_runtime_serves_by_its_path_is_not_shown_twice(self):
        """Round 3 #9: llama.cpp -m, a models.ini preset (through a link), a .kcpps and vLLM's file."""
        for name in ("qwen", "gemma", "phi", "mistral", "other"):
            self.write(f"models/{name}.gguf", "")
        (self.home / "gemma-link.gguf").symlink_to(self.home / "models/gemma.gguf")
        self.write(".config/llama.cpp/models.ini", "[gemma]\nmodel = ~/gemma-link.gguf\nctx-size = 8192\n")
        none = 2 ** 31 - 2   # no such process: no parent, no environment
        llama = invmod.llamacpp([(none, "llama-server", ["llama-server", "-m", str(self.home / "models/qwen.gguf")])])
        self.assertEqual(llama.models_dir, "")
        self.assertLessEqual({"qwen", "gemma"}, set(llama.models))
        folder = [str(self.home / "models")]
        self.assertEqual(invmod.reused_folders([llama], folder), [("~/models", ["mistral", "other", "phi"])])
        self.write(".config/koboldcpp/models/phi.kcpps", json.dumps({"model_param": str(self.home / "models/phi.gguf")}))
        with mock.patch.object(invmod, "KOBOLD_CONFIGS", self.home / ".config/koboldcpp/models"):
            kobold = invmod.koboldcpp([(none, "koboldcpp", ["koboldcpp"])])
        self.assertNotIn("phi", invmod.reused_folders([kobold], folder)[0][1])
        vllm = invmod.vllm([(none, "vllm", ["vllm", "serve", str(self.home / "models/mistral.gguf")])])
        self.assertNotIn("mistral", invmod.reused_folders([vllm], folder)[0][1])

    def test_r3_a_running_agent_on_a_moved_llm_invents_no_cloud(self):
        """Round 3 #6: Pi running on its configured model, its llama.cpp moved to 9090."""
        pi = self.agent("pi", "Pi")
        provider, llama = self.moved_llama()
        inv = invmod.Inventory(runtimes=[llama])
        for instance in ("", "qwen", "qwen (llama)"):
            with self.subTest(instance=instance):
                g = graph.build([pi], {"pi": ad.Details(model="qwen (llama)", providers=[provider])}, inv,
                                running={"pi": [instance]})
                self.assertFalse([n for n in g.nodes if n.kind == "cloud"])
                self.assertEqual({e for e in self.edges(g) if e[0] == "agent:pi"},
                                 {("agent:pi", "runtime:llamacpp", "failed")})

    @unittest.skip("Maprisma Flutter route; the GTK map gets it in parity phase 2")
    def test_r3_an_inactive_provider_whose_llm_moved_stays_broken(self):
        """Round 3 #7: Pi uses Ollama; its llama.cpp provider is set to 8080, llama.cpp moved to 9090."""
        import flutter_map_payload
        pi = self.agent("pi", "Pi")
        provider, llama = self.moved_llama()
        provider.active = False
        ollama = ad.provider_for_url("ollama", "http://127.0.0.1:11434/v1")
        inv = invmod.Inventory(runtimes=[llama, invmod.Runtime("ollama", "Ollama", True, True,
                                                              address="127.0.0.1:11434", models=["gemma3:4b"])])
        g = graph.build([pi], {"pi": ad.Details(model="gemma3:4b (ollama)", providers=[ollama, provider])}, inv)
        self.assertIn(("agent:pi", "runtime:ollama", "uses"), self.edges(g))
        self.assertIn(("agent:pi", "runtime:llamacpp", "failed"), self.edges(g))
        self.assertFalse([e for e in g.edges if e.kind == "default" and e.target.startswith("model:runtime:llamacpp")])
        broken = next(e for e in g.edges if e.kind == "failed")
        self.assertEqual((broken.failed, broken.dangling, broken.fixed), ("error", "", ""))
        row = next(r for r in flutter_map_payload.edges(g, {}, {}, inv, "off") if r["kind"] == "failed")
        self.assertIn("another port", row["hints"][0]["text"])

    @unittest.skip("Maprisma Flutter route; the GTK map gets it in parity phase 2")
    def test_r3_an_agent_the_app_cannot_repair_says_where_to_change_it(self):
        """Round 3 #8: Claude Code's local endpoint is in its own settings: no Repair, the reason instead."""
        import flutter_map_payload
        claude, pi = self.agent("claude", "Claude Code"), self.agent("pi", "Pi")
        provider, llama = self.moved_llama()
        local = ad.provider_for_url("Anthropic API", "http://localhost:8080")
        inv = invmod.Inventory(runtimes=[llama])
        g = graph.build([claude, pi], {"claude": ad.Details(model="qwen", providers=[local]),
                                       "pi": ad.Details(model="qwen (llama)", providers=[provider])}, inv)
        rows = {r["source"]: r for r in flutter_map_payload.edges(g, {}, {}, inv, "off") if r["kind"] == "failed"}
        self.assertIn("127.0.0.1:9090", rows["agent:claude"]["fixed"])
        self.assertNotIn("hints", rows["agent:claude"])
        self.assertEqual(rows["agent:pi"]["fixed"], "")
        self.assertTrue(rows["agent:pi"]["hints"])
