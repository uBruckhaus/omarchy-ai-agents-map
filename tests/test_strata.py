"""Strata (provider_strata, install_ctl.Strata): what is read and what is refused. Throwaway HOME and
XDG folders only; nothing is started and nothing leaves the machine."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import inventory  # noqa: E402
import install_ctl  # noqa: E402
import provider_strata as ps  # noqa: E402
from agent_details import provider_for_url  # noqa: E402


class Home:
    """A HOME with XDG folders inside it, PATH limited to the given folders."""

    def __init__(self):
        self._temp = tempfile.TemporaryDirectory()
        self.home = Path(self._temp.name)
        self.data = self.home / ".local/share/strata"

    def __enter__(self):
        env = {"HOME": str(self.home), "XDG_DATA_HOME": str(self.home / ".local/share"),
               "XDG_CONFIG_HOME": str(self.home / ".config"), "PATH": str(self.home / "nothing")}
        self._env = mock.patch.dict(os.environ, env)
        self._env.start()
        self._home = mock.patch("pathlib.Path.home", return_value=self.home)
        self._home.start()
        self._inventory = mock.patch.object(inventory, "HOME", self.home)
        self._inventory.start()
        return self

    def __exit__(self, *exc):
        self._inventory.stop()
        self._home.stop()
        self._env.stop()
        self._temp.cleanup()

    def terminal(self, link=True):
        version = self.data / "terminal/0.1.57"
        (version / "bin").mkdir(parents=True)
        (version / "bin/strata").write_text("#!/bin/sh\n")
        os.chmod(version / "bin/strata", 0o755)
        (version / "VERSION").write_text("0.1.57\n")
        (self.data / "terminal/current").symlink_to(version)
        if link:
            (self.home / ".local/bin").mkdir(parents=True, exist_ok=True)
            (self.home / ".local/bin/strata").symlink_to(self.data / "terminal/current/bin/strata")

    def preset(self, slug, **fields):
        folder = self.home / ".config/strata/launch-presets"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{slug}.json").write_text(json.dumps(fields))


class FindTest(unittest.TestCase):
    def test_another_program_called_strata_is_not_strata(self):
        with Home() as h:
            other = h.home / "usr/bin"
            other.mkdir(parents=True)
            (other / "strata").write_text("#!/bin/sh\n")
            os.chmod(other / "strata", 0o755)
            os.environ["PATH"] = str(other)
            self.assertIsNone(ps.strata_binary())
            self.assertFalse(inventory.strata([]).installed)
            h.terminal()
            self.assertEqual(ps.strata_binary(), str(h.home / ".local/bin/strata"))   # not /usr/bin's
            self.assertTrue(inventory.strata([]).installed)

    def test_a_link_out_of_its_terminal_is_not_strata(self):
        with Home() as h:
            h.terminal(link=False)
            elsewhere = h.home / "elsewhere/strata"
            elsewhere.parent.mkdir()
            elsewhere.write_text("#!/bin/sh\n")
            os.chmod(elsewhere, 0o755)
            (h.home / ".local/bin").mkdir(parents=True)
            (h.home / ".local/bin/strata").symlink_to(elsewhere)
            # Its own terminal's strata is still found.
            self.assertEqual(ps.strata_binary(), str(h.data / "terminal/current/bin/strata"))

    def test_a_huge_folder_is_read_only_up_to_its_limit(self):
        # Review 064 round 2: never every entry of a folder, as the Dart core's ScanFiles.list (4000).
        with Home() as h, mock.patch.object(ps, "LIST_LIMIT", 6):
            install = h.data / "install"
            install.mkdir(parents=True)
            for i in range(20):
                (install / f"strata-m{i:02d}.json").write_text("{}")
                h.preset(f"p{i:02d}", last_used=f"2026-11-05T10:{i:02d}:00")
            read = []
            real = ps._listed

            def counted(folder):
                found = real(folder)
                read.append(len(found))
                return found
            with mock.patch.object(ps, "_listed", counted):
                self.assertLessEqual(len(ps.model_ids()), 6)
                ps.last_preset()
            self.assertEqual(read, [6, 6])

    def test_models_are_its_run_configs_only(self):
        with Home() as h:
            install = h.data / "install"
            install.mkdir(parents=True)
            for name in ("strata-qwen-q2_0.json", "strata-coder.iq1_m.json", "strata-Upper.json",
                         "strata-a..b.json", "strata-x.shared-settings.json", "notes.json"):
                (install / name).write_text("{}")
            (install / "strata-linked.json").symlink_to(install / "notes.json")
            (install / "strata-folder.json").mkdir()
            self.assertEqual(ps.model_ids(), ["coder.iq1_m", "qwen-q2_0"])
            rt = inventory.strata([])
            self.assertEqual(rt.models_dir, "~/.local/share/strata/install")

    def test_the_preset_used_last_and_never_its_key(self):
        with Home() as h:
            self.assertEqual(ps.last_preset(), ("", 0))
            h.preset("office", port=8090, last_used="2026-11-05 10:00:00.000000", api_key="secret")
            h.preset("older", port=1234, last_used="2026-10-01 10:00:00.000000")
            h.preset("weird", port="x", last_used=5, created="2026-01-01")
            h.preset("Bad Name", port=9, last_used="2099-01-01")
            (h.home / ".config/strata/launch-presets/.hidden.json").write_text('{"last_used": "2099"}')
            (h.home / ".config/strata/launch-presets/huge.json").write_text(
                json.dumps({"last_used": "2099", "pad": "x" * (ps.MAX_JSON + 1)}))
            self.assertEqual(ps.last_preset(), ("office", 8090))
            h.preset("nonport", port=True, last_used="2027-01-01")
            self.assertEqual(ps.last_preset(), ("nonport", ps.DEFAULT_PORT))
            self.assertEqual(inventory.strata([]).address, "127.0.0.1:8080")

    def test_its_server_and_port(self):
        with Home() as h:
            config = h.home / "run.json"
            config.write_text(json.dumps({"port": 8123}))
            exe = str(h.data / "terminal/0.1.57/python/bin/python3.12")
            Path(exe).parent.mkdir(parents=True)
            Path(exe).write_text("")
            server = [exe, "-m", "serve.server", "--config", str(config)]
            self.assertTrue(ps.is_server(server))
            self.assertFalse(ps.is_server(["/usr/bin/python3", "-m", "serve.server"]))
            self.assertFalse(ps.is_server([exe, "-m", "strata_control.runtime"]))
            self.assertEqual(ps.server_port(server), 8123)
            self.assertEqual(ps.server_port(server + ["--port", "9000"]), 9000)
            self.assertEqual(ps.server_port(server + ["--port=70000"]), 8123)
            self.assertEqual(ps.server_port([exe, "-m", "serve.server", "--port", "--8080"]), ps.DEFAULT_PORT)
            self.assertEqual(ps.server_port([exe, "-m", "serve.server", "--config", "relative.json"]), ps.DEFAULT_PORT)
            # Review 064 #1: an oversized, signed or non-ASCII --port never breaks the scan; the config wins.
            for odd in ("9" * 5000, "99999999999999999999", "-8080", "８０８０", "²", "0", "65536", "1e3", " 80"):
                self.assertEqual(ps.server_port(server + ["--port", odd]), 8123, odd)
                self.assertEqual(ps.server_port(server + [f"--port={odd}"]), 8123, odd)
            self.assertEqual(ps.server_port(server + ["--port", "x", "--port", "9001"]), 9001)
            rt = inventory.strata([(1, "python3.12", server)])
            self.assertEqual((rt.running, rt.installed, rt.address), (True, True, "127.0.0.1:8123"))

    def test_ownership_only_inside_its_own_folders(self):
        """Review 064 #2: its server and engine are the executables inside this user's own Strata
        folders, compared as resolved paths; another path that merely contains /strata/terminal/ is not."""
        with Home() as h:
            engine = ps.Engine()
            python = h.data / "terminal/0.1.57/python/bin/python3.12"
            binary = h.data / "components/engine/0.1.57-linux-x86_64-cuda/strata"
            for path in (python, binary):
                path.parent.mkdir(parents=True)
                path.write_text("")
            elsewhere = h.home / "tmp/strata/terminal/0.1.57/python/bin/python3.12"
            elsewhere.parent.mkdir(parents=True)
            elsewhere.write_text("")
            inside_link = h.home / "link-python"
            inside_link.symlink_to(python)
            outside_link = h.data / "terminal/0.1.57/python/bin/evil"
            outside_link.symlink_to(elsewhere)
            server = ["-m", "serve.server", "--config", "/x"]
            self.assertEqual(engine.owns_process("python3.12", [str(python)]), 2)
            self.assertEqual(engine.owns_process("strata", [str(binary)]), 2)
            self.assertEqual(engine.owns_process("python3.12", [str(inside_link)]), 2)
            self.assertTrue(ps.is_server([str(python), *server]))
            for exe in (str(elsewhere), str(outside_link), "/usr/bin/strata", "relative/strata/terminal/x",
                        str(h.data / "terminal/../../elsewhere/strata/terminal/x"), str(h.data / "terminal/missing")):
                self.assertEqual(engine.owns_process("python3.12", [exe]), 0, exe)
                self.assertFalse(ps.is_server([exe, *server]), exe)
            self.assertFalse(ps.is_server([str(binary), *server]))      # the engine is no server


class SharedPortTest(unittest.TestCase):
    def test_the_provider_it_names_wins_on_a_shared_port(self):
        llama = inventory.Runtime("llamacpp", "llama.cpp", installed=True, address="127.0.0.1:8080")
        strata = inventory.Runtime("strata", "Strata", installed=True, address="127.0.0.1:8080")
        ours = provider_for_url("strata-local", "http://127.0.0.1:8080/v1")
        self.assertEqual(ours.runtime, "Strata")
        self.assertIs(inventory.local_runtime(ours, [llama, strata]), strata)
        theirs = provider_for_url("llama-local", "http://127.0.0.1:8080/v1")
        self.assertIs(inventory.local_runtime(theirs, [llama, strata]), llama)
        other = provider_for_url("mine", "http://127.0.0.1:8080/v1")
        self.assertEqual(other.runtime, "llama.cpp")          # the usual port's first one, as before
        self.assertIs(inventory.local_runtime(other, [llama, strata]), llama)


class InstallerTest(unittest.TestCase):
    def release(self):
        return install_ctl.Release("v0.2.11", [("install.sh", "https://x/install.sh", 10)], {"install.sh": "a" * 64})

    def run_install(self, inst, folder, release=None):
        inst.install(folder, None, False, False, None, release or self.release(), lambda *_a: None, lambda: False)

    def test_refused_before_anything_is_downloaded(self):
        with Home() as h, mock.patch.object(install_ctl, "BIN_DIR", h.home / ".local/bin"), \
                mock.patch.object(install_ctl, "download", side_effect=AssertionError("downloaded")):
            inst = install_ctl.Strata()
            folder = inst.default_folder
            self.assertEqual(folder, h.data / "terminal")
            with self.assertRaisesRegex(RuntimeError, "its own folder"):
                self.run_install(inst, h.home / "elsewhere")
            with self.assertRaisesRegex(RuntimeError, "no SHA-256"):
                self.run_install(inst, folder, install_ctl.Release("v0.2.11", [("install.sh", "u", 1)], {}))
            with self.assertRaisesRegex(RuntimeError, "no SHA-256"):
                self.run_install(inst, folder, install_ctl.Release(
                    "v0.2.11", [("other.sh", "u", 1)], {"other.sh": "a" * 64}))
            # The file manager's (or any) ~/.local/bin/strata is never replaced.
            (h.home / ".local/bin").mkdir(parents=True)
            (h.home / ".local/bin/strata").write_text("#!/bin/sh\n")
            with self.assertRaisesRegex(RuntimeError, "another program"):
                self.run_install(inst, folder)
            (h.home / ".local/bin/strata").unlink()
            # Strata installed by itself (no marker): updated with its own `strata update`.
            h.terminal(link=False)
            with self.assertRaisesRegex(RuntimeError, "installed already"):
                self.run_install(inst, folder)

    def test_versions_and_uninstall_leave_its_data(self):
        with Home() as h, mock.patch.object(install_ctl, "BIN_DIR", h.home / ".local/bin"):
            h.terminal()
            inst = install_ctl.Strata()
            folder = inst.default_folder
            self.assertIsNone(inst.installed_folder())            # installed by itself: not ours
            inst.record_version(folder, "0.1.57")
            self.assertEqual(inst.installed_folder(), folder)
            self.assertEqual(inst.current_version(), "0.1.57")
            newer = folder / "0.1.58"
            (newer / "bin").mkdir(parents=True)
            (newer / "bin/strata").write_text("")
            (newer / "VERSION").write_text("0.1.58\n")
            self.assertEqual(set(inst.versions(folder)), {"0.1.57", "0.1.58"})
            (h.data / "install").mkdir()
            (h.data / "install/strata-qwen.json").write_text("{}")
            (folder / "notes.txt").write_text("the user's")
            with mock.patch.object(install_ctl.ai_core, "update_settings"):
                inst.uninstall()
            self.assertFalse((h.home / ".local/bin/strata").exists())
            self.assertEqual(sorted(p.name for p in folder.iterdir()), ["notes.txt"])
            self.assertTrue((h.data / "install/strata-qwen.json").exists())


class EngineTest(unittest.TestCase):
    def test_start_needs_a_preset_and_uses_the_last_one(self):
        with Home() as h:
            engine, rt = ps.Engine(), NS(installed=True, key="strata", address="127.0.0.1:8080")
            self.assertFalse(engine.can_control(rt))
            h.terminal()
            self.assertFalse(engine.can_control(rt))            # no preset yet
            self.assertEqual(engine.start(rt)[0], False)
            h.preset("office", port=8090, last_used="2026-11-05")
            self.assertTrue(engine.can_control(rt))
            calls = []
            with mock.patch("ai_core.read_bounded_output", side_effect=lambda argv, **kw: calls.append(
                    (argv, kw["timeout"])) or (b"Strata is serving.\n", True)):
                self.assertEqual(engine.start(rt), (True, "Strata is serving."))
            self.assertEqual(calls, [([str(h.home / ".local/bin/strata"), "launch", "--preset", "office", "--yes",
                                       "--no-tui"], ps.START_TIMEOUT)])
            self.assertFalse(engine.starts_with_agents)


class PortTextTest(unittest.TestCase):
    """Review 064 #1, the class: a port written as text never fails a scan, the watcher's config or an
    address (gpu_usage.port_number, its copy in provider_watch)."""

    def test_port_number(self):
        import gpu_usage
        for text, port in (("8080", 8080), ("65535", 65535), ("1", 1), ("²", 0), ("８０", 0), ("-1", 0),
                           ("+80", 0), (" 80", 0), ("0", 0), ("65536", 0), ("9" * 5000, 0), ("", 0)):
            self.assertEqual(gpu_usage.port_number(text), port, text)
        for address in ("127.0.0.1:²", "127.0.0.1:" + "9" * 30, "127.0.0.1:-1", "nothing"):
            self.assertEqual(gpu_usage.port_of(NS(address=address)), 0, address)

    def test_the_watcher_config_takes_any_address(self):
        import provider_watch
        for address in ("127.0.0.1:²", "127.0.0.1:" + "9" * 30):
            rts = [NS(key="koboldcpp", installed=True, agents=["pi"], active_agents=["pi"], service="koboldcpp",
                      address=address)]
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "c.json"
                self.assertTrue(provider_watch.write_config(NS(runtimes=rts), path))
                self.assertEqual(json.loads(path.read_text())["runtimes"]["koboldcpp"]["port"], 0)


class ConfigureTest(unittest.TestCase):
    """Review 064 #3: Configure… and its --help fallback run Strata's own strata, never one by name
    (/usr/bin/strata is a file manager)."""

    def test_configure_runs_the_verified_binary(self):
        import configure_ctl
        with Home() as h:
            other = h.home / "usr/bin"
            other.mkdir(parents=True)
            (other / "strata").write_text("#!/bin/sh\n")
            os.chmod(other / "strata", 0o755)
            os.environ["PATH"] = f"{other}:{h.home / '.local/bin'}"
            self.assertEqual(configure_ctl.for_runtime("strata"), [])      # only the file manager here
            self.assertEqual(configure_ctl.provider_program("strata"), "")
            h.terminal()
            own = str(h.home / ".local/bin/strata")
            entries = configure_ctl.for_runtime("strata")
            self.assertEqual([e.argv for e in entries], [(own, "launch"), (own, "launch", "--status")])
            self.assertTrue(all(own in configure_ctl.script(e, "done") for e in entries))
            self.assertEqual(configure_ctl.provider_program("strata"), own)


if __name__ == "__main__":
    unittest.main()
