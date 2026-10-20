"""The Map: graph.Graph drawn as four columns of boxes joined by curves.

Hovering a box lights it and everything it is joined to; the rest fades.
A click selects a box (agent, provider, anything), Shift+click adds or
removes more; only one agent at a time (another agent replaces it). With
an agent selected, a click on a model of a local LLM the agent is
connected to calls on_pick_model(agent id, runtime id, model) to make it
the agent's model. In a category with a selection (agents, say) only the
selected boxes stay; the other columns keep every box, but only the
connections of the selection are drawn and the rest fades. Holding Shift
brings the hidden boxes back (faded) so more can be added.

Dragging from an agent to a local LLM, an MCP server or a skill set (or back) draws a
line; letting go over a fitting box calls on_link(agent id, target id) to
set it up. Clicking an agent's line to a local LLM or MCP server selects
it (a hook at the far end); dragging it to another LLM calls
on_relink(agent, old, new), onto empty space on_relink(agent, old, None).
A local LLM's line to a model that agents use (drawn in the accent colour) can be
selected the same way: dragging its end onto another model of that local LLM calls
on_remodel(runtime, old model, new model), onto empty space on_remodel(runtime, old, None).
Drawing from a local LLM to one of its models calls on_attach(runtime, model): agents are
given that model (the window asks which).
Selecting a box shows a hook (orange: the theme's signal colour, apart from the lines' own) on
every line of it that can be moved or removed, so what can be done is in view; a hook is grabbed
at once, without clicking its line first, and hovering it says what dragging it does.
Right-click on a box calls on_context(node id, box rectangle).
The pin at a box's top right corner (shown on hover, filled when pinned)
keeps that box's connections lit: while anything is pinned, hovering other
boxes changes nothing.
Dragging a model box onto another model folder (a group in the models column)
calls on_move(model id, folder, rectangle) when move_target_ok(model id, folder) agrees.
Zoom (set_zoom, Ctrl+scroll): the drawing is scaled; boxes, lines and
clicks stay in map units (x / zoom), rectangles handed out are in pixels.
Each agent's active path (agent → provider in use → its model) is drawn
bold in the accent colour: every agent's without a selection, else only
the selected or hovered agent's.
Colours come from the Omarchy theme (set_palette).
"""

import math

import cairo

import graph as graph_model
from glyphs import MCP_GLYPHS, glyph

import gi

gi.require_version("PangoCairo", "1.0")
from gi.repository import Gdk, GLib, Gtk, Pango, PangoCairo  # noqa: E402

import icons  # noqa: E402

NODE_H = 42
NODE_GAP = 8
GROUP_GAP = 22           # column 0: between the MCP servers and the skill sets
GROUP_LABEL_H = 16       # the small heading above each group
HEADER_H = 30
PAD = 12
ZOOM_STEPS = (0.5, 0.6, 0.7, 0.8, 0.9, 1.0, 1.1, 1.25, 1.5, 1.75, 2.0, 2.5)
COLUMN_GAP_MIN = 56
COLUMN_GAP_MAX = 300
NODE_W_MAX = 260
NODE_W_MIN = 120
PIN_W = 14               # the pin's square at a box's top right
HOOK_R = 8               # a hook's radius: the grip at a movable line's end
PIN, PIN_OUTLINE = "\U000F0403", "\U000F0931"   # Nerd Font pin, pin-outline
PLAY, CONSOLE = "\U000F040A", "\U000F018D"      # Nerd Font play, console: an agent's start button
STOP_VRAM = 256 * 1024 ** 2                        # a local LLM holding this much GPU memory gets a stop button
STOP = "\U000F04DB"                              # Nerd Font stop: beside the console while it runs here
ANIMATION_MODES = ("all", "agents", "off")    # Settings › Map animations
START_W = 18             # the start button's square, left of where the pin and the tick go
# The categories whose boxes each column holds (a column with none of them on is gone).
# Categories: selecting in one hides that category's other boxes.
CATEGORY = {"agent": "agent", "cloud": "provider", "runtime": "provider", "model": "model", "more": "model",
            "mcp": "tool", "skills": "tool"}
# The icon tile at a box's start: its glyph and the colour of the kind's lines.
TILE_GLYPHS = {"cloud": "cloud", "runtime": "chip", "model": "model", "more": "folder", "skills": "skill",
               "mcp": "app", "agent": "robot"}
TILE_TONES = {"mcp": "yellow", "skills": "green", "model": "muted", "more": "muted"}
# "available": set up but not in use (dotted), in the alert colour of the header's ! (the legend too).
# "loaded": a provider → model line while that model is loaded but no agent uses it (solid, light).
EDGE_COLORS = {"uses": "accent", "available": "red", "serves": "muted", "mcp": "yellow", "skills": "green",
               "loaded": "fg"}


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
        self.group_titles = {}   # node kind -> heading of its group in column 0 (mcp, skills)
        self.group_labels = []   # (x, y, text, column) drawn above the groups
        self.active = {}         # agent id -> edge indexes of its active path (provider in use, its model)
        self.selected = []       # node ids picked by clicking, in click order
        self.on_selection = None # callback(list of selected nodes)
        self.on_link = None      # callback(agent node id, runtime node id) after a drawn line
        self.link = None         # while drawing: {"from": id, "x": .., "y": .., "target": id or None}
        self.on_relink = None    # callback(agent id, old runtime id, new runtime id)
        self.on_remodel = None   # callback(runtime id, old model id, new model id or None): a model line moved
        self.on_attach = None    # callback(runtime id, model id): a line drawn from a local LLM to its model
        # What a line can do, on hover (translated by the window): an agent's line, a model line
        # ({0}: the agents that use the model).
        self.edge_tips = ("Drag its orange hook to another local LLM, or onto empty space to remove it",
                          "Used by {0}. Drag its orange hook onto another model (they switch to it) "
                          "or onto empty space (they are disconnected)",
                          "Used by {0}, in use right now",
                          "Loaded, no agent uses it. Drag its orange hook into empty space to unload "
                          "it, or onto another model to load that one instead",
                          "Used by {0}. Drag its orange hook onto another model: they switch to it",
                          "Drag its orange hook into empty space to sign out",
                          "Not working: click the line to see why, then repair or remove it")
        self.pick_tip = "Click: {0} uses this model"   # a model the selected agent can switch to
        self.on_context = None   # callback(node id, (x, y, w, h)) for a right-click
        self.on_pick_model = None  # callback(agent id, runtime id, model name): a model clicked
        self.selected_edge = None  # (agent id, runtime id) of the clicked line
        self.pinned = []         # node ids whose connections stay lit, whatever the pointer is over
        self.pin_rects = {}      # node id -> rectangle of its pin, for clicks
        self.pin_tips = ("Pin", "Unpin")   # tooltips (pin, unpin), translated by the window
        self.start_rects = {}    # agent box id -> rectangle of its start button, for clicks
        self.stop_rects = {}     # agent box id -> rectangle of its stop button (while it runs here)
        self.batteries = {}      # box id -> (left 0..1, colour, tooltip): an online agent's limits, a local LLM's GPU
        self.battery_rects = {}  # agent box id -> rectangle of its battery, for the tooltip
        self.start_state = None  # start_state(node id) -> "start", "show" (runs here) or "" (no button)
        self.on_start = None     # on_start(node id): start it, or show its terminal
        self.on_stop = None      # on_stop(node id): stop it (it runs here)
        self.start_tips = ("Start", "Show terminal")   # translated by the window
        self.group_rects = {}    # model folder -> rectangle of its group in the models column
        self.move_target_ok = None  # callback(model id, folder) -> bool: may the model go there
        self.on_move = None      # callback(model id, folder, (x, y, w, h)): a model dropped on a folder
        self.on_failed_edge = None # callback((agent id, target id), (x, y, w, h)): a line with hints clicked
        self.marker_points = []    # ((source, target), x, y) of the "!" on failed lines, for clicks
        self.shift = False       # Shift held: hidden boxes come back to be added
        self.layers = set(graph_model.LAYERS)   # categories switched on
        self.mode_off = set()    # categories the header's setup choice (All · Online · Local LLMs) leaves out
        self.mode_hidden = set() # boxes it leaves out (agents that cannot use local LLMs)
        self.column_x = {}       # column -> x of its boxes, for the headings
        self.zoom = 1.0          # drawing scale (ZOOM_STEPS)
        self.on_zoom = None      # callback(zoom) after Ctrl+scroll
        self.usage = None        # gpu_usage.Usage: GPU memory per local LLM, loaded models, activity
        self.animate = "all"     # Settings › Map animations: "all" (agents and other apps), "agents", "off"
        self.flow_layer = None   # FlowLayer over the map (with_flow_layer)
        self.working = None      # agent commands working now (agent_activity); None: every running one flows
        self.outside = {}        # runtime id -> {model id: "in use · Video Trans"}: an app outside the map generates
        self.set_draw_func(self.on_draw)
        self.scroll_adjustment = None
        self.connect("map", lambda *_: self.follow_scrolling())
        self.set_hexpand(True)
        self.set_vexpand(True)
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self.on_motion)
        motion.connect("leave", lambda *_: self.set_hover(None))
        self.add_controller(motion)
        click = Gtk.GestureClick()
        click.connect("released", self.on_click)
        self.add_controller(click)
        context = Gtk.GestureClick(button=Gdk.BUTTON_SECONDARY)
        context.connect("pressed", self.on_right_click)
        self.add_controller(context)
        drawer = Gtk.GestureDrag()
        drawer.connect("drag-begin", self.on_link_begin)
        drawer.connect("drag-update", self.on_link_update)
        drawer.connect("drag-end", self.on_link_end)
        self.add_controller(drawer)
        scroll = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.VERTICAL)
        scroll.connect("scroll", self.on_scroll)
        self.add_controller(scroll)

    def set_palette(self, colors: dict, radius: int):
        self.colors = dict(colors)
        self.radius = max(0, min(radius, 12))
        self.queue_draw()

    def set_batteries(self, batteries: dict):
        """{box id: (left, colour, tooltip)} from agent_details.battery or gpu_battery: drawn in the box."""
        if batteries != self.batteries:
            self.batteries = dict(batteries)
            self.queue_draw()

    def set_usage(self, usage):
        self.usage = usage
        self.queue_draw()

    def runtime_use(self, node):
        """gpu_usage.RuntimeUse of a local LLM box, or of the LLM serving a model box."""
        if self.usage is None or node.kind not in ("runtime", "model") or not node.id.count(":") >= 1:
            return None
        parts = node.id.split(":", 3)
        key = parts[1] if node.kind == "runtime" else (parts[2] if len(parts) > 3 and parts[1] == "runtime" else "")
        return self.usage.runtimes.get(key)

    def model_state(self, node, use=None) -> str | None:
        """A model box's state in its local LLM now: "loaded", "loading", "sleeping" or None."""
        use = use or self.runtime_use(node)
        if use is None:
            return None
        return use.models.get(node.label.lower()) or next(
            (value for name, value in use.models.items() if graph_model.model_key(name) == graph_model.model_key(node.label)),
            None)                    # listed by folder or file name, loaded under its id

    def loaded_edge(self, edge) -> bool:
        """A local LLM's line to a model it has loaded now (whether or not an agent uses it)."""
        if edge.kind != "serves" or edge.failed or not edge.source.startswith("runtime:"):
            return False
        node = next((n for n in self.graph.nodes if n.id == edge.target), None)
        return node is not None and node.kind == "model" and self.model_state(node) == "loaded"

    def set_graph(self, graph, headers):
        self.graph = graph
        self.headers = list(headers)
        self.defaults = {e.target for e in graph.edges if e.kind == "default"}
        self.active = self.active_paths(graph)
        if self.selected_edge and not any((e.source, e.target) == self.selected_edge for e in graph.edges):
            self.selected_edge = None
        self.hover = None
        ids = {n.id for n in graph.nodes}
        if any(i not in ids for i in self.selected):
            self.selected = [i for i in self.selected if i in ids]
            self.selection_changed()
        self.pinned = [i for i in self.pinned if i in ids]
        self.set_content_height(int(self.needed_height() * self.zoom))
        self.queue_draw()

    def groups(self, column: int) -> list:
        """The shown boxes of a column in groups: column 0 has MCP servers and
        skill sets, column 3 one group per model folder ("+ Model" box last)."""
        nodes = self.shown(column)
        if column == 0:
            return [g for g in ([n for n in nodes if n.kind == "mcp"], [n for n in nodes if n.kind != "mcp"]) if g]
        if column == 3:
            folders = {}
            for node in nodes:
                folders.setdefault(node.group, []).append(node)
            return list(folders.values())
        return [nodes] if nodes else []

    def titled(self, column: int) -> bool:
        """Groups get a heading and a gap: always in column 0, for models only with more than one folder."""
        return column == 0 or (column == 3 and len(self.groups(3)) > 1)

    def group_title(self, nodes) -> str:
        first = nodes[0]
        if first.column == 3:
            return first.group or self.group_titles.get("online", "").upper()
        return self.group_titles.get(first.kind, "")

    def column_height(self, column: int) -> int:
        groups = self.groups(column)
        rows = sum(len(g) for g in groups)
        height = rows * NODE_H + max(0, rows - 1) * NODE_GAP
        if self.titled(column) and groups:
            height += len(groups) * GROUP_LABEL_H + (len(groups) - 1) * GROUP_GAP
        return height

    def needed_height(self) -> int:
        if self.graph is None:
            return 0
        return HEADER_H + PAD * 2 + max((self.column_height(c) for c in range(4)), default=0)

    # --- layout -----------------------------------------------------------------------

    def layout(self, width: int, height: int):
        self.boxes = {}
        if self.graph is None:
            return 0, 0
        # Only columns with something to show take room; they share the width.
        active = [c for c in range(4) if self.shown(c)]
        count = max(1, len(active))
        node_w = max(NODE_W_MIN, min(NODE_W_MAX, (width - 2 * PAD - (count - 1) * COLUMN_GAP_MIN) // count))
        gap = max(COLUMN_GAP_MIN, (width - 2 * PAD - count * node_w) // max(1, count - 1))
        gap = min(gap, COLUMN_GAP_MAX)   # few columns: keep them together, centred
        if count == 1:
            gap = 0
        used = count * node_w + (count - 1) * gap
        left = PAD + max(0, (width - 2 * PAD - used) // 2)
        self.column_x = {}
        self.group_labels = []
        self.group_rects = {}
        for slot, column in enumerate(active):
            y = self.column_top(self.column_height(column), height, hub=column == 1)
            x = left + slot * (node_w + gap)
            self.column_x[column] = x
            titled = self.titled(column)
            for number, nodes in enumerate(self.groups(column)):
                if titled:
                    if number:
                        y += GROUP_GAP - NODE_GAP
                    self.group_labels.append((x, y, self.group_title(nodes), column))
                    y += GROUP_LABEL_H
                top = y - (GROUP_LABEL_H if titled else 0)
                for node in nodes:
                    self.boxes[node.id] = (x, y, node_w, NODE_H)
                    y += NODE_H + NODE_GAP
                if column == 3 and nodes[0].group:
                    self.group_rects[nodes[0].group] = (x, top, node_w, y - NODE_GAP - top)
        return node_w, gap

    def hidden(self) -> set:
        """Boxes of switched-off categories, and the unselected boxes of every
        category that has a selection (those come back while Shift is held)."""
        if self.graph is None:
            return set()
        off = graph_model.hidden_by_layers(self.graph, self.layers - self.mode_off,
                                           self.mode_hidden)
        if not self.selected or self.shift:
            return off
        return off | self.selection_hidden()

    def selection_hidden(self) -> set:
        by_id = {n.id: n for n in self.graph.nodes}
        picked = {CATEGORY.get(by_id[i].kind) for i in self.selected if i in by_id}
        return {n.id for n in self.graph.nodes
                if CATEGORY.get(n.kind) in picked and n.id not in self.selected}

    def set_mode(self, off_layers, hidden_ids):
        """The header's setup choice: categories and boxes left out, on top of the
        user's own Show choices (which stay as they are)."""
        self.mode_off, self.mode_hidden = set(off_layers), set(hidden_ids)
        self.set_layers(self.layers)

    def set_layers(self, layers):
        self.layers = set(layers)
        if self.graph is not None:
            off = graph_model.hidden_by_layers(self.graph, self.layers - self.mode_off,
                                               self.mode_hidden)
            if any(i in off for i in self.selected):
                self.selected = [i for i in self.selected if i not in off]
                self.selection_changed()
        self.relayout()

    def shown(self, column: int) -> list:
        hidden = self.hidden()
        return [n for n in self.graph.column(column) if n.id not in hidden]

    def set_shift(self, held: bool):
        if held != self.shift:
            self.shift = held
            if self.selected:
                self.relayout()

    def follow_scrolling(self):
        """Short columns stay in the middle of what is visible: draw again on scrolling."""
        scroller = self.get_ancestor(Gtk.ScrolledWindow)
        adjustment = scroller.get_vadjustment() if scroller is not None else None
        if adjustment is None or adjustment is self.scroll_adjustment:
            return
        self.scroll_adjustment = adjustment
        adjustment.connect("value-changed", lambda *_: self.queue_draw())
        adjustment.connect("notify::page-size", lambda *_: self.queue_draw())

    def visible_rows(self, height: float) -> tuple[float, float]:
        """(top, height) of the part of the map in view, in map units (zoom undone)."""
        adjustment = self.scroll_adjustment
        if adjustment is None or adjustment.get_page_size() <= 0:
            return 0.0, height
        return adjustment.get_value() / self.zoom, adjustment.get_page_size() / self.zoom

    def column_top(self, column_height: float, height: float, hub: bool = False) -> float:
        """A column that fits the view sits in its middle (below the headings while they
        show); a taller one starts under its heading and scrolls with the map. The agents
        (hub) are always centred on the view: taller than it, their middle follows the view's
        middle while scrolling, kept inside the map (so at the top they start under the heading)."""
        top, visible = self.visible_rows(height)
        area_top = max(HEADER_H, top + PAD)
        area_h = top + visible - PAD - area_top
        if column_height > area_h and not hub:
            return HEADER_H
        y = area_top + (area_h - column_height) / 2
        return max(HEADER_H, min(y, height - PAD - column_height))

    def viewport_width(self) -> int:
        """The width the scrolled view shows (not the map's own, which grows when zoomed in)."""
        parent = self.get_ancestor(Gtk.Viewport) or self.get_parent()
        return parent.get_width() if parent is not None and parent.get_width() > 0 else self.get_width()

    def relayout(self):
        self.set_content_height(int(self.needed_height() * self.zoom))
        # Zoomed in: wider than the view, so it scrolls sideways too.
        self.set_content_width(int(self.viewport_width() * self.zoom) if self.zoom > 1 else 0)
        self.queue_draw()

    def set_zoom(self, zoom: float):
        zoom = min(ZOOM_STEPS, key=lambda z: abs(z - zoom))
        if zoom != self.zoom:
            self.zoom = zoom
            self.relayout()

    def zoom_step(self, direction: int) -> float:
        """The next zoom step in or out (direction +1 / -1)."""
        index = ZOOM_STEPS.index(min(ZOOM_STEPS, key=lambda z: abs(z - self.zoom)))
        return ZOOM_STEPS[max(0, min(len(ZOOM_STEPS) - 1, index + direction))]

    def on_scroll(self, controller, _dx, dy):
        """Ctrl+scroll zooms; plain scrolling scrolls."""
        if not controller.get_current_event_state() & Gdk.ModifierType.CONTROL_MASK or dy == 0:
            return False
        self.set_zoom(self.zoom_step(-1 if dy > 0 else 1))
        if self.on_zoom is not None:
            self.on_zoom(self.zoom)
        return True

    def to_pixels(self, rect):
        return tuple(v * self.zoom for v in rect)

    # --- hover ---------------------------------------------------------------------------

    # --- lines ----------------------------------------------------------------------------------

    def edge_points(self, edge):
        """(x1, y1, x2, y2) of a line between two boxes, from the box sides that face each other."""
        sx, sy, sw, sh = self.boxes[edge.source]
        tx, ty, tw, th = self.boxes[edge.target]
        if sx < tx:
            return sx + sw, sy + sh / 2, tx, ty + th / 2
        return sx, sy + sh / 2, tx + tw, ty + th / 2

    def model_users(self, runtime_id: str, model_id: str) -> list:
        """The agents that use this model of this local LLM (their own setting: an agent → model
        line and a line to the local LLM)."""
        if self.graph is None:
            return []
        users = [e.source for e in self.graph.edges if e.kind == "default" and e.target == model_id]
        return [a for a in dict.fromkeys(users)
                if any(e.source == a and e.target == runtime_id and e.kind in ("uses", "available")
                       for e in self.graph.edges)]

    def model_busy(self, runtime_id: str, model_id: str) -> list:
        """Why this model may not be moved away now: a program outside the map uses it, or an agent
        that uses it runs. Only loaded (kept in memory, nobody working with it) is free. Empty: free."""
        if self.graph is None or not runtime_id.startswith("runtime:"):
            return []                  # an online model: the switch counts from the agent's next start
        nodes = {n.id: n for n in self.graph.nodes}
        why = []
        outside = self.outside.get(runtime_id, {}).get(model_id)
        if outside:
            why.append(outside)
        why += [nodes[a].label for a in self.model_users(runtime_id, model_id)
                if a in nodes and nodes[a].status == "running"]
        return why

    def movable_edge(self, edge) -> bool:
        """An agent's line to a local LLM (can be moved to another) or to an MCP server (can be
        removed), or a local LLM's line to a model that agents use (can be moved to another model)."""
        if edge.kind == "serves" and not edge.failed and self.kind_of(edge.source) in ("runtime", "cloud") \
                and self.kind_of(edge.target) == "model":
            # A model agents use, or one that is only loaded (unload it, or load another instead).
            return bool(self.model_users(edge.source, edge.target)) or self.loaded_edge(edge)
        if self.kind_of(edge.source) != "agent":
            return False
        if edge.kind == "failed":
            return self.kind_of(edge.target) in ("runtime", "mcp", "skills")
        return (edge.kind in ("uses", "available") and self.kind_of(edge.target) in ("runtime", "cloud")) \
            or (edge.kind in ("mcp", "skills") and self.kind_of(edge.target) in ("mcp", "skills"))

    def curve_point(self, edge, t: float) -> tuple[float, float]:
        """A point on the line's curve (t from 0 at the source to 1 at the target)."""
        x1, y1, x2, y2 = self.edge_points(edge)
        bend = (x2 - x1) * 0.5
        px = (1 - t) ** 3 * x1 + 3 * (1 - t) ** 2 * t * (x1 + bend) + 3 * (1 - t) * t ** 2 * (x2 - bend) + t ** 3 * x2
        py = (1 - t) ** 3 * y1 + 3 * (1 - t) ** 2 * t * y1 + 3 * (1 - t) * t ** 2 * y2 + t ** 3 * y2
        return px, py

    def draw_hooks(self, cr):
        """The hooks: a grip filled in the hook colour at each movable end (a dark dot in its middle),
        with a soft halo; the clicked line's is larger and ringed in the text colour."""
        moving = self.link is not None and (self.link.get("relink") or self.link.get("remodel")) and self.link["drawing"]
        for edge in self.hooked_edges():
            _x1, _y1, x2, y2 = self.edge_points(edge)
            picked = (edge.source, edge.target) == self.selected_edge
            alpha = 0.35 if moving and picked else 1.0
            radius = HOOK_R + (1.5 if picked else 0)
            cr.arc(x2, y2, radius + 4, 0, 2 * math.pi)              # a halo, so it stands out on any line
            cr.set_source_rgba(*self.color("hook", 0.22 * alpha))
            cr.fill()
            cr.arc(x2, y2, radius, 0, 2 * math.pi)
            cr.set_source_rgba(*self.color("hook", alpha))
            cr.fill_preserve()
            cr.set_source_rgba(*self.color("fg" if picked else "bg", alpha))
            cr.set_line_width(2 if picked else 1.5)
            cr.stroke()
            cr.arc(x2, y2, 2.2, 0, 2 * math.pi)                    # the grip's dot
            cr.set_source_rgba(*self.color("bg", 0.9 * alpha))
            cr.fill()

    def draw_markers(self, cr, edges):
        """A "!" on every failed line (click the line for its hints), apart from each other."""
        placed = []
        self.marker_points = []
        for edge in sorted(edges, key=lambda e: e.failed != "error"):     # errors first: they keep the middle
            for t in (0.5, 0.38, 0.62, 0.28, 0.72, 0.2, 0.8):
                mx, my = self.curve_point(edge, t)
                if all(math.hypot(mx - px, my - py) >= 18 for px, py in placed):
                    break
            placed.append((mx, my))
            self.marker_points.append(((edge.source, edge.target), mx, my))
            color = "red" if edge.failed == "error" else "yellow"
            cr.arc(mx, my, 7.5, 0, 2 * math.pi)
            cr.set_source_rgba(*self.color("bg"))
            cr.fill_preserve()
            cr.set_source_rgba(*self.color(color))
            cr.set_line_width(1.8)
            cr.stroke()
            cr.set_line_cap(cairo.LINE_CAP_ROUND)
            cr.move_to(mx, my - 3.8)
            cr.line_to(mx, my + 0.9)
            cr.stroke()
            cr.set_line_cap(cairo.LINE_CAP_BUTT)
            cr.arc(mx, my + 3.4, 1.0, 0, 2 * math.pi)
            cr.fill()

    def edge_distance(self, edge, x, y) -> float:
        """How far (x, y) is from the line, sampled along its curve."""
        x1, y1, x2, y2 = self.edge_points(edge)
        bend = (x2 - x1) * 0.5
        best = math.inf
        for step in range(25):
            t = step / 24
            # Cubic Bézier with control points (x1 + bend, y1) and (x2 - bend, y2).
            px = (1 - t) ** 3 * x1 + 3 * (1 - t) ** 2 * t * (x1 + bend) + 3 * (1 - t) * t ** 2 * (x2 - bend) + t ** 3 * x2
            py = (1 - t) ** 3 * y1 + 3 * (1 - t) ** 2 * t * y1 + 3 * (1 - t) * t ** 2 * y2 + t ** 3 * y2
            best = min(best, math.hypot(px - x, py - y))
        return best

    def edge_at(self, x, y, reach: float = 6):
        best, best_dist = None, reach
        for edge in self.graph.edges if self.graph else []:
            if not self.movable_edge(edge) or edge.source not in self.boxes or edge.target not in self.boxes:
                continue
            dist = self.edge_distance(edge, x, y)
            if dist < best_dist:
                best, best_dist = (edge.source, edge.target), dist
        return best

    def near_selected_edge(self, x, y, reach: float = 10) -> bool:
        """On the selected line (its handle included), even where other lines end at the same box."""
        edge = next((e for e in self.graph.edges if (e.source, e.target) == self.selected_edge), None) \
            if self.graph and self.selected_edge else None
        return edge is not None and edge.source in self.boxes and edge.target in self.boxes \
            and self.edge_distance(edge, x, y) <= reach

    def hooked_edges(self) -> list:
        """The lines that show a hook: the clicked one, and every movable line of a selected box."""
        if not self.graph:
            return []
        return [e for e in self.graph.edges if e.source in self.boxes and e.target in self.boxes
                and ((e.source, e.target) == self.selected_edge
                     or (self.movable_edge(e) and (e.source in self.selected or e.target in self.selected)))]

    def hook_at(self, x, y, reach: float = HOOK_R + 4):
        """The line whose hook is at (x, y), or None."""
        best, best_dist = None, reach
        for edge in self.hooked_edges():
            _x1, _y1, x2, y2 = self.edge_points(edge)
            dist = math.hypot(x2 - x, y2 - y)
            if dist <= best_dist:
                best, best_dist = (edge.source, edge.target), dist
        return best

    def on_right_click(self, _gesture, _count, x, y):
        x, y = x / self.zoom, y / self.zoom
        node_id = self.node_at(x, y)
        if node_id is not None and self.on_context is not None:
            self.on_context(node_id, self.to_pixels(self.boxes[node_id]))

    # --- drawing a connection ---------------------------------------------------------------

    def kind_of(self, node_id):
        node = next((n for n in self.graph.nodes if n.id == node_id), None) if self.graph else None
        return node.kind if node else None

    def link_target_ok(self, source: str, target: str) -> bool:
        """Agent ↔ installed local runtime or MCP server, in either direction; a model line: another
        model of the same local LLM."""
        if self.link and self.link.get("remodel"):
            return target != self.link["remodel"] and self.kind_of(target) == "model" and any(
                e.kind == "serves" and e.source == source and e.target == target for e in self.graph.edges)
        if self.link and self.link.get("relink"):
            # Moving a line: to another installed local LLM (an MCP line can only be removed).
            if self.kind_of(target) != "runtime" or target == self.link["relink"] \
                    or self.kind_of(self.link["relink"]) != "runtime":
                return False
            node = next(n for n in self.graph.nodes if n.id == target)
            return node.status != "missing"
        kinds = {self.kind_of(source), self.kind_of(target)}
        if kinds in ({"agent", "mcp"}, {"agent", "skills"}):
            return True
        if self.kind_of(source) == "runtime" and self.kind_of(target) == "model":
            # From a local LLM to one of its models: agents get that model.
            return any(e.kind == "serves" and e.source == source and e.target == target for e in self.graph.edges)
        if kinds == {"agent", "cloud"}:
            return True                 # an agent to an online provider: its sign-in
        if kinds != {"agent", "runtime"}:
            return False
        runtime = source if self.kind_of(source) == "runtime" else target
        node = next(n for n in self.graph.nodes if n.id == runtime)
        return node.status != "missing"

    def on_link_begin(self, gesture, x, y):
        x, y = x / self.zoom, y / self.zoom
        hook = self.hook_at(x, y)
        if hook is not None:
            self.selected_edge = hook          # a hook is grabbed at once: its line is the one moved
        if hook is not None or self.near_selected_edge(x, y):
            source, target = self.selected_edge
            if self.kind_of(source) == "runtime":       # a model line: its model end moves
                self.link = {"from": source, "remodel": target, "start": (x, y), "x": x, "y": y, "target": None,
                             "drawing": False}
                return
            self.link = {"from": source, "relink": target, "start": (x, y), "x": x, "y": y, "target": None,
                         "drawing": False}
            return
        node_id = self.node_at(x, y)
        if node_id is not None and self.kind_of(node_id) == "model" and self.move_target_ok is not None \
                and any(self.move_target_ok(node_id, f) for f in self.group_rects):
            self.link = {"from": node_id, "move": True, "start": (x, y), "x": x, "y": y, "target": None,
                         "drawing": False}
            return
        if node_id is None or self.kind_of(node_id) not in ("agent", "runtime", "mcp", "skills"):
            gesture.set_state(Gtk.EventSequenceState.DENIED)
            self.link = None
            return
        self.link = {"from": node_id, "start": (x, y), "x": x, "y": y, "target": None, "drawing": False}

    def on_link_update(self, gesture, dx, dy):
        if self.link is None:
            return
        dx, dy = dx / self.zoom, dy / self.zoom
        if not self.link["drawing"]:
            if abs(dx) + abs(dy) < 8:
                return
            self.link["drawing"] = True
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)   # a drag, not a click
            self.set_cursor_from_name("crosshair")
        x0, y0 = self.link["start"]
        self.link["x"], self.link["y"] = x0 + dx, y0 + dy
        if self.link.get("move"):
            self.link["target"] = self.folder_at(self.link["x"], self.link["y"], self.link["from"])
            self.queue_draw()
            return
        target = self.node_at(self.link["x"], self.link["y"])
        self.link["target"] = target if target and self.link_target_ok(self.link["from"], target) else None
        self.queue_draw()

    def folder_at(self, x, y, model_id):
        """The model folder whose group is at (x, y), when the model may go there."""
        for folder, (gx, gy, gw, gh) in self.group_rects.items():
            if gx - 6 <= x <= gx + gw + 6 and gy - 6 <= y <= gy + gh + 6:
                return folder if self.move_target_ok(model_id, folder) else None
        return None

    def on_link_end(self, _gesture, _dx, _dy):
        link, self.link = self.link, None
        self.set_cursor_from_name(None)
        self.queue_draw()
        if link and link.get("move"):
            if link["drawing"] and link["target"] and self.on_move is not None:
                self.on_move(link["from"], link["target"], self.to_pixels(self.group_rects[link["target"]]))
            return
        if link and link.get("remodel") and link["drawing"] and self.on_remodel is not None:
            # Let go on another model of this local LLM: its users move there. Empty space: they
            # are disconnected from the local LLM.
            if link["target"]:
                self.on_remodel(link["from"], link["remodel"], link["target"])
            elif self.node_at(link["x"], link["y"]) is None:
                self.on_remodel(link["from"], link["remodel"], None)
            self.selected_edge = None
            return
        if link and link.get("relink") and link["drawing"] and self.on_relink is not None:
            # Let go on another local LLM: move the line. Anywhere else: remove it.
            if link["target"]:
                self.on_relink(link["from"], link["relink"], link["target"])
            elif self.node_at(link["x"], link["y"]) is None:
                self.on_relink(link["from"], link["relink"], None)
            return
        if link and link["drawing"] and link["target"] and self.kind_of(link["from"]) == "runtime" \
                and self.kind_of(link["target"]) == "model":
            if self.on_attach is not None:
                self.on_attach(link["from"], link["target"])
            return
        if link and link["drawing"] and link["target"] and self.on_link is not None:
            a, b = link["from"], link["target"]
            agent, runtime = (a, b) if self.kind_of(a) == "agent" else (b, a)
            self.on_link(agent, runtime)

    def draw_link(self, cr):
        """The line being drawn, and the boxes it may end on."""
        link = self.link
        if not link or not link["drawing"] or link["from"] not in self.boxes:
            return
        if link.get("move"):
            self.draw_move(cr, link)
            return
        for node in self.graph.nodes:
            if node.id in self.boxes and self.link_target_ok(link["from"], node.id):
                x, y, w, h = self.boxes[node.id]
                self.rounded(cr, x - 2, y - 2, w + 4, h + 4, self.radius + 2)
                cr.set_source_rgba(*self.color("accent", 0.9 if node.id == link["target"] else 0.35))
                cr.set_line_width(2.5 if node.id == link["target"] else 1.2)
                cr.set_dash([] if node.id == link["target"] else [4, 3])
                cr.stroke()
                cr.set_dash([])
        sx, sy, sw, sh = self.boxes[link["from"]]
        x1 = sx + sw if link["x"] > sx + sw / 2 else sx
        y1 = sy + sh / 2
        x2, y2 = link["x"], link["y"]
        bend = (x2 - x1) * 0.5
        cr.set_source_rgba(*self.color("accent", 0.95))
        cr.set_line_width(2.2)
        cr.set_dash([6, 4])
        cr.move_to(x1, y1)
        cr.curve_to(x1 + bend, y1, x2 - bend, y2, x2, y2)
        cr.stroke()
        cr.set_dash([])
        cr.arc(x2, y2, 4, 0, 2 * math.pi)
        cr.fill()

    def draw_move(self, cr, link):
        """A model being moved: the folders it may go to framed, the box following the pointer."""
        for folder, (gx, gy, gw, gh) in self.group_rects.items():
            if not self.move_target_ok(link["from"], folder):
                continue
            target = folder == link["target"]
            self.rounded(cr, gx - 5, gy - 5, gw + 10, gh + 10, self.radius + 3)
            if target:
                cr.set_source_rgba(*self.color("accent", 0.10))
                cr.fill_preserve()
            cr.set_source_rgba(*self.color("accent", 0.9 if target else 0.4))
            cr.set_line_width(2.2 if target else 1.2)
            cr.set_dash([] if target else [4, 3])
            cr.stroke()
            cr.set_dash([])
        _sx, _sy, sw, sh = self.boxes[link["from"]]
        x, y = link["x"] - sw / 2, link["y"] - sh / 2
        self.rounded(cr, x, y, sw, sh, self.radius)
        cr.set_source_rgba(*self.color("bg", 0.9))
        cr.fill_preserve()
        cr.set_source_rgba(*self.color("accent", 0.9))
        cr.set_line_width(1.5)
        cr.set_dash([5, 3])
        cr.stroke()
        cr.set_dash([])
        node = next(n for n in self.graph.nodes if n.id == link["from"])
        layout = PangoCairo.create_layout(cr)
        layout.set_font_description(Pango.FontDescription.from_string("Sans 9"))
        layout.set_width(int((sw - 20) * Pango.SCALE))
        layout.set_ellipsize(Pango.EllipsizeMode.END)
        layout.set_text(node.label, -1)
        cr.set_source_rgba(*self.color("fg"))
        cr.move_to(x + 10, y + (sh - layout.get_pixel_size()[1]) / 2)
        PangoCairo.show_layout(cr, layout)

    def on_motion(self, controller, x, y):
        x, y = x / self.zoom, y / self.zoom
        self.set_shift(bool(controller.get_current_event_state() & Gdk.ModifierType.SHIFT_MASK))
        found = self.node_at(x, y)
        self.set_cursor_from_name("pointer" if found else None)
        self.set_hover(found)
        pin = self.pin_at(x, y)
        if pin is not None:
            self.set_tooltip_text(self.pin_tips[1 if pin in self.pinned else 0])
        start = self.start_at(x, y)
        battery = next((n for n, (bx, by, bw, bh) in self.battery_rects.items()
                        if bx <= x <= bx + bw and by <= y <= by + bh and n in self.batteries), None)
        stop = self.stop_at(x, y)
        if stop is not None and len(self.start_tips) > 2:
            self.set_tooltip_text(self.start_tips[3 if stop.startswith("runtime:") and len(self.start_tips) > 3 else 2])
        elif start is not None and self.start_state is not None:
            self.set_tooltip_text(self.start_tips[1 if self.start_state(start) == "show" else 0])
        elif battery is not None:
            self.set_tooltip_text(self.batteries[battery][2])
        elif pin is None and ((edge := self.hook_at(x, y)) is not None
                              or (found is None and (edge := self.edge_at(x, y)) is not None)):
            # A hook, or a line that can be moved: what to do with it (the cursor says it can be grabbed).
            source, target = edge
            broken = any((e.source, e.target) == edge and e.failed for e in self.graph.edges)
            if broken and len(self.edge_tips) > 6:
                self.set_tooltip_text(self.edge_tips[6])
            elif self.kind_of(source) in ("runtime", "cloud"):
                names = ", ".join(n.label for n in self.graph.nodes if n.id in self.model_users(source, target))
                busy = self.model_busy(source, target)
                tip = (4 if self.kind_of(source) == "cloud" else 1) if names and not busy else 2 if names else 3
                self.set_tooltip_text(self.edge_tips[min(tip, len(self.edge_tips) - 1)].format(names))
            elif self.kind_of(target) == "cloud" and len(self.edge_tips) > 5:
                self.set_tooltip_text(self.edge_tips[5])
            else:
                self.set_tooltip_text(self.edge_tips[0])
            self.set_cursor_from_name("grab" if edge == self.selected_edge or edge == self.hook_at(x, y)
                                      else "pointer")
        elif pin is None:
            self.set_tooltip_text(None)        # no tooltip left over from a button the pointer left

    def pin_at(self, x, y):
        """The node whose pin is at (x, y) in map units, or None."""
        for node_id, (px, py, pw, ph) in self.pin_rects.items():
            if px <= x <= px + pw and py <= y <= py + ph:
                return node_id
        return None

    def stop_at(self, x, y):
        for node_id, (sx, sy, sw, sh) in self.stop_rects.items():
            if sx <= x <= sx + sw and sy <= y <= sy + sh:
                return node_id
        return None

    def start_at(self, x, y):
        """The agent box whose start button is at (x, y) in map units, or None."""
        for node_id, (sx, sy, sw, sh) in self.start_rects.items():
            if sx <= x <= sx + sw and sy <= y <= sy + sh:
                return node_id
        return None

    def toggle_pin(self, node_id):
        if node_id in self.pinned:
            self.pinned.remove(node_id)
        else:
            self.pinned.append(node_id)
        self.queue_draw()

    def focus(self):
        """The hovered box the lights follow; none while something is pinned."""
        return None if self.pinned else self.hover

    def set_hover(self, node_id):
        if node_id == self.hover:
            return
        self.hover = node_id
        node = next((n for n in self.graph.nodes if n.id == node_id), None) if self.graph else None
        tip = " · ".join(x for x in (node.label, node.detail) if x) if node else None
        pick = self.model_pick(node_id) if node is not None else None
        if pick is not None:                      # with an agent selected: this model can be its model
            agent = next((n.label for n in self.graph.nodes if n.id == pick[0]), "")
            tip += "\n" + self.pick_tip.format(agent)
        self.set_tooltip_text(tip)
        self.queue_draw()

    def lit(self):
        """Nodes and edges joined to the hovered node (none while something is pinned)."""
        if self.focus() is None or self.graph is None:
            return None, None
        return self.related(self.focus())

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
        if start.startswith("model:"):
            # A model: also the agents that use it, with their line to the provider that serves it,
            # so their whole path (and its light) stays lit.
            for edge in self.graph.edges:
                if edge.kind == "default" and edge.target == start:
                    for index, uses in enumerate(self.graph.edges):
                        if uses.kind == "uses" and uses.source == edge.source and uses.target in nodes:
                            edges.add(index)
                            nodes.add(edge.source)
        return nodes, edges

    # --- selection ------------------------------------------------------------------------

    def highlighted(self):
        """(node ids, edge indexes) to light: the hovered box's, else the
        selected and pinned boxes' together; (None, None) when nothing is picked."""
        if self.focus() is not None:
            return self.lit()
        picked = list(dict.fromkeys(self.selected + self.pinned))
        if not picked or self.graph is None:
            return None, None
        nodes, edges = set(), set()
        for node_id in picked:
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
        """Click: select this box only (again: none). Shift+click: add or remove it.
        A click on a box's pin pins or unpins it."""
        x, y = x / self.zoom, y / self.zoom
        pin = self.pin_at(x, y)
        if pin is not None:
            self.toggle_pin(pin)
            return
        stop = self.stop_at(x, y)
        if stop is not None and self.on_stop is not None:
            self.on_stop(stop)
            return
        start = self.start_at(x, y)
        if start is not None and self.on_start is not None:
            self.on_start(start)
            return
        hook = self.hook_at(x, y)
        if hook is not None:                   # a hook clicked: its line is picked (even over a box)
            if hook != self.selected_edge:
                self.selected_edge = hook
                self.queue_draw()
            return
        node_id = self.node_at(x, y)
        edge = self.edge_at(x, y) if node_id is None else None
        # A "!" clicked: its own line, even where other lines cross it.
        marker = min(((math.hypot(mx - x, my - y), key) for key, mx, my in self.marker_points), default=None)
        if node_id is None and marker is not None and marker[0] <= 10:
            edge = marker[1]
        if edge is not None or self.selected_edge is not None:
            self.selected_edge = None if edge == self.selected_edge else edge
            self.queue_draw()
            if edge is not None:
                failed = next((e for e in self.graph.edges if (e.source, e.target) == edge and e.failed), None)
                if failed is not None and self.on_failed_edge is not None:
                    self.on_failed_edge(edge, self.to_pixels((x - 4, y - 4, 8, 8)))
                return
        if node_id is None:
            return
        pick = self.model_pick(node_id)
        if pick is not None and self.on_pick_model is not None:
            self.on_pick_model(*pick)
            return
        adding = bool(gesture.get_current_event_state() & Gdk.ModifierType.SHIFT_MASK)
        if adding:
            if node_id in self.selected:
                self.selected.remove(node_id)
            else:
                if node_id.startswith("agent:"):
                    # One agent at a time: another one replaces it.
                    self.selected = [i for i in self.selected if not i.startswith("agent:")]
                self.selected.append(node_id)
        else:
            self.selected = [] if self.selected == [node_id] else [node_id]
        self.selection_changed()

    def model_pick(self, node_id):
        """(agent id, runtime id, model name) when node_id is a model of a local
        LLM the one selected agent is connected to; else None."""
        agents = [i for i in self.selected if i.startswith("agent:")]
        if self.graph is None or len(agents) != 1 or self.kind_of(node_id) != "model":
            return None
        agent = agents[0]
        node = next(n for n in self.graph.nodes if n.id == node_id)
        for edge in self.graph.edges:
            if edge.target == node_id and edge.kind == "serves" and edge.source.startswith(("runtime:", "cloud:")) \
                    and any(e.source == agent and e.target == edge.source and e.kind in ("uses", "available")
                            for e in self.graph.edges):
                return agent, edge.source, node.label
        return None

    @staticmethod
    def active_paths(graph) -> dict:
        """agent id -> {edge index}: agent → provider it uses → the model it uses."""
        index = {(e.source, e.target): i for i, e in enumerate(graph.edges)}
        paths = {}
        for edge in graph.edges:
            if edge.kind != "default":
                continue
            agent, model = edge.source, edge.target
            for (source, target), i in index.items():
                if source == agent and graph.edges[i].kind == "uses" and (target, model) in index:
                    paths.setdefault(agent, set()).update({i, index[(target, model)]})
        return paths

    def active_now(self, lit_edges):
        """(edge indexes, model ids) of the active paths to draw bold: the hovered
        agent's, else the selected agents', else every agent's; never outside the lit lines."""
        if self.focus() is not None and self.focus().startswith("agent:"):
            agents = [self.focus()]
        else:
            agents = [i for i in self.selected + self.pinned if i.startswith("agent:")] or list(self.active)
        edges = set().union(*(self.active.get(a, set()) for a in agents)) if agents else set()
        if lit_edges is not None:
            edges &= lit_edges
        models = {e.target for i, e in enumerate(self.graph.edges)
                  if e.kind == "default" and e.source in agents
                  and any(self.graph.edges[j].target == e.target for j in edges)}
        return edges, models

    def set_selected(self, ids):
        """Selection from outside (the other map, the cards): no callback."""
        ids = list(ids)
        if ids != self.selected:
            self.selected = ids
            self.relayout()

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

    # --- flowing light on running agents' paths ------------------------------------------------

    def set_animate(self, mode):
        """"all": light on running agents' lines and on lines other programs use; "agents": only
        the agents'; "off" (or False): still lines."""
        self.animate = mode if mode in ANIMATION_MODES else "all" if mode is True else "off"
        self.queue_draw()           # the map works out the flowing paths again and tells the layer

    def animations_allowed(self) -> bool:
        """The setting, and the desktop's own "animations" switch (reduced motion)."""
        if self.animate == "off":
            return False
        settings = Gtk.Settings.get_default()
        return settings is None or bool(settings.get_property("gtk-enable-animations"))

    def running_agents(self) -> set:
        """The agents whose lines flow: running ones, and of those only the working ones once known."""
        running = {n.id for n in self.graph.nodes if n.kind == "agent" and n.status == "running"}
        if self.working is not None:
            running &= {f"agent:{command}" for command in self.working}
        return running

    def set_working(self, commands):
        working = set(commands)
        if working != self.working:
            self.working = working
            self.queue_draw()          # the flow follows on this drawing

    def set_outside(self, outside: dict):
        """{runtime id: {model id: label}}: a program that is not on the map (Video Trans, a script)
        is using that local LLM now: the line to the model is drawn bold in its own colour with light
        running along it, and the model box names the program when nothing else does."""
        outside = {rid: dict(models) for rid, models in outside.items() if models}
        if outside != self.outside:
            self.outside = outside
            self.queue_draw()

    def outside_edge(self, edge) -> bool:
        return edge.kind == "serves" and not edge.failed and edge.target in self.outside.get(edge.source, {})

    def outside_label(self, node_id: str) -> str:
        return next((models[node_id] for models in self.outside.values() if node_id in models), "")

    def outside_paths(self, drawn) -> list:
        """[("outside:<model id>", [the line local LLM → model])] for set_outside."""
        paths = []
        for index, edge in enumerate(self.graph.edges):
            if index in drawn and self.outside_edge(edge):
                paths.append((f"outside:{edge.target}", [edge]))
        return paths

    def flow_paths(self, active_edges, drawn, running_only: bool = True) -> list:
        """[(agent id, [edges agent → provider → model])] of the agents (the running ones, unless
        running_only is off) whose active path is drawn bold now. With the agents hidden (Show) the
        light runs on the part still drawn: provider → model."""
        agents = self.running_agents() if running_only else set(self.active)
        paths = []
        for agent in sorted(agents):
            edges = [self.graph.edges[i] for i in self.active.get(agent, ()) if i in active_edges]
            drawn_edges = [self.graph.edges[i] for i in self.active.get(agent, ())
                           if i in active_edges and i in drawn]
            first = [e for e in edges if e.source == agent]
            if not first:
                continue
            rest = [e for e in drawn_edges if e.source == first[0].target]
            path = [e for e in first[:1] if e in drawn_edges] + rest[:1]
            if path:
                paths.append((agent, path))
        return paths

    FLOW_PERIOD = 2.6            # seconds for a light to run the whole path
    FLOW_PULSES = 3              # lights on a path at a time
    FLOW_FADE = 0.3              # seconds to fade an agent's lights in or out (never on and off at once)
    FLOW_GRACE = 5.0             # seconds an agent's lights keep running after it stopped working (pauses
                                 # between tokens, a missed check), so they do not blink

    def draw_flow(self, cr, paths, now: float, levels=None):
        """Comets running from the agent through the provider to the model: a bright head in a soft
        glow and a tail that tapers and fades along the line."""
        tail, steps = 0.3, 22        # tail length (in segments) and the samples it is drawn with
        fg = self.color("fg")
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        for agent, edges in paths:
            level = 1.0 if levels is None else levels.get(agent, 0.0)
            if level <= 0:
                continue
            # An app outside the map: its own colour; an agent: the accent.
            accent = self.color("outside" if agent.startswith("outside:") else "accent")
            light = tuple(a + (f - a) * 0.6 for a, f in zip(accent[:3], fg[:3]))   # brighter than the line under it
            offset = (sum(map(ord, agent)) % 97) / 97          # agents do not pulse in step
            count = len(edges)
            for pulse in range(self.FLOW_PULSES):
                head = ((now / self.FLOW_PERIOD + offset + pulse / self.FLOW_PULSES) % 1.0) * count
                # Fade in at the agent and out at the model, so the comets do not pop.
                ends = max(0.0, min(1.0, head / 0.15, (count - head) / 0.15)) * level
                if ends <= 0:
                    continue
                points = []
                for step in range(steps + 1):
                    at = head - tail * step / steps
                    if at < 0:
                        break
                    edge = edges[min(int(at), count - 1)]
                    points.append(self.curve_point(edge, at - min(int(at), count - 1)))
                for i in range(len(points) - 1):             # the tail: thick and bright near the head
                    fade = 1 - i / steps
                    cr.set_source_rgba(*light, 0.9 * fade * fade * ends)
                    cr.set_line_width(1.0 + 3.5 * fade)
                    if math.hypot(points[i][0] - points[i + 1][0], points[i][1] - points[i + 1][1]) > 40:
                        continue             # the jump through the provider's box: no line across it
                    cr.move_to(*points[i])
                    cr.line_to(*points[i + 1])
                    cr.stroke()
                x, y = points[0]
                glow = cairo.RadialGradient(x, y, 0, x, y, 18)
                glow.add_color_stop_rgba(0, *light, 0.8 * ends)
                glow.add_color_stop_rgba(0.35, *accent[:3], 0.35 * ends)
                glow.add_color_stop_rgba(1, *accent[:3], 0.0)
                cr.set_source(glow)
                cr.arc(x, y, 18, 0, 2 * math.pi)
                cr.fill()
                cr.set_source_rgba(*self.color("fg", ends))
                cr.arc(x, y, 3.2, 0, 2 * math.pi)
                cr.fill()
        cr.set_line_cap(cairo.LINE_CAP_BUTT)

    def with_flow_layer(self) -> Gtk.Widget:
        """The map with its FlowLayer on top (put this into the page, not the map alone)."""
        overlay = Gtk.Overlay(child=self)
        self.flow_layer = FlowLayer(self)
        overlay.add_overlay(self.flow_layer)
        return overlay

    # --- drawing ------------------------------------------------------------------------------

    def color(self, name: str, alpha: float = 1.0):
        return rgba(self.colors.get(name, "#888888"), alpha)

    def on_draw(self, _area, cr, width, height):
        if self.graph is None:
            return
        zoom = self.zoom
        if zoom > 1 and abs(int(self.viewport_width() * zoom) - self.get_content_width()) > 2:
            GLib.idle_add(self.relayout)          # the view was resized: follow it
        cr.scale(zoom, zoom)
        width, height = width / zoom, height / zoom
        node_w, _gap = self.layout(width, height)
        lit_nodes, lit_edges = self.highlighted()
        # Hover and pins only fade the rest; a selection draws nothing but its own connections.
        only_lit = self.focus() is None and lit_edges is not None and bool(self.selected)
        layout = PangoCairo.create_layout(cr)

        # Column headings.
        for column, title in enumerate(self.headers):
            if column not in self.column_x:
                continue
            x = self.column_x[column]
            text = title.upper()
            layout.set_font_description(Pango.FontDescription.from_string("Sans Bold 8"))
            layout.set_text(text, -1)
            layout.set_width(node_w * Pango.SCALE)
            layout.set_ellipsize(Pango.EllipsizeMode.END)
            cr.set_source_rgba(*self.color("accent"))
            cr.move_to(x, 6)
            PangoCairo.show_layout(cr, layout)

        # Small headings above the groups of column 0.
        layout.set_font_description(Pango.FontDescription.from_string("Sans Bold 7"))
        layout.set_width(node_w * Pango.SCALE)
        layout.set_ellipsize(Pango.EllipsizeMode.END)
        for gx, gy, text, column in self.group_labels:
            layout.set_text(text if column == 3 else text.upper(), -1)   # folder paths keep their case
            cr.set_source_rgba(*self.color("dim"))
            cr.move_to(gx + 2, gy)
            PangoCairo.show_layout(cr, layout)

        # Edges first, under the boxes; the active paths last, on top.
        active_edges, active_models = self.active_now(lit_edges)
        order = [i for i in range(len(self.graph.edges)) if i not in active_edges] + sorted(active_edges)
        markers = []                 # failed lines: a "!" on each, drawn over all lines
        drawn = set()                # edge indexes drawn this time
        for index in order:
            edge = self.graph.edges[index]
            if edge.kind == "default" or edge.source not in self.boxes or edge.target not in self.boxes:
                continue
            x1, y1, x2, y2 = self.edge_points(edge)
            bend = (x2 - x1) * 0.5
            picked = (edge.source, edge.target) == self.selected_edge
            alpha = 0.55
            if only_lit and index not in lit_edges and not picked:
                continue
            if lit_edges is not None:
                alpha = 0.95 if index in lit_edges else 0.08
            if picked:
                moving = self.link is not None and self.link.get("relink") and self.link["drawing"]
                alpha = 0.3 if moving else 1.0
            active = index in active_edges
            outside = not active and self.outside_edge(edge)
            loaded = not active and not outside and self.loaded_edge(edge)
            if (active or outside or loaded) and not picked and not (lit_edges is not None and index not in lit_edges):
                alpha = 1.0
            if edge.failed:
                # Failed (red) or with warnings (yellow), dotted, until it is removed by hand.
                cr.set_source_rgba(*self.color("red" if edge.failed == "error" else "yellow",
                                               max(alpha, 0.85) if not (lit_edges is not None and alpha < 0.5) else alpha))
                cr.set_line_width(3.2 if picked else 2.4)
                cr.set_line_cap(cairo.LINE_CAP_ROUND)
                cr.set_dash([0.1, 5])
            else:
                # Loaded in its local LLM but no agent's: solid and light (EDGE_COLORS["loaded"]).
                cr.set_source_rgba(*self.color("accent" if active else "outside" if outside
                                               else EDGE_COLORS.get("loaded" if loaded else edge.kind, "muted"),
                                               0.85 if loaded and alpha == 1.0 else alpha))
                cr.set_line_width(3.2 if picked else 2.8 if active or outside else 2.2 if loaded else
                                  2.0 if lit_edges is not None and index in lit_edges else 1.4)
                # Dashed: set up but not in use (an agent's second provider).
                cr.set_dash([5, 4] if edge.kind == "available" else [])
            cr.move_to(x1, y1)
            cr.curve_to(x1 + bend, y1, x2 - bend, y2, x2, y2)
            cr.stroke()
            cr.set_dash([])
            drawn.add(index)
            cr.set_line_cap(cairo.LINE_CAP_BUTT)
            if edge.failed and not (only_lit and index not in lit_edges):
                markers.append(edge)

        self.draw_markers(cr, markers)
        allowed = self.animations_allowed() and self.link is None
        if self.flow_layer is not None:
            outside = self.outside_paths(drawn) if allowed and self.animate == "all" else []
            self.flow_layer.update(self.flow_paths(active_edges, drawn, running_only=False) + outside if allowed else [],
                                   self.running_agents() | {key for key, _edges in outside} if allowed else set())

        self.pin_rects = {}
        self.start_rects = {}
        self.stop_rects = {}
        self.battery_rects = {}
        for node in self.graph.nodes:
            if node.id not in self.boxes:
                continue
            x, y, w, h = self.boxes[node.id]
            faded = lit_nodes is not None and node.id not in lit_nodes
            alpha = 0.25 if faded else (0.55 if node.dim or node.status == "missing" else 1.0)
            selected = node.id in self.selected
            # A card: the theme's background, a light tint (stronger for agents), a thin frame.
            self.rounded(cr, x, y, w, h, self.radius)
            cr.set_source_rgba(*self.color("bg", 1.0))
            cr.fill_preserve()
            fill = 0.09 if node.kind == "agent" else 0.05
            cr.set_source_rgba(*self.color("fg", fill * (0.4 if faded else 1.0)))
            cr.fill_preserve()
            if selected:
                cr.set_source_rgba(*self.color("accent", 0.20))
                cr.fill_preserve()
            # A model an app outside the map generates with now: framed in that light's colour.
            outside = node.kind == "model" and bool(self.outside_label(node.id))
            border = "accent" if node.id == self.hover or node.id in self.defaults or node.kind == "agent" \
                or selected else "outside" if outside else "fg"
            cr.set_source_rgba(*self.color(border, (0.16 if border == "fg" else 0.9) * (0.3 if faded else 1)))
            cr.set_line_width(2.2 if selected else 2 if node.id in active_models or outside else 1)
            cr.stroke()
            if selected:
                self.draw_check(cr, layout, x + w - PIN_W - 22, y + (h - 16) / 2)
            self.draw_pin(cr, layout, node.id, x, y, w, faded)

            use = self.runtime_use(node)
            detail = node.detail or (self.outside_label(node.id) if node.kind == "model" else "")
            right = 0                # room kept free at the right for the loaded mark or the start button
            if node.kind == "agent" and self.start_state is not None and self.start_state(node.id):
                right = self.draw_start(cr, layout, node.id, x, y, w, h, faded)
            elif node.kind == "runtime" and self.on_stop is not None and use is not None and use.running \
                    and use.vram >= STOP_VRAM:
                right = self.draw_runtime_stop(cr, layout, node.id, x, y, w, h, faded)
            if node.kind in ("agent", "runtime") and node.id in self.batteries:
                right += self.draw_battery(cr, node.id, x + w - PIN_W - (24 if selected else 4) - right, y, h, faded)
            text_x = self.draw_tile(cr, layout, node, x, y, h, faded, alpha, use)
            if node.kind == "runtime" and use is not None and use.vram >= 64 * 1024 ** 2:
                detail = " · ".join(filter(None, [detail, f"{use.vram / 1024 ** 3:.1f} GB GPU"]))
            if node.kind == "model" and use is not None:
                state = self.model_state(node, use)
                if state:
                    # Loaded (green), loading (yellow), asleep (a grey ring: listed, GPU memory free).
                    cx, cy = x + w - PIN_W - (30 if selected else 8), y + h / 2
                    if state == "sleeping":
                        cr.set_source_rgba(*self.color("muted", 0.35 if faded else 1.0))
                        cr.set_line_width(1.5)
                        cr.arc(cx, cy, 3.5, 0, 2 * math.pi)
                        cr.stroke()
                    else:
                        loaded = "outside" if outside else "green"
                        cr.set_source_rgba(*self.color(loaded if state == "loaded" else "yellow",
                                                       0.35 if faded else 1.0))
                        cr.arc(cx, cy, 4, 0, 2 * math.pi)
                        cr.fill()
                    right = 14
            layout.set_width(int((x + w - PIN_W - (24 if node.id in self.selected else 4) - right - text_x)
                                 * Pango.SCALE))
            layout.set_ellipsize(Pango.EllipsizeMode.END)
            if detail:
                layout.set_font_description(Pango.FontDescription.from_string("Sans Bold 9"))
                layout.set_text(node.label, -1)
                top_h = layout.get_pixel_size()[1]
                top = y + (h - 2 * top_h + 1) / 2
                cr.set_source_rgba(*self.color("fg", alpha))
                cr.move_to(text_x, top)
                PangoCairo.show_layout(cr, layout)
                layout.set_font_description(Pango.FontDescription.from_string("Sans 8"))
                layout.set_text(detail, -1)
                cr.set_source_rgba(*self.color("outside" if outside and not node.detail else "dim", alpha))
                cr.move_to(text_x, top + top_h)
                PangoCairo.show_layout(cr, layout)
            else:
                weight = "Bold " if node.kind in ("agent", "cloud", "runtime") else ""
                layout.set_font_description(Pango.FontDescription.from_string(f"Sans {weight}9"))
                layout.set_text(node.label, -1)
                cr.set_source_rgba(*self.color("fg", alpha))
                cr.move_to(text_x, y + (h - layout.get_pixel_size()[1]) / 2)
                PangoCairo.show_layout(cr, layout)

        self.draw_hooks(cr)                       # over the boxes: a hook is never half hidden
        self.draw_link(cr)

    def tile_glyph(self, node) -> tuple[str, str]:
        """(glyph, font) of a box's icon tile: an agent's own icon, else one for its kind."""
        if node.icon:
            return node.icon, node.icon_font
        if node.kind == "mcp":
            return glyph(MCP_GLYPHS.get(node.label, "app")), ""
        return glyph(TILE_GLYPHS.get(node.kind, "package")), ""

    _SVGS: dict = {}           # (icon name, colour) -> Rsvg.Handle with that colour's stylesheet

    def draw_icon(self, cr, text: str, font: str, x: float, y: float, size: float, rgba) -> bool:
        """A glyph from the app's own icons (icons.py) in one colour, in the square (x, y, size);
        False when there is no stored icon for it (then the caller draws the font's glyph)."""
        name = icons.name_for(text, font)
        path = icons.path_for(name) if name else None
        if path is None:
            return False
        try:
            gi.require_version("Rsvg", "2.0")
            from gi.repository import Rsvg
        except (ImportError, ValueError):
            return False
        key = (name, tuple(round(c, 3) for c in rgba))
        handle = self._SVGS.get(key)
        if handle is None:
            try:
                handle = Rsvg.Handle.new_from_file(str(path))
                r, g, b, a = (max(0.0, min(1.0, c)) for c in rgba)
                handle.set_stylesheet(f"* {{ fill: rgba({r * 255:.0f},{g * 255:.0f},{b * 255:.0f},{a:.3f}) "
                                      f"!important; stroke: none !important; }}".encode())
            except Exception:
                return False
            if len(self._SVGS) > 400:
                self._SVGS.clear()
            self._SVGS[key] = handle
        viewport = Rsvg.Rectangle()
        viewport.x, viewport.y, viewport.width, viewport.height = x, y, size, size
        try:
            handle.render_document(cr, viewport)
        except Exception:
            return False
        return True

    def draw_tile(self, cr, layout, node, x, y, h, faded, alpha, use) -> float:
        """The icon tile at a box's start, tinted in the colour of the kind's lines, with the status
        dot on its corner (green running, yellow stopped, grey not installed; a ring while
        generating). Returns where the text starts."""
        size = h - 14
        tx, ty = x + 7, y + (h - size) / 2
        tone = TILE_TONES.get(node.kind, "accent")
        self.rounded(cr, tx, ty, size, size, min(self.radius, 7))
        cr.set_source_rgba(*self.color(tone, 0.18 * (0.4 if faded else 1.0)))
        cr.fill()
        text, font = self.tile_glyph(node)
        colour = self.color(tone if tone != "muted" else "fg", alpha)
        inner = size * 0.62
        if not self.draw_icon(cr, text, font, tx + (size - inner) / 2, ty + (size - inner) / 2, inner, colour):
            family = "omarchy" if font == "omarchy" else "JetBrainsMono Nerd Font, Symbols Nerd Font"
            layout.set_font_description(Pango.FontDescription.from_string(f"{family} 13"))
            layout.set_width(-1)
            layout.set_text(text, -1)
            glyph_w, glyph_h = layout.get_pixel_size()
            cr.set_source_rgba(*colour)
            cr.move_to(tx + (size - glyph_w) / 2, ty + (size - glyph_h) / 2)
            PangoCairo.show_layout(cr, layout)
        if node.status:
            name = {"running": "green", "stopped": "yellow", "noserver": "yellow"}.get(node.status, "muted")
            cx, cy = tx + size - 1, ty + size - 1
            cr.set_source_rgba(*self.color("bg", 1.0))
            cr.arc(cx, cy, 5.5, 0, 2 * math.pi)
            cr.fill()
            cr.set_source_rgba(*self.color(name, 0.35 if faded else 1.0))
            cr.arc(cx, cy, 4, 0, 2 * math.pi)
            cr.fill()
            if node.kind == "runtime" and use is not None and use.vram >= 256 * 1024 ** 2 and self.usage.generating:
                # Working: a ring around the dot, as LLM Monitor's "generating".
                cr.set_source_rgba(*self.color("accent", 0.35 if faded else 1.0))
                cr.set_line_width(1.5)
                cr.arc(cx, cy, 7.5, 0, 2 * math.pi)
                cr.stroke()
        return tx + size + 9

    def draw_check(self, cr, layout, x, y):
        """A selected box's badge: ✓ on the accent colour (round, or square with Square corners)."""
        self.rounded(cr, x, y, 16, 16, 8 if self.radius else 0)
        cr.set_source_rgba(*self.color("accent"))
        cr.fill()
        layout.set_font_description(Pango.FontDescription.from_string("Sans Bold 8"))
        layout.set_width(-1)
        layout.set_text("✓", -1)
        text_w, text_h = layout.get_pixel_size()
        cr.set_source_rgba(*self.color("bg"))
        cr.move_to(x + (16 - text_w) / 2, y + (16 - text_h) / 2)
        PangoCairo.show_layout(cr, layout)

    def draw_start(self, cr, layout, node_id, x, y, w, h, faded) -> int:
        """An agent's start button (▶), left of the tick; while it runs here a stop button in its
        place, and left of it a console. Returns the room they take from the text."""
        state = self.start_state(node_id) if self.start_state is not None else ""
        sx = x + w - PIN_W - (24 if node_id in self.selected else 4) - START_W
        sy = y + (h - START_W) / 2
        hot = node_id == self.hover
        if state != "show":
            self.start_rects[node_id] = (sx, sy, START_W, START_W)
            self.draw_square_button(cr, layout, sx, sy, PLAY, "fg", False, hot, faded)
            return START_W + 4
        # Running here: stop where ▶ was, then left of it the console (show its terminal).
        self.stop_rects[node_id] = (sx, sy, START_W, START_W)
        self.draw_square_button(cr, layout, sx, sy, STOP, "red", True, hot, faded)
        left = sx - START_W - 4
        self.start_rects[node_id] = (left, sy, START_W, START_W)
        self.draw_square_button(cr, layout, left, sy, CONSOLE, "accent", True, hot, faded)
        return int(sx - left) + START_W + 4

    def draw_runtime_stop(self, cr, layout, node_id, x, y, w, h, faded) -> int:
        """A local LLM holding GPU memory: a stop button (it asks: stop, or only free the memory)."""
        sx = x + w - PIN_W - (24 if node_id in self.selected else 4) - START_W
        sy = y + (h - START_W) / 2
        self.stop_rects[node_id] = (sx, sy, START_W, START_W)
        self.draw_square_button(cr, layout, sx, sy, STOP, "red", True, node_id == self.hover, faded)
        return START_W + 4

    def draw_square_button(self, cr, layout, sx, sy, glyph_, tone, lit, hot, faded):
        """A small square button with a glyph: tinted in `tone` when lit (a running agent's)."""
        self.rounded(cr, sx, sy, START_W, START_W, min(self.radius, 4))
        cr.set_source_rgba(*self.color(tone if lit else "fg", (0.18 if lit else 0.08) * (0.4 if faded else 1)))
        cr.fill_preserve()
        cr.set_source_rgba(*self.color(tone if hot or lit else "fg", (0.9 if hot else 0.3)
                                       * (0.4 if faded else 1)))
        cr.set_line_width(1)
        cr.stroke()
        colour = self.color(tone if hot or lit else "fg", 0.4 if faded else 1.0)
        if not self.draw_icon(cr, glyph_, "", sx + 3, sy + 3, START_W - 6, colour):
            layout.set_font_description(Pango.FontDescription.from_string("JetBrainsMono Nerd Font, Symbols Nerd Font 9"))
            layout.set_width(-1)
            layout.set_ellipsize(Pango.EllipsizeMode.NONE)
            layout.set_text(glyph_, -1)
            text_w, text_h = layout.get_pixel_size()
            cr.set_source_rgba(*colour)
            cr.move_to(sx + (START_W - text_w) / 2, sy + (START_W - text_h) / 2)
            PangoCairo.show_layout(cr, layout)

    def draw_battery(self, cr, node_id, right, y, h, faded) -> int:
        """A small battery ending at `right`: filled to what is left of the tightest limit, green,
        yellow (a quarter or less) or red (under a tenth). Returns the room it takes."""
        left, colour, _tip = self.batteries[node_id]
        bw, bh = 20, 10
        bx, by = right - bw - 5, y + (h - bh) / 2
        self.battery_rects[node_id] = (bx - 2, by - 4, bw + 6, bh + 8)
        alpha = 0.4 if faded else 1.0
        cr.set_source_rgba(*self.color("fg", 0.55 * alpha))
        cr.set_line_width(1)
        cr.rectangle(bx + 0.5, by + 0.5, bw - 1, bh - 1)
        cr.stroke()
        cr.rectangle(bx + bw, by + 3, 2, bh - 6)          # the nub
        cr.fill()
        cr.set_source_rgba(*self.color(colour, alpha))
        cr.rectangle(bx + 2, by + 2, max(1.5, (bw - 4) * left), bh - 4)
        cr.fill()
        return bw + 9

    def draw_pin(self, cr, layout, node_id, x, y, w, faded):
        """The pin at the top right: filled when pinned, an outline while the box is hovered."""
        pinned = node_id in self.pinned
        self.pin_rects[node_id] = (x + w - PIN_W - 2, y + 1, PIN_W, PIN_W)
        if not pinned and node_id != self.hover:
            return
        colour = self.color("accent" if pinned else "dim", 0.4 if faded and not pinned else 1.0)
        if self.draw_icon(cr, PIN if pinned else PIN_OUTLINE, "", x + w - PIN_W - 1, y + 2, PIN_W - 2, colour):
            return
        layout.set_font_description(Pango.FontDescription.from_string("JetBrainsMono Nerd Font, Symbols Nerd Font 9"))
        layout.set_width(-1)
        layout.set_text(PIN if pinned else PIN_OUTLINE, -1)
        cr.set_source_rgba(*colour)
        text_w, _text_h = layout.get_pixel_size()
        cr.move_to(x + w - PIN_W - 2 + (PIN_W - text_w) / 2, y + 2)
        PangoCairo.show_layout(cr, layout)

    @staticmethod
    def rounded_top(cr, x, y, w, h, r):
        """A bar: rounded at its data end (the top), square on the baseline."""
        r = min(r, h, w / 2)
        cr.new_sub_path()
        cr.move_to(x, y + h)
        cr.line_to(x, y + r)
        if r > 0:
            cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
            cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
        else:
            cr.line_to(x + w, y)
        cr.line_to(x + w, y + h)
        cr.close_path()

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


class Meter(Gtk.DrawingArea):
    """A thin rounded bar split into parts, as LLM Monitor's GPU memory and
    activity meters: parts = [(fraction, colour name, alpha)], the rest is the
    empty track. `colors`: a function (name, alpha) -> rgba, the map's palette."""

    def __init__(self, colors, width: int = 120, height: int = 8):
        super().__init__()
        self.colors = colors
        self.parts = []
        self.set_content_width(width)
        self.set_content_height(height)
        self.set_valign(Gtk.Align.CENTER)
        self.set_draw_func(self.on_draw)

    def set_parts(self, parts):
        self.parts = list(parts)
        self.queue_draw()

    def on_draw(self, _area, cr, width, height):
        radius = height / 2
        MapView.rounded(cr, 0, 0, width, height, radius)
        cr.set_source_rgba(*self.colors("fg", 0.12))
        cr.fill_preserve()
        cr.save()
        cr.clip()
        x = 0.0
        for fraction, name, alpha in self.parts:
            part = width * max(0.0, min(1.0, fraction))
            cr.rectangle(x, 0, part, height)
            cr.set_source_rgba(*self.colors(name, alpha))
            cr.fill()
            x += part
        cr.restore()


class FlowLayer(Gtk.DrawingArea):
    """The lights on running agents' paths, drawn over the map in a layer of their own: only this
    layer is drawn again each frame (the map's drawing stays as it is), and it takes no clicks.

    Against flicker: drawn on every frame of the display (smooth, no stepping); positions follow
    one steady clock, so a new drawing of the map never moves or restarts them; an agent's lights
    fade in and out (FLOW_FADE) instead of switching, and keep running FLOW_GRACE seconds after
    the agent was last seen running, so a missed process check does not blink them."""

    def __init__(self, view: MapView):
        super().__init__()
        self.view = view
        self.tick = None             # the frame-clock callback while lights flow or fade
        self.last = 0                # frame time (µs) of the last tick
        self.paths = {}              # agent id -> its edges, last known (kept while fading out)
        self.visible = set()         # agents whose path the map draws bold now
        self.running = set()         # agents running at the map's last drawing
        self.stopped = {}            # agent id -> monotonic time it was no longer seen running
        self.levels = {}             # agent id -> 0..1, how bright its lights are now
        self.set_can_target(False)
        self.set_draw_func(self.on_draw)

    def update(self, paths, running):
        """From each drawing of the map: the agents' bold paths and which agents run."""
        now = GLib.get_monotonic_time() / 1e6
        for agent in self.running - running:
            self.stopped[agent] = now
        for agent in running:
            self.stopped.pop(agent, None)
        self.running = set(running)
        self.visible = {agent for agent, _edges in paths}
        self.paths.update(paths)
        self.follow()

    def target(self, agent: str, now: float) -> float:
        if agent not in self.visible:
            return 0.0
        if agent in self.running:
            return 1.0
        stopped = self.stopped.get(agent)
        return 1.0 if stopped is not None and now - stopped < MapView.FLOW_GRACE else 0.0

    def follow(self):
        """Tick on every frame while anything is lit or should be; stop when all is dark."""
        now = GLib.get_monotonic_time() / 1e6
        busy = any(self.target(a, now) > 0 for a in self.paths) or any(v > 0 for v in self.levels.values())
        if busy and self.tick is None:
            self.last = 0
            self.tick = self.add_tick_callback(self.on_tick)
        elif not busy and self.tick is not None:
            self.remove_tick_callback(self.tick)
            self.tick = None
            self.queue_draw()        # no lights left over

    def on_tick(self, _widget, clock):
        frame = clock.get_frame_time()
        step = min(0.1, (frame - self.last) / 1e6) if self.last else 0.0
        self.last = frame
        now = GLib.get_monotonic_time() / 1e6
        for agent in list(self.paths):
            goal, level = self.target(agent, now), self.levels.get(agent, 0.0)
            change = step / MapView.FLOW_FADE
            level = min(goal, level + change) if goal > level else max(goal, level - change)
            if level <= 0 and goal <= 0:
                self.levels.pop(agent, None)
                self.paths.pop(agent, None)
                self.stopped.pop(agent, None)
            else:
                self.levels[agent] = level
        self.queue_draw()
        if not self.paths:
            self.tick = None
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    def on_draw(self, _area, cr, _width, _height):
        view = self.view
        if not self.levels or view.graph is None:
            return
        cr.scale(view.zoom, view.zoom)
        try:
            view.draw_flow(cr, list(self.paths.items()), GLib.get_monotonic_time() / 1e6, self.levels)
        except KeyError:
            pass                     # a box went away since the map was drawn: the next drawing fixes it


class LegendSample(Gtk.DrawingArea):
    """One mark of the map, drawn small for the ⓘ legend as the map draws it:
    "dot:<colour>" / "ring" a box's status dot, "model:<state>" a model's mark
    at the right, "line:<kind>" a line between boxes, "check" a selected box.
    `colors`: a function (name, alpha) -> rgba, the map's palette."""

    def __init__(self, colors, sample: str, width: int = 40, height: int = 18):
        super().__init__()
        self.colors = colors
        self.sample = sample
        self.set_content_width(width)
        self.set_content_height(height)
        self.set_valign(Gtk.Align.CENTER)
        self.set_draw_func(self.on_draw)

    def box(self, cr, width, height):
        MapView.rounded(cr, 1, 1, width - 2, height - 2, 4)
        cr.set_source_rgba(*self.colors("fg", 0.05))
        cr.fill_preserve()
        cr.set_source_rgba(*self.colors("fg", 0.3))
        cr.set_line_width(1)
        cr.stroke()

    def on_draw(self, _area, cr, width, height):
        kind, _sep, value = self.sample.partition(":")
        mid = height / 2
        if kind in ("dot", "ring"):
            # As on a box: the icon tile with the status dot on its corner.
            self.box(cr, width, height)
            size = height - 6
            MapView.rounded(cr, 3, 3, size, size, 3)
            cr.set_source_rgba(*self.colors("accent", 0.18))
            cr.fill()
            cx, cy = 3 + size - 1, 3 + size - 1
            cr.set_source_rgba(*self.colors("bg"))
            cr.arc(cx, cy, 5, 0, 2 * math.pi)
            cr.fill()
            cr.set_source_rgba(*self.colors("green" if kind == "ring" else value))
            cr.arc(cx, cy, 3.5, 0, 2 * math.pi)
            cr.fill()
            if kind == "ring":
                cr.set_source_rgba(*self.colors("accent"))
                cr.set_line_width(1.5)
                cr.arc(cx, cy, 6.5, 0, 2 * math.pi)
                cr.stroke()
        elif kind == "model":
            self.box(cr, width, height)
            if value == "outside":
                MapView.rounded(cr, 1, 1, width - 2, height - 2, 4)
                cr.set_source_rgba(*self.colors("outside", 0.9))
                cr.set_line_width(2)
                cr.stroke()
            cx = width - 10
            if value == "sleeping":
                cr.set_source_rgba(*self.colors("muted"))
                cr.set_line_width(1.5)
                cr.arc(cx, mid, 3.5, 0, 2 * math.pi)
                cr.stroke()
            else:
                cr.set_source_rgba(*self.colors({"loaded": "green", "outside": "outside"}.get(value, "yellow")))
                cr.arc(cx, mid, 4, 0, 2 * math.pi)
                cr.fill()
        elif kind == "check":
            self.box(cr, width, height)
            MapView.rounded(cr, 1, 1, width - 2, height - 2, 4)
            cr.set_source_rgba(*self.colors("accent", 0.2))
            cr.fill_preserve()
            cr.set_source_rgba(*self.colors("accent"))
            cr.set_line_width(1.5)
            cr.stroke()
            badge = height - 6
            MapView.rounded(cr, width - badge - 4, 3, badge, badge, badge / 2)
            cr.set_source_rgba(*self.colors("accent"))
            cr.fill()
            layout = PangoCairo.create_layout(cr)
            layout.set_font_description(Pango.FontDescription.from_string("Sans Bold 7"))
            layout.set_text("✓", -1)
            text_w, text_h = layout.get_pixel_size()
            cr.set_source_rgba(*self.colors("bg"))
            cr.move_to(width - badge - 4 + (badge - text_w) / 2, 3 + (badge - text_h) / 2)
            PangoCairo.show_layout(cr, layout)
        elif kind == "line":
            if value in ("error", "warning"):
                color = "red" if value == "error" else "yellow"
                cr.set_source_rgba(*self.colors(color))
                cr.set_line_width(2.4)
                cr.set_line_cap(cairo.LINE_CAP_ROUND)
                cr.set_dash([0.1, 5])
                cr.move_to(2, mid)
                cr.line_to(width - 2, mid)
                cr.stroke()
                cr.set_dash([])
                cx = width / 2
                cr.arc(cx, mid, 7.5, 0, 2 * math.pi)
                cr.set_source_rgba(*self.colors("bg"))
                cr.fill_preserve()
                cr.set_source_rgba(*self.colors(color))
                cr.set_line_width(1.8)
                cr.stroke()
                cr.move_to(cx, mid - 3.8)
                cr.line_to(cx, mid + 0.9)
                cr.stroke()
                cr.arc(cx, mid + 3.4, 1.0, 0, 2 * math.pi)
                cr.fill()
                return
            if value == "outside":
                # A local LLM used from outside the map: the provider → model line, bold in its colour,
                # with the light's bright head.
                cr.set_source_rgba(*self.colors("outside"))
                cr.set_line_width(2.8)
                cr.move_to(2, mid)
                cr.line_to(width - 2, mid)
                cr.stroke()
                cr.set_source_rgba(*self.colors("fg", 0.9))
                cr.arc(width * 0.62, mid, 2.6, 0, 2 * math.pi)
                cr.fill()
                return
            active = value == "active"
            cr.set_source_rgba(*self.colors("accent" if active else EDGE_COLORS.get(value, "muted"),
                                            1.0 if active else 0.8))
            cr.set_line_width(2.8 if active else 2.2 if value == "loaded" else 1.6)
            cr.set_dash([5, 4] if value == "available" else [])
            cr.move_to(2, mid)
            cr.line_to(width - 2, mid)
            cr.stroke()
            cr.set_dash([])
            if active:
                # The flowing light's bright head.
                cr.set_source_rgba(*self.colors("fg", 0.9))
                cr.arc(width * 0.62, mid, 2.6, 0, 2 * math.pi)
                cr.fill()
