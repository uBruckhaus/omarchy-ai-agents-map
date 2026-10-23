"""Every web request of AI Agents Map goes through here: curl, run through
ai_core.read_bounded_output, so each answer is capped in size and in time
(one deadline for connect, headers and body).

- https only (`--proto =https`), at most `redirects` redirects, each one
  https as well;
- headers and request bodies reach curl on stdin (`--config -`), never in
  argv: they can hold API keys, and argv is visible to other accounts in
  /proc/<pid>/cmdline;
- the URL is checked here as well (scheme, no control characters, no
  spaces, no user:password@).

Local servers (127.0.0.1) are reached with `local=True`: http allowed, no
redirects. Callers check that the listener is theirs first (models_ctl).
`user_url=True`: an address the user set up for an MCP server (http or
https, any host, no redirects), asked only when the user clicks Test.
"""

import json
import re
from urllib.parse import urlsplit

import ai_core

_BAD = re.compile(r"[\x00-\x20\x7f\"\\]")


class FetchError(RuntimeError):
    pass


def _check(url: str, local: bool, user_url: bool = False):
    parts = urlsplit(url)
    if _BAD.search(url) or parts.username or parts.password or not parts.hostname:
        raise FetchError("not a usable address")
    if parts.scheme != "https" and not (local and parts.scheme == "http"):
        raise FetchError("only https addresses")
    if local and not user_url and parts.hostname not in ("127.0.0.1", "localhost", "::1"):
        raise FetchError("not a local address")


def _quote(value: str) -> str:
    """A value for curl's config file: one line, quoted."""
    if any(ord(c) < 32 or c == "\x7f" for c in value):
        raise FetchError("control characters in a request header")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def fetch(url: str, max_bytes: int = 2 * 1024 * 1024, timeout: float = 20, redirects: int = 3,
          headers: dict | None = None, data: bytes | None = None, method: str | None = None,
          head: bool = False, local: bool = False, user_url: bool = False, include: bool = False,
          errors: bool = False) -> bytes:
    """The body (or, with head=True, the response headers) of url; raises FetchError. errors: an
    answer with an error status is returned too (with include, its status line says which), for a
    service whose error answer is what the caller wants to read."""
    _check(url, local or user_url, user_url)
    config = [f"url = {_quote(url)}"]
    for name, value in (headers or {}).items():
        if not re.fullmatch(r"[A-Za-z0-9-]{1,100}", str(name)):
            raise FetchError("not a header name")
        config.append(f"header = {_quote(f'{name}: {value}')}")
    argv = ["curl", "--config", "-", "--silent", "--show-error"] + ([] if errors else ["--fail"]) + [
            "--proto", "=https" if not (local or user_url) else "=http,https",
            "--max-time", str(int(timeout)), "--max-filesize", str(max_bytes),
            "--user-agent", "ai-agents-map"]
    if local or user_url:
        argv += ["--max-redirs", "0"] + (["--noproxy", "*"] if local else [])
    elif redirects > 0:
        argv += ["--location", "--max-redirs", str(redirects), "--proto-redir", "=https"]
    if head:
        argv += ["--head"]
    if include:
        argv += ["--include"]          # the response headers before the body
    if method:
        argv += ["--request", method]
    stdin = ("\n".join(config) + "\n").encode()
    if data is not None:
        # The body after the config would need a second stdin: put it into the config as a quoted value.
        stdin = ("\n".join(config + [f"data-binary = {_quote(data.decode('utf-8'))}"]) + "\n").encode()
    out, ok = ai_core.read_bounded_output(argv, max_bytes=max_bytes + (64 * 1024 if head else 0),
                                          timeout=timeout + 5, stdin=stdin)
    if not ok:
        host = urlsplit(url).hostname
        raise FetchError(f"{host} did not answer (or the answer was too large)")
    return out


def fetch_json(url: str, **kwargs):
    body = fetch(url, **kwargs)
    try:
        return json.loads(body.decode("utf-8", "replace"))
    except ValueError as error:
        raise FetchError("not a JSON answer") from error
