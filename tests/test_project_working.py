"""Show that the AI is working, Skip planning and the folder dialog's sidebar (Maprisma tasks 098, 099):
project_working's facts and output line, the footer's Skip planning and its flow on the controller,
folder_picker's sidebar list. Headless: unset DISPLAY / WAYLAND_DISPLAY; no network."""

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import folder_picker  # noqa: E402
import project_texts as texts  # noqa: E402
import project_working as pw  # noqa: E402
from test_projects_ui import ControllerCase, card  # noqa: E402

pw.ANIMATE = False


def member(phase, position, role):
    return {"phase": phase, "position": position, "agent": "claude", "name": "Claude Code", "role": role,
            "issues": []}


def working(status="planning", phase="planning", position=0, state="active", output=""):
    team = [member(phase, 0, "lead"), member(phase, 1, "reviewer")]
    return {**card("a", status=status, active={"phase": phase, "position": position, "state": state},
                   planning=team if phase == "planning" else [], execution=team if phase == "execution" else []),
            "phase": "running" if phase == "execution" else phase,
            "events": [{"at": "2026-11-23T10:00:00Z", "type": "started", "text": "Go."},
                       {"at": "2026-11-23T10:09:00Z", "type": "assignment", "text": "Claude Code: lead"},
                       {"at": "2026-11-23T10:10:00Z", "type": "message", "text": "Hi"}],
            "output": output}


def headline(item, project=None):
    work = pw.project_work(item, project if project is not None else item)
    return work and work["headline"]


class WorkTest(unittest.TestCase):
    def test_the_headline_names_what_the_agent_does(self):
        self.assertEqual(headline(working()), "AI is drafting the plan")
        self.assertEqual(headline(working(position=1)), "AI is checking the plan")
        self.assertEqual(headline(working("running", "execution")), "AI is working on the project")
        self.assertEqual(headline(working("reviewing", "execution", 1)), "AI is reviewing the changes")
        self.assertEqual(headline(working("pausing", state="stopping")), "Stopping the agent")
        self.assertEqual(headline({**working(), "phase": "setup", "active": None}), "AI is setting up the team")
        for state in ("stopped", "next"):
            self.assertIsNone(headline(working("paused", state=state)))
        self.assertIsNone(headline({**working("completed"), "active": None}))
        # Old processes still stopping, no active assignment.
        self.assertEqual(headline(None, {**working("paused"), "processes_stopping": True}), "Stopping the agent")

    def test_the_project_view_decides_the_card_only_says_who(self):
        stale = working("running", "execution")
        self.assertIsNone(headline(stale, {**stale, "status": "paused"}))
        ready = {**working("ready", "execution", state="next")}
        work = pw.project_work(ready, {**ready, "status": "planning", "phase": "planning"})
        self.assertEqual(work["headline"], "AI is drafting the plan")
        self.assertNotIn("step", work["detail"] or "")
        # The reviewer started after the board was read: not the lead's name.
        item = working()
        work = pw.project_work(item, {**item, "next_member": 1, "events": [
            {"at": "2026-11-23T10:11:00Z", "type": "assignment", "text": "Codex: reviewer"}]})
        self.assertEqual((work["headline"], work["detail"]), ("AI is checking the plan", "Codex · Reviewer"))

    def test_who_step_time_and_setup_helper(self):
        work = pw.project_work(working(), working())
        self.assertEqual(work["detail"], "Claude Code · Lead agent · step 1 of 2")
        now = datetime(2026, 11, 23, 10, 12, tzinfo=timezone.utc)
        self.assertEqual(pw.working_for(work["since"], now), "for 3 min")
        self.assertEqual(pw.working_for(work["since"], datetime(2026, 11, 23, 12, 14, tzinfo=timezone.utc)),
                         "for 2 h 5 min")
        project = {**working("pausing"), "phase": "setup", "setup_agent": "claude", "setup_output": "Choosing",
                   "events": [{"at": "2026-11-23T10:09:00Z", "type": "setup_started", "text": ""},
                              {"at": "2026-11-23T10:09:01Z", "type": "assignment", "text": "Claude Code: setup"}]}
        work = pw.project_work(None, project)
        self.assertEqual((work["headline"], work["detail"], work["output"]),
                         ("Stopping the agent", "claude", "Choosing"))
        named = pw.project_work({**working(), "setup_agent": {"agent": "claude", "name": "Claude Code"}},
                                {**project, "status": "planning"})
        self.assertEqual(named["detail"], "Claude Code")
        # The time counts from this attempt, not an older assignment.
        old = {**working("running", "execution", state="next"), "active": None, "events": [
            {"at": "2026-11-23T08:00:00Z", "type": "assignment", "text": "A: lead"},
            {"at": "2026-11-23T08:30:00Z", "type": "finished", "text": "Done."},
            {"at": "2026-11-23T10:11:30Z", "type": "started", "text": "Go."}]}
        work = pw.project_work(old, old)
        self.assertIsNone(work["key"])
        self.assertEqual(pw.working_for(work["since"], now), "just started")

    def test_output_line_without_terminal_codes(self):
        self.assertEqual(pw.last_output_line("Reading\n\x1b[32mWriting the plan\x1b[0m\r\n  \n"), "Writing the plan")
        self.assertEqual(pw.last_output_line("\x1b]0;Title\x1b\\See \x1b]8;;https://x\x07here\x1b]8;;\x07"),
                         "See here")
        self.assertIsNone(pw.last_output_line(" \n "))

    def test_output_of_the_previous_attempt_or_agent_is_not_shown(self):
        baseline = pw.OutputBaseline()

        def poll(item):
            return baseline.line(pw.project_work(item, item), item["id"])
        self.assertIsNone(poll(working(output="The previous agent said this")))
        self.assertEqual(poll(working(output="Inspecting\nComparing the routes")), "Comparing the routes")
        continued = {**working("running", "execution", state="next"), "output": "Old interrupted line",
                     "events": [{"at": "2026-11-23T10:11:00Z", "type": "continued", "text": ""}]}
        baseline = pw.OutputBaseline()
        self.assertIsNone(poll(continued))
        lead = {**continued, "events": continued["events"] + [
            {"at": "2026-11-23T10:11:05Z", "type": "assignment", "text": "Claude Code: lead"}],
            "output": "Inspecting the repo"}
        self.assertEqual(poll(lead), "Inspecting the repo")
        reviewer = {**lead, "active": {"phase": "execution", "position": 1, "state": "active"},
                    "events": lead["events"] + [{"at": "2026-11-23T10:11:30Z", "type": "assignment",
                                                 "text": "Codex: reviewer"}],
                    "output": "Inspecting the repo\nDone."}
        self.assertIsNone(poll(reviewer))
        self.assertEqual(poll({**reviewer, "output": "Reading the diff"}), "Reading the diff")

    def test_a_board_card_bar_only_while_an_agent_works(self):
        self.assertTrue(pw.working_card(working("running", "execution")))
        self.assertFalse(pw.working_card(working("paused", state="stopped")))
        self.assertTrue(pw.working_card(card("d", status="ready", activity="stopping", processes_stopping=True)))
        self.assertFalse(pw.working_card(card("c")))


class SkipPlanningTest(ControllerCase):
    def open(self, ctrl, pid):
        ctrl.open_card(next(p for p in ctrl.projects if p["id"] == pid))
        self.runner.flush()

    def actions(self, project, plan="", running=False):
        return {op: on for _t, op, on in texts.footer_actions(project, plan, False, running, False, True, True)}

    def test_offered_only_without_a_plan_and_nothing_to_wait_for(self):
        self.assertTrue(self.actions(card("a"))["skip_plan"])
        self.assertFalse(self.actions(card("a"), plan="1. Do it.")["skip_plan"])
        self.assertFalse(self.actions(card("a", processes_stopping=True))["skip_plan"])
        self.assertFalse(self.actions(card("a", needs_reconciliation=True))["skip_plan"])
        self.assertFalse(self.actions(card("a", status="paused"))["skip_plan"])
        self.assertFalse(self.actions(card("a", status="running"), running=True)["skip_plan"])
        no_team = {op: on for _t, op, on in texts.footer_actions(card("a"), "", False, False, False, True, False)}
        self.assertFalse(no_team["skip_plan"])

    def test_asks_then_saves_the_note_approves_and_runs(self):
        ctrl = self.make([card("a")])
        self.open(ctrl, "a")
        self.questions.answers.append(False)
        ctrl.perform("skip_plan")
        self.runner.flush()
        self.assertEqual(self.questions.asked[-1][1], texts.SKIP_QUESTION)
        self.assertFalse(self.ops("save"))
        self.assertFalse(ctrl.busy)
        self.questions.answers.append(True)
        ctrl.perform("skip_plan")
        self.runner.flush()
        self.assertEqual([op for op, _p in self.center.calls if op in ("save", "review", "run")],
                         ["save", "review", "run"])
        self.assertEqual(self.ops("save")[-1][1]["plan"], texts.SKIPPED_PLAN)
        self.assertTrue(self.ops("review")[-1][1]["approved"])
        self.assertEqual(ctrl.tab, texts.TAB_ACTIVITY)
        self.assertFalse(ctrl.busy)

    def test_a_failed_save_leaves_the_form_as_it_was(self):
        ctrl = self.make([card("a")])
        self.open(ctrl, "a")
        self.center.fail["save"] = "project_folder_unavailable"
        self.questions.answers.append(True)
        ctrl.perform("skip_plan")
        self.runner.flush()
        self.assertEqual(ctrl.texts["plan"], "")
        self.assertFalse(ctrl.dirty)
        self.assertFalse(self.ops("review"))
        self.assertTrue(ctrl.error)

    def test_a_refused_run_keeps_its_reason_and_reads_the_board_again(self):
        ctrl = self.make([card("a")])
        self.open(ctrl, "a")
        self.center.fail["run"] = "project_folder_busy"
        self.questions.answers.append(True)
        reads = len(self.ops("list"))
        ctrl.perform("skip_plan")
        self.runner.flush()
        self.assertEqual(ctrl.error, "Another project is working in this folder.")
        self.assertGreater(len(self.ops("list")), reads + 1)
        self.assertFalse(ctrl.busy)


class FolderShortcutsTest(unittest.TestCase):
    MOUNTS = "\n".join([
        "/dev/nvme0n1p2 / btrfs rw 0 0", "/dev/nvme0n1p2 /home btrfs rw 0 0", "/dev/nvme0n1p1 /boot vfat rw 0 0",
        "/dev/nvme0n1p3 /mnt/Data ext4 rw 0 0", "/dev/nvme1n1p3 /mnt/Games ext4 rw 0 0",
        "/dev/mapper/omarchy /mnt/omarchy-root btrfs rw 0 0", "/dev/nvme1n1p1 /mnt/omarchy-root/boot vfat rw 0 0",
        "nas:/export /mnt/Nas nfs4 rw 0 0", "/dev/sda1 /run/media/uwe/My\\040Stick vfat rw 0 0",
        "tmpfs /mnt/ram tmpfs rw 0 0", "proc /proc proc rw 0 0"])
    COSMIC = '[\n    Home,\n    Path("/mnt/Data"),\n    Path("/mnt/Data/Ai"),\n    Path("/gone"),\n]'
    KDE = """
<bookmark href="file:///home/uwe"><title>Home</title></bookmark>
<bookmark href="file:///mnt/Data/Projects"><title>Projects</title></bookmark>
<bookmark href="file:///home/uwe/My%20Work/"><title>Work</title></bookmark>
<bookmark href="trash:/"><title>Trash</title></bookmark>
<bookmark href="file:///mnt/Data/Old">
  <title>Old</title><info><metadata><isHidden>true</isHidden></metadata></info>
</bookmark>
<bookmark href="file:///mnt/Data/Uwe&apos;s"><title>Mine</title></bookmark>
"""

    def test_mounted_data_disks_first_then_pinned_folders_once_each(self):
        found = folder_picker.folder_shortcuts(self.MOUNTS, "/home/uwe/", self.COSMIC, self.KDE,
                                               is_dir=lambda p: p != "/gone",
                                               is_system_root=lambda p: p == "/mnt/omarchy-root")
        self.assertEqual(found, ["/mnt/Data", "/mnt/Games", "/mnt/Nas", "/run/media/uwe/My Stick", "/mnt/Data/Ai",
                                 "/mnt/Data/Projects", "/home/uwe/My Work", "/mnt/Data/Uwe's"])

    def test_nothing_to_add_without_mounts_or_pins(self):
        self.assertEqual(folder_picker.folder_shortcuts("", "/home/uwe", is_dir=lambda _p: True), [])


if __name__ == "__main__":
    unittest.main()
