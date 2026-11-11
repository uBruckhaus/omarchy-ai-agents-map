"""Dictation in Ask AI (Maprisma task 024, dictation.dart): speak the request instead of typing it.

Engines (Settings › Dictation in Ask AI):
- "omarchy": Omarchy's own dictation (omarchy-dictation start / stop, its default tool: voxtype or
  Superwhisper); the desktop types the text into the field that has the keyboard.
- "superwhisper": Superwhisper directly through its command line (Uwe, 2026-11-11), whatever Omarchy's
  default tool is: superwhisper start / stop, then the text of the dictation made since the start (its
  history); with its own auto-paste on it has typed it already.
- "auto": Omarchy's dictation where it is there, else Superwhisper.
Nothing is recorded or kept by the app itself; the engines keep their own history as set up there.
"""

from __future__ import annotations

import json
import shutil
import time

ENGINES = ("auto", "omarchy", "superwhisper")
WAIT = 90            # seconds a dictation may take to be written after Stop
TIMEOUT = 20         # seconds one engine command may take
MAX_OUTPUT = 256 * 1024   # bytes read of one engine command's output


def available() -> list[str]:
    """The engines this PC has."""
    found = []
    if shutil.which("omarchy-dictation"):
        found.append("omarchy")
    if shutil.which("superwhisper"):
        found.append("superwhisper")
    return found


def chosen(setting) -> str | None:
    """The engine to use: the one set when it is here, else Omarchy's, else Superwhisper; None: none."""
    here = available()
    if setting in here:
        return setting
    return here[0] if here else None


def _run(argv: list[str]) -> tuple[bool, str]:
    """One engine command: argv (no shell), stdout read with a byte cap and one deadline."""
    import ai_core
    out, ok = ai_core.read_bounded_output(argv, max_bytes=MAX_OUTPUT, timeout=TIMEOUT)
    return ok, out.decode("utf-8", "replace").strip()


def _json(text: str):
    try:
        return json.loads(text)
    except ValueError:
        return None


class Dictation:
    """One dictation at a time: start(), then stop() -> its text ("" when the desktop typed it)."""

    def __init__(self, engine: str):
        self.engine = engine
        self.started = 0.0

    def start(self) -> str | None:
        """None when it records now, else what went wrong."""
        self.started = time.time()
        argv = ["omarchy-dictation", "start"] if self.engine == "omarchy" else ["superwhisper", "start"]
        ok, out = _run(argv)
        return None if ok else (out[-200:] or "could not start")

    def cancel(self):
        _run(["omarchy-dictation", "stop"] if self.engine == "omarchy" else ["superwhisper", "cancel"])

    def stop(self, wait: float = WAIT, sleep=time.sleep) -> str:
        """Stop and wait for the text. Omarchy's dictation types it itself: ""."""
        if self.engine == "omarchy":
            _run(["omarchy-dictation", "stop"])
            return ""
        _run(["superwhisper", "stop"])
        deadline = time.time() + wait
        while time.time() < deadline:
            ok, out = _run(["superwhisper", "status"])
            state = _json(out) if ok else None
            if isinstance(state, dict) and state.get("phase") == "idle":
                break
            sleep(0.4)
        return newest_text(self.started)


def newest_text(since: float) -> str:
    """The text of Superwhisper's newest dictation made since `since` (its history), or ""."""
    ok, out = _run(["superwhisper", "history", "list", "--limit", "3"])
    data = _json(out) if ok else None
    for item in (data.get("items") if isinstance(data, dict) else None) or []:
        stamp = item.get("timestamp") if isinstance(item, dict) else None
        when = float(stamp) / (1000 if isinstance(stamp, (int, float)) and stamp > 1e11 else 1) \
            if isinstance(stamp, (int, float)) else _parse_stamp(stamp)
        if when is None or when + 2 < since:
            continue
        meta = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        text = meta.get("result") or meta.get("llmResult") or meta.get("rawResult") or ""
        if isinstance(text, str) and text.strip():
            return text.strip()[:4000]
    return ""


def _parse_stamp(value) -> float | None:
    """A history timestamp as seconds: a number (seconds or milliseconds) or an ISO time."""
    if not isinstance(value, str):
        return None
    if value.isdigit():
        number = float(value)
        return number / 1000 if number > 1e11 else number
    try:
        import datetime as dt
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None
