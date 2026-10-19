"""AI Agents Map: small drawing and text helpers shared by the window's modules."""

import datetime as dt
import re

from gi.repository import Gdk, Gtk

import icons
from glyphs import GLYPHS, MCP_GLYPHS, MCP_GROUP_GLYPHS, RUN_GLYPHS, glyph  # noqa: F401  (shared)
from i18n import _
from map_view import MapView
from gi.repository import Pango, PangoCairo  # noqa: E402  (map_view asked for PangoCairo's version)


def glyph_widget(text: str, font: str = "", size: int = 24, classes=()) -> Gtk.Widget:
    """A glyph as the app's own stored icon (icons.py; coloured by CSS as the text was), else the
    font's glyph as text. font: "" Nerd Font, "omarchy" Omarchy's font, "icon" an icon name."""
    name = icons.name_for(text, font)
    if name:
        widget = Gtk.Image.new_from_icon_name(name)
        widget.set_pixel_size(size)
        widget.add_css_class("am-icon")
    else:
        widget = Gtk.Label(label=text if font != "icon" else "")
        if font == "omarchy":
            widget.add_css_class("am-agent-icon-omarchy")
    for css in classes:
        widget.add_css_class(css)
    return widget


def glyph_label(name: str) -> Gtk.Widget:
    """A card's icon: a glyph in the accent colour."""
    widget = glyph_widget(glyph(name), "", 20, ("am-agent-icon", "am-pick-glyph"))
    widget.set_valign(Gtk.Align.CENTER)
    return widget


def pick_cards(listbox: Gtk.ListBox) -> Gtk.ListBox:
    """A list whose rows are cards (spaced, framed, the selected one in the accent colour)."""
    listbox.remove_css_class("boxed-list")
    listbox.add_css_class("am-pick-cards")
    return listbox


def size_text(n: int) -> str:
    """2490000000 -> "2.5 GB"."""
    for size, unit in ((1e12, "TB"), (1e9, "GB"), (1e6, "MB")):
        if n >= size:
            return f"{n / size:.1f} {unit}"
    return f"{max(0, n) // 1000} kB"


class SpaceBar(Gtk.DrawingArea):
    """One stacked bar: parts (label, bytes, colour name) side by side, as wide as their share.
    whole: the bar's full width in bytes (a disk's size); what the parts leave is the empty
    track, named rest (e.g. "Free"). Hovering a part tells its name and size."""

    HEIGHT = 14

    def __init__(self, color, parts, whole: int = 0, rest: str = ""):
        super().__init__()
        self.color = color          # (name, alpha) -> rgba, the map's palette
        self.parts = [p for p in parts if p[1] > 0]
        self.whole, self.rest = whole, rest
        self.spans = []
        self.set_content_height(self.HEIGHT)
        self.set_hexpand(True)
        self.set_has_tooltip(True)
        self.connect("query-tooltip", self.on_tooltip)
        self.set_draw_func(self.on_draw)

    def on_draw(self, _area, cr, width, height):
        used = sum(size for _l, size, _c in self.parts)
        total = max(self.whole, used) or 1
        MapView.rounded(cr, 0, 0, width, height, 4)
        cr.set_source_rgba(*self.color("fg", 0.08))
        cr.fill()
        x, self.spans = 0.0, []
        for label, size, name in self.parts:
            w = max(2.0, width * size / total)
            cr.save()
            MapView.rounded(cr, 0, 0, width, height, 4)
            cr.clip()
            cr.rectangle(x, 0, w - (1 if x + w < width - 1 else 0), height)
            cr.set_source_rgba(*self.color(name, 0.9))
            cr.fill()
            cr.restore()
            self.spans.append((x, x + w, label, size))
            x += w
        if self.rest and total > used:
            self.spans.append((x, float(width) + 1, self.rest, total - used))

    def on_tooltip(self, _widget, x, _y, _keyboard, tooltip):
        for start, end, label, size in self.spans:
            if start <= x < end:
                tooltip.set_text(f"{label}: {size_text(size)}")
                return True
        return False


# --- short texts (moved from ai_popup.py) ---------------------------------------------------

def short_number(n: int) -> str:
    """36835328 -> "36.8M"."""
    for size, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "k")):
        if n >= size:
            return f"{n / size:.1f}".rstrip("0").rstrip(".") + suffix
    return str(n)


def when(moment, future: bool = False) -> str:
    """A time in words: "today 14:18", "yesterday 09:02", "Mon 14:00", "2026-10-03"."""
    if moment is None:
        return ""
    if moment.tzinfo is not None:
        moment = moment.astimezone().replace(tzinfo=None)
    days = (moment.date() - dt.date.today()).days
    clock = moment.strftime("%H:%M")
    if days == 0:
        return _("today {0}").format(clock)
    if days == -1:
        return _("yesterday {0}").format(clock)
    if days == 1:
        return _("tomorrow {0}").format(clock)
    if future and 1 < days < 7:
        return moment.strftime("%a ") + clock
    return moment.strftime("%Y-%m-%d")



def short_gpu(name: str, vram: int = 0) -> str:
    """"Advanced Micro Devices, Inc. [AMD/ATI] Navi 31 [Radeon RX 7900 XT/7900 XTX/…] (rev c8)" + 24 GB
    -> "Radeon RX 7900 · 24 GB" (lspci names the family, not the exact card)."""
    bracket = re.findall(r"\[([^\]]+)\]", name or "")
    text = bracket[-1] if bracket else (name or "")
    if "/" in text:
        text = text.split("/")[0].strip().rsplit(" ", 1)[0]
    text = text.strip() or (name or "")
    return f"{text} · {round(vram / 2**30)} GB" if vram else text


def monitor_height() -> int:
    """Logical height of the smallest monitor, so the card always fits."""
    display = Gdk.Display.get_default()
    heights = []
    if display is not None:
        monitors = display.get_monitors()
        for i in range(monitors.get_n_items()):
            heights.append(monitors.get_item(i).get_geometry().height)
    return min(heights) if heights else 1080


class TokenChart(Gtk.DrawingArea):
    """Tokens per day as bars (one series, the accent colour; today full strength).
    Hovering a bar tells its day and number; the largest one is labelled."""

    HEIGHT = 120
    LABEL_H = 16

    def __init__(self, color, days):
        super().__init__()
        self.color = color          # (name, alpha) -> rgba, the map's palette
        self.days = list(days)
        self.bars = []
        self.set_content_height(self.HEIGHT)
        self.set_hexpand(True)
        self.set_has_tooltip(True)
        self.connect("query-tooltip", self.on_tooltip)
        self.set_draw_func(self.on_draw)

    def on_draw(self, _area, cr, width, height):
        layout = PangoCairo.create_layout(cr)
        layout.set_font_description(Pango.FontDescription.from_string("Sans 8"))
        top, bottom = self.LABEL_H, height - self.LABEL_H
        peak = max((t for _d, t in self.days), default=0) or 1
        count = max(1, len(self.days))
        slot = width / count
        bar_w = max(6.0, min(48.0, slot - 8))
        self.bars = []
        # Baseline (recessive).
        cr.set_source_rgba(*self.color("fg", 0.15))
        cr.rectangle(0, bottom, width, 1)
        cr.fill()
        for index, (day, tokens) in enumerate(self.days):
            x = index * slot + (slot - bar_w) / 2
            h = (bottom - top) * tokens / peak
            today = index == count - 1
            if tokens:
                MapView.rounded_top(cr, x, bottom - h, bar_w, h, 4)
                cr.set_source_rgba(*self.color("accent", 1.0 if today else 0.55))
                cr.fill()
            self.bars.append((x, bar_w, day, tokens))
            layout.set_text(_("today") if today else day.strftime("%a"), -1)
            text_w, _text_h = layout.get_pixel_size()
            cr.set_source_rgba(*self.color("dim"))
            cr.move_to(x + (bar_w - text_w) / 2, bottom + 3)
            PangoCairo.show_layout(cr, layout)
            if tokens == peak and tokens:
                layout.set_text(short_number(tokens), -1)
                text_w, text_h = layout.get_pixel_size()
                cr.set_source_rgba(*self.color("fg"))
                cr.move_to(x + (bar_w - text_w) / 2, bottom - h - text_h - 1)
                PangoCairo.show_layout(cr, layout)

    def on_tooltip(self, _widget, x, _y, _keyboard, tooltip):
        for left, bar_w, day, tokens in self.bars:
            if left - 4 <= x <= left + bar_w + 4:
                tooltip.set_text(f"{day.strftime('%a %d.%m.')} · " + _("{0} tokens").format(f"{tokens:,}".replace(",", " ")))
                return True
        return False


class Battery(Gtk.DrawingArea):
    """What is left of an online agent's limits, as on its box on the map (MapView.draw_battery):
    green, yellow (a quarter or less) or red (under a tenth); the tooltip lists every limit."""

    def __init__(self, colors, left: float, colour: str, tip: str):
        super().__init__()
        self.colors, self.left, self.colour = colors, left, colour
        self.set_content_width(24)
        self.set_content_height(12)
        self.set_valign(Gtk.Align.CENTER)
        self.set_tooltip_text(tip)
        self.set_draw_func(self.on_draw)

    def on_draw(self, _area, cr, width, height):
        bw, bh = 20, 10
        bx, by = 0, (height - bh) / 2
        cr.set_source_rgba(*self.colors("fg", 0.55))
        cr.set_line_width(1)
        cr.rectangle(bx + 0.5, by + 0.5, bw - 1, bh - 1)
        cr.stroke()
        cr.rectangle(bx + bw, by + 3, 2, bh - 6)
        cr.fill()
        cr.set_source_rgba(*self.colors(self.colour))
        cr.rectangle(bx + 2, by + 2, max(1.5, (bw - 4) * self.left), bh - 4)
        cr.fill()

