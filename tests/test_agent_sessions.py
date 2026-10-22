import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import agent_sessions as a  # noqa: E402

NOW = time.time()

try:
    from compression import zstd
except ImportError:          # before Python 3.14
    zstd = None


def lines(*entries) -> str:
    return "".join(json.dumps(e) + "\n" for e in entries)


class ReadersTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        a._KEPT.clear()

    def put(self, rel: str, data):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data if isinstance(data, bytes) else data.encode())
        return path

    def test_codex_totals_with_cached_input_taken_out(self):
        self.put("2026/10/22/rollout-x.jsonl", lines(
            {"timestamp": "2026-10-22T10:00:00Z", "type": "session_meta", "payload": {"cwd": "/home/u/p"}},
            {"timestamp": "2026-10-22T10:00:01Z", "type": "turn_context", "payload": {"model": "gpt-6-luna"}},
            {"timestamp": "2026-10-22T10:00:05Z", "type": "event_msg", "payload": {"type": "token_count", "info": {
                "total_token_usage": {"input_tokens": 100, "cached_input_tokens": 60, "output_tokens": 7,
                                      "cache_write_input_tokens": 3}}}},
            {"timestamp": "2026-10-22T10:00:09Z", "type": "event_msg", "payload": {"type": "token_count", "info": {
                "total_token_usage": {"input_tokens": 1000, "cached_input_tokens": 600, "output_tokens": 70,
                                      "cache_write_input_tokens": 30}}}}))
        (session,) = a.collect("codex-files", self.root, NOW).values()
        self.assertEqual(session.tokens, {"gpt-6-luna": [400, 70, 600, 30]})    # the last totals only
        self.assertEqual((session.cwd, session.replies), ("/home/u/p", 2))
        self.assertLess(session.first, session.last)

    def test_pi_each_reply_under_its_model(self):
        reply = lambda model, n: {"type": "message", "timestamp": "2026-10-22T10:00:00Z", "message": {
            "role": "assistant", "model": model, "usage": {"input": n, "output": 2, "cacheRead": 3, "cacheWrite": 4},
            "content": [{"type": "text", "text": "never read"}]}}
        self.put("--p--/s.jsonl", lines({"type": "session", "cwd": "/w"}, reply("claude-opus-5-5", 10),
                                        reply("claude-opus-5-5", 5), reply("qwen-local", 1),
                                        {"type": "message", "message": {"role": "user", "content": "hi"}}))
        (session,) = a.collect("pi-files", self.root, NOW).values()
        self.assertEqual(session.tokens, {"claude-opus-5-5": [15, 4, 6, 8], "qwen-local": [1, 2, 3, 4]})
        self.assertEqual(session.cwd, "/w")

    @unittest.skipIf(zstd is None, "needs Python 3.14's compression.zstd")
    def test_dsh_frame_after_frame(self):
        first = lines({"type": "session", "time": 1792000000000, "data": {"cwd": "/w"}})
        reply = lines({"type": "assistant/message", "time": 1792000005000, "data": {
            "model": "deepseek-v4-pro", "usage": {"inputTokens": 10, "outputTokens": 2, "cacheReadTokens": 30}}})
        self.put("--w--/session-1/session.v4.jsonl.zstd", zstd.compress(first.encode()) + zstd.compress(reply.encode()))
        (session,) = a.collect("dsh-files", self.root, NOW).values()
        self.assertEqual(session.tokens, {"deepseek-v4-pro": [10, 2, 30, 0]})
        self.assertEqual(session.last, 1792000005.0)

    @unittest.skipIf(zstd is None, "needs Python 3.14's compression.zstd")
    def test_dsh_too_large_once_unpacked(self):
        bomb = zstd.compress(b" " * (2 * 1024 * 1024))
        with mock.patch.object(a, "MAX_BYTES", 1024 * 1024):
            self.assertEqual(a._unzstd(bomb), b"")
        self.assertEqual(a._unzstd(b"not zstd"), b"")

    def test_links_old_and_large_files_left_out(self):
        outside = self.put("../outside.jsonl", lines({"type": "message", "message": {
            "role": "assistant", "model": "m", "usage": {"input": 1}}}))
        (self.root / "--l--").mkdir()
        os.symlink(outside, self.root / "--l--" / "link.jsonl")
        old = self.put("--o--/old.jsonl", lines({"type": "message", "message": {
            "role": "assistant", "model": "m", "usage": {"input": 1}}}))
        os.utime(old, (NOW - (a.DAYS + 1) * 86400,) * 2)
        self.put("--b--/big.jsonl", "x" * 2048)
        with mock.patch.object(a, "MAX_BYTES", 1024):
            self.assertEqual(a.collect("pi-files", self.root, NOW), {})

    def test_unchanged_file_not_read_again(self):
        path = self.put("--p--/s.jsonl", lines({"type": "message", "timestamp": "2026-10-22T10:00:00Z", "message": {
            "role": "assistant", "model": "m", "usage": {"input": 1}}}))
        a.collect("pi-files", self.root, NOW)
        with mock.patch.object(a, "_read", side_effect=AssertionError("read again")):
            a.collect("pi-files", self.root, NOW)
        path.write_text(path.read_text() * 2)
        (session,) = a.collect("pi-files", self.root, NOW).values()
        self.assertEqual(session.tokens["m"][0], 2)


if __name__ == "__main__":
    unittest.main()


class OmpTest(unittest.TestCase):
    """Oh My Pi's client_usage rows, read-only; a linked database is not opened."""

    def setUp(self):
        import sqlite3
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "agent.db"
        con = sqlite3.connect(self.db)
        con.execute("CREATE TABLE client_usage (id INTEGER PRIMARY KEY, recorded_at, install_id, app, provider, model,"
                    " requests, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, cost_usd)")
        con.executemany("INSERT INTO client_usage VALUES (?, ?, 'i', 'omp', 'anthropic', ?, ?, ?, ?, ?, ?, 0)", [
            (1, int((NOW - 3600) * 1000), "claude-opus-5-5", 2, 100, 20, 300, 4),
            (2, "2026-10-22T10:00:00Z", "gpt-6-luna", 1, 10, 1, 0, 0),
            (3, int((NOW - (a.DAYS + 2) * 86400) * 1000), "claude-opus-5-5", 1, 9, 9, 9, 9)])
        con.commit()
        con.close()

    def test_rows_of_the_last_days(self):
        found = a.omp_db(self.db, NOW)
        self.assertIn("omp:1", found)
        self.assertNotIn("omp:3", found)                         # too old
        self.assertEqual(found["omp:1"].tokens, {"claude-opus-5-5": [100, 20, 300, 4]})
        self.assertEqual(found["omp:1"].replies, 2)

    def test_linked_or_missing_database(self):
        link = Path(self.tmp.name) / "link.db"
        os.symlink(self.db, link)
        self.assertEqual(a.omp_db(link, NOW), {})
        self.assertEqual(a.omp_db(Path(self.tmp.name) / "none.db", NOW), {})


class DocumentedFormatsTest(unittest.TestCase):
    """Goose, Gemini CLI, OpenClaw and Crush, from their documented formats (not installed here)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        a._KEPT.clear()

    def sqlite(self, path: Path, schema: str, rows: list, insert: str):
        import sqlite3
        path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(path)
        con.execute(schema)
        con.executemany(insert, rows)
        con.commit()
        con.close()
        return path

    def test_goose_cache_taken_out_of_input(self):
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(NOW - 600))
        db = self.sqlite(self.root / "sessions.db",
                         "CREATE TABLE sessions (id TEXT, working_dir TEXT, created_at TEXT, updated_at TEXT,"
                         " model_config_json TEXT, accumulated_input_tokens INT, accumulated_output_tokens INT,"
                         " accumulated_cache_read_tokens INT, accumulated_cache_write_tokens INT)",
                         [("s1", "/w", when, when, json.dumps({"model_name": "claude-opus-5-5"}), 1000, 50, 600, 100)],
                         "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)")
        (session,) = a.goose_db(db, NOW).values()
        self.assertEqual(session.tokens, {"claude-opus-5-5": [300, 50, 600, 100]})
        self.assertEqual(session.cwd, "/w")

    def test_goose_before_its_cache_columns(self):
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(NOW - 600))
        db = self.sqlite(self.root / "old.db",
                         "CREATE TABLE sessions (id TEXT, working_dir TEXT, created_at TEXT, updated_at TEXT,"
                         " model_config_json TEXT, accumulated_input_tokens INT, accumulated_output_tokens INT)",
                         [("s1", "/w", when, when, "{}", 10, 5)], "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?)")
        (session,) = a.goose_db(db, NOW).values()
        self.assertEqual(session.tokens, {"unknown": [10, 5, 0, 0]})

    def test_gemini_message_written_twice_counts_once(self):
        reply = {"id": "m1", "type": "gemini", "timestamp": "2026-10-22T10:00:00Z", "model": "gemini-3.1-pro",
                 "content": "never read"}
        text = lines({"sessionId": "x", "startTime": "2026-10-22T10:00:00Z"}, {"type": "user", "id": "u1"},
                     reply, {**reply, "tokens": {"input": 100, "output": 10, "cached": 40, "thoughts": 5, "tool": 2}},
                     {"$set": {"lastUpdated": "now"}})
        path = self.root / "proj" / "chats" / "session-2026-10-22T10-00-abcd1234.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text(text)
        (session,) = a.collect("gemini-files", self.root, NOW).values()
        self.assertEqual(session.tokens, {"gemini-3.1-pro": [62, 15, 40, 0]})
        self.assertEqual(session.replies, 1)

    def test_gemini_older_single_json(self):
        record = {"sessionId": "x", "messages": [{"id": "m1", "type": "gemini", "model": "gemini-2.5-pro",
                                                   "timestamp": "2026-10-22T10:00:00Z",
                                                   "tokens": {"input": 10, "output": 1, "cached": 0}}]}
        path = self.root / "proj" / "chats" / "session-old.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(record, indent=2))
        (session,) = a.collect("gemini-files", self.root, NOW).values()
        self.assertEqual(session.tokens, {"gemini-2.5-pro": [10, 1, 0, 0]})

    def test_openclaw_events_plain_and_zstd(self):
        event = lambda n, state="ok": json.dumps({"message": {"role": "assistant", "model": "claude-opus-5-5",
                                                              "timestamp": int((NOW - 60) * 1000),
                                                              "usage": {"input": n, "output": 1, "cacheRead": 2, "cacheWrite": 3}},
                                                  "usageReport": {"state": state}})
        rows = [("s1", 1, event(10), None), ("s1", 2, event(99, "unavailable"), None)]
        if zstd is not None:
            rows.append(("s1", 3, None, zstd.compress(event(5).encode())))
        self.sqlite(self.root / "main" / "agent" / "openclaw-agent.sqlite",
                    "CREATE TABLE transcript_events (session_id TEXT, seq INT, event_json TEXT, event_zstd BLOB)",
                    rows, "INSERT INTO transcript_events VALUES (?, ?, ?, ?)")
        found = a.openclaw_db(self.root, NOW)
        session = found["openclaw:s1"]
        self.assertEqual(session.tokens["claude-opus-5-5"][0], 15 if zstd is not None else 10)
        self.assertEqual(session.tokens["claude-opus-5-5"][1:], [2, 4, 6] if zstd is not None else [1, 2, 3])

    def test_crush_reports_cost_only(self):
        db = self.root / "project" / ".crush" / "crush.db"
        self.sqlite(db, "CREATE TABLE sessions (id TEXT, cost REAL, prompt_tokens INT, created_at INT, updated_at INT)",
                    [("s1", 1.25, 999, int(NOW - 100), int(NOW - 50)), ("s2", 0, 5, int(NOW - 100), int(NOW - 50))],
                    "INSERT INTO sessions VALUES (?, ?, ?, ?, ?)")
        listed = self.root / "projects.json"
        listed.write_text(json.dumps({"projects": [{"path": str(self.root / "project"), "data_dir": str(db.parent)},
                                                   {"path": "relative/not/taken"}]}))
        (session,) = a.crush_db(listed, NOW).values()
        self.assertEqual((session.cost, session.tokens), (1.25, {}))


class GooseMcpTest(unittest.TestCase):
    """Goose's extensions in config.yaml: read like any server, written in its own shape with its name."""

    def test_read_and_write(self):
        import mcp_ctl
        stdio = {"type": "stdio", "name": "fs", "cmd": "npx", "args": ["-y", "fs"], "envs": {"A": "1"}, "enabled": True}
        remote = {"type": "streamable_http", "name": "r", "uri": "https://example.com/mcp", "headers": {"X": "y"}}
        self.assertEqual(mcp_ctl.normal(stdio)["command"], "npx")
        self.assertEqual(mcp_ctl.normal(stdio)["env"], {"A": "1"})
        self.assertEqual(mcp_ctl.normal(remote)["url"], "https://example.com/mcp")
        self.assertIsNone(mcp_ctl.normal({"type": "builtin", "name": "developer"}))     # not an MCP server
        written = mcp_ctl.native("goose", mcp_ctl.normal(stdio))
        self.assertEqual((written["type"], written["cmd"], written["envs"]), ("stdio", "npx", {"A": "1"}))
        sse = mcp_ctl.native("goose", {**mcp_ctl.normal(remote), "transport": "sse"})
        self.assertEqual(sse["type"], "streamable_http")                                 # Goose has no SSE


class GooseDetailsTest(unittest.TestCase):
    """Goose's model and providers from config.yaml (active_provider, providers.<name>.model)."""

    def run_reader(self, text: str):
        import agent_details as ad
        import agent_readers
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "config.yaml"
        path.write_text(text)
        details = ad.Details()
        with mock.patch.object(ad, "_expand", return_value=path):
            agent_readers.goose(details)
        return details

    def test_current_and_older_config(self):
        details = self.run_reader("active_provider: anthropic\nproviders:\n  anthropic:\n    enabled: true\n"
                                  "    model: claude-opus-5-5\n  ollama:\n    configured: true\n    model: qwen\n"
                                  "extensions: {}\n")
        self.assertEqual(details.model, "claude-opus-5-5 (anthropic)")
        self.assertEqual({p.name: (p.active, p.local) for p in details.providers},
                         {"anthropic": (True, False), "ollama": (False, True)})
        older = self.run_reader("GOOSE_PROVIDER: openai\nGOOSE_MODEL: gpt-6-luna\n")
        self.assertEqual(older.model, "gpt-6-luna (openai)")
        self.assertEqual(self.run_reader(": not yaml [").model, "")
