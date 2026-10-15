import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
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
        self.assertEqual({n.label for n in g.column(3)}, {"opus", "qwen", "gemma"})
        self.assertEqual({n.label for n in g.column(0)}, {"photo", "~/.agents/skills", "+ MCP server", "+ Skills"})
        links = {(e.source, e.target, e.kind) for e in g.edges}
        self.assertIn(("agent:pi", "runtime:llamacpp", "uses"), links)
        self.assertIn(("agent:pi", "model:runtime:llamacpp:qwen", "default"), links)
        self.assertIn(("cloud:anthropic", "model:cloud:anthropic:opus", "serves"), links)
        self.assertNotIn("agent:grok", {n.id for n in g.nodes})
        ids = {n.id for n in g.nodes}
        self.assertTrue(all(e.source in ids and e.target in ids for e in g.edges))

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
