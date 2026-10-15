"""The Map: graph.Graph drawn as four columns of boxes joined by curves.

Hovering a box lights it and everything it is joined to; the rest fades.
A click selects a box (agent, provider, anything), Shift+click adds or
removes more. In a category with a selection (agents, say) only the
selected boxes stay; the other columns keep every box, but only the
connections of the selection are drawn and the rest fades. Holding Shift
brings the hidden boxes back (faded) so more can be added.
Colours come from the Omarchy theme (set_palette).
"""

import math

import gi

gi.require_version("PangoCairo", "1.0")
from gi.repository import Gdk, Gtk, Pango, PangoCairo  # noqa: E402

NODE_H = 38
NODE_GAP = 8
HEADER_H = 30
PAD = 12
COLUMN_GAP_MIN = 56
NODE_W_MAX = 260
NODE_W_MIN = 120
# Categories: selecting in one hides that category's other boxes.
CATEGORY = {"agent": "agent", "cloud": "provider", "runtime": "provider", "model": "model", "more": "model",
            "mcp": "tool", "skills": "tool"}
EDGE_COLORS = {"uses": "accent", "serves": "muted", "mcp": "yellow", "skills": "green"}


def rgba(hex_code: str, alpha: float = 1.0):
    color = Gdk.RGBA()
    if not color.parse(hex_code or "#888888"):
        color.parse("#888888")
    return color.red, color.green, color.blue, alpha


class MapView(Gtk.DrawingArea):
    def __init__(self):
        super().__init__()
        self.graph = None
        self.headers = ["", "", "", ""]
        self.colors = {}
        self.radius = 6
        self.boxes = {}          # node id -> (x, y, w, h)
        self.hover = None
        self.defaults = set()    # model ids some agent uses by default
        self.selected = []       # node ids picked by clicking, in click order
        self.on_selection = None # callback(list of selected nodes)
        self.shift = False       # Shift held: hidden boxes come back to be added
        self.set_draw_func(self.on_draw)
        self.set_hexpand(True)
        self.set_vexpand(True)
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self.on_motion)
        motion.connect("leave", lambda *_: self.set_hover(None))
        self.add_controller(motion)
        click = Gtk.GestureClick()
        click.connect("released", self.on_click)
        self.add_controller(click)

    def set_palette(self, colors: dict, radius: int):
        self.colors = dict(colors)
        self.radius = max(0, min(radius, 12))
        self.queue_draw()

    def set_graph(self, graph, headers):
        self.graph = graph
        self.headers = list(headers)
        self.defaults = {e.target for e in graph.edges if e.kind == "default"}
        self.hover = None
        ids = {n.id for n in graph.nodes}
        if any(i not in ids for i in self.selected):
            self.selected = [i for i in self.selected if i in ids]
            self.selection_changed()
        self.set_content_height(self.needed_height())
        self.queue_draw()

    def needed_height(self) -> int:
        if self.graph is None:
            return 0
        rows = max((len(self.shown(c)) for c in range(4)), default=0)
        return HEADER_H + PAD * 2 + rows * (NODE_H + NODE_GAP)

    # --- layout -----------------------------------------------------------------------

    def layout(self, width: int, height: int):
        self.boxes = {}
        if self.graph is None:
            return 0, 0
        node_w = max(NODE_W_MIN, min(NODE_W_MAX, (width - 2 * PAD - 3 * COLUMN_GAP_MIN) // 4))
        gap = max(COLUMN_GAP_MIN, (width - 2 * PAD - 4 * node_w) // 3)
        body = max(0, height - HEADER_H - PAD)
        for column in range(4):
            nodes = self.shown(column)
            total = len(nodes) * NODE_H + max(0, len(nodes) - 1) * NODE_GAP
            y = HEADER_H + max(0, (body - total) // 2)
            x = PAD + column * (node_w + gap)
            for node in nodes:
                self.boxes[node.id] = (x, y, node_w, NODE_H)
                y += NODE_H + NODE_GAP
        return node_w, gap

    def hidden(self) -> set:
        """Unselected boxes of every category that has a selection (none while Shift is held)."""
        if not self.selected or self.shift or self.graph is None:
            return set()
        by_id = {n.id: n for n in self.graph.nodes}
        picked = {CATEGORY.get(by_id[i].kind) for i in self.selected if i in by_id}
        return {n.id for n in self.graph.nodes
                if CATEGORY.get(n.kind) in picked and n.id not in self.selected}

    def shown(self, column: int) -> list:
        hidden = self.hidden()
        return [n for n in self.graph.column(column) if n.id not in hidden]

    def set_shift(self, held: bool):
        if held != self.shift:
            self.shift = held
            if self.selected:
                self.relayout()

    def relayout(self):
        self.set_content_height(self.needed_height())
        self.queue_draw()

    # --- hover ---------------------------------------------------------------------------

    def on_motion(self, controller, x, y):
        self.set_shift(bool(controller.get_current_event_state() & Gdk.ModifierType.SHIFT_MASK))
        found = self.node_at(x, y)
        self.set_cursor_from_name("pointer" if found else None)
        self.set_hover(found)

    def set_hover(self, node_id):
        if node_id == self.hover:
            return
        self.hover = node_id
        node = next((n for n in self.graph.nodes if n.id == node_id), None) if self.graph else None
        self.set_tooltip_text(" · ".join(x for x in (node.label, node.detail) if x) if node else None)
        self.queue_draw()

    def lit(self):
        """Nodes and edges joined to the hovered node."""
        if self.hover is None or self.graph is None:
            return None, None
        return self.related(self.hover)

    def related(self, start: str):
        """The node, what it is joined to, and for an agent also its providers' models."""
        nodes, edges = {start}, set()
        frontier = [start]
        for depth in range(2):
            following = []
            for node_id in frontier:
                for index, edge in enumerate(self.graph.edges):
                    if edge.kind == "default":
                        continue
                    if node_id in (edge.source, edge.target):
                        other = edge.target if edge.source == node_id else edge.source
                        # Second step only outwards from an agent: its provider's models.
                        if depth == 1 and not (edge.kind == "serves" and edge.source == node_id):
                            continue
                        edges.add(index)
                        if other not in nodes:
                            nodes.add(other)
                            following.append(other)
            frontier = following if start.startswith("agent:") else []
        return nodes, edges

    # --- selection ------------------------------------------------------------------------

    def highlighted(self):
        """(node ids, edge indexes) to light: the hovered box's, else the
        selected boxes' together; (None, None) when nothing is picked."""
        if self.hover is not None:
            return self.lit()
        if not self.selected or self.graph is None:
            return None, None
        nodes, edges = set(), set()
        for node_id in self.selected:
            more_nodes, more_edges = self.related(node_id)
            nodes |= more_nodes
            edges |= more_edges
        return nodes, edges

    def node_at(self, x, y):
        for node_id, (bx, by, bw, bh) in self.boxes.items():
            if bx <= x <= bx + bw and by <= y <= by + bh:
                return node_id
        return None

    def on_click(self, gesture, _count, x, y):
        """Click: select this box only (again: none). Shift+click: add or remove it."""
        node_id = self.node_at(x, y)
        if node_id is None:
            return
        adding = bool(gesture.get_current_event_state() & Gdk.ModifierType.SHIFT_MASK)
        if adding:
            if node_id in self.selected:
                self.selected.remove(node_id)
            else:
                self.selected.append(node_id)
        else:
            self.selected = [] if self.selected == [node_id] else [node_id]
        self.selection_changed()

    def clear_selection(self) -> bool:
        if not self.selected:
            return False
        self.selected = []
        self.selection_changed()
        return True

    def selection_changed(self):
        self.relayout()
        if self.on_selection is not None and self.graph is not None:
            by_id = {n.id: n for n in self.graph.nodes}
            self.on_selection([by_id[i] for i in self.selected if i in by_id])

    # --- drawing ------------------------------------------------------------------------------

    def color(self, name: str, alpha: float = 1.0):
        return rgba(self.colors.get(name, "#888888"), alpha)

    def on_draw(self, _area, cr, width, height):
        if self.graph is None:
            return
        node_w, _gap = self.layout(width, height)
        lit_nodes, lit_edges = self.highlighted()
        # Hover only fades the rest; a selection draws nothing but its own connections.
        only_lit = self.hover is None and lit_edges is not None
        layout = PangoCairo.create_layout(cr)

        # Column headings.
        for column, title in enumerate(self.headers):
            nodes = [n for n in self.graph.column(column) if n.id in self.boxes]
            x = PAD + column * (node_w + _gap)
            layout.set_font_description(Pango.FontDescription.from_string("Sans Bold 8"))
            layout.set_text(title.upper() if nodes else "", -1)
            layout.set_width(node_w * Pango.SCALE)
            layout.set_ellipsize(Pango.EllipsizeMode.END)
            cr.set_source_rgba(*self.color("dim"))
            cr.move_to(x, 6)
            PangoCairo.show_layout(cr, layout)

        # Edges first, under the boxes.
        for index, edge in enumerate(self.graph.edges):
            if edge.kind == "default" or edge.source not in self.boxes or edge.target not in self.boxes:
                continue
            sx, sy, sw, sh = self.boxes[edge.source]
            tx, ty, tw, th = self.boxes[edge.target]
            if sx < tx:
                x1, y1, x2, y2 = sx + sw, sy + sh / 2, tx, ty + th / 2
            else:
                x1, y1, x2, y2 = sx, sy + sh / 2, tx + tw, ty + th / 2
            bend = (x2 - x1) * 0.5
            alpha = 0.55
            if only_lit and index not in lit_edges:
                continue
            if lit_edges is not None:
                alpha = 0.95 if index in lit_edges else 0.08
            cr.set_source_rgba(*self.color(EDGE_COLORS.get(edge.kind, "muted"), alpha))
            cr.set_line_width(2.0 if lit_edges is not None and index in lit_edges else 1.4)
            cr.move_to(x1, y1)
            cr.curve_to(x1 + bend, y1, x2 - bend, y2, x2, y2)
            cr.stroke()

        for node in self.graph.nodes:
            if node.id not in self.boxes:
                continue
            x, y, w, h = self.boxes[node.id]
            faded = lit_nodes is not None and node.id not in lit_nodes
            alpha = 0.25 if faded else (0.55 if node.dim else 1.0)
            self.rounded(cr, x, y, w, h, self.radius)
            cr.set_source_rgba(*self.color("bg", 1.0))
            cr.fill_preserve()
            fill = 0.10 if node.kind == "agent" else 0.05
            cr.set_source_rgba(*self.color("fg", fill * (0.4 if faded else 1.0)))
            cr.fill_preserve()
            if node.id in self.selected:
                cr.set_source_rgba(*self.color("accent", 0.40))
                cr.fill_preserve()
            border = "accent" if node.id == self.hover or node.id in self.defaults or node.kind == "agent" \
                or node.id in self.selected else "fg"
            cr.set_source_rgba(*self.color(border, (0.9 if border == "accent" else 0.18) * (0.3 if faded else 1)))
            cr.set_line_width(2.5 if node.id in self.selected else 1)
            cr.stroke()
            if node.id in self.selected:
                # A check mark in the corner: selected, not just highlighted.
                layout.set_font_description(Pango.FontDescription.from_string("Sans Bold 10"))
                layout.set_width(-1)
                layout.set_text("✓", -1)
                cr.set_source_rgba(*self.color("accent"))
                text_w, text_h = layout.get_pixel_size()
                cr.move_to(x + w - text_w - 8, y + (h - text_h) / 2)
                PangoCairo.show_layout(cr, layout)

            text_x = x + 10
            if node.icon:
                family = "omarchy" if node.icon_font == "omarchy" else "JetBrainsMono Nerd Font"
                layout.set_font_description(Pango.FontDescription.from_string(f"{family} 13"))
                layout.set_width(-1)
                layout.set_text(node.icon, -1)
                cr.set_source_rgba(*self.color("accent", alpha))
                cr.move_to(text_x, y + (h - layout.get_pixel_size()[1]) / 2)
                PangoCairo.show_layout(cr, layout)
                text_x += 24
            layout.set_width(int((x + w - (26 if node.id in self.selected else 8) - text_x) * Pango.SCALE))
            layout.set_ellipsize(Pango.EllipsizeMode.END)
            if node.detail:
                layout.set_font_description(Pango.FontDescription.from_string("Sans Bold 9"))
                layout.set_text(node.label, -1)
                cr.set_source_rgba(*self.color("fg", alpha))
                cr.move_to(text_x, y + 4)
                PangoCairo.show_layout(cr, layout)
                layout.set_font_description(Pango.FontDescription.from_string("Sans 8"))
                layout.set_text(node.detail, -1)
                cr.set_source_rgba(*self.color("dim", alpha))
                cr.move_to(text_x, y + h / 2 + 1)
                PangoCairo.show_layout(cr, layout)
            else:
                weight = "Bold " if node.kind in ("agent", "cloud", "runtime") else ""
                layout.set_font_description(Pango.FontDescription.from_string(f"Sans {weight}9"))
                layout.set_text(node.label, -1)
                cr.set_source_rgba(*self.color("fg", alpha))
                cr.move_to(text_x, y + (h - layout.get_pixel_size()[1]) / 2)
                PangoCairo.show_layout(cr, layout)

    @staticmethod
    def rounded(cr, x, y, w, h, r):
        r = min(r, h / 2, w / 2)
        if r <= 0:
            cr.rectangle(x + 0.5, y + 0.5, w - 1, h - 1)
            return
        x, y, w, h = x + 0.5, y + 0.5, w - 1, h - 1
        cr.new_sub_path()
        cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
        cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
        cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
        cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
        cr.close_path()
