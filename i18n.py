"""Interface translations (built in; nothing is downloaded or generated).

gettext style: the English text is the key. `_("Add a shortcut")`, and for
text with values `_("Opens {0}").format(name)`. Anything without a
translation stays English. The language follows the system (LC_ALL,
LC_MESSAGES, LANG) unless one is picked in the settings (`language` in
SETTINGS_FILE, "auto" or a code).
"""

import json
import os
from pathlib import Path

LANGUAGES = [
    ("en", "English"), ("de", "Deutsch"), ("fr", "Français"), ("es", "Español"),
    ("it", "Italiano"), ("pt", "Português"), ("nl", "Nederlands"), ("pl", "Polski"),
]
CODES = {code for code, _name in LANGUAGES}
SETTINGS_FILE = None  # set by the plugin: Path of a small JSON settings file
_lang = None


def system_language():
    for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
        value = (os.environ.get(var) or "").lower()
        code = value.replace("-", "_").split("_")[0].split(".")[0].split("@")[0]
        if code in ("", "c", "posix"):
            continue
        return code if code in CODES else "en"
    return "en"


def language_setting():
    """'auto' or a language code from the settings file."""
    if SETTINGS_FILE is None:
        return "auto"
    try:
        with open(SETTINGS_FILE, "rb") as fh:
            data = json.loads(fh.read(64 * 1024) or b"{}")
    except (OSError, ValueError):
        return "auto"
    value = data.get("language") if isinstance(data, dict) else None
    return value if value in CODES else "auto"


def save_language(code):
    """Store the choice (atomic, private); "auto" follows the system again."""
    if SETTINGS_FILE is None:
        return
    path = Path(SETTINGS_FILE)
    try:
        with open(path, "rb") as fh:
            data = json.loads(fh.read(64 * 1024) or b"{}")
        data = data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        data = {}
    if code == "auto":
        data.pop("language", None)
    elif code in CODES:
        data["language"] = code
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    global _lang
    _lang = None


def current():
    global _lang
    if _lang is None:
        setting = language_setting()
        _lang = setting if setting != "auto" else system_language()
    return _lang


def language_name(code):
    return dict(LANGUAGES).get(code, code)


_RENAME = None   # (old name, new name): the same app under another name (set_name)


def set_name(old, new):
    """The same app under another name (AI Agents Map → Asterism): every text says the new one."""
    global _RENAME
    _RENAME = (old, new) if old and new and old != new else None


def _named(text):
    return text.replace(*_RENAME) if _RENAME else text


def _(text):
    return _named(STRINGS.get(current(), {}).get(text, text))


def N_(text):
    """Mark a module-level constant for translation without translating it yet:
    the language is only known once settings are read. Translate it with _() where it is used."""
    return text


PLURAL_PREFIX = "\x04"   # plural tables live under their own keys, apart from plain msgids


def ngettext(one, other, n):
    """Plural: tables store a list of forms under PLURAL_PREFIX + the English
    `one` text ([one, other], or [one, few, many] for Polish)."""
    lang = current()
    forms = STRINGS.get(lang, {}).get(PLURAL_PREFIX + one)
    if not isinstance(forms, list):
        return _named(one if n == 1 else other)
    if lang == "pl" and len(forms) == 3:
        m10, m100 = n % 10, n % 100
        return _named(forms[0] if n == 1 else forms[1] if 2 <= m10 <= 4 and not 12 <= m100 <= 14 else forms[2])
    if lang in ("fr", "pt"):
        return _named(forms[0] if n <= 1 else forms[-1])
    return _named(forms[0] if n == 1 else forms[-1])


# The tables: translations/<code>.json beside this file, one per language ({msgid: text}, plural
# forms under PLURAL_PREFIX + msgid as lists). Data files, read once, size-capped.
TRANSLATIONS = Path(__file__).resolve().parent / "translations"
MAX_TABLE = 2 * 1024 * 1024


def _load_tables() -> dict:
    tables = {}
    for code, _name in LANGUAGES:
        if code == "en":
            continue
        try:
            with open(TRANSLATIONS / f"{code}.json", "rb") as handle:
                data = json.loads(handle.read(MAX_TABLE + 1)[:MAX_TABLE])
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            tables[code] = {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, (str, list))}
    return tables


STRINGS = _load_tables()
