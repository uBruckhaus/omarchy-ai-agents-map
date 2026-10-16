import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import connect  # noqa: E402
import dsh_ctl  # noqa: E402
import inventory  # noqa: E402
import mcp_ctl  # noqa: E402
import provider_watch  # noqa: E402

START = dsh_ctl.HEADER + "[]\n"
USER = """# my notes
- id: ui-settings-general
  name: "@deepseek-ai/dsh-client-ui-settings-general"
  config:
    welcomeNoticeVersion: 2026-09-28.1
- id: llm-pi-ai
  name: "@deepseek-ai/dsh-llm-pi-ai"
  config:
    providers:
      gateway:
        api: openai-completions
        baseURL: https://gateway.example/v1
        apiKeyEnv: !!js process.env.GATEWAY || 'GATEWAY_KEY'
        models:
          - id: big
"""


class DshTextTest(unittest.TestCase):
    def test_connect_on_a_fresh_profile_and_remove_again(self):
        text = dsh_ctl.connect_text(START, "llama-local", "llama.cpp (local)", "http://127.0.0.1:8080/v1",
                                    ["a", "b"], {"a": 32768}, "a", True)
        providers = dsh_ctl.providers(text)
        self.assertEqual(providers["llama-local"]["headers"], {"Authorization": "Bearer local"})
        self.assertEqual(providers["llama-local"]["models"], [{"id": "a", "contextWindow": 32768}, {"id": "b"}])
        self.assertEqual(dsh_ctl.default_model(text), ("llama-local", "a"))
        back = dsh_ctl.connect_text(text, "llama-local", "", "", [], {}, "", False, remove=True)
        self.assertEqual(back, START)                  # dsh refuses an empty patch file: "[]" again

    def test_other_providers_and_entries_are_kept(self):
        text = dsh_ctl.connect_text(USER, "jan-local", "Jan (local)", "http://127.0.0.1:6767/v1", ["m"], {}, "m",
                                    False)
        self.assertTrue(text.startswith("# my notes\n- id: ui-settings-general\n"))
        self.assertIn("welcomeNoticeVersion: 2026-09-28.1", text)           # untouched, as written
        providers = dsh_ctl.providers(text)
        self.assertEqual(set(providers), {"gateway", "jan-local"})
        self.assertIsInstance(providers["gateway"]["apiKeyEnv"], dsh_ctl.Js)  # a !!js expression stays one
        self.assertEqual(providers["gateway"]["apiKeyEnv"], "process.env.GATEWAY || 'GATEWAY_KEY'")
        self.assertIn("apiKeyEnv: !!js ", text)
        self.assertEqual(dsh_ctl.default_model(text), ("", ""))             # not used now: no default written

    def test_mcp_insert_and_remove(self):
        stdio = {"transport": "stdio", "command": "/x/grid-cli", "args": ["mcp"], "env": {}, "url": "", "headers": {}}
        text = dsh_ctl.mcp_text(USER, "grid", dsh_ctl.mcp_config(stdio))
        self.assertEqual(dsh_ctl.mcp_servers(text)["grid"]["command"], "/x/grid-cli")
        self.assertIn("- insert:\n  - id: mcp-grid\n", text)
        web = {"transport": "http", "command": "", "args": [], "env": {}, "url": "https://m.example/mcp",
               "headers": {"X": "1"}}
        text = dsh_ctl.mcp_text(text, "web", dsh_ctl.mcp_config(web))
        self.assertEqual(dsh_ctl.mcp_servers(text)["web"]["transport"], "streamable-http")
        self.assertEqual(mcp_ctl.normal(dsh_ctl.mcp_servers(text)["web"])["url"], "https://m.example/mcp")
        text = dsh_ctl.mcp_text(text, "grid", None)
        self.assertEqual(set(dsh_ctl.mcp_servers(text)), {"web"})
        self.assertEqual(dsh_ctl.mcp_text(text, "web", None).count("insert"), 0)

    def test_connect_plan_writes_the_web_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            patch = home / ".dsh/profiles/web/cordis.patch.yml"
            patch.parent.mkdir(parents=True)
            patch.write_text(START)
            rt = inventory.Runtime("llamacpp", "llama.cpp", installed=True, address="127.0.0.1:8080",
                                   models=["gemma"])
            with mock.patch.object(connect, "HOME", home):
                plan = connect.plan("dsh", rt, "gemma", True)
                backup = connect.apply(plan, home / "b")
                self.assertTrue(connect.is_connected("dsh", "llamacpp"))
                self.assertTrue(connect.is_active("dsh", "llamacpp"))
                connect.undo(backup)
                self.assertEqual(patch.read_text(), START)


class BionicTest(unittest.TestCase):
    def test_lmstudio_stop_leaves_bionic_alone(self):
        with tempfile.TemporaryDirectory() as proc:
            def process(pid, parent, argv):
                folder = Path(proc, str(pid))
                folder.mkdir()
                (folder / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
                (folder / "stat").write_text(f"{pid} (x) S {parent} 0 0")
                (folder / "comm").write_text(Path(argv[0]).name[:15])
            home = str(Path.home())
            process(10, 1, ["/opt/lm-studio/lm-studio.AppImage", "--run-as-service"])
            process(11, 10, ["/tmp/.mount_lm-stuX/lm-studio", "--run-as-service"])
            process(12, 11, [f"{home}/.lmstudio/extensions/backends/llama/llama-server"])
            process(20, 1, ["/x/bionic/squashfs-root/bionic"])
            process(21, 20, [f"{home}/.lmstudio/apps/bionic/.internal/utils/node", "-e", "x"])
            process(22, 20, [f"{home}/.lmstudio/extensions/backends/llama/llama-server"])   # Bionic's own engine
            with mock.patch.object(provider_watch.os, "getuid", lambda: os.stat(proc).st_uid):
                self.assertEqual(provider_watch.lmstudio_pids(proc), [10, 11, 12])

    def test_bionic_never_starts_lm_studio(self):
        rt = inventory.Runtime("lmstudio", "LM Studio", installed=True, address="127.0.0.1:1234",
                               agents=["bionic", "hermes"], active_agents=["bionic", "hermes"])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "config.json")
            provider_watch.write_config(type("Inv", (), {"runtimes": [rt]})(), path)
            agents, _runtimes = provider_watch.read_config(path)
        self.assertEqual(agents, {"hermes": ["lmstudio"]})

    def test_fixed_and_in_app(self):
        self.assertEqual(connect.FIXED["bionic"], "lmstudio")
        self.assertTrue(connect.is_active("bionic", "lmstudio"))
        self.assertIn("bionic", mcp_ctl.IN_APP)
        self.assertNotIn("bionic", connect.SUPPORTED)


if __name__ == "__main__":
    unittest.main()
