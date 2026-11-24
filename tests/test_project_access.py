"""Project access (project_access.py, Maprisma task 104): what a plan needs, the gaps of a team's
access, the steps a run left undone, the capabilities an adapter declares per level. Pure functions:
no display, no network."""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import project_access as pa  # noqa: E402

FACTS = ("edit", "shell", "network", "outside_project", "git_commit", "git_push")


def facts(*on) -> dict:
    return {fact: fact in on for fact in FACTS}


def member(agent="fixture", level="project-edit", modes=("read-only", "project-edit", "project-commands"),
           push=True) -> dict:
    """A Codex-like member: Edit files can't commit, Edit + commands can, push only in the push variant."""
    capabilities = {"read-only": facts(), "project-edit": facts("edit", "shell"),
                    "project-commands": facts("edit", "shell", "outside_project", "git_commit")}
    if push:
        capabilities["project-commands-push"] = facts("edit", "shell", "network", "outside_project", "git_commit",
                                                      "git_push")
    return {"agent": agent, "level": level, "modes": list(modes), "capabilities": capabilities}


class FencedBlockTest(unittest.TestCase):
    def test_the_last_block_of_that_name_wins_in_any_case(self):
        text = "```needs\nedit\n```\nmore\n  ```NEEDS \r\ntest\npush\n  ```\n```other\nx\n```"
        self.assertEqual(pa.last_fenced_block(text, "needs"), "test\npush\n")
        self.assertIsNone(pa.last_fenced_block("no block here", "needs"))
        self.assertIsNone(pa.last_fenced_block("```needsmore\nedit\n```", "needs"))
        self.assertEqual(pa.last_fenced_block("```needs\n```", "needs"), "")

    def test_block_items_drop_list_marks_and_the_words_for_nothing(self):
        self.assertEqual(pa.block_items("- edit\n* test\n• build\n1. install\n2) commit\n\n"),
                         ["edit", "test", "build", "install", "commit"])
        self.assertEqual(pa.block_items("edit, test\npush", "\n,"), ["edit", "test", "push"])
        # Review round 1: "None" in any case (with a final period) is no item.
        for empty in ("None", "- None", "NONE", "nothing.", "N/A", "na", "-", "All done!", "Nothing left",
                      "(none)"):
            with self.subTest(empty=empty):
                self.assertEqual(pa.block_items(empty), [])


class PlanNeedsTest(unittest.TestCase):
    def test_the_plan_block_wins(self):
        plan = "1. Change the code.\n2. Test it.\n\n```needs\nedit\ntest\ninstall\ncommit, push\n```\n"
        self.assertEqual(pa.plan_needs(plan, "Download it with curl."),
                         (["edit", "test", "install", "commit", "push"], "plan"))
        # Unknown words are ignored; display order, not the block's order; "none" is an empty list.
        self.assertEqual(pa.plan_needs("```needs\nPUSH\nedit\nfly\n```", ""), (["edit", "push"], "plan"))
        self.assertEqual(pa.plan_needs("Look around.\n```needs\nnone\n```", "Fix it."), ([], "plan"))

    def test_keywords_without_a_block(self):
        self.assertEqual(pa.plan_needs("Fix the bug, run cargo test, then install, commit and push.", ""),
                         (["edit", "test", "install", "commit", "push"], "keywords"))
        self.assertEqual(pa.plan_needs("", "Download the data with curl."), (["network"], "keywords"))
        self.assertEqual(pa.plan_needs("", "  "), ([], "none"))

    def test_build_and_make_alone_are_ordinary_words(self):
        """Review round 1: "make" and "build" alone don't ask for shell access."""
        self.assertEqual(pa.plan_needs("Make the header bold, then build the app.", "")[0], ["build"])
        self.assertEqual(pa.plan_needs("Make the header bold, then build the app.",
                                       "Build the requested change and verify it.")[0], ["edit", "build"])
        self.assertEqual(pa.plan_needs("", "Build the requested change and verify it.")[0], ["edit"])
        self.assertEqual(pa.plan_needs("Make sure it remembers the size.", "")[0], [])
        for command in ("cargo build", "flutter build", "npm run build", "make build", "make install", "compile",
                        "makepkg", "build the project", "build it"):
            with self.subTest(command=command):
                self.assertIn("build", pa.plan_needs(command, "")[0])


class AccessGapsTest(unittest.TestCase):
    def test_gaps_name_the_lowest_level_that_covers_them(self):
        needs = ["edit", "test", "install", "commit", "push"]
        gaps = {gap["need"]: gap for gap in pa.access_gaps(needs, [member()], False)}
        self.assertEqual(list(gaps), ["install", "commit", "push"])
        self.assertEqual(gaps["install"], {"need": "install", "members": ["fixture"],
                                           "suggest_access": "project-commands", "needs_allow_push": False})
        self.assertFalse(gaps["commit"]["needs_allow_push"])
        self.assertEqual((gaps["push"]["suggest_access"], gaps["push"]["needs_allow_push"]),
                         ("project-commands", True))

    def test_a_member_at_its_level_covers_and_allow_push_counts(self):
        self.assertEqual([g["need"] for g in pa.access_gaps(["commit", "push"], [member(level="project-commands")],
                                                             False)], ["push"])
        self.assertEqual(pa.access_gaps(["commit", "push"], [member(level="project-commands-push")], True), [])
        # With Allow push on, a push gap at Edit files is project-commands without needing it again.
        gap = pa.access_gaps(["push"], [member()], True)[0]
        self.assertEqual((gap["suggest_access"], gap["needs_allow_push"]), ("project-commands", False))

    def test_the_edit_level_first_and_none_when_nobody_can(self):
        reader = member("reader", "read-only")
        gap = pa.access_gaps(["edit"], [reader], False)[0]
        self.assertEqual((gap["suggest_access"], gap["members"]), ("project-edit", ["reader"]))
        # Without Edit + commands in its modes, nothing above Edit files is suggested.
        plain = member("plain", modes=("read-only", "project-edit"))
        self.assertEqual(pa.access_gaps(["commit"], [plain], False),
                         [{"need": "commit", "members": [], "suggest_access": None, "needs_allow_push": False}])
        # No push variant: the push need falls back to project-commands' facts (no git_push there).
        self.assertIsNone(pa.access_gaps(["push"], [member(push=False)], False)[0]["suggest_access"])
        # No members: every need is a gap no agent can fill.
        self.assertEqual([g["suggest_access"] for g in pa.access_gaps(["edit", "test"], [], False)], [None, None])

    def test_two_members_list_each_able_agent_once(self):
        gaps = pa.access_gaps(["install"], [member("a"), member("b"), member("a")], False)
        self.assertEqual(gaps[0]["members"], ["a", "b"])


class NotDoneTest(unittest.TestCase):
    def test_the_last_not_done_block_bounded(self):
        output = "```not_done\nold\n```\nDone.\n```not_done\n- cargo test: denied\n- install: not allowed\n```"
        self.assertEqual(pa.not_done_items(output), ["cargo test: denied", "install: not allowed"])
        self.assertEqual(pa.not_done_items("Done."), [])
        self.assertEqual(pa.not_done_items("```not_done\nNone\n```"), [])
        self.assertEqual(pa.not_done_items("```not_done\n```"), [])
        many = "```not_done\n" + "\n".join(f"step {i}" for i in range(30)) + "\n```"
        self.assertEqual(len(pa.not_done_items(many)), 20)
        self.assertEqual(len(pa.not_done_items(many, limit=3)), 3)
        long = pa.not_done_items("```not_done\n" + "x" * 400 + "\n```")[0]
        self.assertEqual((len(long), long[-1]), (300, "…"))


class CapabilitiesTest(unittest.TestCase):
    def test_every_fact_present_and_levels_only_with_their_commands(self):
        project = {"run": ["a"], "capabilities": {"project-edit": {"edit": True}}}
        levels = pa.project_capabilities(project)
        self.assertEqual(set(levels), {"read-only", "project-edit"})
        self.assertEqual(levels["project-edit"], facts("edit"))
        self.assertEqual(levels["read-only"], facts())
        project.update(run_commands=["a"], capabilities={"project-commands": {"shell": True, "git_commit": True}})
        self.assertEqual(set(pa.project_capabilities(project)), {"read-only", "project-edit", "project-commands"})
        # The push level falls back to project-commands' facts.
        project["run_commands_push"] = ["a"]
        self.assertEqual(pa.project_capabilities(project)["project-commands-push"], facts("shell", "git_commit"))
        self.assertEqual(pa.project_capabilities({})["project-edit"], facts())

    def test_capability_text(self):
        self.assertEqual(pa.capability_text(facts("edit", "git_push")),
                         "edit files: yes; run commands: no; network: no; install outside the project: no; "
                         "git commit: no; git push: yes")

    def test_the_lists_match_the_adapter_check(self):
        import adapter_data
        self.assertEqual(pa.PROJECT_ACCESS, adapter_data.PROJECT_ACCESS)
        self.assertEqual(pa.CAPABILITY_LEVELS, adapter_data.PROJECT_CAPABILITY_LEVELS)
        self.assertEqual(pa.CAPABILITIES, adapter_data.PROJECT_CAPABILITIES)
        self.assertEqual(set(pa.NEED_CAPABILITY), set(pa.PROJECT_NEEDS))
        self.assertTrue(set(pa.NEED_CAPABILITY.values()) <= set(pa.CAPABILITIES))


if __name__ == "__main__":
    unittest.main()
