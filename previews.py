"""Animated sketches, drawn in the theme's colours (as App Manager's settings).

- WidenSketch (Settings): a screen with a terminal and this window tiled narrow,
  a click on "Maximize to see the map", then what the chosen option does.
- SetupSketch (Help): how the AI setup is made by drawing on the map, with dummy
  boxes: agent → provider, a click on a model, agent → MCP server, agent → skills;
  then the agent's pin (its lines stay lit while the pointer moves on) and its
  right-click menu, whose Start here opens it in a terminal beside the map.
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
    cr.new_path()               # not joined to where the last text was drawn
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


def text(cr, x, y, label, size=8, color=(1, 1, 1, 1), bold=False, width=None, center=False, right=False):
    layout = PangoCairo.create_layout(cr)
    layout.set_font_description(Pango.FontDescription.from_string(f"Sans {'Bold ' if bold else ''}{size}"))
    if width:
        layout.set_width(int(width * Pango.SCALE))
        layout.set_ellipsize(Pango.EllipsizeMode.END)
        if center or right:
            layout.set_alignment(Pango.Alignment.RIGHT if right else Pango.Alignment.CENTER)
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
    (11.8, 12.6, "move", "skills", "pin", 5),
    (12.6, 13.0, "pin", None, "pin", 5),
    (13.0, 14.2, "move", "pin", "online", 5),
    (14.2, 15.0, "pause", None, None, 5),
    (15.0, 15.8, "move", "online", "agent", 6),
    (15.8, 16.2, "menu", None, "agent", 6),
    (16.2, 17.0, "move", "agent", "start", 6),
    (17.0, 17.3, "choose", None, "start", 6),
    (17.3, 17.9, "terminal", None, None, 6),
    (17.9, 19.4, "pause", None, None, 6),
    (19.4, 21.8, "pause", None, None, 7),
)
# Where the pointer goes: the boxes, the agent's pin and the menu's Start here.
POINTS = set(BOXES) | {"pin", "start"}
MENU = ("m_start", "m_terminal", "m_details", "m_hide")   # the agent's right-click menu
MENU_ROW = 14
DURATION = STEPS[-1][1]


def setup_state(t: float) -> dict:
    """At time t: the finished lines, a line being drawn (from, progress), the pointer's
    box and progress, a click, and the caption number (0: not started)."""
    done = []
    state = {"done": done, "drawing": None, "pointer": ("agent", "agent", 0.0), "click": None, "caption": 0,
             "pinned": False, "hover": None, "menu": False, "terminal": 0.0}
    if t <= 0:
        return state
    for start, end, what, src, dst, caption in STEPS:
        if t < start:
            break
        progress = min(1.0, (t - start) / (end - start))
        state["caption"] = caption
        if what in ("move", "drag"):
            state["pointer"] = (src or dst, dst, ease(progress))
            state["hover"] = dst if progress >= 1 and dst in BOXES else None
        if what == "pin":
            state["pointer"] = (dst, dst, 1.0)
            if progress < 1:
                state["click"] = dst
            state["pinned"] = progress >= 0.5
        if what == "menu":
            state["pointer"] = (dst, dst, 1.0)
            if progress < 0.6:
                state["click"] = dst
            state["menu"] = True
        if what == "choose":
            state["pointer"] = (dst, dst, 1.0)
            state["click"] = dst if progress < 1 else None
            state["menu"] = progress < 1
        if what == "terminal":
            state["terminal"] = ease(progress)
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
            if s["pinned"] and key == s["hover"] and not active:
                # Hovered while the agent is pinned: only its own frame, the lit lines stay.
                cr.set_source_rgba(*rgba(c.get("foreground"), 0.03))
                cr.fill_preserve()
                cr.set_source_rgba(*rgba(c.get("accent"), 0.5))
            else:
                cr.set_source_rgba(*rgba(c.get("accent") if active else c.get("foreground"),
                                         0.9 if active else 0.12 if s["pinned"] else 0.25))
            cr.set_line_width(2 if key == "model_b" and ("local", "model_b") in s["done"] else 1)
            cr.stroke()
            if key in ("local",):
                cr.arc(x + 9, y + h / 2, 3.5, 0, 2 * math.pi)
                cr.set_source_rgba(*rgba(c.get("green"), 1))
                cr.fill()
            text(cr, x + (18 if key == "local" else 9), y + 8, self.labels[BOXES[key][2]], 8,
                 rgba(c.get("foreground"), 0.4 if s["pinned"] and key not in lit and key != "agent" else 1),
                 bold=key in ("agent", "online", "local"), width=w - 20)
            if key == "agent" and (s["pinned"] or s["pointer"][1] == "pin"):
                self.pin(cr, c, x + w - 10, y + 8, s["pinned"])
        self.terminal(cr, c, s["terminal"], width)
        if s["menu"]:
            self.menu(cr, c, width, height)
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
        number = f"{s['caption']}  " if 0 < s["caption"] < 7 else ""  # 7: the closing sentence
        text(cr, 28, height - 36, number + caption, 9, rgba(c.get("foreground"), 1), width=width - 56)

    def menu_origin(self, width, height):
        """The menu's top left: where the right-click was, as on the map."""
        x, y, w, h = self.box_rect("agent", width, height)
        return x + w * 0.55, y + h * 0.6

    def menu(self, cr, c, width, height):
        x, y = self.menu_origin(width, height)
        w, h = 112, len(MENU) * MENU_ROW + 8
        rounded(cr, x, y, w, h, 5)
        cr.set_source_rgba(*rgba(c.get("background"), 1))
        cr.fill_preserve()
        cr.set_source_rgba(*rgba(c.get("foreground"), 0.3))
        cr.set_line_width(1)
        cr.stroke()
        for row, key in enumerate(MENU):
            text(cr, x + 9, y + 4 + row * MENU_ROW, self.labels[key], 7,
                 rgba(c.get("accent") if row == 0 else c.get("foreground"), 1), bold=row == 0, width=w - 14)

    def anchor(self, key, width, height):
        if key == "start":
            x, y = self.menu_origin(width, height)
            return x + 30, y + 4 + MENU_ROW / 2
        if key == "pin":
            x, y, w, _h = self.box_rect("agent", width, height)
            return x + w - 10, y + 9
        x, y, w, h = self.box_rect(key, width, height)
        return x + w * 0.55, y + h * 0.6

    @staticmethod
    def pin(cr, c, x, y, pinned):
        """A small pin, head at (x, y): filled when pinned, an outline before."""
        cr.set_source_rgba(*rgba(c.get("accent") if pinned else c.get("foreground"), 1 if pinned else 0.6))
        cr.set_line_width(1.4)
        cr.move_to(x - 1, y + 2)
        cr.line_to(x - 5, y + 8)            # the needle
        cr.stroke()
        cr.arc(x, y, 3.2, 0, 2 * math.pi)
        if pinned:
            cr.fill()
        else:
            cr.stroke()

    def terminal(self, cr, c, shown, width):
        """The agent's terminal sliding in over the right of the map (after the double-click)."""
        if shown <= 0:
            return
        left = 20 + 2 * (width - 40) / 4 - 6
        x, y, w, h = lerp(width, left, shown), 24, width - 16 - left, 112
        rounded(cr, x, y, w, h, 5)
        cr.set_source_rgba(*rgba(c.get("background"), 1))
        cr.fill_preserve()
        cr.set_source_rgba(*rgba(c.get("foreground"), 0.08))
        cr.fill_preserve()
        cr.set_source_rgba(*rgba(c.get("accent"), 0.9))
        cr.set_line_width(1.2)
        cr.stroke()
        cr.arc(x + 12, y + 12, 3.5, 0, 2 * math.pi)
        cr.set_source_rgba(*rgba(c.get("green"), 1))
        cr.fill()
        text(cr, x + 22, y + 5, self.labels["agent"], 8, rgba(c.get("foreground"), 1), bold=True, width=w - 30)
        cr.move_to(x + 1, y + 24)
        cr.line_to(x + w - 1, y + 24)
        cr.set_source_rgba(*rgba(c.get("foreground"), 0.15))
        cr.set_line_width(1)
        cr.stroke()
        _tw, _th = text(cr, x + 10, y + 32, "›", 9, rgba(c.get("accent"), 1), bold=True)
        if int(time.monotonic() * 2) % 2 == 0 or not self.playing:
            cr.rectangle(x + 22, y + 34, 6, 11)       # the blinking cursor
            cr.set_source_rgba(*rgba(c.get("foreground"), 0.8))
            cr.fill()

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


# --- Help: LLM Monitor, the bar plugin (drawn in the theme's colours, not a picture) ------------

MONITOR_LOOP = 9.0          # seconds: the context fills while generating, then the agent compacts


def monitor_frame(t: float) -> dict:
    """LLM Monitor at loop time t: context fill (0..1), tokens per second, generating or idle."""
    t %= MONITOR_LOOP
    if t < 6.0:                                   # generating: the context fills up
        fill, generating = lerp(0.38, 0.93, ease(t / 6.0)), True
    elif t < 6.6:                                 # the agent compacts: the context drops
        fill, generating = lerp(0.93, 0.31, ease((t - 6.0) / 0.6)), False
    else:                                         # idle, then it starts again
        fill, generating = lerp(0.31, 0.38, ease((t - 6.6) / 2.4)), t > 8.4
    speed = 27.1 + 1.6 * math.sin(t * 2.3) + 0.7 * math.sin(t * 5.1) if generating else 0.0
    return {"fill": fill, "generating": generating, "speed": speed}


def level_color(c: dict, fill: float) -> str:
    """LLM Monitor's colours for a filling buffer: green < 50 %, yellow, orange, red ≥ 90 %."""
    for limit, name in ((0.5, "green"), (0.75, "yellow"), (0.9, "orange")):
        if fill < limit:
            return c.get(name)
    return c.get("red")


class LlmMonitorSketch(Sketch):
    """The bar item (GPU memory and context fill, pulsing while generating) and its panel."""

    def __init__(self, colors, labels):
        super().__init__(560, 214, colors)
        self.labels = labels            # key -> translated label
        self.set_halign(Gtk.Align.CENTER)
        self.connect("map", lambda *_a: self.animate(self.motion_allowed()))
        self.connect("unmap", lambda *_a: self.animate(False))

    @staticmethod
    def motion_allowed() -> bool:
        settings = Gtk.Settings.get_default()
        return settings is None or bool(settings.get_property("gtk-enable-animations"))

    @staticmethod
    def bar(cr, c, x, y, w, h, parts):
        rounded(cr, x, y, w, h, h / 2)
        cr.set_source_rgba(*rgba(c.get("foreground"), 0.10))
        cr.fill()
        cr.save()
        rounded(cr, x, y, w, h, h / 2)
        cr.clip()
        for share, color, alpha in parts:
            cr.rectangle(x, y, w * share, h)
            cr.set_source_rgba(*rgba(color, alpha))
            cr.fill()
            x += w * share
        cr.restore()

    @staticmethod
    def dot(cr, c, x, y, color, square=False):
        cr.new_sub_path()
        if square:
            rounded(cr, x - 3.5, y - 3.5, 7, 7, 1.5)
        else:
            cr.arc(x, y, 3.2, 0, 2 * math.pi)
        if color:
            cr.set_source_rgba(*rgba(c.get(color), 1))
            cr.fill()
        else:
            cr.set_source_rgba(*rgba(c.get("foreground"), 0.6))
            cr.set_line_width(1)
            cr.stroke()

    def draw(self, _area, cr, width, height):
        c = self.colors()
        moving = self.tick is not None
        f = monitor_frame(time.monotonic() - self.started if moving else 2.5)
        fg = lambda a=1.0: rgba(c.get("foreground"), a)    # noqa: E731
        head = rgba(c.get("accent"), 0.9)
        level = level_color(c, f["fill"])
        pulse = 0.6 + 0.4 * math.sin(time.monotonic() * 5) if f["generating"] and moving else 1.0
        # Left: the bar item, as in Omarchy's bar.
        bw, bh = min(220, width * 0.38), 54
        bx, by = 16, (height - bh - 56) / 2
        rounded(cr, bx, by, bw, bh, 8)
        cr.set_source_rgba(*rgba(c.get("background"), 1))
        cr.fill_preserve()
        cr.set_source_rgba(*rgba(c.get("accent"), 0.9 * pulse))
        cr.set_line_width(2)
        cr.stroke()
        text(cr, bx + 16, by + 10, f"▦ 18.1G {round(f['fill'] * 100)}%", 15, rgba(c.get("accent"), 1), bold=True,
             width=bw - 24)
        self.bar(cr, c, bx + 16, by + bh - 12, bw - 32, 4, [(f["fill"], level, 1)])
        text(cr, bx, by + bh + 14, self.labels["gpu"] + " · " + self.labels["context"], 9, fg(0.8), width=bw)
        for i, name in enumerate(("green", "yellow", "orange", "red")):
            rounded(cr, bx + i * 18, by + bh + 38, 14, 8, 2)
            cr.set_source_rgba(*rgba(c.get(name), 1))
            cr.fill()
        text(cr, bx + 78, by + bh + 34, "< 50 · 75 · 90 %", 8, fg(0.6))
        # Right: the panel.
        px, py = bx + bw + 28, 8
        pw, ph = width - px - 16, height - 16
        rounded(cr, px, py, pw, ph, 6)
        cr.set_source_rgba(*rgba(c.get("background"), 1))
        cr.fill_preserve()
        cr.set_source_rgba(*rgba(c.get("accent"), 0.9))
        cr.set_line_width(1.2)
        cr.stroke()
        x, w = px + 14, pw - 28
        text(cr, x, py + 10, "LLM Monitor", 10, fg(), bold=True, width=w * 0.6)
        text(cr, x, py + 26, "llama.cpp · Qwen3.8-27B-Q4_K_M", 7, fg(0.6), width=w * 0.7)
        state = self.labels["generating"] if f["generating"] else self.labels["running"]
        text(cr, x + w - 110, py + 12, state, 8, rgba(c.get("green"), 1) if f["generating"] else fg(0.6),
             width=110, right=True)
        y = py + 48
        text(cr, x, y, self.labels["gpu"].upper(), 7, head, bold=True, width=w - 110)
        text(cr, x + w - 110, y, "19.2 / 24.0 GiB", 7, fg(0.7), width=110, right=True)
        self.bar(cr, c, x, y + 15, w, 6, [(0.75, c.get("accent"), 0.9), (0.05, c.get("muted"), 1)])
        y += 28
        for i, (label, color) in enumerate(((self.labels["llms"], "accent"), (self.labels["desktop"], "muted"),
                                            (self.labels["free"], None))):
            self.dot(cr, c, x + i * w / 3 + 3.5, y + 7, color, square=True)
            text(cr, x + i * w / 3 + 12, y, label, 7, fg(0.8), width=w / 3 - 16)
        y += 20
        text(cr, x, y, self.labels["context"].upper(), 7, head, bold=True, width=w - 140)
        text(cr, x + w - 140, y, f"{round(f['fill'] * 98)}k / 98k ({round(f['fill'] * 100)}%)", 7, fg(0.7),
             width=140, right=True)
        self.bar(cr, c, x, y + 15, w, 6, [(f["fill"], level, 1)])
        y += 28
        text(cr, x, y, self.labels["activity"].upper(), 7, head, bold=True, width=w - 90)
        speed = f"{f['speed']:.1f} tok/s" if f["generating"] else "0 tok/s"
        text(cr, x + w - 90, y, speed, 7, fg(0.7), width=90, right=True)
        load = 0.9 + 0.06 * math.sin(time.monotonic() * 3) if f["generating"] and moving else (0.93 if f["generating"] else 0.03)
        self.bar(cr, c, x, y + 15, w, 6, [(load, c.get("accent"), 0.6)])
        y += 30
        cx = x
        for i, (name, color, note, share) in enumerate((("llama.cpp", "green", self.labels["running"], 0.42),
                                                        ("Ollama", "yellow", "", 0.24),
                                                        ("vLLM", None, self.labels["stopped"], 0.34))):
            self.dot(cr, c, cx + 3.5, y + 7, color)
            label = f"{name} · {note}" if note else name
            text(cr, cx + 12, y, label, 7, fg(1 if i == 0 else 0.75), bold=i == 0, width=w * share - 14)
            cx += w * share
        y += 22
        rounded(cr, x, y, 112, 20, 4)
        cr.set_source_rgba(*fg(0.35))
        cr.set_line_width(1)
        cr.stroke()
        text(cr, x + 8, y + 4, self.labels["free_gpu"], 7, fg(0.9), width=100)
