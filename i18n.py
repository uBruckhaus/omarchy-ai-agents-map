"""AI Agents Map fork: settings I/O uses ai_core to preserve bounded project history.
Do not overwrite from omarchy-plugin-i18n without preserving this integration.

Interface translations (built in; nothing is downloaded or generated).

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
    """Use the same bounded private-file reader as the settings writer."""
    global SETTINGS_FILE
    if SETTINGS_FILE is None:
        return "auto"
    path = Path(SETTINGS_FILE)
    import ai_core
    SETTINGS_FILE = path  # preserve an explicit path across ai_core's first lazy import
    data = ai_core.load_settings(path)
    value = data.get("language")
    return value if isinstance(value, str) and value in CODES else "auto"


def save_language(code):
    """Preserve large project histories through the shared atomic settings policy."""
    global SETTINGS_FILE
    if SETTINGS_FILE is None or not isinstance(code, str) or code not in CODES | {"auto"}:
        return
    path = Path(SETTINGS_FILE)
    import ai_core
    SETTINGS_FILE = path
    ai_core.update_settings(path, language=None if code == "auto" else code)
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


def _(text):
    return STRINGS.get(current(), {}).get(text, text)


def N_(text):
    """Mark a module-level constant for translation without translating it yet:
    the language is only known once settings are read. Translate it with _() where it is used."""
    return text


PLURAL_PREFIX = "\x04"   # plural tables live under their own keys, apart from plain msgids


def ngettext(one, other, n):
    """Plural: tables store a list of forms under PLURAL_PREFIX + the English
    `one` text ([one, other], or [one, few, many] for Polish)."""
    lang = current()
    return pick_plural(lang, STRINGS.get(lang, {}).get(PLURAL_PREFIX + one), n, one, other)


def pick_plural(lang: str, forms, n: int, one: str, other: str) -> str:
    """The form for n from a table's list ([one, other], or [one, few, many] for Polish)."""
    if not isinstance(forms, list) or not forms or not all(isinstance(f, str) for f in forms):
        return one if n == 1 else other
    if lang == "pl" and len(forms) == 3:
        m10, m100 = n % 10, n % 100
        return forms[0] if n == 1 else forms[1] if 2 <= m10 <= 4 and not 12 <= m100 <= 14 else forms[2]
    if lang in ("fr", "pt"):
        return forms[0] if n <= 1 else forms[-1]
    return forms[0] if n == 1 else forms[-1]


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
