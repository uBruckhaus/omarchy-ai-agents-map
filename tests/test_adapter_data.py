import dataclasses
import json
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import adapter_data  # noqa: E402
import agent_security  # noqa: E402
import harnesses  # noqa: E402

CALLABLES = {"sessions", "access", "security", "latest", "installer", "detect", "usage", "limits", "local_llm",
             "skills", "mcp", "details", "models"}       # skills and mcp hold functions (listed, write): compared by what they do
# Settings files with values set, so the security readers have something to read.
FIXTURES = {
    ".claude/settings.json": json.dumps({"permissions": {"defaultMode": "acceptEdits", "allow": ["Bash(git status)"],
                                                         "deny": ["Read(.env)"]},
                                         "sandbox": {"enabled": True}}),
    # Copilot: the lists only in its managed file (the fallback).
    ".copilot/settings.json": json.dumps({"defaultPermissionMode": "assisted"}),
    ".copilot/config.json": json.dumps({"trustedFolders": ["/home/u/work"], "allowedUrls": ["github.com"]}),
    ".codex/config.toml": 'approval_policy = "never"\n[sandbox_workspace_write]\nnetwork_access = true\n'
                          '[projects."/home/u/a"]\ntrust_level = "trusted"\n[projects."/home/u/b"]\n'
                          'trust_level = "untrusted"\n',
    ".config/cursor/cli-config.json": json.dumps({"approvalMode": "unrestricted", "sandbox": {"mode": "enabled"},
                                                  "permissions": {"allow": ["ls"]}}),
    # Folder lists of the skills readers.
    ".omp/agent/settings.json": json.dumps({"skills": ["~/x/skills"]}),
    ".config/crush/crush.json": json.dumps({"options": {"skills_paths": ["~/y/skills"]}}),
    ".grok/config.toml": '[skills]\npaths = ["~/z/skills"]\n',
    ".hermes/config.yaml": "approvals:\n  mode: off\nsecurity:\n  redact_secrets: false\nterminal:\n  backend: docker\n"
                           "skills:\n  external_dirs:\n    - ~/h/skills\n",
    ".pi/agent/settings.json": json.dumps({"defaultTools": ["read", "grep", "find", "ls"], "skills": ["~/p/skills"]}),
    ".openclaw/openclaw.json": json.dumps({"skills": {"load": {"extraDirs": ["~/o/skills"]}}}),
    ".config/opencode/opencode.json": json.dumps({"permission": {"edit": "ask", "bash": {"*": "allow"},
                                                                 "webfetch": {"git *": "allow", "*": "ask"}}}),
}
MCP_SAMPLE = json.dumps({"mcp_servers": {"old": {"command": "a"}}})


SNAPSHOTS = ROOT / "tests/adapter_snapshots"


def snapshot(harness, home: Path) -> dict:
    """Everything an adapter yields in the fixture home, as plain JSON (paths relative to that home):
    its facts, sign-in, security report, folder lists and MCP edits. A TOML file is checked against
    the snapshot of what it yielded when it was approved (made from the Python adapter it replaced).
    After an intended change: python3 tests/test_adapter_data.py --update-snapshots"""
    def plain(value):
        text = json.dumps(value, default=lambda o: dataclasses.asdict(o) if dataclasses.is_dataclass(o) else str(o),
                          ensure_ascii=False)
        return json.loads(text.replace(str(home), "~"))
    out = {f.name: plain(getattr(harness, f.name)) for f in dataclasses.fields(harnesses.Harness)
           if f.name not in CALLABLES}
    out["access"] = plain(harness.access()) if harness.access else None
    if harness.security:
        report = agent_security.Report(harness.command, True)
        harness.security(report)
        out["security"] = plain({"settings": report.settings, "rules": report.rules, "files": report.files,
                                 "note": report.note})
    else:
        out["security"] = None
    def attempt(function, *args):
        try:
            return plain(function(*args))
        except Exception as error:                 # an error is a result too (same error, same text)
            return f"error: {type(error).__name__}: {error}"
    def reader(function) -> str:
        """A reader the app has by its name; the adapter's own (module or hook) as "own"."""
        module = getattr(function, "__module__", "") or ""
        return "own" if module.startswith(("harness_", "adapter_hooks_")) else name_of(function)
    out["sessions"] = {"collect": reader(harness.sessions.collect), "note": harness.sessions.note()} \
        if harness.sessions else None
    out["usage"] = name_of(harness.usage) if harness.usage else None
    out["latest"] = name_of(harness.latest) if harness.latest else None
    out["installer"] = type(harness.installer()).__name__ if harness.installer else None
    for field in ("detect", "limits", "details"):
        out[field] = getattr(harness, field) is not None
    out["models"] = [name_of(f) for f in harness.models] if harness.models else None
    llm = harness.local_llm
    if llm is None:
        out["local_llm"] = None
    else:
        texts = [""] * len(llm.files)
        added = llm.transform(texts, "llamacpp", "llama.cpp", "http://127.0.0.1:8080/v1", ["m1", "m2"],
                              {"m1": 8192}, "m1", True, False)
        out["local_llm"] = {"files": list(llm.files), "added": plain(added),
                            "connected": llm.connected(added, "llamacpp"), "active": llm.active(added, "llamacpp"),
                            "removed": attempt(llm.transform, added, "llamacpp", "llama.cpp",
                                               "http://127.0.0.1:8080/v1", ["m1", "m2"], {"m1": 8192}, "m1",
                                               True, True)}
    sk = harness.skills
    out["skills"] = None if sk is None else {
        "dirs": list(sk.dirs), "own": sk.own, "settings_list": sk.settings_list, "settings_file": sk.settings_file,
        "listed": attempt(sk.listed) if sk.listed else None,
        "write": [attempt(sk.write, "", "/home/u/skills", False),
                  attempt(sk.write, "skills:\n  external_dirs:\n    - /home/u/skills\n", "/home/u/skills", True),
                  attempt(sk.write, '{"skills": ["/home/u/skills"]}', "/home/u/skills", True)] if sk.write else None}
    m = harness.mcp
    sample = "" if m is not None and m.format == "yaml" else MCP_SAMPLE
    out["mcp"] = None if m is None else {
        "path": m.path, "format": m.format, "key": m.key, "shape": m.shape, "in_app": m.in_app,
        "write": [attempt(m.write, sample, "new", item) for item in ({"command": "b"}, None)] if m.write else None,
        "read": attempt(m.read, sample) if m.read else None,
        "native": attempt(m.native, {"command": "x", "args": ["a"], "env": {"K": "v"}}) if m.native else None}
    return out


def python_adapter(command: str):
    import importlib
    return importlib.import_module(f"harness_{command.replace('-', '_')}").HARNESS


def name_of(function) -> str:
    return getattr(function, "__qualname__", "") if "." in getattr(function, "__qualname__", "") else \
        f"{function.__module__}.{function.__name__}"


class SameAsApprovedTest(unittest.TestCase):
    """Each adapter file yields what its snapshot holds: first the Python adapter it replaced (the
    files were proven equal to those before they went), then each intended change, approved by
    updating the snapshot."""
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        for rel, text in FIXTURES.items():
            path = Path(self.home.name) / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        import skills_ctl
        for module in (agent_security, skills_ctl):
            patch = mock.patch.object(module, "HOME", Path(self.home.name))
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(self.home.cleanup)

    def security_of(self, harness):
        report = agent_security.Report(harness.command, True)
        harness.security(report)
        return report.settings, report.rules, report.files

    def test_every_adapter_file_matches_its_snapshot(self):
        for path in sorted(adapter_data.ADAPTERS.glob("*.toml")):
            with self.subTest(file=path.name):
                stored = SNAPSHOTS / f"{path.stem}.json"
                self.assertTrue(stored.is_file(), f"no snapshot for {path.name}")
                self.assertEqual(snapshot(adapter_data.load(path), Path(self.home.name)),
                                 json.loads(stored.read_text()))

    def test_the_new_facts_are_really_used(self):
        load = lambda c: adapter_data.load(adapter_data.ADAPTERS / f"{c}.toml")
        self.assertEqual(load("grok").skills.listed(), ["~/z/skills"])
        self.assertEqual(load("crush").skills.listed(), ["~/y/skills"])
        _settings, rules, _files = self.security_of(load("codex"))
        self.assertEqual(rules[0][2], ["/home/u/a"])                       # only the trusted project
        _settings, rules, _files = self.security_of(load("copilot"))
        self.assertEqual([r[2] for r in rules], [["/home/u/work"], ["github.com"]])   # from the managed file
        self.assertIn('"schema_version": 1', load("muse").mcp.write("{}", "x", {"command": "c"}))

    def test_the_fixture_is_really_read(self):
        settings, rules, _files = self.security_of(adapter_data.load(adapter_data.ADAPTERS / "claude.toml"))
        self.assertEqual(settings[0].current, "acceptEdits")
        self.assertEqual([title for title, _key, _entries in rules], ["Allowed without asking", "Never allowed"])


class CheckTest(unittest.TestCase):
    """A file that cannot be used is refused, naming the place."""

    def claude(self) -> dict:
        return tomllib.loads((adapter_data.ADAPTERS / "claude.toml").read_text())

    def refused(self, data, words):
        with self.assertRaises(adapter_data.AdapterError) as caught:
            adapter_data.harness(data, "test.toml")
        self.assertIn(words, str(caught.exception))

    def test_unknown_key(self):
        data = self.claude()
        data["homepgae"] = "x"                                    # a typo
        self.refused(data, "unknown key “homepgae”")

    def test_wrong_type(self):
        data = self.claude()
        data["data"] = "~/.claude"
        self.refused(data, "data: must be a list of texts")

    def test_missing_command(self):
        data = self.claude()
        del data["command"]
        self.refused(data, "“command” is missing")

    def test_reader_the_app_does_not_have(self):
        data = self.claude()
        data["sessions"]["reader"] = "my-own"
        self.refused(data, "is not one the app has")

    def test_choice_needs_a_value_or_unset(self):
        data = self.claude()
        del data["security"]["settings"][0]["choices"][0]["value"]
        self.refused(data, "needs either “value” or “unset = true”")

    def test_risk_range(self):
        data = self.claude()
        data["security"]["settings"][0]["choices"][0]["risk"] = 7
        self.refused(data, "0 (safe) to 3 (high)")

    def test_every_adapter_file_loads(self):
        for path in sorted(adapter_data.ADAPTERS.glob("*.toml")):
            with self.subTest(file=path.name):
                self.assertEqual(adapter_data.load(path).command, path.stem)


def update_snapshots(source: str = "toml"):
    """Write the snapshots: of the TOML files (after an intended change), or with "python" of the
    Python adapters still there (the reference a converted file must match)."""
    case = SameAsApprovedTest()
    case.setUp()
    try:
        SNAPSHOTS.mkdir(exist_ok=True)
        for path in sorted(adapter_data.ADAPTERS.glob("*.toml")):
            if source == "python" and not (ROOT / f"harness_{path.stem.replace('-', '_')}.py").is_file():
                continue                   # converted earlier: its snapshot stays
            harness = python_adapter(path.stem) if source == "python" else adapter_data.load(path)
            data = snapshot(harness, Path(case.home.name))
            (SNAPSHOTS / f"{path.stem}.json").write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n")
            print(f"{path.stem}.json")
    finally:
        case.doCleanups()


if __name__ == "__main__":
    if "--update-snapshots" in sys.argv:
        update_snapshots("python" if "--from-python" in sys.argv else "toml")
    else:
        unittest.main()
