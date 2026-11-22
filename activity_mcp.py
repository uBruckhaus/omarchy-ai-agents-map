#!/usr/bin/env python3
"""Local stdio MCP servers for the activity features (Maprisma task 074/075: activityMcpReply).

    activity_mcp.py progress   report_progress(step, done, total, note?): the running agent instance
                               that owns this server (one of its parents) shows it in What is it doing?
    activity_mcp.py log        activity_log_report(from_day?, through_day?, cursor?), reports_list(query?,
                               cursor?) and reports_get(id): read-only views of the log and the library

Configured by hand as a stdio MCP server of an agent; nothing starts it on its own. JSON-RPC 2.0, one
message per line, lines over 64 KiB refused. The progress server writes only the instance's own
progress file (agent_activity_report.report_progress); the log server never writes.
"""

from __future__ import annotations

import json
import sys

MAX_LINE = 64 * 1024

PROGRESS_TOOL = {
    "name": "report_progress", "description": "Report progress for this running agent in AI Agents Map.",
    "inputSchema": {"type": "object", "properties": {"step": {"type": "string"}, "done": {"type": "integer"},
                                                     "total": {"type": "integer"}, "note": {"type": "string"}},
                    "required": ["step", "done", "total"], "additionalProperties": False},
}
LOG_TOOLS = [
    {"name": "activity_log_report", "description": "Read retained local AI Agents Map activity observations.",
     "inputSchema": {"type": "object", "properties": {"from_day": {"type": "string"}, "through_day": {"type": "string"},
                                                      "cursor": {"type": "integer"}}, "additionalProperties": False}},
    {"name": "reports_list", "description": "List saved AI Agents Map reports, optionally searching by text.",
     "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}, "cursor": {"type": "integer"}},
                     "additionalProperties": False}},
    {"name": "reports_get", "description": "Read a saved AI Agents Map report.",
     "inputSchema": {"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"],
                     "additionalProperties": False}},
]


def agent_pids() -> list[int]:
    """This account's running agent instances (all known agents)."""
    import harnesses
    import provider_watch
    pids: dict = {}
    provider_watch.agent_instances([h.command for h in harnesses.all_harnesses()], pids=pids)
    return [p for found in pids.values() for p in found]


def _text(payload, error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}], "isError": error}


def log_call(name: str, arguments: dict) -> dict:
    import activity_log
    import reports_ctl
    if name == "activity_log_report":
        if any(k not in ("from_day", "through_day", "cursor") for k in arguments):
            raise ValueError("Invalid report arguments")
        return _text(activity_log.ActivityLog().report(arguments))
    try:
        if name == "reports_list":
            query, cursor = arguments.get("query", ""), arguments.get("cursor")
            if any(k not in ("query", "cursor") for k in arguments) or not isinstance(query, str) or len(query) > 200 \
                    or (cursor is not None and (type(cursor) is not int or cursor < 1)):
                return _text({"code": "invalid_request"}, True)
            return _text(reports_ctl.ReportsLibrary().list(query, cursor))
        report_id = arguments.get("id")
        if set(arguments) != {"id"} or type(report_id) is not int or report_id < 1:
            return _text({"code": "invalid_request"}, True)
        return _text({"report": reports_ctl.ReportsLibrary().get(report_id)})
    except reports_ctl.ReportError as error:
        return _text({"code": error.code}, True)


def reply(frame, mode: str, pids=agent_pids) -> dict | None:
    """The answer to one JSON-RPC frame (None for a notification)."""
    if not isinstance(frame, dict) or frame.get("jsonrpc") != "2.0":
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid request"}}
    if "id" not in frame:
        return None
    out = {"jsonrpc": "2.0", "id": frame["id"]}
    method, params = frame.get("method"), frame.get("params")
    if method == "initialize":
        out["result"] = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                         "serverInfo": {"name": "AI Agents Map activity", "version": "1"}}
    elif method == "ping":
        out["result"] = {}
    elif method == "tools/list":
        out["result"] = {"tools": [PROGRESS_TOOL] if mode == "progress" else LOG_TOOLS}
    elif method == "tools/call" and isinstance(params, dict):
        name, arguments = params.get("name"), params.get("arguments", {})
        if not isinstance(arguments, dict):
            out["error"] = {"code": -32602, "message": "Invalid tool call"}
        elif mode == "progress" and name == "report_progress":
            import agent_activity_report
            try:
                accepted = agent_activity_report.report_progress(arguments, pids())
            except ValueError:
                accepted = False
            out["result"] = _text({"accepted": accepted}, not accepted)
        elif mode == "log" and name in ("activity_log_report", "reports_list", "reports_get"):
            try:
                out["result"] = log_call(name, arguments)
            except ValueError:
                out["error"] = {"code": -32602, "message": "Invalid tool call"}
        else:
            out["error"] = {"code": -32602, "message": "Invalid tool call"}
    else:
        out["error"] = {"code": -32601, "message": "Method not found"}
    return out


def serve(mode: str, stdin=None, stdout=None) -> int:
    stdin = stdin or sys.stdin.buffer
    stdout = stdout or sys.stdout
    while True:
        line = stdin.readline(MAX_LINE + 1)
        if not line:
            return 0
        if len(line) > MAX_LINE:
            while line and not line.endswith(b"\n"):          # drop the rest of an over-long line
                line = stdin.readline(MAX_LINE)
            answer = {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Request too large"}}
        else:
            try:
                answer = reply(json.loads(line), mode)
            except ValueError:
                answer = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
        if answer is not None:
            stdout.write(json.dumps(answer, ensure_ascii=False) + "\n")
            stdout.flush()


def main(argv) -> int:
    if len(argv) != 2 or argv[1] not in ("progress", "log"):
        sys.stderr.write("usage: activity_mcp.py progress|log\n")
        return 2
    return serve(argv[1])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
