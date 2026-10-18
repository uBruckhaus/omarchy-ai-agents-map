import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import isolate  # noqa: E402
import connect  # noqa: E402
import mcp_ctl  # noqa: E402
import mcp_library  # noqa: E402
import mcp_scan  # noqa: E402
import mcp_update  # noqa: E402

USAGE = b"\0protocolVersion\0  gridapp-cli mcp [--connect PORT]\n"


class UpdateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        isolate.desktop_in(self, self.home)
        for module in (connect, mcp_ctl, mcp_library, mcp_update):
            patcher = mock.patch.object(module, "HOME", self.home)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(mcp_update, "LINK_DIRS", (self.home / ".local/bin",))
        patcher.start()
        self.addCleanup(patcher.stop)
        for name, value in (("HOME", self.home), ("CACHE", self.home / "cache/appimages"),
                            ("SCAN_CACHE", self.home / "cache/scan.json"), ("_CACHE", None),
                            ("OPT", self.home / "opt")):
            patcher = mock.patch.object(mcp_scan, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.dict(os.environ, {"PATH": str(self.home / "nowhere")})
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def program(self, path: Path, body: bytes = USAGE):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x7fELF" + body)
        os.chmod(path, 0o755)
        return path

    def test_versions(self):
        self.assertEqual(mcp_scan.app_version("gridcraft-0.10.0"), ("gridcraft", (0, 10, 0)))
        self.assertIsNone(mcp_scan.app_version("gridcraft"))
        self.assertGreater(mcp_scan.app_version("x-0.10.0")[1], mcp_scan.app_version("x-0.9.2")[1])

    def test_newest_only_and_new_versions_looked_into(self):
        old = self.program(self.home / "Applications/gridapp-0.6.1/usr/bin/gridapp-cli")
        image = self.program(self.home / "Applications/gridapp-0.7.0-linux-x86_64.AppImage", b"runtime")
        other = self.program(self.home / "Applications/otherapp-1.0-linux-x86_64.AppImage", b"runtime")
        # The new version of an app known before is named, never run (not even to look inside).
        with mock.patch("ai_core.read_bounded_output") as run, mock.patch("subprocess.run") as sub, \
                mock.patch("subprocess.Popen") as popen:
            self.assertEqual(mcp_scan.new_versions_found(), [image])
        for called in (run, sub, popen):
            called.assert_not_called()
        self.assertIn(other, mcp_scan.not_looked_into())
        mcp_scan._cache()[mcp_scan._image_key(image)] = [("gridapp-cli", 1, "gridapp-cli mcp [--connect PORT]")]
        found = mcp_scan.scan({str(old): ["claude"]})
        self.assertEqual(len(found), 1)
        newest = found[0]
        self.assertEqual((newest.version, newest.update_from, newest.users), ("0.7.0", "0.6.1", ["claude"]))
        self.assertEqual(newest.program, str(self.home / "Applications/gridapp-0.7.0/usr/bin/gridapp-cli"))
        self.assertEqual(newest.old_folders, [str(self.home / "Applications/gridapp-0.6.1")])

    def test_plan_moves_agents_list_and_links_undo_restores(self):
        old_folder = self.home / "Applications/gridapp-0.6.1"
        old = self.program(old_folder / "usr/bin/gridapp-cli")
        new = self.program(self.home / "Applications/gridapp-0.7.0/usr/bin/gridapp-cli")
        link = self.home / ".local/bin/gridapp-cli"
        link.parent.mkdir(parents=True)
        link.symlink_to(old)
        entry = {"transport": "stdio", "command": str(old), "args": ["mcp", "--in", str(old_folder / "share/x")],
                 "env": {}, "url": "", "headers": {}}
        by_link = {**entry, "command": str(link), "args": ["mcp"]}
        connect.apply(connect.merge([mcp_ctl.plan("pi", "gridapp", entry=entry),
                                     mcp_ctl.plan("gemini", "grid2", entry=by_link)]), self.home / "b")
        mcp_library.remember({"gridapp": mcp_ctl.check_entry(entry)})
        candidate = next(c for c in mcp_scan.scan({str(old): ["pi"]}) if c.name == "gridapp")
        self.assertEqual((candidate.program, candidate.update_from), (str(new), "0.6.1"))
        plan = mcp_update.plan(candidate, ["pi", "gemini"])
        backup = connect.apply(plan, self.home / "b")
        pi = mcp_ctl.normal(mcp_ctl.raw_entries("pi")["gridapp"])
        self.assertEqual(pi["command"], str(new))
        self.assertEqual(pi["args"][-1], str(self.home / "Applications/gridapp-0.7.0/share/x"))
        self.assertEqual(mcp_library.load()["gridapp"]["command"], str(new))
        # Started through the link: the entry stays, the link moves.
        self.assertEqual(mcp_ctl.normal(mcp_ctl.raw_entries("gemini")["grid2"])["command"], str(link))
        self.assertEqual(os.readlink(link), str(new))
        self.assertFalse(old_folder.exists())
        self.assertTrue(any(backup.glob("retired-*")))
        connect.undo(backup)
        self.assertEqual(mcp_ctl.normal(mcp_ctl.raw_entries("pi")["gridapp"])["command"], str(old))
        self.assertEqual(os.readlink(link), str(old))
        self.assertTrue(old.exists())

    def test_plan_all_updates_several_apps_in_the_same_files(self):
        olds, news = {}, {}
        for app in ("gridapp", "figapp"):
            olds[app] = self.program(self.home / f"Applications/{app}-0.6.1/usr/bin/{app}-cli",
                                     USAGE.replace(b"gridapp", app.encode()))
            news[app] = self.program(self.home / f"Applications/{app}-0.7.0/usr/bin/{app}-cli",
                                     USAGE.replace(b"gridapp", app.encode()))
        entries = {app: {"transport": "stdio", "command": str(olds[app]), "args": ["mcp"], "env": {}, "url": "",
                         "headers": {}} for app in olds}
        for agent in ("pi", "claude"):
            for app in olds:
                connect.apply(mcp_ctl.plan(agent, app, entry=entries[app]), self.home / "b")
        mcp_library.remember({app: mcp_ctl.check_entry(entries[app]) for app in olds})
        used = {str(p): ["pi", "claude"] for p in olds.values()}
        candidates = [c for c in mcp_scan.scan(used) if c.update_from]
        self.assertEqual(sorted(c.name for c in candidates), ["figapp", "gridapp"])
        plan = mcp_update.plan_all(candidates, ["pi", "claude"])
        self.assertEqual(len({c.path for c in plan.changes}), len(plan.changes))   # one change per file
        backup = connect.apply(plan, self.home / "b")
        for agent in ("pi", "claude"):
            for app in olds:
                self.assertEqual(mcp_ctl.normal(mcp_ctl.raw_entries(agent)[app])["command"], str(news[app]))
        self.assertEqual({n: e["command"] for n, e in mcp_library.load().items()},
                         {app: str(news[app]) for app in olds})
        self.assertEqual(sorted(mcp_update.updated_entries(candidates, ["pi", "claude"])), ["figapp", "gridapp"])
        for app in olds:
            self.assertFalse(olds[app].exists())
        connect.undo(backup)
        for agent in ("pi", "claude"):
            for app in olds:
                self.assertEqual(mcp_ctl.normal(mcp_ctl.raw_entries(agent)[app])["command"], str(olds[app]))
        self.assertTrue(all(p.exists() for p in olds.values()))
        self.assertIsNone(getattr(connect._STAGED, "texts", None))

    def test_prune_retired(self):
        root = self.home / "b"
        (root / "20200101-000000-000000/retired-0-app-1.0").mkdir(parents=True)
        fresh = root / connect.dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f") / "retired-0-app-1.1"
        fresh.mkdir(parents=True)
        connect.prune_retired(root)
        self.assertFalse((root / "20200101-000000-000000/retired-0-app-1.0").exists())
        self.assertTrue(fresh.exists())


if __name__ == "__main__":
    unittest.main()
