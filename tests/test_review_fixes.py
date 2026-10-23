"""Fixes from the code review of 2026-10-23: the poll's refresh guard, batteries past their reset or
without their record, model advice kept with its own page, the background viewer's session file
and printed paths, and Connect's check right before writing."""
import datetime as dt
import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import agent_details  # noqa: E402
import connect  # noqa: E402
import session_follow  # noqa: E402


def popup():
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Gdk", "4.0")
    import ai_popup
    return ai_popup


class RefreshGuardTest(unittest.TestCase):
    def test_a_copy_of_the_same_runtimes_counts_as_the_same(self):
        same = popup().same_objects
        a, b = object(), object()
        current = [a, b]
        self.assertTrue(same(list(current), current))            # the poll's copy: was always False with `is`
        self.assertFalse(same([a, object()], current))           # read again meanwhile
        self.assertFalse(same([a], current))
        self.assertNotIn("runtimes is self.inventory.runtimes", Path(popup().__file__).read_text())


class Limit:
    def __init__(self, percent, resets_at, label="5h"):
        self.percent, self.resets_at, self.label = percent, resets_at, label


class BatteryTest(unittest.TestCase):
    def test_records_written_again_new_or_gone(self):
        renewed = agent_details.renewed_records
        self.assertEqual(renewed(None, {"codex": 1}, {"codex"}), set())                 # the first poll
        self.assertEqual(renewed({"codex": 1, "claude": 1}, {"codex": 2, "claude": 1}, {"codex", "claude"}), {"codex"})
        self.assertEqual(renewed({"codex": 1}, {}, {"codex"}), {"codex"})               # its record went
        self.assertEqual(renewed({}, {"pi": 1}, {"pi"}), {"pi"})
        self.assertEqual(renewed({}, {"pi": 1}, {"codex"}), set())                      # not installed

    def test_next_reset_is_the_soonest_to_come(self):
        now = dt.datetime(2026, 10, 23, 12, tzinfo=dt.timezone.utc)
        soon, later = now + dt.timedelta(hours=1), now + dt.timedelta(days=2)
        limits = [Limit(0.5, later), Limit(0.95, soon), Limit(0.1, now - dt.timedelta(hours=1)), Limit(0.2, None)]
        self.assertEqual(agent_details.next_reset(limits, now), soon)
        self.assertIsNone(agent_details.next_reset([Limit(0.2, None)], now))

    def test_a_red_battery_turns_green_once_its_limit_resets(self):
        ai_popup = popup()

        class View:
            set_agent_battery = ai_popup.AgentsMap.set_agent_battery
            batteries_past_reset = ai_popup.AgentsMap.batteries_past_reset
            shown = 0

            def warn_red(self, *_args):
                pass

            def show_batteries(self):
                self.shown += 1

        view = View()
        now = dt.datetime.now(dt.timezone.utc)
        session = Limit(0.97, now + dt.timedelta(seconds=60))
        week = Limit(0.2, now + dt.timedelta(days=3), "week")
        view.set_agent_battery("codex", [session, week], show=False)
        self.assertEqual(view.agent_batteries["agent:codex"][1], "red")
        view.batteries_past_reset()
        self.assertEqual(view.agent_batteries["agent:codex"][1], "red")                 # not reset yet
        session.resets_at = now - dt.timedelta(seconds=1)                               # its session window ended
        view.battery_resets["codex"] = (session.resets_at, [session, week])
        view.batteries_past_reset()
        self.assertEqual(view.agent_batteries["agent:codex"][1], "green")               # only the week counts now
        self.assertEqual(view.battery_resets["codex"][0], week.resets_at)
        self.assertEqual(view.shown, 1)


class ModelAdviceTest(unittest.TestCase):
    """Ask on runtime A's page, open B's, ask there: A's answer stays with A."""

    def test_an_answer_goes_to_the_page_that_asked(self):
        import models_page
        calls = []

        class View:
            ask_model_advice = models_page.ModelsPage.ask_model_advice
            models_ask_text = "a coding model"
            shown = []

            def fill_model_ask(self):
                calls.append(self.models_state["rt"].name)

            def show_model_suggestions(self, state):
                self.shown.append(state["rt"].name)

            def toast(self, *_args):
                pass

        class Rt:
            def __init__(self, name):
                self.name, self.models = name, []

        view = View()
        page_a = {"rt": Rt("A"), "vram": 0, "ram": 0}
        page_b = {"rt": Rt("B"), "vram": 0, "ram": 0}
        pending = []
        gpu = mock.Mock(vram=0, vendor="amd")
        gpu.name = "gpu"
        with mock.patch.object(models_page.threading, "Thread",
                               lambda target, daemon: mock.Mock(start=lambda: pending.append(target))), \
                mock.patch.object(models_page.GLib, "idle_add", lambda fn, *a: fn(*a)), \
                mock.patch.object(models_page.install_ctl, "detect_gpu", return_value=gpu), \
                mock.patch.object(models_page.model_advice, "ask",
                                  side_effect=lambda *a, **k: mock.Mock(models=[], agent="claude", tag=f"answer for {a[2]}")):
            view.models_state = page_a
            view.ask_model_advice("claude")
            self.assertTrue(page_a["asking"])
            view.models_state = page_b                    # another runtime's page opened meanwhile
            view.ask_model_advice("claude")               # not blocked by A's request
            self.assertTrue(page_b["asking"])
            pending.pop(0)()                              # A answers first
            self.assertEqual(page_a.get("advice").tag, "answer for A")
            self.assertTrue(page_b["asking"])             # B still waits
            self.assertNotIn("advice", page_b)
            pending.pop(0)()
            self.assertEqual(page_b["advice"].tag, "answer for B")
            self.assertFalse(page_b["asking"])


class SessionFileTest(unittest.TestCase):
    """Two instances in one folder: each follows its own session file."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        for patcher in (mock.patch.object(session_follow, "session_folder", return_value=self.folder),
                        mock.patch.object(session_follow, "_cwd", return_value="/x")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def session(self, name, begun, written):
        path = self.folder / name
        stamp = dt.datetime.fromtimestamp(begun, dt.timezone.utc).isoformat().replace("+00:00", "Z")
        path.write_text(json.dumps({"type": "session", "timestamp": stamp}) + "\n")
        os.utime(path, (written, written))
        return path

    def test_each_instance_its_own_file(self):
        t = time.time() - 3600
        a = self.session("a.jsonl", t + 1, t + 900)           # A started at t, B at t+600
        b = self.session("b.jsonl", t + 601, t + 1000)        # B's is the newest
        pick = session_follow.session_file
        self.assertEqual(pick("pi-files", 999999, t, siblings=[t + 600]), a)
        self.assertEqual(pick("pi-files", 999999, t + 600, siblings=[t]), b)

    def test_a_new_file_after_clear_and_a_resumed_one(self):
        t = time.time() - 3600
        self.session("first.jsonl", t + 1, t + 100)
        cleared = self.session("cleared.jsonl", t + 200, t + 300)  # /clear in the same instance
        self.assertEqual(session_follow.session_file("claude-files", 999999, t, siblings=[]), cleared)
        for path in self.folder.iterdir():
            path.unlink()
        resumed = self.session("old.jsonl", t - 86400, t + 50)     # begun yesterday, resumed now
        self.assertEqual(session_follow.session_file("claude-files", 999999, t, siblings=[]), resumed)
        self.assertIsNone(session_follow.session_file("claude-files", 999999, t, siblings=[t - 10]))  # whose?

    def test_printed_paths_lose_escape_sequences(self):
        out = io.StringIO()
        with mock.patch.object(session_follow, "output_file", return_value=None), \
                mock.patch.object(session_follow, "session_file", return_value=None), \
                mock.patch.object(session_follow, "_cwd", return_value="/tmp/\x1b]52;c;aGk=\x07x"), \
                mock.patch.object(session_follow, "_started", return_value=0.0), \
                mock.patch.object(session_follow, "_identity", return_value=1.0):
            session_follow.follow("-", 4242, "Pi\x1b[2J", out=out)
        self.assertNotIn("\x1b", out.getvalue())
        self.assertNotIn("\x07", out.getvalue())


class ApplyTest(unittest.TestCase):
    def test_a_file_rewritten_while_commands_ran_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "settings.json"
            target.write_text("old")
            plan = connect.Plan("claude", "", "", changes=[connect.Change(target, "old", "planned")],
                                commands=[["true"]])

            def agent_writes(_argv):
                target.write_text("the agent's newer text")

            with mock.patch.object(connect, "_run", agent_writes):
                with self.assertRaises(RuntimeError):
                    connect.apply(plan, Path(tmp) / "backups")
            self.assertEqual(target.read_text(), "the agent's newer text")
            self.assertFalse((Path(tmp) / "backups").exists())


class ArchiveTest(unittest.TestCase):
    """A release archive is measured and checked before anything is unpacked."""

    def make(self, entries, name="a.tar.gz"):
        import tarfile
        path = Path(self.tmp.name) / name
        mode = "w:zst" if name.endswith(".zst") else "w:gz"
        with tarfile.open(path, mode) as archive:
            for entry in entries:
                info = tarfile.TarInfo(entry["name"])
                info.type = entry.get("type", tarfile.REGTYPE)
                info.linkname = entry.get("link", "")
                info.mode = 0o755 if info.type == tarfile.DIRTYPE else 0o644
                data = entry.get("data", b"")
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data) if data else None)
        return path

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_a_good_release_with_its_own_links(self):
        import install_ctl
        import tarfile
        good = self.make([{"name": "bin", "type": tarfile.DIRTYPE}, {"name": "bin/ollama", "data": b"x" * 10},
                          {"name": "lib/libx.so", "type": tarfile.SYMTYPE, "link": "libx.so.1"},
                          {"name": "lib/libx.so.1", "type": tarfile.SYMTYPE, "link": "libx.so.1.2"},
                          {"name": "lib/libx.so.1.2", "data": b"y"},
                          {"name": "share/doc", "type": tarfile.SYMTYPE, "link": "../lib/libx.so"}],
                         "good.tar.zst")
        install_ctl.check_archive(good, limit=100)
        folder = Path(self.tmp.name) / "out"
        install_ctl.extract(good, folder)
        self.assertEqual((folder / "bin/ollama").read_bytes(), b"x" * 10)

    def test_bad_archives_are_refused_before_unpacking(self):
        import install_ctl
        import tarfile
        cases = {
            "too large": [{"name": "big", "data": b"z" * 2000}],
            "dot-dot": [{"name": "../evil", "data": b"x"}],
            "absolute": [{"name": "/etc/evil", "data": b"x"}],
            "absolute link": [{"name": "l", "type": tarfile.SYMTYPE, "link": "/home"}],
            "escaping link": [{"name": "a/l", "type": tarfile.SYMTYPE, "link": "../../x"}],
            "fifo": [{"name": "p", "type": tarfile.FIFOTYPE}],
            "composed links": [{"name": "a", "type": tarfile.SYMTYPE, "link": "."},
                               {"name": "b", "type": tarfile.SYMTYPE, "link": "a/.."},
                               {"name": "copied", "type": tarfile.LNKTYPE, "link": "b/outside"}],
            "write through a link": [{"name": "d", "type": tarfile.SYMTYPE, "link": "lib"},
                                     {"name": "d/x", "data": b"x"}],
            "dot link then climb": [{"name": "a", "type": tarfile.SYMTYPE, "link": "."},
                                    {"name": "b", "type": tarfile.SYMTYPE, "link": "a/../outside"}],
            "hardlink to a link": [{"name": "s", "type": tarfile.SYMTYPE, "link": "x"},
                                   {"name": "x", "data": b"x"},
                                   {"name": "h", "type": tarfile.LNKTYPE, "link": "s"}],
            "link defined after use": [{"name": "d/x", "data": b"x"},
                                       {"name": "d", "type": tarfile.SYMTYPE, "link": "/tmp"}],
        }
        for label, entries in cases.items():
            with self.subTest(label):
                archive = self.make(entries, f"{label.replace(' ', '-')}.tar.gz")
                folder = Path(self.tmp.name) / f"out-{label}"
                with mock.patch.object(install_ctl, "MAX_UNPACKED", 1000):
                    with self.assertRaises(RuntimeError):
                        install_ctl.extract(archive, folder)
                self.assertFalse(folder.exists())                 # nothing was written


class ReadHeadTest(unittest.TestCase):
    def test_at_most_the_limit_and_only_regular_files(self):
        import ai_core
        with tempfile.TemporaryDirectory() as tmp:
            big = Path(tmp) / "big"
            big.write_bytes(b"a" * 5000)
            self.assertEqual(ai_core.read_head(big, 100), b"a" * 100)
            fifo = Path(tmp) / "fifo"
            os.mkfifo(fifo)
            with self.assertRaises(OSError):              # no writer: a blocking open would hang here
                ai_core.read_head(fifo, 100)
            link = Path(tmp) / "link"
            link.symlink_to(big)
            with self.assertRaises(OSError):
                ai_core.read_head(link, 100, follow=False)
            self.assertIsNone(agent_details._read(fifo))  # the settings reader: not hung, nothing read


class NotificationTextTest(unittest.TestCase):
    def test_markup_and_controls_are_taken_out(self):
        import ai_core
        text = ai_core.plain_text('5h <img src="https://x.example/p.png"> &amp; \x1b[2J used\r\nnow', 200)
        for bad in "<>&\x1b\r\n":
            self.assertNotIn(bad, text)
        self.assertTrue(text.startswith("5h img src="))
        self.assertEqual(len(ai_core.plain_text("x" * 500, 80)), 80)
        self.assertIn("ai_core.plain_text(body", Path(popup().__file__).read_text())


class FollowerTest(unittest.TestCase):
    def test_rotation_truncation_and_what_is_not_a_regular_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.log"
            path.write_bytes(b"one\ntwo\nhalf")
            reader = session_follow.Follower(path)
            self.assertEqual(reader.new_lines(), [b"one", b"two"])
            path.write_bytes(b"x\n")                                   # cut short
            self.assertEqual(reader.new_lines(), [b"x"])                # no "half" glued onto it
            path.unlink()
            path.write_bytes(b"new\n")                                 # replaced (rotated)
            self.assertEqual(reader.new_lines(), [b"new"])
            path.unlink()
            os.mkfifo(path)                                             # a FIFO in its place
            self.assertEqual(reader.new_lines(), [])                    # not hung, nothing read
            path.unlink()
            path.symlink_to(Path(tmp) / "elsewhere")
            (Path(tmp) / "elsewhere").write_bytes(b"secret\n")
            self.assertEqual(reader.new_lines(), [])                    # a link is not followed


class DiskBudgetTest(unittest.TestCase):
    """An AppImage that unpacks itself is stopped as soon as it writes more than its budget."""

    def test_an_endless_writer_is_stopped_and_a_small_one_finishes(self):
        import ai_core
        endless = [sys.executable, "-c", "import os\nf=open('blob','wb')\nwhile True:\n f.write(os.urandom(1<<20)); f.flush()"]
        small = [sys.executable, "-c", "open('ok','wb').write(b'x'*1000)"]
        with tempfile.TemporaryDirectory() as tmp:
            start = time.monotonic()
            self.assertFalse(ai_core.run_with_disk_budget(endless, tmp, 20 << 20, timeout=30, every=0.1))
            self.assertLess(time.monotonic() - start, 10)
            self.assertLess(ai_core.folder_bytes(tmp), 400 << 20)       # stopped long before filling the disk
        with tempfile.TemporaryDirectory() as tmp:
            self.assertTrue(ai_core.run_with_disk_budget(small, tmp, 20 << 20, timeout=30))

    def test_children_left_behind_are_ended_too(self):
        import ai_core
        parent = [sys.executable, "-c", "import subprocess,sys\n"
                  "subprocess.Popen([sys.executable, '-c', 'import time,os\\nwhile True:\\n open(\\'w\\',\\'ab\\').write(b\\'x\\'); time.sleep(0.05)'])"]
        with tempfile.TemporaryDirectory() as tmp:
            ai_core.run_with_disk_budget(parent, tmp, 20 << 20, timeout=30)
            time.sleep(0.3)
            size = ai_core.folder_bytes(tmp)
            time.sleep(0.5)
            self.assertEqual(ai_core.folder_bytes(tmp), size)          # nothing writes any more

    def test_a_failed_appimage_leaves_nothing_behind(self):
        import install_ctl
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "partial"
            folder.mkdir()
            (folder / "half").write_bytes(b"x")
            image = Path(tmp) / "app.AppImage"
            image.write_bytes(b"#!/bin/sh\nexit 1\n")
            for outcome in ({"return_value": False}, {"side_effect": OSError("disk full")}):
                folder.mkdir(exist_ok=True)
                (folder / "half").write_bytes(b"x")
                with mock.patch.object(install_ctl.ai_core, "run_with_disk_budget", **outcome):
                    with self.assertRaises((RuntimeError, OSError)):
                        install_ctl.unpack_appimage(image, folder)
                self.assertFalse(folder.exists())
            folder.mkdir()
            good = Path(tmp) / "ok.tar.gz"
            import tarfile
            with tarfile.open(good, "w:gz") as tar:
                info = tarfile.TarInfo("a")
                info.size = 1
                tar.addfile(info, io.BytesIO(b"x"))
            with mock.patch.object(install_ctl.ai_core, "read_bounded_output", side_effect=OSError("boom")):
                with self.assertRaises(OSError):
                    install_ctl.extract(good, folder)
            self.assertFalse(folder.exists())

    def test_unknown_identity_is_not_followed(self):
        out = io.StringIO()
        with mock.patch.object(session_follow, "_identity", return_value=0.0), \
                mock.patch.object(session_follow, "session_file") as find:
            self.assertEqual(session_follow.follow("pi-files", 4242, "Pi\x1b[2J", out=out), 1)
        find.assert_not_called()
        self.assertNotIn("\x1b", out.getvalue())


class ModelAskProgressTest(unittest.TestCase):
    """While the agent looks for models: a spinner and the time it has taken so far."""

    def test_the_time_counts_up_and_stops_with_the_answer(self):
        popup()
        import models_page
        from gi.repository import Gtk

        class View:
            model_ask_progress = models_page.ModelsPage.model_ask_progress
            ask_progress = models_page.ModelsPage.ask_progress

        view, ticks = View(), []
        state = {"asking": True, "asked_at": time.monotonic() - 75}
        view.models_state = state
        with mock.patch.object(models_page.GLib, "timeout_add_seconds", lambda _s, fn: ticks.append(fn)):
            row = view.model_ask_progress(state, "Codex")
        label = row.get_last_child()
        self.assertEqual(label.get_label(), "Codex is looking on the web for models · 1:15")
        self.assertIsNotNone(row.get_first_child())                       # the spinner
        state["asking"] = False                                           # the answer came
        self.assertFalse(ticks[0]())                                      # the timer ends


class ModelAdviceCountTest(unittest.TestCase):
    """Ask an agent: 10 suggestions by default, another number from the Suggestions control."""

    def test_the_count_reaches_the_prompt_and_the_answer(self):
        import model_advice
        import settings_schema
        self.assertEqual(settings_schema.value({}, "model_advice_count"), 10)
        self.assertEqual(settings_schema.value({"model_advice_count": 7}, "model_advice_count"), 10)   # not offered
        self.assertIn("At most 15 models", model_advice.prompt("q", "Ollama", "", 0, 0, most=15))
        self.assertIn("At most 10 models", model_advice.prompt("q", "Ollama", "", 0, 0, most=99))
        answer = {"summary": "s", "models": [{"name": f"Model {i}", "search": f"model {i} GGUF", "size": "", "why": ""}
                                            for i in range(30)]}
        self.assertEqual(len(model_advice.validate(answer, "codex", "q").models), 10)
        self.assertEqual(len(model_advice.validate(answer, "codex", "q", most=20).models), 20)
        self.assertEqual(len(model_advice.validate(answer, "codex", "q", most=5).models), 5)


class SuggestionsInResultListTest(unittest.TestCase):
    """The agent's suggestions go into the search result list, found as Hugging Face repos."""

    def test_found_ones_are_listed_missing_ones_named(self):
        import models_page
        import model_advice
        rows, labels = [], {}

        class Listbox:
            def get_first_child(self):
                return None

            def append(self, row):
                rows.append(row)

        class Label:
            def __init__(self, key):
                self.key = key

            def set_label(self, text):
                labels[self.key] = text

        class View:
            show_model_suggestions = models_page.ModelsPage.show_model_suggestions
            SUGGESTED = models_page.ModelsPage.SUGGESTED
            models_repos = Listbox()
            models_results_heading, models_status = Label("heading"), Label("status")

            def repo_row(self, repo, subtitle=None, tip=""):
                return (repo, subtitle, tip)

            def update_model_buttons(self):
                pass

            def set_models_note(self, text):
                labels["note"] = text

        repo = mock.Mock(id="unsloth/Qwen3-GGUF")
        item = model_advice.Suggestion("Qwen3 8B", "Qwen3-8B GGUF", "Q4_K_M, 5 GB", "fits 24 GB")
        state = {"advice": model_advice.Advice("codex", "q", "s", [item]),
                 "advice_found": [(repo, item)] * 12, "advice_missing": ["Nope 3B"]}
        view = View()
        view.models_state = state
        with mock.patch.object(models_page.ai_core, "setting", return_value=10):
            view.show_model_suggestions(state)
        self.assertEqual(len(rows), 10)                                   # as many as Suggestions says
        self.assertEqual(rows[0], (repo, "Q4_K_M, 5 GB · fits 24 GB", "Qwen3 8B: fits 24 GB"))
        self.assertEqual(labels["heading"], "SUGGESTED BY CODEX")
        self.assertIn("Nope 3B", labels["note"])                         # under the heading, not in a footer
        self.assertEqual(state["query"], View.SUGGESTED)                  # a late search does not replace them


class OllamaNameTest(unittest.TestCase):
    """Hugging Face repos keep their capitals when Ollama pulls them (hf.co/<user>/<repo>:<quant>)."""

    def test_real_repo_names_pass_and_broken_ones_do_not(self):
        import models_ctl
        ok = ["hf.co/unsloth/Qwen3-8B-GGUF:Q4_K_M", "hf.co/bartowski/google_gemma-3-12b-it-GGUF:Q4_K_M",
              "hf.co/unsloth/SmolLM2-135M-Instruct-GGUF", "qwen3:8b", "llama3.2"]
        bad = ["Qwen3:8b", "hf.co/u/r;rm -rf", "hf.co/u/r:Q4 K", "hf.co/u/$(x)", "hf.co/-u/r", "", "hf.co/u"]
        for name in ok:
            self.assertTrue(models_ctl.OLLAMA_NAME_RE.fullmatch(name), name)
        for name in bad:
            self.assertFalse(models_ctl.OLLAMA_NAME_RE.fullmatch(name), name)
        with self.assertRaises(ValueError):
            models_ctl.ollama_pull("hf.co/../etc", "127.0.0.1:11434", lambda *a: None, lambda: False)


class OllamaPullableTest(unittest.TestCase):
    """Ollama pulls a Hugging Face GGUF by its tag only: split, sub-folder and ambiguous files are not offered."""

    def test_which_files_ollama_can_pull_and_which_is_recommended(self):
        import models_ctl
        G = models_ctl.GgufFile
        q4 = G("Model-Q4_K_M", ["Model-Q4_K_M.gguf"], 10, "Q4_K_M", fits=True)
        split = G("Model-BF16", ["BF16/Model-BF16-00001-of-00002.gguf", "BF16/Model-BF16-00002-of-00002.gguf"], 30, "BF16")
        folder = G("dspark-Model-BF16", ["dspark/dspark-Model-BF16.gguf"], 8, "BF16", fits=True)
        twin_a = G("Model-Q8_0", ["Model-Q8_0.gguf"], 15, "Q8_0", fits=True)
        twin_b = G("Model-Q8_0-v2", ["Model-Q8_0-v2.gguf"], 16, "Q8_0", fits=True)
        entries = [q4, split, folder, twin_a, twin_b]
        problem = {e.name: models_ctl.ollama_pull_problem(e, entries) for e in entries}
        self.assertEqual(problem, {"Model-Q4_K_M": "", "Model-BF16": "split", "dspark-Model-BF16": "folder",
                                   "Model-Q8_0": "tag", "Model-Q8_0-v2": "tag"})
        models_ctl.recommend(entries, lambda e: not problem[e.name])
        self.assertEqual([e.name for e in entries if e.recommended], ["Model-Q4_K_M"])
        models_ctl.recommend([folder], lambda e: not problem[e.name])
        self.assertFalse(folder.recommended)                       # nothing pullable: nothing recommended


class OllamaRegistryCheckTest(unittest.TestCase):
    """Hugging Face's registry is asked, as Ollama asks it, before a file is offered for Ollama."""

    def test_the_answer_is_read_from_status_and_body(self):
        import models_ctl
        ok = b"HTTP/2 200\r\ncontent-type: application/json\r\n\r\n{\"schemaVersion\":2}"
        refused = (b"HTTP/2 307\r\nlocation: /x\r\n\r\nHTTP/2 400\r\ncontent-type: application/json\r\n\r\n"
                   b'{"error":"This repository only contains sharded GGUF files."}')
        with mock.patch("netfetch.fetch", return_value=ok):
            self.assertEqual(models_ctl.ollama_tag_problem("unsloth/Qwen3-8B-GGUF", "Q4_K_M"), "")
        with mock.patch("netfetch.fetch", return_value=refused):
            self.assertEqual(models_ctl.ollama_tag_problem("unsloth/X-GGUF", "Q8_0"),
                             "This repository only contains sharded GGUF files.")
        self.assertEqual(models_ctl.ollama_tag_problem("bad repo", "Q8_0"), "not a model name")

    def test_the_page_offers_only_what_the_registry_accepts(self):
        import models_page
        import models_ctl
        G = models_ctl.GgufFile

        class View:
            pull_problems = models_page.ModelsPage.pull_problems
            PULL_CHECKS = models_page.ModelsPage.PULL_CHECKS

        rt = mock.Mock()
        files = [G("A-Q8_0", ["A-Q8_0.gguf"], 8, "Q8_0"), G("A-Q4_K_M", ["A-Q4_K_M.gguf"], 4, "Q4_K_M"),
                 G("A-BF16", ["BF16/A-BF16-00001-of-00002.gguf", "BF16/A-BF16-00002-of-00002.gguf"], 16, "BF16")]
        asked = []
        def registry(repo, quant, base):
            asked.append(quant)
            return "sharded only" if quant == "Q8_0" else ""
        with mock.patch.object(models_page.llm_engine, "of", return_value=mock.Mock(download="pull")), \
                mock.patch.object(models_page.models_ctl, "ollama_tag_problem", side_effect=registry):
            problems = View().pull_problems(rt, "u/A-GGUF", files, "https://huggingface.co")
        self.assertEqual(problems, {"A-Q8_0": "registry:sharded only", "A-BF16": "split"})
        self.assertEqual(sorted(asked), ["Q4_K_M", "Q8_0"])             # the split one is not even asked
        with mock.patch.object(models_page.llm_engine, "of", return_value=mock.Mock(download="file")):
            self.assertEqual(View().pull_problems(rt, "u/A-GGUF", files, "https://huggingface.co"), {})


class McpAdviceTest(unittest.TestCase):
    """Ask an agent on the Add MCP server page: search words from the agent, candidates from the
    downloaded registry list, then only names from those candidates."""

    def registry(self, tmp):
        import mcp_registry
        def item(name, title, description):
            return {"server": {"name": name, "title": title, "description": description,
                               "remotes": [{"type": "streamable-http", "url": f"https://{name.split('/')[-1]}.example/mcp"}]},
                    "_meta": {"io.modelcontextprotocol.registry/official": {"status": "active"}}}
        data = {"fetched": "2026-10-23T09:00:00Z", "checked": 0, "servers": {
            "com.notion/mcp": item("com.notion/mcp", "Notion", "Read and write Notion pages"),
            "io.github.x/notion-copy": item("io.github.x/notion-copy", "Notion copy", "Unofficial Notion pages"),
            "io.github.y/postgres": item("io.github.y/postgres", "Postgres", "Query a Postgres database"),
        }}
        path = Path(tmp) / "list.json"
        path.write_text(json.dumps(data))
        mcp_registry._list_cache = None
        return path

    def test_two_steps_and_only_candidates_are_kept(self):
        import mcp_advice
        with tempfile.TemporaryDirectory() as tmp:
            path = self.registry(tmp)
            prompts, phases = [], []
            def look_up(agent, task, prompt, schema, result_of=None):
                prompts.append(prompt)
                if "terms" in schema["required"]:
                    return '{"terms": ["notion", "pages", "bad;term", "notion"]}'
                return ('{"summary": "The official Notion server.", "servers": ['
                        '{"name": "com.notion/mcp", "why": "official"}, {"name": "invented/server", "why": "x"}]}')
            advice = mcp_advice.ask("codex", "my Notion pages", look_up=look_up, path=path, phase=phases.append)
        self.assertEqual(advice.terms, ["notion", "pages"])                    # plain words, no repeats
        self.assertEqual([(s.name, why) for s, why in advice.servers], [("com.notion/mcp", "official")])
        self.assertEqual(phases, ["terms", "picking"])
        self.assertIn("com.notion/mcp | Notion |", prompts[1])                 # the candidates, as lines
        self.assertNotIn("postgres", prompts[1])                               # only what the words found
        self.assertIn("Do not search the web", prompts[0])

    def test_no_list_downloaded_and_no_match(self):
        import mcp_advice
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(RuntimeError):
                mcp_advice.ask("codex", "x", look_up=lambda *a, **k: '{"terms": ["x"]}', path=Path(tmp) / "none.json")
            path = self.registry(tmp)
            advice = mcp_advice.ask("codex", "x", look_up=lambda *a, **k: '{"terms": ["kubernetes"]}', path=path)
        self.assertEqual((advice.servers, advice.terms), ([], ["kubernetes"]))


class OllamaUncheckedTest(unittest.TestCase):
    def test_sizes_beyond_the_checked_ones_are_not_offered(self):
        import models_page
        import models_ctl

        class View:
            pull_problems = models_page.ModelsPage.pull_problems
            PULL_CHECKS = 3

        files = [models_ctl.GgufFile(f"M-Q{i}", [f"M-Q{i}.gguf"], i, f"Q{i}") for i in range(5)]
        with mock.patch.object(models_page.llm_engine, "of", return_value=mock.Mock(download="pull")), \
                mock.patch.object(models_page.models_ctl, "ollama_tag_problem", return_value=""):
            problems = View().pull_problems(mock.Mock(), "u/M-GGUF", files, "https://huggingface.co")
        self.assertEqual(problems, {"M-Q3": "unchecked", "M-Q4": "unchecked"})


if __name__ == "__main__":
    unittest.main()
