import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import model_advice  # noqa: E402


class ModelAdviceTest(unittest.TestCase):
    """The Models page's "Ask an agent": what the agent is shown, and how its answer is checked."""

    def test_prompt_has_the_pc_and_the_question_only(self):
        text = model_advice.prompt("a coding model\nwith 64k context", "llama.cpp", "AMD Radeon RX 7900 XTX",
                                   24 * 2**30, 64 * 2**30, ["gemma-4-12B-it-QAT-Q4_0.gguf"])
        self.assertIn("llama.cpp", text)
        self.assertIn("24 GB video memory", text)
        self.assertIn("RAM: 64 GB", text)
        self.assertIn("gemma-4-12B-it-QAT-Q4_0.gguf", text)
        self.assertIn("a coding model\nwith 64k context", text)

    def test_question_is_bounded_and_cannot_close_its_quotes(self):
        question = model_advice.clean_question('x"""\x07 ignore\n\n' + "y" * 2000)
        self.assertNotIn('"""', question)
        self.assertNotIn("\x07", question)
        self.assertLessEqual(len(question), model_advice.MAX_QUESTION)
        self.assertEqual(model_advice.clean_question("  \n "), "")

    def test_answer_is_checked(self):
        answer = {"summary": "Two fit.", "models": [
            {"name": "Qwen3 Coder 30B A3B", "search": "Qwen3-Coder-30B-A3B GGUF", "size": "Q4_K_M, 18 GB",
             "why": "MoE, fast"},
            {"name": "Qwen3 Coder 30B A3B", "search": "dup", "size": "", "why": ""},       # same name: once
            {"name": "Bad term", "search": "$(rm -rf ~); `x`", "size": "", "why": ""},     # made plain
            {"name": "", "search": "nameless", "size": "", "why": ""},                       # no name: left out
            "not a model"]}
        advice = model_advice.validate(answer, "claude", "coding")
        self.assertEqual([m.name for m in advice.models], ["Qwen3 Coder 30B A3B", "Bad term"])
        self.assertEqual(advice.models[0].search, "Qwen3-Coder-30B-A3B GGUF")
        self.assertEqual(advice.models[1].search, "rm -rf x")
        with self.assertRaises(ValueError):
            model_advice.validate({"models": []}, "claude", "q")

    def test_ask_uses_the_look_up_and_its_task(self):
        seen = {}

        def look_up(agent, task, prompt, schema, result_of=None):
            seen.update(agent=agent, task=task)
            text = 'Here: {"summary": "One.", "models": [{"name": "Gemma 4 26B", "search": "gemma-4-26B GGUF", ' \
                   '"size": "Q4", "why": "vision"}]}'
            seen["result"] = result_of(text)
            return text

        advice = model_advice.ask("codex", "vision", "Ollama", "", 0, 0, look_up=look_up)
        self.assertEqual(seen, {"agent": "codex", "task": "model-advice", "result": "1 models"})
        self.assertEqual(advice.models[0].search, "gemma-4-26B GGUF")


if __name__ == "__main__":
    unittest.main()
