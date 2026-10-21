"""The window's sizes and the decisions that depend on them, without GTK (so they are tested):
when the window counts as narrow or really maximized, whether the
shows, and which of the map's categories (Show) a window gets on its own."""

# Smallest card (a small monitor); normally the card fills the screen.
# Default outer size as in the other popups: a 420 px card around a 590 px
# page, the same on every page so switching never resizes the popup.
CARD_WIDTH = 420
LIST_HEIGHT = 590
SCROLLER_MIN_HEIGHT = 120
SCREEN_MARGIN = 120    # space kept free above and below the card
WINDOW_SIZE = (1600, 960)   # first open; afterwards the size it was closed with
AGENT_SPACING = 8
SPLIT_WIDTH = 1300        # from this window width the whole map fits
MODEL_LIST_WIDTH = 460       # the models page's list (search results, installed models)
RIGHT_PAGES = ("connect", "install", "models", "mcp", "addmcp", "addskills", "status", "health", "terminal",
               "traffic")   # open in the map's place
DETAIL_LABEL_WIDTH = 90   # the dim labels of a compact detail grid
FULL_WIDTH_SHARE = 0.9    # of the monitor's width: a window this wide is really maximized

NARROW_WITH_AGENT = frozenset({"agents", "online", "local"})     # who works through what
NARROW_IDLE = frozenset({"online", "local", "models"})            # what is there to use


def narrow(width: int) -> bool:
    """Narrower than the whole map (a half-screen tile or less). 0: no width yet (before the window's
    first allocation), which is never narrow nor wide."""
    return 0 < width < SPLIT_WIDTH


def full_size(width: int) -> bool:
    return width >= SPLIT_WIDTH


def really_maximized(reported, width: int, monitor_width: int | None) -> bool:
    """The desktop says maximized (None: unknown) and the window has about the monitor's width:
    Hyprland reports every tiled window as maximized, a half-screen tile included."""
    if reported is not True:
        return False
    return monitor_width is None or width >= monitor_width * FULL_WIDTH_SHARE


def layer_situation(width: int, agent_running: bool):
    """What the map's categories are chosen for: ("narrow", running?), ("wide",), or None while the
    window has no width yet."""
    if width <= 0:
        return None
    return ("narrow", bool(agent_running)) if narrow(width) else ("wide",)


def layers_for(situation, wide_layers, saved) -> set:
    """The categories for a situation. Narrow: with an agent running, agents and providers; with
    none, providers and models. Wide: the full-size window's own (kept while it was narrow), else
    the saved choice with the agents shown."""
    if situation[0] == "narrow":
        return set(NARROW_WITH_AGENT if situation[1] else NARROW_IDLE)
    if wide_layers is not None:
        return set(wide_layers)
    return set(saved) | {"agents"}
