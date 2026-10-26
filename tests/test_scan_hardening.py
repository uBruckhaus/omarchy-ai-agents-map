"""0.26.1: the scans read files without mapping them and without blocking, list folders with a
budget, and an odd MCP server reply ends as a failed ask instead of a dead worker."""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ai_core  # noqa: E402
import mcp_scan  # noqa: E402
import mcp_tools  # noqa: E402
import skills_scan  # noqa: E402


class ScanFileTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = Path(self.dir.name)

    def test_fifo_never_blocks(self):
        fifo = self.root / "tool-mcp"
        os.mkfifo(fifo)
        started = time.monotonic()
        with mock.patch.object(Path, "stat", return_value=os.stat_result((0o100755, 0, 0, 1, 0, 0, 10, 0, 0, 0))):
            self.assertEqual(mcp_scan.scan_file(fifo, keep=False), (0, b""))
        self.assertFalse(mcp_scan._is_elf_or_script(fifo))
        self.assertLess(time.monotonic() - started, 1.0)

    def test_usage_across_a_chunk_seam_is_found(self):
        program = self.root / "wordcraft-cli"
        usage = b"wordcraft-cli mcp [--root DIR]\n      --root DIR   where documents go\n"
        with mock.patch.object(mcp_scan, "CHUNK", 4096):
            program.write_bytes(b"\x7fELF" + b"x" * (4096 - 20) + b"\n" + usage + b"\n" + b"y" * 5000)
            count, found = mcp_scan.scan_file(program, keep=False)
        self.assertTrue(found.startswith(b"wordcraft-cli mcp [--root DIR]"))

    def test_file_shorter_than_its_size_reads_short(self):
        program = self.root / "short"
        program.write_bytes(b"#!/bin/sh\nexec x mcp [--y]\n")
        fd = ai_core.open_regular(program)
        try:
            self.assertEqual(mcp_scan._scan_bytes(fd, 10_000, "short")[1], b"x mcp [--y]")
        finally:
            os.close(fd)


class BudgetTest(unittest.TestCase):
    def test_scandir_head_stops_at_the_limit(self):
        with tempfile.TemporaryDirectory() as folder:
            for i in range(50):
                Path(folder, f"f{i}").touch()
            self.assertEqual(len(ai_core.scandir_head(folder, 7)), 7)

    def test_skills_queue_never_exceeds_the_budget(self):
        with tempfile.TemporaryDirectory() as folder:
            for i in range(40):
                os.makedirs(os.path.join(folder, f"d{i}", "e"))
            with mock.patch.object(skills_scan, "MAX_DIRS", 5), \
                    mock.patch.object(skills_scan, "ROOTS", [(Path(folder), 3)]), \
                    mock.patch("extra_folders.folders", return_value=[]), \
                    mock.patch.object(skills_scan.ai_core if hasattr(skills_scan, "ai_core") else ai_core,
                                      "scandir_head", wraps=ai_core.scandir_head) as listed:
                skills_scan.scan()
            self.assertLessEqual(listed.call_count, 5)


class OddRepliesTest(unittest.TestCase):
    def test_error_and_result_of_any_shape(self):
        with self.assertRaises(RuntimeError):
            mcp_tools._result({"id": 1, "error": "bad"})
        with self.assertRaises(RuntimeError):
            mcp_tools._result({"id": 1, "error": {"message": "nope"}})
        self.assertEqual(mcp_tools._result({"id": 1, "result": ["not", "a", "dict"]}), {})
        self.assertEqual(mcp_tools._result({"id": 1, "result": {"tools": []}}), {"tools": []})

    def test_ask_and_keep_never_raises(self):
        with mock.patch.object(mcp_tools, "ask", side_effect=AttributeError("odd reply")), \
                mock.patch.object(ai_core, "atomic_write"):
            result = mcp_tools.ask_and_keep("x", {"transport": "stdio"})
        self.assertEqual(result["status"], "failed")



class StdioWriteDeadlineTest(unittest.TestCase):
    def test_server_that_stops_reading_ends_with_timeout(self):
        with mock.patch.object(mcp_tools, "TIMEOUT", 1.0):
            link = mcp_tools._Stdio({"command": "sleep", "args": ["30"], "env": {}})
        try:
            started = time.monotonic()
            with self.assertRaises(TimeoutError):
                link.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list",
                           "params": {"cursor": "x" * 1_000_000}})
            self.assertLess(time.monotonic() - started, 5)
        finally:
            link.close()

    def test_huge_cursor_ends_the_paging(self):
        pages = [{"tools": [{"name": "a"}], "nextCursor": "c" * (mcp_tools.MAX_CURSOR + 1)}]

        class Link:
            def __init__(self, *_a):
                self.sent = []

            def send(self, message):
                self.sent.append(message)

            def answer(self, number):
                return {"capabilities": {}} if number == 1 else pages[0]

            def close(self):
                pass
        with mock.patch.object(mcp_tools, "_Stdio", Link):
            result = mcp_tools.ask({"transport": "stdio"})
        self.assertEqual([t["name"] for t in result["tools"]], ["a"])


if __name__ == "__main__":
    unittest.main()
