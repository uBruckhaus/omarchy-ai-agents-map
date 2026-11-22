"""Activity log, history import, live activity and the report library (Maprisma tasks 074-076), headless."""
import datetime as dt
import fcntl
import json
import os
import sqlite3
import stat
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
import activity_log
import agent_activity_report as live
import history_import
import reports_ctl

UTC = dt.timezone.utc


def setUpModule():
    global _tz
    _tz = os.environ.get("TZ")
    os.environ["TZ"] = "UTC"
    time.tzset()


def tearDownModule():
    if _tz is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = _tz
    time.tzset()


def at(text: str) -> dt.datetime:
    return dt.datetime.fromisoformat(text).replace(tzinfo=UTC)


class Settings(dict):
    def __call__(self, key):
        return self.get(key)


def settings(**values):
    base = {"ai_activity_log": True, "ai_activity_log_days": 7, "ai_activity_import": True,
            "ai_activity_import_topics": True}
    base.update(values)
    return Settings(base)


class TempCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.settings = settings()
        self.log = activity_log.ActivityLog(self.tmp / "data/ai-log", setting=self.settings)
        # These tests look at a span from its start to its end only; the real poll looks every 2 s
        # (the gap rule has its own test, test_unobserved_time_is_never_logged).
        self.log.unobserved = dt.timedelta(days=30)

    def rows(self, day: str) -> list:
        path = self.log.directory / f"{day}.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


# --- topics ----------------------------------------------------------------------------------------

class TopicTest(unittest.TestCase):
    def test_secrets_are_redacted(self):
        cases = {
            "use sk-abcdefghijklmnopqrstuv now": "sk-",
            "token ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345": "ghp_",
            "github_pat_11ABCDEFG0123456789_abcdefghijkl": "github_pat_",
            "jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl here": "eyJ",
            "Authorization: Bearer abc.def.ghi": "abc.def",
            "set api_key=hunter2hunter2": "hunter2",
            "password: 'p4ss w0rd'": "p4ss",
            "hash 0123456789abcdef0123456789abcdef0123": "0123456789abcdef",
            "mail me at someone@example.com please": "someone@",
            "-----BEGIN PRIVATE KEY-----MIIB-----END PRIVATE KEY----- rest": "MIIB",
            "AKIAIOSFODNN7EXAMPLE key": "AKIAIOSFODNN7",
            "hf_abcdefghijklmnop": "hf_",
        }
        for text, secret in cases.items():
            with self.subTest(text=text):
                clean = activity_log.sanitize_topic(text)
                self.assertNotIn(secret, clean or "")

    def test_urls_paths_and_bounds(self):
        self.assertNotIn("user:pw", activity_log.sanitize_topic("see https://user:pw@host.example/x") or "")
        clean = activity_log.sanitize_topic("open https://example.com/a?token=SECRET123&page=2 now")
        self.assertIn("page=2", clean)
        self.assertNotIn("SECRET123", clean)
        self.assertEqual(activity_log.sanitize_topic("fix /home/uwe/project/src/main.py now"), "fix main.py now")
        self.assertEqual(activity_log.sanitize_topic("look at ~/notes/todo.md"), "look at todo.md")
        self.assertEqual(activity_log.sanitize_topic("a\u202eb\x00c\n d"), "a b c d")
        self.assertEqual(len(activity_log.sanitize_topic("word " * 100)), 120)
        long = "a" * 110 + " sk-abcdefghijklmnopqrstuvwxyz"     # a secret across character 120
        self.assertNotIn("sk-", activity_log.sanitize_topic(long))
        self.assertEqual(activity_log.sanitize_topic("Größe ändern 日本語"), "Größe ändern 日本語")
        self.assertIsNone(activity_log.sanitize_topic("   "))
        self.assertIsNone(activity_log.sanitize_topic(3))
        self.assertIn("Asian", activity_log.sanitize_topic("Asian food"))

    def test_session_and_label(self):
        self.assertEqual(activity_log.history_session("abc-1.2_3"), "abc-1.2_3")
        self.assertEqual(len(activity_log.history_session("a/b c")), 64)
        self.assertIsNone(activity_log.label("my-secret.txt"))
        self.assertIsNone(activity_log.label("../x"))
        self.assertEqual(activity_log.label("main.py"), "main.py")


# --- the log ---------------------------------------------------------------------------------------

class LogTest(TempCase):
    def test_off_writes_nothing(self):
        self.settings["ai_activity_log"] = False
        self.log.observe({"agent:claude"}, now=at("2026-11-20T10:00:00"))
        self.log.observe(set(), now=at("2026-11-20T10:05:00"))
        self.assertFalse(self.log.directory.exists())

    def test_span_written_privately(self):
        self.log.observe({"agent:claude", "bogus"}, now=at("2026-11-20T10:00:00"))
        self.log.observe(set(), now=at("2026-11-20T10:05:00"))
        rows = self.rows("2026-11-20")
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["source"], rows[0]["duration_ms"], rows[0]["origin"]), ("claude", 300000, "live"))
        self.assertEqual(stat.S_IMODE(os.stat(self.log.directory).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(self.log.directory / "2026-11-20.jsonl").st_mode), 0o600)

    def test_project_files_and_tokens(self):
        usage = iter([{"session": "s", "model": "opus", "input": 10, "output": 5},
                      {"session": "s", "model": "opus", "input": 40, "output": 9}])
        self.log.timeline = lambda roots, since: [{"kind": "testing", "files": [
            {"path": "/p/app/test_x.py", "at": "2026-11-20T10:01:00Z"}]}]
        self.log.observe({"agent:pi"}, {"agent:pi": "/p/app"}, lambda s: next(usage), now=at("2026-11-20T10:00:00"))
        self.log.observe(set(), usage_for=lambda s: next(usage), now=at("2026-11-20T10:02:00"))
        row = self.rows("2026-11-20")[0]
        self.assertEqual((row["project"], row["topic"], row["files"]), ("app", "testing", ["test_x.py"]))
        self.assertEqual((row["tokens_in"], row["tokens_out"], row["model"]), (30, 4, "opus"))

    def test_lock_taken_keeps_span_for_next_tick(self):
        self.log.observe({"agent:claude"}, now=at("2026-11-20T10:00:00"))
        fd = activity_log.private_dir(self.log.directory)
        lock = activity_log.open_lock(fd, ".lock")
        fcntl.flock(lock, fcntl.LOCK_EX)
        self.log.observe(set(), now=at("2026-11-20T10:05:00"))
        self.assertEqual(self.rows("2026-11-20"), [])
        os.close(lock)
        os.close(fd)
        self.log.observe(set(), now=at("2026-11-20T10:06:00"))
        self.assertEqual(len(self.rows("2026-11-20")), 1)

    def test_switching_off_drops_buffer(self):
        self.log.observe({"agent:claude"}, now=at("2026-11-20T10:00:00"))
        self.settings["ai_activity_log"] = False
        self.log.observe(set(), now=at("2026-11-20T10:05:00"))
        self.settings["ai_activity_log"] = True
        self.log.observe(set(), now=at("2026-11-20T10:06:00"))
        self.assertEqual(self.rows("2026-11-20"), [])

    def test_pruning_and_report_totals(self):
        self.settings["ai_activity_log_days"] = 2
        self.log.directory.mkdir(parents=True)
        (self.log.directory / "2026-11-01.jsonl").write_text("{}\n")
        for start, end in (("2026-11-19T09:00:00", "2026-11-19T09:10:00"), ("2026-11-20T10:00:00", "2026-11-20T10:01:00")):
            self.log.observe({"agent:codex"}, now=at(start))
            self.log.observe(set(), now=at(end))
        self.assertFalse((self.log.directory / "2026-11-01.jsonl").exists())
        report = self.log.report({}, now=at("2026-11-20T12:00:00"))
        self.assertEqual((report["from_day"], report["through_day"]), ("2026-11-19", "2026-11-20"))
        self.assertEqual(len(report["entries"]), 2)
        self.assertEqual(report["entries"][0]["started_at"][:10], "2026-11-20")      # newest first
        self.assertEqual(report["by_source"][0]["count"], 2)
        self.assertEqual(report["by_source"][0]["duration_ms"], 660000)
        self.assertIsNone(report["by_source"][0]["tokens_in"])
        self.assertEqual(report["by_week"][0]["key"], "2026-11-16")
        with self.assertRaises(ValueError):
            self.log.report({"from_day": "2026-11-21", "through_day": "2026-11-20"})
        with self.assertRaises(ValueError):
            self.log.report({"from_day": "2026-13-01"})
        self.assertIn("By agent or runtime", activity_log.log_text(report))
        self.assertIn('"entries"', activity_log.format_activity_log(report, True))

    def test_rows_are_cleaned_on_read(self):
        self.log.directory.mkdir(parents=True)
        good = {"version": 1, "source_kind": "agent", "source": "claude", "started_at": "2026-11-20T10:00:00Z",
                "ended_at": "2026-11-20T10:01:00Z", "duration_ms": 999, "topic": "key=abcdefgh123", "project": "a/b",
                "files": ["ok.py", "secret.txt", 7], "tokens_in": -1}
        bad = [dict(good, source="UPPER"), dict(good, ended_at="2026-11-20T09:00:00Z"), dict(good, version=2),
               dict(good, origin="history")]          # history without a session
        (self.log.directory / "2026-11-20.jsonl").write_text(
            "\n".join(json.dumps(r) for r in [good, *bad]) + "\nnot json\n")
        entries = self.log.report({}, now=at("2026-11-20T12:00:00"))["entries"]
        self.assertEqual(len(entries), 1)
        row = entries[0]
        self.assertEqual((row["duration_ms"], row["project"], row["files"], row["tokens_in"]), (60000, None, ["ok.py"], None))
        self.assertNotIn("abcdefgh123", row["topic"])

    def test_text_shows_free_topics(self):
        row = {"source_kind": "agent", "source": "claude", "project": "app", "topic": "Fix the login",
               "started_at": "2026-11-20T10:00:00Z", "duration_ms": 61000}
        self.assertIn("Fix the login", activity_log.row_text(row))
        self.assertIn("Editing", activity_log.row_text(dict(row, topic="editing")))
        self.assertIn("0:01:01", activity_log.row_text(row))


class MergeTest(TempCase):
    def history_row(self, start, end, **extra):
        row = {"version": 1, "id": "", "origin": "history", "session": "s1", "source_kind": "agent", "source": "claude",
               "started_at": start, "ended_at": end, "duration_ms": 0, "files": [], "tokens_in": 5, "tokens_out": 1}
        row.update(extra)
        return row

    def test_add_unchanged_update_and_supersede(self):
        self.log.observe({"agent:claude"}, now=at("2026-11-20T10:05:00"))
        self.log.observe(set(), now=at("2026-11-20T10:15:00"))
        row = self.history_row("2026-11-20T10:00:00Z", "2026-11-20T10:20:00Z")
        now = at("2026-11-20T12:00:00")
        self.assertEqual(self.log.merge_history(row, now=now)[0], "added")
        self.assertEqual(self.log.merge_history(row, now=now)[0], "unchanged")
        self.assertEqual(self.log.merge_history(dict(row, tokens_in=50), now=now)[0], "updated")
        stored = self.rows("2026-11-20")
        live_row = next(r for r in stored if r["origin"] == "live")
        self.assertTrue(live_row["superseded_by"].startswith("history:agent:claude:s1:"))
        entries = self.log.report({}, now=now)["entries"]
        self.assertEqual([e["origin"] for e in entries], ["history"])
        self.assertEqual(entries[0]["tokens_in"], 50)
        # a later live write keeps the history row
        self.log.observe({"agent:codex"}, now=at("2026-11-20T11:00:00"))
        self.log.observe(set(), now=at("2026-11-20T11:01:00"))
        self.assertEqual(len([r for r in self.rows("2026-11-20") if r["origin"] == "history"]), 1)

    def test_short_burst_and_zero_length(self):
        hist = {"origin": "history", "source": "claude", "source_kind": "agent", "project": None,
                "started_at": "2026-11-20T10:00:00Z", "ended_at": "2026-11-20T10:10:00Z"}
        live_row = dict(hist, origin="live", started_at="2026-11-20T10:09:00Z", ended_at="2026-11-20T10:20:00Z")
        self.assertFalse(activity_log.overlaps(live_row, hist))
        self.assertTrue(activity_log.overlaps(dict(live_row, ended_at="2026-11-20T10:10:30Z"), hist))
        zero = dict(live_row, started_at="2026-11-20T10:05:00Z", ended_at="2026-11-20T10:05:00Z")
        self.assertTrue(activity_log.overlaps(zero, hist))
        self.assertFalse(activity_log.overlaps(dict(zero, project="a"), dict(hist, project="b")))

    def test_midnight_supersedes_end_day(self):
        self.log.observe({"agent:claude"}, now=at("2026-11-21T00:05:00"))
        self.log.observe(set(), now=at("2026-11-21T00:10:00"))
        row = self.history_row("2026-11-20T23:50:00Z", "2026-11-21T00:20:00Z")
        self.assertEqual(self.log.merge_history(row, now=at("2026-11-21T08:00:00"))[0], "added")
        self.assertIsNotNone(self.rows("2026-11-21")[0]["superseded_by"])

    def test_log_off_during_merge(self):
        self.settings["ai_activity_log"] = False
        with self.assertRaises(activity_log.ActivityError) as caught:
            self.log.merge_history(self.history_row("2026-11-20T10:00:00Z", "2026-11-20T10:20:00Z"))
        self.assertEqual(caught.exception.code, "log_off")

    def test_day_cap(self):
        with mock.patch.object(activity_log, "MAX_FILE", 1200):
            results = [self.log.merge_history(self.history_row(f"2026-11-20T1{i}:00:00Z", f"2026-11-20T1{i}:10:00Z",
                                                               session=f"s{i}"), now=at("2026-11-20T23:00:00"))[0]
                       for i in range(6)]
        self.assertIn("skipped", results)
        self.assertLessEqual((self.log.directory / "2026-11-20.jsonl").stat().st_size, 1200)


# --- history import ----------------------------------------------------------------------------------

def jl(*rows) -> str:
    return "".join(json.dumps(r) + "\n" for r in rows)


class ImportTest(TempCase):
    NOW = at("2026-11-20T18:00:00")

    def claude_source(self):
        root = self.tmp / "claude/projects"
        project = root / "-home-u-app"
        project.mkdir(parents=True)
        (project / "subagents").mkdir()
        (project / "subagents/sub.jsonl").write_text(jl({"type": "user", "timestamp": "2026-11-20T10:00:00Z"}))
        (project / "agent-x.jsonl").write_text(jl({"type": "user", "timestamp": "2026-11-20T10:00:00Z"}))

        def msg(role, when, **extra):
            message = {"role": role, "content": extra.pop("content", "hi")}
            message.update(extra)
            return {"type": role, "timestamp": when, "cwd": "/home/u/app", "message": message}
        (project / "sess-1.jsonl").write_text(jl(
            msg("user", "2026-11-20T10:00:00Z", content="Fix login, my key is sk-abcdefghijklmnopqrstu"),
            msg("assistant", "2026-11-20T10:05:00Z", id="m1", model="claude-opus", usage={"input_tokens": 100, "output_tokens": 20},
                content=[{"type": "tool_use", "name": "Edit", "input": {"file_path": "/home/u/app/tests/test_login.py"}}]),
            msg("assistant", "2026-11-20T10:06:00Z", id="m1", usage={"input_tokens": 120, "output_tokens": 30}),
            msg("user", "2026-11-20T11:00:00Z", content="second span"),          # after a 30-min gap
            msg("assistant", "2026-11-20T11:10:00Z", id="m2", usage={"input_tokens": 7, "output_tokens": 3}),
            msg("user", "2026-11-20T17:59:00Z", content="still running"),        # under 2 minutes old
        ))
        return {"source": "agent:claude", "name": "Claude Code", "supported": True, "form": "claude-files",
                "root": str(root)}

    def codex_source(self):
        root = self.tmp / "codex/sessions"
        day = root / "2026/11/20"
        day.mkdir(parents=True)

        def ev(when, kind, payload):
            return {"timestamp": when, "type": kind, "payload": payload}
        (day / "rollout-a.jsonl").write_text(jl(
            ev("2026-11-20T09:00:00Z", "session_meta", {"id": "ca", "cwd": "/w/api"}),
            ev("2026-11-20T09:00:01Z", "turn_context", {"model": "gpt-5"}),
            ev("2026-11-20T09:00:02Z", "event_msg", {"type": "user_message", "message": "add endpoint"}),
            ev("2026-11-20T09:01:00Z", "event_msg", {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 100, "output_tokens": 10}}}),
            ev("2026-11-20T09:02:00Z", "response_item", {"type": "custom_tool_call", "name": "apply_patch",
                                                          "input": "*** Begin Patch\n*** Update File: src/api.rs\n"}),
            ev("2026-11-20T09:03:00Z", "event_msg", {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 150, "output_tokens": 30}}}),
        ))
        (day / "rollout-sub.jsonl").write_text(jl(
            ev("2026-11-20T09:00:00Z", "session_meta", {"id": "cs", "cwd": "/w/api", "source": {"subagent": {}}}),
            ev("2026-11-20T09:00:02Z", "event_msg", {"type": "user_message", "message": "sub"})))
        return {"source": "agent:codex", "name": "Codex", "supported": True, "form": "codex-jsonl", "root": str(root)}

    def importer(self, *sources):
        return history_import.HistoryImporter(self.log, list(sources), now=self.NOW)

    def test_claude_spans_topics_tokens(self):
        result = self.importer(self.claude_source()).run()
        source = result["sources"][0]
        self.assertEqual((source["sessions"], source["added"], source["status"]), (1, 2, "ok"))
        rows = sorted(self.rows("2026-11-20"), key=lambda r: r["started_at"])
        self.assertEqual(len(rows), 2)
        first = rows[0]
        self.assertEqual((first["tokens_in"], first["tokens_out"]), (120, 30))       # same message id counted once
        self.assertEqual((first["project"], first["files"], first["model"]), ("app", ["test_login.py"], "claude-opus"))
        self.assertTrue(first["topic"].startswith("Fix login"))
        self.assertNotIn("sk-", first["topic"])
        self.assertEqual(rows[1]["duration_ms"], 600000)
        self.assertFalse(any(p.name == "claude-sessions.json" for p in self.tmp.rglob("*")))
        # a second run adds nothing; nothing outside ai-log
        again = self.importer({"source": "agent:claude", "name": "Claude Code", "supported": True,
                               "form": "claude-files", "root": str(self.tmp / "claude/projects")}).run()
        self.assertEqual((again["added"], again["updated"]), (0, 0))
        before = {p: p.stat().st_mtime_ns for p in self.tmp.rglob("*") if p.is_file()}
        self.importer({"source": "agent:claude", "name": "Claude Code", "supported": True,
                       "form": "claude-files", "root": str(self.tmp / "claude/projects")}).run()
        after = {p: p.stat().st_mtime_ns for p in self.tmp.rglob("*") if p.is_file()}
        changed = {p for p in after if before.get(p) != after[p]}
        self.assertTrue(changed)                                 # the import state at least
        self.assertTrue(all(self.log.directory in p.parents for p in changed), changed)

    def test_topics_off_gives_heuristic(self):
        self.settings["ai_activity_import_topics"] = False
        self.importer(self.claude_source()).run()
        topics = [r["topic"] for r in sorted(self.rows("2026-11-20"), key=lambda r: r["started_at"])]
        self.assertEqual(topics, ["testing", None])

    def test_codex_deltas_and_subagents(self):
        result = self.importer(self.codex_source()).run()
        source = result["sources"][0]
        self.assertEqual((source["sessions"], source["skipped"]), (1, 1))
        row = self.rows("2026-11-20")[0]
        self.assertEqual((row["tokens_in"], row["tokens_out"], row["model"]), (150, 30, "gpt-5"))
        self.assertEqual((row["project"], row["files"], row["session"]), ("api", ["api.rs"], "ca"))

    def test_big_file_head_and_tail(self):
        source = self.codex_source()
        path = Path(source["root"]) / "2026/11/20/rollout-a.jsonl"
        lines = path.read_text().splitlines()
        filler = [json.dumps({"timestamp": "2026-11-20T09:00:30Z", "type": "noise", "payload": {"x": "y" * 50}})] * 40
        path.write_text("\n".join(lines[:3] + filler + lines[3:]) + "\n")
        with mock.patch.object(history_import, "WHOLE", 1024), mock.patch.object(history_import, "HEAD", 600), \
                mock.patch.object(history_import, "TAIL", 1500):
            result = self.importer(source).run()
        self.assertEqual(result["sources"][0]["status"], "partial")
        self.assertEqual(result["sources"][0]["added"], 0)          # a cut-off leading span only updates

    def test_statuses(self):
        missing = {"source": "agent:pi", "name": "Pi", "supported": True, "form": "pi-files", "root": str(self.tmp / "none")}
        unsupported = {"source": "agent:goose", "name": "Goose", "supported": False, "form": "", "root": ""}
        broken_root = self.tmp / "agy"
        broken_root.mkdir()
        (broken_root / "x.db").write_bytes(b"not a database at all" * 10)
        broken = {"source": "agent:agy", "name": "Antigravity", "supported": True, "form": "trajectory-sqlite",
                  "root": str(broken_root)}
        result = self.importer(missing, unsupported, broken).run()
        self.assertEqual([s["status"] for s in result["sources"]], ["unavailable", "unsupported", "corrupt"])

    def test_trajectory_database(self):
        root = self.tmp / "agy"
        root.mkdir()
        db = root / "ead64663-d6a3-47c5-aa00-146aab4cd680.db"
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE steps (idx INTEGER, step_type INTEGER, status INTEGER, step_format INTEGER, step_payload BLOB)")

        def varint(n):
            out = b""
            while True:
                low, n = n & 0x7f, n >> 7
                out += bytes([low | (0x80 if n else 0)])
                if not n:
                    return out

        def field(tag, data):
            return varint(tag << 3 | 2) + varint(len(data)) + data
        request = field(19, field(2, b"Refactor the parser"))
        write = field(5, field(4, field(2, b"write_file"))) + field(140, field(1, field(2, b"/w/parser/lib.rs")))
        con.executemany("INSERT INTO steps VALUES (?,?,?,?,?)", [(0, 14, 3, 0, request), (1, 132, 3, 0, write)])
        con.commit()
        con.close()
        os.utime(db, (self.NOW.timestamp() - 3600, self.NOW.timestamp() - 3600))
        source = {"source": "agent:agy", "name": "Antigravity", "supported": True, "form": "trajectory-sqlite", "root": str(root)}
        with mock.patch.object(history_import, "_birth", return_value=self.NOW.timestamp() - 7200):
            result = self.importer(source).run()
        self.assertEqual(result["sources"][0]["status"], "partial")
        row = self.rows("2026-11-20")[0]
        self.assertEqual((row["topic"], row["files"]), ("Refactor the parser", ["lib.rs"]))
        activity = live.trajectory_activity(str(db), "s")
        self.assertEqual(activity["request"]["text"], "Refactor the parser")
        self.assertEqual(activity["edited_files"], [{"at": None, "path": "/w/parser/lib.rs"}])

    def test_refusals_and_throttle(self):
        source = self.claude_source()
        self.settings["ai_activity_import"] = False
        with self.assertRaises(activity_log.ActivityError) as caught:
            self.importer(source).run()
        self.assertEqual(caught.exception.code, "import_off")
        self.settings["ai_activity_import"] = True
        with self.assertRaises(activity_log.ActivityError) as caught:
            self.importer(source).run(days=99)
        self.assertEqual(caught.exception.code, "invalid_request")
        fd = activity_log.private_dir(self.log.directory)
        lock = activity_log.open_lock(fd, ".import.lock")
        fcntl.flock(lock, fcntl.LOCK_EX)
        with self.assertRaises(activity_log.ActivityError) as caught:
            self.importer(source).run()
        self.assertEqual(caught.exception.code, "import_running")
        os.close(lock)
        os.close(fd)
        self.importer(source).run()
        state = self.log.directory / ".import-state.json"
        self.assertEqual(stat.S_IMODE(state.stat().st_mode), 0o600)
        self.assertEqual(self.importer(source).run(auto=True), {"skipped": "throttled"})
        self.assertIn("sources", self.importer(source).run(auto=False))
        later = history_import.HistoryImporter(self.log, [source], now=self.NOW + dt.timedelta(hours=2))
        self.assertIn("sources", later.run(auto=True))

    def test_log_off_during_import_writes_nothing(self):
        source = self.claude_source()
        calls = []

        def progress(_name):
            calls.append(1)
            self.settings["ai_activity_log"] = False
        result = self.importer(source).run(progress=progress)
        self.assertEqual(result["code"], "log_off")
        self.assertEqual(self.rows("2026-11-20"), [])

    def test_observe_and_import_together(self):
        source = self.claude_source()
        errors = []

        def observe():
            try:
                for i in range(20):
                    self.log.observe({"agent:pi"}, now=at("2026-11-20T15:00:00") + dt.timedelta(minutes=2 * i))
                    self.log.observe(set(), now=at("2026-11-20T15:01:00") + dt.timedelta(minutes=2 * i))
            except Exception as error:      # pragma: no cover - reported below
                errors.append(error)
        thread = threading.Thread(target=observe)
        thread.start()
        self.importer(source).run()
        thread.join()
        self.log.observe(set(), now=at("2026-11-20T17:00:00"))     # flush anything buffered
        self.assertEqual(errors, [])
        rows = self.rows("2026-11-20")
        self.assertEqual(len([r for r in rows if r["source"] == "pi"]), 20)
        self.assertEqual(len([r for r in rows if r["origin"] == "history"]), 2)

    def test_sources_and_help(self):
        import harnesses
        fake = []
        for command, sessions, files, full, name in (("claude", object(), "claude-files", "Claude Code", "Claude"),
                                                     ("goose", object(), "goose-db", "", "Goose"),
                                                     ("nothing", None, "", "", "Nothing")):
            h = mock.Mock(command=command, sessions=sessions, session_files=files, full_name=full)
            h.name = name
            fake.append(h)
        with mock.patch.object(harnesses, "all_harnesses", return_value=fake), \
                mock.patch.object(harnesses, "get", side_effect=lambda c: next(h for h in fake if h.command == c)):
            for h in fake:
                h.activity_reader = ""
                h.activity_dir = "~/.claude/projects" if h.command == "claude" else ""
                h.extra_dir_option = ""
            found = history_import.history_sources({"claude", "goose", "nothing"}, home=self.tmp)
        self.assertEqual([(s["source"], s["supported"]) for s in found], [("agent:claude", True), ("agent:goose", False)])
        text = reports_ctl.help_text(history_import.public_sources(found))
        for title, _body in reports_ctl.HELP_SECTIONS:
            self.assertIn(title, text)
        self.assertIn("Claude Code · Supported", text)
        self.assertIn("Goose · Unsupported", text)


# --- live activity ---------------------------------------------------------------------------------

class LiveTest(unittest.TestCase):
    def test_claude_turn(self):
        tail = jl({"type": "user", "timestamp": "2026-11-20T10:00:00Z", "message": {"role": "user", "content": "old"}},
                  {"type": "user", "timestamp": "2026-11-20T10:01:00Z", "message": {"role": "user", "content": "do\x1b[31m it"}},
                  {"type": "assistant", "timestamp": "2026-11-20T10:02:00Z", "message": {"role": "assistant", "content": [
                      {"type": "text", "text": "On it"}, {"type": "tool_use", "name": "Bash"}]}})
        result = live.jsonl_session_activity(tail, "s1")
        self.assertEqual(result["status"], "available")
        self.assertEqual(result["request"]["text"], "do [31m it")
        self.assertEqual([t["name"] for t in result["tool_calls"]], ["Bash"])
        self.assertEqual(live.jsonl_session_activity("", "s")["status"], "unavailable")

    def test_codex_rollout(self):
        tail = jl({"type": "response_item", "payload": {"type": "message", "role": "user", "content": [
                      {"type": "input_text", "text": "<environment_context>x"}, {"type": "input_text", "text": "add tests"}]}},
                  {"type": "response_item", "timestamp": "2026-11-20T10:00:00Z", "payload": {
                      "type": "custom_tool_call", "name": "apply_patch", "call_id": "c1",
                      "input": "*** Begin Patch\n*** Add File: tests/test_a.py\n"}},
                  {"type": "response_item", "payload": {"type": "custom_tool_call_output", "call_id": "c1",
                                                        "output": "Success. Updated the following files:\nA tests/test_a.py"}})
        result = live.jsonl_session_activity(tail, "r", rollout=True)
        self.assertEqual(result["request"]["text"], "add tests")
        self.assertEqual(result["edited_files"][0]["path"], "tests/test_a.py")
        self.assertEqual(result["tool_calls"][0]["status"], "completed")

    def test_timeline_and_phase(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        for name in ("src/a.py", "src/b.py", "tests/test_a.py", ".git/x", "node_modules/m.js", "build/o", ".hidden"):
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_text("x")
        base = time.time() - 3000
        os.utime(root / "src/a.py", (base, base))
        os.utime(root / "src/b.py", (base + 60, base + 60))
        os.utime(root / "tests/test_a.py", (base + 120, base + 120))
        groups = live.activity_timeline({str(root)}, base - 10)
        self.assertEqual([g["kind"] for g in groups], ["editing", "testing"])
        paths = [f["path"] for g in groups for f in g["files"]]
        self.assertFalse(any(".git" in p or "node_modules" in p or "build" in p for p in paths))
        self.assertEqual(live.activity_timeline({"/"}, base), [])
        self.assertEqual(live.activity_phase(False, None, groups, False), "testing")
        self.assertEqual(live.activity_phase(True, None, [], False), "planning")
        self.assertEqual(live.activity_phase(False, {"done": 2, "total": 2}, groups, True), "finished")
        self.assertEqual(live.activity_phase(False, None, [], False), "unknown")

    def test_stuck(self):
        idle = live.ActivityIdle()
        for elapsed in range(0, 610_001, 10_000):
            value = idle.observe("i", "same", [5, 6], True, elapsed_ms=elapsed)
        self.assertEqual(value, 610_000)
        self.assertIsNone(idle.observe("i", "changed", [5, 6], True, elapsed_ms=620_000))
        self.assertIsNone(idle.observe("j", "e", [None], True, elapsed_ms=0))

    def test_report_for_own_process(self):
        me = os.getpid()
        child = __import__("subprocess").Popen(["sleep", "5"])
        self.addCleanup(lambda: (child.kill(), child.wait()))
        result = live.agent_activity_report("claude", None, [me], windows={})
        self.assertEqual(result["status"], "available")
        report = result["report"]
        self.assertEqual(report["instance"]["id"], live.instance_id(me))
        self.assertIn(child.pid, [h["pid"] for h in report["helpers"]])
        text = live.report_text(report)
        for heading in ("What is it doing?", "Current work", "Activity timeline", "Helper processes", "Started by"):
            self.assertIn(heading, text)
        self.assertEqual(live.agent_activity_report("claude", None, [me, child.pid])["status"], "choose_instance")
        self.assertEqual(live.agent_activity_report("claude", f"{me}:1", [me])["status"], "unavailable")
        self.assertEqual(live.agent_activity_report("claude", None, [])["status"], "unavailable")

    def test_progress_owned_and_validated(self):
        runtime = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(runtime, ignore_errors=True))
        me = os.getpid()
        with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(runtime)}):
            self.assertTrue(live.report_progress({"step": "Tests\x07", "done": 1, "total": 3}, [me]))
            self.assertFalse(live.report_progress({"step": "x", "done": 1, "total": 3}, [1]))
            for bad in ({"step": "", "done": 0, "total": 1}, {"step": "x", "done": 2, "total": 1},
                        {"step": "x", "done": 0, "total": 1, "extra": 1}, {"step": "x", "done": "1", "total": 2}):
                with self.assertRaises(ValueError):
                    live.report_progress(bad, [me])
            ident = live.instance_id(me)
            got = live.progress("claude", ident, [me])
            self.assertEqual((got["step"], got["done"], got["total"]), ("Tests", 1, 3))
            self.assertIsNone(live.progress("claude", f"{me}:0", [me]))      # another start time: reused pid
            self.assertIsNone(live.progress("claude", ident, []))


# --- report library and generator -----------------------------------------------------------------

DRAFT = {"title": "T", "from_day": "2026-11-19", "through_day": "2026-11-20", "filters": {"project": None},
         "helper": "local-helper", "generated_at": "2026-11-20T10:00:00Z", "entry_count": 2, "total_entries": 2,
         "capped": False, "topics": [{"name": "Login 100%_done", "summary": "s", "sources": ["agent:claude"],
                                      "projects": [], "spans": 2, "elapsed_ms": 1000, "tokens_in": None,
                                      "tokens_out": None, "usage_unknown": 2}], "text": "Body ünïcode 日本"}


class LibraryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.library = reports_ctl.ReportsLibrary(self.tmp / "reports")

    def test_save_list_search_get_update_delete(self):
        saved = self.library.save(DRAFT, "First", None)
        other = self.library.save(DRAFT, "Second", "a note")
        self.assertEqual(saved["text"], "Body ünïcode 日本")
        self.assertEqual([r["title"] for r in self.library.list()["reports"]], ["Second", "First"])
        self.assertEqual(len(self.library.list("100%")["reports"]), 2)          # topic names, % literal
        self.assertEqual(len(self.library.list("%x")["reports"]), 0)
        self.assertEqual(len(self.library.list("0%_d")["reports"]), 2)
        self.assertEqual(len(self.library.list("a note")["reports"]), 1)
        self.assertEqual(self.library.list()["reports"][0]["topic_count"], 1)
        self.assertEqual(self.library.update(other["id"], title="Renamed", note="")["note"], None)
        self.assertEqual(self.library.get(other["id"])["title"], "Renamed")
        self.assertTrue(self.library.delete(saved["id"]))
        with self.assertRaises(reports_ctl.ReportError) as caught:
            self.library.get(saved["id"])
        self.assertEqual(caught.exception.code, "report_not_found")
        self.assertEqual(stat.S_IMODE(os.stat(self.tmp / "reports").st_mode), 0o700)
        for name in ("reports.db", "reports.db-wal", "reports.db-shm"):
            if (self.tmp / "reports" / name).exists():
                self.assertEqual(stat.S_IMODE(os.stat(self.tmp / "reports" / name).st_mode), 0o600)

    def test_paging(self):
        for i in range(52):
            self.library.save(DRAFT, f"R{i}")
        page = self.library.list()
        self.assertEqual(len(page["reports"]), 50)
        rest = self.library.list(cursor=page["next_cursor"])
        self.assertEqual([r["title"] for r in rest["reports"]], ["R1", "R0"])

    def test_corrupt_and_move_aside(self):
        folder = self.tmp / "reports"
        folder.mkdir(mode=0o700)
        (folder / "reports.db").write_bytes(b"garbage" * 200)
        with self.assertRaises(reports_ctl.ReportError) as caught:
            self.library.list()
        self.assertEqual(caught.exception.code, "reports_db_corrupt")
        moved = self.library.move_aside()
        self.assertIn(".broken-", moved)
        self.assertFalse((folder / "reports.db").exists())
        self.assertTrue(Path(moved).exists())
        self.library.save(DRAFT, "Fresh")
        with self.assertRaises(reports_ctl.ReportError):
            self.library.move_aside()                       # a sound library is never moved

    def test_newer_schema_is_read_only(self):
        self.library.save(DRAFT, "Old")
        con = sqlite3.connect(self.tmp / "reports/reports.db")
        con.execute("PRAGMA user_version=2")
        con.commit()
        con.close()
        listed = self.library.list()
        self.assertTrue(listed["read_only"])
        with self.assertRaises(reports_ctl.ReportError) as caught:
            self.library.save(DRAFT, "New")
        self.assertEqual(caught.exception.code, "reports_db_read_only")

    def test_links_refused(self):
        (self.tmp / "elsewhere").mkdir()
        os.symlink(self.tmp / "elsewhere", self.tmp / "reports")
        with self.assertRaises(reports_ctl.ReportError):
            self.library.list()
        other = reports_ctl.ReportsLibrary(self.tmp / "r2")
        (self.tmp / "r2").mkdir(mode=0o700)
        (self.tmp / "target.db").write_bytes(b"")
        os.symlink(self.tmp / "target.db", self.tmp / "r2/reports.db")
        with self.assertRaises(reports_ctl.ReportError):
            other.list()

    def test_locked_and_two_writers(self):
        self.library.save(DRAFT, "x")
        fd = os.open(self.tmp / "reports", os.O_RDONLY | os.O_DIRECTORY)
        fcntl.flock(fd, fcntl.LOCK_EX)
        with mock.patch.object(reports_ctl, "LOCK_WAIT", 0.1), self.assertRaises(reports_ctl.ReportError) as caught:
            self.library.list()
        self.assertEqual(caught.exception.code, "reports_db_locked")
        os.close(fd)
        threads = [threading.Thread(target=lambda: [self.library.save(DRAFT, "w") for _ in range(5)]) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(self.library.list()["reports"]), 11)

    def test_fields(self):
        self.assertEqual(reports_ctl.report_field(" a\x07b ", 10), "a b")
        self.assertEqual(reports_ctl.report_field("a\nb", 10, multiline=True), "a\nb")
        with self.assertRaises(ValueError):
            reports_ctl.report_field("", 10, required=True)
        with self.assertRaises(ValueError):
            reports_ctl.report_field("x" * 11, 10)


class GenerateTest(TempCase):
    def setUp(self):
        super().setUp()
        self.log.directory.mkdir(parents=True)
        rows = [{"version": 1, "origin": "history", "session": "s", "source_kind": "agent", "source": "claude",
                 "project": "app", "topic": "Private request text", "started_at": "2026-11-20T10:00:00Z",
                 "ended_at": "2026-11-20T10:10:00Z", "duration_ms": 0, "files": ["a.py"], "tokens_in": 10, "tokens_out": 2},
                {"version": 1, "source_kind": "agent", "source": "codex", "project": "web", "topic": "editing",
                 "started_at": "2026-11-20T11:00:00Z", "ended_at": "2026-11-20T11:05:00Z", "duration_ms": 0}]
        (self.log.directory / "2026-11-20.jsonl").write_text(jl(*rows))
        self.now = at("2026-11-20T12:00:00")
        self.prompts = []

    def ask(self, answer):
        def run(prompt):
            self.prompts.append(prompt)
            return answer() if callable(answer) else answer
        return run

    def test_draft_and_duplicate_ids(self):
        answer = json.dumps({"title": "Week", "text": "Summary", "topics": [
            {"name": "Login", "summary": "Work on login", "ids": [1, 1, 2, 99]}]})
        draft = reports_ctl.generate_topic_report(self.log, {"helper": "local-helper"}, self.ask(answer), now=self.now)
        topic = draft["topics"][0]
        self.assertEqual((topic["spans"], topic["tokens_in"], topic["usage_unknown"]), (2, 10, 1))
        self.assertEqual((draft["entry_count"], draft["total_entries"], draft["capped"]), (2, 2, False))
        self.assertIn("<untrusted-entries>", self.prompts[0])
        self.assertNotIn("a.py", self.prompts[0])               # file names never, only their count
        self.assertIn("Private request text", self.prompts[0])
        for form in ("text", "markdown", "json"):
            self.assertIn("Week", reports_ctl.format_topic_report(draft, form))
        self.assertTrue(reports_ctl.format_topic_report(draft, "markdown").startswith("# Week"))

    def test_topics_off_hides_free_text(self):
        self.settings["ai_activity_import_topics"] = False
        answer = json.dumps({"title": "t", "text": "x", "topics": []})
        reports_ctl.generate_topic_report(self.log, {"helper": "h"}, self.ask(answer), now=self.now)
        self.assertNotIn("Private request text", self.prompts[0])
        self.assertIn('"editing"', self.prompts[0])

    def test_bad_answer_retries_once(self):
        with self.assertRaises(reports_ctl.ReportError) as caught:
            reports_ctl.generate_topic_report(self.log, {"helper": "h"}, self.ask("not json"), now=self.now)
        self.assertEqual(caught.exception.code, "bad_answer")
        self.assertEqual(len(self.prompts), 2)

    def test_failures(self):
        def boom(_prompt):
            raise RuntimeError("down")
        with self.assertRaises(reports_ctl.ReportError) as caught:
            reports_ctl.generate_topic_report(self.log, {"helper": "h"}, boom, now=self.now)
        self.assertEqual(caught.exception.code, "helper_failed")
        with self.assertRaises(reports_ctl.ReportError) as caught:
            reports_ctl.generate_topic_report(self.log, {"helper": "h", "project": "none"}, self.ask("{}"), now=self.now)
        self.assertEqual(caught.exception.code, "no_entries")

        def gate():
            raise reports_ctl.ReportError("online_blocked")
        with self.assertRaises(reports_ctl.ReportError) as caught:
            reports_ctl.generate_topic_report(self.log, {"helper": "claude"}, self.ask("{}"), before_run=gate, now=self.now)
        self.assertEqual(caught.exception.code, "online_blocked")
        self.assertEqual(self.prompts, [])
        self.settings["ai_activity_log"] = False
        with self.assertRaises(reports_ctl.ReportError) as caught:
            reports_ctl.generate_topic_report(self.log, {"helper": "h"}, self.ask("{}"), now=self.now)
        self.assertEqual(caught.exception.code, "log_off")
        with self.assertRaises(ValueError):
            reports_ctl.report_parameters({"helper": "h", "other": "x"})

    def test_no_tools_check(self):
        self.assertTrue(reports_ctl.rewrite_without_tools(mock.Mock(rewrite=("claude", "-p", "--tools", ""))))
        self.assertFalse(reports_ctl.rewrite_without_tools(mock.Mock(rewrite=("codex", "exec", "--sandbox", "read-only"))))

    def test_drafts(self):
        drafts = reports_ctl.Drafts()
        handles = [drafts.add({"n": i}) for i in range(10)]
        with self.assertRaises(reports_ctl.ReportError):
            drafts.take(handles[0])
        self.assertEqual(drafts.take(handles[-1]), {"n": 9})


# --- the page and dialog logic (no display) --------------------------------------------------------

class PageLogicTest(TempCase):
    def test_activity_view(self):
        import agent_activity_dialog
        answers = [{"status": "choose_instance", "instances": [{"id": "1:2", "pid": 1, "label": "a"}], "report": None}]
        view = agent_activity_dialog.ActivityView("claude", lambda instance: answers.pop(0) if answers else
                                                  {"status": "unavailable", "instances": [], "report": None})
        self.assertEqual(view.load()["status"], "choose_instance")
        self.assertEqual(view.text(), "")
        view.choose("1:2")
        view.closed = True
        self.assertIsNone(view.load())                  # closed: no more reads (Esc stops the refresh)

        def failing(_instance):
            raise OSError("gone")
        broken = agent_activity_dialog.ActivityView("claude", failing)
        self.assertEqual(broken.load(), {})

    def test_history_job_auto_once_and_quiet(self):
        import reports_page
        job = reports_page.HistoryJob()
        self.settings["ai_activity_import"] = False
        with mock.patch.object(reports_page.GLib, "idle_add"):
            self.assertTrue(job.start(self.log, [], True, lambda: None))
            job.thread.join()
            self.assertIsNone(job.error)                # an automatic run says nothing
            self.assertFalse(job.start(self.log, [], True, lambda: None))
            self.assertTrue(job.start(self.log, [], False, lambda: None))
            job.thread.join()
            self.assertEqual(job.error, "import_off")
            self.settings["ai_activity_import"] = True
            self.assertTrue(job.start(self.log, [], False, lambda: None))
            job.thread.join()
            self.assertEqual(job.result["sources"], [])

    def test_local_topics_classified_off_the_page(self):
        import ai_core
        candidate = object()
        report = {"entries": [{"id": "a", "topic": None, "project": "app", "files": ["x.py"]},
                              {"id": "b", "topic": "Fix login", "project": "app", "files": []}]}
        with mock.patch.object(ai_core, "setting", side_effect=lambda k: k == "ai_activity_local_topics"), \
                mock.patch("local_helper.advise_argv", return_value=(["helper"], "text")):
            out = reports_ctl.classify_topics(report, candidate,
                                              run=lambda argv, **kw: (b'["testing"]', True))
            self.assertEqual(out["entries"][0]["topic"], "testing")
            self.assertEqual(out["entries"][1]["topic"], "Fix login")      # a request topic stays
            wrong = {"entries": [{"id": "c", "topic": None, "project": "app", "files": []}]}
            reports_ctl.classify_topics(wrong, candidate, run=lambda argv, **kw: (b'["deploying"]', True))
            self.assertIsNone(wrong["entries"][0]["topic"])

    def test_keep_choices(self):
        import reports_page
        self.assertEqual(reports_page.keep_choices(7), [1, 2, 7, 30, 90, 365])
        self.assertEqual(reports_page.keep_choices(3), [1, 2, 3, 7, 30, 90, 365])
        self.assertIn("Default", reports_page.keep_label(7))

    def test_observe_routing(self):
        import reports_page
        page = reports_page.ReportsPage()
        page._activity_store = mock.Mock(enabled=True)
        page._history_job = mock.Mock(auto_tried=True)
        usage = mock.Mock(generating=True, runtimes={"llamacpp": mock.Mock(running=True)})
        runtime = mock.Mock(key="llamacpp")
        with mock.patch.object(live, "_cwd", return_value="/w/app"):
            page.observe_activity({"claude", "codex"}, {"claude": [10], "codex": [11, 12]}, usage, [runtime])
        active, dirs, usage_for = page._activity_store.observe.call_args[0]
        self.assertTrue(callable(usage_for))
        self.assertEqual(active, {"agent:claude", "agent:codex", "runtime:llamacpp"})
        self.assertEqual(dirs, {"agent:claude": "/w/app"})      # two instances: no folder to name
        page._activity_store = mock.Mock(enabled=False)
        page.observe_activity({"claude"}, {})
        page._activity_store.observe.assert_called_once_with(set())


class McpTest(unittest.TestCase):
    def call(self, mode, method, params=None, pids=lambda: [os.getpid()]):
        import activity_mcp
        return activity_mcp.reply({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}, mode, pids)

    def test_progress_server(self):
        import activity_mcp
        runtime = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(runtime, ignore_errors=True))
        with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(runtime)}):
            self.assertEqual(self.call("progress", "tools/list")["result"]["tools"][0]["name"], "report_progress")
            done = self.call("progress", "tools/call", {"name": "report_progress",
                                                        "arguments": {"step": "Build", "done": 1, "total": 2}})
            self.assertFalse(done["result"]["isError"])
            snapshot = live.progress_snapshot({"claude": [os.getpid()]})
            self.assertEqual(snapshot["claude"][0]["progress"]["step"], "Build")
            refused = self.call("progress", "tools/call", {"name": "report_progress",
                                                           "arguments": {"step": "x", "done": 3, "total": 2}})
            self.assertTrue(refused["result"]["isError"])
            self.assertIn("error", self.call("progress", "tools/call", {"name": "activity_log_report"}))
        self.assertIsNone(activity_mcp.reply({"jsonrpc": "2.0", "method": "notifications/initialized"}, "log"))
        self.assertEqual(self.call("log", "nope")["error"]["code"], -32601)

    def test_log_server(self):
        import io
        import activity_mcp
        home = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(home, ignore_errors=True))
        with mock.patch.dict(os.environ, {"XDG_DATA_HOME": str(home)}), \
                mock.patch.object(activity_log, "_setting", settings()):
            reports_ctl.ReportsLibrary().save(DRAFT, "Saved one")
            names = [t["name"] for t in self.call("log", "tools/list")["result"]["tools"]]
            self.assertEqual(names, ["activity_log_report", "reports_list", "reports_get"])
            listed = json.loads(self.call("log", "tools/call", {"name": "reports_list"})["result"]["content"][0]["text"])
            self.assertEqual(listed["reports"][0]["title"], "Saved one")
            missing = self.call("log", "tools/call", {"name": "reports_get", "arguments": {"id": 99}})["result"]
            self.assertTrue(missing["isError"])
            report = self.call("log", "tools/call", {"name": "activity_log_report", "arguments": {}})["result"]
            self.assertIn("entries", json.loads(report["content"][0]["text"]))
            self.assertIn("error", self.call("log", "tools/call", {"name": "activity_log_report",
                                                                   "arguments": {"bad": 1}}))
            out = io.StringIO()
            activity_mcp.serve("log", io.BytesIO(b'{"jsonrpc":"2.0","id":7,"method":"ping"}\nnot json\n' +
                                                 b"x" * (activity_mcp.MAX_LINE + 10) + b"\n"), out)
            answers = [json.loads(line) for line in out.getvalue().splitlines()]
            self.assertEqual([a.get("id") for a in answers], [7, None, None])


class RoundOneTest(TempCase):
    """Review round 1: paths in topics, SQLite edges, bounded reads, the page's workers."""

    def test_private_paths_never_reach_the_helper(self):
        self.log.directory.mkdir(parents=True)
        row = {"version": 1, "origin": "history", "session": "s", "source_kind": "agent", "source": "claude",
               "topic": "fix file:///home/u/private/a.py and [/home/u/private/b.py] (~/secret/c.md) x:/home/u/private/d",
               "started_at": "2026-11-20T10:00:00Z", "ended_at": "2026-11-20T10:10:00Z", "duration_ms": 0}
        (self.log.directory / "2026-11-20.jsonl").write_text(jl(row))
        prompts = []
        answer = json.dumps({"title": "t", "text": "x", "topics": []})
        reports_ctl.generate_topic_report(self.log, {"helper": "h"}, lambda p: prompts.append(p) or answer,
                                          now=at("2026-11-20T12:00:00"))
        self.assertNotIn("/home/u", prompts[0])
        self.assertNotIn("private/", prompts[0])
        self.assertNotIn("secret/", prompts[0])
        for name in ("a.py", "[b.py]", "c.md"):
            self.assertIn(name, prompts[0])
        self.assertEqual(activity_log.sanitize_topic("see https://example.com/docs/x?q=1"),
                         "see https://example.com/docs/x?q=1")
        self.assertEqual(activity_log.sanitize_topic("ratio 2/3 and a/b"), "ratio 2/3 and a/b")

    def test_damaged_report_json(self):
        library = reports_ctl.ReportsLibrary(self.tmp / "reports")
        saved = library.save(DRAFT, "x")
        con = sqlite3.connect(self.tmp / "reports/reports.db")
        con.execute("UPDATE reports SET filters_json='{bad', topics_json='[1]'")
        con.commit()
        con.close()
        with self.assertRaises(reports_ctl.ReportError) as caught:
            library.get(saved["id"])
        self.assertEqual(caught.exception.code, "report_damaged")
        self.assertIn("damaged", reports_ctl.message("report_damaged"))

    def test_oversized_wal_is_unavailable_not_damaged(self):
        import readonly_sqlite
        library = reports_ctl.ReportsLibrary(self.tmp / "reports")
        library.save(DRAFT, "x")
        folder = self.tmp / "reports"
        (folder / "reports.db-wal").write_bytes(b"\0" * 4096)
        with mock.patch.object(readonly_sqlite, "MAX_BYTES", 1024):
            for call in (library.list, library.move_aside):
                with self.assertRaises(reports_ctl.ReportError) as caught:
                    call()
                self.assertEqual(caught.exception.code, "reports_db_unavailable")
        self.assertTrue((folder / "reports.db").exists())          # never moved aside

    def test_probe_distrusts_the_schema(self):
        library = reports_ctl.ReportsLibrary(self.tmp / "reports")
        library.save(DRAFT, "x")
        fd = activity_log.private_dir(self.tmp / "reports")
        try:
            for wal in (False, True):
                if wal:
                    (self.tmp / "reports/reports.db-wal").write_bytes(b"")
                con = library._probe(fd, wal)
                try:
                    self.assertEqual(con.execute("PRAGMA trusted_schema").fetchone()[0], 0)
                finally:
                    con.close()
        finally:
            os.close(fd)

    def test_export_name_takes_only_days(self):
        self.assertEqual(reports_ctl.export_name({"from_day": "2026-11-01", "through_day": "../../x"}, "txt"),
                         "ai-agents-map-report-2026-11-01.txt")

    def test_untrusted_ancestor_refused(self):
        shared = self.tmp / "shared"
        shared.mkdir()
        os.chmod(shared, 0o777)                        # writable by everyone, no sticky bit
        with self.assertRaises(PermissionError):
            activity_log.private_dir(shared / "a" / "ai-log")

    def test_history_root_and_adapter_checks(self):
        import adapter_data
        import harnesses
        h = mock.Mock(command="evil", sessions=object(), session_files="", full_name="Evil", activity_reader="pi-files",
                      activity_dir="~/../../etc", extra_dir_option="")
        with mock.patch.object(harnesses, "all_harnesses", return_value=[h]), \
                mock.patch.object(harnesses, "get", return_value=h):
            found = history_import.history_sources({"evil"}, home=self.tmp)
        self.assertEqual(found[0]["supported"], False)
        self.assertEqual(found[0]["root"], "")
        base = (adapter_data.ADAPTERS / "pi.toml").read_text()
        for bad in ('activity_dir = "~/../x"', 'activity_reader = "sqlite-anything"', 'extra_dir_option = "; rm"'):
            path = self.tmp / "pi.toml"
            path.write_text(base.replace("[sessions]\n", f"[sessions]\n{bad}\n", 1).replace(
                'activity_dir = "~/.pi/agent/sessions"\n', "", 1 if "activity_dir" in bad else 0))
            with self.subTest(bad=bad), self.assertRaises(adapter_data.AdapterError):
                adapter_data.load(path)

    def test_enumeration_is_budgeted(self):
        root = self.tmp / "claude"
        project = root / "p"
        project.mkdir(parents=True)
        for i in range(30):
            (project / f"s{i}.jsonl").write_text("{}\n")
        importer = history_import.HistoryImporter(self.log, [], now=at("2026-11-20T18:00:00"))
        importer._overall = importer._source = importer.clock()
        with mock.patch.object(history_import, "MAX_VISITED", 10):
            found, partial = importer._candidates(str(root), "claude-files", at("2000-01-01T00:00:00"))
        self.assertTrue(partial)
        self.assertLess(len(found), 10)

    def test_proc_reads_are_bounded_and_own(self):
        self.assertTrue(live.argv_of(os.getpid()))
        self.assertTrue(live.comm(os.getpid()))
        if os.stat("/proc/1").st_uid != os.getuid():
            self.assertEqual(live.argv_of(1), [])         # another account's process
            self.assertEqual(live.comm(1), "")
        with mock.patch("builtins.open", mock.mock_open(read_data=b"x" * 10)) as opened, \
                mock.patch.object(live.os, "stat", return_value=mock.Mock(st_uid=os.getuid())):
            live.argv_of(5)
        opened.return_value.read.assert_called_once_with(64 * 1024)

    def test_usage_totals_and_live_rows(self):
        claude = jl({"type": "assistant", "message": {"id": "m1", "model": "opus", "usage": {"input_tokens": 5, "output_tokens": 1}}},
                    {"type": "assistant", "message": {"id": "m1", "model": "opus", "usage": {"input_tokens": 9, "output_tokens": 2}}},
                    {"type": "assistant", "message": {"id": "m2", "model": "opus", "usage": {"input_tokens": 1, "output_tokens": 1}}})
        self.assertEqual(live.usage_totals(claude, False), {"model": "opus", "input": 10, "output": 3})
        codex = jl({"type": "turn_context", "payload": {"model": "gpt-5"}},
                   {"type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 40, "output_tokens": 4}}}})
        self.assertEqual(live.usage_totals(codex, True), {"model": "gpt-5", "input": 40, "output": 4})
        # through observe_activity: the page's callback reaches the log's live row
        import reports_page
        page = reports_page.ReportsPage()
        page._activity_store = self.log
        page._history_job = mock.Mock(auto_tried=True)
        readings = iter([{"session": "1:2:/s", "model": "opus", "input": 100, "output": 10},
                         {"session": "1:2:/s", "model": "opus", "input": 160, "output": 30}])
        with mock.patch.object(live, "session_usage", side_effect=lambda *a, **k: next(readings)), \
                mock.patch.object(live, "_cwd", return_value=""), \
                mock.patch.object(activity_log, "now_utc", side_effect=[at("2026-11-20T10:00:00"), at("2026-11-20T10:05:00")]):
            page.observe_activity({"claude"}, {"claude": [123]})
            page.observe_activity(set(), {"claude": [123]})
        row = self.rows("2026-11-20")[0]
        self.assertEqual((row["tokens_in"], row["tokens_out"], row["model"]), (60, 20, "opus"))
        with mock.patch.object(live, "live_session_file", return_value=None):
            self.assertIsNone(live.session_usage("claude", [os.getpid()]))
        self.assertIsNone(live.session_usage("claude", []))

    def test_page_workers_drop_stale_answers(self):
        import reports_page
        page = reports_page.ReportsPage()
        page.reports_requests = {}
        got = []
        pending = []
        with mock.patch.object(reports_page.GLib, "idle_add", side_effect=lambda f, *a: pending.append((f, a))), \
                mock.patch.object(reports_page.threading, "Thread",
                                  side_effect=lambda target, daemon: mock.Mock(start=target)):
            page.reports_worker("library", lambda: "old", got.append)
            page.reports_worker("library", lambda: "new", got.append)
            page.reports_worker("log", lambda: 1 / 0, got.append)
        for function, args in pending:
            function(*args)
        self.assertEqual(got[0], "new")
        self.assertIsInstance(got[1], ZeroDivisionError)
        self.assertEqual(len(got), 2)

    def test_online_switch_reloads_helpers(self):
        import reports_page
        page = reports_page.ReportsPage()
        page.reports_tab = "topics"
        page.save_setting = mock.Mock()
        page.load_report_helpers = mock.Mock()
        page.fill_reports_topics = mock.Mock()
        with mock.patch.object(reports_page.ai_core, "setting", return_value=False), \
                mock.patch.object(reports_page.GLib, "idle_add", side_effect=lambda f, *a: f(*a)):
            page.change_report_setting("ai_activity_log_online", True)
        page.load_report_helpers.assert_called_once()
        page.fill_reports_topics.assert_called_once()

    def test_activity_view_stale_answer(self):
        import agent_activity_dialog
        view = agent_activity_dialog.ActivityView("claude", lambda i: {"status": "unavailable", "report": None})
        asked = view.begin()[0]
        self.assertIsNone(view.begin())                  # one read at a time
        view.choose("9:9")
        self.assertIsNone(view.accept(asked, {"status": "x"}))   # another instance chosen meanwhile
        self.assertFalse(view.loading)


class RoundTwoTest(TempCase):
    def test_paths_inside_urls_never_reach_the_helper(self):
        self.log.directory.mkdir(parents=True)
        row = {"version": 1, "origin": "history", "session": "s", "source_kind": "agent", "source": "claude",
               "topic": "see https://example.com/preview?file=/home/u/private/a.py#/home/u/private/b.py "
                        "and https://e.com/?u=file:///home/u/private/c.py&page=2",
               "started_at": "2026-11-20T10:00:00Z", "ended_at": "2026-11-20T10:10:00Z", "duration_ms": 0}
        (self.log.directory / "2026-11-20.jsonl").write_text(jl(row))
        prompts = []
        answer = json.dumps({"title": "t", "text": "x", "topics": []})
        reports_ctl.generate_topic_report(self.log, {"helper": "h"}, lambda p: prompts.append(p) or answer,
                                          now=at("2026-11-20T12:00:00"))
        self.assertNotIn("/home/u", prompts[0])
        self.assertNotIn("%2Fhome", prompts[0])
        self.assertIn("file=a.py", prompts[0])
        self.assertIn("page=2", prompts[0])

    def test_older_addons_keep_the_session_facts(self):
        import dataclasses
        import harnesses
        own = next(h for h in harnesses.built_in_adapters() if h.command == "codex")
        old = dataclasses.replace(own, activity_reader="", activity_dir="", extra_dir_option="")
        filled = harnesses._with_newer_facts(old)
        self.assertEqual((filled.activity_reader, filled.activity_dir, filled.extra_dir_option),
                         (own.activity_reader, own.activity_dir, own.extra_dir_option))

    def test_progress_rechecks_the_owner_before_writing(self):
        runtime = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(runtime, ignore_errors=True))
        me, parent = os.getpid(), os.getppid()
        lines = iter([[parent], []])                       # the parent left the caller's line meanwhile
        with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(runtime)}), \
                mock.patch.object(live, "_ancestors", side_effect=lambda pid, proc="/proc": next(lines)):
            self.assertFalse(live.report_progress({"step": "x", "done": 0, "total": 1}, [parent], caller=me))
        self.assertFalse((runtime / "ai-agents-map/progress").exists() and any((runtime / "ai-agents-map/progress").iterdir()))
        with self.assertRaises(ValueError):
            live.report_progress({"step": "\x07\x08", "done": 0, "total": 1}, [me])

    def test_reads_never_create_the_library(self):
        library = reports_ctl.ReportsLibrary(self.tmp / "none")
        self.assertEqual(library.list()["reports"], [])
        with self.assertRaises(reports_ctl.ReportError) as caught:
            library.get(1)
        self.assertEqual(caught.exception.code, "report_not_found")
        self.assertFalse((self.tmp / "none").exists())
        (self.tmp / "empty").mkdir(mode=0o700)
        empty = reports_ctl.ReportsLibrary(self.tmp / "empty")
        self.assertEqual(empty.list()["reports"], [])
        self.assertEqual(list((self.tmp / "empty").iterdir()), [])
        empty.save(DRAFT, "x")
        files = sorted(p.name for p in (self.tmp / "empty").iterdir())
        empty.list()
        empty.get(1)
        self.assertEqual(sorted(p.name for p in (self.tmp / "empty").iterdir()), files)
        self.assertEqual(activity_log.ActivityLog(self.tmp / "nolog", setting=self.settings).report({"cursor": None})
                         ["entries"], [])
        for bad in (False, "", [], -1):
            with self.assertRaises(ValueError):
                self.log.report({"cursor": bad})

    def test_every_rendered_field_is_checked(self):
        library = reports_ctl.ReportsLibrary(self.tmp / "reports")
        library.save(DRAFT, "ok")
        library.save(DRAFT, "bad")
        con = sqlite3.connect(self.tmp / "reports/reports.db")
        con.execute("""UPDATE reports SET topics_json='[{"name":"n","summary":"s"}]' WHERE title='bad'""")
        con.commit()
        con.close()
        with self.assertRaises(reports_ctl.ReportError) as caught:
            library.get(2)
        self.assertEqual(caught.exception.code, "report_damaged")
        self.assertEqual(library.get(1)["title"], "ok")
        con = sqlite3.connect(self.tmp / "reports/reports.db")
        con.execute("UPDATE reports SET topics_json='{bad' WHERE title='bad'")
        con.commit()
        con.close()
        listed = library.list()["reports"]                    # one damaged row never hides the others
        self.assertEqual([(r["title"], r["topic_count"]) for r in listed], [("bad", 0), ("ok", 1)])
        self.assertEqual(reports_ctl._code(sqlite3.OperationalError("malformed JSON")), "reports_db_unavailable")

    def test_hard_links_sidecar_links_and_move_aside(self):
        library = reports_ctl.ReportsLibrary(self.tmp / "reports")
        library.save(DRAFT, "x")
        folder = self.tmp / "reports"
        os.link(folder / "reports.db", self.tmp / "second-name")
        with self.assertRaises(reports_ctl.ReportError):
            library.list()
        os.unlink(self.tmp / "second-name")
        (self.tmp / "elsewhere").write_bytes(b"")
        os.symlink(self.tmp / "elsewhere", folder / "reports.db-shm")
        with self.assertRaises(reports_ctl.ReportError):
            library.list()
        os.unlink(folder / "reports.db-shm")
        (folder / "reports.db").write_bytes(b"garbage" * 300)
        (folder / "reports.db-journal").write_bytes(b"j")
        moved = library.move_aside()
        suffix = moved.split("reports.db", 1)[1]
        self.assertTrue((folder / ("reports.db-journal" + suffix)).exists())
        self.assertFalse((folder / "reports.db-journal").exists())

    def test_local_classification_asks_eight_and_says_when_it_failed(self):
        import ai_core
        rows = [{"id": f"r{i}", "topic": None, "project": "app", "files": []} for i in range(12)]
        asked = []

        def run(argv, **kw):
            asked.append(json.loads(kw["stdin"].decode().split("\n", 1)[1]))
            return b'["testing", null, "editing", "testing", "testing", "testing", "testing", "testing"]', True
        with mock.patch.object(ai_core, "setting", side_effect=lambda k: k == "ai_activity_local_topics"), \
                mock.patch("local_helper.advise_argv", return_value=(["helper"], "text")):
            answers = reports_ctl.classify_entries(rows, object(), run=run)
            self.assertEqual(len(asked[0]), 8)
            self.assertEqual(set(answers), {f"r{i}" for i in range(8)})
            self.assertIsNone(answers["r1"])
            self.assertIsNone(reports_ctl.classify_entries(rows, object(), run=lambda a, **k: (b"", False)))
            self.assertIsNone(reports_ctl.classify_entries(rows, object(), run=lambda a, **k: (b'["x"]', True)))
        with mock.patch.object(ai_core, "read_bounded_output", return_value=(b"[]", True)) as bounded, \
                mock.patch.dict(os.environ, {}, clear=False):
            reports_ctl._isolated_run(["x"], timeout=1)
        kwargs = bounded.call_args.kwargs
        self.assertEqual(kwargs["env"]["AI_AGENTS_MAP_NO_PROVIDERS"], "1")
        self.assertTrue(kwargs["cwd"])

    def test_page_remembers_only_answers(self):
        import reports_page
        page = reports_page.ReportsPage()
        page.reports_local_topics, page.reports_classifying, page.reports_classify_after = {}, False, 0.0
        page.local_ai_helper = lambda: object()
        page.history_changed = mock.Mock()
        report = {"entries": [{"id": f"r{i}", "topic": None, "project": "app", "files": []} for i in range(12)]}
        results = iter([{"r0": "testing", "r1": None}, None])
        with mock.patch.object(reports_page.ai_core, "setting", side_effect=lambda k: k == "ai_activity_local_topics"), \
                mock.patch.object(reports_page.reports_ctl, "classify_entries", side_effect=lambda *a: next(results)), \
                mock.patch.object(reports_page.GLib, "idle_add", side_effect=lambda f, *a: f(*a)), \
                mock.patch.object(reports_page.threading, "Thread",
                                  side_effect=lambda target, daemon: mock.Mock(start=target)):
            page.local_topics(report)
            self.assertEqual(page.reports_local_topics, {"r0": "testing", "r1": None})
            page.local_topics(report)                           # r2.. asked next; the LLM fails this time
            self.assertEqual(set(page.reports_local_topics), {"r0", "r1"})
            self.assertGreater(page.reports_classify_after, 0)
            page.local_topics(report)                           # backing off: not asked again now
        self.assertEqual(report["entries"][0]["topic"], "testing")

    def test_antigravity_statuses(self):
        root = self.tmp / "agy"
        root.mkdir()
        (root / "a.db").write_bytes(b"SQLite format 3\0" + b"\xff" * 200)        # a header, then nonsense
        source = {"source": "agent:agy", "name": "Antigravity", "supported": True, "form": "trajectory-sqlite",
                  "root": str(root)}
        importer = history_import.HistoryImporter(self.log, [source], now=at("2026-11-20T18:00:00"))
        self.assertEqual(importer.run()["sources"][0]["status"], "corrupt")
        (root / "a.db").unlink()
        con = sqlite3.connect(root / "b.db")
        con.execute("CREATE TABLE steps (idx INTEGER, step_type INTEGER, status INTEGER, step_format INTEGER, step_payload BLOB)")
        con.commit()
        con.close()
        with mock.patch.object(history_import.HistoryImporter, "_open_conversation",
                               side_effect=history_import._OutOfTime()):
            result = history_import.HistoryImporter(self.log, [source], now=at("2026-11-20T18:00:00")).run()
        self.assertEqual(result["sources"][0]["status"], "partial")
        os.chmod(root / "b.db", 0)
        if os.getuid() != 0:
            result = history_import.HistoryImporter(self.log, [source], now=at("2026-11-20T18:00:00")).run()
            self.assertEqual(result["sources"][0]["status"], "unavailable")

    def test_usage_needs_exactly_one_live_instance(self):
        with mock.patch.object(live, "alive", side_effect=lambda p, proc="/proc": p != 2):
            with mock.patch.object(live, "instance_id", return_value=None):
                self.assertIsNone(live.session_usage("codex", [1, 2, 3]))      # two still run: no attribution
        big = jl({"type": "assistant", "message": {"id": "m", "usage": {"input_tokens": 3, "output_tokens": 1},
                                                   "content": "x" * (200 * 1024)}})
        self.assertEqual(live.usage_totals(big, False)["input"], 3)
        negative = jl({"type": "event_msg", "payload": {"type": "token_count", "info": {
            "total_token_usage": {"input_tokens": -5, "output_tokens": 1}}}})
        self.assertIsNone(live.usage_totals(negative, True))


class RoundThreeTest(TempCase):
    def test_paths_in_url_keys_and_encoded_segments(self):
        self.log.directory.mkdir(parents=True)
        topics = ["a https://example.com/?/home/u/private/a.py", "b https://example.com/?/home/u/private/b.py=1",
                  "c https://example.com/?file:///home/u/private/c.py", "d https://example.com/?%2Fhome%2Fu%2Fprivate%2Fd.py",
                  "e https://example.com/open/%2Fhome%2Fu%2Fprivate%2Fe.py"]
        rows = [{"version": 1, "origin": "history", "session": f"s{i}", "source_kind": "agent", "source": "claude",
                 "topic": t, "started_at": f"2026-11-20T1{i}:00:00Z", "ended_at": f"2026-11-20T1{i}:10:00Z",
                 "duration_ms": 0} for i, t in enumerate(topics)]
        (self.log.directory / "2026-11-20.jsonl").write_text(jl(*rows))
        prompts = []
        answer = json.dumps({"title": "t", "text": "x", "topics": []})
        reports_ctl.generate_topic_report(self.log, {"helper": "h"}, lambda p: prompts.append(p) or answer,
                                          now=at("2026-11-20T23:00:00"))
        for leak in ("/home/u", "%2Fhome", "private"):
            self.assertNotIn(leak, prompts[0])
        for name in ("a.py", "b.py=1", "c.py", "d.py", "/open/e.py"):
            self.assertIn(name, prompts[0])

    def conversation(self, root: Path) -> Path:
        root.mkdir(exist_ok=True)
        db = root / "conv.db"
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE steps (idx INTEGER, step_type INTEGER, status INTEGER, step_format INTEGER, step_payload BLOB)")
        con.commit()
        con.close()
        return db

    def importer(self, root, now="2026-11-20T18:00:00"):
        source = {"source": "agent:agy", "name": "Antigravity", "supported": True, "form": "trajectory-sqlite",
                  "root": str(root)}
        return history_import.HistoryImporter(self.log, [source], now=at(now))

    def test_hot_journal_and_changing_snapshot_are_unavailable(self):
        db = self.conversation(self.tmp / "agy")
        (self.tmp / "agy/conv.db-journal").write_bytes(b"\xd9\xd5\x05\xf9\x20\xa1\x63\xd7" + b"\0" * 100)
        self.assertEqual(self.importer(self.tmp / "agy").run()["sources"][0]["status"], "unavailable")
        (self.tmp / "agy/conv.db-journal").unlink()
        real = history_import._read_all
        calls = []

        def checkpoint_between(fd, limit):           # the WAL appears after the base was read
            data = real(fd, limit)
            if not calls:
                (self.tmp / "agy/conv.db-wal").write_bytes(b"")
            calls.append(1)
            return data
        importer = self.importer(self.tmp / "agy")
        importer._overall = importer._source = importer.clock()
        with mock.patch.object(history_import, "_read_all", side_effect=checkpoint_between):
            with self.assertRaises(OSError):
                importer._open_conversation(str(db))

    def test_conversation_limits(self):
        db = self.conversation(self.tmp / "agy")
        importer = self.importer(self.tmp / "agy")
        importer._overall = importer._source = importer.clock()
        con = importer._open_conversation(str(db))
        try:
            self.assertEqual(con.getlimit(sqlite3.SQLITE_LIMIT_LENGTH), 1 << 20)
            self.assertEqual(con.getlimit(sqlite3.SQLITE_LIMIT_COLUMN), 128)
            with self.assertRaises(sqlite3.DataError):
                con.execute("SELECT zeroblob(2000000)").fetchone()
        finally:
            con.close()

    def test_birth_time_and_stable_conversation_id(self):
        db = self.conversation(self.tmp / "agy")
        born = history_import._birth(str(db))
        if born is not None:                                  # this file system keeps birth times
            os.utime(db, (born + 5000, born + 5000))
            self.assertEqual(history_import._birth(str(db)), born)      # a change never moves the start
        start = at("2026-11-20T14:00:00").timestamp()
        with mock.patch.object(history_import, "_birth", return_value=start):
            t = at("2026-11-20T15:00:00").timestamp()
            os.utime(db, (t, t))
            first = self.importer(self.tmp / "agy").run()
            t2 = at("2026-11-20T16:00:00").timestamp()
            os.utime(db, (t2, t2))
            second = self.importer(self.tmp / "agy", now="2026-11-20T19:30:00").run()
        self.assertEqual((first["sources"][0]["added"], second["sources"][0]["added"],
                          second["sources"][0]["updated"]), (1, 0, 1))
        rows = [r for r in self.rows("2026-11-20") if r["origin"] == "history"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["duration_ms"], 2 * 3600 * 1000)

    def test_library_refuses_a_hot_journal(self):
        library = reports_ctl.ReportsLibrary(self.tmp / "reports")
        library.save(DRAFT, "x")
        (self.tmp / "reports/reports.db-journal").write_bytes(reports_ctl.HOT_JOURNAL + b"\0" * 50)
        for call in (library.list, library.move_aside):
            with self.assertRaises(reports_ctl.ReportError) as caught:
                call()
            self.assertEqual(caught.exception.code, "reports_db_unavailable")
        self.assertTrue((self.tmp / "reports/reports.db").exists())

    def test_proc_reads_are_budgeted(self):
        import scan_budget
        budget = scan_budget.Budget(visits=3, seconds=5)
        self.assertLessEqual(len(live.own_processes(budget=budget)), 3)
        self.assertTrue(budget.stopped)
        with mock.patch.object(live.os, "listdir", side_effect=AssertionError("listed whole")):
            live.own_processes()
            self.assertTrue(live.open_files(os.getpid()))
        self.assertLessEqual(len(live.open_files(os.getpid(), most=2)), 2)


class RoundFourTest(TempCase):
    def test_hostless_and_encoded_authority_urls(self):
        self.log.directory.mkdir(parents=True)
        topics = ["a https:///home/u/private/a.py", "b http:///home/u/private/b.py?x=1",
                  "c https://user%40evil.com/x", "d https://user%3Apw%40host.example/y"]
        rows = [{"version": 1, "origin": "history", "session": f"s{i}", "source_kind": "agent", "source": "claude",
                 "topic": t, "started_at": f"2026-11-20T1{i}:00:00Z", "ended_at": f"2026-11-20T1{i}:10:00Z",
                 "duration_ms": 0} for i, t in enumerate(topics)]
        (self.log.directory / "2026-11-20.jsonl").write_text(jl(*rows))
        prompts = []
        answer = json.dumps({"title": "t", "text": "x", "topics": []})
        reports_ctl.generate_topic_report(self.log, {"helper": "h"}, lambda p: prompts.append(p) or answer,
                                          now=at("2026-11-20T23:00:00"))
        for leak in ("/home/u", "private", "evil.com", "%40", "pw"):
            self.assertNotIn(leak, prompts[0])
        self.assertIn('"a a.py"', prompts[0])

    def test_live_conversation_reads_are_limited(self):
        db = self.tmp / "conv.db"
        con = sqlite3.connect(db)
        con.execute("CREATE VIEW steps AS SELECT 0 AS idx, 14 AS step_type, 3 AS status, 0 AS step_format, "
                    "zeroblob(2000000) AS step_payload")
        con.commit()
        con.close()
        import readonly_sqlite
        seen = []

        class Watched:                                 # the real snapshot, its limits looked at per query
            def __init__(self, con):
                self.con = con

            def __getattr__(self, name):
                return getattr(self.con, name)

            def execute(self, *args):
                seen.append(self.con.getlimit(sqlite3.SQLITE_LIMIT_LENGTH))
                return self.con.execute(*args)
        result = live.trajectory_activity(str(db), "s", connect=lambda path: Watched(readonly_sqlite.connect(path)))
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(seen, [1 << 20])
        opened = readonly_sqlite.connect(db)
        try:
            live.restrict_reads(opened)
            self.assertEqual(opened.getlimit(sqlite3.SQLITE_LIMIT_LENGTH), 1 << 20)
        finally:
            opened.close()

    def test_replaced_database_between_opens(self):
        root = self.tmp / "agy"
        root.mkdir()
        db = root / "conv.db"
        for name in ("conv.db", "other.db"):
            con = sqlite3.connect(root / name)
            con.execute("CREATE TABLE steps (idx INTEGER)")
            con.commit()
            con.close()
        importer = history_import.HistoryImporter(self.log, [], now=at("2026-11-20T18:00:00"))
        importer._overall = importer._source = importer.clock()
        real_open = os.open

        def swap(name, flags, *args, **kwargs):     # the database is replaced right after it was opened
            fd = real_open(name, flags, *args, **kwargs)
            if name == str(db):
                os.replace(root / "other.db", db)
            return fd
        with mock.patch.object(history_import.os, "open", side_effect=swap):
            with self.assertRaises(OSError):
                importer._open_conversation(str(db))

    def test_mcp_refuses_huge_numbers_and_survives(self):
        import io
        import activity_mcp
        home = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(home, ignore_errors=True))
        with mock.patch.dict(os.environ, {"XDG_DATA_HOME": str(home)}):
            reports_ctl.ReportsLibrary().save(DRAFT, "x")
            for name, arguments in (("reports_get", {"id": 1 << 63}), ("reports_list", {"cursor": 1 << 64})):
                result = activity_mcp.reply({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                             "params": {"name": name, "arguments": arguments}}, "log")["result"]
                self.assertTrue(result["isError"])
                self.assertIn("invalid_request", result["content"][0]["text"])
            out = io.StringIO()
            with mock.patch.object(activity_mcp, "reply", side_effect=[RuntimeError("boom"), {"jsonrpc": "2.0", "id": 2,
                                                                                            "result": {}}]):
                activity_mcp.serve("log", io.BytesIO(b'{"jsonrpc":"2.0","id":1,"method":"ping"}\n'
                                                     b'{"jsonrpc":"2.0","id":2,"method":"ping"}\n'), out)
            answers = [json.loads(line) for line in out.getvalue().splitlines()]
            self.assertEqual([(a["id"], "error" in a) for a in answers], [(1, True), (2, False)])
        with self.assertRaises(reports_ctl.ReportError):
            reports_ctl.ReportsLibrary(self.tmp / "r").get(1 << 63)

    def test_listing_marks_damaged_rows(self):
        library = reports_ctl.ReportsLibrary(self.tmp / "reports")
        library.save(DRAFT, "ok")
        library.save(DRAFT, "bad")
        con = sqlite3.connect(self.tmp / "reports/reports.db")
        con.execute("UPDATE reports SET title=x'00ff', from_day=7 WHERE title='bad'")
        con.commit()
        con.close()
        rows = library.list()["reports"]
        self.assertEqual([(r["title"], r.get("damaged", False)) for r in rows], [("Damaged report", True), ("ok", False)])
        json.dumps(rows)                                   # always serializable (the MCP server, the page)
        with self.assertRaises(reports_ctl.ReportError) as caught:
            library.get(rows[0]["id"])
        self.assertEqual(caught.exception.code, "report_damaged")


class RoundFiveTest(TempCase):
    def test_cancelled_report_sends_nothing(self):
        import threading as _threading
        import ai_core
        import ai_helper
        self.log.directory.mkdir(parents=True)
        (self.log.directory / "2026-11-20.jsonl").write_text(jl(
            {"version": 1, "source_kind": "agent", "source": "codex", "started_at": "2026-11-20T11:00:00Z",
             "ended_at": "2026-11-20T11:05:00Z", "duration_ms": 0}))
        cancel = _threading.Event()
        cancel.set()
        asked = []
        with self.assertRaises(reports_ctl.ReportError) as caught:
            reports_ctl.generate_topic_report(self.log, {"helper": "h"}, asked.append, cancel=cancel,
                                              now=at("2026-11-20T12:00:00"))
        self.assertEqual((caught.exception.code, asked), ("cancelled", []))
        helper = mock.Mock(local=True, candidate=object())
        with ai_helper.watched(cancel), mock.patch.object(ai_helper, "advise") as advise:
            with self.assertRaises(reports_ctl.ReportError):
                reports_ctl.ask_with(helper, "prompt")
        advise.assert_not_called()
        with mock.patch.object(ai_core.subprocess, "Popen") as popen:
            self.assertEqual(ai_core.read_bounded_output(["true"], stdin=b"log", cancel=cancel), (b"", False))
        popen.assert_not_called()

    def test_mcp_survives_malformed_unicode(self):
        import io
        import activity_mcp
        out = io.StringIO()
        activity_mcp.serve("log", io.BytesIO(b'{"jsonrpc":"2.0","id":"\\ud800","method":"ping"}\n'
                                             b'{"jsonrpc":"2.0","id":2,"method":"ping"}\n'), out)
        text = out.getvalue()
        self.assertTrue(text.isascii())
        answers = [json.loads(line) for line in text.splitlines()]
        self.assertEqual(answers[0]["error"]["code"], -32600)
        self.assertEqual((answers[1]["id"], answers[1]["result"]), (2, {}))
        self.assertEqual(activity_mcp.reply({"jsonrpc": "2.0", "id": 1 << 80, "method": "ping"}, "log")["error"]["code"],
                         -32600)

    def test_unobserved_time_is_never_logged(self):
        self.log.unobserved = activity_log.UNOBSERVED
        t0 = at("2026-11-20T10:00:00")
        self.log.observe({"agent:claude"}, now=t0)
        self.log.observe({"agent:claude"}, now=t0 + dt.timedelta(seconds=2))
        self.log.observe(set(), now=t0 + dt.timedelta(hours=8))          # window was hidden all that time
        row = self.rows("2026-11-20")[0]
        self.assertEqual(row["duration_ms"], 2000)
        self.log.observe({"agent:pi"}, now=t0 + dt.timedelta(hours=9))
        self.log.observe({"agent:pi"}, now=t0 + dt.timedelta(hours=9, seconds=2))
        self.log.observe({"agent:pi"}, now=t0 + dt.timedelta(hours=12))  # still working after the gap
        self.log.observe(set(), now=t0 + dt.timedelta(hours=12, seconds=4))
        pi = sorted((r["duration_ms"] for r in self.rows("2026-11-20") if r["source"] == "pi"))
        self.assertEqual(pi, [2000, 4000])

    def test_hiding_ends_open_spans(self):
        import reports_page
        page = reports_page.ReportsPage()
        page._activity_store = self.log
        page.agent_pids = {}
        page.reports_timer = page.reports_pulse = None
        self.log.observe({"agent:claude"}, now=activity_log.now_utc() - dt.timedelta(seconds=30))
        workers = []
        with mock.patch.object(reports_page.threading, "Thread",
                               side_effect=lambda target, daemon: workers.append(target) or mock.Mock()):
            page.reports_hidden()
        self.assertEqual(len(workers), 1)                  # the window's thread only marks it paused
        self.assertIn("agent:claude", self.log.active)
        self.log.observe({"agent:pi"})                      # a poll that was still under way: ignored
        workers[0]()
        self.assertEqual(self.log.active, {})
        rows = [r for d in (activity_log.day_of(activity_log.now_utc()),) for r in self.rows(d)]
        self.assertEqual([r["source"] for r in rows], ["claude"])
        self.assertLess(rows[0]["duration_ms"], 60_000)
        page.reports_tab, page.page_name = "log", lambda: "main"
        page.reports_shown()
        self.assertTrue(self.log.accepting)

    def test_building_the_page_reads_nothing(self):
        import reports_page

        class Toggle:
            def __init__(self, **kw):
                self.active, self.handlers = False, []

            def set_group(self, _g):
                pass

            def connect(self, _name, handler):
                self.handlers.append(handler)

            def get_active(self):
                return self.active

            def set_active(self, value):
                changed, self.active = self.active != value, value
                if changed:
                    for handler in self.handlers:
                        handler(self)

            def add_css_class(self, _c):
                pass
        fake = mock.MagicMock()
        fake.ToggleButton = Toggle
        page = reports_page.ReportsPage()
        page.page_header = mock.Mock()
        page.scrolled = mock.Mock()
        page.show_reports_tab = mock.Mock()
        with mock.patch.object(reports_page, "Gtk", fake), mock.patch.object(reports_page.GLib, "timeout_add_seconds") as timer:
            page.build_reports_page()
        page.show_reports_tab.assert_not_called()
        timer.assert_not_called()
        self.assertTrue(page.reports_tab_buttons["log"].get_active())
        page.reports_tab_buttons["library"].set_active(True)
        page.show_reports_tab.assert_called_once_with("library")


class RoundSixTest(TempCase):
    def test_lone_surrogates_never_stop_an_import(self):
        root = self.tmp / "claude/projects/-w-app"
        root.mkdir(parents=True)
        (root / "s.jsonl").write_text(
            '{"type":"user","timestamp":"2026-11-20T10:00:00Z","cwd":"/w/app","message":{"role":"user",'
            '"content":"fix emoji \\ud83d header"}}\n'
            '{"type":"assistant","timestamp":"2026-11-20T10:05:00Z","message":{"role":"assistant","content":"ok \\udc00"}}\n')
        source = {"source": "agent:claude", "name": "Claude Code", "supported": True, "form": "claude-files",
                  "root": str(self.tmp / "claude/projects")}
        result = history_import.HistoryImporter(self.log, [source], now=at("2026-11-20T18:00:00")).run()
        self.assertEqual((result["sources"][0]["added"], result["sources"][0]["status"]), (1, "ok"))
        row = self.rows("2026-11-20")[0]
        self.assertEqual(row["topic"], "fix emoji header")
        (self.log.directory / "2026-11-20.jsonl").read_bytes().decode("utf-8")
        tail = (root / "s.jsonl").read_text()
        activity = live.jsonl_session_activity(tail, "s")
        json.dumps(activity, ensure_ascii=False).encode("utf-8")
        self.assertEqual(activity_log.plain("a\udcff"), "a\ufffd")
        self.assertEqual(reports_ctl.report_field("t\ud800x", 10), "t x")

    def test_a_crafted_library_is_bounded(self):
        library = reports_ctl.ReportsLibrary(self.tmp / "reports")
        library.save(DRAFT, "ok")
        db = self.tmp / "reports/reports.db"
        con = sqlite3.connect(db)
        con.execute("UPDATE reports SET title=? WHERE id=1", ("x" * 300,))
        con.commit()
        con.close()
        rows = library.list()["reports"]
        self.assertTrue(rows[0].get("damaged"))             # longer than a title can be
        with self.assertRaises(reports_ctl.ReportError) as caught:
            library.get(1)
        self.assertEqual(caught.exception.code, "report_damaged")
        con = sqlite3.connect(db)
        con.execute("ALTER TABLE reports RENAME TO real_reports")
        con.execute("CREATE VIEW reports AS SELECT id, zeroblob(2000000) AS title, note, saved_at, from_day, "
                    "through_day, filters_json, helper, generated_at, entry_count, total_entries, capped, topic_names, "
                    "topics_json, text FROM real_reports")
        con.commit()
        con.close()
        with self.assertRaises(reports_ctl.ReportError):
            library.list()                                  # the value limit, never a 2 MB title
        con = sqlite3.connect(db)
        con.execute("DROP VIEW reports")
        con.execute("CREATE VIEW reports AS WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n) "
                    "SELECT i AS id, '' AS title, '' AS note, '' AS saved_at, '' AS from_day, '' AS through_day, "
                    "'{}' AS filters_json, '' AS helper, '' AS generated_at, 0 AS entry_count, 0 AS total_entries, "
                    "0 AS capped, '' AS topic_names, '[]' AS topics_json, '' AS text FROM n WHERE i < 0")
        con.commit()
        con.close()
        started = time.monotonic()
        with mock.patch.object(reports_ctl, "QUERY_SECONDS", 0.3), self.assertRaises(reports_ctl.ReportError):
            library.list()                                  # a query that never ends is stopped
        self.assertLess(time.monotonic() - started, 5)


class SchemaTest(unittest.TestCase):
    def test_defaults(self):
        import settings_schema
        self.assertEqual(settings_schema.value({}, "ai_activity_log_days"), 7)
        self.assertEqual(settings_schema.value({"ai_activity_log_days": 2}, "ai_activity_log_days"), 2)
        self.assertIs(settings_schema.value({}, "ai_activity_log"), False)
        self.assertIs(settings_schema.value({}, "ai_activity_import"), True)
        self.assertIs(settings_schema.value({}, "ai_activity_log_online"), False)
        self.assertEqual(settings_schema.value({"ai_activity_log_days": True}, "ai_activity_log_days"), 7)


if __name__ == "__main__":
    unittest.main()
