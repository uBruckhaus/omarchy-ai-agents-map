"""How much of a harness's sessions this edition may show.

    NUMBERS  counts, times, tokens, lines changed, cost (harnesses.py and its adapters)
    TITLES   the titles a harness gave its sessions itself (an add-on, e.g. Insights)
    SUMMARY  titles and numbers sent to the user's own Claude Code on a click (an add-on, e.g. Insights)

The level is the highest privacy level of the add-ons loaded now (addons.py, Asterism only), at
least NUMBERS. Without add-ons, and always in the AI Agents Map plugin (no add-on loader), it is
NUMBERS: the code that reads titles is not part of the app at all.
"""

NUMBERS, TITLES, SUMMARY = 1, 2, 3

def maximum() -> int:
    try:
        import addon_api
        loaded = addon_api.loaded_privacy()
    except ImportError:                 # the AI Agents Map plugin: no add-ons
        loaded = 0
    return max(NUMBERS, loaded)


def allows(level: int) -> bool:
    return level <= maximum()
