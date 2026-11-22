"""AI Project Center core (project_center.py, project_stage.py, project_supervisor.py): the store, the
lifecycle, the supervisor, the board projection with issue links, board order and importance, and
messages to a running project. Ported from Maprisma's ai_project_center_test.dart (tasks 082–096).
Headless: no display, no network; agents are fakes or small Python programs."""

import json
import os
import queue
import stat
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import adapter_data  # noqa: E402
import project_center as pc  # noqa: E402
import project_supervisor as ps  # noqa: E402
from project_stage import ProjectFacts, project_state  # noqa: E402
from project_supervisor import ProjectError  # noqa: E402


class FakeProcess:
    """A ProjectProcess the test drives: output lines, its exit, a stop that may be refused or slow."""

    def __init__(self):
        self.lines: queue.Queue = queue.Queue()
        self.exit = None
        self.ended = threading.Event()
        self.stopped = False
        self.refuse_stop = False
        self.stop_gate: threading.Event | None = None
        self.stop_calls = 0
        self.starts = True
        self._finished = False
        self._lock = threading.Lock()

    def output(self):
        while True:
            item = self.lines.get()
            if item is None:
                return
            yield item

    def started(self, timeout=10.0):
        return self.starts() if callable(self.starts) else self.starts

    def wait(self):
        self.ended.wait()
        return self.exit

    def add(self, text):
        self.lines.put(text)

    def finish(self, text="", code=0):
        with self._lock:
            if self._finished:
                return
            self._finished = True
        if text:
            self.lines.put(text)
        self.lines.put(None)
        self.exit = code
        self.ended.set()

    def stop(self, wait=4.0):
        self.stop_calls += 1
        if self.stop_gate is not None:
            self.stop_gate.wait(5)
        if self.refuse_stop:
            raise ProjectError("project_processes_still_running")
        self.stopped = True
        self.finish("", -15)


def wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("the operation did not reach its expected state")


class CenterCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = str((self.root / "workspace"))
        os.mkdir(self.workspace)
        self.workspace = os.path.realpath(self.workspace)
        self.profiles = [{"id": "fixture", "name": "Fixture agent", "installed": True,
                          "plan_argv": ["/fixture/agent", "read-only", "{prompt}"],
                          "run_argv": ["/fixture/agent", "workspace-write", "{prompt}"],
                          "setup_argv": ["/fixture/agent", "no-tools", "{prompt}"]}]
        self.runtimes = []
        self.processes: list[FakeProcess] = []
        self.commands: list[list[str]] = []
        self.directories: list[str] = []
        self.inputs: list = []
        self.environments: list[dict] = []
        self.launch_fail = None
        self.start_gate = None
        self.refused_agents: set[str] = set()
        self.before_launch = None
        self.withdrawn: list[FakeProcess] = []
        self.center = self.make()

    def make(self, **kwargs):
        def runner(argv, cwd, env, input_text, lock, may_launch):
            self.commands.append(argv)
            self.directories.append(cwd)
            self.inputs.append(input_text)
            self.environments.append(env)
            if self.launch_fail is not None:
                raise self.launch_fail
            process = FakeProcess()
            if self.start_gate is not None:
                process.starts = self.start_gate
            if self.before_launch is not None:
                self.before_launch()
            if not may_launch(process):          # as the supervisor: no go-ahead, no launch
                self.withdrawn.append(process)
                raise ProjectError("project_launch_withdrawn")
            self.processes.append(process)
            return process

        def authorize(agent):
            if agent in self.refused_agents:
                raise ProjectError("rule_refused", "Start, Agents: not allowed by your rules")
        options = dict(agent_profiles=lambda: self.profiles, runtime_profiles=lambda: self.runtimes,
                       runner=runner, authorize=authorize, environment={"PATH": "/usr/bin", "HOME": "/h"})
        options.update(kwargs)
        center = pc.ProjectCenter(self.root, **options)
        self.addCleanup(self._close, center)
        return center

    @staticmethod
    def _close(center):
        for process in [a.process for a in center._active.values() if a.process is not None]:
            if hasattr(process, "finish"):
                process.finish("", 0)
        try:
            center.shutdown()
        except ProjectError:
            center.detach()

    # helpers --------------------------------------------------------------------------------------

    def save(self, title="A project", path=None, team=None, plan="", **extra):
        params = {"title": title, "brief": "Build the requested change and verify it.",
                  "path": path or self.workspace, "team": team if team is not None else
                  [{"agent": "fixture", "role": "lead"}], "plan": plan, **extra}
        return self.center.call("save", params)["project"]

    def approve(self, item):
        return self.center.call("review", {"id": item["id"], "revision": item["revision"],
                                           "approved": True})["project"]

    def get(self, pid):
        return self.center.call("get", {"id": pid})["project"]

    def idle(self):
        wait_until(lambda: self.center.summary()["running"] == 0 and not self.center._active)

    def card(self, pid):
        return next(p for p in self.center.call("list", {})["projects"] if p["id"] == pid)

    def started(self, team):
        item = self.approve(self.save(plan="The plan.", team=team))
        self.center.call("run", {"id": item["id"], "revision": item["revision"]})
        wait_until(lambda: len(self.processes) == 1)
        return self.get(item["id"])

    def assertCode(self, code, function, *args):
        with self.assertRaises(ProjectError) as caught:
            function(*args)
        self.assertEqual(caught.exception.code, code)

    def add_runtime(self):
        self.profiles[0].update(provider_option="--provider", model_option="--model")
        self.runtimes.append({"key": "runtime", "name": "Example local runtime", "aliases": ["local-engine"],
                              "installed": True, "running": True, "models": ["tiny-model"],
                              "bindings": {"fixture": {"provider": "local-provider", "models": ["tiny-model"]}}})

    @staticmethod
    def types(item):
        return [event["type"] for event in item["events"]]


class StoreTest(CenterCase):
    def test_saved_privately_and_reopened(self):
        item = self.save()
        self.assertEqual(item["status"], "draft")
        self.assertEqual(item["importance"], "medium")
        store = self.root / "ai-project-center" / "projects.json"
        self.assertEqual(stat.S_IMODE(store.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(store.parent.stat().st_mode), 0o700)
        self.assertCode("project_center_busy", self.make)
        self.center.detach()
        reopened = self.make()
        self.assertEqual(reopened.call("get", {"id": item["id"]})["project"]["title"], "A project")

    def test_stale_revision_unknown_project_and_bad_fields(self):
        item = self.save()
        changed = self.center.call("save", {"id": item["id"], "revision": item["revision"], "title": "New"})["project"]
        self.assertEqual(changed["revision"], item["revision"] + 1)
        self.assertCode("project_changed", self.center.call, "save",
                        {"id": item["id"], "revision": item["revision"], "title": "Stale"})
        self.assertCode("project_unknown", self.center.call, "get", {"id": "0" * 32})
        self.assertCode("invalid", self.center.call, "save", {"title": "x", "path": self.workspace, "evil": 1})
        self.assertCode("invalid", self.center.call, "save", {"title": "x", "path": "relative/folder"})
        self.assertCode("project_folder_unavailable", self.center.call, "save",
                        {"title": "x", "path": "/no/such/folder"})
        self.assertCode("invalid", self.save, "x", None, [{"agent": "fixture", "role": "lead"},
                                                           {"agent": "fixture", "role": "lead"}])
        self.assertCode("invalid", self.save, "x", None, [{"agent": "fixture", "role": "lead", "model": "--evil"}])
        self.assertCode("project_operation_unknown", self.center.call, "explode", {})

    def test_reviewer_is_always_read_only(self):
        item = self.save(team=[{"agent": "fixture", "role": "lead"},
                               {"agent": "fixture", "role": "reviewer", "access": "project-edit"}])
        self.assertEqual([m["access"] for m in item["team"]], ["project-edit", "read-only"])

    def test_project_limit(self):
        with mock.patch.object(pc, "MAX_PROJECTS", 3):
            for n in range(3):
                self.save(title=f"P{n}")
            self.assertCode("project_limit", self.save, "one too many")

    def test_corrupt_and_linked_stores_are_refused(self):
        self.save()
        self.center.detach()
        store = self.root / "ai-project-center" / "projects.json"
        data = json.loads(store.read_text())
        data["projects"][0]["status"] = "exploded"
        store.write_text(json.dumps(data))
        self.assertCode("project_store_corrupt", self.make)
        store.unlink()
        other = self.root / "elsewhere.json"
        other.write_text('{"version": 1, "projects": []}')
        store.symlink_to(other)
        self.assertCode("project_store_unavailable", self.make)

    def test_failed_intent_persistence_prevents_any_launch(self):
        failing = {"on": False}

        def writer(path, body):
            if failing["on"]:
                raise OSError("disk gone")
            import ai_core
            ai_core.atomic_write(path, body)
        self.center.detach()
        self.center = self.make(writer=writer)
        item = self.approve(self.save(plan="The plan."))
        failing["on"] = True
        self.assertCode("project_store_unavailable", self.center.call, "run",
                        {"id": item["id"], "revision": item["revision"]})
        time.sleep(0.05)
        self.assertEqual(self.processes, [])
        self.assertEqual(self.center.summary()["error"], "project_store_unavailable")
        self.assertCode("project_store_unavailable", self.center.call, "save",
                        {"id": item["id"], "revision": item["revision"], "title": "Later"})
        self.center.call("list", {})              # reads still work

    def test_a_store_full_while_work_runs_stops_it_and_a_deletion_clears_it(self):
        self.center.detach()
        self.center = self.make(store_limit=20_000)
        spare = self.save(title="Spare")
        item = self.started([{"agent": "fixture", "role": "lead"}])
        self.processes[0].finish("x" * 30_000)      # the result no longer fits
        self.idle()
        summary = self.center.summary()
        self.assertEqual(summary["error"], "project_store_full")
        self.assertTrue(self.processes[0].ended.is_set())
        self.assertCode("project_store_full", self.center.call, "save",
                        {"id": spare["id"], "revision": spare["revision"], "title": "No"})
        self.assertNotEqual(self.get(item["id"])["status"], "running")
        self.center.call("delete", {"id": spare["id"], "revision": spare["revision"]})
        self.assertNotIn("error", self.center.summary())

    def test_a_restart_interrupts_active_records(self):
        item = self.started([{"agent": "fixture", "role": "lead"}])
        store = self.root / "ai-project-center" / "projects.json"
        saved = store.read_bytes()
        self.processes[0].finish("Done.")
        self.idle()
        self.center.detach()
        store.write_bytes(saved)                    # as if the app had died while it ran
        reopened = self.make()
        again = reopened.call("get", {"id": item["id"]})["project"]
        self.assertEqual(again["status"], "interrupted")
        self.assertTrue(again["needs_reconciliation"])
        self.assertEqual(again["events"][-1]["type"], "interrupted")


class LifecycleTest(CenterCase):
    def test_plan_review_and_sequential_run(self):
        item = self.save(team=[{"agent": "fixture", "role": "reviewer"}, {"agent": "fixture", "role": "lead"},
                               {"agent": "fixture", "role": "implementer", "instructions": "Write tests."}])
        self.center.call("plan", {"id": item["id"], "revision": item["revision"]})
        wait_until(lambda: len(self.processes) == 1)
        self.assertEqual(self.commands[0][:2], ["/fixture/agent", "read-only"])
        self.assertIn("Create a concrete implementation plan", self.commands[0][-1])
        self.assertEqual(self.directories[0], self.workspace)
        self.processes[0].finish("1. Change it. 2. Test it.")
        wait_until(lambda: len(self.processes) == 2)       # the reviewer reviews the plan, read-only
        self.assertEqual(self.commands[1][1], "read-only")
        self.processes[1].finish("Plan looks fine.")
        self.idle()
        planned = self.get(item["id"])
        self.assertEqual(planned["status"], "awaiting_review")
        self.assertEqual(planned["plan"], "1. Change it. 2. Test it.")
        self.assertFalse(planned["approval_valid"])
        ready = self.approve(planned)
        self.assertEqual(ready["status"], "ready")
        self.assertTrue(ready["approval_valid"])
        self.center.call("run", {"id": ready["id"], "revision": ready["revision"]})
        for n, (mode, text) in enumerate((("workspace-write", "Lead done."), ("workspace-write", "Tests written."),
                                          ("read-only", "No findings."))):
            wait_until(lambda: len(self.processes) == 3 + n)
            self.assertEqual(self.commands[2 + n][1], mode)
            self.processes[2 + n].finish(text)
        self.assertIn("Write tests.", self.commands[3][-1])
        self.assertIn("Lead done.", self.commands[3][-1])            # the handoff
        self.idle()
        done = self.get(item["id"])
        self.assertEqual(done["status"], "completed")
        self.assertIn("reviewer (fixture), exit 0:\nNo findings.", done["handoff"])

    def test_duplicate_run_and_unapproved_run_are_refused(self):
        item = self.save(plan="The plan.")
        self.assertCode("project_plan_not_approved", self.center.call, "run",
                        {"id": item["id"], "revision": item["revision"]})
        item = self.approve(item)
        self.center.call("run", {"id": item["id"], "revision": item["revision"]})
        running = self.get(item["id"])
        self.assertCode("project_already_running", self.center.call, "run",
                        {"id": item["id"], "revision": running["revision"]})
        self.assertCode("project_already_running", self.center.call, "delete",
                        {"id": item["id"], "revision": running["revision"]})

    def test_a_changed_profile_invalidates_approval(self):
        item = self.approve(self.save(plan="The plan."))
        self.profiles[0]["run_argv"] = ["/fixture/agent", "changed", "{prompt}"]
        self.center.discovery_generation += 1
        self.assertFalse(self.get(item["id"])["approval_valid"])
        self.assertEqual(self.card(item["id"])["activity"], "approve_again")
        self.assertCode("project_plan_not_approved", self.center.call, "run",
                        {"id": item["id"], "revision": item["revision"]})

    def test_pause_then_acknowledged_continue_from_the_handoff(self):
        item = self.started([{"agent": "fixture", "role": "lead"}, {"agent": "fixture", "role": "implementer"}])
        self.processes[0].add("Half of the work.")
        time.sleep(0.05)
        paused = self.center.call("pause", {"id": item["id"]})["project"]
        self.assertTrue(self.processes[0].stopped)
        self.assertEqual(paused["status"], "paused")
        self.assertTrue(paused["needs_reconciliation"])
        self.assertEqual(self.card(item["id"])["second"], "check_changes_first")
        self.assertCode("project_review_changes_first", self.center.call, "continue",
                        {"id": item["id"], "revision": paused["revision"]})
        self.center.call("continue", {"id": item["id"], "revision": paused["revision"],
                                      "acknowledge_interrupted": True})
        wait_until(lambda: len(self.processes) == 2)
        self.assertIn("Half of the work.", self.commands[1][-1])
        self.assertIn("never repeat an uncertain side effect", self.commands[1][-1])
        self.assertEqual(self.commands[1][1], "workspace-write")      # the unfinished lead again
        self.processes[1].finish("Lead done.")
        wait_until(lambda: len(self.processes) == 3)
        self.processes[2].finish("Done.")
        self.idle()
        self.assertEqual(self.get(item["id"])["status"], "completed")

    def test_a_refused_stop_keeps_the_claim_and_retry_asks_again(self):
        item = self.started([{"agent": "fixture", "role": "lead"}])
        self.processes[0].refuse_stop = True
        self.assertCode("project_still_pausing", self.center.call, "pause", {"id": item["id"]})
        self.assertEqual(self.card(item["id"])["activity"], "stopping")
        self.processes[0].refuse_stop = False
        self.assertEqual(self.center.call("pause", {"id": item["id"]})["project"]["status"], "paused")

    def test_a_failing_agent_fails_the_project(self):
        item = self.started([{"agent": "fixture", "role": "lead"}])
        self.processes[0].finish("Broken.", 3)
        self.idle()
        failed = self.get(item["id"])
        self.assertEqual(failed["status"], "failed")
        self.assertTrue(failed["needs_reconciliation"])
        self.assertEqual(self.card(item["id"])["bucket"], "attention")

    def test_one_folder_one_active_project(self):
        first = self.started([{"agent": "fixture", "role": "lead"}])
        inner = os.path.join(self.workspace, "inner")
        os.mkdir(inner)
        other = self.approve(self.save(title="Other", path=inner, plan="Plan."))
        self.assertCode("project_folder_busy", self.center.call, "run",
                        {"id": other["id"], "revision": other["revision"]})
        self.processes[0].finish("Done.")
        self.idle()
        self.center.call("run", {"id": other["id"], "revision": other["revision"]})
        self.assertIsNotNone(first)

    def test_policy_is_checked_again_for_a_later_teammate(self):
        self.profiles.append({**self.profiles[0], "id": "partner", "name": "Partner"})
        item = self.started([{"agent": "fixture", "role": "lead"}, {"agent": "partner", "role": "reviewer"}])
        self.refused_agents.add("partner")
        self.processes[0].finish("Done.")
        self.idle()
        failed = self.get(item["id"])
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["events"][-1]["text"], "rule_refused")
        self.assertEqual(len(self.processes), 1)

    def test_prompt_size_guard_and_stdin_profiles(self):
        self.center.detach()
        self.center = self.make(prompt_limit=2000)
        ready = self.approve(self.save(plan="p" * 3000))
        self.center.call("run", {"id": ready["id"], "revision": ready["revision"]})
        self.idle()
        self.assertEqual(self.processes, [])
        self.assertEqual(self.get(ready["id"])["events"][-1]["text"], "project_prompt_too_large")
        self.profiles[0].update(stdin=True, run_argv=["/fixture/agent", "workspace-write"])
        again = self.approve(self.get(ready["id"]))
        self.center.call("continue", {"id": ready["id"], "revision": again["revision"],
                                      "acknowledge_interrupted": True})
        wait_until(lambda: len(self.processes) == 1)
        self.assertNotIn("p" * 3000, " ".join(self.commands[0]))
        self.assertIn("p" * 3000, self.inputs[0])

    def test_launch_templates_are_checked(self):
        self.profiles[0]["run_argv"] = ["/fixture/agent", "--", "{prompt}"]
        item = self.save(plan="Plan.")
        self.assertCode("project_profile_invalid", self.center.call, "review",
                        {"id": item["id"], "revision": item["revision"], "approved": True})
        self.profiles[0]["run_argv"] = ["/fixture/agent", "--prompt={prompt}"]
        self.assertCode("project_profile_invalid", self.center.call, "review",
                        {"id": item["id"], "revision": item["revision"], "approved": True})

    def test_model_provider_and_subagent_options_follow_the_template(self):
        self.add_runtime()
        self.profiles[0].update(native_subagents=True, subagent_option="--agents",
                                subagent_definitions={"reviewer": {"description": "d", "prompt": "p"}})
        item = self.started([{"agent": "fixture", "role": "lead", "runtime": "runtime",
                              "provider": "local-provider", "model": "tiny-model", "subagents": ["reviewer"]}])
        argv = self.commands[0]
        self.assertEqual(argv[3:7], ["--provider", "local-provider", "--model", "tiny-model"])
        self.assertEqual(argv[7], "--agents")
        self.assertEqual(json.loads(argv[8]), {"reviewer": {"description": "d", "prompt": "p"}})
        self.assertIsNotNone(item)

    def test_agent_environment_drops_session_variables(self):
        env = pc.agent_environment({"PATH": "/bin", "CLAUDECODE": "1", "CLAUDE_CODE_X": "1",
                                    "AI_AGENTS_MAP_NO_PROVIDERS": "1", "HOME": "/h"})
        self.assertEqual(env, {"PATH": "/bin", "HOME": "/h"})

    def test_the_default_authorize_asks_the_rules(self):
        deny = [{"effect": "allow", "action": "*", "category": "*", "item": "*", "agent": "*"},
                {"effect": "deny", "action": "start", "category": "agents", "item": "fixture", "agent": "*"}]
        values = {"action_rules": deny, "action_rule_hints": True}
        with mock.patch("ai_core.setting", side_effect=values.get):
            with self.assertRaises(ProjectError) as caught:
                pc.rules_authorize("fixture")
            self.assertEqual(caught.exception.code, "rule_refused")
            pc.rules_authorize("partner")

    def test_shutdown_stops_all_and_can_be_retried(self):
        first = self.started([{"agent": "fixture", "role": "lead"}])
        self.processes[0].refuse_stop = True
        self.assertCode("project_still_pausing", self.center.shutdown)
        self.assertFalse(self.center._closed)
        self.processes[0].refuse_stop = False
        self.center.shutdown()
        self.assertCode("project_center_closed", self.center.call, "list", {})
        self.assertIsNotNone(first)


class SetupTest(CenterCase):
    def setup_result(self, item, request, proposal, target="run"):
        count = len(self.processes)
        self.center.call("setup", {"id": item["id"], "revision": item["revision"], "request": request,
                                   "agent": "fixture", "target": target})
        wait_until(lambda: len(self.processes) == count + 1)
        self.processes[-1].finish(json.dumps(proposal))
        self.idle()
        return self.get(item["id"])

    def test_a_proposal_is_drafted_then_applied(self):
        item = self.approve(self.save(plan="The plan."))
        result = self.setup_result(item, "Use the fixture agent.",
                                   {"team": [{"agent": "fixture", "role": "lead"},
                                             {"agent": "fixture", "role": "reviewer"}],
                                    "rationale": "One agent does both.", "warnings": []})
        self.assertEqual(self.commands[0][:2], ["/fixture/agent", "no-tools"])
        self.assertTrue(self.directories[0].endswith("setup-workspace"))
        self.assertEqual(result["status"], "ready")                 # setup only drafts
        self.assertTrue(result["approval_valid"])
        self.assertEqual(len(result["setup_proposal"]["team"]), 2)
        applied = self.center.call("setup_apply", {"id": item["id"], "revision": result["revision"]})["project"]
        self.assertEqual(len(applied["team"]), 2)
        self.assertEqual(applied["status"], "awaiting_review")
        self.assertIsNone(applied["approved_digest"])

    def test_a_named_teammate_cannot_be_omitted(self):
        self.profiles.append({**self.profiles[0], "id": "partner", "name": "Partner agent"})
        item = self.save()
        result = self.setup_result(item, "Use fixture with partner.",
                                   {"team": [{"agent": "fixture", "role": "lead"}], "rationale": "Short.",
                                    "warnings": []})
        self.assertEqual(result["setup_error"], "project_setup_agent_omitted")
        self.assertEqual(result["status"], "draft")
        self.assertNotIn("setup_proposal", result)

    def test_setup_cannot_touch_a_paused_execution(self):
        item = self.started([{"agent": "fixture", "role": "lead"}])
        paused = self.center.call("pause", {"id": item["id"]})["project"]
        self.assertCode("project_review_changes_first", self.center.call, "setup",
                        {"id": item["id"], "revision": paused["revision"], "request": "Other team."})


class StageTest(unittest.TestCase):
    def test_stages_activities_and_buckets(self):
        def state(record, **facts):
            return project_state(record, ProjectFacts(**facts)).to_json()
        self.assertEqual(state({"status": "draft"}), {"stage": "goal", "activity": "draft", "second": None,
                                                      "bucket": "drafts"})
        self.assertEqual(state({"status": "ready"}, approval_valid=True)["activity"], "ready_to_run")
        self.assertEqual(state({"status": "ready"}, approval_valid=True, launch_blocked=True),
                         {"stage": "ready", "activity": "approved_blocked", "second": None, "bucket": "needs_you"})
        self.assertEqual(state({"status": "ready"})["activity"], "approve_again")
        self.assertEqual(state({"status": "paused", "phase": "running", "needs_reconciliation": True}),
                         {"stage": "run", "activity": "paused", "second": "check_changes_first",
                          "bucket": "needs_you"})
        self.assertEqual(state({"status": "failed", "phase": "running"}, approval_valid=True)["bucket"], "attention")
        self.assertEqual(state({"status": "reviewing", "phase": "planning"})["activity"], "reviewing_plan")
        self.assertEqual(state({"status": "completed"})["bucket"], "finished")
        self.assertEqual(state({"status": "paused"}, processes_stopping=True)["activity"], "stopping")
        setup = {"status": "planning", "phase": "setup", "setup_return": {"status": "ready", "phase": "planning"}}
        self.assertEqual(state(setup, approval_valid=True)["stage"], "ready")
        self.assertEqual(state(setup)["activity"], "setting_up_team")


class BoardTest(CenterCase):
    def test_summary_counts_each_project_once_and_by_agent(self):
        self.profiles.append({**self.profiles[0], "id": "partner"})
        self.save(title="Draft", team=[{"agent": "fixture", "role": "lead"}],
                  plan_team=[{"agent": "fixture", "role": "lead"}, {"agent": "partner", "role": "reviewer"}])
        self.save(title="Review", plan="A plan.")
        self.approve(self.save(title="Ready", plan="A plan."))
        summary = self.center.summary()
        self.assertEqual((summary["drafts"], summary["needs_you"], summary["ready"], summary["total"]), (1, 1, 1, 3))
        self.assertEqual(summary["waiting"], 1)
        self.assertEqual(summary["by_agent"], {"fixture": 3, "partner": 1})
        listed = self.center.call("list", {})
        self.assertEqual(listed["summary"], summary)

    def test_a_card_carries_teams_state_and_last_event(self):
        item = self.save(plan="A plan.", team=[{"agent": "fixture", "role": "reviewer"},
                                               {"agent": "fixture", "role": "lead", "model": "m1"}])
        self.profiles[0]["model_option"] = "--model"
        card = self.card(item["id"])
        self.assertEqual(card["stage"], "approval")
        self.assertEqual(card["activity"], "needs_review")
        self.assertEqual([a["role"] for a in card["execution"]], ["lead", "reviewer"])
        self.assertEqual([a["role"] for a in card["planning"]], ["lead", "reviewer"])
        self.assertEqual(card["execution"][0]["name"], "Fixture agent")
        self.assertEqual(card["last_event"]["type"], "saved")
        self.assertIsNone(card["active"])

    def test_issue_links_target_node_and_fix(self):
        self.add_runtime()
        n = [0]

        def issue_of(member):
            item = self.save(title=f"T{n[0]}", plan="A saved plan.", team=[{"role": "lead", **member}])
            n[0] += 1
            self.center.discovery_generation += 1
            return self.card(item["id"])["execution"][0]["issues"][0]
        bound = {"agent": "fixture", "runtime": "runtime", "provider": "local-provider", "model": "tiny-model"}
        runtime = self.runtimes[0]
        runtime["running"] = False
        self.assertEqual(issue_of(bound), {"code": "runtime_stopped", "target": "runtime",
                                           "node": "runtime:runtime", "fix": "map"})
        runtime["installed"] = False
        self.assertEqual(issue_of(bound), {"code": "runtime_stopped", "target": "runtime", "fix": "team"})
        runtime.update(installed=True, running=True)
        self.assertEqual(issue_of({**bound, "model": "Other-Model"}),
                         {"code": "model_unavailable", "target": "model", "node": "runtime:runtime", "fix": "map"})
        self.assertEqual(issue_of({k: v for k, v in bound.items() if k != "model"}),
                         {"code": "model_unavailable", "target": "model", "fix": "team"})
        self.profiles[0]["access_modes"] = ["read-only"]
        self.assertEqual(issue_of({"agent": "fixture", "access": "project-edit"}),
                         {"code": "access_unsupported", "target": "permission", "fix": "team"})
        del self.profiles[0]["access_modes"]
        self.assertEqual(issue_of({**bound, "provider": "another-provider"}),
                         {"code": "provider_unavailable", "target": "provider", "fix": "team"})
        binding = runtime["bindings"].pop("fixture")
        self.assertEqual(issue_of(bound), {"code": "provider_unavailable", "target": "provider",
                                           "node": "runtime:runtime", "fix": "map"})
        option = self.profiles[0].pop("provider_option")
        self.assertEqual(issue_of(bound), {"code": "provider_unavailable", "target": "provider", "fix": "team"})
        self.profiles[0]["provider_option"] = option
        runtime["bindings"]["fixture"] = binding
        self.assertEqual(issue_of({"agent": "fixture", "provider": "local-provider"}),
                         {"code": "runtime_required", "target": "runtime", "fix": "team"})
        model_option = self.profiles[0].pop("model_option")
        self.assertEqual(issue_of({"agent": "fixture", "model": "some-model"}),
                         {"code": "model_unsupported", "target": "model", "fix": "team"})
        self.profiles[0]["model_option"] = model_option
        self.assertEqual(issue_of({"agent": "fixture", "subagents": ["helper"]}),
                         {"code": "subagents_unsupported", "target": "agent", "fix": "team"})
        self.assertEqual(issue_of({"agent": "gone-agent"}),
                         {"code": "agent_unavailable", "target": "agent", "fix": "team"})

    def test_project_issues_link_to_goal_and_team(self):
        item = self.center.call("save", {"title": "Empty", "brief": "", "path": self.workspace,
                                         "team": [], "plan": ""})["project"]
        self.assertEqual(self.card(item["id"])["issues"], [
            {"code": "no_lead", "target": "team", "fix": "team"},
            {"code": "empty_goal", "target": "goal", "fix": "goal"}])
        gone = os.path.join(self.workspace, "gone")
        os.mkdir(gone)
        other = self.save(title="Gone", path=gone)
        os.rmdir(gone)
        self.center.discovery_generation += 1
        self.assertEqual(self.card(other["id"])["issues"], [{"code": "folder_missing", "target": "folder",
                                                             "fix": "goal"}])

    def test_approved_blocked_by_a_stopped_runtime_then_ready_without_a_new_approval(self):
        self.add_runtime()
        item = self.approve(self.save(plan="A plan.", team=[{"agent": "fixture", "role": "lead",
                                                             "runtime": "runtime", "provider": "local-provider",
                                                             "model": "tiny-model"}]))
        self.assertEqual(self.card(item["id"])["activity"], "ready_to_run")
        self.runtimes[0]["running"] = False
        self.center.discovery_generation += 1
        card = self.card(item["id"])
        self.assertEqual((card["activity"], card["bucket"]), ("approved_blocked", "needs_you"))
        self.assertEqual(card["issues"], [{"code": "runtime_stopped", "target": "runtime",
                                           "node": "runtime:runtime", "fix": "map"}])
        self.runtimes[0]["running"] = True
        self.center.discovery_generation += 1
        self.assertEqual(self.card(item["id"])["activity"], "ready_to_run")

    def test_the_active_assignment(self):
        item = self.approve(self.save(plan="P.", team=[{"agent": "fixture", "role": "lead"},
                                                       {"agent": "fixture", "role": "reviewer"}]))
        self.assertEqual(self.card(item["id"])["active"], {"phase": "execution", "position": 0, "state": "next"})
        self.center.call("run", {"id": item["id"], "revision": item["revision"]})
        wait_until(lambda: len(self.processes) == 1)
        self.assertEqual(self.card(item["id"])["active"], {"phase": "execution", "position": 0, "state": "active"})
        self.processes[0].finish("Done.")
        wait_until(lambda: len(self.processes) == 2)
        self.center.call("pause", {"id": item["id"]})
        self.assertEqual(self.card(item["id"])["active"], {"phase": "execution", "position": 1, "state": "stopped"})

    def test_the_summary_poll_reads_memory(self):
        self.approve(self.save(plan="P."))
        self.center.call("list", {})
        checks = self.center.folder_checks
        for _ in range(5):
            self.center.summary()
        self.assertEqual(self.center.folder_checks, checks)


class OrderTest(CenterCase):
    def order(self):
        return [p["title"] for p in self.center.call("list", {})["projects"]]

    def test_new_on_top_moves_and_reopen(self):
        a, b, c = (self.save(title=t) for t in "ABC")
        self.assertEqual(self.order(), ["C", "B", "A"])
        self.assertEqual(self.center.call("move", {"id": a["id"], "before": c["id"]})["order"],
                         [a["id"], c["id"], b["id"]])
        self.center.call("move", {"id": a["id"], "before": None})
        self.assertEqual(self.order(), ["C", "B", "A"])
        moved = self.get(a["id"])
        self.assertEqual(moved["revision"], a["revision"])            # the order is not content
        self.center.call("move", {"id": b["id"], "before": c["id"]})
        self.center.detach()
        self.center = self.make()
        self.assertEqual(self.order(), ["B", "C", "A"])

    def test_unknown_ids_and_fields_are_refused(self):
        a = self.save(title="A")
        self.assertCode("project_unknown", self.center.call, "move", {"id": a["id"], "before": "f" * 32})
        self.assertCode("project_unknown", self.center.call, "move", {"id": "f" * 32})
        self.assertCode("invalid", self.center.call, "move", {"id": a["id"], "after": None})

    def test_importance(self):
        a = self.save(title="A")
        marked = self.center.call("set_importance", {"id": a["id"], "importance": "high"})["project"]
        self.assertEqual((marked["importance"], marked["revision"]), ("high", a["revision"]))
        self.assertCode("invalid", self.center.call, "set_importance", {"id": a["id"], "importance": "urgent"})
        self.center.detach()
        self.center = self.make()
        self.assertEqual(self.get(a["id"])["importance"], "high")

    def test_a_store_from_before_ranks_latest_change_first(self):
        for t in "ABC":
            self.save(title=t)
        self.center.detach()
        store = self.root / "ai-project-center" / "projects.json"
        data = json.loads(store.read_text())
        for n, project in enumerate(data["projects"]):
            project.pop("board_rank")
            project.pop("importance")
            project["updated_at"] = f"2026-01-0{n + 1}T00:00:00Z"   # C oldest … A newest by this
        store.write_text(json.dumps(data))
        self.center = self.make()
        self.assertEqual(self.order(), ["A", "B", "C"])
        self.assertEqual({p["importance"] for p in self.center.call("list", {})["projects"]}, {"medium"})


class MessageTest(CenterCase):
    def message(self, item, text, mode="queue", run_id=None, acknowledge=None):
        params = {"id": item["id"], "run_id": run_id or item["run_id"], "text": text, "mode": mode}
        if acknowledge is not None or mode == "now":
            params["acknowledge_interrupted"] = True if acknowledge is None else acknowledge
        return self.center.call("message", params)

    def test_queued_for_the_next_assignment_exactly_once(self):
        item = self.started([{"agent": "fixture", "role": "lead"}, {"agent": "fixture", "role": "implementer"},
                             {"agent": "fixture", "role": "reviewer"}])
        text = "Please also update the changelog."
        sent = self.message(item, text)
        self.assertEqual((sent["continued"], sent["reason"], sent["message"]["state"]), (False, None, "queued"))
        self.assertEqual(sent["project"]["messages_queued"], 1)
        self.assertFalse(self.processes[0].stopped)
        self.assertEqual(self.card(item["id"])["messages_queued"], 1)
        self.processes[0].finish("Lead done.")
        wait_until(lambda: len(self.processes) == 2)
        self.assertEqual(self.commands[1][-1].count(text), 1)
        self.assertIn("Messages from the user", self.commands[1][-1])
        current = self.get(item["id"])
        stored = current["messages"][0]
        self.assertEqual(stored["state"], "delivered")
        self.assertEqual(stored["delivered_to"], {"phase": "execution", "position": 1, "agent": "fixture",
                                                  "role": "implementer"})
        self.assertIn("message_delivered", self.types(current))
        self.processes[1].finish("Implemented.")
        wait_until(lambda: len(self.processes) == 3)
        self.assertEqual(self.commands[2][-1].count(text), 1)            # in the handoff only
        self.assertNotIn("Messages from the user", self.commands[2][-1])
        self.processes[2].finish("No findings.")
        self.idle()

    def test_refusals(self):
        ready = self.approve(self.save(plan="The plan."))
        self.assertCode("project_not_running", self.message, ready, "Hello", "queue", "none")
        item = self.started([{"agent": "fixture", "role": "lead"}])
        self.assertCode("project_changed", self.message, item, "Hello", "queue", "another-run")
        self.assertCode("project_changed", self.center.call, "message", {"id": item["id"], "text": "Hello"})
        self.assertCode("project_message_empty", self.message, item, "  \n ")
        self.assertCode("project_message_too_long", self.message, item, "ä" * 2049)
        self.assertCode("project_message_invalid", self.message, item, "Hello", "later")
        self.assertCode("project_message_invalid", self.center.call, "message",
                        {"id": item["id"], "run_id": item["run_id"], "text": "Hi", "extra": True})
        self.assertCode("project_unknown", self.center.call, "message", {"id": "x", "run_id": "x", "text": "Hi"})
        self.message(item, "ä" * 2048)
        for n in range(1, pc.MAX_QUEUED_MESSAGES):
            self.message(item, f"Message {n}")
        self.assertCode("project_messages_full", self.message, item, "One too many")
        self.processes[0].finish("Done.")
        self.idle()
        done = self.get(item["id"])
        self.assertEqual(done["status"], "completed")
        self.assertCode("project_not_running", self.message, done, "Too late")
        self.assertEqual(done["messages_queued"], pc.MAX_QUEUED_MESSAGES)
        self.assertIn("message_waiting", self.types(done))

    def test_while_pausing_send_queues_and_send_now_is_refused(self):
        item = self.started([{"agent": "fixture", "role": "lead"}])
        self.processes[0].refuse_stop = True
        self.assertCode("project_still_pausing", self.center.call, "pause", {"id": item["id"]})
        self.assertCode("project_still_pausing", self.message, item, "Now, please", "now")
        self.assertEqual(self.message(item, "When you continue, check the tests.")["project"]["messages_queued"], 1)
        self.processes[0].refuse_stop = False
        current = self.center.call("pause", {"id": item["id"]})["project"]
        self.center.call("continue", {"id": item["id"], "revision": current["revision"],
                                      "acknowledge_interrupted": True})
        wait_until(lambda: len(self.processes) == 2)
        self.assertEqual(self.commands[1][-1].count("When you continue, check the tests."), 1)

    def test_send_now_pauses_and_continues_with_the_handoff_and_the_message(self):
        item = self.started([{"agent": "fixture", "role": "lead"}, {"agent": "fixture", "role": "implementer"}])
        self.processes[0].add("Partial progress.")
        time.sleep(0.05)
        text = "Stop using the old API, switch to the new one."
        self.assertCode("project_review_changes_first", self.message, item, text, "now", None, False)
        sent = self.message(item, text, "now")
        self.assertTrue(sent["continued"], sent["reason"])
        self.assertTrue(self.processes[0].stopped)
        wait_until(lambda: len(self.processes) == 2)
        self.assertEqual(self.commands[1][1], "workspace-write")
        self.assertEqual(self.commands[1][-1].count(text), 1)
        self.assertIn("Partial progress.", self.commands[1][-1])
        current = self.get(item["id"])
        self.assertEqual(current["status"], "running")
        self.assertNotEqual(current["run_id"], item["run_id"])
        types = self.types(current)
        self.assertEqual(types[types.index("message"):],
                         ["message", "pausing", "paused", "continued", "assignment", "message_delivered"])
        self.processes[1].finish("Lead switched the API.")
        wait_until(lambda: len(self.processes) == 3)
        self.processes[2].finish("Implemented.")
        self.idle()

    def test_send_now_that_cannot_continue_keeps_the_message_queued(self):
        item = self.started([{"agent": "fixture", "role": "lead"}])
        original = self.profiles[0]["run_argv"]
        self.profiles[0]["run_argv"] = ["/fixture/agent", "changed", "{prompt}"]
        sent = self.message(item, "Use the staging database.", "now")
        self.assertEqual((sent["continued"], sent["reason"]), (False, "project_plan_not_approved"))
        self.assertEqual(sent["project"]["status"], "paused")
        self.assertEqual(sent["project"]["messages_queued"], 1)
        self.assertEqual(self.types(sent["project"])[-1], "message_waiting")
        self.profiles[0]["run_argv"] = original
        self.center.call("continue", {"id": item["id"], "revision": sent["project"]["revision"],
                                      "acknowledge_interrupted": True})
        wait_until(lambda: len(self.processes) == 2)
        self.assertEqual(self.commands[1][-1].count("Use the staging database."), 1)

    def test_a_launch_that_never_started_keeps_the_message_queued(self):
        item = self.started([{"agent": "fixture", "role": "lead"}, {"agent": "fixture", "role": "implementer"}])
        self.message(item, "For the implementer.")
        self.start_gate = lambda: False
        self.processes[0].finish("Lead done.")
        self.idle()
        current = self.get(item["id"])
        self.assertEqual(current["status"], "failed")
        self.assertEqual(current["messages_queued"], 1)

    def test_drop_a_waiting_message_not_a_delivered_one(self):
        item = self.started([{"agent": "fixture", "role": "lead"}, {"agent": "fixture", "role": "implementer"}])
        first = self.message(item, "One")["message"]
        self.center.call("message_drop", {"id": item["id"], "message_id": first["id"]})
        self.assertEqual(self.get(item["id"])["messages"], [])
        second = self.message(item, "Two")["message"]
        self.processes[0].finish("Lead done.")
        wait_until(lambda: len(self.processes) == 2)
        wait_until(lambda: self.get(item["id"])["messages_queued"] == 0)
        self.assertCode("project_message_delivered", self.center.call, "message_drop",
                        {"id": item["id"], "message_id": second["id"]})
        self.assertCode("project_message_unknown", self.center.call, "message_drop",
                        {"id": item["id"], "message_id": "0" * 32})
        self.assertCode("project_message_invalid", self.center.call, "message_drop", {"id": item["id"]})

    def test_team_setup_refuses_messages(self):
        item = self.save()
        self.center.call("setup", {"id": item["id"], "revision": item["revision"], "request": "Use fixture.",
                                   "agent": "fixture"})
        wait_until(lambda: len(self.processes) == 1)
        current = self.get(item["id"])
        self.assertCode("project_not_running", self.message, current, "Hello")
        self.processes[0].finish("{}")
        self.idle()

    def test_malformed_stored_messages_are_corrupt(self):
        self.save()
        self.center.detach()
        store = self.root / "ai-project-center" / "projects.json"
        data = json.loads(store.read_text())
        data["projects"][0]["messages"] = [{"id": "nothex", "text": "x"}]
        store.write_text(json.dumps(data))
        self.assertCode("project_store_corrupt", self.make)


AGENT = """import os, sys, time
mode = sys.argv[1]
if mode == "tree":
    if os.fork() == 0:
        os.setsid()
        if os.fork() == 0:
            import signal
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            with open("grandchild.tmp", "w") as handle:
                handle.write(str(os.getpid()))
            os.rename("grandchild.tmp", "grandchild.pid")
            time.sleep(300)
        os._exit(0)
    print("tree up", flush=True)
    time.sleep(300)
print("mode", mode, "session", os.environ.get("CLAUDECODE", "none"), "prompt words", len(sys.argv) - 2)
print(sys.argv[-1][:40])
"""


class SupervisorTest(CenterCase):
    """The real supervisor (a subreaper child process) under the project center."""

    def setUp(self):
        super().setUp()
        self.center.detach()
        script = self.root / "agent.py"
        script.write_text(AGENT)
        self.profiles[0].update(plan_argv=[sys.executable, str(script), "read", "{prompt}"],
                                run_argv=[sys.executable, str(script), "write", "{prompt}"])
        self.center = self.make(runner=None, environment={**os.environ, "CLAUDECODE": "1"})

    def test_an_assignment_runs_supervised_without_session_variables(self):
        item = self.approve(self.save(plan="The plan."))
        self.center.call("run", {"id": item["id"], "revision": item["revision"]})
        wait_until(lambda: self.get(item["id"])["status"] == "completed", timeout=20)
        done = self.get(item["id"])
        self.assertIn("mode write session none prompt words 1", done["output"])
        self.assertIn("Project: A project", done["output"])
        self.assertFalse(done["processes_stopping"])

    def test_pause_stops_the_whole_tree_and_releases_the_run_lock(self):
        script = self.profiles[0]["run_argv"][1]
        self.profiles[0]["run_argv"] = [sys.executable, script, "tree", "{prompt}"]
        item = self.approve(self.save(plan="The plan."))
        self.center.call("run", {"id": item["id"], "revision": item["revision"]})
        marker = Path(self.workspace) / "grandchild.pid"
        wait_until(marker.exists, timeout=20)          # a setsid grandchild that ignores SIGTERM runs
        grandchild = int(marker.read_text())
        self.assertTrue(self._held(item["id"]))
        paused = self.center.call("pause", {"id": item["id"]})["project"]
        self.assertEqual(paused["status"], "paused")
        self.assertFalse(self._held(item["id"]))
        self.assertFalse(paused["processes_stopping"])
        self.assertFalse(os.path.exists(f"/proc/{grandchild}") and ps.start_time(grandchild) is not None
                         and (ps._stat_fields(grandchild) or ["Z"])[0] not in ("Z", "X"))
        self.assertEqual([args for args in self._own_command_lines() if str(script) in args and "tree" in args], [])

    @staticmethod
    def _own_command_lines():
        lines = []
        for name in os.listdir("/proc"):
            if name.isdigit():
                try:
                    with open(f"/proc/{name}/cmdline", "rb") as handle:
                        lines.append(handle.read().replace(b"\0", b" ").decode("utf-8", "replace"))
                except OSError:
                    continue
        return lines

    def _held(self, pid):
        return self.center._run_held(pid)


class SupervisorProtocolTest(unittest.TestCase):
    def test_a_start_from_another_controller_is_refused_without_launching(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = os.path.join(tmp, "run.lock")
            open(lock, "w").close()
            marker = os.path.join(tmp, "ran")
            proc = __import__("subprocess").Popen(ps.command(), stdin=-1, stdout=-1, cwd=tmp)
            start = {"argv": [sys.executable, "-c", f"open({marker!r}, 'w')"], "cwd": tmp, "input": None,
                     "lock": lock, "controller": {"pid": 1, "start": "0"}}
            out, _ = proc.communicate(json.dumps(start).encode() + b"\n" + b'{"launch": true}\n', timeout=20)
            events = [json.loads(line) for line in out.splitlines()]
            self.assertEqual(events, [{"error": "controller_gone"}, {"stopped": True}])
            self.assertFalse(os.path.exists(marker))

    def test_eof_before_the_go_ahead_launches_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = os.path.join(tmp, "ran")
            proc = __import__("subprocess").Popen(ps.command(), stdin=-1, stdout=-1, cwd=tmp)
            start = {"argv": [sys.executable, "-c", f"open({marker!r}, 'w')"], "cwd": tmp, "input": None,
                     "controller": {"pid": os.getpid(), "start": ps.start_time(os.getpid())}}
            out, _ = proc.communicate(json.dumps(start).encode() + b"\n", timeout=20)
            events = [json.loads(line) for line in out.splitlines()]
            self.assertEqual(events[-2:], [{"error": "launch_withdrawn"}, {"stopped": True}])
            self.assertFalse(os.path.exists(marker))

    def test_a_missing_program_is_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ProjectError) as caught:
                ps.run(["no-such-agent-here"], tmp, {"PATH": tmp}, None, os.path.join(tmp, "l"), lambda p: True)
            self.assertEqual(caught.exception.code, "project_agent_unavailable")


class StuckProcess(FakeProcess):
    """The leader ended but a descendant survives: wait() reports the unconfirmed cleanup at once,
    the output stays open, and stop is refused until `release` is set."""

    def __init__(self):
        super().__init__()
        self.release = threading.Event()

    def wait(self):
        raise ProjectError("project_processes_still_running")

    def stop(self, wait=4.0):
        self.stop_calls += 1
        if not self.release.is_set():
            raise ProjectError("project_processes_still_running")
        self.stopped = True
        self.lines.put(None)


class ReviewRoundOneTest(CenterCase):
    """Findings of review round 1 (Codex, Antigravity)."""

    def test_output_with_nul_keeps_the_store_loadable(self):
        item = self.started([{"agent": "fixture", "role": "lead"}])
        self.processes[0].finish("before\0after")
        self.idle()
        done = self.get(item["id"])
        self.assertEqual(done["status"], "completed")
        self.assertNotIn("\0", done["output"] + done["handoff"])
        self.assertIn("before\ufffdafter", done["output"])
        self.center.detach()
        self.assertEqual(self.make().call("get", {"id": item["id"]})["project"]["status"], "completed")

    def test_on_change_runs_outside_the_lock(self):
        finished = []

        def listener(project_id):
            other = threading.Thread(target=self.center.summary)
            other.start()
            other.join(2.0)                      # under the center's lock this would block
            finished.append(not other.is_alive())
        self.center.on_change = listener
        self.save()
        wait_until(lambda: finished, timeout=5)
        self.assertTrue(finished[0], "summary() was blocked while on_change ran")

    def test_a_pause_while_the_supervisor_starts_withdraws_the_launch(self):
        item = self.approve(self.save(plan="The plan."))
        gate, reached = threading.Event(), threading.Event()

        def hold():
            reached.set()
            gate.wait(5)
        self.before_launch = hold
        self.center.call("run", {"id": item["id"], "revision": item["revision"]})
        reached.wait(5)
        result = {}
        pausing = threading.Thread(target=lambda: result.update(self.center.call("pause", {"id": item["id"]})))
        pausing.start()
        wait_until(lambda: self.center._active[item["id"]].cancelled)
        gate.set()                                # the go-ahead is asked only now
        pausing.join(10)
        self.assertEqual(self.processes, [])      # never launched
        self.assertEqual(len(self.withdrawn), 1)
        self.assertEqual(result["project"]["status"], "paused")

    def test_a_detach_while_the_supervisor_starts_withdraws_the_launch(self):
        item = self.approve(self.save(plan="The plan."))
        gate, reached = threading.Event(), threading.Event()
        self.before_launch = lambda: (reached.set(), gate.wait(5))
        self.center.call("run", {"id": item["id"], "revision": item["revision"]})
        reached.wait(5)
        self.center.detach()
        gate.set()
        wait_until(lambda: self.withdrawn)
        self.assertEqual(self.processes, [])

    def test_an_unconfirmed_cleanup_after_the_leader_ended_does_not_hang(self):
        item = self.approve(self.save(plan="The plan."))
        stuck = StuckProcess()

        def runner(argv, cwd, env, input_text, lock, may_launch):
            self.assertTrue(may_launch(stuck))
            return stuck
        self.center._runner = runner
        self.center.call("run", {"id": item["id"], "revision": item["revision"]})
        wait_until(lambda: self.get(item["id"])["status"] == "pausing", timeout=10)
        self.assertEqual(self.card(item["id"])["bucket"], "running")
        self.assertIn("cleanup_failed", self.types(self.get(item["id"])))
        self.assertCode("project_still_pausing", self.center.call, "pause", {"id": item["id"]})
        stuck.release.set()
        self.assertEqual(self.center.call("pause", {"id": item["id"]})["project"]["status"], "paused")
        self.idle()

    def test_unhashable_move_parameters_are_unknown(self):
        a = self.save(title="A")
        self.assertCode("project_unknown", self.center.call, "move", {"id": []})
        self.assertCode("project_unknown", self.center.call, "move", {"id": a["id"], "before": {}})

    def test_messages_and_pause_after_shutdown_are_closed(self):
        item = self.started([{"agent": "fixture", "role": "lead"}])
        self.processes[0].finish("Done.")
        self.idle()
        self.center.shutdown()
        self.assertCode("project_center_closed", self.center.call, "message",
                        {"id": item["id"], "run_id": item["run_id"], "text": "Hi"})
        self.assertCode("project_center_closed", self.center.call, "pause", {"id": item["id"]})

    def test_runtime_bindings_offer_the_agents_configured_served_models(self):
        import agent_details
        import inventory
        runtime = inventory.Runtime("llamacpp", "llama.cpp", installed=True, running=True,
                                    address="127.0.0.1:8080", models=["served-a", "served-b", "embed-x"])
        provider = agent_details.Provider("llama-local", local=True, runtime="llama.cpp", port=8080)
        facts = pc.runtime_facts([runtime], [("pi", [provider], ["llama-local/served-a", "llama-local/absent",
                                                                 "other/served-b", "claude-opus"])])
        self.assertEqual(facts[0]["models"], ["served-a", "served-b"])
        self.assertEqual(facts[0]["bindings"], {"pi": {"provider": "llama-local", "models": ["served-a"]}})
        twice = pc.runtime_facts([runtime], [("pi", [provider, provider], [])])
        self.assertEqual(twice[0]["bindings"], {})           # ambiguous: no binding


class SupervisorBoundsTest(unittest.TestCase):
    def test_output_is_bounded_while_nobody_reads(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = os.path.join(tmp, "run.lock")
            open(lock, "w").close()
            process = ps.run([sys.executable, "-c", "import sys\nfor _ in range(200): sys.stdout.write('x' * 16384)"],
                             tmp, dict(os.environ), None, lock, lambda p: True)
            self.assertEqual(process.wait(), 0)
            self.assertLessEqual(process._chunk_bytes, ps.OUTPUT_BUFFER + ps.READ_CHUNK)
            self.assertTrue(process.dropped)
            text = "".join(process.output())
            self.assertGreater(len(text), 64 * 1024)
            process.stop()

    def test_a_supervisor_that_never_reads_cannot_block_the_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            silent = [sys.executable, "-c", "import time; time.sleep(60)"]
            outcome = {}

            def start():
                try:
                    ps.run([sys.executable, "-c", "pass", "p" * 300_000], tmp, dict(os.environ), None,
                           os.path.join(tmp, "l"), lambda p: True, supervisor=silent)
                except ProjectError as error:
                    outcome["code"] = error.code
            began = time.monotonic()
            thread = threading.Thread(target=start, daemon=True)
            thread.start()
            thread.join(15)
            self.assertFalse(thread.is_alive(), "the start blocked on a full pipe")
            self.assertEqual(outcome.get("code"), "project_agent_unavailable")
            self.assertLess(time.monotonic() - began, 12)

    def test_concurrent_stops_reap_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = os.path.join(tmp, "run.lock")
            open(lock, "w").close()
            process = ps.run([sys.executable, "-c", "import time; time.sleep(30)"], tmp, dict(os.environ), None,
                             lock, lambda p: True)
            self.assertTrue(process.started())
            errors = []

            def stop():
                try:
                    process.stop(wait=8)
                except Exception as error:      # noqa: BLE001  (any failure fails the test)
                    errors.append(error)
            threads = [threading.Thread(target=stop) for _ in range(3)]
            with mock.patch("process_group.end_child", wraps=__import__("process_group").end_child) as end:
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(20)
            self.assertEqual(errors, [])
            self.assertEqual(end.call_count, 1)
            self.assertIsNotNone(process._proc.returncode)


DYING_SUPERVISOR = """import json, sys
sys.stdin.buffer.readline()
print(json.dumps({"locked": True, "start_ack": True}), flush=True)
"""


class ReviewRoundTwoTest(CenterCase):
    """Findings of review round 2 (Codex, Antigravity)."""

    def test_unpaired_surrogates_are_refused_and_the_store_reopens(self):
        self.assertCode("invalid", self.save, "bad \ud800 title")
        item = self.save()
        self.assertCode("invalid", self.center.call, "save",
                        {"id": item["id"], "revision": item["revision"], "brief": "x\udfff"})
        running = self.started([{"agent": "fixture", "role": "lead"}])
        self.assertCode("project_message_invalid", self.center.call, "message",
                        {"id": running["id"], "run_id": running["run_id"], "text": "hi \ud800"})
        self.processes[0].finish("Done.")
        self.idle()
        self.center.detach()
        self.assertEqual(len(self.make().call("list", {})["projects"]), 2)

    def test_a_closed_center_says_so_before_checking_message_fields(self):
        self.center.shutdown()
        self.assertCode("project_center_closed", self.center.call, "message", {"id": "x", "text": ""})

    def test_a_returned_message_is_a_copy(self):
        item = self.started([{"agent": "fixture", "role": "lead"}])
        sent = self.center.call("message", {"id": item["id"], "run_id": item["run_id"], "text": "Hello"})
        sent["message"]["state"] = "delivered"
        sent["message"]["text"] = "changed"
        stored = self.get(item["id"])
        self.assertEqual(stored["messages_queued"], 1)
        self.assertEqual(stored["messages"][0]["text"], "Hello")
        self.processes[0].finish("Done.")
        self.idle()

    def test_setup_validation_uses_the_snapshot_runtimes(self):
        self.add_runtime()
        item = self.save()
        snapshot = self.center._snapshot()
        proposal = {"team": [{"agent": "fixture", "role": "lead", "runtime": "runtime",
                              "provider": "local-provider", "model": "tiny-model"}],
                    "target": "run", "rationale": "r", "warnings": [], "excluded_agents": [], "excluded_runtimes": []}

        def no_scan():
            raise AssertionError("runtimes scanned again during validation")
        self.center.runtime_profiles = no_scan
        self.center._validate_setup_proposal(proposal, {**item, "setup_request": "Use fixture."}, snapshot)

    def test_an_old_output_reader_writes_nothing_after_its_assignment(self):
        item = self.approve(self.save(plan="The plan."))
        stuck = StuckProcess()

        def runner(argv, cwd, env, input_text, lock, may_launch):
            self.assertTrue(may_launch(stuck))
            return stuck
        self.center._runner = runner
        self.center.call("run", {"id": item["id"], "revision": item["revision"]})
        wait_until(lambda: self.get(item["id"])["status"] == "pausing", timeout=10)
        before = self.get(item["id"])["output"]
        for text in ("late one ", "late two "):
            stuck.add(text)
            time.sleep(0.6)
        self.assertEqual(self.get(item["id"])["output"], before)
        stuck.release.set()
        self.center.call("pause", {"id": item["id"]})

    def test_the_notifier_ends_with_the_center(self):
        self.center.on_change = lambda project_id: None
        self.save()
        wait_until(lambda: self.center._notifier is not None)
        notifier = self.center._notifier
        self.center.detach()
        wait_until(lambda: not notifier.is_alive())

    def test_a_supervisor_gone_before_the_go_ahead_does_not_wedge_the_project(self):
        script = self.root / "dying.py"
        script.write_text(DYING_SUPERVISOR)
        self.center.detach()
        self.profiles[0]["run_argv"] = [sys.executable, "-c", "pass", "{prompt}"]
        self.center = self.make(runner=None, supervisor=[sys.executable, str(script)])

        def runner(argv, cwd, env, input_text, lock, may_launch):
            def late(process):
                wait_until(lambda: process._confirmed.done, timeout=5)   # gone before the go-ahead
                return may_launch(process)
            return ps.run(argv, cwd, env, input_text, lock, late, supervisor=[sys.executable, str(script)])
        self.center._runner = runner
        item = self.approve(self.save(plan="The plan."))
        self.center.call("run", {"id": item["id"], "revision": item["revision"]})
        wait_until(lambda: self.get(item["id"])["status"] == "failed", timeout=20)
        done = self.get(item["id"])
        self.assertEqual(done["events"][-1]["text"], "project_agent_unavailable")
        self.assertNotIn("cleanup_failed", self.types(done))
        self.assertEqual(self.center.summary()["running"], 0)
        self.center.shutdown()


SPAWNING_SUPERVISOR = """import json, os, sys, time
sys.stdin.buffer.readline()
print(json.dumps({"locked": True, "start_ack": True}), flush=True)
sys.stdin.buffer.readline()                       # the go-ahead
if os.fork() == 0:
    os.setsid()
    for fd in (0, 1, 2):
        os.close(fd)
    with open("escaped.pid", "w") as handle:
        handle.write(str(os.getpid()))
    time.sleep(60)
    os._exit(0)
time.sleep(0.3)
os._exit(9)                                       # dies after the spawn, before reporting it
"""


class ReviewRoundThreeTest(CenterCase):
    def test_a_supervisor_dying_after_the_go_ahead_keeps_the_cleanup_unconfirmed(self):
        script = self.root / "spawning.py"
        script.write_text(SPAWNING_SUPERVISOR)
        escaped = self.root / "escaped.pid"
        process = ps.run([sys.executable, "-c", "pass"], str(self.root), dict(os.environ), None,
                         str(self.root / "l"), lambda p: True, supervisor=[sys.executable, str(script)])
        try:
            wait_until(escaped.exists, timeout=10)
            wait_until(lambda: process._confirmed.done, timeout=10)
            self.assertFalse(process.started(0.1))
            with self.assertRaises(ProjectError) as caught:
                process.stop(wait=1.0)
            self.assertEqual(caught.exception.code, "project_processes_still_running")
        finally:
            wait_until(lambda: escaped.read_text().strip() != "", timeout=5)
            try:
                os.kill(int(escaped.read_text()), 9)
            except ProcessLookupError:
                pass
            process._reap(0.0)

    def test_a_stop_before_the_go_ahead_withdraws_the_launch(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = os.path.join(tmp, "run.lock")
            open(lock, "w").close()
            marker = os.path.join(tmp, "ran")

            release = threading.Event()
            threads = {}

            def authorize(managed):
                original = managed._reap

                def held(wait):                   # the stop's own reap waits until start() decided
                    if threading.current_thread() is threads["stop"]:
                        release.wait(10)
                    return original(wait)
                managed._reap = held
                threads["stop"] = threading.Thread(target=managed.stop, kwargs={"wait": 8})
                threads["stop"].start()
                # The supervisor withdrew, confirmed and its pipe ended before the go-ahead.
                wait_until(lambda: managed._confirmed.done and not managed._input_open, timeout=10)
                return True
            with self.assertRaises(ProjectError) as caught:
                ps.run([sys.executable, "-c", f"open({marker!r}, 'w')"], tmp, dict(os.environ), None, lock,
                       authorize)
            release.set()
            threads["stop"].join(10)
            self.assertEqual(caught.exception.code, "project_launch_withdrawn")
            self.assertFalse(os.path.exists(marker))

    def test_send_now_pauses_in_the_same_step_as_its_message(self):
        item = self.started([{"agent": "fixture", "role": "lead"}, {"agent": "fixture", "role": "implementer"}])
        original, entered, go = self.center._stop_attempt, threading.Event(), threading.Event()

        def slow_stop(project_id, active):
            entered.set()
            go.wait(5)
            return original(project_id, active)
        self.center._stop_attempt = slow_stop
        first = {}
        sender = threading.Thread(target=lambda: first.update(self.center.call(
            "message", {"id": item["id"], "run_id": item["run_id"], "text": "Now one.", "mode": "now",
                        "acknowledge_interrupted": True})))
        sender.start()
        entered.wait(5)
        self.assertEqual(self.get(item["id"])["status"], "pausing")
        self.assertCode("project_still_pausing", self.center.call, "message",
                        {"id": item["id"], "run_id": item["run_id"], "text": "Now two.", "mode": "now",
                         "acknowledge_interrupted": True})
        go.set()
        sender.join(15)
        self.assertTrue(first["continued"], first.get("reason"))
        wait_until(lambda: len(self.processes) == 2)
        self.processes[1].finish("Done.")
        wait_until(lambda: len(self.processes) == 3)
        self.processes[2].finish("Done.")
        self.idle()


class ReviewRoundFourTest(CenterCase):
    def test_control_closing_right_after_a_written_go_ahead_keeps_the_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = os.path.join(tmp, "run.lock")
            open(lock, "w").close()
            real_write = os.write
            seen = {}

            def authorize(managed):
                seen["managed"] = managed

                def write(fd, data):
                    written = real_write(fd, data)
                    if b'"launch"' in bytes(data):
                        managed._input_open = False    # EOF (_lost) or a detach right after the write
                    return written
                seen["patch"] = mock.patch.object(ps.os, "write", side_effect=write)
                seen["patch"].start()
                return True
            try:
                process = ps.run([sys.executable, "-c", "import time; time.sleep(30)"], tmp, dict(os.environ),
                                 None, lock, authorize)
            finally:
                if "patch" in seen:
                    seen["patch"].stop()
            self.assertIs(process, seen["managed"])        # not withdrawn: it may have launched
            self.assertTrue(process._go_ahead_sent)
            process.stop(wait=8)                            # control closed: the supervisor ends its tree
            self.assertTrue(process._confirmed.value)

    def test_a_runner_failing_after_a_written_go_ahead_keeps_the_claim(self):
        item = self.approve(self.save(plan="The plan."))
        stuck = StuckProcess()
        stuck._go_ahead_sent = True

        def runner(argv, cwd, env, input_text, lock, may_launch):
            self.assertTrue(may_launch(stuck))
            raise ProjectError("project_agent_unavailable")
        self.center._runner = runner
        self.center.call("run", {"id": item["id"], "revision": item["revision"]})
        wait_until(lambda: self.get(item["id"])["status"] in ("pausing", "failed"), timeout=10)
        self.assertEqual(self.get(item["id"])["status"], "pausing")
        self.assertIn(item["id"], self.center._active)
        stuck.release.set()
        self.center.call("pause", {"id": item["id"]})

    def test_send_now_continues_when_the_attempt_ends_before_its_stop(self):
        item = self.started([{"agent": "fixture", "role": "lead"}, {"agent": "fixture", "role": "implementer"}])
        begin = self.center._begin_pause

        def begin_and_end(found, active):
            begin(found, active)
            self.processes[0].finish("Lead stopped.")       # the agent ends by itself right away
        self.center._begin_pause = begin_and_end
        original_stop = self.center._stop_attempt

        def late_stop(project_id, active):                  # the attempt is gone before the stop
            wait_until(lambda: item["id"] not in self.center._active, timeout=5)
            return original_stop(project_id, active)
        self.center._stop_attempt = late_stop
        sent = self.center.call("message", {"id": item["id"], "run_id": item["run_id"], "text": "Now.",
                                            "mode": "now", "acknowledge_interrupted": True})
        self.assertTrue(sent["continued"], sent["reason"])
        wait_until(lambda: len(self.processes) == 2)
        self.processes[1].finish("Done.")
        wait_until(lambda: len(self.processes) == 3)
        self.processes[2].finish("Done.")
        self.idle()

    def test_a_slow_discovery_does_not_hold_up_pause(self):
        item = self.started([{"agent": "fixture", "role": "lead"}])
        gate, scanning = threading.Event(), threading.Event()
        profiles = self.profiles

        def slow():
            scanning.set()
            gate.wait(10)
            return profiles
        self.center.agent_profiles = slow
        self.center.discovery_generation += 1               # the next list scans
        lister = threading.Thread(target=lambda: self.center.call("list", {}))
        lister.start()
        scanning.wait(5)
        result = {}
        pauser = threading.Thread(target=lambda: result.update(self.center.call("pause", {"id": item["id"]})))
        pauser.start()
        pauser.join(5)
        stopped = not pauser.is_alive()
        gate.set()
        lister.join(10)
        pauser.join(10)
        self.assertTrue(stopped, "Pause waited for the discovery")
        self.assertEqual(result["project"]["status"], "paused")


class ReviewRoundFiveTest(CenterCase):
    def test_a_stop_racing_a_written_go_ahead_waits_for_its_record(self):
        script = self.root / "spawning.py"
        script.write_text(SPAWNING_SUPERVISOR)
        escaped = self.root / "escaped.pid"
        real_write = os.write
        written, release = threading.Event(), threading.Event()
        seen, outcome = {}, {}

        def authorize(managed):
            seen["managed"] = managed

            def write(fd, data):
                count = real_write(fd, data)
                if b'"launch"' in bytes(data):
                    written.set()
                    release.wait(10)           # the writer holds on after the write, before its record
                return count
            seen["patch"] = mock.patch.object(ps.os, "write", side_effect=write)
            seen["patch"].start()
            return True

        def start():
            try:
                ps.run([sys.executable, "-c", "pass"], str(self.root), dict(os.environ), None,
                       str(self.root / "l"), authorize, supervisor=[sys.executable, str(script)])
            except ProjectError as error:
                outcome["start"] = error.code
        starter = threading.Thread(target=start)
        starter.start()
        try:
            self.assertTrue(written.wait(10))
            managed = seen["managed"]
            wait_until(lambda: managed._confirmed.done, timeout=10)       # the supervisor is gone

            def stop():
                try:
                    managed.stop(wait=1.0)
                    outcome["stop"] = "confirmed"
                except ProjectError as error:
                    outcome["stop"] = error.code
            stopper = threading.Thread(target=stop)
            stopper.start()
            time.sleep(0.3)
            self.assertNotIn("stop", outcome)                              # waits for the writer
            release.set()
            stopper.join(10)
            starter.join(10)
            self.assertEqual(outcome["stop"], "project_processes_still_running")
            self.assertNotIn("start", outcome)                              # it may have launched
        finally:
            release.set()
            seen.get("patch") and seen["patch"].stop()
            wait_until(escaped.exists, timeout=10)
            wait_until(lambda: escaped.read_text().strip() != "", timeout=5)
            try:
                os.kill(int(escaped.read_text()), 9)
            except ProcessLookupError:
                pass
            if "managed" in seen:
                seen["managed"]._reap(0.0)

    def test_only_the_failure_of_the_confirmed_process_is_taken_over(self):
        item = self.save()
        confirmed, other = FakeProcess(), FakeProcess()
        active = pc._Attempt()
        active.process, active.finished, active.cleanup_failed, active.failed_process = confirmed, True, True, other
        with self.center._lock:
            self.center._active[item["id"]] = active
        result = {}

        def stop():
            try:
                self.center._stop_attempt(item["id"], active)
                result["done"] = True
            except ProjectError as error:
                result["error"] = error.code
        stopping = threading.Thread(target=stop)
        stopping.start()
        time.sleep(0.5)
        self.assertTrue(confirmed.stopped)
        self.assertIn(item["id"], self.center._active)         # another process's failure: not taken over
        self.assertEqual(result, {})
        with self.center._lock:
            active.failed_process = confirmed                    # now it is the one that failed
        stopping.join(10)
        self.assertEqual(result, {"done": True})
        self.assertNotIn(item["id"], self.center._active)
        self.assertEqual(self.get(item["id"])["status"], "paused")

    def test_a_scan_from_before_a_discovery_change_is_not_installed(self):
        self.add_runtime()
        item = self.approve(self.save(plan="A plan.", team=[{"agent": "fixture", "role": "lead",
                                                             "runtime": "runtime", "provider": "local-provider",
                                                             "model": "tiny-model"}]))
        gate, scanning = threading.Event(), threading.Event()
        stopped = [dict(self.runtimes[0], running=False)]

        def slow():
            scanning.set()
            gate.wait(10)
            return stopped
        self.center.runtime_profiles = slow
        self.center.discovery_generation += 1
        lister = threading.Thread(target=lambda: self.center.call("list", {}))
        lister.start()
        scanning.wait(5)
        self.center.runtime_profiles = lambda: self.runtimes        # fixed meanwhile
        self.center.discovery_generation += 1
        gate.set()
        lister.join(10)
        self.assertEqual(self.card(item["id"])["activity"], "ready_to_run")

    def test_a_closed_center_says_so_before_any_discovery(self):
        item = self.approve(self.save(plan="A plan."))
        self.center.shutdown()

        def broken():
            raise OSError("discovery failed")
        self.center.agent_profiles = broken
        self.assertCode("project_center_closed", self.center.call, "run",
                        {"id": item["id"], "revision": item["revision"]})

    def test_a_refused_launch_leaves_a_display_snapshot(self):
        item = self.approve(self.save(plan="A plan."))
        self.assertCode("project_changed", self.center.call, "run", {"id": item["id"], "revision": 999})
        self.assertIsNotNone(self.center._display)
        self.assertTrue(self.get(item["id"])["approval_valid"])


class SupervisorControlTest(unittest.TestCase):
    def silent(self, tmp):
        import subprocess
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], stdin=-1, stdout=-1,
                                cwd=tmp, start_new_session=True)
        return ps.SupervisedProcess(proc)

    def test_detach_during_a_send_still_closes_control(self):
        with tempfile.TemporaryDirectory() as tmp:
            managed = self.silent(tmp)
            managed._send_lock.acquire()          # a write in progress
            closing = threading.Thread(target=managed.detach)
            closing.start()
            time.sleep(0.1)
            managed._send_lock.release()
            closing.join(5)
            self.assertTrue(managed._proc.stdin.closed)
            managed._reap(0.0)

    def test_withdrawal_reaps_only_through_the_single_reaper(self):
        with tempfile.TemporaryDirectory() as tmp:
            managed = self.silent(tmp)
            original = managed._proc.wait

            def wait(*args, **kwargs):
                assert managed._reap_lock.locked(), "the supervisor was waited for outside the reaper"
                return original(*args, **kwargs)
            managed._proc.wait = wait
            managed._withdraw(1.0)
            self.assertIsNotNone(managed._proc.returncode)


class AdapterProjectTest(unittest.TestCase):
    def test_declared_profiles_load(self):
        claude = json.loads(adapter_data.load(ROOT / "adapters/claude.toml").project)
        self.assertEqual(claude["subagent_option"], "--agents")
        self.assertIn("reviewer", claude["subagent_definitions"])
        pi = json.loads(adapter_data.load(ROOT / "adapters/pi.toml").project)
        self.assertEqual(pi["access_modes"], ["read-only"])
        for command in ("agy", "codex"):
            facts = json.loads(adapter_data.load(ROOT / f"adapters/{command}.toml").project)
            self.assertEqual(facts["plan"][0], command)

    def test_unsafe_project_tables_are_refused(self):
        base = {"command": "x", "name": "X"}
        for project in ({"plan": ["sh", "-c", "{prompt}"]}, {"plan": ["x", "--", "{prompt}"]},
                        {"plan": ["x", "--p={prompt}"]}, {"run": ["x"]}, {"model_option": "-m"},
                        {"access_modes": ["everything"]}, {"surprise": True},
                        {"subagent_definitions": {"r": {"description": "d", "prompt": "p"}}}):
            with self.subTest(project=project), self.assertRaises(adapter_data.AdapterError):
                adapter_data.check({**base, "project": project}, "x.toml")

    def test_default_profiles_use_the_adapter_facts(self):
        import agents as agents_mod
        fake = [agents_mod.Agent("claude", "Claude Code", "", installed=True),
                agents_mod.Agent("gemini", "Gemini", "", installed=True)]
        with mock.patch.object(agents_mod, "list_agents", return_value=fake), \
                mock.patch("agent_details.details_for", side_effect=OSError), \
                mock.patch("online_models.choices", return_value=["claude-opus-5"]):
            found = {p["id"]: p for p in pc.default_agent_profiles()}
        self.assertEqual(found["gemini"]["project_supported"], False)
        self.assertEqual(found["claude"]["plan_argv"][0], "claude")
        self.assertEqual(found["claude"]["models"], ["claude-opus-5"])
        self.assertTrue(found["claude"]["native_subagents"])


if __name__ == "__main__":
    unittest.main()
