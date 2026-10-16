import json
import os
import stat
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import install_ctl  # noqa: E402
import jobs  # noqa: E402


class JobsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.file = Path(self.tmp.name) / "state/jobs.json"
        self.ended = threading.Event()
        self.seen = []
        self.manager = jobs.Jobs(lambda job: self.seen.append((job.state, job.retry_in)), self.file,
                                 sleep=lambda _s: None)

    def run_job(self, work, spec=None):
        job = self.manager.start("model", "gemma.gguf · llama.cpp", work, spec, "runtime:llamacpp",
                                 on_end=lambda _j: self.ended.set())
        self.assertTrue(self.ended.wait(5))
        return job

    def test_lost_connection_is_retried_and_resumes(self):
        calls = []

        def work(progress, cancelled):
            calls.append(1)
            progress(0.4 * len(calls), "part")
            if len(calls) < 3:
                raise install_ctl.DownloadError("download failed: connection reset")

        job = self.run_job(work, {"kind": "model"})
        self.assertEqual((job.state, len(calls), job.attempt), ("done", 3, 2))
        self.assertIn(("waiting", 5), self.seen)          # the strip can say "again in 5 s"
        self.assertFalse(self.file.exists())               # finished: nothing to continue

    def test_refusals_are_not_retried(self):
        def work(progress, cancelled):
            raise RuntimeError("gemma.gguf does not match the release's SHA-256 checksum")

        job = self.run_job(work)
        self.assertEqual((job.state, job.attempt), ("failed", 0))
        self.assertIn("SHA-256", job.error)

    def test_gives_up_after_the_last_retry(self):
        def work(progress, cancelled):
            raise install_ctl.DownloadError("download incomplete")

        job = self.run_job(work)
        self.assertEqual((job.state, job.attempt), ("failed", len(jobs.RETRY_DELAYS)))

    def test_stop(self):
        started = threading.Event()

        def work(progress, cancelled):
            started.set()
            while not cancelled():
                pass
            raise install_ctl.Cancelled()

        job = self.manager.start("model", "x", work, on_end=lambda _j: self.ended.set())
        self.assertTrue(started.wait(5))
        self.manager.stop(job.id)
        self.assertTrue(self.ended.wait(5))
        self.assertEqual(job.state, "stopped")

    def test_unfinished_job_is_interrupted_next_time(self):
        release = threading.Event()
        started = threading.Event()

        def work(progress, cancelled):
            progress(0.42, "")
            started.set()
            release.wait(5)

        spec = {"kind": "model", "runtime": "llamacpp", "repo": "org/model", "paths": ["m.gguf"]}
        job = self.manager.start("model", "m.gguf · llama.cpp", work, spec, "runtime:llamacpp")
        self.assertTrue(started.wait(5))
        self.manager.save()
        self.assertEqual(stat.S_IMODE(self.file.stat().st_mode), 0o600)
        # The app ends here; the next start finds it.
        again = jobs.Jobs(state_file=self.file)
        found = again.shown()
        self.assertEqual([(j.id, j.state, j.spec, j.target) for j in found],
                         [(job.id, "interrupted", spec, "runtime:llamacpp")])
        self.assertAlmostEqual(found[0].fraction, 0.42, places=2)
        # Continue replaces it; Discard forgets it.
        done = threading.Event()
        again.start("model", found[0].title, lambda p, c: None, spec, replaces=found[0].id,
                    on_end=lambda _j: done.set())
        self.assertTrue(done.wait(5))
        self.assertNotIn(found[0].id, [j.id for j in again.shown()])
        release.set()

    def test_state_file_is_untrusted(self):
        self.file.parent.mkdir(parents=True)
        self.file.write_text(json.dumps([{"kind": "rm -rf", "title": "x", "id": "1", "spec": {}},
                                         {"kind": "model", "title": "ok", "id": "2", "spec": "nope"},
                                         {"kind": "pull", "title": "t" * 999, "id": "3", "spec": {},
                                          "fraction": 7}]))
        found = jobs.Jobs(state_file=self.file).shown()
        self.assertEqual([(j.id, len(j.title), j.fraction) for j in found], [("3", 200, 1.0)])
        os.unlink(self.file)
        os.symlink("/etc/passwd", self.file)
        self.assertEqual(jobs.Jobs(state_file=self.file).shown(), [])

    def test_release_round_trip(self):
        release = install_ctl.Release("v1.2.3", [("a.tgz", "https://github.com/x/a.tgz", 10)], {"a.tgz": "0" * 64})
        self.assertEqual(install_ctl.Release.from_json(json.loads(json.dumps(release.to_json()))), release)
        for bad in ({"version": "../x", "assets": []},
                    {"version": "v1", "assets": [["a", "http://x", 1]]},
                    {"version": "v1", "assets": [["../a", "https://x", 1]]}):
            with self.assertRaises((ValueError, TypeError, IndexError)):
                install_ctl.Release.from_json(bad)


if __name__ == "__main__":
    unittest.main()
