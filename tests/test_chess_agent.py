import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import adapter_data  # noqa: E402
import chess_agent  # noqa: E402
import chess_rules as rules  # noqa: E402
import i18n  # noqa: E402


class Prompt(unittest.TestCase):
    def setUp(self):
        i18n._lang = "en"
        self.addCleanup(setattr, i18n, "_lang", None)

    def test_first_move_and_later_moves(self):
        start = rules.start()
        text = chess_agent.prompt(start, [], "w")
        self.assertIn("You play White, I play Black.", text)
        self.assertIn("You make the first move.", text)
        self.assertIn(rules.START_FEN, text)
        e4 = rules.parse(start, "e4")
        text = chess_agent.prompt(start, [e4], "b", "de")
        self.assertIn("Moves so far: 1. e4", text)
        self.assertIn("My last move: e4", text)
        self.assertIn("Nf6", text.split("Your legal moves:")[1])
        self.assertIn("in German", text)

    def test_retry_quotes_the_answer_briefly(self):
        text = chess_agent.retry_prompt("P", "hello\n" * 200)
        self.assertTrue(text.startswith("P\n\n"))
        self.assertLess(len(text), 400)


class Asking(unittest.TestCase):
    def setUp(self):
        i18n._lang = "en"
        self.addCleanup(setattr, i18n, "_lang", None)
        start = rules.start()
        self.pos = rules.apply(start, rules.parse(start, "e4"))

    def fake(self, answer: bytes, ok=True):
        seen = {}

        def run(argv, max_bytes, timeout, cwd, env):
            seen.update(argv=argv, cwd=cwd, mode=os.stat(cwd).st_mode & 0o777, empty=not os.listdir(cwd),
                        max_bytes=max_bytes, timeout=timeout)
            return answer, ok
        return run, seen

    def test_the_prompt_is_one_argument_in_a_private_empty_folder(self):
        run, seen = self.fake(b"e5\nThe classical answer: I claim the centre.")
        reply = chess_agent.ask("claude", "a prompt; with $(rm -rf ~) and \"quotes\"", self.pos, runner=run)
        self.assertEqual(seen["argv"][0], "claude")
        self.assertEqual(seen["argv"].count("a prompt; with $(rm -rf ~) and \"quotes\""), 1)
        self.assertEqual(seen["mode"], 0o700)
        self.assertTrue(seen["empty"])
        self.assertFalse(Path(seen["cwd"]).exists(), "the folder is removed afterwards")
        self.assertEqual(rules.san(self.pos, reply.move), "e5")
        self.assertEqual(reply.comment, "The classical answer: I claim the centre.")
        self.assertEqual(reply.error, "")

    def test_answers_without_a_move_or_at_all(self):
        run, _seen = self.fake(b"I would rather not play.")
        reply = chess_agent.ask("claude", "p", self.pos, runner=run)
        self.assertIsNone(reply.move)
        self.assertEqual(reply.comment, "I would rather not play.")
        run, _seen = self.fake(b"", ok=False)
        self.assertIn("took too long", chess_agent.ask("claude", "p", self.pos, runner=run).error)
        self.assertIn("cannot be asked", chess_agent.ask("no-such-agent", "p", self.pos, runner=run).error)

    def test_the_comment_drops_only_a_move_line(self):
        self.assertEqual(chess_agent.comment_of("**Nf6**\nDeveloping.", "Nf6"), "Developing.")
        self.assertEqual(chess_agent.comment_of("I play Nf6 to develop.", "Nf6"), "I play Nf6 to develop.")
        self.assertLessEqual(len(chess_agent.comment_of("x" * 5000, "")), chess_agent.MAX_COMMENT + 1)


class WhoCanPlay(unittest.TestCase):
    def test_only_installed_set_up_agents_with_an_ask_command(self):
        agent = SimpleNamespace(command="claude", installed=True)
        has_it = bool(chess_agent.ask_argv("claude")) and bool(__import__("shutil").which("claude"))
        self.assertEqual(chess_agent.can_play(agent, True), has_it)
        self.assertFalse(chess_agent.can_play(agent, False))
        self.assertFalse(chess_agent.can_play(SimpleNamespace(command="claude", installed=False), True))
        self.assertFalse(chess_agent.can_play(SimpleNamespace(command="opencode", installed=True), True))

    def test_an_adapter_cannot_ask_another_program_or_a_shell(self):
        base = {"command": "x", "name": "X"}
        for argv in (["sh", "-c", "{prompt}"], ["x", "--flag={prompt}"], ["x"], ["x", "{prompt}", "{prompt}"]):
            with self.assertRaises(adapter_data.AdapterError, msg=argv):
                adapter_data.check({**base, "ask": {"argv": argv}}, "x.toml")
        adapter_data.check({**base, "ask": {"argv": ["x", "-p", "{prompt}"], "games": ["chess"]}}, "x.toml")
        with self.assertRaises(adapter_data.AdapterError):
            adapter_data.check({**base, "ask": {"argv": ["x", "{prompt}"], "games": ["poker"]}}, "x.toml")

    def test_thinking_time_sets_the_options_the_deadline_and_the_strength(self):
        argv = chess_agent.run_argv("claude", "PROMPT", "deep")
        self.assertEqual(argv[-3:], ["--effort", "high", "PROMPT"])
        self.assertEqual(chess_agent.run_argv("pi", "P", "quick")[-3:], ["--thinking", "low", "P"])
        self.assertEqual(chess_agent.run_argv("codex", "P", "max")[-3:], ["-c", "model_reasoning_effort=high", "P"])
        seen = {}

        def run(argv, max_bytes, timeout, cwd, env):
            seen["timeout"] = timeout
            return b"e5", True
        start = rules.start()
        pos = rules.apply(start, rules.parse(start, "e4"))
        chess_agent.ask("claude", "p", pos, runner=run, level="quick")
        quick = seen["timeout"]
        chess_agent.ask("claude", "p", pos, runner=run, level="max")
        self.assertLess(quick, seen["timeout"])
        self.assertIn("as strongly as you can", chess_agent.prompt(start, [], "w", level="max"))
        self.assertIn("casual club player", chess_agent.prompt(start, [], "w", level="quick"))

    def test_effort_goes_where_the_adapter_says(self):
        # Hermes takes the prompt right after -z: its adapter puts the effort options at {effort}.
        self.assertEqual(chess_agent.run_argv("hermes", "P", "deep"),
                         ["hermes", "--reasoning", "high", "-t", "todo", "--ignore-rules", "-z", "P"])
        self.assertTrue(chess_agent.plays("hermes"))
        base = {"command": "x", "name": "X"}
        for argv in (["x", "{effort}", "{effort}", "{prompt}"], ["x", "--e={effort}", "{prompt}"]):
            with self.assertRaises(adapter_data.AdapterError, msg=argv):
                adapter_data.check({**base, "ask": {"argv": argv}}, "x.toml")

    def test_effort_options_are_checked(self):
        base = {"command": "x", "name": "X"}
        for effort in ({"forever": ["--effort", "max"]}, {"deep": ["high"]}, {"deep": ["--x", "{prompt}"]},
                       {"deep": []}):
            with self.assertRaises(adapter_data.AdapterError, msg=effort):
                adapter_data.check({**base, "ask": {"argv": ["x", "{prompt}"], "effort": effort}}, "x.toml")

    def test_the_adapter_says_which_agents_play(self):
        self.assertTrue(chess_agent.plays("claude"))
        self.assertTrue(chess_agent.plays("pi"))
        self.assertFalse(chess_agent.plays("opencode"))
        self.assertFalse(chess_agent.plays("claude", "poker"))


if __name__ == "__main__":
    unittest.main()
