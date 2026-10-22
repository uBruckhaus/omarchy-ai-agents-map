import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import subagents  # noqa: E402

NOW = 1_800_000_000.0


class ClaudeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.proc, self.projects = root / "proc", root / "projects"
        self.work = root / "work" / "my.project"
        for folder in (self.proc, self.projects, self.work):
            folder.mkdir(parents=True)
        (self.proc / "100").mkdir()
        os.symlink(self.work, self.proc / "100" / "cwd")
        self.project = self.projects / subagents.project_name(str(self.work))

    def subagent(self, session: str, sid: str, meta: dict, written: float):
        folder = self.project / session / "subagents"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{sid}.meta.json").write_text(json.dumps(meta))
        transcript = folder / f"{sid}.jsonl"
        transcript.write_text("{}\n")
        os.utime(transcript, (written, written))
        return folder

    def find(self, pids=(100,)):
        return subagents.claude_projects("claude", pids, NOW, self.projects, str(self.proc))

    def test_project_name_like_claude_code(self):
        self.assertEqual(subagents.project_name("/home/uwe/.cache/x-y"), "-home-uwe--cache-x-y")

    def test_working_finished_and_old_ones(self):
        self.subagent("s1", "agent-a1", {"agentType": "Explore", "description": "Find the tests"}, NOW - 5)
        self.subagent("s1", "agent-a2", {"agentType": "general-purpose", "description": "Fix it"}, NOW - 600)
        self.subagent("s2", "agent-a3", {"description": "Long ago"}, NOW - subagents.RECENT - 10)
        found = self.find()
        self.assertEqual([(s.kind, s.task, s.running) for s in found],
                         [("Explore", "Find the tests", True), ("general-purpose", "Fix it", False)])
        self.assertEqual(subagents.working(found), 1)
        self.assertEqual(found[0].key, "claude:s1:agent-a1")

    def test_only_the_running_instances_project(self):
        self.subagent("s1", "agent-a1", {"description": "Mine"}, NOW - 5)
        self.assertEqual(self.find(pids=()), [])
        self.assertEqual(self.find(pids=(999,)), [])                 # no such process

    def test_text_is_one_bounded_plain_line(self):
        self.subagent("s1", "agent-a1", {"agentType": "x" * 500, "description": "two\nlines\x1b[31m " + "y" * 900},
                      NOW - 5)
        sub = self.find()[0]
        self.assertTrue(sub.task.startswith("two lines [31m "))
        self.assertLessEqual(len(sub.task), subagents.MAX_TASK)
        self.assertLessEqual(len(sub.kind), subagents.MAX_KIND)

    def test_a_handed_back_one_is_done_at_once(self):
        folder = self.subagent("s1", "agent-a1", {"description": "Answered"}, NOW - 5)
        call = {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": subagents.HANDBACK}]}}
        result = {"type": "user", "message": {"content": [{"type": "tool_result", "content": "ok"}]}}
        (folder / "agent-a1.jsonl").write_text(json.dumps(call) + "\n" + json.dumps(result) + "\n")
        os.utime(folder / "agent-a1.jsonl", (NOW - 5, NOW - 5))
        self.assertEqual([(s.task, s.running) for s in self.find()], [("Answered", False)])

    def test_another_accounts_process_is_not_looked_into(self):
        self.subagent("s1", "agent-a1", {"description": "Mine"}, NOW - 5)
        real_stat = os.stat

        def stat(path, *args, **kwargs):
            info = real_stat(path, *args, **kwargs)
            if str(path) == f"{self.proc}/100":
                return os.stat_result((info.st_mode, 0, 0, 0, os.getuid() + 1, 0, 0, 0, 0, 0))
            return info
        with mock.patch.object(subagents.os, "stat", stat):
            self.assertEqual(self.find(), [])

    def test_paths_under_home_are_shown_short(self):
        with mock.patch.object(subagents, "HOME", Path("/home/someone")):
            self.assertEqual(subagents._home("/home/someone/Projects/x"), "~/Projects/x")
            self.assertEqual(subagents._home("/home/someone"), "~")
            self.assertEqual(subagents._home("/home/someoneelse"), "/home/someoneelse")

    def test_links_and_odd_files_are_not_followed(self):
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        (outside / "agent-x.meta.json").write_text(json.dumps({"description": "secret"}))
        (self.project / "s1").mkdir(parents=True)
        os.symlink(outside, self.project / "s1" / "subagents")     # a linked subagents folder
        folder = self.subagent("s2", "agent-a1", {"description": "ok"}, NOW - 5)
        os.symlink(outside / "agent-x.meta.json", folder / "agent-b1.meta.json")   # a linked meta file
        (folder / "agent-c1.meta.json").write_text("x" * (subagents.MAX_META + 1))  # too large
        (folder / "agent-c1.jsonl").write_text("{}\n")
        os.utime(folder / "agent-c1.jsonl", (NOW - 5, NOW - 5))
        tasks = sorted(s.task for s in self.find())
        self.assertEqual(tasks, ["", "ok"])                          # the large one: listed without a task
        self.assertNotIn("secret", tasks)


class OpenCodeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "opencode.db"
        con = sqlite3.connect(self.db)
        con.execute("CREATE TABLE session_v2 (id TEXT, parent_id TEXT, title TEXT, agent TEXT, directory TEXT,"
                    " time_updated INTEGER, time_idle INTEGER)")
        ms = lambda t: int(t * 1000)
        con.executemany("INSERT INTO session_v2 VALUES (?, ?, ?, ?, ?, ?, ?)", [
            ("p1", None, "The main session", "build", "/work", ms(NOW - 1), None),
            ("c1", "p1", "Search the docs", "general", "/work", ms(NOW - 3), None),
            ("c2", "p1", "Review the diff", "review", "/work", ms(NOW - 300), ms(NOW - 299)),
            ("c3", "p1", "Too old", "general", "/work", ms(NOW - subagents.RECENT - 5), None),
        ])
        con.commit()
        con.close()

    def test_child_sessions_only(self):
        found = subagents.opencode_db("opencode", NOW, self.db)
        self.assertEqual([(s.kind, s.task, s.running) for s in found],
                         [("general", "Search the docs", True), ("review", "Review the diff", False)])

    def test_missing_or_linked_database(self):
        self.assertEqual(subagents.opencode_db("opencode", NOW, Path(self.tmp.name) / "none.db"), [])
        link = Path(self.tmp.name) / "link.db"
        os.symlink(self.db, link)
        self.assertEqual(subagents.opencode_db("opencode", NOW, link), [])


if __name__ == "__main__":
    unittest.main()


class AdapterTest(unittest.TestCase):
    """Which agent keeps its subagents where comes from its adapter, not from the code."""

    def test_claude_and_opencode_name_theirs(self):
        self.assertEqual(subagents.where("claude"), ("claude-projects", "~/.claude/projects"))
        self.assertEqual(subagents.where("opencode")[0], "opencode-db")
        self.assertEqual(subagents.where("no-such-agent"), ())
        self.assertEqual(subagents.of("no-such-agent", (1,)), [])

    @staticmethod
    def sub(name: str, running: bool, age: float) -> subagents.SubAgent:
        return subagents.SubAgent("agent", name, "general-purpose", name, "~", running, age)

    def test_of_reads_with_the_named_form_at_the_named_place(self):
        found = [self.sub("one", True, 1)]
        for form, reader in (("claude-projects", "claude_projects"), ("opencode-db", "opencode_db")):
            with mock.patch.object(subagents, "where", return_value=(form, "~/x")), \
                    mock.patch.object(subagents, reader, return_value=found) as called:
                self.assertEqual(subagents.of("agent", (7,), NOW, recent=600), found)
                self.assertEqual(called.call_args.args[0], "agent")
                self.assertIn(Path(os.path.expanduser("~/x")), called.call_args.args)
                self.assertEqual(called.call_args.kwargs["recent"], 600)

    def test_every_working_one_and_the_newest_finished_ones(self):
        subs = [self.sub(f"w{i}", True, i) for i in range(3)] + [self.sub(f"d{i}", False, 10 + i) for i in range(12)]
        with mock.patch.object(subagents, "where", return_value=("claude-projects", "~/x")), \
                mock.patch.object(subagents, "claude_projects", return_value=subs):
            kept = subagents.of("agent", (7,), NOW, kept=10)
        self.assertEqual([s.key for s in kept], [f"w{i}" for i in range(3)] + [f"d{i}" for i in range(10)])
        with mock.patch.object(subagents, "where", return_value=("claude-projects", "~/x")), \
                mock.patch.object(subagents, "claude_projects", return_value=subs):
            self.assertEqual(len(subagents.of("agent", (7,), NOW, kept=5)), 3 + 5)


class UsingTest(unittest.TestCase):
    """An agent started by another one (through a shell or a script) is used by it."""

    PARENTS = {300: 200, 200: 100, 100: 1,        # pi 100 → bash 200 → claude 300
               500: 400, 400: 1,                  # codex 500 started by its own shell 400
               700: 600, 600: 300,                # opencode 700 ← sh 600 ← claude 300
               900: 100}                          # another pi 900 directly under pi 100

    def test_who_uses_whom(self):
        pids = {"pi": [100, 900], "claude": [300], "codex": [500], "opencode": [700]}
        found = subagents.agents_using_agents(pids, lambda p: self.PARENTS.get(p, 0))
        self.assertEqual(found, {("pi", "claude"), ("claude", "opencode")})

    def test_a_loop_or_a_deep_chain_ends(self):
        parents = {10: 11, 11: 10}                # a loop (PID reuse while reading)
        self.assertEqual(subagents.agents_using_agents({"a": [10]}, parents.get), set())
        chain = {n: n + 1 for n in range(1000, 1100)}
        found = subagents.agents_using_agents({"a": [1000], "b": [1050]}, chain.get)
        self.assertEqual(found, set())             # b is further up than MAX_DEPTH
