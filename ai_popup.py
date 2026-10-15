#!/usr/bin/env python3
"""AI Agents Map popup: a Wayland layer-shell card that shows the AI setup of
this computer: agents, local LLMs and their models, MCP servers and skills.
Two views (the overview as cards, and a Map drawn at full size) and a
settings page.

A singleton process woken by SIGUSR1 from ai-agents-map-toggle (as Theme
Manager and App Manager). The card always fills the screen: cards on the
left, the map beside them; it is neither moved nor resized.
"""

import colorsys
import re
import os
import datetime as dt
import signal
import sys
import threading

# GTK4 layer shell must load before GTK's Wayland client library. The Python
# bindings load GTK first, so preload the layer-shell library on re-exec.
layer_shell_library = "libgtk4-layer-shell.so.0"
if layer_shell_library not in os.environ.get("LD_PRELOAD", "").split(":"):
    os.environ["LD_PRELOAD"] = ":".join(filter(None, (
        layer_shell_library, os.environ.get("LD_PRELOAD", ""))))
    os.execv(sys.executable, [sys.executable, *sys.argv])

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gtk4LayerShell", "1.0")
from gi.repository import Gdk, Gio, GLib, Gtk, Gtk4LayerShell, Pango

# Once Gtk4LayerShell is loaded into this process by ld.so, remove it from
# LD_PRELOAD so child processes do not inherit it.
_preloads = [p for p in os.environ.get("LD_PRELOAD", "").split(":")
             if p and p != layer_shell_library]
if _preloads:
    os.environ["LD_PRELOAD"] = ":".join(_preloads)
else:
    os.environ.pop("LD_PRELOAD", None)

import atexit

import agent_details
import agents
import ai_core
import connect
import runtime_ctl
import install_ctl
import subprocess
from pathlib import Path
import graph
import inventory
import i18n
import pidfile
from i18n import _, ngettext
from layer_watch import LayerWatch
from map_view import MapView
from palette import ThemePalette

# Smallest card (a small monitor); normally the card fills the screen.
# Default outer size as in the other popups: a 420 px card around a 590 px
# page, the same on every page so switching never resizes the popup.
CARD_WIDTH = 420
LIST_HEIGHT = 590
SCREEN_MARGIN = 120    # space kept free above and below the card
CURSOR_SLACK = 24      # px the pointer may move after opening before the keyboard is released
AGENT_SPACING = 8
SPLIT_WIDTH = 1000        # from this card width the overview shows the map beside the cards
LIST_PANE_WIDTH = 440
DETAIL_LABEL_WIDTH = 90   # the dim labels in an opened agent card


def write_pid_file():
    try:
        pidfile.write()
    except OSError:
        pass


def cleanup_pid():
    try:
        pidfile.remove_if_own()
    except OSError:
        pass


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


def size_text(n: int) -> str:
    """2490000000 -> "2.5 GB"."""
    for size, unit in ((1e12, "TB"), (1e9, "GB"), (1e6, "MB")):
        if n >= size:
            return f"{n / size:.1f} {unit}"
    return f"{max(0, n) // 1000} kB"


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


class AgentsMap(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="org.omarchy.aiagentsmap")
        self.window = None
        self.theme = ThemePalette()
        self.style_provider = None
        # Omarchy's menus (Super + Space, …) open underneath this popup: close for them.
        self.layer_watch = LayerWatch("ai-agents-map", self.on_menu_opened)
        self.card_w, self.list_h = CARD_WIDTH, LIST_HEIGHT   # fit() makes it fill the screen
        self.scrollers = []        # every page's scroller follows the page height
        self.view = ai_core.last_view() if ai_core.start_view() == "last" else ai_core.start_view()
        self.settings_ready = False
        self.expanded = set()      # commands of the agent cards shown with their details
        self.refreshing = set()    # commands whose usage Omarchy is collecting right now
        self.detail_boxes = {}     # card key -> (details box, fill function)
        self.agent_list = []
        self.inventory = inventory.Inventory()
        self.selection = []        # node ids selected in the maps and (wide overview) the cards
        self.card_widgets = {}     # card key -> (card box, check mark label)
        self.split = False         # wide overview: cards left, map right

    def do_shutdown(self):
        cleanup_pid()
        Gtk.Application.do_shutdown(self)

    def do_activate(self):
        if self.window is None:
            self.build_window()
            self.hold()  # Keep the singleton alive while its popup is hidden.
            signal.signal(signal.SIGUSR1, lambda *_: GLib.idle_add(self.toggle))
            for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
                signal.signal(sig, lambda *_: sys.exit(0))
            GLib.timeout_add(100, lambda: True)  # Let Python dispatch SIGUSR1.
            self.watch_theme()
            write_pid_file()
        self.open()

    # --- window ----------------------------------------------------------------

    def build_window(self):
        settings = Gtk.Settings.get_default()
        if settings is not None:
            settings.set_property("gtk-enable-primary-paste", False)
        window = Gtk.ApplicationWindow(application=self)
        window.set_decorated(False)
        window.set_title("AI Agents Map")
        window.set_name("aiagentsmap-window")
        Gtk4LayerShell.init_for_window(window)
        Gtk4LayerShell.set_namespace(window, "ai-agents-map")
        Gtk4LayerShell.set_layer(window, Gtk4LayerShell.Layer.OVERLAY)
        Gtk4LayerShell.set_keyboard_mode(window, Gtk4LayerShell.KeyboardMode.ON_DEMAND)
        # Full screen and transparent like App Manager: a click beside the card
        # closes it, and so does focus moving elsewhere. The card itself sits
        # on top, centred or where it was moved to.
        for edge in (Gtk4LayerShell.Edge.TOP, Gtk4LayerShell.Edge.BOTTOM,
                     Gtk4LayerShell.Edge.LEFT, Gtk4LayerShell.Edge.RIGHT):
            Gtk4LayerShell.set_anchor(window, edge, True)
        Gtk4LayerShell.set_exclusive_zone(window, 0)
        self.window = window

        overlay = Gtk.Overlay()
        overlay.set_hexpand(True)
        overlay.set_vexpand(True)
        overlay.set_child(Gtk.Box(hexpand=True, vexpand=True))
        window.set_child(overlay)
        self.overlay = overlay
        click = Gtk.GestureClick()
        click.connect("released", self.on_overlay_click)  # on release, so the bar never sees half a click
        overlay.add_controller(click)
        window.connect("notify::is-active", self.on_active_changed)

        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        card.add_css_class("am-card")
        frame = Gtk.Overlay()
        frame.set_child(card)
        frame.set_halign(Gtk.Align.CENTER)
        frame.set_valign(Gtk.Align.CENTER)
        overlay.add_overlay(frame)
        frame.add_overlay(self.build_undo_bar())
        self.card = card
        self.card_frame = frame
        self.list_h = max(ai_core.MIN_WINDOW_SIZE[1], min(self.list_h, monitor_height() - 2 * SCREEN_MARGIN))

        card.append(self.build_header())

        stack = Gtk.Stack()
        stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
        stack.set_transition_duration(140)
        stack.set_interpolate_size(False)
        stack.set_hhomogeneous(True)
        stack.set_vhomogeneous(True)
        stack.add_named(self.build_main_page(), "main")
        stack.add_named(self.build_map_page(), "map")
        stack.add_named(self.build_settings_page(), "settings")
        stack.add_named(self.build_connect_page(), "connect")
        stack.add_named(self.build_install_page(), "install")
        stack.connect("notify::visible-child-name", lambda *_: self.update_header())
        card.append(stack)
        self.stack = stack

        # Capture phase: a focused control must not see Esc/Backspace first.
        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self.on_key)
        keys.connect("key-released", self.on_key_released)
        window.add_controller(keys)

        self.fit()
        self.apply_theme_css()
        start, self.view = self.view, "main"
        self.show_view(start, save=False)

    def build_header(self) -> Gtk.Widget:
        header = Gtk.Box(spacing=8)
        self.header = header
        title = Gtk.Label(label="AI Agents Map", xalign=0)
        title.add_css_class("am-title")
        title.set_ellipsize(Pango.EllipsizeMode.END)
        title.set_max_width_chars(30)
        title.set_hexpand(True)
        header.append(title)
        self.title_label = title
        # The two views, as a pair of flat toggle buttons.
        self.view_buttons = {}
        views = Gtk.Box(spacing=0)
        views.add_css_class("am-view-switch")
        for name, icon, tip in (("main", "view-list-symbolic", _("Overview (Ctrl+1)")),
                                ("map", "network-workgroup-symbolic", _("Map (Ctrl+2)"))):
            button = Gtk.ToggleButton(icon_name=icon)
            button.set_tooltip_text(tip)
            button.add_css_class("flat")
            button.add_css_class("am-header-btn")
            button.add_css_class("am-view-btn")
            button.connect("clicked", lambda b, n=name: self.on_view_button(b, n))
            views.append(button)
            self.view_buttons[name] = button
        header.append(views)
        self.view_switch = views

        # The same Nerd Font gear (U+F0493) in a small bordered box as the other plugins.
        settings_button = Gtk.Button()
        gear = Gtk.Label(label="\U000F0493")
        gear.add_css_class("am-gear")
        settings_button.set_child(gear)
        settings_button.set_tooltip_text(_("Settings"))
        settings_button.add_css_class("flat")
        settings_button.add_css_class("am-header-btn")
        settings_button.add_css_class("am-gear-btn")
        settings_button.connect("clicked", lambda *_: self.open_settings())
        header.append(settings_button)   # rightmost
        self.settings_button = settings_button
        return header

    def page_scroller(self, child: Gtk.Widget) -> Gtk.ScrolledWindow:
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_child(child)
        self.scrollers.append(scroller)
        return scroller

    def empty_state(self, icon: str, title: str, hint: str) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_valign(Gtk.Align.CENTER)
        box.set_vexpand(True)
        image = Gtk.Image.new_from_icon_name(icon)
        image.set_pixel_size(48)
        image.add_css_class("am-empty-icon")
        box.append(image)
        heading = Gtk.Label(label=title, wrap=True, justify=Gtk.Justification.CENTER, max_width_chars=40)
        heading.add_css_class("am-empty-title")
        box.append(heading)
        text = Gtk.Label(label=hint, wrap=True, justify=Gtk.Justification.CENTER, max_width_chars=40)
        text.add_css_class("am-hint")
        box.append(text)
        return box

    def build_main_page(self) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        page.append(self.view_heading(_("AI setup")))
        body = Gtk.Box(spacing=16)
        self.agents_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=AGENT_SPACING)
        self.agents_box.add_css_class("am-agents")
        self.list_pane = self.page_scroller(self.agents_box)
        self.list_pane.set_hexpand(True)
        body.append(self.list_pane)
        # Wide: the map beside the cards; a click on a card selects it there.
        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, hexpand=True)
        self.side_filter = Gtk.Label(xalign=0)
        self.side_filter.add_css_class("am-agent-detail")
        self.side_filter.set_ellipsize(Pango.EllipsizeMode.END)
        self.side_filter.set_max_width_chars(1)
        side.append(self.side_filter)
        self.side_map = MapView()
        self.side_map.on_selection = self.on_map_selection
        self.side_map.on_link = self.on_map_link
        self.side_map.on_relink = self.on_map_relink
        self.side_map.on_context = lambda node_id, rect, v=self.side_map: self.show_context(v, node_id, rect)
        self.side_map.on_header = lambda column, rect: self.show_provider_list(self.side_map, rect)
        side.append(self.page_scroller(self.side_map))
        side.set_visible(False)
        self.side_pane = side
        body.append(side)
        page.append(body)
        return page

    def collect(self):
        """Everything both views show (file system and /proc only, ~0.1 s)."""
        self.agent_list = agents.list_agents()
        self.inventory = inventory.collect(self.agent_list)

    def refresh_overview(self):
        """Rebuild the main view: agents, local LLMs, MCP servers, skills."""
        box = self.agents_box
        while (child := box.get_first_child()) is not None:
            box.remove(child)
        self.detail_boxes = {}
        self.card_widgets = {}
        installed = [a for a in self.agent_list if a.installed]
        missing = [a for a in self.agent_list if not a.installed]
        inv = self.inventory
        names = self.agent_names()

        self.section(_("Agents ({0})").format(len(installed)))
        for agent in installed:
            box.append(self.agent_card(agent))

        installed = [rt for rt in inv.runtimes if rt.installed]
        self.section(_("Local LLMs ({0})").format(len(installed)))
        # Installed ones first; the others too, grey, as something to install and connect.
        for rt in sorted(inv.runtimes, key=lambda r: not r.installed):
            status = graph.runtime_status(rt)
            state = {"running": _("running"), "stopped": _("installed, not running")}.get(status, _("not installed"))
            sub = state if not rt.installed else \
                " · ".join([state, ngettext("1 model", "{0} models", len(rt.models)).format(len(rt.models))])
            box.append(self.expandable_card(f"runtime:{rt.key}", "\U000F061A", "", rt.name, sub,
                                            lambda b, rt=rt: self.fill_runtime(b, rt, names),
                                            dim=not rt.installed, status=status))

        self.section(_("MCP servers ({0})").format(len(inv.mcp)))
        if not inv.mcp:
            self.section_hint(_("No agent has an MCP server set up."))
        for server in inv.mcp:
            users = ", ".join(names.get(a, a) for a in server.agents)
            box.append(self.expandable_card(f"mcp:{server.name}", "\U000F0337", "", server.name, users,
                                 lambda b, s=server: self.fill_mcp(b, s, names)))

        self.section(_("Skills ({0})").format(sum(len(s.skills) for s in inv.skill_sets)))
        if not inv.skill_sets:
            self.section_hint(_("No skills found for the installed agents."))
        for skill_set in inv.skill_sets:
            sub = ngettext("1 skill", "{0} skills", len(skill_set.skills)).format(len(skill_set.skills)) \
                + " · " + ", ".join(names.get(a, a) for a in skill_set.agents)
            box.append(self.expandable_card(f"skills:{skill_set.path}", "\U000F0EB3", "", skill_set.path, sub,
                                 lambda b, s=skill_set: self.fill_skills(b, s, names)))

        if missing:
            self.section(_("Not installed ({0})").format(len(missing)))
            for agent in missing:
                box.append(self.agent_card(agent))

    def agent_names(self) -> dict:
        names = {a.command: a.name for a in self.agent_list}
        names["lmstudio"] = "LM Studio"
        return names

    def section(self, title: str):
        heading = Gtk.Label(label=title.upper(), xalign=0)
        heading.add_css_class("am-group-heading")
        self.agents_box.append(heading)

    def section_hint(self, text: str):
        hint = Gtk.Label(label=text, xalign=0, wrap=True, max_width_chars=1)
        hint.add_css_class("am-agent-detail")
        self.agents_box.append(hint)

    def expandable_card(self, key: str, icon: str, icon_font: str, title: str, subtitle: str, fill,
             dim: bool = False, badge: str = "", badge_tip: str = "", highlight: bool = False,
             status: str = "") -> Gtk.Widget:
        """A card with an icon, a title and a dim line; a click shows or hides
        what fill(box) puts into it."""
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        card.add_css_class("am-agent")
        if dim:
            card.add_css_class("am-agent-missing")
        if highlight:
            card.add_css_class("am-agent-default")
        top = Gtk.Box(spacing=10)
        top.set_cursor_from_name("pointer")
        glyph = Gtk.Label(label=icon)
        glyph.add_css_class("am-agent-icon")
        if status:
            glyph.add_css_class(f"am-status-{status}")   # green / yellow / grey, from the theme
        if icon_font == "omarchy":
            glyph.add_css_class("am-agent-icon-omarchy")
        glyph.set_valign(Gtk.Align.CENTER)
        top.append(glyph)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        text.set_hexpand(True)
        text.set_valign(Gtk.Align.CENTER)
        name = Gtk.Label(label=title, xalign=0)
        name.add_css_class("am-agent-name")
        name.set_ellipsize(Pango.EllipsizeMode.END)
        name.set_max_width_chars(1)
        name.set_hexpand(True)
        text.append(name)
        line = Gtk.Box(spacing=6)
        if badge:
            mark = Gtk.Label(label=badge)
            mark.add_css_class("am-agent-badge")
            if badge_tip:
                mark.set_tooltip_text(badge_tip)
            line.append(mark)
        detail = Gtk.Label(label=subtitle, xalign=0)
        detail.add_css_class("am-agent-detail")
        detail.set_ellipsize(Pango.EllipsizeMode.END)
        detail.set_max_width_chars(1)
        detail.set_hexpand(True)
        line.append(detail)
        text.append(line)
        top.append(text)
        check = Gtk.Label(label="✓")
        check.add_css_class("am-card-check")
        check.set_valign(Gtk.Align.CENTER)
        check.set_visible(key in self.selection)
        top.append(check)
        if key in self.selection:
            card.add_css_class("am-agent-selected")
        self.card_widgets[key] = (card, check)
        expanded = key in self.expanded
        chevron = Gtk.Image.new_from_icon_name("pan-up-symbolic" if expanded else "pan-down-symbolic")
        chevron.add_css_class("am-agent-chevron")
        chevron.set_valign(Gtk.Align.CENTER)
        top.append(chevron)
        card.append(top)

        revealer = Gtk.Revealer()
        revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        revealer.set_transition_duration(150)
        details = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        details.add_css_class("am-agent-details")
        revealer.set_child(details)
        card.append(revealer)
        self.detail_boxes[key] = (details, fill)
        if expanded:
            self.fill_card(key)
            revealer.set_reveal_child(True)

        click = Gtk.GestureClick()
        click.connect("released", lambda g, *_: self.on_card_click(g, key, revealer, chevron))
        top.add_controller(click)
        # Wide view: a click inside the opened details selects the card too.
        body_click = Gtk.GestureClick()
        body_click.connect("released", lambda g, *_: self.on_card_body_click(g, key))
        details.add_controller(body_click)
        return card

    def on_card_click(self, gesture, key: str, revealer, chevron):
        """Narrow: open or close the details. Wide (map beside the cards): a
        click selects the card in the map and opens it; Shift+click adds or
        removes it; clicking the only selected card again lets it go."""
        selectable = self.split and key in self.map_node_ids()
        if not selectable:
            self.toggle_card(key, revealer, chevron)
            return
        if gesture.get_current_event_state() & Gdk.ModifierType.SHIFT_MASK:
            self.set_selection([k for k in self.selection if k != key] if key in self.selection
                               else self.selection + [key])
            return
        if self.selection == [key]:
            self.set_selection([])
            if key in self.expanded:
                self.toggle_card(key, revealer, chevron)
            return
        self.set_selection([key])
        if key not in self.expanded:
            self.toggle_card(key, revealer, chevron)

    def on_card_body_click(self, gesture, key: str):
        """Inside the details: select (Shift: add or remove), never close them."""
        if not self.split or key not in self.map_node_ids():
            return
        if gesture.get_current_event_state() & Gdk.ModifierType.SHIFT_MASK:
            self.set_selection([k for k in self.selection if k != key] if key in self.selection
                               else self.selection + [key])
        elif self.selection != [key]:
            self.set_selection([key])

    def map_node_ids(self) -> set:
        graph_now = self.map_view.graph
        return {n.id for n in graph_now.nodes} if graph_now is not None else set()

    def set_selection(self, ids):
        """One selection for both maps and the cards."""
        self.selection = list(dict.fromkeys(ids))
        for view in (self.map_view, self.side_map):
            view.set_selected(self.selection)
        for key, (card, check) in self.card_widgets.items():
            on = key in self.selection
            check.set_visible(on)
            if on:
                card.add_css_class("am-agent-selected")
            else:
                card.remove_css_class("am-agent-selected")
        self.update_selection_labels()

    def toggle_card(self, key: str, revealer, chevron):
        if key in self.expanded:
            self.expanded.discard(key)
            revealer.set_reveal_child(False)
            chevron.set_from_icon_name("pan-down-symbolic")
            return
        self.expanded.add(key)
        self.fill_card(key)
        revealer.set_reveal_child(True)
        chevron.set_from_icon_name("pan-up-symbolic")
        self.toggle_card_hook(key)

    def fill_card(self, key: str):
        entry = self.detail_boxes.get(key)
        if entry is None:
            return
        box, fill = entry
        while (child := box.get_first_child()) is not None:
            box.remove(child)
        fill(box)

    def detail_grid(self, box, rows):
        """rows: (label, value, tooltip); empty values are left out."""
        grid = Gtk.Grid(column_spacing=10, row_spacing=4)
        index = 0
        for label, value, tip in rows:
            if not value:
                continue
            name = Gtk.Label(label=label, xalign=0, yalign=0)
            name.add_css_class("am-line-label")
            name.set_size_request(DETAIL_LABEL_WIDTH, -1)
            text = Gtk.Label(label=value, xalign=0, wrap=True, max_width_chars=1, hexpand=True)
            text.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
            text.add_css_class("am-detail-value")
            if tip:
                name.set_tooltip_text(tip)
                text.set_tooltip_text(tip)
            grid.attach(name, 0, index, 1, 1)
            grid.attach(text, 1, index, 1, 1)
            index += 1
        box.append(grid)

    def note(self, box, text: str):
        label = Gtk.Label(label=text, xalign=0, wrap=True, max_width_chars=1)
        label.add_css_class("am-agent-detail")
        box.append(label)

    # --- agent cards -------------------------------------------------------------------

    def agent_card(self, agent) -> Gtk.Widget:
        key = f"agent:{agent.command}"
        subtitle = (agent.version or _("Installed")) if agent.installed else _("Not installed")
        card = self.expandable_card(key, agent.icon, agent.icon_font, agent.name, subtitle,
                         lambda b, a=agent: self.fill_agent(b, a), dim=not agent.installed,
                         badge=_("Default") if agent.default else "", badge_tip=_("Omarchy's default agent"),
                         highlight=agent.default)
        if key in self.expanded and agent.installed and agent_details.usage_is_stale(agent.command):
            self.refresh_usage(agent)
        return card

    def toggle_card_hook(self, key: str):
        """Opening an agent card asks Omarchy for fresh usage when it is old."""
        if key.startswith("agent:") and key in self.expanded:
            agent = next((a for a in self.agent_list if f"agent:{a.command}" == key), None)
            if agent is not None and agent.installed and agent_details.usage_is_stale(agent.command):
                self.refresh_usage(agent)

    def refresh_usage(self, agent):
        """Ask Omarchy for fresh usage on a thread; fill the card again after."""
        command = agent.command
        if command in self.refreshing:
            return
        self.refreshing.add(command)
        self.fill_card(f"agent:{command}")

        def work():
            agent_details.refresh_usage(command)
            GLib.idle_add(done)

        def done():
            self.refreshing.discard(command)
            self.fill_card(f"agent:{command}")
            return False

        threading.Thread(target=work, daemon=True).start()

    def fill_agent(self, box, agent):
        command = agent.command
        info = agent_details.details_for(command)
        inv = self.inventory
        rows = []
        if not agent.installed:
            rows.append((_("Status"), _("Not installed"),
                         _("Install it from Omarchy's menu: Setup → Defaults → Agent.")))
        rows.append((_("Plan"), info.plan, _("Your subscription, as Omarchy's usage data reports it.")))
        rows.append((_("Model"), info.model, _("The model set in the agent's settings.")))
        for provider in info.providers:
            parts = [provider.name]
            if provider.local:
                parts.append(_("local · {0}").format(provider.runtime) if provider.runtime else _("local"))
            if provider.models:
                parts.append(ngettext("1 model", "{0} models", provider.models).format(provider.models))
            rows.append((_("Provider"), " · ".join(parts), ""))
        servers = inv.agent_mcp.get(command, [])
        rows.append((_("MCP servers"), ", ".join(servers), ""))
        sets = [s for s in inv.skill_sets if s.path in inv.agent_skills.get(command, [])]
        if sets:
            count = sum(len(s.skills) for s in sets)
            rows.append((_("Skills"), ngettext("1 skill", "{0} skills", count).format(count) + " · "
                         + ", ".join(s.path for s in sets), ""))
        rows.append((_("Tools"), ", ".join(inv.agent_tools.get(command, [])),
                     _("The built-in tools the agent may use, as its settings list them.")))
        if info.today_prompts is not None or info.today_tokens is not None:
            today = []
            if info.today_prompts is not None:
                today.append(ngettext("1 prompt", "{0} prompts", info.today_prompts).format(info.today_prompts))
            if info.today_sessions is not None:
                today.append(ngettext("1 session", "{0} sessions", info.today_sessions).format(info.today_sessions))
            if info.today_tokens:
                today.append(_("{0} tokens").format(short_number(info.today_tokens)))
            rows.append((_("Today"), " · ".join(today), ""))
        if info.total_sessions:
            total = ngettext("1 session", "{0} sessions", info.total_sessions).format(info.total_sessions)
            if info.active_days:
                total += " · " + ngettext("1 active day", "{0} active days", info.active_days).format(info.active_days)
            rows.append((_("In total"), total, ""))
        if agent.installed:
            rows.append((_("Last used"), when(info.last_used), ""))
            source = {"mise": _("with mise"), "package": _("as a package"),
                      "own": _("your own install")}.get(agent.source, "")
            rows.append((_("Installed"), " · ".join(x for x in (source, agent.version) if x), ""))
        rows.append((_("Command"), command, ""))
        rows.append((_("Settings"), info.config_dir, ""))
        self.detail_grid(box, rows)

        if info.limits:
            heading = Gtk.Label(label=_("Limits").upper(), xalign=0)
            heading.add_css_class("am-group-heading")
            box.append(heading)
            for limit in info.limits:
                box.append(self.limit_row(limit))
        status = ""
        if command in self.refreshing:
            status = _("Updating usage…")
        elif info.usage_updated is not None:
            status = _("Usage from Omarchy, updated {0}").format(when(info.usage_updated))
        if info.usage_note and command not in self.refreshing:
            status = " · ".join(x for x in (status, info.usage_note) if x)
        if status:
            self.note(box, status)

    # --- runtime, MCP and skill cards ----------------------------------------------------

    def fill_runtime(self, box, rt, names):
        self.detail_grid(box, [
            (_("Status"), {"running": _("running"), "stopped": _("installed, not running")}
             .get(graph.runtime_status(rt), _("not installed")), ""),
            (_("Address"), rt.address, ""),
            (_("Service"), rt.service, _("The user service that starts it (systemctl --user).")),
            (_("Models folder"), rt.models_dir, ""),
            (_("Used by"), ", ".join(names.get(a, a) for a in rt.agents),
             _("Agents with a provider pointing at this runtime.")),
        ])
        if rt.models:
            heading = Gtk.Label(label=ngettext("1 model", "{0} models", len(rt.models)).format(len(rt.models)).upper(),
                                xalign=0)
            heading.add_css_class("am-group-heading")
            box.append(heading)
            self.note(box, "\n".join(rt.models))

    def fill_mcp(self, box, server, names):
        kind = {"command": _("runs a program"), "url": _("remote server")}.get(server.kind, "")
        self.detail_grid(box, [
            (_("Runs"), server.target, ""),
            (_("Type"), kind, ""),
            (_("Used by"), ", ".join(names.get(a, a) for a in server.agents), ""),
        ])

    def fill_skills(self, box, skill_set, names):
        self.detail_grid(box, [
            (_("Folder"), skill_set.path, ""),
            (_("Used by"), ", ".join(names.get(a, a) for a in skill_set.agents), ""),
        ])
        self.note(box, ", ".join(skill_set.skills))

    def limit_row(self, limit) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        top = Gtk.Box(spacing=8)
        label = Gtk.Label(label=limit.label, xalign=0, hexpand=True)
        label.set_ellipsize(Pango.EllipsizeMode.END)
        label.set_max_width_chars(1)
        label.add_css_class("am-detail-value")
        top.append(label)
        value = _("{0} %").format(round(limit.percent * 100))
        if limit.resets_at is not None and limit.resets_at > dt.datetime.now(dt.timezone.utc):
            value += " · " + _("resets {0}").format(when(limit.resets_at, future=True))
        right = Gtk.Label(label=value, xalign=1)
        right.add_css_class("am-agent-detail")
        top.append(right)
        box.append(top)
        bar = Gtk.LevelBar(min_value=0, max_value=1, value=limit.percent)
        bar.add_css_class("am-limit")
        for name in ("low", "high", "full"):
            bar.remove_offset_value(name)
        bar.add_offset_value("am-ok", 0.5)
        bar.add_offset_value("am-warn", 0.75)
        bar.add_offset_value("am-hot", 0.9)
        bar.add_offset_value("am-full", 1.0)
        box.append(bar)
        return box

    def build_map_page(self) -> Gtk.Widget:
        """Agents with what they use, drawn as a graph. Opening it makes the
        card as large as the screen allows; leaving it restores the size."""
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        page.append(self.view_heading(_("Map")))
        page.append(self.build_layer_toggles())
        # The filter line: a hint, or what is selected and a way back to everything.
        bar = Gtk.Box(spacing=8)
        self.map_filter = Gtk.Label(xalign=0, hexpand=True)
        self.map_filter.add_css_class("am-agent-detail")
        self.map_filter.set_ellipsize(Pango.EllipsizeMode.END)
        self.map_filter.set_max_width_chars(1)
        bar.append(self.map_filter)
        self.map_show_all = Gtk.Button(label=_("Clear selection"))
        self.map_show_all.add_css_class("flat")
        self.map_show_all.add_css_class("am-small-btn")
        self.map_show_all.set_tooltip_text(_("Clear the selection (Esc)"))
        self.map_show_all.connect("clicked", lambda *_: self.set_selection([]))
        bar.append(self.map_show_all)
        page.append(bar)
        self.map_view = MapView()
        self.map_view.set_layers(ai_core.map_layers())
        self.update_layer_toggles()
        self.map_view.on_selection = self.on_map_selection
        self.map_view.on_link = self.on_map_link
        self.map_view.on_relink = self.on_map_relink
        self.map_view.on_context = lambda node_id, rect, v=self.map_view: self.show_context(v, node_id, rect)
        self.map_view.on_header = lambda column, rect: self.show_provider_list(self.map_view, rect)
        page.append(self.page_scroller(self.map_view))
        return page

    def build_layer_toggles(self) -> Gtk.Widget:
        """One toggle per category: shorten the map to what you are setting up."""
        row = Gtk.Box(spacing=6)
        label = Gtk.Label(label=_("Show:"))
        label.add_css_class("am-line-label")
        row.append(label)
        names = {"agents": _("Agents"), "online": _("Online providers"), "local": _("Local LLMs"),
                 "models": _("Models"), "mcp": _("MCP servers"), "skills": _("Skills")}
        self.layer_buttons = {}
        for layer in graph.LAYERS:
            button = Gtk.ToggleButton(label=names[layer])
            button.add_css_class("am-chip")
            button.connect("toggled", lambda b, l=layer: self.on_layer_toggled(l, b.get_active()))
            row.append(button)
            self.layer_buttons[layer] = button
        return row

    def update_layer_toggles(self):
        """Pressed = switched on; greyed out while what it needs is off."""
        on = self.map_view.layers
        needs = {"models": _("Needs Online providers or Local LLMs"),
                 "mcp": _("Needs Agents"), "skills": _("Needs Agents")}
        self.updating_layers = True
        for layer, button in self.layer_buttons.items():
            button.set_active(layer in on)
            usable = not graph.REQUIRES.get(layer) or any(n in on for n in graph.REQUIRES[layer])
            button.set_sensitive(usable)
            button.set_tooltip_text(None if usable else needs[layer])
        self.updating_layers = False

    updating_layers = False

    def on_layer_toggled(self, layer: str, active: bool):
        if self.updating_layers:
            return
        layers = set(self.map_view.layers)
        layers.add(layer) if active else layers.discard(layer)
        if not graph.effective_layers(layers):
            self.update_layer_toggles()   # keep at least one category on
            return
        self.map_view.set_layers(layers)
        self.side_map.set_layers(layers)
        self.set_selection([i for i in self.selection if i in set(self.map_view.selected)])
        self.update_layer_toggles()
        self.save_setting(map_layers=[l for l in graph.LAYERS if l in layers])

    def show_provider_list(self, view, rect):
        """The providers column's list: tick the online and local providers to show."""
        graph_now = view.graph
        if graph_now is None:
            return
        popover = Gtk.Popover()
        popover.set_parent(view)
        area = Gdk.Rectangle()
        area.x, area.y, area.width, area.height = (int(v) for v in rect)
        popover.set_pointing_to(area)
        popover.set_position(Gtk.PositionType.BOTTOM)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.add_css_class("am-provider-list")
        hidden = set(view.hidden_providers)
        groups = ((_("Online"), [n for n in graph_now.column(2) if n.kind == "cloud"]),
                  (_("Local"), [n for n in graph_now.column(2) if n.kind == "runtime"]))
        checks = {}
        for title, nodes in groups:
            if not nodes:
                continue
            heading = Gtk.Label(label=title.upper(), xalign=0)
            heading.add_css_class("am-group-heading")
            box.append(heading)
            for node in sorted(nodes, key=lambda n: n.label.lower()):
                state = {"running": _("running"), "stopped": _("installed, not running"),
                         "missing": _("not installed")}.get(node.status, "")
                check = Gtk.CheckButton(label=f"{node.label}  ·  {state}" if state else node.label)
                check.set_active(node.id not in hidden)
                check.connect("toggled", lambda b, i=node.id: self.set_provider_shown(i, b.get_active()))
                box.append(check)
                checks[node.id] = check
        row = Gtk.Box(spacing=6, halign=Gtk.Align.END, margin_top=6)
        for label, value in ((_("All"), True), (_("None"), False)):
            button = Gtk.Button(label=label)
            button.add_css_class("am-small-btn")
            button.connect("clicked", lambda _b, v=value: [c.set_active(v) for c in checks.values()])
            row.append(button)
        box.append(row)
        popover.set_child(box)
        popover.connect("closed", lambda p: GLib.idle_add(p.unparent))
        popover.popup()

    def set_provider_shown(self, node_id: str, shown: bool):
        hidden = set(self.map_view.hidden_providers)
        hidden.discard(node_id) if shown else hidden.add(node_id)
        for view in (self.map_view, self.side_map):
            view.set_hidden_providers(hidden)
        self.set_selection([i for i in self.selection if i not in self.map_view.hidden()])
        self.save_setting(hidden_providers=sorted(hidden))

    def on_map_selection(self, nodes):
        """A click in either map."""
        self.set_selection([n.id for n in nodes])

    def update_selection_labels(self):
        by_id = {n.id: n for n in self.map_view.graph.nodes} if self.map_view.graph else {}
        nodes = [by_id[i] for i in self.selection if i in by_id]
        if nodes:
            self.side_filter.set_label(_("Selected: {0} · hold Shift and click to add or remove one")
                                       .format(", ".join(n.label for n in nodes)))
        else:
            self.side_filter.set_label(_("Click a card or a box to see only its connections; "
                                         "Shift+click selects more than one."))
        if nodes:
            self.map_filter.set_label(_("Selected: {0} · hold Shift and click to add or remove one")
                                      .format(", ".join(n.label for n in nodes)))
        else:
            self.map_filter.set_label(_("Click a box to see only its connections; Shift+click selects more than one."))
        self.map_filter.set_tooltip_text(self.map_filter.get_label())
        self.map_show_all.set_visible(bool(nodes))

    def refresh_map(self):
        details = {a.command: agent_details.details_for(a.command) for a in self.agent_list if a.installed}
        labels = {"skills": lambda n: ngettext("1 skill", "{0} skills", n).format(n),
                  "more": lambda n: _("+{0} more").format(n)}
        built = graph.build(self.agent_list, details, self.inventory, labels)
        headers = [_("MCP servers · skills"), _("Agents"), _("LLM providers"), _("Models")]
        hidden = set(ai_core.hidden_providers())
        for view in (self.map_view, self.side_map):
            view.set_graph(built, headers)
            view.set_layers(self.map_view.layers)
            view.set_hidden_providers(hidden)
        ids = {n.id for n in built.nodes}
        self.set_selection([i for i in self.selection if i in ids])

    @staticmethod
    def view_heading(text: str) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        label = Gtk.Label(label=text.upper(), xalign=0)
        label.add_css_class("am-section-title")
        box.append(label)
        rule = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        rule.add_css_class("am-settings-rule")
        box.append(rule)
        return box

    # --- views -----------------------------------------------------------------

    def on_view_button(self, button, name: str):
        if self.updating_buttons:
            return
        self.show_view(name)

    updating_buttons = False

    def show_view(self, name: str, save: bool = True):
        name = name if name in ai_core.VIEWS else "main"
        self.view = name
        self.stack.set_visible_child_name(name)
        self.update_header()
        if save and ai_core.start_view() == "last":
            try:
                ai_core.update_settings(last_view=name)
            except OSError:
                pass

    def update_header(self):
        page = self.stack.get_visible_child_name()
        on_settings = page in ("settings", "connect", "install")
        # Faded out rather than hidden: the header keeps its height, so the
        # card does not jump when the settings page opens.
        for widget in (self.settings_button, self.view_switch):
            widget.set_opacity(0 if on_settings else 1)
            widget.set_can_target(not on_settings)
            widget.set_can_focus(not on_settings)
            widget.set_sensitive(not on_settings)
        self.updating_buttons = True
        for name, button in self.view_buttons.items():
            button.set_active(name == page)
        self.updating_buttons = False

    # --- connecting (a line drawn on the map) -----------------------------------------------

    def build_connect_page(self) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        header = Gtk.Box(spacing=8)
        back = Gtk.Button()
        back.add_css_class("flat")
        back.add_css_class("am-header-btn")
        back.add_css_class("am-back-bordered")
        back_box = Gtk.Box(spacing=6)
        back_box.append(Gtk.Image.new_from_icon_name("go-previous-symbolic"))
        back_box.append(Gtk.Label(label=_("Back")))
        back_box.append(Gtk.Label(label="· ⌫"))
        back.set_child(back_box)
        back.set_tooltip_text(_("Back without changing anything (Backspace, Esc)"))
        back.connect("clicked", lambda *_: self.close_connect())
        header.append(back)
        self.connect_title = Gtk.Label(xalign=0, hexpand=True)
        self.connect_title.add_css_class("am-section-title")
        header.append(self.connect_title)
        page.append(header)
        rule = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        rule.add_css_class("am-settings-rule")
        page.append(rule)

        self.connect_intro = Gtk.Label(xalign=0, wrap=True, max_width_chars=1)
        self.connect_intro.add_css_class("am-detail-value")
        page.append(self.connect_intro)
        self.connect_form = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.connect_form.add_css_class("am-settings")
        page.append(self.connect_form)

        heading = Gtk.Label(label=_("What changes").upper(), xalign=0)
        heading.add_css_class("am-group-heading")
        self.connect_preview_heading = heading
        page.append(heading)
        self.connect_preview = Gtk.TextView(editable=False, cursor_visible=False, monospace=True)
        self.connect_preview.add_css_class("am-diff")
        self.connect_preview.set_wrap_mode(Gtk.WrapMode.CHAR)
        self.connect_preview_scroller = Gtk.ScrolledWindow(vexpand=True)
        self.connect_preview_scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.connect_preview_scroller.set_child(self.connect_preview)
        page.append(self.connect_preview_scroller)
        self.connect_status = Gtk.Label(xalign=0, wrap=True, max_width_chars=1)
        self.connect_status.add_css_class("am-agent-detail")
        page.append(self.connect_status)

        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        cancel = Gtk.Button(label=_("Cancel"))
        cancel.add_css_class("am-small-btn")
        cancel.connect("clicked", lambda *_: self.close_connect())
        buttons.append(cancel)
        self.connect_remove = Gtk.Button(label=_("Remove connection"))
        self.connect_remove.add_css_class("am-small-btn")
        self.connect_remove.add_css_class("am-danger")
        self.connect_remove.connect("clicked", lambda *_: self.apply_connect(remove=True))
        buttons.append(self.connect_remove)
        self.connect_ok = Gtk.Button(label=_("Connect"))
        self.connect_ok.add_css_class("am-small-btn")
        self.connect_ok.add_css_class("am-primary")
        self.connect_ok.connect("clicked", lambda *_: self.apply_connect(remove=False))
        buttons.append(self.connect_ok)
        page.append(buttons)
        return page

    def on_map_relink(self, agent_id: str, old_id: str, new_id: str | None):
        """A line moved to another local LLM: the old provider goes, the new one
        comes. Dropped on empty space (new_id None): the connection is removed."""
        if new_id is None:
            self.on_map_link(agent_id, old_id, remove_only=True)
        else:
            self.on_map_link(agent_id, new_id, replaces_id=old_id)

    def on_map_link(self, agent_id: str, runtime_id: str, replaces_id: str | None = None,
                    remove_only: bool = False):
        """A line drawn from an agent to a local LLM: ask how, show what changes."""
        agent = next((a for a in self.agent_list if f"agent:{a.command}" == agent_id), None)
        runtime = next((r for r in self.inventory.runtimes if f"runtime:{r.key}" == runtime_id), None)
        replaces = next((r for r in self.inventory.runtimes if f"runtime:{r.key}" == replaces_id), None)
        if agent is None or runtime is None:
            return
        self.connecting = {"agent": agent, "runtime": runtime, "from": self.view, "replaces": replaces,
                           "remove_only": remove_only}
        self.connect_title.set_label((_("Remove {0}'s connection to {1}").format(agent.name, runtime.name)
                                      if remove_only else
                                      _("Move {0} from {1} to {2}").format(agent.name, replaces.name, runtime.name)
                                      if replaces else _("Connect {0} to {1}").format(agent.name, runtime.name)).upper())
        form = self.connect_form
        while (child := form.get_first_child()) is not None:
            form.remove(child)
        supported = agent.command in connect.SUPPORTED
        self.connect_preview_heading.set_visible(supported)
        self.connect_preview_scroller.set_visible(supported)
        self.connect_ok.set_visible(supported)
        self.connect_remove.set_visible(False)
        self.connect_status.set_label("")
        if not supported:
            self.connect_intro.set_label(_("{0} cannot be connected from here yet. Hermes, Pi and OpenCode can.")
                                         .format(agent.name))
        else:
            already = connect.is_connected(agent.command, runtime.key)
            if remove_only:
                warning = _(" {0} uses it right now: choose another model in {0} afterwards (or draw a line to "
                            "another local LLM instead).").format(agent.name) \
                    if connect.is_active(agent.command, runtime.key) else ""
                self.connect_intro.set_label(_("The {0} provider is removed from {1}'s settings.")
                                             .format(runtime.name, agent.name) + warning)
            elif replaces is not None:
                self.connect_intro.set_label(
                    _("{0} gets {1} as a provider ({2}) instead of {3}; the {3} provider is removed from its "
                      "settings. Nothing else changes.").format(agent.name, runtime.name,
                                                               connect.base_url(runtime.address), replaces.name))
            else:
                self.connect_intro.set_label(
                    (_("{0} already has {1} as a provider. Change it, or remove the connection.") if already else
                     _("{0} gets {1} as a provider ({2}), with all its models. Nothing else in its settings changes."))
                    .format(agent.name, runtime.name, connect.base_url(runtime.address)))
            self.connect_remove.set_visible(already and replaces is None)
            self.connect_ok.set_visible(not remove_only)
            self.connect_ok.set_label(_("Move") if replaces else _("Update") if already else _("Connect"))
            self.settings_ready = False
            models = connect.chat_models(runtime.models)
            self.connect_models = models
            context = inventory.llama_context() if runtime.key == "llamacpp" else {}
            self.connect_context = context
            self.connect_model = self.dropdown(models or [_("(no models found)")], 0,
                                               lambda _i: self.update_connect_preview())
            if not remove_only:
                self.setting_line(form, _("Model"), _("The model the agent uses when it works with this provider."),
                                  self.connect_model)
            self.connect_use_now = Gtk.Switch(active=True)
            if replaces is not None:
                # Moving the line of the provider the agent uses now: it must use the new one.
                # Moving a line only set up: stays only set up unless switched on.
                in_use = connect.is_active(agent.command, replaces.key)
                self.connect_use_now.set_active(in_use)
                self.connect_use_now.set_sensitive(not in_use)
            self.connect_use_now.connect("notify::active", lambda *_: self.settings_ready and self.update_connect_preview())
            if not remove_only:
                self.setting_line(form, _("Use it now"),
                                  _("Make it the agent's model right away. Off: only set it up, to switch later."),
                                  self.connect_use_now)
            self.settings_ready = True
            self.update_connect_preview()
        self.stack.set_visible_child_name("connect")

    def current_connect_plan(self, remove: bool = False):
        info = self.connecting
        models = self.connect_models
        model = models[self.connect_model.get_selected()] if models else ""
        return connect.plan(info["agent"].command, info["runtime"], model, self.connect_use_now.get_active(),
                            remove=remove, context=self.connect_context,
                            replaces=None if remove else info.get("replaces"))

    def update_connect_preview(self):
        try:
            plan_now = self.current_connect_plan(remove=bool(self.connecting.get("remove_only")))
            text = plan_now.diff() or _("Nothing changes: it is set up like this already.")
            self.connect_ok.set_sensitive(bool(plan_now.changes))
        except (OSError, ValueError) as error:
            text = _("Cannot read the agent's settings: {0}").format(error)
            self.connect_ok.set_sensitive(False)
        self.connect_preview.get_buffer().set_text(text)

    def apply_connect(self, remove: bool):
        info = self.connecting
        try:
            plan_now = self.current_connect_plan(remove=remove)
            if not plan_now.changes:
                self.close_connect()
                return
            backup = connect.apply(plan_now)
        except (OSError, ValueError, RuntimeError) as error:
            self.connect_status.set_label(_("Not changed: {0}").format(error))
            return
        message = (_("{0}: {1} removed") if remove else _("{0} connected to {1}")).format(
            info["agent"].name, info["runtime"].name)
        if info.get("replaces") and not remove:
            message = _("{0} moved from {1} to {2}").format(info["agent"].name, info["replaces"].name,
                                                            info["runtime"].name)
        self.close_connect()
        self.collect()
        self.refresh_overview()
        self.refresh_map()
        self.show_undo(message, backup)

    def close_connect(self):
        info = getattr(self, "connecting", None)
        self.connecting = None
        self.show_view(info["from"] if info else self.view, save=False)

    # --- installing local LLMs (right-click → Install… / Update… / Model folder… / Uninstall…) --------

    def build_install_page(self) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        header = Gtk.Box(spacing=8)
        back = Gtk.Button()
        back.add_css_class("flat")
        back.add_css_class("am-header-btn")
        back.add_css_class("am-back-bordered")
        back_box = Gtk.Box(spacing=6)
        back_box.append(Gtk.Image.new_from_icon_name("go-previous-symbolic"))
        back_box.append(Gtk.Label(label=_("Back")))
        back_box.append(Gtk.Label(label="· ⌫"))
        back.set_child(back_box)
        back.set_tooltip_text(_("Back without changing anything (Backspace, Esc)"))
        back.connect("clicked", lambda *_: self.close_install())
        header.append(back)
        self.install_title = Gtk.Label(xalign=0, hexpand=True)
        self.install_title.add_css_class("am-section-title")
        header.append(self.install_title)
        page.append(header)
        rule = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        rule.add_css_class("am-settings-rule")
        page.append(rule)
        self.install_intro = Gtk.Label(xalign=0, wrap=True, max_width_chars=1)
        self.install_intro.add_css_class("am-detail-value")
        page.append(self.install_intro)
        self.install_form = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.install_form.add_css_class("am-settings")
        page.append(self.install_form)
        self.install_bar = Gtk.ProgressBar(show_text=False)
        self.install_bar.add_css_class("am-progress")
        page.append(self.install_bar)
        self.install_status = Gtk.Label(xalign=0, wrap=True, max_width_chars=1)
        self.install_status.add_css_class("am-agent-detail")
        page.append(self.install_status)
        page.append(Gtk.Box(vexpand=True))
        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        self.install_cancel = Gtk.Button(label=_("Cancel"))
        self.install_cancel.add_css_class("am-small-btn")
        self.install_cancel.connect("clicked", lambda *_: self.cancel_install())
        buttons.append(self.install_cancel)
        self.install_ok = Gtk.Button()
        self.install_ok.add_css_class("am-small-btn")
        self.install_ok.add_css_class("am-primary")
        self.install_ok.connect("clicked", lambda *_: self.run_install())
        buttons.append(self.install_ok)
        page.append(buttons)
        self.installing = None
        return page

    def folder_field(self, box, title: str, hint: str, value: str, suggestions) -> Gtk.Entry:
        """A path field with the suggested folders as buttons under it."""
        entry = Gtk.Entry(text=value, hexpand=True, max_length=1024)
        entry.add_css_class("am-path")
        entry.connect("changed", lambda *_: self.update_install_check())
        column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, hexpand=True)
        column.append(entry)
        if suggestions:
            chips = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=6, row_spacing=4,
                                column_spacing=4)
            for path in suggestions:
                chip = Gtk.Button(label=str(path).replace(str(Path.home()), "~", 1))
                chip.add_css_class("am-chip")
                chip.connect("clicked", lambda _b, p=str(path): entry.set_text(p))
                chips.append(chip)
            column.append(chips)
        self.setting_line(box, title, hint, column)
        return entry

    def open_install(self, rt, mode: str):
        """mode: "install", "update", "models" (change the model folder) or "uninstall"."""
        inst = install_ctl.installer(rt.key)
        if inst is None:
            self.show_note(_("{0} cannot be installed from here yet.").format(rt.name))
            return
        self.installing = {"rt": rt, "inst": inst, "mode": mode, "release": None, "gpu": None,
                           "cancel": False, "busy": False, "from": self.view}
        titles = {"install": _("Install {0}"), "update": _("Update {0}"), "models": _("Model folder of {0}"),
                  "uninstall": _("Uninstall {0}")}
        self.install_title.set_label(titles[mode].format(rt.name).upper())
        form = self.install_form
        while (child := form.get_first_child()) is not None:
            form.remove(child)
        self.install_bar.set_fraction(0)
        self.install_bar.set_visible(mode in ("install", "update"))
        self.install_status.set_label("")
        self.install_cancel.set_label(_("Cancel"))
        self.install_ok.remove_css_class("am-danger")
        self.install_ok.set_sensitive(True)
        self.install_ok.set_visible(True)
        self.install_folder = self.install_models = self.install_login = self.install_start = None
        runtimes = self.inventory.runtimes
        if mode == "install":
            self.install_intro.set_label(_("Looking for the latest version…"))
            folders = install_ctl.folder_suggestions(rt.key, runtimes)
            self.install_folder = self.folder_field(
                form, _("Program folder"), _("Where the program goes. Each version gets its own folder in it."),
                str(folders[0]), folders)
            models = install_ctl.models_suggestions(runtimes, folders[0])
            self.install_models = self.folder_field(
                form, _("Model folder"), _("Where the models go. You can change it later (right-click → Model folder…)."),
                str(models[0]), models)
            self.install_login = Gtk.Switch(active=False)
            self.setting_line(form, _("Start at login"), _("Start it with every login (its user service)."),
                              self.install_login)
            self.install_start = Gtk.Switch(active=True)
            self.setting_line(form, _("Start now"), _("Start it as soon as it is installed."), self.install_start)
            self.install_ok.set_label(_("Install"))
            self.install_ok.set_sensitive(False)
            self.fetch_release()
        elif mode == "update":
            self.install_intro.set_label(_("Looking for the latest version…"))
            self.install_ok.set_label(_("Update"))
            self.install_ok.set_sensitive(False)
            self.fetch_release()
        elif mode == "models":
            current = inst.models_folder()
            self.install_intro.set_label(_("{0} looks for its models here. Models already in the old folder stay "
                                           "there; move them yourself if you want them in the new one.").format(rt.name))
            self.install_models = self.folder_field(form, _("Model folder"), "", current,
                                                    install_ctl.models_suggestions(runtimes, inst.installed_folder()))
            self.install_ok.set_label(_("Change"))
        else:
            folder = inst.installed_folder()
            self.install_intro.set_label(
                _("Removes the program folder {0}, the command, the user service and its settings. "
                  "The models in {1} stay.").format(folder, inst.models_folder() or "—"))
            self.install_ok.set_label(_("Uninstall"))
            self.install_ok.add_css_class("am-danger")
        self.stack.set_visible_child_name("install")
        self.update_install_check()

    def fetch_release(self):
        info = self.installing

        def work():
            try:
                gpu = install_ctl.detect_gpu()
                release = info["inst"].latest(gpu)
                error = None
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as problem:
                gpu = release = None
                error = str(problem)
            GLib.idle_add(done, gpu, release, error)

        def done(gpu, release, error):
            if self.installing is not info:
                return False
            if error:
                self.install_intro.set_label(_("Could not find a version to download: {0}").format(error))
                return False
            info["gpu"], info["release"] = gpu, release
            card = {"amd": _("{0} (ROCm)"), "nvidia": _("{0} (CUDA)")}.get(gpu.vendor, "{0}").format(
                short_gpu(gpu.name, gpu.vram)) if gpu.vendor != "cpu" else _("no graphics card found: the processor")
            current = info["inst"].current_version()
            if info["mode"] == "update" and current == release.version:
                self.install_intro.set_label(_("{0} is up to date ({1}).").format(info["rt"].name, current))
                self.install_ok.set_visible(False)
                return False
            self.install_intro.set_label(
                (_("Version {0} ({1} download) for {2}. Installed in your own folder: no password needed.")
                 if info["mode"] == "install" else _("{3} → {0} ({1} download) for {2}. The settings and models stay."))
                .format(release.version, size_text(release.size), card, current))
            self.update_install_check()
            return False

        threading.Thread(target=work, daemon=True).start()

    def update_install_check(self):
        """Folder checks and free space, as the folders are typed."""
        info = self.installing
        if info is None or info["busy"]:
            return
        problems = []
        for entry in (self.install_folder, self.install_models):
            if entry is None:
                continue
            try:
                install_ctl.check_folder(entry.get_text())
                entry.remove_css_class("error")
            except ValueError as error:
                entry.add_css_class("error")
                problems.append(str(error))
        release = info.get("release")
        if release is not None and self.install_folder is not None and not problems:
            free = install_ctl.free_bytes(install_ctl.check_folder(self.install_folder.get_text()))
            needed = release.size * 3          # the archives and what they unpack to
            if free < needed:
                problems.append(_("not enough space: {0} free, about {1} needed").format(size_text(free),
                                                                                         size_text(needed)))
        self.install_status.set_label(" · ".join(problems))
        ready = not problems and (info["mode"] in ("models", "uninstall") or release is not None)
        self.install_ok.set_sensitive(ready)

    def run_install(self):
        info = self.installing
        if info is None or info["busy"]:
            return
        inst, mode = info["inst"], info["mode"]
        info["busy"] = True
        info["cancel"] = False
        self.install_ok.set_sensitive(False)
        for widget in (self.install_folder, self.install_models, self.install_login, self.install_start):
            if widget is not None:
                widget.set_sensitive(False)
        folder = install_ctl.check_folder(self.install_folder.get_text()) if self.install_folder else None
        models = install_ctl.check_folder(self.install_models.get_text()) if self.install_models else None
        login = self.install_login.get_active() if self.install_login else False
        start_now = self.install_start.get_active() if self.install_start else False

        def progress(fraction, text):
            GLib.idle_add(show_progress, fraction, text)

        def show_progress(fraction, text):
            self.install_bar.set_fraction(max(0.0, min(1.0, fraction)))
            if text:
                self.install_status.set_label(text)
            return False

        def work():
            try:
                if mode == "install":
                    inst.install(folder, models, login, start_now, info["gpu"], info["release"], progress,
                                 lambda: info["cancel"])
                elif mode == "update":
                    inst.update(info["release"], info["gpu"], progress, lambda: info["cancel"])
                elif mode == "models":
                    inst.set_models_folder(models)
                else:
                    inst.uninstall()
                error = None
            except install_ctl.Cancelled:
                error = _("cancelled")
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as problem:
                error = str(problem)
            GLib.idle_add(finished, error)

        def finished(error):
            info["busy"] = False
            if error:
                self.install_status.set_label(_("Not done: {0}").format(error))
                self.install_ok.set_sensitive(True)
                for widget in (self.install_folder, self.install_models, self.install_login, self.install_start):
                    if widget is not None:
                        widget.set_sensitive(True)
                return False
            messages = {"install": _("{0} installed"), "update": _("{0} updated"),
                        "models": _("{0}: model folder changed"), "uninstall": _("{0} uninstalled")}
            self.close_install()
            self.collect()
            self.refresh_overview()
            self.refresh_map()
            self.show_note(messages[mode].format(info["rt"].name))
            return False

        threading.Thread(target=work, daemon=True).start()

    def cancel_install(self):
        info = self.installing
        if info is not None and info["busy"]:
            info["cancel"] = True          # the download stops; the page stays to show it
            return
        self.close_install()

    def close_install(self):
        info = self.installing
        if info is not None and info["busy"]:
            return                         # still working: stay (Cancel stops it)
        self.installing = None
        self.show_view(info["from"] if info else self.view, save=False)

    # --- right-click on a box ---------------------------------------------------------------

    def show_context(self, view, node_id: str, rect):
        """Local LLM: start, stop, start at login."""
        rt = next((r for r in self.inventory.runtimes if f"runtime:{r.key}" == node_id), None)
        if rt is None:
            return
        popover = Gtk.Popover()
        popover.set_parent(view)
        area = Gdk.Rectangle()
        area.x, area.y, area.width, area.height = (int(v) for v in rect)
        popover.set_pointing_to(area)
        popover.set_position(Gtk.PositionType.BOTTOM)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.add_css_class("am-provider-list")
        state = {"running": _("running"), "stopped": _("installed, not running")}.get(graph.runtime_status(rt),
                                                                                       _("not installed"))
        title = Gtk.Label(label=f"{rt.name} · {state}", xalign=0)
        title.add_css_class("am-agent-name")
        box.append(title)
        inst = install_ctl.installer(rt.key)
        ours = inst is not None and inst.installed_folder() is not None
        actions = []
        if not rt.installed and inst is not None:
            actions.append((_("Install…"), "install"))
        elif ours:
            actions += [(_("Update…"), "update"), (_("Model folder…"), "models"), (_("Uninstall…"), "uninstall")]
        if actions:
            column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            for label, mode in actions:
                button = Gtk.Button(label=label, halign=Gtk.Align.START)
                button.add_css_class("flat")
                button.add_css_class("am-menu-item")
                button.connect("clicked", lambda _b, m=mode: (popover.popdown(), self.open_install(rt, m)))
                column.append(button)
            box.append(column)
        elif rt.installed and inst is not None:
            note = Gtk.Label(label=_("Installed by hand: AI Agents Map does not update or remove it."), xalign=0,
                             wrap=True, max_width_chars=36)
            note.add_css_class("am-agent-detail")
            box.append(note)
        if not runtime_ctl.can_control(rt):
            note = Gtk.Label(label=_("Not installed.") if not rt.installed else
                             _("No user service starts it, so it cannot be started from here."), xalign=0,
                             wrap=True, max_width_chars=36)
            note.add_css_class("am-agent-detail")
            if rt.installed or inst is None:
                box.append(note)
        else:
            row = Gtk.Box(spacing=6)
            for label, action, enabled in ((_("Start"), runtime_ctl.start, not rt.running),
                                           (_("Stop"), runtime_ctl.stop, rt.running)):
                button = Gtk.Button(label=label)
                button.add_css_class("am-small-btn")
                button.set_sensitive(enabled)
                button.connect("clicked", lambda _b, a=action, l=label: (popover.popdown(),
                                                                          self.control_runtime(rt, a, l)))
                row.append(button)
            box.append(row)
            login = runtime_ctl.start_at_login(rt)
            if login is not None:
                check = Gtk.CheckButton(label=_("Start at login"), active=login)
                check.set_tooltip_text(_("Enables or disables the user service {0}.").format(rt.service))
                check.connect("toggled", lambda b: self.control_runtime(
                    rt, lambda r: runtime_ctl.set_start_at_login(r, b.get_active()), _("Start at login")))
                box.append(check)
        popover.set_child(box)
        popover.connect("closed", lambda p: GLib.idle_add(p.unparent))
        popover.popup()

    def control_runtime(self, rt, action, label: str):
        """Run a start/stop on a thread; look again once it has had a moment."""
        self.show_note(_("{0}: {1}…").format(rt.name, label))

        def work():
            ok, message = action(rt)
            GLib.timeout_add(1500, lambda: done(ok, message))

        def done(ok, message):
            self.collect()
            self.refresh_overview()
            self.refresh_map()
            status = {"running": _("running"), "stopped": _("installed, not running")}.get(
                graph.runtime_status(next(r for r in self.inventory.runtimes if r.key == rt.key)), "")
            self.show_note(_("{0}: {1}").format(rt.name, status) if ok else
                           _("{0}: {1} failed ({2})").format(rt.name, label, message))
            return False

        threading.Thread(target=work, daemon=True).start()

    def show_note(self, message: str):
        """The undo bar without Undo: a short message."""
        self.undo_label.set_label(message)
        self.undo_backup = None
        self.undo_button.set_visible(False)
        self.undo_bar.set_reveal_child(True)
        if self.undo_timer:
            GLib.source_remove(self.undo_timer)
        self.undo_timer = GLib.timeout_add_seconds(6, self.hide_undo)

    def build_undo_bar(self) -> Gtk.Widget:
        """Floats over the bottom of the card after a change: what happened, and Undo."""
        bar = Gtk.Revealer(transition_type=Gtk.RevealerTransitionType.SLIDE_UP)
        bar.set_valign(Gtk.Align.END)
        bar.set_halign(Gtk.Align.CENTER)
        bar.set_margin_bottom(24)
        line = Gtk.Box(spacing=12)
        line.add_css_class("am-undo")
        self.undo_label = Gtk.Label()
        line.append(self.undo_label)
        undo_button = Gtk.Button(label=_("Undo"))
        undo_button.add_css_class("am-small-btn")
        undo_button.connect("clicked", lambda *_: self.undo_last())
        line.append(undo_button)
        self.undo_button = undo_button
        bar.set_child(line)
        self.undo_bar = bar
        self.undo_backup = None
        self.undo_timer = 0
        return bar

    def show_undo(self, message: str, backup):
        self.undo_label.set_label(message)
        self.undo_backup = backup
        self.undo_button.set_visible(True)
        self.undo_bar.set_reveal_child(True)
        if self.undo_timer:
            GLib.source_remove(self.undo_timer)
        self.undo_timer = GLib.timeout_add_seconds(30, self.hide_undo)

    def hide_undo(self):
        self.undo_timer = 0
        self.undo_bar.set_reveal_child(False)
        return False

    def undo_last(self):
        if self.undo_backup is None:
            return
        try:
            connect.undo(self.undo_backup)
        except (OSError, ValueError) as error:
            self.undo_label.set_label(_("Undo failed: {0}").format(error))
            return
        self.undo_backup = None
        self.hide_undo()
        self.collect()
        self.refresh_overview()
        self.refresh_map()

    # --- settings ----------------------------------------------------------------

    def build_settings_page(self) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        header = Gtk.Box(spacing=8)
        back = Gtk.Button()
        back.add_css_class("flat")
        back.add_css_class("am-header-btn")
        back.add_css_class("am-back-bordered")
        back_box = Gtk.Box(spacing=6)
        back_box.append(Gtk.Image.new_from_icon_name("go-previous-symbolic"))
        back_box.append(Gtk.Label(label=_("Back")))
        back_box.append(Gtk.Label(label="· ⌫"))
        back.set_child(back_box)
        back.set_tooltip_text(_("Back (Backspace, Esc)"))
        back.connect("clicked", lambda *_: self.close_settings())
        header.append(back)
        title = Gtk.Label(label=_("Settings").upper(), xalign=0, hexpand=True)
        title.add_css_class("am-section-title")
        header.append(title)
        page.append(header)
        rule = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        rule.add_css_class("am-settings-rule")
        page.append(rule)
        self.settings_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.settings_box.add_css_class("am-settings")
        page.append(self.page_scroller(self.settings_box))
        return page

    def setting_line(self, box, title: str, hint: str, control) -> Gtk.Box:
        """dim label | control; the explanation is the tooltip of both."""
        row = Gtk.Box(spacing=10)
        row.add_css_class("am-line")
        label = Gtk.Label(label=title, xalign=0, wrap=True, max_width_chars=1)
        label.add_css_class("am-line-label")
        label.set_size_request(140, -1)
        label.set_valign(Gtk.Align.CENTER)
        row.append(label)
        control.set_valign(Gtk.Align.CENTER)
        control.set_halign(Gtk.Align.START)
        control.set_hexpand(True)
        if hint:
            label.set_tooltip_text(hint)
            control.set_tooltip_text(hint)
        row.append(control)
        box.append(row)
        return row

    @staticmethod
    def settings_heading(box, text: str):
        label = Gtk.Label(label=text.upper(), xalign=0)
        label.add_css_class("am-setting-heading")
        box.append(label)

    def dropdown(self, labels: list[str], selected: int, on_change) -> Gtk.DropDown:
        drop = Gtk.DropDown.new_from_strings(labels)
        drop.set_selected(max(0, selected))
        drop.connect("notify::selected", lambda d, _p: self.settings_ready and on_change(d.get_selected()))
        return drop

    def fill_settings(self):
        self.settings_ready = False
        box = self.settings_box
        while (child := box.get_first_child()) is not None:
            box.remove(child)
        codes = ["auto"] + [code for code, _name in i18n.LANGUAGES]
        setting = i18n.language_setting()
        self.setting_line(box, _("Language"), _("AI Agents Map restarts in the new language."),
                          self.dropdown([_("System ({0})").format(i18n.language_name(i18n.system_language()))]
                                        + [name for _code, name in i18n.LANGUAGES],
                                        codes.index(setting) if setting in codes else 0,
                                        lambda i: self.set_language(codes[i])))
        starts = ["main", "map", "last"]
        self.setting_line(box, _("Open on"), _("The view shown when AI Agents Map opens."),
                          self.dropdown([_("Overview"), _("Map"), _("Last used view")],
                                        starts.index(ai_core.start_view()),
                                        lambda i: self.save_setting(start_view=starts[i],
                                                                    last_view=self.view)))
        self.settings_ready = True

    def save_setting(self, **values):
        try:
            ai_core.update_settings(**values)
        except OSError:
            pass

    def set_language(self, code: str):
        if code == i18n.language_setting():
            return
        try:
            i18n.save_language(code)
        except OSError:
            return
        os.environ["AI_AGENTS_MAP_PAGE"] = "settings"
        os.environ["AI_AGENTS_MAP_VIEW"] = self.view
        sys.stdout.flush()
        sys.stderr.flush()
        os.execv(sys.executable, [sys.executable, *sys.argv])

    def open_settings(self):
        self.fill_settings()
        self.stack.set_visible_child_name("settings")

    def close_settings(self):
        self.show_view(self.view, save=False)

    # --- size ----------------------------------------------------------------------

    def size_limits(self) -> tuple[int, int]:
        width = self.window.get_width() if self.window is not None else 0
        if width <= 0:
            geo = self.monitor_geometry() if self.window is not None else None
            width = geo.width if geo is not None else 1920
        return (max(CARD_WIDTH, width - 80),
                max(ai_core.MIN_WINDOW_SIZE[1], monitor_height() - 2 * SCREEN_MARGIN))

    def fit(self):
        """As large as the screen allows (checked on every open: another monitor)."""
        self.card_w, self.list_h = self.size_limits()
        self.card.set_size_request(self.card_w, -1)
        if getattr(self, "side_pane", None) is not None:
            # A very small screen keeps the cards alone.
            self.split = self.card_w >= SPLIT_WIDTH
            self.side_pane.set_visible(self.split)
            self.list_pane.set_hexpand(not self.split)
            self.list_pane.set_size_request(LIST_PANE_WIDTH if self.split else -1, -1)
        for scroller in self.scrollers:
            # Raise the maximum first: GTK refuses a minimum above the maximum.
            scroller.set_max_content_height(100000)
            scroller.set_min_content_height(self.list_h)
            scroller.set_max_content_height(self.list_h)
        if self.window is not None:
            self.window.set_default_size(1, 1)

    # --- screen --------------------------------------------------------------------

    def monitor_geometry(self):
        display = Gdk.Display.get_default()
        surface = self.window.get_surface()
        monitor = display.get_monitor_at_surface(surface) if display and surface else None
        if monitor is None and display is not None and display.get_monitors().get_n_items():
            monitor = display.get_monitors().get_item(0)
        return monitor.get_geometry() if monitor is not None else None

    @staticmethod
    def inside(widget, ancestor) -> bool:
        return widget is not None and (widget == ancestor or widget.is_ancestor(ancestor))

    def on_overlay_click(self, _gesture, _count, x, y):
        picked = self.overlay.pick(x, y, Gtk.PickFlags.DEFAULT)
        if not self.inside(picked, self.card_frame):
            self.close()

    def on_active_changed(self, window, _property):
        if window.get_visible() and not window.is_active():
            GLib.timeout_add(100, self.close_if_inactive)

    def close_if_inactive(self):
        if self.window.get_visible() and not self.window.is_active():
            self.close()
        return False

    # --- theme ---------------------------------------------------------------------

    def watch_theme(self):
        path = os.path.expanduser("~/.local/state/omarchy/current/theme")
        self.theme_monitor = Gio.File.new_for_path(path).monitor(Gio.FileMonitorFlags.WATCH_MOVES, None)
        self.theme_monitor.connect("changed", lambda *_: GLib.timeout_add(300, self.reload_theme))
        parent = Gio.File.new_for_path(os.path.dirname(path))
        self.theme_parent_monitor = parent.monitor_directory(Gio.FileMonitorFlags.WATCH_MOVES, None)
        self.theme_parent_monitor.connect("changed", lambda *_: GLib.timeout_add(300, self.reload_theme))

    def reload_theme(self):
        if self.theme.reload():
            self.apply_theme_css()
        return False

    def apply_theme_css(self):
        colors = self.theme.colors
        display = Gdk.Display.get_default()
        if self.style_provider is not None:
            Gtk.StyleContext.remove_provider_for_display(display, self.style_provider)
        radius = ai_core.hyprland_rounding()
        bg_hex = colors.get("background", "#1e1e2e")
        fg_hex = colors.get("foreground", "#cdd6f4")
        muted_hex = colors.get("muted", "#888894")
        accent_hex = colors.get("accent", "#89b4fa")
        green_hex = colors.get("green", "#a6e3a1")
        yellow_hex = colors.get("yellow", "#f9e2af")
        orange_hex = colors.get("orange", "#fab387")
        red_hex = colors.get("red", "#f04452")
        # The settings labels' grey in the QML plugins: Qt.darker(foreground, 1.45).
        try:
            r, g, b = (int(fg_hex.lstrip("#")[i:i + 2], 16) / 255 for i in (0, 2, 4))
            hue, sat, val = colorsys.rgb_to_hsv(r, g, b)
            dim_hex = "#%02x%02x%02x" % tuple(round(c * 255) for c in colorsys.hsv_to_rgb(hue, sat, val / 1.45))
        except (ValueError, IndexError):
            dim_hex = muted_hex
        css = f"""
        window#aiagentsmap-window {{ background: transparent; }}
        .am-card {{ background: {bg_hex}; color: {fg_hex}; border: 1px solid {accent_hex};
            border-radius: {radius}px; padding: 17px; }}
        .am-title {{ font-size: 15px; font-weight: 600; }}
        .am-header-hint {{ font-size: 10px; color: {dim_hex}; }}
        .am-hint {{ color: {muted_hex}; font-size: 13px; }}
        .am-card button.am-header-btn {{ padding: 4px; min-width: 0; min-height: 0; margin-right: 4px; }}
        .am-card button.am-view-btn {{ margin-right: 0; }}
        .am-card button.am-view-btn:checked {{ color: {accent_hex}; background: alpha({accent_hex}, 0.14); }}
        .am-view-switch {{ margin-right: 4px; }}
        .am-card button.am-gear-btn {{ border: 1px solid alpha({fg_hex}, 0.35); border-radius: {radius}px;
            padding: 1px 6px; margin-right: 0; }}
        .am-gear {{ font-family: "JetBrainsMono Nerd Font", "Symbols Nerd Font", monospace; font-size: 13px; }}
        .am-card button.am-back-bordered {{ border: 1px solid alpha({fg_hex}, 0.35); border-radius: {radius}px;
            padding: 4px 10px; }}
        .am-section-title {{ font-size: 11px; font-weight: 700; letter-spacing: 1px; }}
        separator.am-settings-rule {{ background-color: alpha({fg_hex}, 0.12); background-image: none;
            min-height: 1px; margin: 0; }}
        .am-card label.am-line-label {{ font-size: 11px; color: {dim_hex}; }}
        .am-setting-heading {{ font-size: 11px; font-weight: 700; letter-spacing: 1px; color: {fg_hex};
            margin-top: 6px; }}
        .am-settings {{ padding: 2px 4px 12px 0; }}
        .am-line dropdown > button {{ min-height: 0; padding: 2px 8px; border: 1px solid alpha({fg_hex}, 0.35); }}
        .am-line dropdown label {{ font-size: 11px; }}
        .am-line switch {{ min-height: 16px; min-width: 36px; }}
        .am-line switch slider {{ min-width: 14px; min-height: 14px; margin: 1px; }}
        .am-line switch:checked {{ background: {accent_hex}; }}
        .am-empty-icon {{ color: {muted_hex}; opacity: 0.6; }}
        .am-empty-title {{ font-size: 14px; font-weight: 600; }}
        .am-group-heading {{ font-size: 10px; font-weight: 700; letter-spacing: 1px; color: {dim_hex};
            margin-top: 4px; }}
        .am-agent {{ background: alpha({fg_hex}, 0.05); border: 1px solid alpha({fg_hex}, 0.10);
            border-radius: {radius}px; padding: 10px 12px; }}
        .am-agent-missing {{ opacity: 0.5; }}
        .am-agent.am-agent-selected {{ background: alpha({accent_hex}, 0.18); border: 2px solid {accent_hex}; }}
        .am-agent-icon.am-status-running {{ color: {green_hex}; }}
        .am-agent-icon.am-status-stopped {{ color: {yellow_hex}; }}
        .am-agent-icon.am-status-missing {{ color: {muted_hex}; }}
        .am-card textview.am-diff, .am-card textview.am-diff text {{ background: alpha({fg_hex}, 0.04);
            color: {fg_hex}; font-size: 11px; }}
        .am-card button.am-primary {{ background: alpha({accent_hex}, 0.25); border-color: {accent_hex}; }}
        .am-card button.am-danger {{ color: {red_hex}; border-color: alpha({red_hex}, 0.6); }}
        .am-undo {{ background: {bg_hex}; border: 1px solid {accent_hex}; border-radius: {radius}px;
            padding: 8px 14px; }}
        popover.am-provider-popover > contents, .am-provider-list {{ padding: 6px; }}
        .am-provider-list checkbutton label {{ font-size: 12px; }}
        .am-provider-list checkbutton check {{ border: 1px solid alpha({fg_hex}, 0.4); background: transparent; }}
        .am-provider-list checkbutton check:checked {{ background: {accent_hex}; border-color: {accent_hex};
            color: {bg_hex}; }}
        .am-card entry.am-path {{ font-family: monospace; font-size: 12px; min-height: 0; padding: 3px 8px; }}
        .am-card entry.am-path.error {{ border-color: {red_hex}; }}
        progressbar.am-progress trough {{ min-height: 6px; background: alpha({fg_hex}, 0.10); border-radius: 3px; }}
        progressbar.am-progress progress {{ min-height: 6px; background: {accent_hex}; border-radius: 3px; }}
        .am-card button.am-menu-item {{ padding: 3px 8px; font-size: 12px; }}
        .am-card-check {{ color: {accent_hex}; font-weight: 700; }}
        .am-agent.am-agent-default {{ border-color: alpha({accent_hex}, 0.7); }}
        .am-agent-icon {{ font-family: "JetBrainsMono Nerd Font", "Symbols Nerd Font", monospace;
            font-size: 22px; color: {accent_hex}; min-width: 28px; }}
        .am-agent-icon.am-agent-icon-omarchy {{ font-family: "omarchy"; }}
        .am-agent-missing .am-agent-icon {{ color: {fg_hex}; }}
        .am-agent-name {{ font-size: 13px; font-weight: 600; }}
        .am-agent-detail {{ font-size: 11px; color: {dim_hex}; }}
        .am-agent-badge {{ font-size: 10px; font-weight: 600; color: {bg_hex}; background: {accent_hex};
            border-radius: 4px; padding: 0 5px; }}
        .am-agent {{ padding: 0; }}
        .am-agent > box:first-child {{ padding: 10px 12px; }}
        .am-agent-chevron {{ color: {muted_hex}; }}
        .am-agent-details {{ padding: 2px 12px 12px 50px; }}
        .am-card label.am-detail-value {{ font-size: 12px; }}
        levelbar.am-limit trough {{ min-height: 5px; background: alpha({fg_hex}, 0.10); border: none;
            border-radius: 3px; padding: 0; }}
        levelbar.am-limit block {{ min-height: 5px; border-radius: 3px; border: none; }}
        levelbar.am-limit block.filled {{ background: {accent_hex}; }}
        levelbar.am-limit block.am-ok {{ background: {green_hex}; }}
        levelbar.am-limit block.am-warn {{ background: {yellow_hex}; }}
        levelbar.am-limit block.am-hot {{ background: {orange_hex}; }}
        levelbar.am-limit block.am-full {{ background: {red_hex}; }}
        levelbar.am-limit block.empty {{ background: transparent; }}
        .am-card button.am-small-btn {{ font-size: 11px; min-height: 0; padding: 1px 10px;
            border: 1px solid alpha({fg_hex}, 0.35); border-radius: {radius}px; }}
        .am-card button.am-chip {{ font-size: 11px; min-height: 0; padding: 1px 10px; border-radius: 999px;
            border: 1px solid alpha({fg_hex}, 0.25); background: transparent; color: {dim_hex}; }}
        .am-card button.am-chip:checked {{ background: alpha({accent_hex}, 0.18); border-color: {accent_hex};
            color: {fg_hex}; }}
        .am-card button.am-chip:disabled {{ opacity: 0.4; }}
        .am-canvas {{ border: 1px dashed alpha({fg_hex}, 0.25); border-radius: {radius}px; padding: 12px; }}
        """
        for view in (getattr(self, "map_view", None), getattr(self, "side_map", None)):
            if view is not None:
                view.set_palette({"bg": bg_hex, "fg": fg_hex, "dim": dim_hex, "muted": muted_hex,
                                  "accent": accent_hex, "green": green_hex, "yellow": yellow_hex}, radius)
        provider = Gtk.CssProvider()
        provider.load_from_string(css)
        Gtk.StyleContext.add_provider_for_display(display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.style_provider = provider

    # --- keys ------------------------------------------------------------------------

    def on_key_released(self, _controller, keyval, _keycode, _state):
        if keyval in (Gdk.KEY_Shift_L, Gdk.KEY_Shift_R):
            self.map_view.set_shift(False)

    def on_key(self, _controller, keyval, _keycode, state):
        if keyval in (Gdk.KEY_Shift_L, Gdk.KEY_Shift_R):
            self.map_view.set_shift(True)
            return False
        page = self.stack.get_visible_child_name()
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        if keyval == Gdk.KEY_Escape:
            if page == "settings":
                self.close_settings()
            elif page == "connect":
                self.close_connect()
            elif page == "install":
                self.cancel_install()
            elif page in ("map", "main") and self.selection:
                self.set_selection([])   # the first Esc clears the selection
            else:
                self.close()
            return True
        if page == "install" and keyval == Gdk.KEY_BackSpace and isinstance(self.window.get_focus(), Gtk.Text):
            return False          # Backspace edits the folder fields
        if page in ("settings", "connect", "install"):
            if keyval == Gdk.KEY_BackSpace:
                {"settings": self.close_settings, "connect": self.close_connect,
                 "install": self.close_install}[page]()
                return True
            return False          # Tab and arrows move between the controls
        if ctrl and keyval in (Gdk.KEY_1, Gdk.KEY_KP_1):
            self.show_view("main")
            return True
        if ctrl and keyval in (Gdk.KEY_2, Gdk.KEY_KP_2):
            self.show_view("map")
            return True
        if ctrl and keyval == Gdk.KEY_comma:
            self.open_settings()
            return True
        return False

    # --- open / close ------------------------------------------------------------

    def toggle(self):
        if self.window.is_visible():
            self.close()
        else:
            self.open()

    def open(self):
        self.fit()
        self.collect()
        self.refresh_overview()
        self.refresh_map()
        self.window.present()
        self.layer_watch.start()
        # Restarted after a language change: back on the settings page.
        view = os.environ.pop("AI_AGENTS_MAP_VIEW", "")
        if view in ai_core.VIEWS:
            self.show_view(view, save=False)
        if os.environ.pop("AI_AGENTS_MAP_PAGE", "") == "settings":
            GLib.idle_add(self.open_settings)

    def on_menu_opened(self, _name):
        """Another menu wants the keyboard: get out of its way at once."""
        if self.window is not None and self.window.is_visible():
            self.close()

    def close(self):
        self.layer_watch.stop()
        self.map_view.set_shift(False)   # Shift may have been let go elsewhere
        if self.stack.get_visible_child_name() == "settings":
            self.close_settings()   # never reopen onto the settings page
        elif self.stack.get_visible_child_name() == "connect":
            self.close_connect()    # nor onto a half-made connection
        elif self.stack.get_visible_child_name() == "install" and not (self.installing or {}).get("busy"):
            self.close_install()
        self.window.hide()


def main():
    app = AgentsMap()
    atexit.register(cleanup_pid)
    return app.run()


if __name__ == "__main__":
    sys.exit(main())
