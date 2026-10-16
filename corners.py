"""The "Corners" setting, shared by App Manager and Theme Manager.

"windows" (the default) follows the windows' rounding, which the desktop
module reads (desktop.window_rounding(); who set it: desktop.rounding_source(),
which uses lua_rounding() on Omarchy). "rounded" and "square" ignore it. No
process is started here."""
import json
import os
import re

CHOICES = ("windows", "rounded", "square")
ROUNDED = 12      # "Rounded": Theme Manager's corners up to now.
MAX_RADIUS = 24
MAX_LUA_BYTES = 256 * 1024
# `rounding = 8` outside a comment; "gradient_rounding" and "rounding_power" don't count.
_ROUNDING = re.compile(r"(?<![\w.])rounding\s*=\s*(\d+)")


def choice(value):
    return value if value in CHOICES else "windows"


def parse_rounding(text):
    """The window rounding from Hyprland's `getoption decoration:rounding` JSON, or None."""
    try:
        value = json.loads(text).get("int")
    except (ValueError, TypeError, AttributeError):
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return max(0, min(value, MAX_RADIUS))


def radius(setting, window_rounding):
    """The corner radius in px for this setting and the windows' rounding."""
    setting = choice(setting)
    if setting == "rounded":
        return ROUNDED
    if setting == "square":
        return 0
    return window_rounding if window_rounding is not None else 0


def lua_rounding(path):
    """The last uncommented `rounding = N` in a Lua config file, or None."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        with os.fdopen(fd, "rb") as handle:
            data = handle.read(MAX_LUA_BYTES + 1)
    except OSError:
        return None
    if len(data) > MAX_LUA_BYTES:
        return None
    found = None
    for line in data.decode("utf-8", "replace").splitlines():
        code = line.split("--", 1)[0]
        for match in _ROUNDING.finditer(code):
            found = int(match.group(1))
    return found
