"""Ask an MCP server which tools (and prompts) it offers, the way an agent
does: initialize, then tools/list. Only when asked (the Details page), as
it starts the server's program or talks to its address.

- stdio: the server's command with its args and env (as the agent's own
  settings have them), in its own process group, stopped afterwards;
- streamable HTTP: JSON-RPC posts with the entry's headers (plain JSON or
  an event-stream answer).
Legacy SSE servers are not asked.

Answers are kept in ~/.cache/ai-agents-map/mcp-tools/<name>.json.
"""

import datetime as dt
import json
import os
import re
import select
import signal
import subprocess
import time
from pathlib import Path

HOME = Path.home()
CACHE_DIR = HOME / ".cache/ai-agents-map/mcp-tools"
TIMEOUT = 90                 # npx/uvx may download the server first
MAX_LINE = 4 * 1024 * 1024
MAX_TOOLS = 300
PROTOCOL = "2025-06-18"
CLIENT = {"name": "ai-agents-map", "version": "1"}
_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def _clean(items, kind: str) -> list:
    found = []
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict) and isinstance(item.get("name"), str):
            text = item.get("description") if isinstance(item.get("description"), str) else ""
            text = " ".join(text.split())
            found.append({"name": item["name"][:100], "description": text[:400], "kind": kind})
    return found[:MAX_TOOLS]


# --- stdio ----------------------------------------------------------------------------------------

class _Stdio:
    def __init__(self, entry: dict):
        env = {**os.environ, **entry["env"]}
        self.proc = subprocess.Popen([entry["command"], *entry["args"]], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, cwd=str(HOME),
                                     env=env, start_new_session=True)
        self.buffer = b""
        self.deadline = time.monotonic() + TIMEOUT

    def send(self, message: dict):
        self.proc.stdin.write(json.dumps(message).encode() + b"\n")
        self.proc.stdin.flush()

    def answer(self, wanted: int) -> dict:
        while True:
            while b"\n" in self.buffer:
                line, self.buffer = self.buffer.split(b"\n", 1)
                try:
                    message = json.loads(line)
                except ValueError:
                    continue            # a server printing other things on stdout
                if isinstance(message, dict) and message.get("id") == wanted:
                    if "error" in message:
                        raise RuntimeError(str(message["error"].get("message", message["error"]))[:200])
                    return message.get("result") or {}
            left = self.deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError("no answer")
            ready, _w, _x = select.select([self.proc.stdout], [], [], left)
            if not ready:
                continue
            chunk = os.read(self.proc.stdout.fileno(), 65536)
            if not chunk:
                raise RuntimeError("the server stopped")
            self.buffer += chunk
            if len(self.buffer) > MAX_LINE:
                raise RuntimeError("answer too large")

    def close(self):
        try:
            os.killpg(self.proc.pid, signal.SIGTERM)
            self.proc.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except OSError:
                pass


# --- streamable HTTP -------------------------------------------------------------------------------

class _Http:
    """Streamable HTTP through netfetch: bounded, headers (keys) on curl's stdin."""
    def __init__(self, entry: dict):
        self.url = entry["url"]
        self.headers = {**entry["headers"], "Content-Type": "application/json",
                        "Accept": "application/json, text/event-stream"}
        self.session = None
        self.results = {}

    def send(self, message: dict):
        import netfetch
        headers = dict(self.headers)
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        try:
            raw = netfetch.fetch(self.url, max_bytes=MAX_LINE, timeout=30, user_url=True, include=True,
                                 headers=headers, data=json.dumps(message).encode(), method="POST")
        except netfetch.FetchError as error:
            raise RuntimeError(str(error)) from error
        text = raw.decode("utf-8", "replace")
        # Headers of the last response (a "100 Continue" may come first), then the body.
        head, body = "", text
        while body.startswith("HTTP/"):
            found = re.match(r"(.*?)\r?\n\r?\n", body, re.DOTALL)
            if not found:
                break
            head, body = found.group(1), body[found.end():]
            if not re.match(r"HTTP/\S+ 100\b", head):
                break
        session = re.search(r"^mcp-session-id:\s*([!-~]{1,200})\s*$", head, re.MULTILINE | re.IGNORECASE)
        if session:
            self.session = session.group(1)
        kind = re.search(r"^content-type:\s*(\S+)", head, re.MULTILINE | re.IGNORECASE)
        if "id" not in message:
            return
        texts = [line[5:].strip() for line in body.splitlines() if line.startswith("data:")] \
            if kind and "event-stream" in kind.group(1) else [body]
        for item in texts:
            try:
                answer = json.loads(item)
            except ValueError:
                continue
            if isinstance(answer, dict) and answer.get("id") == message["id"]:
                self.results[message["id"]] = answer

    def answer(self, wanted: int) -> dict:
        message = self.results.get(wanted)
        if message is None:
            raise RuntimeError("no answer")
        if "error" in message:
            raise RuntimeError(str(message["error"].get("message", message["error"]))[:200])
        return message.get("result") or {}

    def close(self):
        pass


# --- asking -------------------------------------------------------------------------------------

def ask(entry: dict) -> dict:
    """{"tools": [...], "prompts": [...], "server": "name version"} from a
    server (mcp_ctl.normal entry). Raises on any failure."""
    if entry["transport"] == "sse":
        raise RuntimeError("an SSE server: not asked")
    link = _Stdio(entry) if entry["transport"] == "stdio" else _Http(entry)
    try:
        link.send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                   "params": {"protocolVersion": PROTOCOL, "capabilities": {}, "clientInfo": CLIENT}})
        hello = link.answer(1)
        link.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        tools, cursor, number = [], None, 2
        while True:
            link.send({"jsonrpc": "2.0", "id": number, "method": "tools/list",
                       "params": {"cursor": cursor} if cursor else {}})
            page = link.answer(number)
            tools += _clean(page.get("tools"), "tool")
            cursor = page.get("nextCursor")
            number += 1
            if not cursor or len(tools) >= MAX_TOOLS or number > 20:
                break
        prompts = []
        if isinstance(hello.get("capabilities"), dict) and "prompts" in hello["capabilities"]:
            link.send({"jsonrpc": "2.0", "id": 99, "method": "prompts/list", "params": {}})
            try:
                prompts = _clean(link.answer(99).get("prompts"), "prompt")
            except (RuntimeError, TimeoutError):
                prompts = []
        info = hello.get("serverInfo") if isinstance(hello.get("serverInfo"), dict) else {}
        server = " ".join(str(info.get(k, ""))[:60] for k in ("name", "version")).strip()
        return {"tools": tools, "prompts": prompts, "server": server}
    finally:
        link.close()


def cache_file(name: str) -> Path:
    if not _NAME.match(name):
        raise ValueError(name)
    return CACHE_DIR / f"{name}.json"


def cached(name: str) -> dict | None:
    try:
        data = json.loads(cache_file(name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def ask_and_keep(name: str, entry: dict) -> dict:
    """ask() and keep the answer (or the error) with the time."""
    now = dt.datetime.now().isoformat(timespec="seconds")
    try:
        result = {"status": "ok", "checked": now, **ask(entry)}
    except (OSError, ValueError, RuntimeError, TimeoutError) as error:
        result = {"status": "failed", "checked": now, "error": str(error)[:300]}
    import ai_core
    ai_core.atomic_write(cache_file(name), json.dumps(result, ensure_ascii=False, indent=1))   # 0600, no links followed
    return result
