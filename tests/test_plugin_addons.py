"""The plugin's add-ons: Maprisma's signed add-ons, data only (no add-on code ever runs here)."""

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import addon_api  # noqa: E402
import addons  # noqa: E402
import harnesses  # noqa: E402
import providers  # noqa: E402

BUNDLED = Path(__file__).resolve().parent.parent / "addons"
BOOM = "raise SystemExit('add-on code ran')\n"


@unittest.skipUnless(shutil.which("ssh-keygen"), "needs OpenSSH's ssh-keygen")
class BundledTest(unittest.TestCase):
    def test_every_bundled_addon_is_signed_and_data(self):
        found = [addons.verify(folder) for folder in addons.find((BUNDLED,))]
        self.assertEqual(len(found), 25)
        for addon in found:
            self.assertEqual((addon.id, addon.state, addon.reason), (addon.id, "verified", ""))
            self.assertFalse(addon.brings_code, addon.id)

    def test_switched_on_they_show_their_agents_and_providers(self):
        hooks = addon_api.Hooks()
        try:
            result = addons.load_all({"agent-claude", "llm-ollama"}, dirs=(BUNDLED,), hooks=hooks)
            loaded = {a.id for a in result if a.state == "loaded"}
            self.assertEqual(loaded, {"agent-claude", "llm-ollama"})
            commands = {h.command for h in harnesses.all_harnesses()}
            self.assertIn("claude", commands)
            self.assertNotIn("codex", commands)          # its add-on is there but off
            self.assertIn("ollama", {p.key for p in providers.local()})
        finally:
            addons.load_all(set(), dirs=(), hooks=hooks)  # back to the built-in tables


@unittest.skipUnless(shutil.which("ssh-keygen"), "needs OpenSSH's ssh-keygen")
class DataOnlyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.keys = tempfile.TemporaryDirectory()
        cls.key = Path(cls.keys.name) / "key"
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "test", "-f", str(cls.key)], check=True)
        cls.signers = f'{addons.PRINCIPAL} namespaces="{addons.NAMESPACE}" ' + \
            " ".join(cls.key.with_suffix(".pub").read_text().split()[:2])

    @classmethod
    def tearDownClass(cls):
        cls.keys.cleanup()

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def build(self, aid, meta, files):
        folder = self.root / aid
        folder.mkdir()
        info = {"id": aid, "name": aid, "version": "1.0.0", "api": addon_api.API_VERSION, "privacy": "numbers",
                **meta}
        files = {"addon.json": json.dumps(info), **files}
        for name, text in files.items():
            (folder / name).write_text(text)
        (folder / "MANIFEST.sha256").write_text(
            "".join(f"{hashlib.sha256((folder / n).read_bytes()).hexdigest()}  {n}\n" for n in sorted(files)))
        subprocess.run(["ssh-keygen", "-q", "-Y", "sign", "-f", str(self.key), "-n", addons.NAMESPACE,
                        str(folder / "MANIFEST.sha256")], check=True, capture_output=True)
        (folder / "MANIFEST.sha256.sig").rename(folder / "SIGNATURE")
        for path in folder.rglob("*"):
            path.chmod(0o644)
        folder.chmod(0o755)
        return addons.verify(folder, self.signers)

    def test_an_addon_with_its_own_code_is_refused(self):
        addon = self.build("feature-x", {"kind": "feature"}, {"addon.py": BOOM})
        self.assertEqual(addon.state, "refused")
        self.assertIn("data only", addon.reason)

    def test_hooks_for_an_agent_the_app_does_not_know_are_refused(self):
        toml = 'command = "newagent"\nname = "New"\n'
        addon = self.build("agent-new", {"kind": "agent", "provides": ["newagent"]},
                           {"agent.toml": toml, "hooks.py": BOOM})
        self.assertEqual(addon.state, "refused")

    def test_known_agent_hooks_and_provider_code_are_accepted_but_never_run(self):
        hooks = addon_api.Hooks()
        pi = (Path(__file__).resolve().parent.parent / "adapters/pi.toml").read_text()
        agent = self.build("agent-pi", {"kind": "agent", "provides": ["pi"], "capabilities": ["agent"]},
                           {"agent.toml": pi, "hooks.py": BOOM})
        provider = self.build("llm-ollama", {"kind": "provider", "provides": ["ollama"]}, {"addon.py": BOOM})
        self.assertEqual((agent.state, provider.state), ("verified", "verified"))
        try:
            # BOOM would end the test run if either add-on's code ran.
            result = addons.load_all({"agent-pi", "llm-ollama"}, dirs=(self.root,), signers=self.signers,
                                     hooks=hooks)
            self.assertEqual({(a.id, a.state) for a in result}, {("agent-pi", "loaded"), ("llm-ollama", "loaded")})
            self.assertIn("pi", {h.command for h in harnesses.all_harnesses()})
        finally:
            addons.load_all(set(), dirs=(), hooks=hooks)


if __name__ == "__main__":
    unittest.main()
