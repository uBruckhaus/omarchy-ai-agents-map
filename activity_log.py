"""The local AI activity log (Maprisma tasks 074, 075, 076: ai_activity_log.dart, history_topic.dart).

Opt-in (Settings › AI activity log, off by default). While the window runs, the map's poll tells
observe() which agents work right now; when one stops working, one row is written for that span of
work: when, which agent, the project folder's name, the names of files changed in it meanwhile and,
when known, the tokens used. "Collect history" (history_import.py) adds rows from the agents' own
session files. Nothing else: no file contents, no paths beyond names, no secrets.

Rows are JSON lines in day files, ${XDG_DATA_HOME:-~/.local/share}/ai-agents-map/ai-log/YYYY-MM-DD.jsonl
(0600, folder 0700, owner checked, never through a link), filed by the local day a span started. Day
files older than the retention (Settings › Keep log for, 7 days by default, including today) are
removed on the next write. Each day file stays within 2 MiB. Every row read back is checked and
cleaned again (_rows), so a hand-edited file cannot inject text the page or a report would show.

Two locks in the folder: `.lock` (only around one day file's read-merge-write; observe() never
waits for it and keeps its spans for the next tick instead) and `.import.lock` (held by an import
for its whole run, history_import.py).
"""

from __future__ import annotations

import datetime as dt
import errno
import fcntl
import hashlib
import json
import os
import re
import stat
import threading
import time
import unicodedata
from pathlib import Path
from urllib.parse import unquote, urlsplit

MAX_FILE = 2 << 20                 # bytes per day file
PAGE_SIZE = 100                    # entries per report page
MAX_FILES = 30                     # file names per row
DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SOURCE = re.compile(r"^(agent|runtime):[a-z0-9][a-z0-9_.-]{0,63}$")
LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
SECRETISH = re.compile(r"(^[a-f0-9]{24,}$|(?:^|[._-])(?:secret|token|credentials?|auth|password|apikey|key)(?:[._-]|$)"
                       r"|^sk-[A-Za-z0-9_-]+)", re.IGNORECASE)
SESSION = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
UNOBSERVED = dt.timedelta(seconds=60)   # observe() comes every 2 s while the window is shown
LOCK_WAIT = 5.0                    # seconds an import waits for a day file's lock
HEURISTIC = ("editing", "testing")


class ActivityError(Exception):
    """A refusal with Maprisma's code (log_off, import_off, import_running, invalid_request, …)."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


# --- folders and settings -----------------------------------------------------------------------

def data_dir() -> Path:
    """${XDG_DATA_HOME:-~/.local/share}/ai-agents-map: the log and the report library."""
    import identity
    configured = os.environ.get("XDG_DATA_HOME", "")
    root = Path(configured) if configured and Path(configured).is_absolute() else identity.HOME / ".local/share"
    return root / identity.short_name()


def identity_home() -> Path:
    import identity
    return identity.HOME


def log_dir() -> Path:
    return data_dir() / "ai-log"


def _setting(key: str):
    import ai_core
    return ai_core.setting(key)


def private_dir(path: Path) -> int:
    """Create (0700) or check the folder: a real folder of this account, not a link; its descriptor."""
    import ai_core
    path = Path(os.path.abspath(path))
    ai_core._resolve_private_parent(path.parent)       # every ancestor this account's or root's, not writable by others
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise PermissionError(errno.EPERM, "not a private folder", str(path))
        if stat.S_IMODE(info.st_mode) != 0o700:
            os.fchmod(fd, 0o700)
        return fd
    except BaseException:
        os.close(fd)
        raise


def open_lock(dir_fd: int, name: str) -> int:
    """A lock file in the folder, opened without following a link, this account's regular file."""
    fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, 0o600, dir_fd=dir_fd)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        os.close(fd)
        raise PermissionError(errno.EPERM, "not a private lock file", name)
    return fd


def try_lock(fd: int) -> bool:
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def read_own(dir_fd: int, name: str, limit: int) -> bytes | None:
    """A file of this account in the folder, at most limit bytes (None: missing, not ours, a link, larger)."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dir_fd)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_size > limit:
            return None
        chunks, left = [], limit + 1
        while left > 0:
            chunk = os.read(fd, min(left, 1 << 20))
            if not chunk:
                break
            chunks.append(chunk)
            left -= len(chunk)
        data = b"".join(chunks)
        return None if len(data) > limit else data
    except OSError:
        return None
    finally:
        os.close(fd)


# --- times ----------------------------------------------------------------------------------------

def iso(moment: dt.datetime) -> str:
    """UTC ISO 8601 with microseconds and Z (the wire form)."""
    return moment.astimezone(dt.timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def parse_time(value) -> dt.datetime | None:
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        moment = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=dt.timezone.utc)


def micros(moment: dt.datetime) -> int:
    delta = moment - dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


def day_of(moment: dt.datetime) -> str:
    """The local calendar day of a moment."""
    return moment.astimezone().date().isoformat()


def local_date(moment: dt.datetime, offset: int = 0) -> dt.date:
    return moment.astimezone().date() + dt.timedelta(days=offset)


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


# --- cleaning -------------------------------------------------------------------------------------

def label(value) -> str | None:
    """A short name (project, file, model) kept only when it looks like a name and not like a secret."""
    if not isinstance(value, str) or not LABEL.match(value) or SECRETISH.search(value):
        return None
    return value


def meta_text(value, most: int = 200) -> str:
    """One line of plain text: control characters become spaces, whitespace collapsed, at most `most` characters."""
    if not isinstance(value, str):
        return ""
    text = "".join(" " if ord(c) < 32 or 0x7f <= ord(c) < 0xa0 or 0xd800 <= ord(c) <= 0xdfff else c for c in value)
    return " ".join(text.split())[:most]


def history_session(value: str) -> str:
    return value if SESSION.match(value) else hashlib.sha256(value.encode("utf-8", "surrogatepass")).hexdigest()


_URL = re.compile(r"[A-Za-z][A-Za-z0-9+.-]{0,31}://\S+")  # also glued to a prefix ("1.https://…")
_PROVIDER = re.compile(r"\b(?:sk-|gh[pousr]_|github_pat_|glpat-|AIza|AKIA|ASIA|xox[baprs]-|hf_|npm_|pypi-)[A-Za-z0-9_+=/.-]+")
# Escapes consume the following character (including newlines), never a closing quote of their own.
_QUOTED_SECRET = r"""(?:"(?:\\[\s\S]|[^"\\])*"|'(?:\\[\s\S]|[^'\\])*')"""
_SECRETS = [re.compile(p, re.IGNORECASE | re.DOTALL) for p in (
    r"-----BEGIN [^-]{1,64}-----(?:.*?-----END [^-]{1,64}-----|.*$)",
    r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+",
    r"\b(?:Bearer|Basic)\s+[^\s,;]+",
    r"""(?<![A-Za-z0-9_.-])[A-Za-z0-9_.-]*(?:key|token|secret|password|authorization|auth|sig)["']?\s*[:=]\s*"""
    rf"(?:{_QUOTED_SECRET}|[^\s,;]+)",
    r"(?<![A-Za-z0-9+/_])(?:[A-Fa-f0-9]{32,}|[A-Za-z0-9+/]{30,}==|[A-Za-z0-9+/]{31,}=|[A-Za-z0-9+/]{32,})(?![A-Za-z0-9+/=_])",
    r"(?<![A-Za-z0-9.!#$%&*+/=?^_{}|~-])[A-Za-z0-9.!#$%&*+/=?^_{}|~-]{1,64}@[A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,}\b",
)]
# Control and bidi characters, and lone UTF-16 surrogates ("\ud83d" from a cut emoji in an agent's
# JSON: they cannot be written as UTF-8, shown by GTK or sent anywhere).
# Redacted before paths are cut: PEM blocks, JWTs and key=/token=/password= assignments whose value is
# quoted or a URL (a plain unquoted path value is left to the path rule, then redacted).
_WHOLE_SECRETS = [_SECRETS[0], _SECRETS[1], re.compile(
    # HTTP Authorization headers (also Proxy-/X-Authorization): everything after the header to the
    # end of its line goes, whatever the scheme, quoting or URL (no path rule needed after it).
    # The value may start on the next line (YAML, pretty JSON, folded headers) or be a multi-line quote.
    # Known schemes may precede an unindented credential; unknown schemes require header folding.
    r"""(?<![A-Za-z0-9_.-])[A-Za-z0-9_.-]*authorization["']?\s*[:=]\s*"""
    r"(?:(?:(?:Bearer|Basic|Token|ApiKey|OAuth|Bot|Digest)\s+|"
    rf"[A-Za-z][A-Za-z0-9+._-]*(?:[ \t]+|[ \t]*\r?\n[ \t]+))?{_QUOTED_SECRET}[^\n]*"
    r"|(?:(?:Bearer|Basic|Token|ApiKey|OAuth|Bot|Digest)\s+|"
    r"[A-Za-z][A-Za-z0-9+._-]*[ \t]*\r?\n[ \t]+)[^\n]*|[^\n]*)",
    re.IGNORECASE), re.compile(
    r"""(?<![A-Za-z0-9_.-])[A-Za-z0-9_.-]*(?:key|token|secret|password|authorization|auth|sig)["']?\s*[:=]\s*"""
    rf"(?:{_QUOTED_SECRET}|[A-Za-z][A-Za-z0-9+.-]*://\S+)", re.IGNORECASE)]
# A whitespace token holding a URL together with userinfo (@ or an encoded @, raw or double-encoded)
# fails closed as a whole, wherever the scheme starts inside it.
_CREDENTIAL_URL_TOKEN = re.compile(r"\S+")  # every whitespace token; the callback skips those without "://"
_CONTROL = re.compile("[\x00-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069\ud800-\udfff]")
SURROGATES = re.compile("[\ud800-\udfff]")


def plain(text: str) -> str:
    """Text safe to write as UTF-8 and to hand to GTK: lone surrogates (also from file names that are
    not UTF-8) become the replacement character."""
    return SURROGATES.sub("\ufffd", text)
_SLASH_COMMAND = re.compile(r"/[A-Za-z][\w-]*")


def _redact(text: str) -> str:
    text = _URL.sub(lambda m: "…" if "@" in m.group(0) or "%40" in m.group(0).lower() else m.group(0), text)
    text = _PROVIDER.sub("…", text)
    for pattern in _SECRETS:
        text = pattern.sub("…", text)
    return text


def _decode_url_part(raw: str) -> str:
    """Decode only inside a URL; never substitute this text into the surrounding request."""
    for _ in range(3):
        decoded = unquote(raw)
        if decoded == raw:
            break
        raw = decoded
    return unicodedata.normalize("NFKC", raw)


_USER_COLON = re.compile(r"[^:@/\s]:")
_ENCODED_AT = re.compile(r"%(?:25)*40", re.IGNORECASE)
_ENCODED_COLON = re.compile(r"%(?:25)*3a", re.IGNORECASE)


def _userinfo(text: str) -> bool:
    """user:password@host in linear time, also :password@host (empty user) and a cut-off user:password@."""
    at = text.rfind("@")
    if at < 2 or text[at + 1:at + 8].lower() in ("sha256:", "sha512:") or text[at + 1:at + 6].lower() == "sha1:":
        return False                       # an image digest (img:1.2@sha256:…), not userinfo
    colon = text.find(":", 0, at)
    while colon >= 0 and (text[colon + 1:colon + 2] == ":" or text[colon - 1:colon] == ":"):
        colon = text.find(":", colon + 2, at)  # a scope operator (std::vector@), not user:password
    if colon < 0:
        return False
    user = text[:colon].rsplit("/", 1)[-1]
    if user.isdigit() and text[colon + 1:at].isdigit():
        return False                       # a time or ratio (10:30@office, 3:1@scale)
    return at < len(text) - 1 or colon + 1 < at


def _credential_token(token: str) -> bool:
    """A whitespace token that holds credentials: a URL with userinfo (@ raw or encoded) anywhere in
    it, or user:password@host with or without a scheme (also after decoding)."""
    if "@" not in token and "%" not in token and "\uff20" not in token and "\ufe6b" not in token:
        return False
    decoded = _decode_url_part(token)
    flat = _ENCODED_COLON.sub(":", _ENCODED_AT.sub("@", token))   # any depth of %25 encoding
    if _userinfo(token) or _userinfo(decoded) or _userinfo(flat) or _userinfo(_decode_url_part(flat)):
        return True
    has_url = "://" in token or "://" in decoded or "%3a" in token.lower()
    return has_url and ("@" in token or "@" in decoded or "@" in flat)


def _clean_url(raw: str) -> tuple[str, str]:
    """An optional HTTP(S) origin and clean basename, with credentials failing closed."""
    decoded = _decode_url_part(raw)
    if "@" in decoded or re.search(r"%(?:25)*40", raw, re.IGNORECASE):
        return "", "…"
    # A second URL chained into this one, or an authority with a colon that is no numeric port
    # ("host:secret"), fails closed: the text after the scheme may be userinfo.
    # A chained URL hidden behind an encoded ? or # (raw, before decoding) fails closed as well.
    raw_head = re.split(r"[?#]", raw.split("://", 1)[1], maxsplit=1)[0] if "://" in raw else ""
    raw_head = _ENCODED_COLON.sub(":", re.sub(r"%(?:25)*2f", "/", raw_head, flags=re.IGNORECASE))  # any depth
    if "://" in raw_head or "://" in _decode_url_part(raw_head):
        return "", "…"
    rest = decoded.split("://", 1)[1] if "://" in decoded else ""
    authority = re.split(r"[/?#,;|&]", rest, maxsplit=1)[0]
    if "://" in re.split(r"[?#]", rest, maxsplit=1)[0] or (":" in authority and not re.fullmatch(r"(?:[^:\[\]]+|\[[0-9A-Fa-f:.]+\])(?::\d{0,5})?", authority)):
        return "", "…"
    try:
        # Drop raw queries/fragments before decoding, including wrapped Markdown/quoted URLs.
        url = urlsplit(raw.rstrip("'\"`)]},>"))
        path = _decode_url_part(url.path).replace("\\", "/")
        if "://" in url.path or "://" in path:
            return "", "…"  # a chained URL's credentials must never become the first URL's basename
        path = re.split(r"[?#]", path, maxsplit=1)[0]
        name = path.rsplit("/", 1)[-1]
        if any(c.isspace() or c in "/\\%" for c in name) or _CONTROL.search(name):
            name = ""
        if url.scheme.lower() in ("http", "https"):
            host = _decode_url_part(url.netloc)
            label = r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
            host_ok = "…" not in url.netloc and re.fullmatch(
                rf"(?:{label}(?:\.{label})*\.?|\[[0-9A-Fa-f:.]+\])(?::\d{{1,5}})?", host or "")
            if host_ok and not _CONTROL.search(host):
                origin = urlsplit(url.scheme + "://" + host)
                origin.port  # a non-numeric "port" (user:password without @) fails closed below
                if origin.hostname:
                    return origin.scheme + "://" + origin.netloc, name
            # Hostless HTTP URLs still have their directory components removed.
            if url.netloc:
                return "", "…"
        return "", name or "…"
    except ValueError:
        return "", "…"


_ENCODED_SEP = re.compile(r"%(?:25)*(2[Ff]|5[Cc]|[Ee][Ff]%(?:25)*[Bb][Cc]%(?:25)*8[Ff])")
# The URL alternative consumes the entire raw token before encoded separators can match in it.
_URL_OR_ENCODED_SEP = re.compile(_URL.pattern + "|" + _ENCODED_SEP.pattern)
_PATH_SEPARATOR = re.compile(r"[/\\]")


def _short_paths(text: str) -> str:
    """Discard from the first separator token through the last separator, regardless of grammar.

    Quotes, flags, variables and whitespace inside directory names do not delimit a path.
    Only standalone slash commands and spaced division slashes are exempt.
    """
    first = None
    first_url = last_url = None
    last = end = 0
    for token in re.finditer(r"\S+", text):
        if _SLASH_COMMAND.fullmatch(token.group()):
            continue
        # URL matches remain raw, atomic spans. Decoding can neither introduce outer tokens nor
        # erase the first separator boundary of a space-continued path (including userinfo URLs).
        cursor = token.start()
        spans = []
        for url in _URL.finditer(text, token.start(), token.end()):
            spans.extend((sep.start(), sep.end(), None)
                         for sep in _PATH_SEPARATOR.finditer(text, cursor, url.start()))
            spans.append((url.start(), url.end(), _clean_url(url.group())))
            cursor = url.end()
        spans.extend((sep.start(), sep.end(), None)
                     for sep in _PATH_SEPARATOR.finditer(text, cursor, token.end()))
        for start, stop, url in spans:
            if (url is None and text[start:stop] == "/" and start > 0 and stop < len(text)
                    and text[start - 1].isspace() and text[stop].isspace()):
                continue
            if first is None:
                first = token.start()
                first_url = (start, url) if url is not None else None
            last, end = stop, token.end()
            last_url = (start, url) if url is not None else None
    if first is None:
        return text
    if first_url and first_url[1][0]:
        origin, name = first_url[1]
        # A later path/URL must never supply a name for the first URL's host.
        kept = origin + ("/" + name if first_url == last_url and name else "")
    elif last_url:
        origin, name = last_url[1]
        kept = origin + ("/" + name if origin and name else name)
    else:
        # Keep any secret crossing the cut redacted, without erasing path boundaries first.
        # Provider tokens, assignments, PEM blocks and base64 can all include separators.
        cut = last
        secrets = sorted((m.start(), m.end()) for pattern in (_PROVIDER, *_SECRETS)
                         for m in pattern.finditer(text))
        prefix_end = first
        for start, stop in secrets:
            if start < first and stop > last:
                prefix_end = min(prefix_end, start)
            if start <= cut < stop:
                cut = stop
        return text[:prefix_end] + ("…" if prefix_end < first or cut > last else "") + text[cut:]
    return text[:first] + kept + text[end:]


def sanitize_topic(value) -> str | None:
    """Normalize a request, redact secrets and conservatively remove directories, then cut to 120.

    HTTP(S) URLs retain only their host and final path name; queries and fragments are dropped.
    """
    if not isinstance(value, str):
        return None
    text = unicodedata.normalize("NFKC", value[:256 * 1024])
    # Keep encoded-separator handling outside URLs, including nested %252F / %255C.
    text = _URL_OR_ENCODED_SEP.sub(lambda m: m.group() if m.group(1) is None else
                                   ("\\" if m.group(1).lower() == "5c" else "/"), text)
    # Whole secrets first (PEM, JWT, Authorization, key=/token=/password= assignments with quoted or URL
    # values): cutting paths could otherwise split a quoted value and leave its rest readable.
    if any(_credential_token(word) for word in text.split()):
        return None  # a topic that held credentials is not kept at all: the caller's heuristic topic is used
    for pattern in _WHOLE_SECRETS:
        text = pattern.sub("…", text)
    text = _short_paths(text)
    text = _CONTROL.sub(" ", text)
    text = " ".join(_redact(text).split())[:120]
    return text or None


def _tokens(value) -> int | None:
    return value if type(value) is int and 0 <= value <= 1 << 45 else None


def clean_rows(text: str):
    """The valid rows of a day file, each checked and cleaned (Maprisma's _rows)."""
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict) or row.get("version") != 1:
            continue
        kind, source = row.get("source_kind"), row.get("source")
        duration = row.get("duration_ms")
        if not isinstance(kind, str) or not isinstance(source, str) or not SOURCE.match(f"{kind}:{source}") \
                or type(duration) is not int or duration < 0:
            continue
        started, ended = parse_time(row.get("started_at")), parse_time(row.get("ended_at"))
        if started is None or ended is None or ended < started:
            continue
        history = row.get("origin") == "history"
        session = history_session(row["session"]) if isinstance(row.get("session"), str) else None
        if history and session is None:
            continue
        start_us = micros(started)
        row_id = f"history:{kind}:{source}:{session}:{start_us}" if history else f"{kind}:{source}:{start_us}"
        superseded = row.get("superseded_by")
        files = row.get("files") if isinstance(row.get("files"), list) else []
        yield {
            "version": 1, "id": row_id, "origin": "history" if history else "live", "session": session,
            "superseded_by": superseded if isinstance(superseded, str) and superseded.startswith("history:")
            and len(superseded) <= 300 else None,
            "source_kind": kind, "source": source, "model": label(row.get("model")),
            "project": label(row.get("project")), "topic": sanitize_topic(row.get("topic")),
            "started_at": iso(started), "ended_at": iso(ended),
            "duration_ms": (micros(ended) - start_us) // 1000,
            "files": [f for f in (label(v) for v in files[:MAX_FILES]) if f is not None],
            "helpers": [], "tokens_in": _tokens(row.get("tokens_in")), "tokens_out": _tokens(row.get("tokens_out")),
            "cost": None, "outcome": "unknown",
        }


def _encoded(rows) -> bytes:
    return plain("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n").encode("utf-8")


def _fits(rows) -> bool:
    return len(_encoded(rows)) <= MAX_FILE


def overlaps(live: dict, history: dict) -> bool:
    """A live row is covered by a history span of the same agent (and project when both have one)
    when more than half of the live row lies inside it; a zero-length one when it starts inside."""
    if live.get("origin") == "history" or history.get("origin") != "history" \
            or live["source"] != history["source"] or live["source_kind"] != history["source_kind"] \
            or (live.get("project") and history.get("project") and live["project"] != history["project"]):
        return False
    a, b = parse_time(live["started_at"]), parse_time(live["ended_at"])
    h_start, h_end = parse_time(history["started_at"]), parse_time(history["ended_at"])
    left, right = max(a, h_start), min(b, h_end)
    duration = micros(b) - micros(a)
    if duration == 0:
        return h_start <= a <= h_end
    return (micros(right) - micros(left)) * 2 > duration


def _larger(old, new):
    return new if old is None else old if new is None or old > new else new


# --- the log --------------------------------------------------------------------------------------

class ActivityLog:
    """The log in one folder. setting(key) reads a setting (ai_core.setting by default)."""

    def __init__(self, directory: Path | None = None, setting=None, timeline=None):
        self.directory = Path(directory) if directory is not None else log_dir()
        self.setting = setting or _setting
        self.timeline = timeline          # (roots, since epoch seconds) -> groups (agent_activity_report)
        self.pending: list[dict] = []
        self.active: dict[str, tuple] = {}
        self.last_seen: dt.datetime | None = None      # the last observe(): a longer gap closes open spans
        self.unobserved = UNOBSERVED
        self.accepting = True                           # False while nothing watches (the window hidden)
        self.hidden_at: dt.datetime | None = None       # when it hid, until the spans open then are ended
        self.mutex = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self.setting("ai_activity_log") is True

    @property
    def keep(self) -> int:
        value = self.setting("ai_activity_log_days")
        return value if type(value) is int and 1 <= value <= 365 else 7

    @property
    def import_topics(self) -> bool:
        return self.setting("ai_activity_import_topics") is True

    # -- live observation -------------------------------------------------------------------------

    def observe(self, active, working_dirs: dict | None = None, usage_for=None, now: dt.datetime | None = None):
        """From the poll: `active` = {"agent:<command>", "runtime:<key>"} working now. A source that
        stopped working gets its row. Never waits for the day lock: when it is taken, the rows wait
        in memory for the next call."""
        now = now or now_utc()
        with self.mutex:
            self._observe(set(active), working_dirs or {}, usage_for, now)

    def pause(self, at: dt.datetime | None = None):
        """Nothing watches the agents from `at` on (the window hides): observations are ignored until
        resume(), and what was open then ends at `at` (end_open, or the first observation after
        resume() when that comes first). Hidden again before that happened, the earlier cutoff stays:
        nothing was observed in between. Cheap, for the window's thread: no lock is waited for."""
        self.accepting = False
        at = at or now_utc()
        if self.hidden_at is None:
            self.hidden_at = at
        return at

    def resume(self):
        """Observed again. The spans of before the pause are still ended at the pause's time: by
        end_open(), or by the next observation (_finish_pause) if that comes first."""
        self.accepting = True

    def _finish_pause(self, usage_for=None) -> list:
        """Under the lock: the spans that were open when the window hid, ended at that moment; a span
        begun after it stays open. Done once per pause (whoever comes first)."""
        at, self.hidden_at = self.hidden_at, None
        if at is None:
            return []
        completed = []
        for source, (start, cwd, usage) in list(self.active.items()):
            if start < at:
                del self.active[source]
                completed.append(self._live_row(source, start, cwd, usage, usage_for, at))
        if not self.active:
            self.last_seen = None             # no surviving span needs the unobserved-gap cutoff
        return completed

    def end_open(self, at: dt.datetime, usage_for=None):
        """On a worker after the window hid: end what was open at `at` and write it. The same lock as
        observe() keeps the two in order; an already-finished or newer pause leaves it alone."""
        with self.mutex:
            if not self.enabled:              # switched off meanwhile: nothing of it is kept
                self.active.clear()
                self.pending.clear()
                self.hidden_at = None
                return
            if self.hidden_at is None or self.hidden_at > at:
                return                        # already ended (by an observation or an earlier worker)
            self.pending.extend(self._finish_pause(usage_for))
            if self.pending:
                self._observe(set(), {}, None, at, flush_only=True)

    def _observe(self, active: set, working_dirs: dict, usage_for, now: dt.datetime, flush_only: bool = False):
        if not self.accepting and not flush_only:
            return
        enabled = self.enabled
        if not flush_only and self.hidden_at is not None:
            if enabled:
                self.pending.extend(self._finish_pause())     # the hide's spans, before anything new
            else:
                self.hidden_at = None
        keep = self.keep
        cutoff = local_date(now, 1 - keep).isoformat()
        if not enabled:
            self.active.clear()
            self.pending.clear()
        completed = list(self.pending)
        self.pending.clear()
        last, self.last_seen = (None, self.last_seen) if flush_only else (self.last_seen, now)
        if enabled and last is not None and now - last > self.unobserved:
            # Not looked at meanwhile (window hidden, PC asleep): what was working is counted only up
            # to the last look, never across the gap; still working now, it starts a new span.
            for source in list(self.active):
                start, cwd, _usage = self.active.pop(source)
                if start < last:
                    completed.append(self._live_row(source, start, cwd, None, None, last))
        if enabled:
            for source in list(self.active):
                if source in active:
                    continue
                start, cwd, usage = self.active.pop(source)
                if now < start:
                    continue
                completed.append(self._live_row(source, start, cwd, usage, usage_for, now))
            for source in active:
                if SOURCE.match(source) and source not in self.active:
                    self.active[source] = (now, working_dirs.get(source),
                                           usage_for(source) if usage_for is not None else None)
        if not completed and not self.directory.exists():
            return
        dir_fd = lock = None
        try:
            dir_fd = private_dir(self.directory)
            lock = open_lock(dir_fd, ".lock")
            if not try_lock(lock):
                self.pending.extend(completed)
                return
            self._prune(dir_fd, cutoff)
            if not completed or not self.enabled:
                return
            grouped: dict[str, list] = {}
            for row in completed:
                day = day_of(parse_time(row["started_at"]))
                if day >= cutoff:
                    grouped.setdefault(day, []).append(row)
            for day, new in grouped.items():
                rows = list(clean_rows((read_own(dir_fd, f"{day}.jsonl", MAX_FILE) or b"").decode("utf-8", "replace")))
                prior = (dt.date.fromisoformat(day) - dt.timedelta(days=1)).isoformat()
                history = rows + list(clean_rows(
                    (read_own(dir_fd, f"{prior}.jsonl", MAX_FILE) or b"").decode("utf-8", "replace")))
                changed = False
                for row in new:
                    if any(r["id"] == row["id"] for r in rows):
                        continue
                    for old in history:
                        if overlaps(row, old):
                            row["superseded_by"] = old["id"]
                            break
                    rows.append(row)
                    if not _fits(rows):
                        rows.pop()        # the day's cap is final for this row: retrying cannot make room
                    else:
                        changed = True
                if changed:
                    self._write(day, rows)
        except OSError:
            self.pending.extend(r for r in completed if r not in self.pending)
        finally:
            if lock is not None:
                os.close(lock)
            if dir_fd is not None:
                os.close(dir_fd)

    def _live_row(self, source, start, cwd, usage, usage_for, now) -> dict:
        kind, _, name = source.partition(":")
        last = usage_for(source) if usage is not None and usage_for is not None else None
        same = last is not None and last.get("session") == usage.get("session") \
            and last.get("input", -1) >= usage.get("input", 0) and last.get("output", -1) >= usage.get("output", 0)
        timeline = []
        if cwd and self.timeline is not None:
            try:
                timeline = self.timeline({cwd}, start.timestamp())
            except Exception:
                timeline = []
        changed = []
        for group in timeline:
            for item in group["files"]:
                at = parse_time(item["at"])
                name_ = label(os.path.basename(item["path"]))
                if at is not None and at < now and name_ and name_ not in changed:
                    changed.append(name_)
        topic = None if not timeline else "testing" if any(g["kind"] == "testing" for g in timeline) else "editing"
        return {
            "version": 1, "origin": "live", "session": None, "superseded_by": None,
            "id": f"{source}:{micros(start)}", "source_kind": kind, "source": name,
            "model": label(last.get("model")) if same else None,
            "project": label(os.path.basename(cwd.rstrip("/"))) if cwd else None, "topic": topic,
            "started_at": iso(start), "ended_at": iso(now), "duration_ms": (micros(now) - micros(start)) // 1000,
            "files": changed[:MAX_FILES], "helpers": [],
            "tokens_in": last["input"] - usage["input"] if same else None,
            "tokens_out": last["output"] - usage["output"] if same else None,
            "cost": None, "outcome": "unknown",
        }

    def _prune(self, dir_fd: int, cutoff: str):
        try:
            names = os.listdir(dir_fd)[:1024]
        except OSError:
            return
        for name in names:
            day = name[:-6] if name.endswith(".jsonl") else ""
            if not DAY.match(day) or day >= cutoff:
                continue
            try:
                info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
                if stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid():
                    os.unlink(name, dir_fd=dir_fd)
            except OSError:
                pass

    def _write(self, day: str, rows: list):
        import ai_core
        rows.sort(key=lambda r: r["started_at"])
        ai_core.atomic_write(self.directory / f"{day}.jsonl", _encoded(rows))

    def _read_day(self, dir_fd: int | None, day: str) -> list:
        if dir_fd is None:
            try:
                fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
            except OSError:
                return []
            try:
                if os.fstat(fd).st_uid != os.getuid():
                    return []
                return self._read_day(fd, day)
            finally:
                os.close(fd)
        return list(clean_rows((read_own(dir_fd, f"{day}.jsonl", MAX_FILE) or b"").decode("utf-8", "replace")))

    # -- history rows (history_import.py) ---------------------------------------------------------

    def merge_history(self, row: dict, leading_partial: bool = False, earliest_start: dt.datetime | None = None,
                      now: dt.datetime | None = None) -> tuple[str, dict | None]:
        """Write one history row under the short day lock: ("added" | "updated" | "unchanged" |
        "skipped", the row written). The same id updates its row only when it grew. A span cut off at
        the start of a tail read only updates a row it overlaps in the same session."""
        normal = list(clean_rows(json.dumps(row)))
        if not normal:
            return "skipped", None
        row = normal[0]
        start, end = parse_time(row["started_at"]), parse_time(row["ended_at"])
        target = day_of(start)
        if leading_partial:
            prior = None
            today, keep, first, last = self.range({}, now=now)
            for _local, candidate in self.retained_rows(today, keep, first, last):
                if candidate["origin"] == "history" and candidate["source"] == row["source"] \
                        and candidate["source_kind"] == row["source_kind"] and candidate["session"] == row["session"] \
                        and parse_time(candidate["ended_at"]) >= start and parse_time(candidate["started_at"]) <= end:
                    prior = candidate
                    break
            if prior is None or (earliest_start is not None and parse_time(prior["started_at"]) < earliest_start):
                return "skipped", None
            target = day_of(parse_time(prior["started_at"]))
        dir_fd = private_dir(self.directory)
        lock = None
        try:
            lock = open_lock(dir_fd, ".lock")
            waited = time.monotonic()
            while not try_lock(lock):
                if time.monotonic() - waited > LOCK_WAIT:
                    raise ActivityError("import_running")
                time.sleep(0.02)
            if not self.enabled:
                raise ActivityError("log_off")
            rows = self._read_day(dir_fd, target)
            match = next((i for i, r in enumerate(rows) if r["id"] == row["id"] or (
                leading_partial and r["origin"] == "history" and r["source"] == row["source"]
                and r["session"] == row["session"] and parse_time(r["ended_at"]) >= start
                and parse_time(r["started_at"]) <= end)), -1)
            result = "added"
            if match >= 0:
                old = rows[match]
                files = list(dict.fromkeys(old["files"] + row["files"]))[:MAX_FILES]
                grows = end > parse_time(old["ended_at"]) or len(files) > len(old["files"]) or (
                    not leading_partial and ((row["tokens_in"] or 0) > (old["tokens_in"] or 0)
                                             or (row["tokens_out"] or 0) > (old["tokens_out"] or 0)))
                if not grows:
                    # Nothing new, but the live rows it covers are marked again (a write of an earlier
                    # import may have stopped after the start day, before the end day).
                    return self._supersede_again(dir_fd, target, rows, old), old
                ended = old["ended_at"] if parse_time(old["ended_at"]) > end else row["ended_at"]
                row = {**row, "id": old["id"], "started_at": old["started_at"], "ended_at": ended,
                       "tokens_in": old["tokens_in"] if leading_partial else _larger(old["tokens_in"], row["tokens_in"]),
                       "tokens_out": old["tokens_out"] if leading_partial else _larger(old["tokens_out"], row["tokens_out"]),
                       "files": files,
                       **{f: old[f] if leading_partial else (row[f] if row[f] is not None else old[f])
                          for f in ("topic", "model", "project")}}
                row["duration_ms"] = (micros(parse_time(row["ended_at"])) - micros(parse_time(row["started_at"]))) // 1000
                rows[match] = row
                result = "updated"
            elif leading_partial:
                return "skipped", None
            else:
                rows.append(row)
            if not _fits(rows):
                return "skipped", None

            def supersede(target_rows) -> bool:
                changed = False
                for live in target_rows:
                    if overlaps(live, row) and live["superseded_by"] != row["id"]:
                        live["superseded_by"] = row["id"]
                        changed = True
                return changed
            supersede(rows)
            if not _fits(rows):
                return "skipped", None
            self._write(target, rows)
            end_day = day_of(parse_time(row["ended_at"]))
            if end_day != target:
                other = self._read_day(dir_fd, end_day)
                if supersede(other) and _fits(other):
                    self._write(end_day, other)
            return result, row
        finally:
            if lock is not None:
                os.close(lock)
            os.close(dir_fd)

    def _supersede_again(self, dir_fd: int, target: str, rows: list, history: dict) -> str:
        """Under the day lock: the live rows a stored history row covers, on its start and its end day,
        marked as covered where they are not yet; "unchanged" either way (no new history)."""
        def mark(day_rows) -> bool:
            changed = False
            for live in day_rows:
                if overlaps(live, history) and live["superseded_by"] != history["id"]:
                    live["superseded_by"] = history["id"]
                    changed = True
            return changed
        if mark(rows) and _fits(rows):
            self._write(target, rows)
        end_day = day_of(parse_time(history["ended_at"]))
        if end_day != target:
            other = self._read_day(dir_fd, end_day)
            if mark(other) and _fits(other):
                self._write(end_day, other)
        return "unchanged"

    # -- reading ----------------------------------------------------------------------------------

    def range(self, params: dict, now: dt.datetime | None = None) -> tuple:
        """(now, keep, from_day, through_day) of a report's parameters; ValueError when they are wrong."""
        today = now or now_utc()
        keep = self.keep

        def day(name, fallback):
            value = params.get(name, fallback)
            if value is None:
                value = fallback
            if not isinstance(value, str) or not DAY.match(value):
                raise ValueError("Expected a calendar day")
            try:
                dt.date.fromisoformat(value)
            except ValueError:
                raise ValueError("Expected a calendar day") from None
            return value
        first = day("from_day", local_date(today, 1 - keep).isoformat())
        last = day("through_day", local_date(today).isoformat())
        if first > last:
            raise ValueError("Invalid activity range")
        return today, keep, first, last

    def retained_rows(self, today, keep: int, first: str, last: str):
        """(local date, row) of every row kept, newest day first, newest row first; superseded ones left out."""
        for offset in range(keep):
            local = local_date(today, -offset)
            day = local.isoformat()
            if day < first or day > last:
                continue
            for row in reversed(self._read_day(None, day)):
                if row["superseded_by"] is None:
                    yield local, row

    def report(self, params: dict | None = None, now: dt.datetime | None = None) -> dict:
        """activitylog.report: one page of entries (100) and totals by agent, project, day and week."""
        params = params or {}
        today, keep, first, last = self.range(params, now=now)
        cursor = params.get("cursor")
        cursor = 0 if cursor is None else cursor
        if type(cursor) is not int or cursor < 0:
            raise ValueError("Invalid activity range")
        page, totals = [], {"by_source": {}, "by_project": {}, "by_day": {}, "by_week": {}}

        def add(group, key, row):
            count, elapsed, tin, tout, unknown = totals[group].get(key, (0, 0, 0, 0, 0))
            a, b = row["tokens_in"], row["tokens_out"]
            totals[group][key] = (count + 1, elapsed + row["duration_ms"], tin + (a or 0), tout + (b or 0),
                                  unknown + (1 if a is None or b is None else 0))
        count = 0
        for local, row in self.retained_rows(today, keep, first, last):
            add("by_source", f"{row['source_kind']}:{row['source']}", row)
            if isinstance(row["project"], str):
                add("by_project", row["project"], row)
            add("by_day", local.isoformat(), row)
            add("by_week", (local - dt.timedelta(days=local.weekday())).isoformat(), row)
            if count >= cursor and len(page) < PAGE_SIZE:
                page.append(row)
            count += 1

        def listed(values):
            return [{"key": k, "count": v[0], "duration_ms": v[1], "tokens_in": None if v[4] == v[0] else v[2],
                     "tokens_out": None if v[4] == v[0] else v[3], "usage_unknown": v[4]} for k, v in values.items()]
        return {"from_day": first, "through_day": last, "entries": page,
                "next_cursor": cursor + len(page) if count > cursor + len(page) else None,
                **{k: listed(v) for k, v in totals.items()}}


# --- text forms -----------------------------------------------------------------------------------

def duration_text(ms: int) -> str:
    seconds = max(0, int(ms)) // 1000
    return f"{seconds // 3600}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"


def topic_text(topic) -> str:
    """editing / testing as words, a free-text topic as it is."""
    from i18n import _
    if topic == "testing":
        return _("Writing or running tests")
    if topic == "editing":
        return _("Editing")
    return topic if isinstance(topic, str) else ""


def row_text(row: dict) -> str:
    """One Log tab row (activity_log_pane _rowText)."""
    started = parse_time(row["started_at"])
    parts = [f"{row['source_kind']}:{row['source']}" if row.get("source_kind") else str(row["source"])]
    if isinstance(row.get("project"), str):
        parts.append(row["project"])
    if row.get("topic"):
        parts.append(topic_text(row["topic"]))
    parts.append(started.astimezone().strftime("%Y-%m-%d %H:%M:%S") if started else "")
    parts.append(duration_text(row["duration_ms"]))
    return " · ".join(parts)


TOTAL_HEADINGS = (("by_source", "By agent or runtime"), ("by_project", "By project"),
                  ("by_day", "By day"), ("by_week", "By week"))


def totals_text(report: dict) -> list[str]:
    from i18n import _
    lines = []
    for key, heading in TOTAL_HEADINGS:
        rows = report.get(key) or []
        if not rows:
            continue
        lines += ["", _(heading)]
        for row in rows:
            parts = [str(row["key"]), str(row["count"]), duration_text(row["duration_ms"])]
            if row["tokens_in"] is not None and row["tokens_out"] is not None:
                parts.append(f"{_('Tokens')} ↑{row['tokens_in']} ↓{row['tokens_out']}")
            lines.append(" · ".join(parts))
    return lines


def log_text(report: dict) -> str:
    """The Log tab's Copy and TXT export: free-text topics as they are, editing/testing as words."""
    return plain("\n".join([f"{report['from_day']} – {report['through_day']}"]
                           + [row_text(r) for r in report["entries"]] + totals_text(report)))


def format_activity_log(report: dict, as_json: bool) -> str:
    """Standalone history output (Maprisma's formatActivityLog): JSON keeps every field and the cursor."""
    if as_json:
        return json.dumps(report, indent=2, ensure_ascii=False)
    lines = [f"{report['from_day']} – {report['through_day']}"]
    for row in report["entries"]:
        parts = [row["started_at"], f"{row['source_kind']}:{row['source']}"]
        parts += [row[k] for k in ("project", "topic") if isinstance(row.get(k), str)]
        parts.append(duration_text(row["duration_ms"]))
        lines.append(" · ".join(parts))
    for key, heading in TOTAL_HEADINGS:
        lines += ["", heading]
        for row in report[key]:
            lines.append(f"{row['key']} · {row['count']} · {duration_text(row['duration_ms'])} · tokens "
                         f"{row['tokens_in'] if row['tokens_in'] is not None else '?'}/"
                         f"{row['tokens_out'] if row['tokens_out'] is not None else '?'} ({row['usage_unknown']} unknown)")
    if report.get("next_cursor") is not None:
        lines.append(f"Next cursor: {report['next_cursor']}")
    return "\n".join(lines)
