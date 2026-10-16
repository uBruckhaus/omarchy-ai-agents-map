"""Background jobs: downloads and installs that keep running when their page is left.

A job is a function work(progress, cancelled) on its own thread; progress(fraction,
text) and cancelled() are what install_ctl and models_ctl already take. Every change
is passed to `notify(job)` (the window hands it to the GTK main loop).

Interruptions:
- a lost connection (install_ctl.DownloadError, a timeout, a refused or reset
  connection) is retried after RETRY_DELAYS; the download resumes where it stopped
  (curl -C - continues the .part file, Ollama resumes its own pulls, an install
  skips parts it has already downloaded and checked);
- a job with a `spec` (plain JSON: what to download or install, never credentials)
  is kept in STATE_FILE while it runs. If AI Agents Map ends before the job does
  (closed, crashed, logged out), the next start lists it as interrupted, to Continue
  or Discard. A checksum mismatch or any other error is not retried.
"""

import json
import os
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

STATE_FILE = Path.home() / ".local/state/ai-agents-map/jobs.json"
RETRY_DELAYS = (5, 15, 30, 60, 120)
MAX_JOBS = 32
KINDS = ("model", "pull", "install")


class Cancelled(Exception):
    """Raised by work() when cancelled() said so (install_ctl.Cancelled is one too)."""


def retryable(error: BaseException) -> bool:
    """A lost connection, not a refusal: worth trying again with resume."""
    import install_ctl
    if isinstance(error, install_ctl.DownloadError):
        return True
    return isinstance(error, (TimeoutError, ConnectionError))


@dataclass
class Job:
    kind: str                       # "model" (a Hugging Face file), "pull" (Ollama), "install"
    title: str                      # what the strip shows: "gemma-4-E2B-it-Q4_K_M.gguf · llama.cpp"
    target: str = ""                # the box it belongs to: "runtime:ollama", "agent:dsh"
    spec: dict | None = None        # how to start it again (None: not resumable)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    state: str = "running"          # running, waiting (retry), done, failed, stopped, interrupted
    fraction: float = 0.0
    text: str = ""
    error: str = ""
    retry_in: int = 0               # seconds until the next try (state "waiting")
    attempt: int = 0
    cancel: bool = False
    started: float = field(default_factory=time.time)

    @property
    def active(self) -> bool:
        return self.state in ("running", "waiting")


class Jobs:
    def __init__(self, notify=lambda job: None, state_file: Path = STATE_FILE, sleep=time.sleep):
        self.notify = notify
        self.state_file = state_file
        self.sleep = sleep
        self.lock = threading.Lock()
        self.jobs: list[Job] = []
        # Jobs a previous run left unfinished: shown as interrupted.
        for entry in self.load():
            self.jobs.append(Job(entry["kind"], entry["title"], entry.get("target", ""), entry["spec"],
                                 id=entry["id"], state="interrupted", fraction=entry.get("fraction", 0.0)))

    # --- the state file ------------------------------------------------------------------------

    def load(self) -> list:
        try:
            if self.state_file.is_symlink() or self.state_file.stat().st_size > 256 * 1024:
                return []
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        found = []
        for entry in data if isinstance(data, list) else []:
            if (isinstance(entry, dict) and entry.get("kind") in KINDS and isinstance(entry.get("spec"), dict)
                    and isinstance(entry.get("title"), str) and isinstance(entry.get("id"), str)):
                entry["title"] = entry["title"][:200]
                fraction = entry.get("fraction")
                entry["fraction"] = min(1.0, max(0.0, float(fraction))) if isinstance(fraction, (int, float)) else 0.0
                found.append(entry)
        return found[:MAX_JOBS]

    def save(self):
        """Resumable jobs that have not ended (running, waiting, interrupted)."""
        with self.lock:
            keep = [{"id": j.id, "kind": j.kind, "title": j.title, "target": j.target, "spec": j.spec,
                     "fraction": round(j.fraction, 4)}
                    for j in self.jobs if j.spec is not None and j.state in ("running", "waiting", "interrupted")]
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if not keep:
                self.state_file.unlink(missing_ok=True)
                return
            fd, tmp = tempfile.mkstemp(prefix=".jobs-", dir=str(self.state_file.parent))
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                os.fchmod(handle.fileno(), 0o600)
                json.dump(keep, handle)
            os.replace(tmp, self.state_file)
        except OSError:
            pass

    # --- running -------------------------------------------------------------------------------

    def start(self, kind: str, title: str, work, spec: dict | None = None, target: str = "",
              on_end=None, replaces: str = "") -> Job:
        """Run work(progress, cancelled) in the background. on_end(job) is called (through
        notify's thread rule: from the worker thread) once it is done, failed or stopped.
        replaces: the id of an interrupted job this one continues."""
        job = Job(kind, title, target, spec)
        with self.lock:
            self.jobs = [j for j in self.jobs if j.id != replaces and (j.active or j.state != "done")][-MAX_JOBS:]
            self.jobs.append(job)
        self.save()
        self.notify(job)
        threading.Thread(target=self._run, args=(job, work, on_end), daemon=True).start()
        return job

    def _run(self, job: Job, work, on_end):
        def progress(fraction, text=None):
            job.fraction = max(0.0, min(1.0, float(fraction or 0.0)))
            if text:
                job.text = str(text)[:200]
            self.notify(job)

        cancelled = lambda: job.cancel            # noqa: E731
        while True:
            try:
                work(progress, cancelled)
                job.state = "done"
                job.fraction = 1.0
                break
            except Exception as error:            # noqa: BLE001 - every outcome is shown
                if job.cancel or type(error).__name__ == "Cancelled":
                    job.state = "stopped"
                    break
                if not retryable(error) or job.attempt >= len(RETRY_DELAYS):
                    job.state, job.error = "failed", str(error)[:300] or type(error).__name__
                    break
                delay = RETRY_DELAYS[job.attempt]
                job.attempt += 1
                job.state, job.error = "waiting", str(error)[:300]
                for left in range(delay, 0, -1):
                    if job.cancel:
                        break
                    job.retry_in = left
                    self.notify(job)
                    self.sleep(1)
                job.retry_in = 0
                if job.cancel:
                    job.state = "stopped"
                    break
                job.state = "running"
                self.notify(job)
        self.save()
        self.notify(job)
        if on_end is not None:
            on_end(job)

    def stop(self, job_id: str):
        for job in self.jobs:
            if job.id == job_id and job.active:
                job.cancel = True
                self.notify(job)

    def discard(self, job_id: str):
        """Forget an ended or interrupted job (a running one is stopped first)."""
        self.stop(job_id)
        with self.lock:
            self.jobs = [j for j in self.jobs if j.id != job_id or j.active]
        self.save()

    def find(self, job_id: str) -> Job | None:
        return next((j for j in self.jobs if j.id == job_id), None)

    def active(self, target: str = "") -> list[Job]:
        return [j for j in self.jobs if j.active and (not target or j.target == target)]

    def shown(self) -> list[Job]:
        """What the main page lists: everything not yet dismissed."""
        return list(self.jobs)
