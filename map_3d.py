"""Settings › Map › 3D map layout (Maprisma task 069b, flutter_app/lib/map_3d.dart) drawn with Cairo.

The map's cards stand as extruded plinths (a right and a bottom wall in two shades, a cast shadow,
a recessed tray when open, a top-left bevel), the summary cards and boxes as solid blocks whose
depth is tiered per column (Agents proudest), lifted while hovered, selected or running and pressed
flat while faded. Lines become floating cables (a drop shadow, a dark jacket, the vivid core and a
sheen), with a socket where they meet a box, and the lights running on them are shaded spheres.
A faint perspective floor lies behind it all.

Only looks change: every rectangle the map hands out for clicks, drags and tooltips stays where the
2D map has it (the walls grow to the right and below, into the gaps between boxes and cards).

Cheap to draw: the wall outlines of each (size, radius, depth) are worked out once and cached
(walls); the map itself is drawn into a texture only when something on it changes (MapView
.do_snapshot), and the lights' layer draws only its small spheres each frame.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from functools import lru_cache

BLACK = (0.0, 0.0, 0.0)
WHITE = (1.0, 1.0, 1.0)

CARD_DEPTH = (5.0, 6.8)          # an open card's plinth
CARD_DEPTH_FOLDED = (3.0, 4.2)   # a folded card: only its header
WELL_TOP = 30                    # an open card's tray starts under its header (MapView's CARD_HEAD + 4)
CABLE_SHADOW = (3.5, 7.5)        # where a cable's shadow falls on the floor below it
ARC_STEP = math.pi / 16          # the walls' rounded corners: a point every 11.25°


def enabled(layout) -> bool:
    """Settings › Map › 3D map layout, kept in flutter_map_layout's `three_d` as in Maprisma: on unless
    switched off (False)."""
    return not (isinstance(layout, dict) and layout.get("three_d") is False)


def column_depth(column: int) -> tuple[float, float]:
    """How far a column's blocks stand out: Agents (1) proudest in the middle, then Providers (2),
    then the outer MCP/skills (0) and Models (3) columns."""
    return {1: (5.2, 6.8), 2: (4.2, 5.6)}.get(column, (3.5, 4.7))


def block_depth(column: int, elevated: bool = False, dimmed: bool = False) -> tuple[float, float]:
    """A block's depth: lifted while hovered, selected or running, nearly flat while faded."""
    dx, dy = column_depth(column)
    if dimmed:
        return dx * 0.55, dy * 0.55
    if elevated:
        return dx + 1.5, dy + 2.0
    return dx, dy


def mix(a, b, t: float) -> tuple:
    """The colour t of the way from a to b (rgb or rgba; the result has a's length)."""
    return tuple(x + (y - x) * t for x, y in zip(a, b)) + tuple(a[len(b):])


def luminance(rgb) -> float:
    def channel(c):
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (channel(c) for c in rgb[:3])
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


@dataclass(frozen=True)
class Palette:
    """The theme's colours the 3D look needs (rgb, 0..1), and whether the theme is dark."""
    bg: tuple
    fg: tuple
    accent: tuple
    dark: bool

    @property
    def face(self) -> tuple:
        """A card's face: Maprisma's surfaceContainerLow."""
        return mix(self.bg, self.fg, 0.03)

    @property
    def outline(self) -> tuple:
        """Maprisma's outlineVariant: a quiet edge."""
        return mix(self.bg, self.fg, 0.22)


def palette(color) -> Palette:
    """From MapView.color(name, alpha) -> rgba."""
    bg = tuple(color("bg")[:3])
    return Palette(bg, tuple(color("fg")[:3]), tuple(color("accent")[:3]), luminance(bg) < 0.4)


# --- geometry -------------------------------------------------------------------------------------

def _arc(cx, cy, r, start, end) -> list:
    """Points on a circle from angle start to end (radians, y down: clockwise), both ends included."""
    steps = max(1, math.ceil(abs(end - start) / ARC_STEP))
    return [(cx + r * math.cos(start + (end - start) * i / steps), cy + r * math.sin(start + (end - start) * i / steps))
            for i in range(steps + 1)]


@lru_cache(maxsize=1024)
def walls(w: float, h: float, radius: float, dx: float, dy: float) -> tuple:
    """(right wall, bottom wall, crease start, crease end) of a w×h rounded rectangle at (0, 0) swept by
    (dx, dy): the two walls as point lists, split at the bottom-right corner (45°) so each takes its
    own shade, as Maprisma's extrudedMapWalls. Cached: the map has few distinct sizes."""
    r = max(0.0, min(radius, min(w, h) / 2))
    if r <= 0.05:
        right = ((w, 0), (w + dx, dy), (w + dx, h + dy), (w, h))
        bottom = ((w, h), (w + dx, h + dy), (dx, h + dy), (0, h))
        return right, bottom, (w, h), (w + dx, h + dy)
    angle = math.atan2(-dx, dy)              # where the sweep's tangent leaves the top-right corner (-90°..0°)
    bl = angle + math.pi                     # … and the bottom-left one
    split = math.pi / 4
    tr, br, blc = (w - r, r), (w - r, h - r), (r, h - r)

    def back(c):
        return c[0] + dx, c[1] + dy
    crease = (br[0] + r * math.cos(split), br[1] + r * math.sin(split))
    right = (_arc(*tr, r, angle, angle)                      # front top-right, at the tangent
             + _arc(*back(tr), r, angle, 0)                  # back top-right corner
             + _arc(*back(br), r, 0, split)                  # down the back right side to the crease
             + _arc(*br, r, split, 0)                        # front bottom-right, back up
             + _arc(*tr, r, 0, angle))                       # front right side up to the tangent
    bottom = (_arc(*br, r, split, split)
              + _arc(*back(br), r, split, math.pi / 2)
              + _arc(*back(blc), r, math.pi / 2, bl)
              + _arc(*blc, r, bl, math.pi / 2)
              + _arc(*br, r, math.pi / 2, split))
    return tuple(right), tuple(bottom), crease, (crease[0] + dx, crease[1] + dy)


def stage_lines(width: float, height: float, rows: int = 14) -> list:
    """The floor grid: (x1, y1, x2, y2, strength 0..1) of lines closer together towards the top and
    rays from a vanishing point above the map."""
    lines = []
    for i in range(1, rows + 1):
        t = i / rows
        y = height * t ** 1.38
        lines.append((0.0, y, width, y, 0.28 + 0.72 * t))
    vx, vy = width * 0.5, -height * 0.85
    step = max(110.0, width / 12)
    x = -width * 0.2
    while x <= width * 1.2:
        ddx, ddy = x - vx, height - vy
        t = -vy / ddy
        lines.append((vx + ddx * t, 0.0, x, height, 0.65))
        x += step
    return lines


# --- drawing --------------------------------------------------------------------------------------

def rounded(cr, x, y, w, h, r):
    r = max(0.0, min(r, w / 2, h / 2))
    cr.new_sub_path()
    if r <= 0:
        cr.rectangle(x, y, w, h)
        return
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


def _polygon(cr, x, y, points):
    cr.new_path()
    first = True
    for px, py in points:
        (cr.move_to if first else cr.line_to)(x + px, y + py)
        first = False
    cr.close_path()


def soft_shadow(cr, x, y, w, h, r, rgb, alpha: float, blur: float):
    """A blurred shadow without a blur filter: three nested rounded shapes, their edges feathered."""
    for i in range(3):
        grow = blur * (i - 1) / 2
        rounded(cr, x - grow, y - grow, w + 2 * grow, h + 2 * grow, r + max(0.0, grow))
        cr.set_source_rgba(*rgb, alpha / 3)
        cr.fill()


@lru_cache(maxsize=256)
def wall_shades(face, pal: Palette, tint=None) -> tuple:
    """(right wall, bottom wall, contour rgba) for a block of this face colour, tinted towards an
    accent while it is lit. Kept visible on near-black themes."""
    base = face if tint is None else mix(face, tint, 0.34 if pal.dark else 0.24)
    if pal.dark and luminance(base) < 0.045:
        base = mix(base, pal.outline, 0.32)
    shade = BLACK if pal.dark else pal.fg
    right = mix(base, shade, 0.34 if pal.dark else 0.14)
    bottom = mix(base, shade, 0.52 if pal.dark else 0.23)
    contour = mix(pal.outline, BLACK, 0.35 if pal.dark else 0.15) + (0.65 if pal.dark else 0.45,)
    return right, bottom, contour


def paint_walls(cr, x, y, w, h, r, depth, right, bottom, alpha: float = 1.0, contour=None):
    """The two walls (and their crease, or their outline) of a block at (x, y)."""
    rwall, bwall, crease0, crease1 = walls(round(w * 2) / 2, round(h * 2) / 2, round(r * 2) / 2,
                                           round(depth[0], 2), round(depth[1], 2))
    _polygon(cr, x, y, rwall)
    cr.set_source_rgba(*right, alpha)
    cr.fill()
    _polygon(cr, x, y, bwall)
    cr.set_source_rgba(*bottom, alpha)
    cr.fill()
    if contour is None:
        return
    cr.set_source_rgba(*contour[:3], contour[3] * alpha)
    cr.set_line_width(0.85)
    for points in (rwall, bwall):
        _polygon(cr, x, y, points)
        cr.stroke()


def _diagonal(x, y, w, h, stops):
    import cairo
    gradient = cairo.LinearGradient(x, y, x + w, y + h)
    for offset, rgba in stops:
        gradient.add_color_stop_rgba(offset, *rgba)
    return gradient


class Stage:
    """The floor drawn once per size and palette into an image of the device's pixels; each drawing
    of the map only pastes it (one operation instead of a gradient and some thirty lines)."""

    def __init__(self):
        self.key = None
        self.image = None

    def paint(self, cr, width: float, height: float, pal: Palette):
        import cairo
        matrix = cr.get_matrix()
        scale = matrix.xx
        pixels = (math.ceil(width * scale), math.ceil(height * scale))
        if pixels[0] <= 0 or pixels[1] <= 0 or pixels[0] * pixels[1] > 64_000_000 or matrix.xy or matrix.yx:
            paint_stage(cr, width, height, pal)        # no cache for a huge or turned drawing
            return
        key = (pixels, round(scale, 4), pal)
        if key != self.key:
            self.image = cairo.ImageSurface(cairo.FORMAT_ARGB32, *pixels)
            draw = cairo.Context(self.image)
            draw.scale(scale, scale)
            paint_stage(draw, width, height, pal)
            self.key = key
        cr.save()
        cr.identity_matrix()
        cr.translate(matrix.x0, matrix.y0)
        cr.set_source_surface(self.image, 0, 0)
        cr.paint()
        cr.restore()


def paint_stage(cr, width: float, height: float, pal: Palette):
    """The floor behind the map: a light from above and a grid that recedes upwards."""
    if width <= 0 or height <= 0:
        return
    import cairo
    light = cairo.LinearGradient(0, 0, 0, height)
    light.add_color_stop_rgba(0, *mix(pal.bg, pal.accent, 0.035 if pal.dark else 0.02), 0.55 if pal.dark else 0.4)
    light.add_color_stop_rgba(0.45, *pal.bg, 0)
    light.add_color_stop_rgba(1, *BLACK, 0.14 if pal.dark else 0.035)
    cr.rectangle(0, 0, width, height)
    cr.set_source(light)
    cr.fill()
    grid = 0.16 if pal.dark else 0.22
    cr.set_line_width(1)
    for x1, y1, x2, y2, strength in stage_lines(width, height):
        cr.set_source_rgba(*pal.outline, grid * strength)
        cr.move_to(x1, y1)
        cr.line_to(x2, y2)
        cr.stroke()


def paint_card(cr, x, y, w, h, r, pal: Palette, folded: bool, lifted: bool = False):
    """A card as a raised plinth: its shadow on the floor, its walls, its lit face, the tray its boxes
    sit in (open cards) and a bevel along its top-left rim. The header and boxes are drawn over it."""
    dx, dy = CARD_DEPTH_FOLDED if folded else CARD_DEPTH
    if lifted:
        dx, dy = dx + 1.5, dy + 2.0
    soft_shadow(cr, x + dx + 2.5, y + dy + 4.5, w, h, r, BLACK, 0.36 if pal.dark else 0.12, 5 if folded else 9)
    right, bottom, contour = wall_shades(pal.face, pal)
    paint_walls(cr, x, y, w, h, r, (dx, dy), right, bottom)
    _rwall, _bwall, (cx0, cy0), (cx1, cy1) = walls(round(w * 2) / 2, round(h * 2) / 2, round(r * 2) / 2,
                                                   round(dx, 2), round(dy, 2))
    cr.set_source_rgba(*contour)
    cr.set_line_width(0.9)
    cr.move_to(x + cx0, y + cy0)
    cr.line_to(x + cx1, y + cy1)
    cr.stroke()
    # The face: opaque (a card over another hides it), lit from the top left.
    top = mix(pal.face, WHITE, 0.045 if pal.dark else 0.45)
    rounded(cr, x, y, w, h, r)
    cr.set_source(_diagonal(x, y, w, h, ((0, (*top, 1.0)), (1, (*pal.face, 1.0)))))
    cr.fill()
    if not folded and h > 54:
        wx, wy, ww, wh = x + 6, y + WELL_TOP, w - 12, h - WELL_TOP - 7
        rounded(cr, wx, wy, ww, wh, max(0.0, r - 3))
        cr.set_source_rgba(*BLACK, 0.16 if pal.dark else 0.035)
        cr.fill_preserve()
        cr.set_source(_diagonal(wx, wy, ww, wh, ((0, (*BLACK, 0.32 if pal.dark else 0.09)),
                                                 (1, (*WHITE, 0.07 if pal.dark else 0.45)))))
        cr.set_line_width(1)
        cr.stroke()
    rounded(cr, x + 0.5, y + 0.5, w - 1, h - 1, max(0.0, r - 0.5))
    cr.set_source(_diagonal(x, y, w, h, ((0, (*WHITE, 0.16 if pal.dark else 0.65)),
                                         (1, (*pal.outline, 0.35 if pal.dark else 0.45)))))
    cr.set_line_width(1)
    cr.stroke()


def paint_block_back(cr, x, y, w, h, r, face, depth, pal: Palette, accent=None,
                     elevated: bool = False, dimmed: bool = False):
    """Under a box or summary card: its cast shadow (tinted by its accent while lifted) and its walls."""
    shadow_alpha = (0.16 if pal.dark else 0.05) if dimmed else (0.44 if pal.dark else 0.18) if elevated \
        else (0.30 if pal.dark else 0.11)
    shadow = mix(BLACK, accent, 0.28 if pal.dark else 0.15) if elevated and accent is not None else BLACK
    lift = (2.5, 4.5) if elevated else (1.5, 2.8)
    soft_shadow(cr, x + depth[0] + lift[0], y + depth[1] + lift[1], w, h, r, shadow, shadow_alpha,
                6.5 if elevated else 4.0)
    right, bottom, contour = wall_shades(face, pal, accent)
    paint_walls(cr, x, y, w, h, r, depth, right, bottom, 0.55 if dimmed else 1.0, contour)


def paint_block_front(cr, x, y, w, h, r, pal: Palette, elevated: bool = False, dimmed: bool = False):
    """Over a box or summary card: a soft sheen across its upper face and a crisp top-left bevel."""
    if dimmed or w <= 0 or h <= 0:
        return
    rounded(cr, x, y, w, h, r)
    cr.set_source(_diagonal(x, y, w, h, ((0, (*WHITE, (0.09 if pal.dark else 0.24) if elevated
                                              else (0.05 if pal.dark else 0.15))),
                                         (0.55, (*WHITE, 0.0)))))
    cr.fill()
    rounded(cr, x + 0.5, y + 0.5, w - 1, h - 1, max(0.0, r - 0.5))
    cr.set_source(_diagonal(x, y, w, h, ((0, (*WHITE, (0.30 if pal.dark else 0.78) if elevated
                                              else (0.18 if pal.dark else 0.58))),
                                         (0.6, (*WHITE, 0.0)))))
    cr.set_line_width(1)
    cr.stroke()


def paint_cable(cr, trace, rgba, width: float, pal: Palette, shadow: bool = True, dash=()):
    """A line as a floating cable: trace(cr) lays its path (called once per pass). Its shadow on the
    floor, a dark jacket, the vivid core, a sheen along its upper side. `dash` as cairo's. Leaves the
    line caps square (BUTT), as the map draws everything else."""
    import cairo
    rgb, alpha = tuple(rgba[:3]), rgba[3] if len(rgba) > 3 else 1.0
    if not dash:
        cr.set_line_cap(cairo.LINE_CAP_ROUND)      # a tube; a dashed or dotted line keeps the caller's caps
    cr.set_dash(list(dash))
    if shadow:
        cr.save()
        cr.translate(*CABLE_SHADOW)
        for grow, part in ((4.0, 0.35), (2.0, 0.65)):          # a soft edge without a blur filter
            trace(cr)
            cr.set_source_rgba(*BLACK, (0.34 if pal.dark else 0.14) * part)
            cr.set_line_width(width + grow)
            cr.stroke()
        cr.restore()
    jacket = mix(rgb, BLACK, 0.42 if pal.dark else 0.25)
    for colour, line in ((jacket, width + 1.6), (rgb, width + 0.4)):
        trace(cr)
        cr.set_source_rgba(*colour, alpha)
        cr.set_line_width(line)
        cr.stroke()
    sheen = min(1.0, alpha * (0.46 if pal.dark else 0.58))
    if sheen > 0.08:
        cr.save()
        cr.translate(-0.35, -0.55)
        trace(cr)
        cr.set_source_rgba(*mix(rgb, WHITE, 0.62), sheen)
        cr.set_line_width(max(0.75, width * 0.38))
        cr.stroke()
        cr.restore()
    cr.set_dash([])
    cr.set_line_cap(cairo.LINE_CAP_BUTT)


def paint_socket(cr, x, y, rgba, pal: Palette):
    """Where a cable meets a box: a bevelled port in the cable's colours."""
    rgb = tuple(rgba[:3])
    cr.new_path()
    cr.arc(x + 1.0, y + 1.5, 4.2, 0, 2 * math.pi)
    cr.set_source_rgba(*BLACK, 0.35 if pal.dark else 0.16)
    cr.fill()
    cr.arc(x, y, 4.0, 0, 2 * math.pi)
    cr.set_source_rgba(*mix(rgb, BLACK, 0.42 if pal.dark else 0.25), 1.0)
    cr.fill()
    cr.arc(x, y, 2.4, 0, 2 * math.pi)
    cr.set_source_rgba(*mix(rgb, WHITE, 0.28), 1.0)
    cr.fill()


def sphere_marks(x, y, accent, ends: float) -> list:
    """A light's head in 3D (for map_view.draw_marks): a soft halo, a shaded ball lit from the top left
    and a white catchlight. The catchlight is a "dot", so map_view.comets still ends the comet there."""
    return [("sphere", x, y, 5.8, tuple(accent[:3]), ends),
            ("dot", x - 1.1, y - 1.2, 1.1, (*WHITE, 0.85 * ends))]


def paint_sphere(cr, x, y, radius, accent, ends: float):
    import cairo
    halo = cairo.RadialGradient(x, y, 0, x, y, radius)
    halo.add_color_stop_rgba(0, *accent, 0.42 * ends)
    halo.add_color_stop_rgba(1, *accent, 0.0)
    cr.new_path()
    cr.arc(x, y, radius, 0, 2 * math.pi)
    cr.set_source(halo)
    cr.fill()
    ball = 3.4
    hx, hy = x - ball * 0.35, y - ball * 0.35
    shade = cairo.RadialGradient(hx, hy, 0, hx, hy, ball * 1.6)
    shade.add_color_stop_rgba(0, *mix(accent, WHITE, 0.55), ends)
    shade.add_color_stop_rgba(0.55, *accent, ends)
    shade.add_color_stop_rgba(1, *mix(accent, BLACK, 0.35), ends)
    cr.arc(x, y, ball, 0, 2 * math.pi)
    cr.set_source(shade)
    cr.fill()
