import hashlib
import io
import json
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import addon_api  # noqa: E402
import addon_store as store  # noqa: E402
import addons  # noqa: E402
import ai_core  # noqa: E402

# The plugin takes add-ons as data only (addons.data_only_refusal): the demo is a provider add-on that
# names a built-in provider.
CODE = ""


@unittest.skipUnless(shutil.which("ssh-keygen"), "needs OpenSSH's ssh-keygen")
class StoreTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.keys = tempfile.TemporaryDirectory()
        cls.key = Path(cls.keys.name) / "key"
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "t", "-f", str(cls.key)], check=True)
        cls.signers = f'{addons.PRINCIPAL} namespaces="{addons.NAMESPACE}" ' + \
            " ".join(cls.key.with_suffix(".pub").read_text().split()[:2])

    @classmethod
    def tearDownClass(cls):
        cls.keys.cleanup()

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.user = self.tmp / "addons"
        self.trashed = []
        settings = self.tmp / "settings.json"
        patcher = mock.patch.object(ai_core, "settings_file", return_value=settings)
        patcher.start()
        self.addCleanup(patcher.stop)

    def sign(self, path: Path) -> bytes:
        subprocess.run(["ssh-keygen", "-q", "-Y", "sign", "-f", str(self.key), "-n", addons.NAMESPACE, str(path)],
                       check=True, capture_output=True)
        sig = path.with_name(path.name + ".sig")
        data = sig.read_bytes()
        sig.unlink()
        return data

    def zip_addon(self, aid="demo", version="1.0.0", code=CODE, tamper=False) -> bytes:
        folder = self.tmp / "build" / aid
        shutil.rmtree(folder.parent, ignore_errors=True)
        folder.mkdir(parents=True)
        files = {"addon.json": json.dumps({"id": aid, "name": "Demo", "version": version, "api": 1,
                                           "privacy": "numbers", "kind": "provider", "provides": ["ollama"]})
                 + code}
        for name, text in files.items():
            (folder / name).write_text(text)
        (folder / "MANIFEST.sha256").write_text("".join(
            f"{hashlib.sha256((folder / n).read_bytes()).hexdigest()}  {n}\n" for n in sorted(files)))
        (folder / "SIGNATURE").write_bytes(self.sign(folder / "MANIFEST.sha256"))
        if tamper:
            (folder / "addon.json").write_text((folder / "addon.json").read_text() + "\n ")
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as zf:
            for path in sorted(folder.iterdir()):
                zf.write(path, f"{aid}/{path.name}")
        return buffer.getvalue()

    def install(self, data, **kw):
        return store.install_bytes(data, user_dir=self.user, signers=self.signers, trash=self.trashed.append, **kw)

    def test_found_addons_switch_on_unless_switched_off(self):
        # Maprisma task 055: what is installed is on the map, the user's off switch counts.
        self.assertEqual(store.enable_found(["agent-a", "agent-b"]), ["agent-a", "agent-b"])
        self.assertEqual(store.enabled(), {"agent-a", "agent-b"})
        store.set_enabled("agent-a", False)
        self.assertEqual(store.switched_off(), {"agent-a"})
        self.assertEqual(store.enable_found(["agent-a", "agent-b", "agent-c"]), ["agent-c"])
        self.assertNotIn("agent-a", store.enabled())
        store.set_enabled("agent-a", True)                 # on by hand: no longer remembered as off
        self.assertEqual(store.switched_off(), set())
        self.assertEqual(store.enable_found([]), [])

    # --- catalog ---
    def catalog_bytes(self, entries) -> tuple[bytes, bytes]:
        path = self.tmp / "catalog.json"
        path.write_text(json.dumps({"catalog": 1, "addons": entries}))
        return path.read_bytes(), self.sign(path)

    def entry(self, data=b"zip", **over):
        e = {"id": "demo", "name": "Demo", "version": "1.0.0", "api": 1, "kind": "agent", "privacy": "numbers",
             "summary": "A demo", "url": f"{store.DOWNLOADS}demo-1.0.0/demo-1.0.0.zip", "size": len(data),
             "sha256": hashlib.sha256(data).hexdigest(), "detect": {"commands": ["demo-cli"], "paths": ["~/.demo"]}}
        e.update(over)
        return e

    def zip_of(self, names: dict, link: str = "") -> bytes:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as zf:
            for name, text in names.items():
                zf.writestr(name, text)
            if link:
                info = zipfile.ZipInfo(link)
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
                zf.writestr(info, "/etc/passwd")
        return buffer.getvalue()

class SmallTest(unittest.TestCase):
    def test_newer(self):
        self.assertTrue(store.newer("1.10.0", "1.9.9"))
        self.assertFalse(store.newer("1.0.0", "1.0.0"))
        self.assertFalse(store.newer("x", "1.0.0"))



if __name__ == "__main__":
    unittest.main()
