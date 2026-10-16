import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import agent_details as ad  # noqa: E402
import agent_status  # noqa: E402
import agents  # noqa: E402
import graph  # noqa: E402
import health  # noqa: E402
import inventory as invmod  # noqa: E402

GiB = 1024 ** 3


def setup(models=("Qwen3-27B-Q4_K_M",), mcp=(), skills=(), vram=24 * GiB, free=500 * GiB, signed_in=True):
    claude = agents.Agent(command="claude", name="Claude Code", icon="c", installed=True)
    pi = agents.Agent(command="pi", name="Pi", icon="p", installed=True)
    details = {"claude": ad.Details(model="opus"),
               "pi": ad.Details(model=f"{models[0]} (local)" if models else "",
                                providers=[ad.Provider("local", True, "llama.cpp", len(models))])}
    rt = invmod.Runtime("llamacpp", "llama.cpp", True, True, address="127.0.0.1:8080", models=list(models),
                        models_dir="/m/llama", agents=["pi"], active_agents=["pi"])
    inv = invmod.Inventory(runtimes=[rt], mcp=[invmod.McpServer(n, "command", n, ["claude"]) for n in mcp],
                           skill_sets=[invmod.SkillSet("~/.agents/skills", list(skills), ["claude"])] if skills else [])
    built = graph.build([claude, pi], details, inv, {"folders": "+{0}".format})
    checks = {"claude": [agent_status.Check("ok", "Signed in")],
              "pi": [agent_status.Check("ok" if signed_in else "missing", "Signed in" if signed_in else "Not signed in",
                                        step="pi login")]}
    pc = health.PC("amd", "x [Radeon RX 7900 XT/7900 XTX]", vram, 64 * GiB, 16, {"/m/llama": free})
    return [claude, pi], details, inv, built, pc, checks


def run(*args, path_has=lambda _name: None, **kwargs):
    agent_list, details, inv, built, pc, checks = setup(*args, **kwargs)
    return health.check(agent_list, details, inv, built, pc, checks, {}, {}, path_has=path_has)


class HealthTest(unittest.TestCase):
    def use(self, report, key):
        return next(u for u in report.uses if u.key == key)

    def test_good_setup(self):
        report = run(models=("Qwen3-Coder-30B-Q4_K_M", "gemma-4-12B-it-Q4_0", "whisper-large-v3"),
                     mcp=("chrome-devtools", "photocraft"), skills=("pdf", "release"))
        self.assertEqual(report.warn, 0)
        self.assertEqual(self.use(report, "coding").level, "ready")
        self.assertIn("Qwen3-Coder-30B-Q4_K_M", self.use(report, "coding").have)
        self.assertEqual(self.use(report, "images").level, "ready")
        self.assertEqual(self.use(report, "voice").have, ["whisper-large-v3"])
        self.assertIn("chrome-devtools", self.use(report, "research").have)
        self.assertEqual(health.card_name(health.PC("amd", "x [Radeon RX 7900 XT/7900 XTX]")), "AMD Radeon RX 7900 XT")

    def test_tips_with_actions(self):
        report = run(models=("Qwen3-27B-Q4_K_M",))
        coding = self.use(report, "coding")
        self.assertEqual(coding.level, "almost")
        self.assertEqual(coding.steps[0].action, ("models", "llamacpp", "coder"))
        self.assertEqual([s.action for s in self.use(report, "images").steps],
                         [("addmcp", "image"), ("models", "llamacpp", "vl")])
        tools = next(a for a in report.areas if a.key == "tools")
        self.assertIn(("addmcp", ""), [f.action for f in tools.findings])
        self.assertGreater(report.hint, 0)

    def test_things_to_fix(self):
        report = run(free=10 * GiB, signed_in=False)
        self.assertEqual(report.level, "warn")
        found = [(f.level, f.title, f.action) for a in report.areas for f in a.findings if f.level == "warn"]
        self.assertIn(("warn", "Pi: Not signed in", ("details", "pi")), found)
        self.assertTrue(any(f[2] == ("reorganize",) for f in found))      # little disk space

    def test_small_card_and_no_card(self):
        small = run(vram=6 * GiB)
        self.assertEqual(next(a for a in small.areas if a.key == "pc").findings[0].level, "hint")
        none = run(vram=0)
        self.assertNotIn("models", [s.action[:1] and s.action[0] for s in self.use(none, "coding").steps])  # no room

    def test_same_model_counts_once(self):
        self.assertEqual(health.matches(health.CODER, ["Tiel-Coder-35B", "tiel-coder-35b:latest"]), ["Tiel-Coder-35B"])
        self.assertEqual(health.matches(health.IMAGE_TOOL, ["gridcraft", "photocraft"]), ["photocraft"])


if __name__ == "__main__":
    unittest.main()
