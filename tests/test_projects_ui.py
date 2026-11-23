"""The Projects pages (WP6: Maprisma tasks 082 UI, 086, 088-090, 094, 095 UI, 096 UI): the words and
rules (project_texts), the page state with the race lessons of Maprisma's reviews (projects_controller,
driven by a deferred runner so reads and mutations can be answered in any order), the controller on
the real ProjectCenter in a temporary folder, and a real GTK build of the board, the Phases view and
the project page under a headless Broadway display (skipped without gtk4-broadwayd).
Headless: unset DISPLAY / WAYLAND_DISPLAY; no network."""

import copy
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import project_texts as texts  # noqa: E402
from projects_controller import ProjectsController  # noqa: E402
from project_supervisor import ProjectError  # noqa: E402


def card(pid, **extra):
    item = {"id": pid, "title": f"Project {pid}", "path": f"/home/u/work/{pid}", "status": "draft", "revision": 1,
            "importance": "medium", "phase": "planning", "run_id": None, "stage": "goal", "activity": "draft",
            "second": None, "bucket": "drafts", "approval_valid": False, "needs_reconciliation": False,
            "processes_stopping": False, "messages_queued": 0, "issues": [], "planning": [],
            "execution": [{"phase": "execution", "position": 0, "agent": "claude", "name": "Claude Code",
                           "role": "lead", "issues": []}], "active": None, "setup_agent": None,
            "setup_error": None, "last_event": None}
    item.update(extra)
    return item


RUNNING = dict(status="running", phase="running", run_id="run-1", stage="run", activity="running", bucket="running",
               active={"phase": "execution", "position": 0, "state": "active"})


class FakeCenter:
    """ProjectCenter.call over board items the test sets; refusals by operation."""

    def __init__(self, projects=()):
        self.projects = [copy.deepcopy(p) for p in projects]
        self.calls = []
        self.discovery_generation = 0
        self.fail = {}
        self.results = {}
        self.profiles = [{"id": "claude", "name": "Claude Code", "access_modes": ["read-only", "project-edit"],
                          "plan_argv": ["claude", "-p", "{prompt}"], "run_argv": ["claude", "{prompt}"],
                          "setup_argv": ["claude", "{prompt}"]}]

    def view(self, pid):
        found = next(p for p in self.projects if p["id"] == pid)
        view = copy.deepcopy(found)
        view.update(brief="Do it.", plan=view.get("plan", ""), team=[{"agent": "claude", "role": "lead"}],
                    events=[], messages=view.get("messages", []))
        return view

    def call(self, operation, params):
        self.calls.append((operation, copy.deepcopy(params)))
        if operation in self.fail:
            raise ProjectError(self.fail.pop(operation))
        if operation in self.results:
            return self.results.pop(operation)
        if operation == "list":
            return {"projects": copy.deepcopy(self.projects), "summary": {"total": len(self.projects)}}
        if operation == "profiles":
            return {"profiles": copy.deepcopy(self.profiles), "runtimes": []}
        if operation == "get":
            if not any(p["id"] == params["id"] for p in self.projects):
                raise ProjectError("project_unknown")
            return {"project": self.view(params["id"])}
        if operation == "move":
            ids = [p["id"] for p in self.projects]
            ids.remove(params["id"])
            ids.insert(len(ids) if params["before"] is None else ids.index(params["before"]), params["id"])
            self.projects = [next(p for p in self.projects if p["id"] == i) for i in ids]
            return {"order": ids}
        if operation == "set_importance":
            item = next(p for p in self.projects if p["id"] == params["id"])
            item["importance"] = params["importance"]
            return {"project": self.view(params["id"])}
        if operation == "delete":
            self.projects = [p for p in self.projects if p["id"] != params["id"]]
            return {"deleted": True}
        if operation == "save":
            pid = params.get("id") or f"new{len(self.projects)}"
            if not params.get("id"):
                self.projects.insert(0, card(pid, title=params["title"]))
            item = next(p for p in self.projects if p["id"] == pid)
            item.update(title=params["title"], revision=item["revision"] + (1 if params.get("id") else 0))
            return {"project": self.view(pid)}
        if operation in ("pause", "review", "run", "continue", "plan", "setup", "setup_apply", "message_drop"):
            return {"project": self.view(params["id"])}
        if operation == "message":
            return {"project": self.view(params["id"]), "message": {"id": "m1", "state": "queued"},
                    "continued": params["mode"] == "now", "reason": None}
        raise ProjectError("project_operation_unknown")


class Job:
    def __init__(self, work, done):
        self.work, self.done = work, done
        self.ran = False
        self.result = self.error = None

    def execute(self):
        if not self.ran:
            self.ran = True
            try:
                self.result = self.work()
            except Exception as error:
                self.error = error

    def deliver(self):
        self.execute()
        self.done(self.result, self.error)


class Deferred:
    """The worker threads, under the test's hand: execute() reads the center now, deliver() hands the
    answer to the GTK side (later, in any order)."""

    def __init__(self):
        self.jobs = []

    def __call__(self, work, done):
        self.jobs.append(Job(work, done))

    def pop(self, index=0):
        return self.jobs.pop(index)

    def flush(self):
        while self.jobs:
            self.jobs.pop(0).deliver()


class Questions:
    def __init__(self):
        self.asked = []
        self.answers = []
        self.held = None

    def __call__(self, kind, question, answer):
        self.asked.append((kind, question))
        if self.answers:
            answer(self.answers.pop(0))
        else:
            self.held = answer


class ControllerCase(unittest.TestCase):
    def make(self, projects=(), loaded=True):
        self.center = FakeCenter(projects)
        self.runner = Deferred()
        self.questions = Questions()
        self.notes = 0

        def notify():
            self.notes += 1
        self.ctrl = ProjectsController(self.center.call, self.runner, self.questions, notify)
        if loaded:
            self.ctrl.set_active(True)
            self.runner.flush()
        return self.ctrl

    def ops(self, name=None):
        return [c for c in self.center.calls if name is None or c[0] == name]


class TextsTest(unittest.TestCase):
    def test_activity_issue_and_failure_words(self):
        self.assertEqual(texts.activity("approved_blocked"), "Approved · blocked")
        self.assertEqual(texts.activity("unknown_code"), "Draft")
        self.assertEqual(texts.issue("runtime_stopped"), "A local runtime is stopped")
        self.assertEqual(texts.failure(ProjectError("project_folder_busy")), "Another project is working in this folder.")
        self.assertEqual(texts.failure(ProjectError("rule_refused", "Start, Agents: not allowed")),
                         "Start, Agents: not allowed")
        self.assertEqual(texts.failure(ValueError()), "The project operation failed.")
        self.assertIn("another run", texts.message_failure(ProjectError("project_changed")))

    def test_issue_links(self):
        runtime = {"code": "runtime_stopped", "fix": "map", "node": "runtime:llama"}
        self.assertEqual(texts.issue_fix(runtime), "map")
        self.assertEqual(texts.issue_fix(runtime, map_fix=False), "team")
        self.assertEqual(texts.issue_fix({"code": "empty_goal", "fix": "goal"}), "goal")
        self.assertEqual(texts.issue_fix_label("map"), "Fix on map")
        project = {"issues": [runtime], "execution": [{"issues": [runtime, {"code": "no_lead"}]}]}
        self.assertEqual([i["code"] for i in texts.collect_issues(project)], ["runtime_stopped", "no_lead"])

    def test_team_nodes_keep_the_marked_member(self):
        team = [{"agent": f"a{i}", "name": f"Agent {i}", "role": "implementer"} for i in range(6)]
        project = {"stage": "run", "execution": team, "active": {"phase": "execution", "position": 5, "state": "active"}}
        shown, more = texts.team_nodes(project, 4)
        self.assertEqual([m["agent"] for m, _s in shown], ["a0", "a1", "a2", "a5"])
        self.assertEqual(shown[-1][1], "active")
        self.assertEqual(more, 2)
        setup = {"stage": "goal", "phase": "setup", "activity": "setting_up_team", "planning": [],
                 "execution": team[:1], "setup_agent": {"agent": "claude", "name": "Claude Code"}}
        shown, _more = texts.team_nodes(setup, 3)
        self.assertEqual((shown[0][0]["role"], shown[0][1]), ("setup", "active"))
        self.assertEqual(texts.member_config({"model": "qwen"}), "qwen")
        self.assertEqual(texts.member_config({}), "Agent default")
        self.assertEqual(texts.graph_config({"runtime": "llama.cpp", "model": "qwen"}), ("llama.cpp · qwen", True))
        self.assertEqual(texts.graph_config({"model": "qwen"}), ("Agent default · qwen", False))

    def test_card_rules(self):
        self.assertFalse(texts.deletable(card("a", **RUNNING)))
        self.assertFalse(texts.deletable(card("a", processes_stopping=True)))
        self.assertFalse(texts.deletable(card("a", activity="setting_up_team")))
        self.assertTrue(texts.deletable(card("a")))
        self.assertEqual(texts.pause_label(card("a", **RUNNING)), "Pause")
        self.assertEqual(texts.pause_label(card("a", status="pausing")), "Retry pause")
        self.assertIsNone(texts.pause_label(card("a", **dict(RUNNING, processes_stopping=True))))
        self.assertIsNone(texts.pause_label(card("a")))
        self.assertEqual(texts.short_path("/home/u/work/site"), "…/work/site")

    def test_phases_and_footer(self):
        groups = texts.by_stage([card("a", stage="plan"), card("b", stage="weird"), card("c", stage="plan")])
        self.assertEqual([p["id"] for p in groups["plan"]], ["a", "c"])
        self.assertEqual([p["id"] for p in groups["goal"]], ["b"])
        paused = {"status": "paused"}
        actions = texts.footer_actions(paused, "Plan", dirty=False, running=False, approved=True, has_team=True)
        self.assertIn(("Continue project", "continue", True), actions)
        self.assertEqual(texts.primary_action(actions, dirty=False)[1], "continue")
        self.assertEqual(texts.primary_action(actions, dirty=True)[1], "continue")   # save is off: nothing changed
        new = texts.footer_actions(None, "", dirty=False, running=False, approved=False, has_team=True)
        self.assertNotIn("delete", [a[1] for a in new])
        self.assertEqual(texts.primary_action(new, dirty=False)[1], "save")
        running = texts.footer_actions({"status": "pausing"}, "Plan", False, True, True, True)
        self.assertIn(("Retry pause", "pause", True), running)
        self.assertIn(("Delete project", "delete", False), running)

    def test_messages_and_events(self):
        self.assertTrue(texts.can_message({"status": "pausing", "run_id": "r", "phase": "running"}))
        self.assertFalse(texts.can_message({"status": "planning", "run_id": "r", "phase": "setup"}))
        self.assertFalse(texts.can_message({"status": "paused", "run_id": "r"}))
        delivered = {"state": "delivered", "delivered_to": {"phase": "planning", "agent": "claude", "role": "implementer"}}
        self.assertEqual(texts.message_state(delivered, lambda a: "Claude Code"),
                         "Delivered to Claude Code · Contributor (Planning)")
        self.assertEqual(texts.messages_waiting(2), "2 messages waiting")
        waiting = {"type": "message_waiting", "text": "The agent could not continue at once (project_capacity). "
                                                      "Your message waits for the next assignment."}
        self.assertIn("Too many projects are running now.", texts.event_text(waiting))
        self.assertIn("did not start", texts.event_text({"type": "continue_failed", "text": "project_agent_unavailable"}))
        self.assertEqual(texts.not_continued("project_message_unknown", {}), texts.not_continued(None, None))
        self.assertEqual(texts.event_text({"type": "assignment", "text": "Claude Code: lead"}), "Claude Code: Lead agent")
        self.assertEqual(texts.event_text({"type": "assignment_done", "text": "reviewer ended successfully."}),
                         "Reviewer ended successfully.")
        self.assertEqual(texts.event_text({"type": "message_delivered",
                                           "text": "2 messages were given to Pi: implementer."}),
                         "2 messages were given to Pi: Implementer.")


class BoardTest(ControllerCase):
    def test_first_read_takes_profiles_counts_and_filters(self):
        ctrl = self.make([card("a", **RUNNING), card("b"), card("c", bucket="attention", importance="high")])
        self.assertTrue(ctrl.loaded)
        self.assertEqual([c[0] for c in self.ops()], ["list", "profiles"])
        counts = ctrl.counts()
        self.assertEqual((counts["running"], counts["drafts"], counts["attention"], counts["all"]), (1, 1, 1, 3))
        ctrl.set_filter("drafts")
        self.assertEqual([p["id"] for p in ctrl.shown()], ["b"])
        ctrl.set_filter("drafts")                      # chosen again clears
        self.assertIsNone(ctrl.filter)
        ctrl.set_importance_filter("high")
        ctrl.set_agent("codex", "Codex")
        self.assertEqual(ctrl.shown(), [])
        self.assertEqual(ctrl.importance_counts()["high"], 0)
        ctrl.set_filter(None)                          # All clears the filter, the importance and the agent
        self.assertEqual((ctrl.filter, ctrl.importance, ctrl.agent), (None, None, None))
        self.assertEqual(ctrl.importance_counts(), {"high": 1, "medium": 2, "low": 0})

    def test_unavailable_never_counts_zero(self):
        ctrl = self.make(loaded=False)
        self.center.fail["list"] = "project_center_busy"
        ctrl.set_active(True)
        self.runner.flush()
        self.assertTrue(ctrl.unavailable)
        self.assertIsNone(ctrl.counts())
        self.assertFalse(ctrl.loaded)
        ctrl.refresh()
        self.runner.flush()
        self.assertTrue(ctrl.loaded)
        self.assertFalse(ctrl.unavailable)

    def test_one_read_at_a_time_and_none_lost(self):
        ctrl = self.make([card("a")])
        seen = []
        ctrl.refresh(then=lambda: seen.append("first"))
        ctrl.refresh(then=lambda: seen.append("second"))
        ctrl.refresh(profiles=True)
        self.assertEqual(len(self.runner.jobs), 1)
        self.runner.pop().deliver()
        self.assertEqual(seen, ["first"])              # its own read ended; the queued read still runs
        self.assertEqual(len(self.runner.jobs), 1)
        self.runner.pop().deliver()
        self.assertEqual(seen, ["first", "second"])
        self.assertEqual([c[0] for c in self.ops()][-2:], ["list", "profiles"])

    def test_a_read_from_before_a_move_does_not_undo_it(self):
        ctrl = self.make([card("a"), card("b"), card("c")])
        ctrl.refresh()
        read = self.runner.pop()
        read.execute()                                 # the list is read before the move
        ctrl.move_from_board("c", None, False)         # Move to top
        self.assertEqual([p["id"] for p in ctrl.projects], ["c", "a", "b"])
        self.assertEqual((ctrl.reveal, ctrl.moving), ("c", True))
        read.deliver()                                 # older: the cards stay as moved
        self.assertEqual([p["id"] for p in ctrl.projects], ["c", "a", "b"])
        self.runner.flush()
        self.assertEqual(self.ops("move")[-1][1], {"id": "c", "before": "a"})
        self.assertFalse(ctrl.moving)
        ctrl.refresh()
        self.runner.flush()
        self.assertEqual([p["id"] for p in ctrl.projects], ["c", "a", "b"])

    def test_a_refused_move_goes_back_and_keeps_its_reason(self):
        ctrl = self.make([card("a"), card("b")])
        self.center.fail["move"] = "project_unknown"
        ctrl.move_from_board("a", "b", True)
        self.assertEqual([p["id"] for p in ctrl.projects], ["b", "a"])
        self.runner.pop().deliver()                    # refused
        self.assertEqual([p["id"] for p in ctrl.projects], ["a", "b"])
        self.assertEqual(ctrl.error, "The project could not be found.")
        self.runner.flush()                            # the read it asked for keeps the reason
        self.assertEqual(ctrl.error, "The project could not be found.")
        self.assertEqual(ctrl.keep_error, 0)
        ctrl.refresh()
        self.runner.flush()
        self.assertIsNone(ctrl.error)

    def test_moves_respect_the_filter(self):
        ctrl = self.make([card("a"), card("b", bucket="running"), card("c"), card("d")])
        ctrl.set_filter("drafts")                      # shows a, c, d
        options = dict((k, place) for k, _label, place in ctrl.move_options("a"))
        self.assertIsNone(options["up"])
        self.assertIsNone(options["top"])
        self.assertEqual(options["down"], ("c", True))
        ctrl.move_by("a", "down")                      # after c: before the next shown card, d
        self.runner.flush()
        self.assertEqual(self.ops("move")[-1][1], {"id": "a", "before": "d"})
        ctrl.move_by("a", "end")
        self.runner.flush()
        self.assertEqual(self.ops("move")[-1][1], {"id": "a", "before": None})
        self.assertFalse(ctrl.move_by("a", "end"))     # already last

    def test_marks_stay_against_older_reads_and_go_back_when_refused(self):
        ctrl = self.make([card("a")])
        ctrl.refresh()
        read = self.runner.pop()
        read.execute()
        ctrl.set_importance("a", "high")
        self.assertEqual(ctrl.projects[0]["importance"], "high")
        read.deliver()                                  # read before the mark: it keeps the mark
        self.assertEqual(ctrl.projects[0]["importance"], "high")
        self.runner.flush()
        self.center.fail["set_importance"] = "project_store_full"
        ctrl.set_importance("a", "low")
        self.runner.pop().deliver()
        self.assertEqual(ctrl.projects[0]["importance"], "high")
        self.assertIn("store is unavailable", ctrl.error)

    def test_pause_once_with_the_cards_run_and_a_refusal_stays(self):
        ctrl = self.make([card("a", **RUNNING)])
        ctrl.pause_from_board(ctrl.projects[0])
        ctrl.pause_from_board(ctrl.projects[0])         # one click at a time
        self.assertEqual(len(self.runner.jobs), 1)
        self.assertTrue(ctrl.card_busy("a"))
        self.center.fail["pause"] = "project_changed"
        self.runner.pop().deliver()
        self.assertTrue(ctrl.card_busy("a"))            # off until the board shows the new state
        self.assertEqual(self.ops("pause")[0][1], {"id": "a", "run_id": "run-1"})
        self.runner.flush()
        self.assertFalse(ctrl.card_busy("a"))
        self.assertEqual(ctrl.error, "This project changed. Reload it before saving.")

    def test_delete_asks_and_keeps_folder_wording(self):
        ctrl = self.make([card("a"), card("b")])
        self.questions.answers = [False]
        ctrl.delete_from_board(ctrl.projects[0])
        self.assertEqual(self.questions.asked[-1], ("confirm", texts.DELETE_QUESTION))
        self.assertEqual(self.ops("delete"), [])
        self.assertFalse(ctrl.busy)
        ctrl.message_drafts["a"] = "hello"
        ctrl.setup_drafts["a:run"] = "team"
        self.questions.answers = [True]
        ctrl.delete_from_board(ctrl.projects[0])
        self.runner.flush()
        self.assertEqual(self.ops("delete")[0][1], {"id": "a", "revision": 1})
        self.assertEqual([p["id"] for p in ctrl.projects], ["b"])
        self.assertNotIn("a", ctrl.message_drafts)
        self.assertNotIn("a:run", ctrl.setup_drafts)
        self.center.fail["delete"] = "project_still_pausing"
        self.questions.answers = [True]
        ctrl.delete_from_board(ctrl.projects[0])
        self.runner.flush()
        self.assertEqual(ctrl.error, "Some agent processes are still stopping. Try again shortly.")
        self.assertFalse(ctrl.busy)

    def test_a_failed_read_keeps_a_refused_actions_reason(self):
        ctrl = self.make([card("a", **RUNNING)])
        self.center.fail["pause"] = "project_changed"
        ctrl.pause_from_board(ctrl.projects[0])
        self.runner.pop().deliver()                    # refused
        self.center.fail["list"] = "project_center_busy"
        self.runner.pop().deliver()                    # its read fails too
        self.assertTrue(ctrl.unavailable)
        self.assertEqual(ctrl.error, "This project changed. Reload it before saving.")

    def test_a_waiter_needs_one_read_after_it_at_most(self):
        ctrl = self.make([card("a")])
        ctrl.refresh()
        seen = []
        ctrl.refresh(then=lambda: seen.append("late"))   # asked while one reads
        self.runner.pop().deliver()
        ctrl.refresh()                                   # more asks during the follow-up
        self.runner.pop().deliver()                      # the follow-up ends: the waiter is told
        self.assertEqual(seen, ["late"])
        self.assertEqual(len(self.runner.jobs), 1)       # the newest ask still runs
        self.runner.flush()

    def test_answers_after_the_page_is_gone_change_nothing(self):
        ctrl = self.make([card("a")])
        ctrl.refresh()
        job = self.runner.pop()
        self.center.projects = []
        ctrl.dispose()
        job.deliver()
        self.assertEqual([p["id"] for p in ctrl.projects], ["a"])
        ctrl.refresh()
        self.assertEqual(self.runner.jobs, [])

    def test_quiet_poll_and_fast_poll(self):
        ctrl = self.make([card("a")])
        for _ in range(7):
            ctrl.tick()
        self.assertEqual(self.runner.jobs, [])
        ctrl.tick()
        self.assertEqual(len(self.runner.jobs), 1)
        self.runner.flush()
        self.center.projects = [card("a", **RUNNING)]
        ctrl.refresh()
        self.runner.flush()
        ctrl.tick()
        self.assertEqual(len(self.runner.jobs), 1)      # every tick while something runs
        self.runner.flush()
        ctrl.set_active(False)
        ctrl.tick()
        self.assertEqual(self.runner.jobs, [])


class ProjectPageTest(ControllerCase):
    def open(self, ctrl, pid, tab=0):
        ctrl.open_card(next(p for p in ctrl.projects if p["id"] == pid), tab=tab)
        self.runner.flush()

    def test_open_and_back_reveal_the_card(self):
        ctrl = self.make([card("a"), card("b", bucket="ready")])
        ctrl.set_filter("ready")
        self.open(ctrl, "b", tab=texts.TAB_PLAN)
        self.assertTrue(ctrl.open)
        self.assertEqual((ctrl.tab, ctrl.project["id"]), (texts.TAB_PLAN, "b"))
        self.center.projects[1]["bucket"] = "drafts"     # left the filter meanwhile
        ctrl.refresh()
        self.runner.flush()
        self.assertEqual(self.ops("get")[-1][1], {"id": "b"})
        ctrl.back_to_board()
        self.assertFalse(ctrl.open)
        self.assertIsNone(ctrl.filter)                  # shown again
        self.assertEqual(ctrl.reveal, "b")
        seq = ctrl.reveal_seq
        self.assertFalse(ctrl.revealed(seq - 1))
        self.assertTrue(ctrl.revealed(seq))

    def test_unsaved_changes_ask_save_discard_cancel(self):
        ctrl = self.make([card("a")])
        self.open(ctrl, "a")
        ctrl.set_text("title", "Project a")              # the same text: no change
        self.assertFalse(ctrl.dirty)
        ctrl.set_text("title", "Renamed")
        self.assertTrue(ctrl.dirty)
        self.questions.answers = ["cancel"]
        ctrl.back_to_board()
        self.assertTrue(ctrl.open)
        self.assertEqual(ctrl.texts["title"], "Renamed")
        self.questions.answers = ["discard"]
        ctrl.back_to_board()
        self.assertFalse(ctrl.open)
        self.assertEqual(ctrl.texts["title"], "Project a")
        self.open(ctrl, "a")
        ctrl.set_text("title", "Saved name")
        self.questions.answers = ["save"]
        ctrl.back_to_board()
        self.runner.flush()
        self.assertFalse(ctrl.open)
        self.assertEqual(self.ops("save")[-1][1]["title"], "Saved name")

    def test_a_plan_typed_into_a_changed_draft_is_drawn(self):
        ctrl = self.make([card("a")])
        self.open(ctrl, "a")
        ctrl.set_text("title", "Changed")
        before = self.notes
        ctrl.set_text("plan", "1. Do it.")
        self.assertGreater(self.notes, before)          # Approve plan follows at once
        before = self.notes
        ctrl.set_text("plan", "1. Do it now.")
        self.assertEqual(self.notes, before)            # no redraw per keystroke otherwise

    def test_busy_page_cannot_be_left_and_one_action_at_a_time(self):
        ctrl = self.make([card("a", status="ready", approval_valid=True)])
        self.open(ctrl, "a")
        ctrl.perform("plan")
        self.assertTrue(ctrl.busy)
        ctrl.perform("run")
        self.assertEqual(len(self.runner.jobs), 1)
        left = []
        ctrl.before_leave(left.append)
        self.assertEqual(left, [False])
        self.runner.flush()
        self.assertFalse(ctrl.busy)
        self.assertEqual(ctrl.tab, texts.TAB_PLAN)

    def test_new_project_saves_first_then_comes_on_top(self):
        ctrl = self.make([card("a")])
        ctrl.open_card(None)
        self.assertTrue(ctrl.open)
        self.assertIsNone(ctrl.project)
        self.assertEqual(ctrl.run_team[0]["role"], "lead")
        ctrl.perform("save")
        self.assertEqual(ctrl.error, "Enter a title, goal and existing folder.")
        for field, value in (("title", "New one"), ("brief", "Goal"), ("path", "/tmp")):
            ctrl.set_text(field, value)
        ctrl.perform("plan")                            # saves first, then plans
        self.runner.flush()
        names = [c[0] for c in self.ops()]
        self.assertLess(names.index("save"), names.index("plan"))
        self.assertEqual(self.ops("plan")[0][1], {"id": "new1", "revision": 1})
        self.assertEqual(ctrl.projects[0]["title"], "New one")

    def test_continue_and_reconciled_run_ask_and_acknowledge(self):
        ctrl = self.make([card("a", status="paused", needs_reconciliation=True)])
        self.open(ctrl, "a")
        self.questions.answers = [False]
        ctrl.perform("continue")
        self.assertEqual(self.questions.asked[-1], ("confirm", texts.CONTINUE_QUESTION))
        self.assertEqual(self.ops("continue"), [])
        self.questions.answers = [True]
        ctrl.perform("continue")
        self.runner.flush()
        self.assertEqual(self.ops("continue")[0][1], {"id": "a", "revision": 1, "acknowledge_interrupted": True})
        self.center.projects[0].update(status="ready")
        ctrl.refresh()
        self.runner.flush()
        self.questions.answers = [True]
        ctrl.perform("run")
        self.runner.flush()
        self.assertTrue(self.ops("run")[0][1]["acknowledge_interrupted"])
        self.assertEqual(ctrl.tab, texts.TAB_ACTIVITY)

    def test_delete_from_the_page_goes_back_to_the_board(self):
        ctrl = self.make([card("a"), card("b")])
        self.open(ctrl, "a")
        self.questions.answers = [True]
        ctrl.perform("delete")
        self.runner.flush()
        self.assertFalse(ctrl.open)
        self.assertEqual(ctrl.reveal, "b")
        self.assertEqual(self.ops("save"), [])          # the draft is never saved before deleting

    def test_a_late_read_of_a_deleted_project_shows_nothing(self):
        ctrl = self.make([card("a"), card("b")])
        self.open(ctrl, "a")
        ctrl.refresh()
        read = self.runner.pop()
        read.execute()                                  # it still sees the project
        self.questions.answers = [True]
        ctrl.perform("delete")
        self.runner.flush()
        read.deliver()
        self.runner.flush()
        self.assertFalse(ctrl.open)
        self.assertIsNone(ctrl.project)

    def test_issue_links(self):
        ctrl = self.make([card("a", issues=[{"code": "runtime_stopped", "fix": "map", "node": "runtime:x"}])])
        went = []
        ctrl.fix_issue(ctrl.projects[0], ctrl.projects[0]["issues"][0], went.append)
        self.assertEqual(went, ["runtime:x"])
        ctrl.fix_issue(ctrl.projects[0], ctrl.projects[0]["issues"][0], None)   # no map: Go to Team
        self.runner.flush()
        self.assertEqual((ctrl.open, ctrl.tab), (True, texts.TAB_TEAM))
        ctrl.fix_issue(None, {"code": "empty_goal", "fix": "goal"}, None)
        self.assertEqual(ctrl.tab, texts.TAB_GOAL)

    def test_team_edits_and_planning_team(self):
        ctrl = self.make([card("a")])
        self.open(ctrl, "a")
        version = ctrl.team_version
        ctrl.set_member(0, "instructions", "Be brief")
        self.assertEqual(ctrl.team_version, version)    # typing does not rebuild the form
        self.assertTrue(ctrl.dirty)
        ctrl.add_member()
        self.assertEqual(ctrl.run_team[-1]["role"], "reviewer")
        self.assertEqual(ctrl.run_team[-1]["access"], "read-only")
        ctrl.change_phase("plan")
        self.assertTrue(ctrl.team_locked)               # follows execution until customized
        ctrl.toggle_plan_team()
        self.assertEqual([m["access"] for m in ctrl.plan_team], ["read-only", "read-only"])
        ctrl.set_setup_request("Use Claude")
        ctrl.change_phase("run")
        self.assertEqual(ctrl.setup_request, "")
        ctrl.change_phase("plan")
        self.assertEqual(ctrl.setup_request, "Use Claude")   # each phase its own draft


class MessagesTest(ControllerCase):
    def running(self):
        ctrl = self.make([card("a", **RUNNING)])
        ctrl.open_card(ctrl.projects[0], tab=texts.TAB_ACTIVITY)
        self.runner.flush()
        return ctrl

    def test_send_queues_and_clears_only_the_sent_text(self):
        ctrl = self.running()
        ctrl.set_message_draft("a", "Check the tests")
        ctrl.send_message("Check the tests", now=False)
        ctrl.set_message_draft("a", "Typed meanwhile")
        self.runner.pop().deliver()
        self.assertEqual(self.ops("message")[0][1],
                         {"id": "a", "run_id": "run-1", "text": "Check the tests", "mode": "queue"})
        self.assertEqual(ctrl.message_drafts["a"], "Typed meanwhile")
        self.runner.flush()
        self.assertFalse(ctrl.busy)
        ctrl.set_message_draft("a", "Again")
        ctrl.send_message("Again", now=False)
        self.runner.flush()
        self.assertEqual(ctrl.message_drafts["a"], "")

    def test_send_now_asks_and_says_why_it_waits(self):
        ctrl = self.running()
        self.questions.answers = [False]
        ctrl.send_message("Stop and fix", now=True)
        self.assertEqual(self.questions.asked[-1], ("confirm", texts.CONTINUE_QUESTION))
        self.assertEqual(self.ops("message"), [])
        self.assertFalse(ctrl.busy)
        self.center.results["message"] = {"project": self.center.view("a"), "message": {"id": "m", "state": "queued"},
                                          "continued": False, "reason": "project_capacity"}
        self.questions.answers = [True]
        ctrl.send_message("Stop and fix", now=True)
        self.runner.flush()
        params = self.ops("message")[0][1]
        self.assertEqual((params["mode"], params["acknowledge_interrupted"]), ("now", True))
        note, error = ctrl.message_notes["a"]
        self.assertFalse(error)
        self.assertIn("Too many projects are running now.", note)
        self.assertIn("waits for the next assignment", note)

    def test_refusal_note_and_drop(self):
        ctrl = self.running()
        self.center.fail["message"] = "project_messages_full"
        ctrl.send_message("One more", now=False)
        self.runner.flush()
        self.assertTrue(ctrl.message_notes["a"][1])
        self.center.fail["message_drop"] = "project_message_pending"
        ctrl.drop_message("m1")
        self.runner.flush()
        self.assertIn("already in the prompt", ctrl.message_notes["a"][0])
        ctrl.drop_message("m1")
        self.runner.flush()
        self.assertEqual(self.ops("message_drop")[-1][1], {"id": "a", "message_id": "m1"})
        self.assertNotIn("a", ctrl.message_notes)

    def test_a_read_from_before_a_message_shows_no_older_project(self):
        ctrl = self.running()
        ctrl.refresh()
        read = self.runner.pop()
        read.execute()                                   # sees no message yet
        newer = self.center.view("a")
        newer["messages"] = [{"id": "m1", "state": "queued", "text": "Hi"}]
        self.center.results["message"] = {"project": newer, "message": newer["messages"][0], "continued": False,
                                          "reason": None}
        ctrl.send_message("Hi", now=False)
        self.runner.pop().deliver()                      # the answer is taken over
        self.assertEqual(len(ctrl.project["messages"]), 1)
        read.deliver()                                   # older: not shown
        self.assertEqual(len(ctrl.project["messages"]), 1)


class RealCenterTest(unittest.TestCase):
    """The controller on the real ProjectCenter (temporary folder, fixture profile, nothing started)."""

    def setUp(self):
        import project_center as pc
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        folder = os.path.realpath(self.tmp.name)
        self.folder = folder
        profiles = [{"id": "fixture", "name": "Fixture agent", "installed": True,
                     "plan_argv": ["/fixture/agent", "{prompt}"], "run_argv": ["/fixture/agent", "{prompt}"]}]
        self.center = pc.ProjectCenter(Path(folder) / "data", agent_profiles=lambda: profiles,
                                       runtime_profiles=lambda: [], authorize=lambda _a: None,
                                       runner=lambda *a: (_ for _ in ()).throw(ProjectError("project_agent_unavailable")),
                                       environment={"PATH": "/usr/bin"})
        self.addCleanup(self.center.detach)

        def run(work, done):
            try:
                result, error = work(), None
            except Exception as caught:
                result, error = None, caught
            done(result, error)
        self.answers = []
        self.ctrl = ProjectsController(self.center.call, run, lambda kind, q, answer: answer(self.answers.pop(0)))
        self.ctrl.set_active(True)

    def create(self, title):
        ctrl = self.ctrl
        ctrl.open_card(None)
        for field, value in (("title", title), ("brief", "Make it."), ("path", self.folder)):
            ctrl.set_text(field, value)
        ctrl.perform("save")
        ctrl.back_to_board()

    def test_create_order_mark_and_delete(self):
        ctrl = self.ctrl
        self.assertTrue(ctrl.loaded)
        self.assertEqual(ctrl.profiles[0]["id"], "fixture")
        self.create("First")
        self.create("Second")
        self.assertEqual([p["title"] for p in ctrl.projects], ["Second", "First"])   # new on top
        self.assertEqual(ctrl.projects[0]["importance"], "medium")
        self.assertEqual(ctrl.projects[0]["stage"], "goal")
        second, first = (p["id"] for p in ctrl.projects)
        ctrl.move_by(first, "up")
        self.assertEqual([p["title"] for p in ctrl.projects], ["First", "Second"])
        ctrl.set_importance(second, "high")
        ctrl.refresh()
        self.assertEqual(next(p for p in ctrl.projects if p["id"] == second)["importance"], "high")
        self.answers.append(True)
        ctrl.delete_from_board(next(p for p in ctrl.projects if p["id"] == second))
        self.assertEqual([p["title"] for p in ctrl.projects], ["First"])
        self.assertIsNone(ctrl.error)

    def test_a_refused_start_keeps_its_reason_then_approve(self):
        ctrl = self.ctrl
        self.create("Plan it")
        ctrl.open_card(ctrl.projects[0])
        ctrl.set_text("plan", "1. Do it.")
        ctrl.perform("run")                          # saves first; the core refuses an unapproved plan
        self.assertEqual(ctrl.error, "Review and approve this plan before running.")
        self.assertFalse(ctrl.busy)
        self.assertIn(ctrl.project["status"], ("draft", "awaiting_review"))
        self.answers.append(True)
        ctrl.perform("review")
        self.assertTrue(ctrl.approved)
        self.assertIsNone(ctrl.error)
        self.assertEqual(ctrl.card_of_open["activity"], "ready_to_run")

    def test_forced_refresh_reaches_the_center(self):
        calls = []
        ctrl = self.ctrl
        real = ctrl.call
        ctrl.call = lambda op, params: (calls.append((op, dict(params))), real(op, {} if op == "list" else params))[1]
        ctrl.refresh(profiles=True, force=True)
        self.assertIn(("list", {"refresh": True}), calls)
        ctrl.refresh()
        self.assertEqual(calls[-1], ("list", {}))


SMOKE = ROOT / "tests" / "projects_ui_smoke.py"


@unittest.skipUnless(shutil.which("gtk4-broadwayd"), "needs gtk4-broadwayd for a headless GTK build")
class GtkBuildTest(unittest.TestCase):
    """The real widgets under a headless Broadway display, with an isolated HOME."""

    def test_board_phases_and_project_page_build(self):
        with tempfile.TemporaryDirectory() as home:
            runtime = Path(home) / "run"
            runtime.mkdir(mode=0o700)
            env = {k: v for k, v in os.environ.items()
                   if k not in ("DISPLAY", "WAYLAND_DISPLAY", "XDG_CURRENT_DESKTOP", "HYPRLAND_INSTANCE_SIGNATURE",
                                "KDE_FULL_SESSION")}
            display = f":{40 + os.getpid() % 200}"        # its own display: parallel runs do not meet
            env.update(HOME=home, XDG_RUNTIME_DIR=str(runtime), XDG_CONFIG_HOME=f"{home}/.config",
                       XDG_STATE_HOME=f"{home}/.local/state", XDG_CACHE_HOME=f"{home}/.cache",
                       GDK_BACKEND="broadway", BROADWAY_DISPLAY=display, GSK_RENDERER="cairo")
            daemon = subprocess.Popen(["gtk4-broadwayd", display], env=env, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL)
            try:
                time.sleep(0.5)
                done = subprocess.run([sys.executable, str(SMOKE)], env=env, capture_output=True, text=True,
                                      timeout=60, cwd=str(ROOT))
            finally:
                daemon.terminate()
                daemon.wait(5)
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            self.assertIn("SMOKE OK", done.stdout)
            self.assertNotIn("Traceback", done.stderr)    # also none from a signal handler


if __name__ == "__main__":
    unittest.main()
