"""Animated sketches, drawn in the theme's colours (as App Manager's settings).

- WidenSketch (Settings): a screen with a terminal and this window tiled narrow,
  a click on "Maximize to see the map", then what the chosen option does.
- SetupSketch (Help): how the AI setup is made by drawing on the map, with dummy
  boxes: agent → provider, a click on a model, agent → MCP server, agent → skills.
  It plays once when started and stays on its last picture.

The timelines and geometry are plain functions, testable without a display.
"""

from __future__ import annotations

import math
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Gtk, Pango, PangoCairo  # noqa: E402

LOOP = 6.0                 # seconds per loop of the settings sketch
WIDEN_MODES = ("workspace", "maximize", "off")


def rgba(hex_code, alpha=1.0):
    hex_code = (hex_code or "#888888").lstrip("#")
    try:
        r, g, b = (int(hex_code[i:i + 2], 16) / 255 for i in (0, 2, 4))
    except ValueError:
        r = g = b = 0.5
    return r, g, b, alpha


def rounded(cr, x, y, w, h, r):
    r = max(0.0, min(r, w / 2, h / 2))
    if r <= 0:
        cr.rectangle(x, y, w, h)
        return
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


def ease(x):
    x = max(0.0, min(1.0, x))
    return x * x * (3 - 2 * x)


def lerp(a, b, t):
    return a + (b - a) * t


def pointer(cr, x, y, c, pressed=False):
    """A small arrow pointer with its tip at (x, y); a ring when it clicks."""
    if pressed:
        cr.arc(x, y, 9, 0, 2 * math.pi)
        cr.set_source_rgba(*rgba(c.get("accent"), 0.5))
        cr.set_line_width(2)
        cr.stroke()
    cr.move_to(x, y)
    cr.line_to(x, y + 14)
    cr.line_to(x + 4, y + 10.5)
    cr.line_to(x + 7, y + 16)
    cr.line_to(x + 9, y + 15)
    cr.line_to(x + 6, y + 9.5)
    cr.line_to(x + 11, y + 9.5)
    cr.close_path()
    cr.set_source_rgba(*rgba(c.get("foreground"), 1))
    cr.fill_preserve()
    cr.set_source_rgba(*rgba(c.get("background"), 1))
    cr.set_line_width(1)
    cr.stroke()


def text(cr, x, y, label, size=8, color=(1, 1, 1, 1), bold=False, width=None, center=False):
    layout = PangoCairo.create_layout(cr)
    layout.set_font_description(Pango.FontDescription.from_string(f"Sans {'Bold ' if bold else ''}{size}"))
    if width:
        layout.set_width(int(width * Pango.SCALE))
        layout.set_ellipsize(Pango.EllipsizeMode.END)
        if center:
            layout.set_alignment(Pango.Alignment.CENTER)
    layout.set_text(label, -1)
    cr.set_source_rgba(*color)
    cr.move_to(x, y)
    PangoCairo.show_layout(cr, layout)
    return layout.get_pixel_size()


class Sketch(Gtk.DrawingArea):
    def __init__(self, width, height, colors):
        super().__init__()
        self.colors = colors            # a callable: the theme palette
        self.set_content_width(width)
        self.set_content_height(height)
        self.set_size_request(width, height)
        self.set_draw_func(self.draw)
        self.started = time.monotonic()
        self.tick = None

    def animate(self, on: bool):
        if on and self.tick is None:
            self.tick = self.add_tick_callback(lambda *_a: (self.queue_draw(), True)[1])
        elif not on and self.tick is not None:
            self.remove_tick_callback(self.tick)
            self.tick = None

    def draw(self, _area, cr, width, height):
        raise NotImplementedError


# --- Settings: what the click on "Maximize to see the map" does ---------------------------------

def widen_frame(t: float, mode: str) -> dict:
    """The picture at loop time t: where the pointer is, whether it clicks, how far the
    window has grown (0..1) and how far the view has slid to the next workspace (0..1)."""
    frame = {"pointer": (0.80, 0.16), "click": False, "grow": 0.0, "slide": 0.0, "hint": mode != "off"}
    if mode == "off":
        frame["pointer"] = (0.7, 0.6)
        return frame
    if t < 1.2:
        frame["pointer"] = (lerp(0.55, 0.80, ease(t / 1.2)), lerp(0.7, 0.16, ease(t / 1.2)))
    frame["click"] = 1.3 <= t < 1.6
    change = ease((t - 1.7) / 1.0) if t < 5.2 else 1.0 - ease((t - 5.2) / 0.6)
    if mode == "workspace":
        frame["slide"] = change
    else:
        frame["grow"] = change
    frame["hint"] = change < 0.5
    return frame


class WidenSketch(Sketch):
    def __init__(self, colors, mode):
        super().__init__(300, 150, colors)
        self.mode = mode                # a callable: one of WIDEN_MODES
        self.animate(True)

    def draw(self, _area, cr, width, height):
        c = self.colors()
        t = (time.monotonic() - self.started) % LOOP
        f = widen_frame(t, self.mode())
        sh = min(height - 8, (width - 8) * 9 / 16)
        sw = sh * 16 / 9
        sx, sy = (width - sw) / 2, (height - sh) / 2
        rounded(cr, sx, sy, sw, sh, 4)
        cr.set_source_rgba(*rgba(c.get("background"), 1))
        cr.fill_preserve()
        cr.set_source_rgba(*rgba(c.get("foreground"), 0.25))
        cr.set_line_width(1)
        cr.stroke()
        cr.save()
        rounded(cr, sx, sy, sw, sh, 4)
        cr.clip()
        cr.rectangle(sx, sy, sw, 7)                       # the bar
        cr.set_source_rgba(*rgba(c.get("foreground"), 0.12))
        cr.fill()
        top, gap = sy + 10, 4
        area_w, area_h = sw - 2 * gap, sh - 10 - gap
        slide = f["slide"] * sw
        # Workspace 1: a terminal left, this window right (narrow: cards only).
        half = (area_w - gap) / 2
        app_x = sx + gap + lerp(half + gap, 0, f["grow"])
        app_w = lerp(half, area_w, f["grow"])
        if f["grow"] < 1:
            self.terminal(cr, c, sx + gap - slide, top, half, area_h)
        self.window(cr, c, app_x - slide, top, app_w, area_h, wide=f["grow"] > 0.6, hint=f["hint"])
        # Workspace 2 (slides in): this window alone, the whole width.
        if f["slide"] > 0:
            self.window(cr, c, sx + gap + sw - slide, top, area_w, area_h, wide=True, hint=False)
        cr.restore()
        px, py = f["pointer"]
        pointer(cr, sx + sw * px, sy + sh * py, c, f["click"])

    @staticmethod
    def terminal(cr, c, x, y, w, h):
        rounded(cr, x, y, w, h, 3)
        cr.set_source_rgba(*rgba(c.get("foreground"), 0.06))
        cr.fill()
        for row in range(6):
            cr.rectangle(x + 6, y + 8 + row * 9, w * (0.3 + (row * 17 % 40) / 100), 3)
            cr.set_source_rgba(*rgba(c.get("green") if row == 5 else c.get("foreground"), 0.35))
            cr.fill()

    @staticmethod
    def window(cr, c, x, y, w, h, wide, hint):
        rounded(cr, x, y, w, h, 3)
        cr.set_source_rgba(*rgba(c.get("background"), 1))
        cr.fill_preserve()
        cr.set_source_rgba(*rgba(c.get("accent"), 0.9))
        cr.set_line_width(1)
        cr.stroke()
        cr.rectangle(x + 6, y + 6, 26, 4)                 # title
        cr.set_source_rgba(*rgba(c.get("foreground"), 0.7))
        cr.fill()
        if hint:
            rounded(cr, x + w - 44, y + 4, 38, 8, 2)
            cr.set_source_rgba(*rgba(c.get("accent"), 0.8))
            cr.set_line_width(1)
            cr.stroke()
        cards_w = min(w - 12, 46) if wide else w - 12
        for row in range(6):                               # the cards
            rounded(cr, x + 6, y + 16 + row * 11, cards_w, 8, 2)
            cr.set_source_rgba(*rgba(c.get("foreground"), 0.10))
            cr.fill()
        if wide:                                            # the map beside them
            mx, mw = x + 58, w - 64
            columns = 4
            for col in range(columns):
                for row in range(3 if col != 1 else 4):
                    bx = mx + col * mw / columns
                    rounded(cr, bx, y + 18 + row * 13, mw / columns - 8, 8, 2)
                    cr.set_source_rgba(*rgba(c.get("accent"), 0.55))
                    cr.fill()
            cr.move_to(mx + mw / 4 - 8, y + 22)
            cr.curve_to(mx + mw / 4 + 4, y + 22, mx + mw / 2 - 12, y + 35, mx + mw / 2, y + 35)
            cr.set_source_rgba(*rgba(c.get("accent"), 0.9))
            cr.set_line_width(1)
            cr.stroke()


# --- Help: setting up by drawing -------------------------------------------------------------

# The dummy map: column, row, label key, kind.
BOXES = {
    "mcp": (0, 0, "mcp"), "skills": (0, 1, "skills"),
    "agent": (1, 0.5, "agent"),
    "online": (2, 0, "online"), "local": (2, 1, "local"),
    "model_a": (3, 0, "model_a"), "model_b": (3, 1, "model_b"),
}
# The story: (start, end, what, from, to, caption number).
STEPS = (
    (0.0, 0.8, "move", None, "agent", 1),
    (0.8, 2.6, "drag", "agent", "local", 1),
    (2.6, 3.4, "pause", None, None, 1),
    (3.4, 4.6, "move", "local", "model_b", 2),
    (4.6, 5.0, "click", None, "model_b", 2),
    (5.0, 6.0, "pause", None, None, 2),
    (6.0, 6.8, "move", "model_b", "agent", 3),
    (6.8, 8.6, "drag", "agent", "mcp", 3),
    (8.6, 9.4, "pause", None, None, 3),
    (9.4, 10.0, "move", "mcp", "agent", 4),
    (10.0, 11.8, "drag", "agent", "skills", 4),
    (11.8, 14.5, "pause", None, None, 5),
)
DURATION = STEPS[-1][1]


def setup_state(t: float) -> dict:
    """At time t: the finished lines, a line being drawn (from, progress), the pointer's
    box and progress, a click, and the caption number (0: not started)."""
    done = []
    state = {"done": done, "drawing": None, "pointer": ("agent", "agent", 0.0), "click": None, "caption": 0}
    if t <= 0:
        return state
    for start, end, what, src, dst, caption in STEPS:
        if t < start:
            break
        progress = min(1.0, (t - start) / (end - start))
        state["caption"] = caption
        if what in ("move", "drag"):
            state["pointer"] = (src or dst, dst, ease(progress))
        if what == "drag":
            if progress < 1:
                state["drawing"] = (src, dst, ease(progress))
            else:
                done.append((src, dst))
        if what == "click":
            state["pointer"] = (dst, dst, 1.0)
            if progress < 1:
                state["click"] = dst
            else:
                done.append(("local", "model_b"))
    return state


class SetupSketch(Sketch):
    """Plays once on start(); before that and after it shows a still picture."""

    def __init__(self, colors, labels, captions, on_end=None):
        super().__init__(560, 205, colors)
        self.labels = labels            # key -> translated box label
        self.captions = captions        # caption number -> translated sentence
        self.on_end = on_end
        self.playing = False
        self.t = 0.0
        self.set_hexpand(True)

    def start(self):
        self.started = time.monotonic()
        self.playing = True
        self.animate(True)

    def now(self) -> float:
        if not self.playing:
            return self.t
        t = time.monotonic() - self.started
        if t >= DURATION:
            self.playing = False
            self.t = DURATION
            self.animate(False)
            if self.on_end:
                self.on_end()
            return DURATION
        return t

    def box_rect(self, key, width, height):
        col, row, _label = BOXES[key]
        box_w, box_h = min(120, (width - 40) / 4 - 16), 30
        x = 20 + col * (width - 40) / 4 + ((width - 40) / 4 - box_w) / 2
        y = 30 + row * 64
        return x, y, box_w, box_h

    def draw(self, _area, cr, width, height):
        c = self.colors()
        t = self.now()
        s = setup_state(t)
        # Column headings.
        for col, key in enumerate(("h_mcp", "h_agents", "h_providers", "h_models")):
            text(cr, 20 + col * (width - 40) / 4, 6, self.labels[key].upper(), 7,
                 rgba(c.get("accent"), 0.9), bold=True, width=(width - 40) / 4 - 8)
        # Finished lines and the one being drawn.
        for src, dst in s["done"]:
            self.line(cr, c, src, dst, width, height, 1.0, solid=True)
        if s["drawing"]:
            src, dst, progress = s["drawing"]
            self.line(cr, c, src, dst, width, height, progress, solid=False)
        # Boxes.
        lit = {k for pair in s["done"] for k in pair}
        for key in BOXES:
            x, y, w, h = self.box_rect(key, width, height)
            rounded(cr, x, y, w, h, 5)
            cr.set_source_rgba(*rgba(c.get("background"), 1))
            cr.fill_preserve()
            active = key in lit or key == "agent"
            cr.set_source_rgba(*rgba(c.get("accent") if active else c.get("foreground"), 0.9 if active else 0.25))
            cr.set_line_width(2 if key == "model_b" and ("local", "model_b") in s["done"] else 1)
            cr.stroke()
            if key in ("local",):
                cr.arc(x + 9, y + h / 2, 3.5, 0, 2 * math.pi)
                cr.set_source_rgba(*rgba(c.get("green"), 1))
                cr.fill()
            text(cr, x + (18 if key == "local" else 9), y + 8, self.labels[BOXES[key][2]], 8,
                 rgba(c.get("foreground"), 1), bold=key in ("agent", "online", "local"), width=w - 20)
        # The pointer.
        frm, to, progress = s["pointer"]
        (x1, y1), (x2, y2) = self.anchor(frm, width, height), self.anchor(to, width, height)
        pointer(cr, lerp(x1, x2, progress), lerp(y1, y2, progress), c,
                pressed=s["click"] is not None or (s["drawing"] is not None))
        # The caption.
        caption = self.captions.get(s["caption"], "") if s["caption"] else self.captions.get(0, "")
        rounded(cr, 16, height - 44, width - 32, 34, 6)
        cr.set_source_rgba(*rgba(c.get("foreground"), 0.06))
        cr.fill()
        number = f"{s['caption']}  " if 0 < s["caption"] < 5 else ""
        text(cr, 28, height - 36, number + caption, 9, rgba(c.get("foreground"), 1), width=width - 56)

    def anchor(self, key, width, height):
        x, y, w, h = self.box_rect(key, width, height)
        return x + w * 0.55, y + h * 0.6

    def line(self, cr, c, src, dst, width, height, progress, solid):
        sx, sy, sw, sh = self.box_rect(src, width, height)
        dx, dy, dw, dh = self.box_rect(dst, width, height)
        left = BOXES[dst][0] < BOXES[src][0]
        x1, y1 = (sx if left else sx + sw), sy + sh / 2
        x2, y2 = (dx + dw if left else dx), dy + dh / 2
        x2, y2 = lerp(x1, x2, progress), lerp(y1, y2, progress)
        mid = (x1 + x2) / 2
        cr.move_to(x1, y1)
        cr.curve_to(mid, y1, mid, y2, x2, y2)
        cr.set_source_rgba(*rgba(c.get("accent"), 1 if solid else 0.7))
        cr.set_line_width(2.5 if (src, dst) == ("local", "model_b") else 1.8)
        if not solid:
            cr.set_dash([4, 3])
        cr.stroke()
        cr.set_dash([])
