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
        self.assertEqual(found, [("web", "command", "npx some-mcp@1"), ("remote", "url", "mcp.example.com")])
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
        """The map has no places to add things: no local LLM that is not installed, no MCP server
        no agent uses, no "+" boxes (Setup adds and removes)."""
        claude = agents.Agent(command="claude", name="Claude Code", icon="c", installed=True)
        inv = invmod.Inventory(
            runtimes=[invmod.Runtime("ollama", "Ollama", True, False, models=["gemma"], models_dir="/m"),
                      invmod.Runtime("jan", "Jan", False, False)],
            mcp=[invmod.McpServer("github", "command", "gh", ["claude"]), invmod.McpServer("old", "command", "x", [])],
            agent_mcp={"claude": ["github"]})
        ids = {n.id for n in graph.build([claude], {"claude": ad.Details(model="opus")}, inv).nodes}
        self.assertIn("runtime:ollama", ids)
        self.assertIn("mcp:github", ids)
        self.assertNotIn("runtime:jan", ids)
        self.assertNotIn("mcp:old", ids)
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
        # Another model is not taken for it.
        details["hermes"] = ad.Details(model="gemma-4-12b-it (lmstudio-local)", providers=details["hermes"].providers)
        g = graph.build([hermes], details, inv)
        self.assertFalse([e for e in g.edges if e.source == "agent:hermes" and e.kind == "default"])

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
