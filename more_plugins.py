"""Generated from ~/Projects/omarchy-plugin-i18n/py/more_plugins.py by
install_more.py; edit it there, not in a plugin.

"More plugins by the same author": a card for each of the author's other
marketplace plugins, with its glyph, name and a one-line explanation. A click
opens the plugin's page in the Omarchy Plugin Marketplace. The URLs are fixed
https addresses; nothing here is read from the network (a caller may pass a list it read
from the marketplace: build_cards(plugins=…)).
"""
from urllib.parse import quote

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gio, Gtk, Pango  # noqa: E402

from i18n import _, N_  # noqa: E402

URL = "https://plugins.omarchy.org/plugin.html?id={0}"

# id, name, glyph, one-line explanation. Names are product names, untranslated.
PLUGINS = [
    ("ubruckhaus.wlan-monitor", "WLAN Monitor", "\U000F043B", N_("Who is on your Wi-Fi, with a network map")),
    ("ubruckhaus.theme-manager", "Theme Manager", "\U000F0E0C", N_("Every Omarchy theme in one place, with wallpapers")),
    ("ubruckhaus.bluetooth-monitor", "Bluetooth Monitor", "\U000F00AF", N_("Battery levels of mice, keyboards and headsets")),
    ("ubruckhaus.llm-usage", "LLM Monitor", "\U000F061A", N_("Which local AI model holds your GPU memory")),
    ("ubruckhaus.ai-agents-map", "AI Agents Map", "\U000F06A9", N_("Your AI agents, models and MCP servers on one map")),
    ("ubruckhaus.simple-launcher-for-fools", "App Manager", "\U0000F00A", N_("Your apps in groups, with shortcuts and a dock")),
    ("ubruckhaus.simple-tile", "Workspace Presets", "\U000F056E", N_("Window limits and layouts per workspace")),
    ("ubruckhaus.hue-lights", "Hue Lights", "\U000F0335", N_("Philips Hue rooms and lights from the bar")),
    ("ubruckhaus.video-trans", "Video Trans", "\U0000F20A", N_("Live translated captions for any video or call")),
    ("ubruckhaus.simple-reader", "Screen Narrator", "\U0000F025", N_("Reads any window, PDF or Kindle page aloud")),
    ("ubruckhaus.simple-recipes", "Simple Recipes", "\U000F0B7C", N_("Your recipes: import, translate and cook")),
    ("ubruckhaus.my-plugins", "My Plugins", "\U000F0431", N_("For plugin authors: views, downloads, status")),
]

TITLE = N_("More plugins by the same author")
OPEN_TIP = N_("Open {0} in the Omarchy Plugin Marketplace")
INFORMATION = N_("Information")
OPEN_GLYPH = "\U000F03CC"
INFO_GLYPH = "\U000F02FC"


def url_for(plugin_id: str) -> str:
    return URL.format(quote(plugin_id, safe=""))


def open_page(plugin_id: str) -> None:
    Gio.AppInfo.launch_default_for_uri(url_for(plugin_id), None)


def build_cards(own_id: str, prefix: str, on_opened=None, leave_out=(), plugins=None) -> Gtk.Widget:
    """The heading and a two-column grid of cards, this plugin left out (and the ids in
    `leave_out`: a plugin shown on its own elsewhere on the page).
    `on_opened` runs after a page was opened (the popup can hide).
    `plugins`: (id, name, glyph, text) to show instead of PLUGINS (a list read from the
    marketplace elsewhere); a text in PLUGINS' English is translated, any other is shown as it is."""
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
    heading = Gtk.Label(label=_(TITLE).upper(), xalign=0)
    heading.add_css_class(f"{prefix}-more-heading")
    box.append(heading)
    grid = Gtk.Grid(column_spacing=8, row_spacing=8, column_homogeneous=True)
    shown = [p for p in (PLUGINS if plugins is None else plugins) if p[0] != own_id and p[0] not in leave_out]
    for index, (plugin_id, name, glyph, text) in enumerate(shown):
        card = Gtk.Button()
        card.add_css_class("flat")
        card.add_css_class(f"{prefix}-more-card")
        card.set_tooltip_text(_(OPEN_TIP).format(name))
        row = Gtk.Box(spacing=8)
        icon = Gtk.Label(label=glyph)
        icon.add_css_class(f"{prefix}-more-icon")
        icon.set_valign(Gtk.Align.CENTER)
        row.append(icon)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
        title = Gtk.Label(label=name, xalign=0)
        title.set_ellipsize(Pango.EllipsizeMode.END)
        title.set_max_width_chars(1)
        title.add_css_class(f"{prefix}-more-name")
        texts.append(title)
        line = Gtk.Label(label=_(text), xalign=0, wrap=True, max_width_chars=1)
        line.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        line.set_lines(3)
        line.set_ellipsize(Pango.EllipsizeMode.END)
        line.add_css_class(f"{prefix}-more-text")
        texts.append(line)
        texts.set_valign(Gtk.Align.CENTER)
        row.append(texts)
        mark = Gtk.Label(label=OPEN_GLYPH)
        mark.add_css_class(f"{prefix}-more-open")
        mark.set_valign(Gtk.Align.START)
        row.append(mark)
        card.set_child(row)

        def clicked(_button, plugin_id=plugin_id):
            open_page(plugin_id)
            if on_opened:
                on_opened()

        card.connect("clicked", clicked)
        grid.attach(card, index % 2, index // 2, 1, 1)
    box.append(grid)
    return box


def css(prefix: str, fg_hex: str, dim_hex: str, accent_hex: str, radius: int = 6) -> str:
    """Styles for build_cards(); `.{prefix}-info-btn` is the header's (i) button."""
    nerd = '"JetBrainsMono Nerd Font", "Symbols Nerd Font", monospace'
    return f"""
    button.{prefix}-more-card {{ padding: 8px; border-radius: {radius}px; border: 1px solid alpha({fg_hex}, 0.16);
        background-color: alpha({fg_hex}, 0.03); }}
    button.{prefix}-more-card:hover, button.{prefix}-more-card:focus-visible {{ border-color: {accent_hex};
        background-color: alpha({fg_hex}, 0.08); }}
    .{prefix}-more-heading {{ font-size: 11px; font-weight: 700; letter-spacing: 1px; color: {dim_hex}; }}
    .{prefix}-more-icon {{ font-family: {nerd}; font-size: 17px; color: {accent_hex}; min-width: 30px;
        min-height: 30px; border-radius: 5px; background-color: alpha({accent_hex}, 0.16); }}
    .{prefix}-more-name {{ font-weight: 700; font-size: 12px; color: {fg_hex}; }}
    .{prefix}-more-text {{ font-size: 11px; color: {dim_hex}; }}
    .{prefix}-more-open {{ font-family: {nerd}; font-size: 11px; color: {dim_hex}; }}
    button.{prefix}-more-card:hover .{prefix}-more-open {{ color: {accent_hex}; }}
    .{prefix}-info {{ font-family: {nerd}; font-size: 13px; }}
    """
