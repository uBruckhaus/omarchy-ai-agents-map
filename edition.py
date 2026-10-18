"""How much of a harness's sessions this edition may show.

    NUMBERS  counts, times, tokens, lines changed, cost (harnesses.py and its adapters)
    TITLES   the titles a harness gave its sessions itself (session_topics.py)
    SUMMARY  titles and numbers sent to the user's own Claude Code on a click (session_topics.py)

The level follows from the code that ships, not from a setting: session_topics.py is Asterism-only
(tools/vendor-into-plugin.sh leaves it out of the AI Agents Map plugin), so the plugin can show
numbers only, because the code that reads titles is not in it at all.
"""

NUMBERS, TITLES, SUMMARY = 1, 2, 3

_maximum = None


def maximum() -> int:
    global _maximum
    if _maximum is None:
        try:
            import session_topics  # noqa: F401
            _maximum = SUMMARY
        except ImportError:
            _maximum = NUMBERS
    return _maximum


def allows(level: int) -> bool:
    return level <= maximum()
