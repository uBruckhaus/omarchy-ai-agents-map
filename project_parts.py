"""AI Project Center pages: widgets the board, the Phases view and the project page share (Maprisma's
StageRail, TeamNodes, ImportanceMark, MessagesBadge in project_board.dart / project_messages.dart,
LargeStageRail, TeamGraph, ProjectIssues in project_page_parts.dart). Drawn in GTK 4 with the
window's theme: the rails' colours come from the desktop palette (AgentsMap.theme.colors) when they
are drawn, the rest from the app's CSS and the small sheet below."""

from __future__ import annotations

import math

from gi.repository import Gdk, GLib, Gtk, Pango

import ai_core
import project_texts as texts
from i18n import _

# The pages' own few styles, on top of the app's (theme_css.py): built from the palette, so they
# follow the theme whenever the Projects page is opened (apply_css).
_CSS = """
.am-project-card {{ background: alpha({fg}, 0.03); border: 1px solid alpha({fg}, 0.10); border-radius: {r}px;
  padding: 12px; }}
.am-project-card:focus-within {{ border-color: alpha({accent}, 0.7); }}
.am-project-card.am-project-busy {{ opacity: 0.75; }}
.am-board-dragging .am-project-card {{ box-shadow: inset 2px 0 0 alpha({accent}, 0.35),
  inset -2px 0 0 alpha({accent}, 0.35); }}
.am-board-dragging .am-project-card.am-project-vertical {{ box-shadow: inset 0 2px 0 alpha({accent}, 0.35),
  inset 0 -2px 0 alpha({accent}, 0.35); }}
.am-project-card.am-project-drop-before {{ box-shadow: inset 4px 0 0 {accent}, inset -2px 0 0 alpha({accent}, 0.35); }}
.am-project-card.am-project-drop-after {{ box-shadow: inset -4px 0 0 {accent}, inset 2px 0 0 alpha({accent}, 0.35); }}
.am-project-card.am-project-vertical.am-project-drop-before {{ box-shadow: inset 0 4px 0 {accent},
  inset 0 -2px 0 alpha({accent}, 0.35); }}
.am-project-card.am-project-vertical.am-project-drop-after {{ box-shadow: inset 0 -4px 0 {accent},
  inset 0 2px 0 alpha({accent}, 0.35); }}
.am-board-dragging .am-project-card.am-project-dragged {{ opacity: 0.4; box-shadow: none; }}
.am-project-grip {{ color: alpha({fg}, 0.45); padding: 4px; }}
.am-project-title {{ font-weight: 600; font-size: 14px; }}
.am-project-small {{ font-size: 11px; }}
.am-project-dim {{ color: alpha({fg}, 0.62); }}
.am-project-error {{ color: {red}; }}
.am-project-warn {{ color: {amber}; }}
.am-project-gaps {{ border: 1px solid {amber}; border-radius: {r}px; padding: 10px; }}
.am-project-node {{ border: 1px solid alpha({fg}, 0.25); border-radius: 999px; min-width: 30px;
  min-height: 30px; font-size: 11px; }}
.am-project-node.am-node-active {{ border: 2px solid {accent}; }}
.am-project-node.am-node-stopping, .am-project-node.am-node-stopped {{ border: 2px solid {red}; }}
.am-project-node.am-node-next {{ border: 2px solid {green}; }}
.am-project-member {{ border: 1px solid alpha({fg}, 0.25); border-radius: {r}px; padding: 6px 8px; }}
.am-project-member.am-node-active {{ border: 2px solid {accent}; }}
.am-project-member.am-node-stopping, .am-project-member.am-node-stopped {{ border: 2px solid {red}; }}
.am-project-member.am-node-next {{ border: 2px solid {green}; }}
.am-project-target {{ border: 1px solid alpha({fg}, 0.18); border-radius: {r}px; padding: 6px 8px; }}
.am-project-target.am-project-explicit {{ background: alpha({accent}, 0.16); }}
.am-project-link {{ min-height: 2px; background: alpha({fg}, 0.25); }}
.am-project-link.am-project-explicit {{ background: {accent}; }}
box.am-project-mark, menubutton.am-project-mark > button {{ font-size: 11px; padding: 0 6px; border-radius: 6px;
  min-height: 18px; min-width: 0; margin: 0; border: none; box-shadow: none; background: none;
  color: alpha({fg}, 0.62); font-weight: normal; }}
menubutton.am-project-mark {{ background: none; padding: 0; min-height: 0; }}
menubutton.am-project-mark > button:hover {{ background: alpha({fg}, 0.08); }}
box.am-project-mark.am-mark-high, menubutton.am-project-mark.am-mark-high > button {{
  background: alpha({accent}, 0.22); color: {accent}; font-weight: 600; }}
dropdown.am-project-drop > button {{ font-size: 13px; font-weight: normal; min-height: 0; padding: 3px 8px; }}
.am-project-badge {{ font-size: 11px; padding: 0 5px; border-radius: 6px; background: alpha({accent}, 0.20); }}
.am-project-banner {{ background: alpha({red}, 0.16); border-radius: {r}px; padding: 6px 10px; }}
.am-phase-column {{ background: alpha({fg}, 0.035); border-radius: {r}px; padding: 8px; }}
.am-phase-card {{ background: alpha({fg}, 0.04); border: 1px solid alpha({fg}, 0.10); border-radius: {r}px;
  padding: 8px; }}
.am-phase-card:focus, .am-phase-card:focus-within {{ border-color: alpha({accent}, 0.7); }}
"""


def apply_css(colors: dict, radius: int = 10):
    """The pages' sheet for the current palette (replaces the previous one)."""
    display = Gdk.Display.get_default()
    if display is None:
        return
    provider = getattr(apply_css, "provider", None)
    if provider is not None:
        Gtk.StyleContext.remove_provider_for_display(display, provider)
    provider = Gtk.CssProvider()
    provider.load_from_string(_CSS.format(fg=colors.get("foreground", "#cdd6f4"), accent=colors.get("accent", "#89b4fa"),
                                          red=colors.get("red", "#f04452"), green=colors.get("green", "#a6e3a1"),
                                          amber=colors.get("yellow", "#f9e2af"),
                                          r=max(0, int(radius))))
    Gtk.StyleContext.add_provider_for_display(display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1)
    apply_css.provider = provider


class SizeBox(Gtk.Widget):
    """A vertical or horizontal box that tells its size when it changes (the pages' breakpoints).
    A plain widget around one Gtk.Box: GTK 4 calls a widget's own size_allocate only when it has no
    layout manager, and a Gtk.Box always has one. Nothing is told once it left its window."""

    def __init__(self, on_size, orientation=Gtk.Orientation.VERTICAL, spacing: int = 0, hexpand: bool = False,
                 vexpand: bool = False):
        super().__init__(hexpand=hexpand, vexpand=vexpand)
        self.on_size = on_size
        self.size = (0, 0)
        self.box = Gtk.Box(orientation=orientation, spacing=spacing, hexpand=hexpand, vexpand=vexpand)
        self.box.set_parent(self)

    def append(self, child):
        self.box.append(child)

    def remove(self, child):
        self.box.remove(child)

    def do_dispose(self):
        if self.box is not None and self.box.get_parent() is self:
            self.box.unparent()
        self.box = None
        Gtk.Widget.do_dispose(self)

    def do_get_request_mode(self):
        return self.box.get_request_mode() if self.box is not None else Gtk.SizeRequestMode.CONSTANT_SIZE

    def do_measure(self, orientation, for_size):
        if self.box is None:
            return 0, 0, -1, -1
        return self.box.measure(orientation, for_size)

    def do_size_allocate(self, width, height, baseline):
        if self.box is not None:
            self.box.allocate(width, height, baseline, None)
        if (width, height) != self.size:
            self.size = (width, height)

            def tell():
                if self.box is not None and self.get_root() is not None:
                    self.on_size(width, height)
                return False
            GLib.idle_add(tell)


def _rgba(hex_code: str, alpha: float = 1.0) -> tuple:
    text = (hex_code or "#888888").lstrip("#")
    try:
        r, g, b = (int(text[i:i + 2], 16) / 255 for i in (0, 2, 4))
    except ValueError:
        r = g = b = 0.53
    return r, g, b, alpha


def label(text: str = "", classes=(), wrap: bool = False, xalign: float = 0, lines: int = 1,
          selectable: bool = False) -> Gtk.Label:
    widget = Gtk.Label(label=text, xalign=xalign)
    if wrap:
        widget.set_wrap(True)
        widget.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        widget.set_max_width_chars(1)
        widget.set_hexpand(True)
    else:
        widget.set_ellipsize(Pango.EllipsizeMode.END)
        widget.set_lines(lines)
    widget.set_selectable(selectable)
    for css in classes:
        widget.add_css_class(css)
    return widget


# Symbols some icon themes lack: the first one found is drawn.
FALLBACKS = {
    "view-columns-symbolic": ("view-columns-symbolic", "view-dual-symbolic"),
    "flag-symbolic": ("flag-symbolic", "starred-symbolic"),
    # Needs you: a person (Maprisma's hand); Adwaita's dialog-question is a diamond that reads as "�".
    "avatar-default-symbolic": ("avatar-default-symbolic", "user-info-symbolic", "system-users-symbolic"),
    "list-drag-handle-symbolic": ("list-drag-handle-symbolic", "open-menu-symbolic"),
    "find-location-symbolic": ("find-location-symbolic", "mark-location-symbolic", "edit-find-symbolic"),
    # The agent's abilities (Maprisma's shield_outlined).
    "security-high-symbolic": ("security-high-symbolic", "channel-secure-symbolic", "dialog-information-symbolic"),
}


def icon(name: str, size: int = 16) -> Gtk.Image:
    names = FALLBACKS.get(name, (name,))
    theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default()) if Gdk.Display.get_default() else None
    found = next((n for n in names if theme is None or theme.has_icon(n)), names[-1])
    image = Gtk.Image.new_from_icon_name(found)
    image.set_pixel_size(size)
    return image


def clear(box):
    while (child := box.get_first_child()) is not None:
        box.remove(child)


def set_accessible(widget, text: str):
    widget.update_property([Gtk.AccessibleProperty.LABEL], [text])


# --- stage rails -------------------------------------------------------------------------------------

def _draw_stops(cr, width, height, current: int, colors: dict, large: bool):
    lit, dim = _rgba(colors.get("accent")), _rgba(colors.get("foreground"), 0.28)
    count = len(texts.STAGES)
    if large:
        step = width / count
        xs = [step * (i + 0.5) for i in range(count)]
    else:
        step = (width - 10) / (count - 1)
        xs = [5 + i * step for i in range(count)]
    y = height / 2
    cr.set_line_width(2)
    for i in range(count - 1):
        cr.set_source_rgba(*(lit if large and i < current else dim))
        cr.move_to(xs[i], y)
        cr.line_to(xs[i + 1], y)
        cr.stroke()
    for i, (code, _label) in enumerate(texts.STAGES):
        cr.set_source_rgba(*(lit if i <= current else dim))
        radius = (7 if i == current else 5) if large else (5 if i == current else 3.5)
        if code == "approval":            # the gate: a small diamond
            r = radius + (1 if large else 0)
            cr.move_to(xs[i], y - r)
            cr.line_to(xs[i] + r, y)
            cr.line_to(xs[i], y + r)
            cr.line_to(xs[i] - r, y)
            cr.close_path()
        else:
            cr.arc(xs[i], y, radius, 0, 2 * math.pi)
        cr.fill()


def stage_rail(stage, colors, compact: bool = False) -> Gtk.Widget:
    """The card's rail: six stops, the current one lit, Approval as a gate; its text names the stage."""
    current = texts.stage_index(stage)
    box = Gtk.Box(spacing=ai_core.GAP_INLINE)
    area = Gtk.DrawingArea(content_width=90 if compact else 132, content_height=14, valign=Gtk.Align.CENTER)
    area.set_draw_func(lambda _a, cr, w, h: _draw_stops(cr, w, h, current, colors(), False))
    box.append(area)
    box.append(label(texts.stage_label(stage), ("am-project-small",)))
    set_accessible(box, _("Stage") + ": " + texts.stage_label(stage))
    return box


def large_stage_rail(project: dict, colors, compact: bool = False) -> Gtk.Widget:
    """The project page's rail (task 089): the six stops with their names, the current one lit; the
    activity and the second line beside it. Compact: only the current stage's name."""
    current = texts.stage_index(project.get("stage"))
    activity = texts.activity(project.get("activity"))
    second = texts.second_line(project.get("second"))
    name = texts.stage_label(project.get("stage"))
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
    if compact:
        row = Gtk.Box(spacing=ai_core.GAP_INLINE)
        row.append(icon("view-continuous-symbolic"))
        row.append(label(" · ".join(p for p in (name, activity) if p), wrap=True))
        box.append(row)
        if second:
            box.append(label(second, ("am-project-small",), wrap=True))
    else:
        area = Gtk.DrawingArea(content_height=18, hexpand=True)
        area.set_draw_func(lambda _a, cr, w, h: _draw_stops(cr, w, h, current, colors(), True))
        box.append(area)
        names = Gtk.Box(homogeneous=True)
        for i, (_code, stage) in enumerate(texts.STAGES):
            stop = label(_(stage), ("am-project-small",) if i == current else ("am-project-small", "am-project-dim"),
                         xalign=0.5)
            if i == current:
                stop.add_css_class("heading")
            names.append(stop)
        box.append(names)
        box.append(label(" · ".join(p for p in (activity, second) if p), ("am-project-small",), wrap=True))
    set_accessible(box, ", ".join(p for p in ((_("Stage") + ": " + name) if name else "", activity, second) if p))
    return box


# --- team ------------------------------------------------------------------------------------------

def _state_class(widget, state):
    if state in ("active", "stopping", "stopped", "next"):
        widget.add_css_class(f"am-node-{state}")


def team_strip(project: dict, limit: int = 4) -> Gtk.Widget:
    """The card's team: up to `limit` nodes (initials, name, model or Agent default), the marked one's
    ring and symbol, a problem mark, "+N" for the rest."""
    shown, more = texts.team_nodes(project, limit)
    row = Gtk.Box(spacing=ai_core.GAP_TIGHT, homogeneous=False)
    if not shown:
        row.set_size_request(-1, 40)
        return row
    for member, state in shown:
        name = str(member.get("name") or member.get("agent") or "")
        config = texts.member_config(member)
        planning = member.get("phase") == "planning"
        role = texts.role(member.get("role"))
        phase = None if state is None or member.get("role") == "setup" else \
            (_("Planning") if planning else _("Execution"))
        node = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1, hexpand=True)
        ring = Gtk.Overlay(halign=Gtk.Align.CENTER)
        circle = label(texts.initials(name), ("am-project-node",), xalign=0.5)
        _state_class(circle, state)
        ring.set_child(circle)
        if texts.state_icon(state):
            mark = icon(texts.state_icon(state), 12)
            mark.set_halign(Gtk.Align.END)
            mark.set_valign(Gtk.Align.END)
            ring.add_overlay(mark)
        if member.get("issues"):
            problem = icon("dialog-error-symbolic", 12)
            problem.add_css_class("am-project-error")
            problem.set_halign(Gtk.Align.END)
            problem.set_valign(Gtk.Align.START)
            ring.add_overlay(problem)
        node.append(ring)
        node.append(label(name, ("am-project-small",), xalign=0.5))
        node.append(label(config, ("am-project-small", "am-project-dim"), xalign=0.5))
        node.set_tooltip_text(" · ".join(p for p in (phase, role, config, texts.state_text(state)) if p))
        set_accessible(node, ", ".join(p for p in (name, phase, role, config, texts.state_text(state),
                                                   _("has a problem") if member.get("issues") else None) if p))
        row.append(node)
    if more > 0:
        more_label = label(f"+{more}", xalign=0.5)
        more_label.set_valign(Gtk.Align.START)
        row.append(more_label)
    return row


def team_graph(project: dict, show_on_map=None) -> Gtk.Widget:
    """The Team tab's graph (task 089): Planning and Execution rows in launch order; each member a box
    with a line to its explicitly chosen provider or runtime, else an "Agent default" box; the marked
    member as on the card; read only. Show on map beside each agent (task 091) when the shell links."""
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP_TIGHT)
    active = project.get("active") if isinstance(project.get("active"), dict) else {}
    for phase, title in (("planning", _("Planning")), ("execution", _("Execution"))):
        box.append(label(title, ("heading",)))
        members = [m for m in project.get(phase) or [] if isinstance(m, dict)]
        if not members:
            box.append(label(_("No agents in this phase yet."), ("am-project-small", "am-project-dim"), wrap=True))
            continue
        marked = active.get("position") if active.get("phase") == phase else None
        flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=2, min_children_per_line=1,
                           column_spacing=ai_core.GAP_TIGHT, row_spacing=ai_core.GAP_TIGHT)
        for member in members:
            state = active.get("state") if type(marked) is int and member.get("position") == marked else None
            child = Gtk.FlowBoxChild(focusable=False)
            child.set_child(_member_box(member, state, show_on_map))
            flow.append(child)
        box.append(flow)
    return box


def _member_box(member: dict, state, show_on_map) -> Gtk.Widget:
    name = str(member.get("name") or member.get("agent") or "")
    role = texts.role(member.get("role"))
    config, explicit = texts.graph_config(member)
    problems = " · ".join(texts.issue(i.get("code")) for i in member.get("issues") or [] if isinstance(i, dict))
    phase = _("Planning") if member.get("phase") == "planning" else _("Execution")
    row = Gtk.Box(spacing=0)
    card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
    card.add_css_class("am-project-member")
    _state_class(card, state)
    head = Gtk.Box(spacing=ai_core.GAP_TIGHT)
    head.append(label(texts.initials(name), ("am-project-node",), xalign=0.5))
    head.append(label(name))
    card.append(head)
    card.append(label(role, ("am-project-small", "am-project-dim")))
    if texts.state_text(state):
        line = Gtk.Box(spacing=4)
        line.append(icon(texts.state_icon(state), 12))
        line.append(label(texts.state_text(state), ("am-project-small",)))
        card.append(line)
    if problems:
        card.append(label(problems, ("am-project-small", "am-project-error"), wrap=True))
    row.append(card)
    link = Gtk.Box(width_request=20, valign=Gtk.Align.CENTER)
    link.add_css_class("am-project-link")
    if explicit:
        link.add_css_class("am-project-explicit")
    row.append(link)
    target = label(config, ("am-project-small", "am-project-target"), lines=3)
    target.set_wrap(True)
    target.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
    target.set_valign(Gtk.Align.CENTER)
    target.set_max_width_chars(24)
    target.set_width_chars(min(len(config), 12))   # never squeezed into one letter per line
    if explicit:
        target.add_css_class("am-project-explicit")
    row.append(target)
    set_accessible(row, ", ".join(p for p in (phase, role, name, config, texts.state_text(state), problems) if p))
    agent = member.get("agent")
    if show_on_map is not None and isinstance(agent, str) and agent:
        tip = _("Show {0} on map").format(name)
        button = Gtk.Button(child=icon("find-location-symbolic"), valign=Gtk.Align.CENTER, tooltip_text=tip)
        button.add_css_class("flat")
        set_accessible(button, tip)
        button.connect("clicked", lambda *_a: show_on_map(agent))
        row.append(button)
    return row


# --- marks and badges ------------------------------------------------------------------------------

def importance_mark(code: str, on_change=None) -> Gtk.Widget:
    """A card's importance (task 095): High stands out, Medium is outlined, Low plain; with on_change
    a click chooses another (a menu of the three)."""
    entry = texts.importance_of(code)
    if entry is None:
        return Gtk.Box()
    content = Gtk.Box(spacing=4)
    content.append(icon(entry[2], 12))
    content.append(label(_(entry[1]), ("am-project-small",)))
    tip = _("Importance")
    accessible = f"{tip}: {_(entry[1])}"
    if on_change is None:
        holder = Gtk.Box(valign=Gtk.Align.START)
        holder.append(content)
        holder.add_css_class("am-project-mark")
        holder.add_css_class(f"am-mark-{entry[0]}")
        holder.set_tooltip_text(tip)
        set_accessible(holder, accessible)
        return holder
    button = Gtk.MenuButton(valign=Gtk.Align.START, tooltip_text=tip)
    button.set_child(content)
    for css in ("flat", "am-project-mark", f"am-mark-{entry[0]}"):
        button.add_css_class(css)
    set_accessible(button, accessible)
    popover = Gtk.Popover()
    menu = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
    group = None
    for value, name, _icon in texts.IMPORTANCES:
        choice = Gtk.CheckButton(label=_(name), active=value == entry[0])
        if group is None:
            group = choice
        else:
            choice.set_group(group)

        def chosen(widget, value=value):
            if widget.get_active():
                popover.popdown()
                if value != entry[0]:
                    on_change(value)
        choice.connect("toggled", chosen)
        menu.append(choice)
    popover.set_child(menu)
    button.set_popover(popover)
    return button


def ability_hint(profile: dict | None, access: str, allow_push: bool = False) -> Gtk.Widget:
    """The shield beside a team member's agent (Maprisma's AgentAbilityHint): what the chosen agent can
    and cannot do here (at Edit + commands with the project's Allow push), as its tooltip on hover and
    in a popover on click, Enter or Space; a screen reader gets its name and the whole hint."""
    message = texts.abilities_text(profile, access, allow_push)
    button = Gtk.MenuButton(valign=Gtk.Align.CENTER, tooltip_text=message)
    button.set_child(icon("security-high-symbolic"))
    button.add_css_class("flat")
    button.agent_hint = message                 # for the build test
    set_accessible(button, _(texts.ABILITIES_NAME))
    button.update_property([Gtk.AccessibleProperty.DESCRIPTION], [message])
    popover = Gtk.Popover()
    text = label(message, wrap=True)
    text.set_max_width_chars(52)
    text.set_margin_start(6)
    text.set_margin_end(6)
    text.set_margin_top(4)
    text.set_margin_bottom(4)
    popover.set_child(text)
    button.set_popover(popover)
    return button


def warn_line(text: str, size: int = 14, title: bool = False) -> Gtk.Widget:
    """An amber warning line (task 104): a symbol and its text."""
    row = Gtk.Box(spacing=ai_core.GAP_TIGHT)
    mark = icon("dialog-warning-symbolic", size)
    mark.add_css_class("am-project-warn")
    row.append(mark)
    text_label = label(text, ("am-project-warn",) if title else ("am-project-small", "am-project-warn"), wrap=True)
    text_label.set_hexpand(True)
    if title:
        text_label.add_css_class("heading")
    row.append(text_label)
    row.warn_text = text                        # for the build test
    return row


def access_gaps_box(project: dict, use_label, on_use, on_leave) -> Gtk.Widget | None:
    """The amber box above the project's tabs (Maprisma's ProjectAccessGaps, task 104): the planned
    steps this team can't do with its access, a button that changes the access (named for what it
    really changes; none when nothing would), and Leave these to me (approve without them).
    on_use / on_leave None: the button is shown but not available now."""
    gaps = texts.gaps_of(project)
    if not gaps:
        return None
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
    box.add_css_class("am-project-gaps")
    box.set_name("access-gaps")
    box.append(warn_line(texts.gaps_title(gaps), 18, title=True))
    for line in texts.gap_lines(gaps):
        box.append(label(line, ("am-project-small",), wrap=True))
    if project.get("needs_source") == "keywords":
        box.append(label(_(texts.GUESSED_NEEDS), ("am-project-small", "am-project-dim"), wrap=True))
    buttons = Gtk.Box(spacing=ai_core.GAP_TIGHT)
    buttons.set_margin_top(4)
    if use_label:
        use = Gtk.Button(label=use_label)
        use.set_name("access-gaps-use")
        use.set_sensitive(on_use is not None)
        if on_use is not None:
            use.connect("clicked", lambda *_a: on_use())
        buttons.append(use)
    leave = Gtk.Button(label=_(texts.LEAVE_TO_ME))
    leave.set_name("access-gaps-leave")
    leave.add_css_class("flat")
    leave.set_sensitive(on_leave is not None)
    if on_leave is not None:
        leave.connect("clicked", lambda *_a: on_leave())
    buttons.append(leave)
    box.append(buttons)
    box.gap_buttons = [b.get_label() for b in (buttons.get_first_child(), buttons.get_last_child())
                       if b is not None]
    return box


def steps_left_box(project: dict, agent_name=None) -> Gtk.Widget | None:
    """The steps a completed run left to the user (Maprisma's ProjectStepsLeft, task 104)."""
    lines = texts.steps_left(project, agent_name)
    if not texts.is_partial(project):
        return None
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
    box.set_name("steps-left")
    box.append(warn_line(_(texts.PARTIAL), 18, title=True))
    for line in lines:
        box.append(label(line, ("am-project-small",), wrap=True))
    box.steps = lines
    return box


def messages_badge(count: int) -> Gtk.Widget:
    """The card's count of waiting messages (task 096)."""
    badge = Gtk.Box(spacing=3, valign=Gtk.Align.CENTER)
    badge.add_css_class("am-project-badge")
    badge.append(icon("mail-unread-symbolic", 12))
    badge.append(label(str(count), ("am-project-small",)))
    text = texts.messages_waiting(count)
    badge.set_tooltip_text(text)
    set_accessible(badge, text)
    return badge


def issues_list(project: dict, on_fix, map_fix: bool) -> Gtk.Widget | None:
    """The open project's issues (task 092): what stops it, each with its link."""
    found = texts.collect_issues(project)
    if not found:
        return None
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
    for item in found:
        row = Gtk.Box(spacing=ai_core.GAP_TIGHT)
        mark = icon("dialog-error-symbolic", 14)
        mark.add_css_class("am-project-error")
        row.append(mark)
        row.append(label(texts.issue(item.get("code")), ("am-project-small", "am-project-error"), wrap=True))
        link = Gtk.Button(label=texts.issue_fix_label(texts.issue_fix(item, map_fix)), valign=Gtk.Align.CENTER)
        link.add_css_class("flat")
        link.add_css_class("am-small-btn")
        link.connect("clicked", lambda *_a, item=item: on_fix(item))
        row.append(link)
        box.append(row)
    return box
