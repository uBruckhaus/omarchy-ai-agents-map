"""A registry answer (or the saved list) that asks for far too much: nothing is built past the
budgets in mcp_registry (servers, options, arguments, placeholders, fields), and an option over a
budget is not offered at all, so the Add page never makes a widget per argument without end."""

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

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


class SavedListBoundsTest(unittest.TestCase):
    """refresh_list: a registry that answers page after page of big entries (packages with
    thousands of arguments, long texts) cannot grow the list in memory past what is saved and read
    back (LIST_MAX_FILE bytes, LIST_MAX_SERVERS entries)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "list.json"
        self.asked = 0

    def tearDown(self):
        self.tmp.cleanup()

    def bloated(self, name, args=15, size=400):
        """Big but still usable after the cut: about 80 KB each (with args=300, size=5000: far over
        LIST_MAX_ITEM even then, left out)."""
        arguments = [{"type": "positional", "value": "x" * size, "description": "d" * size} for _ in range(args)]
        return {"server": {"name": name, "description": "y" * 100000, "packages": [npm(arguments)] * 50,
                           "remotes": [{"type": "sse", "url": "https://x.org/" + "z" * 5000}] * 50,
                           "repository": {"url": "https://x.org", "junk": ["j" * 1000] * 5000}}}

    def endless(self, url, **_kw):
        """Never ends: a next cursor on every page, 100 new big servers each."""
        self.asked += 1
        one = self.bloated("")
        return {"servers": [{"server": {**one["server"], "name": f"io.x/p{self.asked}-{i}"}} for i in range(100)],
                "metadata": {"nextCursor": f"c{self.asked}"}}

    def test_entries_are_cut_to_what_parse_reads(self):
        big = self.bloated("io.x/big")
        kept = reg._kept(big)
        self.assertLessEqual(reg._size(kept), reg.LIST_MAX_ITEM)
        self.assertGreater(reg._size(kept), 50_000)
        server = kept["server"]
        self.assertLessEqual(len(server["packages"]), reg.MAX_OPTIONS)
        self.assertLessEqual(len(server["description"]), 500)
        self.assertEqual(server["repository"], {"url": "https://x.org"})
        self.assertEqual([(o.entry, o.needs) for s in reg.parse({"servers": [kept]}) for o in s.options],
                         [(o.entry, o.needs) for s in reg.parse({"servers": [big]}) for o in s.options])
        # Still too many arguments after the cut: refused, as before it.
        many = server_kept = reg._kept(server_with := {"server": {"name": "io.x/many", "packages": [npm(required(500))]}})
        self.assertEqual(len(server_kept["server"]["packages"][0]["packageArguments"]), reg.MAX_ARGUMENTS + 1)
        self.assertEqual(reg.parse({"servers": [many]}), reg.parse({"servers": [server_with]}))
        # Far too big even after the cut: left out.
        self.assertIsNone(reg._kept(self.bloated("io.x/huge", args=300, size=5000)))

    def test_the_same_servers_as_before_the_cut(self):
        item = server("io.x/ok", [npm(required(3), [{"name": "TOKEN", "isSecret": True}])],
                      [{"type": "streamable-http", "url": "https://x.org/mcp"}])
        [before], [after] = reg.parse({"servers": [item]}), reg.parse({"servers": [reg._kept(item)]})
        self.assertEqual([(o.entry, o.needs) for o in before.options], [(o.entry, o.needs) for o in after.options])

    def test_an_endless_registry_stops_at_the_byte_budget(self):
        with mock.patch.object(reg, "LIST_MAX_FILE", 2 * 1024 * 1024), mock.patch.object(reg, "SAVE_EVERY", 3):
            start = time.monotonic()
            self.assertTrue(reg.refresh_list(self.path, now=1000.0, fetch=self.endless))
            self.assertLess(time.monotonic() - start, 20)
            self.assertLess(self.asked, reg.LIST_MAX_PAGES)            # ended by the budget, not the page limit
            self.assertLessEqual(self.path.stat().st_size, reg.LIST_MAX_FILE)
            saved = reg.load_list(self.path)
            self.assertIsNotNone(saved)                                # read back: not over the limit
            self.assertTrue(saved["fetched"])                          # done for now: not again on every start
            self.assertFalse(reg.refresh_list(self.path, now=2000.0, fetch=self.endless))

    def test_entry_count_bounded(self):
        def small(url, **_kw):
            self.asked += 1
            return {"servers": [server(f"io.x/s{self.asked}-{i}", [npm()]) for i in range(100)],
                    "metadata": {"nextCursor": f"c{self.asked}"}}
        with mock.patch.object(reg, "LIST_MAX_SERVERS", 250):
            self.assertTrue(reg.refresh_list(self.path, now=1000.0, fetch=small))
        self.assertEqual(len(reg.load_list(self.path)["servers"]), 250)
        self.assertEqual(self.asked, 3)

    def test_an_old_bloated_cache_is_cut_down_on_the_next_refresh(self):
        old = {f"io.x/old{i}": self.bloated(f"io.x/old{i}") for i in range(3)}     # as 0.20.1 saved them
        self.path.write_text(json.dumps({"fetched": "", "checked": 0, "servers": old}))
        self.assertTrue(reg.refresh_list(self.path, now=1000.0, fetch=lambda url, **_kw: {"servers": []}))
        saved = reg.load_list(self.path)["servers"]
        self.assertEqual(sorted(saved), sorted(old))
        self.assertTrue(all(reg._size(item) <= reg.LIST_MAX_ITEM for item in saved.values()))

    def test_nothing_over_the_limit_is_saved(self):
        with mock.patch.object(reg, "LIST_MAX_FILE", 1000):
            with self.assertRaises(OSError):
                reg._write_list(self.path, {"servers": {"x": "y" * 2000}})
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
