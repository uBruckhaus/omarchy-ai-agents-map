"""The AI help protocol: what each AI help run was asked, what came back and what it led to, so a user
who knows the setup can look over what was done (Smart Check › Protocol).

One entry per run, a JSON file of its own in ~/.local/state/ai-agents-map/ai-help/ (the folder only
the user can open, each file only the user can read): when, the title, the agent and its model, the
options that closed it in, each look-up's prompt and answer (our words and the agent's, as sent and
received), the steps and what each came to, the result (ok, failed, cancelled), the time and the
tokens; for a task tab also the terminal's text when it ended (what the agent showed and did, and
what was typed). What the map then changes itself (its preview, backup and Undo) is added by the
page that does it (note_change). Keys and passwords never go into a prompt; the transcript holds
what was on the screen. The oldest entries go when there are more than MAX_ENTRIES.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import tempfile
from pathlib import Path

FOLDER = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "ai-agents-map/ai-help"
MAX_ENTRIES = 200
MAX_TEXT = 64 * 1024             # a prompt or an answer
MAX_TRANSCRIPT = 256 * 1024      # a task tab's terminal text
MAX_FILE = 1024 * 1024
ID = re.compile(r"\d{8}-\d{6}-\d{6}-[a-z0-9-]{1,40}")


def _clip(text, most: int) -> str:
    text = str(text or "")
    return text if len(text) <= most else text[:most] + "\n… (cut)"


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "run"


class Entry:
    """One run, written again whenever it changes (so a crash leaves what was known)."""

    def __init__(self, title: str, kind: str, agent: str, model: str = "", argv=(), folder: Path | None = None,
                 now: dt.datetime | None = None):
        when = now or dt.datetime.now()
        self.folder = folder or FOLDER
        self.id = f"{when.strftime('%Y%m%d-%H%M%S-%f')}-{_slug(title)}"
        # The options that closed the agent in: its argv without the prompt (that is in look_ups).
        self.data = {"id": self.id, "when": when.isoformat(timespec="seconds"), "title": title, "kind": kind,
                     "agent": agent, "model": model, "options": [a for a in argv if a.startswith("-")][:60],
                     "look_ups": [], "steps": [], "changes": [], "result": "running", "error": "",
                     "seconds": 0, "tokens": 0, "transcript": ""}
        self.save()

    def look_up(self, task: str, prompt: str, answer: str = "", error: str = "", argv=()):
        self.data["look_ups"].append({"task": task, "prompt": _clip(prompt, MAX_TEXT),
                                      "answer": _clip(answer, MAX_TEXT), "error": _clip(error, 2000),
                                      "options": [a for a in argv if a.startswith("-")][:60]})
        self.save()

    def steps(self, steps: list):
        self.data["steps"] = [{"text": str(t)[:200], "state": str(s)[:20], "detail": str(d)[:200]}
                              for t, s, d in steps][:20]

    def note_change(self, what: str, backup: str = ""):
        """A change the map made from this run's answer (with its backup, for Undo by hand)."""
        self.data["changes"].append({"what": str(what)[:500], "backup": str(backup)[:500]})
        self.save()

    def end(self, result: str, error: str = "", seconds: float = 0, tokens: int = 0, transcript: str = ""):
        self.data.update(result=result, error=_clip(error, 2000), seconds=int(seconds), tokens=int(tokens or 0))
        if transcript:
            self.data["transcript"] = _clip(transcript, MAX_TRANSCRIPT)
        self.save()
        prune(self.folder)

    def save(self):
        try:
            self.folder.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd, tmp = tempfile.mkstemp(prefix=".entry-", dir=str(self.folder))
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                os.fchmod(handle.fileno(), 0o600)
                json.dump(self.data, handle, ensure_ascii=False, indent=1)
            os.replace(tmp, self.folder / f"{self.id}.json")
        except OSError:
            pass                     # the protocol must never stop a run


def entries(folder: Path | None = None, most: int = MAX_ENTRIES) -> list[dict]:
    """The newest first; only our own regular files, never through a link, each up to MAX_FILE."""
    folder = folder or FOLDER
    try:
        names = sorted((n for n in os.listdir(folder) if n.endswith(".json") and ID.fullmatch(n[:-5])),
                       reverse=True)[:most]
    except OSError:
        return []
    found = []
    for name in names:
        try:
            fd = os.open(folder / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except OSError:
            continue
        try:
            info = os.fstat(fd)
            if not (info.st_mode & 0o170000 == 0o100000) or info.st_uid != os.getuid() or info.st_size > MAX_FILE:
                continue
            data = json.loads(os.read(fd, MAX_FILE).decode("utf-8", "replace"))
        except (OSError, ValueError):
            continue
        finally:
            os.close(fd)
        if isinstance(data, dict) and isinstance(data.get("title"), str):
            found.append(data)
    return found


def prune(folder: Path | None = None, keep: int = MAX_ENTRIES):
    folder = folder or FOLDER
    try:
        names = sorted(n for n in os.listdir(folder) if n.endswith(".json") and ID.fullmatch(n[:-5]))
    except OSError:
        return
    for name in names[:max(0, len(names) - keep)]:
        try:
            (folder / name).unlink()
        except OSError:
            pass


def as_text(entry: dict) -> str:
    """One entry as plain text (Copy, for a report or a second opinion)."""
    lines = [f"{entry.get('title')} · {entry.get('when')}",
             f"Agent: {entry.get('agent')}" + (f" · {entry.get('model')}" if entry.get("model") else ""),
             f"Result: {entry.get('result')}" + (f" · {entry.get('error')}" if entry.get("error") else ""),
             f"Time: {entry.get('seconds')} s · tokens: {entry.get('tokens')}"]
    if entry.get("options"):
        lines.append("Options: " + " ".join(entry["options"]))
    for step in entry.get("steps", []):
        lines.append(f"  [{step.get('state')}] {step.get('text')}" + (f" · {step['detail']}" if step.get("detail") else ""))
    for number, look in enumerate(entry.get("look_ups", []), 1):
        lines += ["", f"--- Look-up {number} ({look.get('task')}): prompt", look.get("prompt", ""),
                  f"--- Look-up {number}: answer", look.get("answer") or look.get("error", "")]
    for change in entry.get("changes", []):
        lines.append(f"Changed: {change.get('what')}" + (f" (backup: {change['backup']})" if change.get("backup") else ""))
    if entry.get("transcript"):
        lines += ["", "--- Terminal", entry["transcript"]]
    return "\n".join(lines) + "\n"
