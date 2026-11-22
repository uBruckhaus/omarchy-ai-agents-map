"""English-only compatibility API for the AI Agents Map interface.

Keep gettext-style callers working without reading locale, settings or translation
files. Install Maprisma for other languages and translated manuals.
"""

LANGUAGES = [("en", "English")]
CODES = {"en"}
STRINGS = {}
SETTINGS_FILE = None  # retained for callers that assign the former settings path
PLURAL_PREFIX = "\x04"


def system_language():
    return "en"


def language_setting():
    return "en"


def save_language(code):
    """Compatibility no-op: legacy language choices never change or write settings."""


def current():
    return "en"


def language_name(code):
    return dict(LANGUAGES).get(code, code)


def _(text):
    return text


def N_(text):
    """Keep existing marked English strings unchanged."""
    return text


def ngettext(one, other, n):
    return one if n == 1 else other


def pick_plural(lang: str, forms, n: int, one: str, other: str) -> str:
    """Compatibility for add-on callers; always use the English forms."""
    return ngettext(one, other, n)
