"""A registry answer (or the saved list) that asks for far too much: nothing is built past the
budgets in mcp_registry (servers, options, arguments, placeholders, fields), and an option over a
budget is not offered at all, so the Add page never makes a widget per argument without end."""

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import mcp_registry as reg  # noqa: E402


def server(name, packages=(), remotes=()):
    return {"server": {"name": name, "packages": list(packages), "remotes": list(remotes)}}


def npm(args=(), env=(), runtime=None, kind="npm"):
    package = {"registryType": kind, "identifier": "some-mcp", "version": "1.0.0",
               "packageArguments": list(args), "environmentVariables": list(env)}
    if runtime is not None:
        package["runtimeArguments"] = runtime
    return package


def required(n):
    return [{"type": "positional", "isRequired": True, "description": f"arg {i}"} for i in range(n)]


class RegistryBoundsTest(unittest.TestCase):
    def test_too_many_required_arguments_are_not_offered(self):
        start = time.monotonic()
        servers = reg.parse({"servers": [server("io.x/huge", [npm(required(50000))])]})
        self.assertLess(time.monotonic() - start, 1)
        self.assertEqual(servers, [])                       # its only option is refused: no server, no form

    def test_up_to_the_budget_works_as_before(self):
        [found] = reg.parse({"servers": [server("io.x/ok", [npm(required(reg.MAX_ARGUMENTS))])]})
        [option] = found.options
        self.assertEqual(len(option.needs), reg.MAX_ARGUMENTS)
        self.assertEqual(option.entry["args"][2:4], ["{arg:0}", "{arg:1}"])

    def test_docker_runtime_arguments_bounded(self):
        runtime = [{"type": "named", "name": "-e", "value": f"V{i}"} for i in range(20000)]
        self.assertIsNone(reg.option_from_package(npm(runtime=runtime, kind="oci")))

    def test_placeholders_in_one_value_bounded(self):
        value = "".join("{p%d}" % i for i in range(120))
        option = reg.option_from_package(npm([{"type": "positional", "value": value}]))
        self.assertLessEqual(len(option.needs), reg.MAX_PLACEHOLDERS)

    def test_many_variables_or_headers_bounded(self):
        env = [{"name": f"V{i}", "isRequired": True} for i in range(10000)]
        option = reg.option_from_package(npm(env=env))
        self.assertLessEqual(len(option.needs), reg.MAX_FIELDS)
        headers = [{"name": f"H{i}", "value": "{a%d}{b%d}" % (i, i)} for i in range(10000)]
        self.assertIsNone(reg.option_from_remote({"type": "streamable-http", "url": "https://x.org/mcp",
                                                  "headers": headers}))       # 80 fields: over the budget

    def test_servers_and_options_per_answer_bounded(self):
        many = [server(f"io.x/s{i}", [npm()] * 5000, [{"type": "sse", "url": "https://x.org"}] * 5000)
                for i in range(1000)]
        start = time.monotonic()
        servers = reg.parse({"servers": many})
        self.assertLess(time.monotonic() - start, 2)
        self.assertEqual(len(servers), reg.MAX_SERVERS)
        self.assertTrue(all(len(s.options) <= reg.MAX_OPTIONS for s in servers))

    def test_saved_list_pages_past_the_answer_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "list.json"
            items = {f"io.x/tool{i:03}": server(f"io.x/tool{i:03}", [npm()]) for i in range(250)}
            items["io.x/tool-gone"] = {**server("io.x/tool-gone", [npm()]),
                                       "_meta": {"io.modelcontextprotocol.registry/official": {"status": "deleted"}}}
            path.write_text(json.dumps({"fetched": "2026-10-22T10:00:00Z", "checked": 0, "servers": items}))
            page, cursor = reg.search_list("tool", offset=220, limit=20, path=path)
            self.assertEqual(len(page), 20)
            self.assertEqual(cursor, "#240")
            last, cursor = reg.search_list("tool", offset=240, limit=20, path=path)
            self.assertEqual((len(last), cursor), (10, ""))         # 250 active ones; the deleted one is left out
            self.assertNotIn("io.x/tool-gone", [s.name for s in page + last])


if __name__ == "__main__":
    unittest.main()
