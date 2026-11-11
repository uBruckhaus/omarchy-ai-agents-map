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

    def test_catalog_only_with_the_signature(self):
        text, sig = self.catalog_bytes([self.entry()])
        entries = store.parse_catalog(text, sig, self.signers)
        self.assertEqual([(e.id, e.kind, e.commands, e.paths) for e in entries],
                         [("demo", "agent", ["demo-cli"], ["~/.demo"])])
        self.assertIsNone(store.parse_catalog(text, sig))                     # the built-in key: not ours
        self.assertIsNone(store.parse_catalog(text.replace(b"A demo", b"A dEmo"), sig, self.signers))

    def test_entries_are_checked(self):
        bad = [self.entry(url="https://example.com/demo-1.0.0.zip"), self.entry(id="../x"),
               self.entry(size=store.MAX_ZIP + 1), self.entry(capabilities=["root"]),
               self.entry(detect={"commands": ["rm -rf"], "paths": ["/etc/passwd", "~/../x"]}, id="other",
                          url=f"{store.DOWNLOADS}other-1.0.0/other-1.0.0.zip")]
        text, sig = self.catalog_bytes(bad)
        entries = store.parse_catalog(text, sig, self.signers)
        self.assertEqual([(e.id, e.commands, e.paths) for e in entries], [("other", [], [])])

    def test_newest_version_this_maprisma_can_load(self):
        """Older versions stay in the catalog for older Maprisma releases (which take an add-on's first
        entry); this one takes the newest whose needs it meets."""
        def version(v, needs):
            return self.entry(version=v, needs=needs, url=f"{store.DOWNLOADS}demo-{v}/demo-{v}.zip")
        with mock.patch.object(store.addons, "MAPRISMA_VERSION", "0.8.0"):
            text, sig = self.catalog_bytes([version("1.0.1", "maprisma >= 0.7.0"), version("9.0.0", "maprisma >= 9.0.0"),
                                            version("1.1.0", "maprisma >= 0.8.0"), self.entry(id="other", url=
                                            f"{store.DOWNLOADS}other-1.0.0/other-1.0.0.zip")])
            self.assertEqual([(e.id, e.version, e.needs) for e in store.parse_catalog(text, sig, self.signers)],
                             [("demo", "1.1.0", "0.8.0"), ("other", "1.0.0", "")])
        with mock.patch.object(store.addons, "MAPRISMA_VERSION", "0.7.2"):
            self.assertEqual([e.version for e in store.parse_catalog(text, sig, self.signers)], ["1.0.1", "1.0.0"])
        text, sig = self.catalog_bytes([self.entry(needs="soon")])
        self.assertEqual(store.parse_catalog(text, sig, self.signers), [])     # an unreadable needs: left out

    def test_catalog_cache_and_offline(self):
        text, sig = self.catalog_bytes([self.entry()])
        calls = []

        def fetch(url, **kw):
            calls.append(url)
            return text if url == store.CATALOG_URL else sig
        cache = self.tmp / "cache.json"
        self.assertEqual(len(store.catalog(True, fetch, cache, now=1000, signers=self.signers)), 1)
        self.assertEqual(len(store.catalog(True, fetch, cache, now=2000, signers=self.signers)), 1)
        self.assertEqual(len(calls), 2)                                       # the second from the cache
        self.assertEqual(len(store.catalog(False, fetch, cache, now=10 ** 9, signers=self.signers)), 1)
        self.assertEqual(store.catalog(False, fetch, self.tmp / "none.json", signers=self.signers), [])
        self.assertEqual(cache.stat().st_mode & 0o777, 0o600)

    # --- unpacking ---
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

    def test_unpack_refuses_bad_zips(self):
        for data, why in ((self.zip_of({"demo/../../x": "a"}), "bad name"), (self.zip_of({"/abs/x": "a"}), "bad name"),
                          (self.zip_of({"demo/a": "1"}, link="demo/l"), "links"),
                          (self.zip_of({"one/a": "1", "two/b": "2"}), "one add-on"), (b"no zip", "not a zip")):
            target = self.tmp / f"t{len(why)}"
            target.mkdir(exist_ok=True)
            with self.assertRaises(store.InstallError) as caught:
                store.unpack(data, target)
            self.assertIn(why, str(caught.exception))
        self.assertFalse((self.tmp.parent / "x").exists())

    # --- installing ---
    def test_install_verified_only(self):
        addon = self.install(self.zip_addon())
        self.assertEqual((addon.state, addon.id), ("verified", "demo"))
        self.assertTrue((self.user / "demo/addon.json").is_file())
        with self.assertRaises(store.InstallError) as caught:
            self.install(self.zip_addon(aid="other", tamper=True))
        self.assertIn("changed", str(caught.exception))
        self.assertFalse((self.user / "other").exists())
        self.assertEqual([p.name for p in self.user.iterdir()], ["demo"])     # no staging left behind

    def test_update_replaces_and_trashes_the_old(self):
        self.install(self.zip_addon(version="1.0.0"))
        self.install(self.zip_addon(version="1.1.0"))
        self.assertIn('"1.1.0"', (self.user / "demo/addon.json").read_text())
        self.assertEqual(len(self.trashed), 1)
        self.assertTrue(self.trashed[0].name.startswith(".old-demo-"))

    def test_a_failed_update_keeps_the_old(self):
        self.install(self.zip_addon(version="1.0.0"))
        with self.assertRaises(store.InstallError):
            self.install(self.zip_addon(version="1.1.0", tamper=True))
        self.assertIn('"1.0.0"', (self.user / "demo/addon.json").read_text())

    def test_download_must_match_the_catalog(self):
        data = self.zip_addon()
        entry = store.entry_from(self.entry(data, kind="provider", summary=""))   # as the zip declares itself
        addon = store.install(entry, fetch=lambda url, **kw: data, user_dir=self.user, signers=self.signers,
                              trash=self.trashed.append)
        self.assertEqual(addon.state, "verified")
        with self.assertRaises(store.InstallError):
            store.install(entry, fetch=lambda url, **kw: data + b"x", user_dir=self.user, signers=self.signers)

    def test_the_installed_add_on_must_declare_what_the_catalog_showed(self):
        data = self.zip_addon()
        wider = store.entry_from(self.entry(data, kind="provider", summary="", privacy="summary"))   # the confirmation said: summaries
        with self.assertRaises(store.InstallError):
            store.install(wider, fetch=lambda url, **kw: data, user_dir=self.user, signers=self.signers)
        self.assertFalse((self.user / "demo").exists())
        renamed = store.entry_from(self.entry(data, kind="provider", summary="", name="Something else"))
        with self.assertRaises(store.InstallError):
            store.install(renamed, fetch=lambda url, **kw: data, user_dir=self.user, signers=self.signers)
        other_kind = store.entry_from(self.entry(data, kind="agent", summary=""))
        with self.assertRaises(store.InstallError):
            store.install(other_kind, fetch=lambda url, **kw: data, user_dir=self.user, signers=self.signers)

    def test_install_file_and_remove(self):
        path = self.tmp / "demo.zip"
        path.write_bytes(self.zip_addon())
        store.install_file(path, user_dir=self.user, signers=self.signers)
        store.set_enabled("demo", True)
        self.assertEqual(store.enabled(), {"demo"})
        self.assertTrue(store.remove("demo", self.user, trash=self.trashed.append))
        self.assertEqual(store.enabled(), set())
        self.assertFalse(store.remove("../etc", self.user, trash=self.trashed.append))

    def test_the_rules_are_asked_before_anything_changes(self):
        path = self.tmp / "demo.zip"
        path.write_bytes(self.zip_addon())
        asked = []

        def refuse(aid, kind, replacing):
            asked.append((aid, replacing))
            return "Install: not allowed by your rules"
        with self.assertRaises(store.InstallError):
            store.install_file(path, user_dir=self.user, signers=self.signers, allow=refuse)
        self.assertEqual(asked, [("demo", False)])
        self.assertFalse((self.user / "demo").exists())                       # nothing installed
        store.install_file(path, user_dir=self.user, signers=self.signers)
        before = sorted(p.name for p in (self.user / "demo").iterdir())
        with self.assertRaises(store.InstallError):
            store.install_file(path, user_dir=self.user, signers=self.signers, allow=refuse)
        self.assertEqual(asked[-1], ("demo", True))                            # replacing: an update
        self.assertEqual(sorted(p.name for p in (self.user / "demo").iterdir()), before)

    def test_present(self):
        entry = store.entry_from(self.entry())
        with mock.patch.object(store.shutil, "which", return_value=None), \
                mock.patch.object(Path, "home", return_value=self.tmp):
            self.assertFalse(store.present(entry))
            (self.tmp / ".demo").mkdir()
            self.assertTrue(store.present(entry))
        with mock.patch.object(store.shutil, "which", return_value="/usr/bin/demo-cli"):
            self.assertTrue(store.present(entry))


class SmallTest(unittest.TestCase):
    def test_newer(self):
        self.assertTrue(store.newer("1.10.0", "1.9.9"))
        self.assertFalse(store.newer("1.0.0", "1.0.0"))
        self.assertFalse(store.newer("x", "1.0.0"))



if __name__ == "__main__":
    unittest.main()
