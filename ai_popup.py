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
import agent_profiles
import agents
import ai_core
import connect
import corners
import mcp_ctl
import mcp_library
import mcp_registry
import mcp_scan
import mcp_tools
import skills_ctl
import skills_scan
import skills_web
import runtime_ctl
import install_ctl
import models_ctl
import context_ctl
import time
import struct
import shutil
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
        self.researching = set()   # new agents being looked up on the web (agent_profiles)
        self.asking_tools = set()  # MCP servers being asked for their tools (mcp_tools)
        self.found_mcp = None      # mcp_scan results (None: not scanned in this session)
        self.configured_now = None
        self.scanning_mcp = False
        self.found_skills = None   # skills_scan results
        self.checking_new = False
        self.detail_boxes = {}     # card key -> (details box, fill function)
        self.agent_list = []
        self.inventory = inventory.Inventory()
        self.selection = []        # node ids selected in the maps and (wide overview) the cards
        self.card_widgets = {}     # card key -> (card box, check mark label)
        self.split = False         # wide overview: cards left, map right
        self.sidebar_visible = True  # the cards beside the map (header toggle, Ctrl+B); shown on every open

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

    # --- text fields: paste and drops only through bounded reads --------------------------------

    def guard_text_field(self, field):
        """Route every way text can enter a field from another client through a
        bounded read. GTK's own paste, middle-click paste and text drops buffer
        the whole offer in memory with no limit or deadline (as in Theme
        Manager). `field`: a Gtk.Entry (or its Gtk.Text) or a Gtk.TextView."""
        text = field.get_delegate() if isinstance(field, Gtk.Entry) else field
        text = text or field
        if getattr(text, "_am_guarded", False):
            return
        text._am_guarded = True
        if isinstance(text, Gtk.Text) and text.get_max_length() == 0:
            text.set_max_length(4096)
        # Ctrl+V, Shift+Insert and the context menu's Paste all emit this.
        text.connect("paste-clipboard", self.on_field_paste, field)
        middle = Gtk.GestureClick(button=Gdk.BUTTON_MIDDLE)
        middle.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        middle.connect("pressed", lambda gesture, *_: gesture.set_state(Gtk.EventSequenceState.CLAIMED))
        field.add_controller(middle)
        controllers = text.observe_controllers()
        for i in reversed(range(controllers.get_n_items())):
            controller = controllers.get_item(i)
            if isinstance(controller, (Gtk.DropTarget, Gtk.DropTargetAsync)):
                text.remove_controller(controller)

    def guard_text_fields_in(self, root):
        guarded = 0
        stack = [root]
        while stack:
            widget = stack.pop()
            if isinstance(widget, (Gtk.Text, Gtk.TextView)):
                self.guard_text_field(widget)
                guarded += 1
                continue
            child = widget.get_first_child()
            while child is not None:
                stack.append(child)
                child = child.get_next_sibling()
        return guarded

    def on_field_paste(self, text, field):
        text.stop_emission_by_name("paste-clipboard")
        if isinstance(field, Gtk.TextView) and not field.get_editable():
            return
        lines = isinstance(field, Gtk.TextView)

        def worker():
            pasted = ai_core.clipboard_text(lines=lines)
            GLib.idle_add(self.insert_pasted, field, pasted)

        threading.Thread(target=worker, daemon=True).start()

    def insert_pasted(self, field, pasted):
        if pasted is None:
            self.show_note(_("Clipboard text is too long to paste (max {0} KiB)").format(
                ai_core.MAX_PASTE_BYTES // 1024))
        elif pasted and isinstance(field, Gtk.TextView):
            buffer = field.get_buffer()
            buffer.delete_selection(True, True)
            buffer.insert_at_cursor(pasted)
        elif pasted:
            text = field.get_delegate() if isinstance(field, Gtk.Entry) else field
            text = text or field
            text.delete_selection()
            position = text.insert_text(pasted, text.get_position())
            text.set_position(position)
        return GLib.SOURCE_REMOVE

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
        stack.add_named(self.build_settings_page(), "settings")
        stack.add_named(self.build_connect_page(), "connect")
        stack.add_named(self.build_install_page(), "install")
        stack.add_named(self.build_models_page(), "models")
        stack.add_named(self.build_mcp_page(), "mcp")
        stack.add_named(self.build_add_mcp_page(), "addmcp")
        stack.add_named(self.build_add_skills_page(), "addskills")
        self.guard_text_fields_in(window)
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
        # Show or hide the cards beside the map (Overview, wide screens only).
        sidebar_button = Gtk.ToggleButton(icon_name="sidebar-show-symbolic", active=True)
        sidebar_button.set_tooltip_text(_("Show or hide the cards (Ctrl+B)"))
        sidebar_button.add_css_class("flat")
        sidebar_button.add_css_class("am-header-btn")
        sidebar_button.connect("toggled", lambda b: self.set_sidebar(b.get_active()))
        header.append(sidebar_button)
        self.sidebar_button = sidebar_button
        title = Gtk.Label(label="AI Agents Map", xalign=0)
        title.add_css_class("am-title")
        title.set_ellipsize(Pango.EllipsizeMode.END)
        title.set_max_width_chars(30)
        title.set_hexpand(True)
        header.append(title)
        self.title_label = title
        # One view (the cards with the map beside them): no view switch any more.
        self.view_buttons = {}
        self.view_switch = Gtk.Box()

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
        side.append(self.build_layer_toggles())
        # The filter line: a hint, or what is selected and a way back to everything.
        bar = Gtk.Box(spacing=8)
        self.side_filter = Gtk.Label(xalign=0, hexpand=True)
        self.side_filter.add_css_class("am-agent-detail")
        self.side_filter.set_ellipsize(Pango.EllipsizeMode.END)
        self.side_filter.set_max_width_chars(1)
        bar.append(self.side_filter)
        self.map_show_all = Gtk.Button(label=_("Clear selection"))
        self.map_show_all.add_css_class("flat")
        self.map_show_all.add_css_class("am-small-btn")
        self.map_show_all.set_tooltip_text(_("Clear the selection (Esc)"))
        self.map_show_all.connect("clicked", lambda *_: self.set_selection([]))
        bar.append(self.map_show_all)
        # Zoom: − 100 % + (also Ctrl+scroll on the map, Ctrl+plus / minus / 0).
        zoom = Gtk.Box(spacing=0)
        zoom.add_css_class("am-zoom")
        for icon, tip, step in (("zoom-out-symbolic", _("Zoom out (Ctrl+minus, Ctrl+scroll)"), -1),
                                (None, _("Back to 100 % (Ctrl+0)"), 0),
                                ("zoom-in-symbolic", _("Zoom in (Ctrl+plus, Ctrl+scroll)"), 1)):
            button = Gtk.Button(icon_name=icon) if icon else Gtk.Button(label="100 %")
            button.add_css_class("flat")
            button.add_css_class("am-small-btn")
            button.set_tooltip_text(tip)
            button.connect("clicked", lambda _b, d=step: self.zoom_map(d))
            zoom.append(button)
            if step == 0:
                self.zoom_label = button
        bar.append(zoom)
        side.append(bar)
        self.side_map = MapView()
        self.side_map.set_layers(ai_core.map_layers())
        self.map_view = self.side_map      # the one map (code below still says map_view in places)
        self.side_map.on_zoom = lambda _z: self.zoom_changed()
        self.update_layer_toggles()
        self.side_map.on_selection = self.on_map_selection
        self.side_map.on_link = self.on_map_link
        self.side_map.on_relink = self.on_map_relink
        self.side_map.on_pick_model = self.on_map_pick_model
        self.side_map.on_add = self.on_map_add
        self.side_map.on_context = lambda node_id, rect, v=self.side_map: self.show_context(v, node_id, rect)
        self.side_map.on_header = lambda column, rect: self.show_column_list(self.side_map, column, rect)
        map_scroller = self.page_scroller(self.side_map)
        map_scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)   # zoomed in: sideways too
        side.append(map_scroller)
        self.side_map.set_zoom(ai_core.load_settings().get("map_zoom", 1.0)
                               if isinstance(ai_core.load_settings().get("map_zoom"), (int, float)) else 1.0)
        self.zoom_label.set_label(f"{round(self.side_map.zoom * 100)} %")
        side.set_visible(False)
        self.side_pane = side
        body.append(side)
        page.append(body)
        return page

    def collect(self):
        """Everything both views show (file system and /proc only, ~0.1 s)."""
        self.agent_list = agents.list_agents()
        self.inventory = inventory.collect(self.agent_list)
        self.configured_now = None     # installed_mcp() reads the settings again

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
            users = ", ".join(names.get(a, a) for a in server.agents) or _("not used by any agent")
            box.append(self.expandable_card(f"mcp:{server.name}", "\U000F0337", "", server.name, users,
                                 lambda b, s=server: self.fill_mcp(b, s, names)))
        new_mcp, new_skills = self.new_found()
        add = Gtk.Button(label=_("Add MCP server…") + (f" ({_('{0} new').format(len(new_mcp))})" if new_mcp else ""),
                         halign=Gtk.Align.START)
        add.add_css_class("am-small-btn")
        add.set_tooltip_text(_("Found on this PC, from the web, or by hand"))
        add.connect("clicked", lambda *_: self.open_add_mcp())
        box.append(add)

        self.section(_("Skills ({0})").format(sum(len(s.skills) for s in inv.skill_sets)))
        if not inv.skill_sets:
            self.section_hint(_("No skills found for the installed agents."))
        for skill_set in inv.skill_sets:
            sub = ngettext("1 skill", "{0} skills", len(skill_set.skills)).format(len(skill_set.skills)) \
                + " · " + ", ".join(names.get(a, a) for a in skill_set.agents)
            box.append(self.expandable_card(f"skills:{skill_set.path}", "\U000F0EB3", "", skill_set.path, sub,
                                 lambda b, s=skill_set: self.fill_skills(b, s, names)))
        add = Gtk.Button(label=_("Add skills…") + (f" ({_('{0} new').format(len(new_skills))})" if new_skills else ""),
                         halign=Gtk.Align.START)
        add.add_css_class("am-small-btn")
        add.set_tooltip_text(_("Found on this PC or from the web"))
        add.connect("clicked", lambda *_: self.open_add_skills())
        box.append(add)

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
        """One selection for both maps and the cards; one agent at most (the last one picked)."""
        ids = list(dict.fromkeys(ids))
        agents = [i for i in ids if i.startswith("agent:")]
        self.selection = [i for i in ids if not i.startswith("agent:") or i == agents[-1]]
        for view in (self.side_map,):
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

    # --- new agents: look up on the web how to set them up ------------------------------------

    @staticmethod
    def is_new_agent(command: str) -> bool:
        """An agent this plugin knows nothing about (not in skills_ctl / mcp_ctl)."""
        return command not in skills_ctl.NATIVE and command not in mcp_ctl.SUPPORTED

    def research_new_agents(self):
        """Once per new installed agent (setting "look_up_new_agents", on by default)."""
        if not ai_core.load_settings().get("look_up_new_agents", True):
            return
        for agent in self.agent_list:
            if agent.installed and self.is_new_agent(agent.command) and agent_profiles.needs_research(agent.command):
                self.start_research(agent)

    def start_research(self, agent):
        command = agent.command
        if command in self.researching:
            return
        self.researching.add(command)
        self.fill_card(f"agent:{command}")
        installed = {a.command for a in self.agent_list if a.installed}

        def work():
            result = agent_profiles.research(agent, installed)
            GLib.idle_add(done, result)

        def done(result):
            self.researching.discard(command)
            self.collect()
            self.refresh_overview()
            self.refresh_map()
            self.show_note(_("{0}: found out how to set up MCP servers and skills").format(agent.name)
                           if result.get("status") == "ok" else
                           _("{0}: could not find out how to set it up ({1})").format(agent.name,
                                                                                    result.get("error", "")))
            return False

        threading.Thread(target=work, daemon=True).start()

    def research_row(self, agent):
        """(label, value, tip) for the card of a new agent."""
        if agent.command in self.researching:
            return (_("Setup"), _("Looking up on the web where it keeps MCP servers and skills…"), "")
        profile = agent_profiles.load(agent.command)
        if profile is None:
            return (_("Setup"), _("Not looked up yet"), "")
        if profile["status"] != "ok":
            return (_("Setup"), _("Look-up failed: {0}").format(profile.get("error", "")),
                    _("Right-click the agent on the map to try again."))
        researcher = next((a.name for a in self.agent_list if a.command == profile.get("researcher")),
                          profile.get("researcher", ""))
        found = [_("MCP servers") if profile.get("mcp") else "", _("Skills") if profile.get("skills") else ""]
        value = _("Looked up on the web on {0} by {1}: {2}").format(profile.get("checked", "")[:10], researcher,
                                                                    ", ".join(f for f in found if f))
        tip = "\n".join([profile.get("notes", "")] + profile.get("sources", [])).strip()
        return (_("Setup"), value, tip)

    def fill_agent(self, box, agent):
        command = agent.command
        info = agent_details.details_for(command)
        inv = self.inventory
        rows = []
        if agent.installed and self.is_new_agent(command):
            rows.append(self.research_row(agent))
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
        buttons = Gtk.Box(spacing=8)
        buttons.append(self.details_button(lambda *_: self.open_mcp_details(server.name)))
        remove = Gtk.Button(halign=Gtk.Align.START)
        content = Gtk.Box(spacing=6)
        content.append(Gtk.Image.new_from_icon_name("user-trash-symbolic"))
        content.append(Gtk.Label(label=_("Remove")))
        remove.set_child(content)
        remove.add_css_class("flat")
        remove.add_css_class("am-small-btn")
        remove.set_tooltip_text(_("Take it from every agent and from the list"))
        remove.connect("clicked", lambda *_: self.open_mcp_delete(server.name))
        buttons.append(remove)
        box.append(buttons)

    def details_button(self, on_click) -> Gtk.Widget:
        """The (i) button that opens a card's full details page."""
        button = Gtk.Button(halign=Gtk.Align.START)
        content = Gtk.Box(spacing=6)
        content.append(Gtk.Image.new_from_icon_name("dialog-information-symbolic"))
        content.append(Gtk.Label(label=_("Details")))
        button.set_child(content)
        button.add_css_class("flat")
        button.add_css_class("am-small-btn")
        button.connect("clicked", on_click)
        return button

    # --- MCP server details (the (i) on its card) ---------------------------------------------------

    def build_mcp_page(self) -> Gtk.Widget:
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
        back.set_tooltip_text(_("Back (Backspace, Esc)"))
        back.connect("clicked", lambda *_: self.close_mcp_details())
        header.append(back)
        self.mcp_title = Gtk.Label(xalign=0, hexpand=True)
        self.mcp_title.add_css_class("am-section-title")
        header.append(self.mcp_title)
        page.append(header)
        rule = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        rule.add_css_class("am-settings-rule")
        page.append(rule)
        self.mcp_body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.mcp_body.add_css_class("am-settings")
        page.append(self.page_scroller(self.mcp_body))
        return page

    def open_mcp_details(self, name: str):
        server = next((m for m in self.inventory.mcp if m.name == name), None)
        if server is None:
            return
        if self.stack.get_visible_child_name() != "mcp":
            self.mcp_from = self.view if self.stack.get_visible_child_name() in ai_core.VIEWS else "main"
        self.mcp_title.set_label(_("MCP server {0}").format(name).upper())
        body = self.mcp_body
        while (child := body.get_first_child()) is not None:
            body.remove(child)
        names = {a.command: a.name for a in self.agent_list}
        names["lmstudio"] = "LM Studio"
        for command in server.agents:
            self.settings_heading(body, names.get(command, command))
            entry = mcp_ctl.normal(mcp_ctl.raw_entries(command).get(name))
            self.detail_grid(body, self.mcp_rows(command, entry))
        missing = [a.name for a in self.agent_list
                   if a.installed and a.command not in server.agents and mcp_ctl.supported(a.command)]
        if missing:
            self.settings_heading(body, _("Not set up"))
            self.note(body, ", ".join(missing) + " · "
                      + _("draw a line from the agent to this box on the map to add it."))
        self.mcp_tools_section(body, server)
        self.mcp_showing = name
        self.stack.set_visible_child_name("mcp")
        self.update_header()

    @staticmethod
    def mcp_rows(command: str, entry) -> list:
        """(label, value, tip) of one agent's entry. Values of env and headers stay hidden."""
        files = mcp_ctl.paths_for(command)
        file = "~/.claude.json" if command == "claude" else (skills_ctl.tilde(files[0]) if files else "")
        rows = [(_("Settings file"), file, _("Where this agent keeps the server."))]
        if entry is None:
            return rows + [(_("Type"), _("cannot be read"), "")]
        if entry["transport"] == "stdio":
            import shlex
            rows += [(_("Type"), _("runs a program (stdio)"), ""),
                     (_("Command"), entry["command"], ""),
                     (_("Arguments"), " ".join(shlex.quote(a) for a in entry["args"]), "")]
            hidden = entry["env"]
            rows.append((_("Environment"), ", ".join(f"{k} = •••" for k in hidden),
                         _("Values are not shown: they may be keys or tokens.")))
        else:
            from urllib.parse import urlsplit
            parts = urlsplit(entry["url"])
            address = f"{parts.scheme}://{parts.netloc}{parts.path}" + (" ?…" if parts.query else "")
            rows += [(_("Type"), _("remote server (HTTP)") if entry["transport"] == "http"
                      else _("remote server (SSE)"), ""),
                     (_("Address"), address, _("Query parameters are not shown: they may hold a token.")),
                     (_("Headers"), ", ".join(f"{k}: •••" for k in entry["headers"]),
                      _("Values are not shown: they may be keys or tokens."))]
        return rows

    def open_skills_details(self, path: str):
        """A skill set's page: which agents get it and how, every skill with its description."""
        skill_set = next((s for s in self.inventory.skill_sets if s.path == path), None)
        if skill_set is None:
            return
        if self.stack.get_visible_child_name() != "mcp":
            self.mcp_from = self.view if self.stack.get_visible_child_name() in ai_core.VIEWS else "main"
        self.mcp_title.set_label(_("Skills in {0}").format(path).upper())
        self.mcp_showing = None
        body = self.mcp_body
        while (child := body.get_first_child()) is not None:
            body.remove(child)
        names = {a.command: a.name for a in self.agent_list}
        ways = {"native": _("reads the folder by itself"), "listed": _("the folder is in its settings"),
                "links": _("links in its own folder ({0})")}
        rows = []
        for command in skill_set.agents:
            way = skills_ctl.how(command, path)
            text = ways.get(way, _("through a folder it reads"))
            if way == "links":
                text = text.format(skills_ctl.own_dir(command) or "")
            rows.append((names.get(command, command), text, ""))
        self.settings_heading(body, _("Used by"))
        self.detail_grid(body, rows)
        missing = [a.name for a in self.agent_list
                   if a.installed and a.command not in skill_set.agents and skills_ctl.own_dir(a.command)]
        if missing:
            self.settings_heading(body, _("Not set up"))
            self.note(body, ", ".join(missing) + " · "
                      + _("draw a line from the agent to this box on the map to add it."))
        self.settings_heading(body, ngettext("1 skill", "{0} skills", len(skill_set.skills))
                              .format(len(skill_set.skills)))
        folder = skills_ctl.expand(path)
        located = {p.name: p for p in skills_ctl.skills_of(folder)}
        self.detail_grid(body, [(name, (skills_ctl.description(located[name]) if name in located else "") or "—", "")
                                for name in skill_set.skills])
        self.stack.set_visible_child_name("mcp")
        self.update_header()

    def mcp_tools_section(self, body, server):
        """The tools (and prompts) the server offers: asked on request, kept with the date."""
        self.settings_heading(body, _("Tools"))
        name = server.name
        known = mcp_tools.cached(name)
        line = Gtk.Box(spacing=10)
        if name in self.asking_tools:
            text = _("Asking the server…")
        elif known is None:
            text = _("Not asked yet. Asking starts the server once (like an agent does) and stops it again.")
        elif known.get("status") != "ok":
            text = _("Asked on {0}: {1}").format(known.get("checked", "")[:16].replace("T", " "),
                                                 known.get("error", ""))
        else:
            count = len(known.get("tools", []))
            text = ngettext("Asked on {1}: 1 tool", "Asked on {1}: {0} tools", count).format(
                count, known.get("checked", "")[:16].replace("T", " "))
            if known.get("server"):
                text += f" · {known['server']}"
        label = Gtk.Label(label=text, xalign=0, wrap=True, max_width_chars=1, hexpand=True)
        label.add_css_class("am-agent-detail")
        line.append(label)
        button = Gtk.Button(label=_("Ask again") if known else _("Ask the server"), valign=Gtk.Align.CENTER)
        button.add_css_class("am-small-btn")
        button.set_sensitive(name not in self.asking_tools)
        button.connect("clicked", lambda *_: self.ask_mcp_tools(server))
        line.append(button)
        body.append(line)
        if known and known.get("status") == "ok":
            self.detail_grid(body, [(t["name"], t["description"] or "—", "") for t in known.get("tools", [])])
            if known.get("prompts"):
                self.settings_heading(body, _("Prompts"))
                self.detail_grid(body, [(t["name"], t["description"] or "—", "") for t in known["prompts"]])

    def ask_mcp_tools(self, server):
        name = server.name
        entry = None
        for command in server.agents:
            entry = mcp_ctl.normal(mcp_ctl.raw_entries(command).get(name))
            if entry is not None:
                break
        if entry is None or name in self.asking_tools:
            return
        self.asking_tools.add(name)
        self.open_mcp_details(name)

        def work():
            result = mcp_tools.ask_and_keep(name, entry)
            GLib.idle_add(done, result)

        def done(result):
            self.asking_tools.discard(name)
            if self.stack.get_visible_child_name() == "mcp" and getattr(self, "mcp_showing", None) == name:
                self.open_mcp_details(name)
            elif result.get("status") != "ok":
                self.show_note(_("{0}: {1}").format(name, result.get("error", "")))
            return False

        threading.Thread(target=work, daemon=True).start()

    # --- add a new MCP server: found on this PC, from the web, or by hand -----------------------

    def build_add_mcp_page(self) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        header = Gtk.Box(spacing=8)
        back = Gtk.Button()
        back.add_css_class("flat")
        back.add_css_class("am-header-btn")
        back.add_css_class("am-back-bordered")
        back_box = Gtk.Box(spacing=6)
        back_box.append(Gtk.Image.new_from_icon_name("go-previous-symbolic"))
        back_box.append(Gtk.Label(label=_("Back")))
        back_box.append(Gtk.Label(label="· Esc"))
        back.set_child(back_box)
        back.set_tooltip_text(_("Back without changing anything (Backspace, Esc)"))
        back.connect("clicked", lambda *_: self.close_add_mcp())
        header.append(back)
        title = Gtk.Label(label=_("Add an MCP server").upper(), xalign=0, hexpand=True)
        title.add_css_class("am-section-title")
        header.append(title)
        page.append(header)
        rule = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        rule.add_css_class("am-settings-rule")
        page.append(rule)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        body.add_css_class("am-settings")
        self.add_scroller = self.page_scroller(body)
        self.add_body = body
        page.append(self.add_scroller)

        # Found on this PC.
        top = Gtk.Box(spacing=8)
        heading = Gtk.Label(label=_("Found on this PC").upper(), xalign=0, hexpand=True)
        heading.add_css_class("am-setting-heading")
        top.append(heading)
        rescan = Gtk.Button(label=_("Scan again"))
        rescan.add_css_class("am-small-btn")
        rescan.connect("clicked", lambda *_: self.scan_mcp(force=True))
        top.append(rescan)
        body.append(top)
        self.add_found_status = Gtk.Label(xalign=0, wrap=True, max_width_chars=1)
        self.add_found_status.add_css_class("am-agent-detail")
        body.append(self.add_found_status)
        self.add_found_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        body.append(self.add_found_list)
        # AppImages are never run by the scan: looking inside them is the user's click.
        self.add_inside_row = Gtk.Box(spacing=8)
        self.add_inside_label = Gtk.Label(xalign=0, hexpand=True, wrap=True, max_width_chars=1)
        self.add_inside_label.add_css_class("am-agent-detail")
        self.add_inside_row.append(self.add_inside_label)
        self.add_inside = Gtk.Button(label=_("Look inside…"))
        self.add_inside.add_css_class("am-small-btn")
        self.add_inside.set_tooltip_text(_("Each AppImage is unpacked into a temporary folder, its programs are "
                                           "read, and the folder is removed again. Nothing is started."))
        self.add_inside.connect("clicked", lambda *_: self.look_inside_appimages())
        self.add_inside_row.append(self.add_inside)
        self.add_inside_row.set_visible(False)
        body.append(self.add_inside_row)

        # Search the web (the official MCP registry).
        self.settings_heading(body, _("Search the web"))
        line = Gtk.Box(spacing=8)
        self.add_query = Gtk.Entry(hexpand=True, placeholder_text=_("e.g. github, postgres, browser"))
        self.add_query.connect("activate", lambda *_: self.search_mcp())
        line.append(self.add_query)
        search = Gtk.Button(label=_("Search"))
        search.add_css_class("am-small-btn")
        search.connect("clicked", lambda *_: self.search_mcp())
        line.append(search)
        body.append(line)
        self.add_web_status = Gtk.Label(xalign=0, wrap=True, max_width_chars=1,
                                        label=_("From the official MCP registry. Anyone can publish there: "
                                                "check where a server comes from before adding it."))
        self.add_web_status.add_css_class("am-agent-detail")
        body.append(self.add_web_status)
        self.add_web_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        body.append(self.add_web_list)

        # The server.
        self.add_form_heading = Gtk.Label(label=_("Server").upper(), xalign=0)
        self.add_form_heading.add_css_class("am-setting-heading")
        body.append(self.add_form_heading)
        self.add_form = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        body.append(self.add_form)
        self.add_name = Gtk.Entry(placeholder_text="my-server")
        self.add_name.connect("changed", lambda *_: self.add_changed())
        self.setting_line(self.add_form, _("Name"), _("Letters, digits, - _ . (the agents know it by this name)"),
                          self.add_name)
        self.add_kind = Gtk.DropDown.new_from_strings([_("Program (stdio)"), _("Remote (HTTP)"),
                                                       _("Remote (SSE)")])
        self.add_kind.connect("notify::selected", lambda *_: self.add_kind_changed())
        self.setting_line(self.add_form, _("Type"), "", self.add_kind)
        self.add_command = Gtk.Entry(placeholder_text="npx -y @scope/server --root ~/Documents")
        self.add_command.connect("changed", lambda *_: self.add_changed())
        self.add_command_row = self.setting_line(self.add_form, _("Command"),
                                                 _("The program and its arguments, as in a terminal."),
                                                 self.add_command)
        self.add_url = Gtk.Entry(placeholder_text="https://example.com/mcp")
        self.add_url.connect("changed", lambda *_: self.add_changed())
        self.add_url_row = self.setting_line(self.add_form, _("Address"), "", self.add_url)
        self.add_pairs = Gtk.TextView(monospace=True, wrap_mode=Gtk.WrapMode.CHAR)
        self.add_pairs.add_css_class("am-diff")
        self.add_pairs.set_size_request(-1, 56)
        self.add_pairs.get_buffer().connect("changed", lambda *_: self.add_changed())
        self.add_pairs_row = self.setting_line(self.add_form, _("Environment"),
                                               _("One per line: NAME=value"), self.add_pairs)
        for wide in (self.add_command, self.add_url, self.add_pairs):
            wide.set_halign(Gtk.Align.FILL)     # setting_line puts controls at the start
        self.add_needs = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.add_form.append(self.add_needs)
        self.add_source = Gtk.Label(xalign=0, wrap=True, max_width_chars=1)
        self.add_source.add_css_class("am-agent-detail")
        self.add_form.append(self.add_source)
        self.add_agents = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=4,
                                      column_spacing=8, row_spacing=4)
        self.setting_line(self.add_form, _("For"), _("The agents that get it. More later: draw lines on the map."),
                          self.add_agents)
        self.add_agents.set_halign(Gtk.Align.FILL)

        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        self.add_test = Gtk.Button(label=_("Test"))
        self.add_test.add_css_class("am-small-btn")
        self.add_test.set_tooltip_text(_("Start it once and ask for its tools"))
        self.add_test.connect("clicked", lambda *_: self.test_new_mcp())
        buttons.append(self.add_test)
        self.add_ok = Gtk.Button(label=_("Add"))
        self.add_ok.add_css_class("am-small-btn")
        self.add_ok.add_css_class("am-primary")
        self.add_ok.connect("clicked", lambda *_: self.apply_new_mcp())
        buttons.append(self.add_ok)
        body.append(buttons)
        self.add_status = Gtk.Label(xalign=0, wrap=True, max_width_chars=1)
        self.add_status.add_css_class("am-agent-detail")
        body.append(self.add_status)
        heading = Gtk.Label(label=_("What changes").upper(), xalign=0)
        heading.add_css_class("am-group-heading")
        body.append(heading)
        self.add_preview = Gtk.TextView(editable=False, cursor_visible=False, monospace=True)
        self.add_preview.add_css_class("am-diff")
        self.add_preview.set_wrap_mode(Gtk.WrapMode.CHAR)
        body.append(self.add_preview)
        self.add_from = None       # {"appimage": path} or {"registry": Option} behind the form
        self.add_checks = {}
        self.add_need_entries = {}
        self.add_timer = 0
        return page

    def open_add_mcp(self):
        if self.stack.get_visible_child_name() != "addmcp":
            self.add_return = self.view if self.stack.get_visible_child_name() in ai_core.VIEWS else "main"
        flow = self.add_agents
        while (child := flow.get_first_child()) is not None:
            flow.remove(child)
        self.add_checks = {}
        selected = {i[6:] for i in self.selection if i.startswith("agent:")}
        for agent in self.agent_list:
            if agent.installed and mcp_ctl.supported(agent.command):
                check = self.small_check(agent.name)
                # Only an agent picked on the map is ticked: the user chooses, nothing is guessed.
                check.set_active(agent.command in selected)
                check.connect("toggled", lambda *_: self.add_changed())
                flow.append(check)
                self.add_checks[agent.command] = check
        self.add_kind_changed()
        self.stack.set_visible_child_name("addmcp")
        self.update_header()
        if self.found_mcp is None:
            self.scan_mcp()
        else:
            self.show_found_mcp()
            self.mark_seen("mcp")
        self.add_changed()

    def close_add_mcp(self):
        self.show_view(getattr(self, "add_return", self.view), save=False)

    # The three sources -------------------------------------------------------------------------

    def configured_programs(self) -> dict:
        found = {}
        for agent in self.agent_list:
            if not agent.installed or not mcp_ctl.supported(agent.command):
                continue
            for raw in mcp_ctl.raw_entries(agent.command).values():
                entry = mcp_ctl.normal(raw)
                if entry and entry["transport"] == "stdio":
                    program = shutil.which(entry["command"]) or entry["command"]
                    try:
                        found.setdefault(str(Path(program).resolve()), []).append(agent.command)
                    except OSError:
                        pass
        # Servers in the list that no agent uses: known too ("list" stands for the list).
        for entry in mcp_library.load().values():
            if entry["transport"] == "stdio":
                program = shutil.which(entry["command"]) or entry["command"]
                try:
                    found.setdefault(str(Path(program).resolve()), []).append("list")
                except OSError:
                    pass
        return found

    def scan_mcp(self, force: bool = False):
        if self.scanning_mcp:
            return
        self.scanning_mcp = True
        self.add_found_status.set_label(_("Looking for programs that offer an MCP server…"))
        configured = self.configured_programs()
        commands = [a.command for a in self.agent_list]

        def work():
            try:
                found = mcp_scan.scan(configured, commands)
            except OSError:
                found = []
            GLib.idle_add(done, found)

        def done(found):
            self.scanning_mcp = False
            self.found_mcp = found
            self.show_found_mcp()
            if self.stack.get_visible_child_name() == "addmcp":
                self.mark_seen("mcp")
            return False

        threading.Thread(target=work, daemon=True).start()

    def installed_mcp(self, candidate) -> bool:
        """Set up now: for an agent, or in the list of MCP servers (same program or same name).
        Worked out from the settings as they are, not from the scan (things change after it)."""
        if self.configured_now is None:
            self.configured_now = self.configured_programs()
        try:
            real = str(Path(candidate.program).resolve())
        except OSError:
            real = candidate.program
        return real in self.configured_now or any(m.name == candidate.name for m in self.inventory.mcp)

    def show_found_mcp(self):
        """Programs offering an MCP server that is not set up yet (the others are on the map)."""
        box = self.add_found_list
        while (child := box.get_first_child()) is not None:
            box.remove(child)
        found = self.found_mcp or []
        fresh = [c for c in found if not self.installed_mcp(c)]
        # Shown before mark_seen("mcp"), so the star marks what is new since the last look.
        seen_found = ai_core.load_settings().get("seen_found", {})
        seen = set(seen_found.get("mcp", [])) if isinstance(seen_found, dict) else set()
        if not found:
            text = _("Nothing found.")
        elif not fresh:
            text = _("Every program found here is set up already.")
        else:
            text = ngettext("1 program offers an MCP server that is not set up yet.",
                            "{0} programs offer an MCP server that is not set up yet.", len(fresh)).format(len(fresh))
        if found and len(found) > len(fresh):
            text += " " + ngettext("1 more is set up already.", "{0} more are set up already.",
                                   len(found) - len(fresh)).format(len(found) - len(fresh))
        self.add_found_status.set_label(text)
        pending = mcp_scan.not_looked_into()
        self.add_inside_row.set_visible(bool(pending))
        self.add_inside_label.set_label(ngettext(
            "1 AppImage not looked into yet ({1}).", "{0} AppImages not looked into yet ({1}).", len(pending))
            .format(len(pending), ", ".join(p.name for p in pending[:4]) + ("…" if len(pending) > 4 else "")))
        for candidate in sorted(fresh, key=lambda c: (c.program in seen, c.name)):
            where = Path(candidate.appimage).name if candidate.appimage else skills_ctl.tilde(candidate.program)
            if candidate.program not in seen:
                where = "★ " + _("new") + " · " + where
            self.add_found_list.append(self.choice_row(candidate.name, where,
                                                       lambda *_, c=candidate: self.use_found(c)))

    def look_inside_appimages(self):
        pending = mcp_scan.not_looked_into()
        if not pending:
            return
        self.add_inside.set_sensitive(False)
        self.add_inside_label.set_label(_("Looking inside {0} AppImages…").format(len(pending)))

        def work():
            mcp_scan.look_inside(pending)
            GLib.idle_add(done)

        def done():
            self.add_inside.set_sensitive(True)
            self.found_mcp = None
            self.scan_mcp()
            return False

        threading.Thread(target=work, daemon=True).start()

    def choice_row(self, title: str, detail: str, on_use) -> Gtk.Widget:
        """name ······ [Add…] on one line (a dotted line leads the eye to the
        button), the details under it."""
        row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        row.add_css_class("am-choice")
        top = Gtk.Box(spacing=8)
        name = Gtk.Label(label=title, xalign=0)
        name.add_css_class("am-detail-value")
        name.set_ellipsize(Pango.EllipsizeMode.END)
        name.set_max_width_chars(48)
        top.append(name)
        leader = Gtk.Box(hexpand=True, valign=Gtk.Align.CENTER)
        leader.add_css_class("am-leader")
        top.append(leader)
        use = Gtk.Button(label=_("Add…"), valign=Gtk.Align.CENTER)
        use.add_css_class("am-small-btn")
        use.connect("clicked", on_use)
        top.append(use)
        row.append(top)
        sub = Gtk.Label(label=detail, xalign=0, wrap=True, max_width_chars=1)
        sub.add_css_class("am-agent-detail")
        row.append(sub)
        return row

    def use_found(self, candidate):
        import shlex
        command = list(candidate.command)
        source = _("Program: {0}").format(skills_ctl.tilde(candidate.program))
        if candidate.appimage:
            folder = Path.home() / "Applications" / mcp_scan.appimage_stem(Path(candidate.appimage))
            command[0] = str(folder / "usr/bin" / Path(candidate.program).name)
            source = _("Unpacks {0} into {1} first (the server lives inside it).").format(
                Path(candidate.appimage).name, skills_ctl.tilde(folder))
        if candidate.usage:
            source += "\n" + _("Its usage: {0}").format(candidate.usage)
        self.add_from = {"appimage": candidate.appimage, "inner": candidate.inner} if candidate.appimage else None
        self.fill_add_form(candidate.name, 0, " ".join(shlex.quote(c) for c in command), "", "", [], source)

    def search_mcp(self):
        query = self.add_query.get_text().strip()
        if not query:
            return
        self.add_web_status.set_label(_("Searching…"))

        def work():
            try:
                result = mcp_registry.search(query)
                error = ""
            except (OSError, ValueError) as failure:
                result, error = [], str(failure)
            GLib.idle_add(done, result, error)

        def done(result, error):
            box = self.add_web_list
            while (child := box.get_first_child()) is not None:
                box.remove(child)
            self.add_web_status.set_label(_("Search failed: {0}").format(error) if error else
                                          ngettext("1 server found.", "{0} servers found.", len(result))
                                          .format(len(result)))
            for server in result[:20]:
                for option in server.options[:3]:
                    detail = " · ".join(x for x in (option.label, server.description) if x)
                    if not option.runnable:
                        detail += " · " + _("needs {0}, not installed").format(option.entry["command"])
                    box.append(self.choice_row(server.title or server.short, detail,
                                               lambda *_, s=server, o=option: self.use_registry(s, o)))
            return False

        threading.Thread(target=work, daemon=True).start()

    def use_registry(self, server, option):
        import shlex
        entry = option.entry
        source = _("From the MCP registry: {0}").format(server.name)
        if server.repository:
            source += " · " + server.repository
        self.add_from = {"registry": option}
        if entry["transport"] == "stdio":
            command = " ".join(shlex.quote(c) for c in [entry["command"], *entry["args"]])
            pairs = "\n".join(f"{k}={v}" for k, v in entry["env"].items() if not v.startswith("{"))
            self.fill_add_form(server.short, 0, command, "", pairs, option.needs, source)
        else:
            pairs = "\n".join(f"{k}: {v}" for k, v in entry["headers"].items() if not v.startswith("{"))
            self.fill_add_form(server.short, 1 if entry["transport"] == "http" else 2, "", entry["url"], pairs,
                               option.needs, source)

    def fill_add_form(self, name, kind, command, url, pairs, needs, source):
        self.add_filling = True
        self.add_name.set_text(name)
        self.add_kind.set_selected(kind)
        self.add_command.set_text(command)
        self.add_url.set_text(url)
        self.add_pairs.get_buffer().set_text(pairs)
        box = self.add_needs
        while (child := box.get_first_child()) is not None:
            box.remove(child)
        self.add_need_entries = {}
        for need in needs:
            entry = Gtk.Entry(hexpand=True, text=need.value or "", visibility=not need.secret,
                              placeholder_text=_("needed") if need.required else _("optional"))
            entry.connect("changed", lambda *_: self.add_changed())
            self.guard_text_field(entry)
            label = need.description if need.name.startswith("arg:") else need.name
            self.setting_line(box, label[:40], need.description, entry)
            entry.set_halign(Gtk.Align.FILL)
            self.add_need_entries[need.name] = (entry, need)
        self.add_source.set_label(source)
        self.add_filling = False
        self.add_kind_changed()
        self.add_status.set_label(_("Filled in below: tick the agents, then Test or Add."))
        self.add_timer and GLib.source_remove(self.add_timer)
        self.add_timer = GLib.timeout_add(1500, self.update_add_preview)    # the hint first, then why Add is off
        GLib.idle_add(self.scroll_to, self.add_scroller, self.add_body, self.add_form_heading, self.add_ok)

    # The form ------------------------------------------------------------------------------------

    def add_kind_changed(self):
        program = self.add_kind.get_selected() == 0
        self.add_command_row.set_visible(program)
        self.add_url_row.set_visible(not program)
        label = self.add_pairs_row.get_first_child()
        label.set_label(_("Environment") if program else _("Headers"))
        hint = _("One per line: NAME=value") if program else _("One per line: Name: value")
        label.set_tooltip_text(hint)
        self.add_pairs.set_tooltip_text(hint)
        self.add_changed()

    def add_changed(self):
        if getattr(self, "add_filling", False):
            return
        if self.add_timer:
            GLib.source_remove(self.add_timer)
        self.add_timer = GLib.timeout_add(300, self.update_add_preview)

    def new_mcp_entry(self) -> dict:
        """The form as an entry (mcp_ctl.normal shape); raises ValueError with a reason."""
        import shlex
        buffer = self.add_pairs.get_buffer()
        text = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False)
        values = {name: entry.get_text().strip() for name, (entry, _need) in self.add_need_entries.items()}
        missing = [need.description if name.startswith("arg:") else name
                   for name, (entry, need) in self.add_need_entries.items() if need.required and not values[name]]
        if missing:
            raise ValueError(_("Fill in: {0}").format(", ".join(missing)))
        kind = self.add_kind.get_selected()
        pairs = {}
        separator = "=" if kind == 0 else ":"
        for line in text.splitlines():
            if line.strip():
                key, sep, value = line.partition(separator)
                if not sep:
                    raise ValueError(_("Not NAME{0}value: {1}").format(separator, line.strip()))
                pairs[key.strip()] = value.strip()
        if kind == 0:
            try:
                parts = shlex.split(self.add_command.get_text())
            except ValueError as error:
                raise ValueError(str(error)) from error
            if not parts:
                raise ValueError(_("No command"))
            parts = [os.path.expanduser(p) if p.startswith("~") else p for p in parts]
            entry = {"transport": "stdio", "command": parts[0], "args": parts[1:], "env": pairs, "url": "",
                     "headers": {}}
            option = (self.add_from or {}).get("registry")
            if option is not None:
                entry["env"] = {**{k: v for k, v in option.entry["env"].items()}, **pairs}
        else:
            entry = {"transport": "http" if kind == 1 else "sse", "command": "", "args": [], "env": {},
                     "url": self.add_url.get_text().strip(), "headers": pairs}
            option = (self.add_from or {}).get("registry")
            if option is not None:
                entry["headers"] = {**option.entry["headers"], **pairs}
        entry = mcp_registry.fill(entry, values)
        # Optional fields left empty: their {placeholders} go.
        entry["env"] = {k: v for k, v in entry["env"].items() if not re.fullmatch(r"\{[^}]+\}", v)}
        entry["headers"] = {k: v for k, v in entry["headers"].items() if not re.fullmatch(r"\{[^}]+\}", v)}
        if mcp_registry.unfilled(entry):
            raise ValueError(_("Fill in: {0}").format(", ".join(mcp_registry.unfilled(entry))))
        return mcp_ctl.check_entry(entry)

    def new_mcp_plan(self):
        name = self.add_name.get_text().strip()
        if not mcp_ctl.NAME_RE.match(name):
            raise ValueError(_("Give it a name: letters, digits, - _ ."))
        agents = [c for c, check in self.add_checks.items() if check.get_active()]
        if not agents:
            raise ValueError(_("Choose at least one agent."))
        entry = self.new_mcp_entry()
        return connect.merge([mcp_ctl.plan(a, name, entry=entry) for a in agents]), entry, agents

    def update_add_preview(self):
        self.add_timer = 0
        try:
            plan_now, _entry, agents = self.new_mcp_plan()
            name = self.add_name.get_text().strip()
            taken = [self.agent_names().get(a, a) for a in agents if mcp_ctl.is_connected(a, name)]
            text = plan_now.diff() or _("Nothing changes: it is set up like this already.")
            if taken:
                text = _("Replaces the server {0} of {1}.").format(name, ", ".join(taken)) + "\n\n" + text
            if (self.add_from or {}).get("appimage"):
                text = self.add_source.get_label().split("\n")[0] + "\n\n" + text
            self.add_ok.set_sensitive(not plan_now.empty())
            self.add_ok.set_tooltip_text(None)
            if self.add_status.get_label().startswith("⚠"):
                self.add_status.set_label("")
        except (OSError, ValueError) as error:
            text = str(error)
            self.add_ok.set_sensitive(False)
            # Why Add is off, right under the button (the preview is further down).
            self.add_ok.set_tooltip_text(text)
            self.add_status.set_label("⚠ " + text)
        self.add_preview.get_buffer().set_text(text)
        return False

    def test_new_mcp(self):
        try:
            entry = self.new_mcp_entry()
        except ValueError as error:
            self.add_status.set_label(str(error))
            return
        found = (self.add_from or {})
        self.add_status.set_label(_("Asking the server…"))
        self.add_test.set_sensitive(False)

        def work():
            temp = None
            try:
                run = entry
                if found.get("appimage") and entry["transport"] == "stdio":
                    # Not unpacked yet: its usr/bin into a temporary folder, removed below.
                    temp, folder = mcp_scan.unpack_bin(Path(found["appimage"]))
                    run = {**entry, "command": str(folder / found["inner"])}
                result = mcp_tools.ask(run)
                text = ngettext("Works: {1} offers 1 tool.", "Works: {1} offers {0} tools.", len(result["tools"])) \
                    .format(len(result["tools"]), result["server"] or self.add_name.get_text())
            except (OSError, ValueError, RuntimeError, TimeoutError) as error:
                text = _("Does not answer: {0}").format(error)
            finally:
                if temp is not None:
                    shutil.rmtree(temp, ignore_errors=True)
            GLib.idle_add(done, text)

        def done(text):
            self.add_test.set_sensitive(True)
            self.add_status.set_label(text)
            return False

        threading.Thread(target=work, daemon=True).start()

    def apply_new_mcp(self):
        found = self.add_from or {}
        if found.get("appimage"):
            # Unpack first (a few seconds, in the background), then add.
            self.add_ok.set_sensitive(False)
            self.add_status.set_label(_("Unpacking {0}…").format(Path(found["appimage"]).name))

            def work():
                try:
                    mcp_scan.unpack(Path(found["appimage"]))
                    error = ""
                except (OSError, subprocess.SubprocessError) as failure:
                    error = str(failure)
                GLib.idle_add(done, error)

            def done(error):
                if error:
                    self.add_status.set_label(_("Not changed: {0}").format(error))
                    self.add_ok.set_sensitive(True)
                else:
                    self.add_from = None
                    self.write_new_mcp()
                return False

            threading.Thread(target=work, daemon=True).start()
            return
        self.write_new_mcp()

    def write_new_mcp(self):
        try:
            plan_now, _entry, agents = self.new_mcp_plan()
            backup = connect.apply(plan_now)
        except (OSError, ValueError, RuntimeError) as error:
            self.add_status.set_label(_("Not changed: {0}").format(error))
            self.add_ok.set_sensitive(True)
            return
        name = self.add_name.get_text().strip()
        self.found_mcp = None              # what is set up changed
        self.close_add_mcp()
        self.collect()
        self.refresh_overview()
        self.refresh_map()
        self.show_undo(_("{0} added for {1}").format(name, ", ".join(self.agent_names().get(a, a) for a in agents)),
                       backup)

    # --- new apps: programs with an MCP server, skill sets ----------------------------------------

    def new_found(self) -> tuple[list, list]:
        """(MCP programs, skill sets) found and not set up, that the add pages have not shown yet."""
        seen = ai_core.load_settings().get("seen_found", {})
        seen_mcp = set(seen.get("mcp", [])) if isinstance(seen, dict) else set()
        seen_skills = set(seen.get("skills", [])) if isinstance(seen, dict) else set()
        mcp = [c for c in self.found_mcp or [] if not self.installed_mcp(c) and c.program not in seen_mcp]
        skills = [s for s in self.found_skills or [] if s.path not in seen_skills]
        return mcp, skills

    def mark_seen(self, kind: str):
        seen = ai_core.load_settings().get("seen_found", {})
        seen = seen if isinstance(seen, dict) else {}
        current = [c.program for c in self.found_mcp or []] if kind == "mcp" else \
            [s.path for s in self.found_skills or []]
        merged = sorted(set(seen.get(kind, [])) | set(current))[-500:]
        if merged != sorted(seen.get(kind, [])):
            self.save_setting(seen_found={**seen, kind: merged})
            self.refresh_overview()
            self.refresh_map()

    def check_new_apps(self):
        """On every open, in the background (both scans are cached): new programs
        with an MCP server, new skill sets from apps."""
        if self.checking_new:
            return
        self.checking_new = True
        configured = self.configured_programs()
        commands = [a.command for a in self.agent_list]
        known = [s.path for s in self.inventory.skill_sets] + \
            [str(p) for a in self.agent_list if a.installed for p in skills_ctl.skill_dirs(a.command)]
        before = self.new_found()

        def work():
            mcp_scan.cleanup_old_cache()
            try:
                mcp = mcp_scan.scan(configured, commands)
            except OSError:
                mcp = self.found_mcp
            try:
                skills = skills_scan.scan(known)
            except OSError:
                skills = self.found_skills
            GLib.idle_add(done, mcp, skills)

        def done(mcp, skills):
            self.checking_new = False
            self.found_mcp, self.found_skills = mcp, skills
            new_mcp, new_skills = self.new_found()
            if len(new_mcp) + len(new_skills) > len(before[0]) + len(before[1]):
                names = [c.name for c in new_mcp] + [s.origin for s in new_skills]
                self.show_note(_("New on this PC: {0} · see + MCP server / + Skills").format(", ".join(names[:5])
                                                                                          + ("…" if len(names) > 5 else "")))
            self.refresh_overview()
            self.refresh_map()
            page = self.stack.get_visible_child_name()
            if page == "addskills":
                self.show_found_skills()
                self.mark_seen("skills")
            elif page == "addmcp" and not self.scanning_mcp:
                self.show_found_mcp()
                self.mark_seen("mcp")
            return False

        threading.Thread(target=work, daemon=True).start()

    @staticmethod
    def scroll_to(scroller, body, widget, focus=None):
        """Scroll a page so `widget` is at the top; focus `focus`."""
        point = widget.translate_coordinates(body, 0, 0)
        if point is not None:
            adjustment = scroller.get_vadjustment()
            adjustment.set_value(min(max(0, point[1] - 8), adjustment.get_upper() - adjustment.get_page_size()))
        if focus is not None:
            focus.grab_focus()
        return False

    def small_check(self, text: str) -> Gtk.CheckButton:
        """A check box with a label in the page's own size."""
        check = Gtk.CheckButton()
        label = Gtk.Label(label=text)
        label.add_css_class("am-detail-value")
        check.set_child(label)
        return check

    # --- add skills: found on this PC or from the web ------------------------------------------------

    def build_add_skills_page(self) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        header = Gtk.Box(spacing=8)
        back = Gtk.Button()
        back.add_css_class("flat")
        back.add_css_class("am-header-btn")
        back.add_css_class("am-back-bordered")
        back_box = Gtk.Box(spacing=6)
        back_box.append(Gtk.Image.new_from_icon_name("go-previous-symbolic"))
        back_box.append(Gtk.Label(label=_("Back")))
        back_box.append(Gtk.Label(label="· Esc"))
        back.set_child(back_box)
        back.set_tooltip_text(_("Back without changing anything (Backspace, Esc)"))
        back.connect("clicked", lambda *_: self.close_add_skills())
        header.append(back)
        title = Gtk.Label(label=_("Add skills").upper(), xalign=0, hexpand=True)
        title.add_css_class("am-section-title")
        header.append(title)
        page.append(header)
        rule = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        rule.add_css_class("am-settings-rule")
        page.append(rule)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        body.add_css_class("am-settings")
        scroller_holder = [self.page_scroller(body)]
        page.append(scroller_holder[0])

        self.settings_heading(body, _("Found on this PC"))
        self.skills_found_status = Gtk.Label(xalign=0, wrap=True, max_width_chars=1)
        self.skills_found_status.add_css_class("am-agent-detail")
        body.append(self.skills_found_status)
        self.skills_found_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        body.append(self.skills_found_list)

        self.settings_heading(body, _("Search the web"))
        line = Gtk.Box(spacing=8)
        self.skills_query = Gtk.Entry(hexpand=True, placeholder_text=_("e.g. pdf, docker, testing"))
        self.skills_query.connect("activate", lambda *_: self.search_skills())
        line.append(self.skills_query)
        search = Gtk.Button(label=_("Search"))
        search.add_css_class("am-small-btn")
        search.connect("clicked", lambda *_: self.search_skills())
        line.append(search)
        body.append(line)
        self.skills_web_status = Gtk.Label(xalign=0, wrap=True, max_width_chars=1,
                                           label=_("From the skills.sh directory; the files come from the skill's "
                                                   "GitHub repository. Skills can hold scripts an agent may run: "
                                                   "check where one comes from before adding it."))
        self.skills_web_status.add_css_class("am-agent-detail")
        body.append(self.skills_web_status)
        self.skills_web_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        body.append(self.skills_web_list)

        self.skills_selected_heading = Gtk.Label(label=_("Selected").upper(), xalign=0)
        self.skills_selected_heading.add_css_class("am-setting-heading")
        body.append(self.skills_selected_heading)
        self.skills_selected = Gtk.Label(xalign=0, wrap=True, max_width_chars=1, label=_("Nothing selected yet."))
        self.skills_selected.add_css_class("am-detail-value")
        body.append(self.skills_selected)
        self.skills_into = Gtk.DropDown.new_from_strings([""])
        self.skills_into.connect("notify::selected", lambda *_: self.update_skills_preview())
        self.skills_into_row = self.setting_line(body, _("Into"), _("The folder the skill goes to. Agents that read "
                                                                   "it get the skill at once."), self.skills_into)
        self.skills_agents = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=4,
                                         column_spacing=8, row_spacing=4)
        self.setting_line(body, _("Also for"), _("Agents that do not read this folder yet get it too."),
                          self.skills_agents)
        self.skills_agents.set_halign(Gtk.Align.FILL)
        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        self.skills_ok = Gtk.Button(label=_("Add"))
        self.skills_ok.add_css_class("am-small-btn")
        self.skills_ok.add_css_class("am-primary")
        self.skills_ok.connect("clicked", lambda *_: self.apply_skills())
        self.skills_scroller = scroller_holder[0]
        self.skills_body = body
        buttons.append(self.skills_ok)
        body.append(buttons)
        self.skills_status = Gtk.Label(xalign=0, wrap=True, max_width_chars=1)
        self.skills_status.add_css_class("am-agent-detail")
        body.append(self.skills_status)
        heading = Gtk.Label(label=_("What changes").upper(), xalign=0)
        heading.add_css_class("am-group-heading")
        body.append(heading)
        self.skills_preview = Gtk.TextView(editable=False, cursor_visible=False, monospace=True)
        self.skills_preview.add_css_class("am-diff")
        self.skills_preview.set_wrap_mode(Gtk.WrapMode.CHAR)
        body.append(self.skills_preview)
        self.skills_choice = None      # {"set": SkillSet} or {"web": Skill, "files": [...]}
        self.skills_targets = []
        self.skills_checks = {}
        return page

    def open_add_skills(self):
        if self.stack.get_visible_child_name() != "addskills":
            self.skills_return = self.view if self.stack.get_visible_child_name() in ai_core.VIEWS else "main"
        self.skills_choice = None
        self.skills_selected.set_label(_("Nothing selected yet."))
        self.skills_status.set_label("")
        # Target folders: the shared one first, then the user's own skill folders.
        targets = [skills_ctl.SHARED] + [s.path for s in self.inventory.skill_sets
                                         if s.path.startswith("~/") and s.path != skills_ctl.SHARED]
        self.skills_targets = list(dict.fromkeys(targets))
        readers = {}
        for agent in self.agent_list:
            if agent.installed:
                for target in self.skills_targets:
                    if skills_ctl.is_connected(agent.command, skills_ctl.expand(target)):
                        readers.setdefault(target, []).append(agent.name)
        labels = [f"{t} · " + _("read by {0}").format(", ".join(readers[t])) if readers.get(t) else t
                  for t in self.skills_targets]
        self.skills_into.set_model(Gtk.StringList.new(labels))
        self.skills_into.set_selected(0)
        flow = self.skills_agents
        while (child := flow.get_first_child()) is not None:
            flow.remove(child)
        self.skills_checks = {}
        for agent in self.agent_list:
            if agent.installed and skills_ctl.own_dir(agent.command):
                check = self.small_check(agent.name)
                check.connect("toggled", lambda *_: self.update_skills_preview())
                flow.append(check)
                self.skills_checks[agent.command] = check
        self.show_found_skills()
        self.mark_seen("skills")
        self.stack.set_visible_child_name("addskills")
        self.update_header()
        self.update_skills_preview()

    def close_add_skills(self):
        self.show_view(getattr(self, "skills_return", self.view), save=False)

    def show_found_skills(self):
        box = self.skills_found_list
        while (child := box.get_first_child()) is not None:
            box.remove(child)
        found = self.found_skills or []
        self.skills_found_status.set_label(
            _("Looking for skills that apps brought along…") if self.found_skills is None else
            _("No skills from apps that the agents do not have yet.") if not found else
            ngettext("1 skill folder from an app.", "{0} skill folders from apps.", len(found)).format(len(found)))
        for skill_set in found:
            detail = f"{skill_set.path} · " + ngettext("1 skill", "{0} skills", len(skill_set.skills)).format(
                len(skill_set.skills)) + ": " + ", ".join(skill_set.skills[:6])
            box.append(self.choice_row(skill_set.origin, detail, lambda *_, s=skill_set: self.use_skill_set(s)))
        if self.found_skills is None and not self.checking_new:
            self.check_new_apps()

    def use_skill_set(self, skill_set):
        self.skills_choice = {"set": skill_set}
        self.skills_selected.set_label(_("The skill folder {0} ({1}): agents ticked below get it.").format(
            skill_set.path, ", ".join(skill_set.skills[:8])))
        self.skills_into_row.set_visible(False)
        self.update_skills_preview()
        GLib.idle_add(self.scroll_to, self.skills_scroller, self.skills_body, self.skills_selected_heading,
                      self.skills_ok)

    def search_skills(self):
        query = self.skills_query.get_text().strip()
        if not query:
            return
        self.skills_web_status.set_label(_("Searching…"))

        def work():
            try:
                result, error = skills_web.search(query), ""
            except (OSError, ValueError) as failure:
                result, error = [], str(failure)
            GLib.idle_add(done, result, error)

        def done(result, error):
            box = self.skills_web_list
            while (child := box.get_first_child()) is not None:
                box.remove(child)
            self.skills_web_status.set_label(_("Search failed: {0}").format(error) if error else
                                             ngettext("1 skill found.", "{0} skills found.", len(result))
                                             .format(len(result)))
            for skill in result[:25]:
                detail = f"github.com/{skill.repo} · " + ngettext("1 install", "{0} installs", skill.installs) \
                    .format(f"{skill.installs:,}")
                box.append(self.choice_row(skill.name, detail, lambda *_, k=skill: self.use_web_skill(k)))
            return False

        threading.Thread(target=work, daemon=True).start()

    def use_web_skill(self, skill):
        self.skills_selected.set_label(_("Fetching the file list of {0}…").format(skill.name))
        GLib.idle_add(self.scroll_to, self.skills_scroller, self.skills_body, self.skills_selected_heading)
        self.skills_ok.set_sensitive(False)

        def work():
            try:
                files, error = skills_web.fetch(skill), ""
            except (OSError, ValueError) as failure:
                files, error = [], str(failure)
            GLib.idle_add(done, files, error)

        def done(files, error):
            if error:
                self.skills_selected.set_label(_("Could not fetch {0}: {1}").format(skill.name, error))
                return False
            self.skills_choice = {"web": skill, "files": files}
            scripts = [f for f, _d, _x in files if f.endswith((".py", ".sh", ".js", ".ts", ".rb", ".pl"))]
            text = f"{skill.name} · github.com/{skill.repo}\n{skills_web.description(files)}"
            if scripts:
                text += "\n" + ngettext("Holds 1 script an agent may run: {1}", "Holds {0} scripts an agent may run: {1}",
                                        len(scripts)).format(len(scripts), ", ".join(scripts[:6]))
            self.skills_selected.set_label(text)
            self.skills_into_row.set_visible(True)
            self.update_skills_preview()
            GLib.idle_add(self.scroll_to, self.skills_scroller, self.skills_body, self.skills_selected_heading,
                          self.skills_ok)
            return False

        threading.Thread(target=work, daemon=True).start()

    def skills_plan(self):
        choice = self.skills_choice
        if choice is None:
            raise ValueError(_("Choose a skill folder or a skill first."))
        agents = [c for c, check in self.skills_checks.items() if check.get_active()]
        plans = []
        if "set" in choice:
            folder = choice["set"].path
            if not agents:
                raise ValueError(_("Tick the agents that should get it."))
        else:
            folder = self.skills_targets[self.skills_into.get_selected()]
            skill = choice["web"]
            target = skills_ctl.expand(folder) / skill.skill_id
            if target.exists():
                raise ValueError(_("{0} is there already.").format(skills_ctl.tilde(target)))
            install = connect.Plan("", "", skill.skill_id)
            install.files_add = [(target / inner, data, executable) for inner, data, executable in choice["files"]]
            plans.append(install)
        for agent in agents:
            plan_now = skills_ctl.plan(agent, folder)
            if not plan_now.empty():
                plans.append(plan_now)
        if not plans:
            raise ValueError(_("Nothing changes: it is set up like this already."))
        return connect.merge(plans), folder, agents

    def update_skills_preview(self):
        try:
            plan_now, _folder, _agents = self.skills_plan()
            text = plan_now.diff()
            self.skills_ok.set_sensitive(True)
            self.skills_ok.set_tooltip_text(None)
            if self.skills_status.get_label().startswith("⚠"):
                self.skills_status.set_label("")
        except (OSError, ValueError) as error:
            text = str(error)
            self.skills_ok.set_sensitive(False)
            self.skills_ok.set_tooltip_text(text)
            self.skills_status.set_label("⚠ " + text)
        self.skills_preview.get_buffer().set_text(text)

    def apply_skills(self):
        try:
            plan_now, folder, agents = self.skills_plan()
            backup = connect.apply(plan_now)
        except (OSError, ValueError, RuntimeError) as error:
            self.skills_status.set_label(_("Not changed: {0}").format(error))
            return
        choice = self.skills_choice
        what = choice["web"].name if "web" in choice else choice["set"].path
        self.found_skills = None
        self.close_add_skills()
        self.collect()
        self.refresh_overview()
        self.refresh_map()
        self.show_undo(_("{0} added").format(what) + (f" · {folder}" if "web" in choice else ""), backup)

    def close_mcp_details(self):
        self.show_view(getattr(self, "mcp_from", self.view), save=False)

    def fill_skills(self, box, skill_set, names):
        self.detail_grid(box, [
            (_("Folder"), skill_set.path, ""),
            (_("Used by"), ", ".join(names.get(a, a) for a in skill_set.agents), ""),
        ])
        self.note(box, ", ".join(skill_set.skills))
        box.append(self.details_button(lambda *_: self.open_skills_details(skill_set.path)))

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

    def zoom_map(self, direction: int):
        """−1 out, +1 in, 0 back to 100 %."""
        self.side_map.set_zoom(1.0 if direction == 0 else self.side_map.zoom_step(direction))
        self.zoom_changed()

    def zoom_changed(self):
        self.zoom_label.set_label(f"{round(self.side_map.zoom * 100)} %")
        self.save_setting(map_zoom=self.side_map.zoom)

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
        self.side_map.set_layers(layers)
        self.set_selection([i for i in self.selection if i in set(self.map_view.selected)])
        self.update_layer_toggles()
        self.save_setting(map_layers=[l for l in graph.LAYERS if l in layers])

    def on_map_add(self, kind: str, node, rect):
        """A dashed "+" box on the map: add an MCP server, skills, or a model into that folder."""
        if kind == "mcp":
            self.open_add_mcp()
        elif kind == "skills":
            self.open_add_skills()
        elif kind == "model":
            runtimes = {f"runtime:{rt.key}": rt for rt in self.inventory.runtimes if rt.installed}
            owners = [runtimes[o] for o in node.owners if o in runtimes]
            if len(owners) == 1:
                self.open_models(owners[0])
            elif owners:
                self.pick_model_runtime(owners, rect)

    def pick_model_runtime(self, owners, rect):
        """Several local LLMs share the folder: which one downloads the model."""
        popover = Gtk.Popover()
        popover.set_parent(self.side_map)
        area = Gdk.Rectangle()
        area.x, area.y, area.width, area.height = (int(v) for v in rect)
        popover.set_pointing_to(area)
        popover.set_position(Gtk.PositionType.BOTTOM)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.add_css_class("am-provider-list")
        heading = Gtk.Label(label=_("Download with"), xalign=0)
        heading.add_css_class("am-group-heading")
        box.append(heading)
        for rt in owners:
            button = Gtk.Button(label=rt.name)
            button.add_css_class("am-small-btn")
            button.connect("clicked", lambda _b, r=rt: (popover.popdown(), self.open_models(r)))
            box.append(button)
        popover.set_child(box)
        popover.connect("closed", lambda p: GLib.idle_add(p.unparent))
        popover.popup()

    def column_list_groups(self, graph_now, column: int) -> list:
        """(heading, [(node, label)]) for a column's list: MCP servers and skill
        sets, agents, online and local providers, models by the folder they live in."""
        boxes = graph.listable(graph_now, column)
        by_name = lambda items: sorted(items, key=lambda item: item[1].lower())
        if column == 0:
            return [(_("MCP servers"), by_name((n, n.label) for n in boxes if n.kind == "mcp")),
                    (_("Skills"), by_name((n, n.label) for n in boxes if n.kind == "skills"))]
        if column == 1:
            return [(_("Agents"), by_name((n, n.label) for n in boxes))]
        if column == 2:
            def state(node):
                text = {"running": _("running"), "stopped": _("installed, not running"),
                        "missing": _("not installed")}.get(node.status, "")
                return f"{node.label}  ·  {text}" if text else node.label
            return [(_("Online"), by_name((n, state(n)) for n in boxes if n.kind == "cloud")),
                    (_("Local"), by_name((n, state(n)) for n in boxes if n.kind == "runtime"))]
        # Models: one group per folder (several local LLMs can share one), online ones together.
        by_id = {n.id: n for n in graph_now.nodes}
        provider = {e.target: by_id[e.source] for e in graph_now.edges if e.kind == "serves" and e.source in by_id}
        folders = {}
        for node in boxes:
            folders.setdefault(node.group, []).append(node)
        groups = []
        for folder in sorted(folders, key=lambda f: (not f, f.lower())):
            nodes = folders[folder]
            names = sorted({provider[n.id].label for n in nodes if n.id in provider}, key=str.lower)
            heading = " · ".join([folder] + names) if folder else _("Online")
            several = len(names) > 1 or not folder
            groups.append((heading, sorted(
                ((n, f"{n.label}  ·  {provider[n.id].label}" if several and n.id in provider else n.label)
                 for n in nodes), key=lambda item: (item[0].kind == "more", item[1].lower()))))
        return groups

    def show_column_list(self, view, column: int, rect):
        """A column heading's list: tick the boxes the map shows, a group at a time or one by one."""
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
        hidden = set(view.hidden_ids)
        checks = {}
        headings = []          # (heading check, member ids)
        syncing = [False]

        def sync_heading(heading, members):
            on = [checks[i].get_active() for i in members]
            syncing[0] = True
            heading.set_inconsistent(any(on) and not all(on))
            heading.set_active(all(on))
            syncing[0] = False

        def on_heading(heading, members):
            if syncing[0]:
                return
            shown = heading.get_active()
            syncing[0] = True
            heading.set_inconsistent(False)
            for i in members:
                checks[i].set_active(shown)
            syncing[0] = False
            self.set_nodes_shown(members, shown)

        def on_check(node_id, shown, heading, members):
            if syncing[0]:
                return
            self.set_nodes_shown([node_id], shown)
            sync_heading(heading, members)

        for title, items in self.column_list_groups(graph_now, column):
            if not items:
                continue
            members = [node.id for node, _label in items]
            heading = Gtk.CheckButton(label=title)
            heading.add_css_class("am-list-group")
            box.append(heading)
            for node, label in items:
                check = Gtk.CheckButton(label=label, margin_start=18)
                check.set_active(node.id not in hidden)
                check.connect("toggled", lambda b, i=node.id, h=heading, m=members: on_check(i, b.get_active(), h, m))
                box.append(check)
                checks[node.id] = check
            sync_heading(heading, members)
            heading.connect("toggled", lambda h, m=members: on_heading(h, m))
            headings.append((heading, members))

        def set_all(shown):
            syncing[0] = True
            for check in checks.values():
                check.set_active(shown)
            syncing[0] = False
            for heading, members in headings:
                sync_heading(heading, members)
            self.set_nodes_shown(list(checks), shown)

        row = Gtk.Box(spacing=6, halign=Gtk.Align.END, margin_top=6)
        for label, value in ((_("All"), True), (_("None"), False)):
            button = Gtk.Button(label=label)
            button.add_css_class("am-small-btn")
            button.connect("clicked", lambda _b, v=value: set_all(v))
            row.append(button)
        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, propagate_natural_height=True,
                                      propagate_natural_width=True, max_content_height=520)
        scroller.set_child(box)
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        outer.append(scroller)
        outer.append(row)
        popover.set_child(outer)
        popover.connect("closed", lambda p: GLib.idle_add(p.unparent))
        popover.popup()

    def set_nodes_shown(self, node_ids, shown: bool):
        hidden = set(self.map_view.hidden_ids)
        hidden.difference_update(node_ids) if shown else hidden.update(node_ids)
        for view in (self.side_map,):
            view.set_hidden_ids(hidden)
        self.set_selection([i for i in self.selection if i not in self.map_view.hidden()])
        # Only ids still on the map are kept (a removed model does not linger in the settings).
        ids = {n.id for n in self.map_view.graph.nodes} if self.map_view.graph else set()
        self.save_setting(hidden_nodes=sorted(i for i in hidden if i in ids), hidden_providers=None)

    def on_map_selection(self, nodes):
        """A click in either map."""
        self.set_selection([n.id for n in nodes])

    def update_selection_labels(self):
        by_id = {n.id: n for n in self.map_view.graph.nodes} if self.map_view.graph else {}
        nodes = [by_id[i] for i in self.selection if i in by_id]
        # With an agent selected, a click on a model of its local LLMs makes it the agent's model.
        pick_hint = " · " + _("click a model of its local LLM to use it") \
            if any(n.kind == "agent" for n in nodes) else ""
        if nodes:
            self.side_filter.set_label(_("Selected: {0} · hold Shift and click to add or remove one")
                                       .format(", ".join(n.label for n in nodes)) + pick_hint)
        else:
            self.side_filter.set_label(_("Click a card or a box to see only its connections; "
                                         "Shift+click selects more than one."))
        self.side_filter.set_tooltip_text(self.side_filter.get_label())
        self.map_show_all.set_visible(bool(nodes))

    def refresh_map(self):
        details = {a.command: agent_details.details_for(a.command) for a in self.agent_list if a.installed}
        labels = {"skills": lambda n: ngettext("1 skill", "{0} skills", n).format(n),
                  "more": lambda n: _("+{0} more").format(n)}
        new_mcp, new_skills = self.new_found()
        labels["unused"] = _("not used by any agent")
        labels["add_mcp"] = _("+ MCP server") + (" · " + _("{0} new").format(len(new_mcp)) if new_mcp else "")
        labels["add_skills"] = _("+ Skills") + (" · " + _("{0} new").format(len(new_skills)) if new_skills else "")
        labels["add_model"] = _("+ Model")
        built = graph.build(self.agent_list, details, self.inventory, labels)
        headers = [_("MCP servers · skills"), _("Agents"), _("LLM providers"), _("Models")]
        hidden = set(ai_core.hidden_nodes())
        for view in (self.side_map,):
            view.group_titles = {"mcp": _("MCP servers"), "skills": _("Skills"), "online": _("Online")}
            view.set_graph(built, headers)
            view.set_layers(self.map_view.layers)
            view.set_hidden_ids(hidden)
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
        on_settings = page in ("settings", "connect", "install", "models", "mcp", "addmcp", "addskills")
        # Faded out rather than hidden: the header keeps its height, so the
        # card does not jump when the settings page opens.
        for widget in (self.settings_button, self.view_switch):
            widget.set_opacity(0 if on_settings else 1)
            widget.set_can_target(not on_settings)
            widget.set_can_focus(not on_settings)
            widget.set_sensitive(not on_settings)
        # The sidebar button only where there is a sidebar: the Overview with the map beside the cards.
        self.sidebar_button.set_visible(page == "main" and self.split)
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

    def on_map_pick_model(self, agent_id: str, runtime_id: str, model: str):
        """A model clicked while an agent connected to its local LLM is selected:
        the agent uses it from now on (backup, Undo)."""
        agent = next((a for a in self.agent_list if f"agent:{a.command}" == agent_id), None)
        runtime = next((r for r in self.inventory.runtimes if f"runtime:{r.key}" == runtime_id), None)
        if agent is None or runtime is None:
            return
        if agent.command not in connect.SUPPORTED:
            self.show_note(_("{0} cannot be changed from here yet. Hermes, Pi and OpenCode can.").format(agent.name))
            return
        try:
            context = inventory.llama_context() if runtime.key == "llamacpp" else {}
            plan_now = connect.plan(agent.command, runtime, model, use_now=True, context=context)
            if not plan_now.changes:
                self.show_note(_("{0} uses {1} already.").format(agent.name, model))
                return
            backup = connect.apply(plan_now)
        except (OSError, ValueError, RuntimeError) as error:
            self.show_note(_("Not changed: {0}").format(error))
            return
        self.collect()
        self.refresh_overview()
        self.refresh_map()
        self.show_undo(_("{0} uses {1} ({2}) now").format(agent.name, model, runtime.name), backup)

    def on_map_link(self, agent_id: str, runtime_id: str, replaces_id: str | None = None,
                    remove_only: bool = False):
        """A line drawn from an agent to a local LLM: ask how, show what changes."""
        if runtime_id.startswith("mcp:"):
            self.open_mcp_connect(agent_id, runtime_id[4:], remove_only)
            return
        if runtime_id.startswith("skills:"):
            self.open_skills_connect(agent_id, runtime_id[7:], remove_only)
            return
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

    def open_mcp_connect(self, agent_id: str, name: str, remove_only: bool = False):
        """A line drawn from an agent to an MCP server: copy the server's settings
        from an agent that has it; or (line dragged off) remove it."""
        agent = next((a for a in self.agent_list if f"agent:{a.command}" == agent_id), None)
        server = next((m for m in self.inventory.mcp if m.name == name), None)
        if agent is None or server is None:
            return
        already = mcp_ctl.is_connected(agent.command, name)
        self.connecting = {"agent": agent, "mcp": name, "sources": list(server.agents), "from": self.view,
                           "remove_only": remove_only}
        self.connect_title.set_label((_("Remove {0} from {1}") if remove_only else _("Connect {0} to {1}"))
                                     .format(name, agent.name).upper())
        form = self.connect_form
        while (child := form.get_first_child()) is not None:
            form.remove(child)
        supported = mcp_ctl.supported(agent.command)
        self.connect_preview_heading.set_visible(supported)
        self.connect_preview_scroller.set_visible(supported)
        self.connect_status.set_label("")
        # As for a local LLM: the red "Remove connection" removes, "Connect" adds.
        self.connect_remove.set_visible(supported and already)
        self.connect_ok.set_visible(supported and not remove_only and not already)
        self.connect_ok.set_label(_("Connect"))
        if not supported:
            self.connect_intro.set_label(_("MCP servers cannot be set up for {0} from here yet.").format(agent.name))
        elif remove_only:
            self.connect_intro.set_label(_("The MCP server {0} is removed from {1}'s settings. The other agents keep it.")
                                         .format(name, agent.name))
        elif already:
            self.connect_intro.set_label(_("{0} has the MCP server {1} already.").format(agent.name, name))
        else:
            source, entry = mcp_ctl.entry_from([a for a in server.agents if a != agent.command], name)
            if entry is None:
                entry = mcp_library.load().get(name)
                self.connect_intro.set_label(
                    _("{0} gets the MCP server {1} ({2}) again, with the settings it had. "
                      "Nothing else in its settings changes.").format(agent.name, name, mcp_ctl.describe(entry)))
            else:
                source_name = next((a.name for a in self.agent_list if a.command == source), source or "?")
                self.connect_intro.set_label(
                    _("{0} gets the MCP server {1} ({2}), with the settings {3} uses for it. "
                      "Nothing else in its settings changes.").format(agent.name, name, mcp_ctl.describe(entry),
                                                                       source_name))
        if supported:
            self.update_connect_preview()
        self.stack.set_visible_child_name("connect")

    def open_skills_connect(self, agent_id: str, folder: str, remove_only: bool = False):
        """A line drawn from an agent to a skill set: the agent gets the folder
        (settings list or one link per skill); or (line dragged off) loses it."""
        agent = next((a for a in self.agent_list if f"agent:{a.command}" == agent_id), None)
        if agent is None:
            return
        self.connecting = {"agent": agent, "skills": folder, "from": self.view, "remove_only": remove_only}
        self.connect_title.set_label((_("Remove {0} from {1}") if remove_only else _("Connect {0} to {1}"))
                                     .format(folder, agent.name).upper())
        form = self.connect_form
        while (child := form.get_first_child()) is not None:
            form.remove(child)
        self.connect_status.set_label("")
        try:
            plan_now = skills_ctl.plan(agent.command, folder, remove=True)
            already = plan_now.connected
            how = skills_ctl.plan(agent.command, folder) if not already else None
        except (OSError, ValueError):
            plan_now, already, how = None, False, None
        supported = plan_now is not None
        removable = supported and not plan_now.empty()
        stays = ", ".join(plan_now.notes) if supported else ""
        self.connect_preview_heading.set_visible(supported)
        self.connect_preview_scroller.set_visible(supported)
        self.connect_remove.set_visible(already and removable)
        self.connect_ok.set_visible(supported and not remove_only and not already)
        self.connect_ok.set_label(_("Connect"))
        if not supported:
            text = _("Skills cannot be set up for {0} from here yet.").format(agent.name)
        elif remove_only or already:
            text = (_("{0} no longer gets the skills in {1}.") if removable and not stays else
                    _("{0} has the skills in {1}.") if not remove_only else "").format(agent.name, folder)
            if stays:
                text = (text + " " if text else "") + _(
                    "{0} reads {1} by itself, so these skills stay.").format(
                        agent.name, stays)
        elif agent.command in skills_ctl.SETTINGS_LIST:
            text = _("{0} gets the folder {1} in its settings: all its skills, also ones added later.").format(
                agent.name, folder)
        else:
            text = ngettext("{0} gets a link to the skill in {1} in its own folder ({2}).",
                            "{0} gets links to the {3} skills in {1} in its own folder ({2}).",
                            len(how.links_add) if how else 0).format(
                agent.name, folder, skills_ctl.own_dir(agent.command) or "", len(how.links_add) if how else 0)
        self.connect_intro.set_label(text)
        if supported:
            self.update_connect_preview()
        self.stack.set_visible_child_name("connect")

    def open_mcp_delete(self, name: str):
        """The card's Remove: the server goes from every agent and from the list."""
        server = next((m for m in self.inventory.mcp if m.name == name), None)
        if server is None:
            return
        names = self.agent_names()
        users = [a for a in server.agents if mcp_ctl.supported(a)]
        self.connecting = {"delete": name, "users": users, "from": self.view, "remove_only": True}
        self.connect_title.set_label(_("Remove {0}").format(name).upper())
        form = self.connect_form
        while (child := form.get_first_child()) is not None:
            form.remove(child)
        self.connect_status.set_label("")
        self.connect_preview_heading.set_visible(True)
        self.connect_preview_scroller.set_visible(True)
        self.connect_ok.set_visible(False)
        self.connect_remove.set_visible(True)
        text = (_("The MCP server {0} is removed from {1} and from the list of MCP servers.").format(
            name, ", ".join(names.get(a, a) for a in users)) if users else
            _("The MCP server {0} is removed from the list of MCP servers.").format(name))
        others = [names.get(a, a) for a in server.agents if a not in users]
        if others:
            text += " " + _("{0} keeps it: change it there.").format(", ".join(others))
        self.connect_intro.set_label(text + " " + _("Undo brings it back."))
        self.update_connect_preview()
        self.stack.set_visible_child_name("connect")

    def current_connect_plan(self, remove: bool = False):
        info = self.connecting
        if "delete" in info:
            plans = [mcp_ctl.plan(a, info["delete"], remove=True) for a in info["users"]]
            listed = connect.Plan("", "", info["delete"])
            change = mcp_library.change(info["delete"], None)
            listed.changes = [change] if change else []
            return connect.merge(plans + [listed])
        if "skills" in info:
            return skills_ctl.plan(info["agent"].command, info["skills"], remove=remove or info["remove_only"])
        if "mcp" in info:
            return mcp_ctl.plan(info["agent"].command, info["mcp"], info["sources"],
                                remove=remove or info["remove_only"])
        models = self.connect_models
        model = models[self.connect_model.get_selected()] if models else ""
        return connect.plan(info["agent"].command, info["runtime"], model, self.connect_use_now.get_active(),
                            remove=remove, context=self.connect_context,
                            replaces=None if remove else info.get("replaces"))

    def update_connect_preview(self):
        try:
            plan_now = self.current_connect_plan(remove=bool(self.connecting.get("remove_only")))
            text = plan_now.diff() or _("Nothing changes: it is set up like this already.")
            self.connect_ok.set_sensitive(not plan_now.empty())
        except (OSError, ValueError) as error:
            text = _("Cannot read the agent's settings: {0}").format(error)
            self.connect_ok.set_sensitive(False)
        self.connect_preview.get_buffer().set_text(text)

    def apply_connect(self, remove: bool):
        info = self.connecting
        try:
            plan_now = self.current_connect_plan(remove=remove)
            if plan_now.empty():
                self.close_connect()
                return
            backup = connect.apply(plan_now)
        except (OSError, ValueError, RuntimeError) as error:
            self.connect_status.set_label(_("Not changed: {0}").format(error))
            return
        remove = remove or bool(info.get("remove_only"))
        if "delete" in info:
            self.close_connect()
            self.collect()
            self.refresh_overview()
            self.refresh_map()
            self.show_undo(_("{0} removed").format(info["delete"]), backup)
            return
        target = info.get("mcp") or info.get("skills") or info["runtime"].name
        message = (_("{0}: {1} removed") if remove else _("{0} connected to {1}")).format(
            info["agent"].name, target)
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

    # --- models of a local LLM (right-click → Models…) ---------------------------------------------

    def build_models_page(self) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        header = Gtk.Box(spacing=8)
        back = Gtk.Button()
        back.add_css_class("flat")
        back.add_css_class("am-header-btn")
        back.add_css_class("am-back-bordered")
        back_box = Gtk.Box(spacing=6)
        back_box.append(Gtk.Image.new_from_icon_name("go-previous-symbolic"))
        back_box.append(Gtk.Label(label=_("Back")))
        back_box.append(Gtk.Label(label="· Esc"))
        back.set_child(back_box)
        back.set_tooltip_text(_("Back (Esc)"))
        back.connect("clicked", lambda *_: self.close_models())
        header.append(back)
        self.models_title = Gtk.Label(xalign=0, hexpand=True)
        self.models_title.add_css_class("am-section-title")
        header.append(self.models_title)
        page.append(header)
        rule = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        rule.add_css_class("am-settings-rule")
        page.append(rule)
        self.models_intro = Gtk.Label(xalign=0, wrap=True, max_width_chars=1)
        self.models_intro.add_css_class("am-detail-value")
        page.append(self.models_intro)

        body = Gtk.Box(spacing=24, vexpand=True)
        # Left: what is there.
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        left.set_size_request(LIST_PANE_WIDTH, -1)
        self.models_have_heading = Gtk.Label(xalign=0)
        self.models_have_heading.add_css_class("am-group-heading")
        left.append(self.models_have_heading)
        self.models_have = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        have_scroller = Gtk.ScrolledWindow(vexpand=True)
        have_scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        have_scroller.set_child(self.models_have)
        left.append(have_scroller)
        body.append(left)
        # Right: get more.
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, hexpand=True)
        heading = Gtk.Label(label=_("Download").upper(), xalign=0)
        heading.add_css_class("am-group-heading")
        right.append(heading)
        self.models_pull_row = Gtk.Box(spacing=8)
        self.models_pull_entry = Gtk.Entry(hexpand=True, max_length=200,
                                           placeholder_text=_("Ollama library name, e.g. qwen3:8b or gemma3:12b"))
        self.models_pull_entry.add_css_class("am-path")
        self.models_pull_entry.connect("activate", lambda *_: self.start_model_download(pull=True))
        self.models_pull_row.append(self.models_pull_entry)
        pull = Gtk.Button(label=_("Download"))
        pull.add_css_class("am-small-btn")
        pull.connect("clicked", lambda *_: self.start_model_download(pull=True))
        self.models_pull_row.append(pull)
        right.append(self.models_pull_row)
        search_row = Gtk.Box(spacing=8)
        self.models_search = Gtk.Entry(hexpand=True, max_length=100,
                                       placeholder_text=_("Search Hugging Face (GGUF), e.g. qwen3 coder"))
        self.models_search.add_css_class("am-path")
        self.models_search.connect("activate", lambda *_: self.search_models())
        search_row.append(self.models_search)
        find = Gtk.Button(label=_("Search"))
        find.add_css_class("am-small-btn")
        find.connect("clicked", lambda *_: self.search_models())
        search_row.append(find)
        right.append(search_row)
        lists = Gtk.Box(spacing=12, vexpand=True)
        self.models_repos = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.models_repos.add_css_class("am-list")
        self.models_repos.connect("row-selected", lambda _l, row: row is not None and self.show_model_files(row.repo))
        repo_scroller = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        repo_scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        repo_scroller.set_child(self.models_repos)
        lists.append(repo_scroller)
        self.models_files = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.models_files.add_css_class("am-list")
        self.models_files.connect("row-selected", lambda *_: self.update_model_buttons())
        file_scroller = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        file_scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        file_scroller.set_child(self.models_files)
        lists.append(file_scroller)
        right.append(lists)
        self.models_bar = Gtk.ProgressBar()
        self.models_bar.add_css_class("am-progress")
        right.append(self.models_bar)
        self.models_status = Gtk.Label(xalign=0, wrap=True, max_width_chars=1)
        self.models_status.add_css_class("am-agent-detail")
        right.append(self.models_status)
        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        self.models_cancel = Gtk.Button(label=_("Cancel"))
        self.models_cancel.add_css_class("am-small-btn")
        self.models_cancel.connect("clicked", lambda *_: self.cancel_model_download())
        buttons.append(self.models_cancel)
        self.models_get = Gtk.Button(label=_("Download"))
        self.models_get.add_css_class("am-small-btn")
        self.models_get.add_css_class("am-primary")
        self.models_get.connect("clicked", lambda *_: self.start_model_download(pull=False))
        buttons.append(self.models_get)
        right.append(buttons)
        body.append(right)
        page.append(body)
        self.models_state = None
        return page

    def open_models(self, rt):
        self.models_state = {"rt": rt, "busy": False, "cancel": False, "from": self.view, "repo": None,
                             "vram": install_ctl.detect_gpu().vram}
        self.models_title.set_label(_("Models of {0}").format(rt.name).upper())
        folder = rt.models_dir or "—"
        self.models_intro.set_label(
            _("Model folder: {0}. Pick a model on Hugging Face, then a size: the one marked fits your graphics "
              "card with room for context.").format(folder) +
            (" " + _("Ollama is started for a download if it is not running.") if rt.key == "ollama" else ""))
        self.models_pull_row.set_visible(rt.key == "ollama")
        self.models_status.set_label("")
        self.models_bar.set_fraction(0)
        self.fill_model_list(rt)
        for listbox in (self.models_repos, self.models_files):
            while (row := listbox.get_first_child()) is not None:
                listbox.remove(row)
        self.update_model_buttons()
        self.stack.set_visible_child_name("models")
        self.models_search.grab_focus()

    def fill_model_list(self, rt):
        """The installed models, each with its context now and the best one for the card."""
        box = self.models_have
        while (child := box.get_first_child()) is not None:
            box.remove(child)
        self.models_have_heading.set_label(
            ngettext("1 model installed", "{0} models installed", len(rt.models)).format(len(rt.models)).upper())
        rows = {}
        for name in rt.models:
            row = Gtk.Box(spacing=6)
            label = Gtk.Label(label=name, xalign=0, hexpand=True)
            label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
            label.set_max_width_chars(1)
            label.add_css_class("am-detail-value")
            row.append(label)
            info = Gtk.Label(xalign=1)
            info.add_css_class("am-agent-detail")
            row.append(info)
            button = Gtk.Button(label=_("Set"))
            button.add_css_class("am-small-btn")
            button.set_visible(False)
            row.append(button)
            box.append(row)
            rows[name] = (info, button)
        state = self.models_state
        vram = state["vram"] if state else 0

        def work():
            results = {}
            for name in rt.models:
                try:
                    results[name] = self.context_for(rt, name, vram)
                except (OSError, ValueError, KeyError, struct.error) as problem:
                    results[name] = (None, None, str(problem))
            GLib.idle_add(done, results)

        def done(results):
            if self.models_state is not state:
                return False
            for name, (now, fit_info, error) in results.items():
                info, button = rows[name]
                if fit_info is None:
                    info.set_label(_("context: ?"))
                    info.set_tooltip_text(error or "")
                    continue
                best = fit_info.best
                if not fit_info.known:
                    info.set_label(_("now {0}").format(context_ctl.short_context(now) if now else _("default")))
                    info.set_tooltip_text(_("Not a chat model, or its file does not say how much memory its context needs."))
                    continue
                info.set_label(_("now {0} · best {1}").format(context_ctl.short_context(now) if now else _("default"),
                                                               context_ctl.short_context(best) if best else _("too big")))
                info.set_tooltip_text(_("Trained for {0} tokens; each token of context takes {1} of video memory.")
                                      .format(fit_info.native or "?", size_text(fit_info.per_token)))
                if best and best != now and rt.key in ("llamacpp", "ollama"):
                    button.set_visible(True)
                    button.set_tooltip_text(_("Set the context of {0} to {1} tokens.").format(name, best))
                    button.connect("clicked", lambda _b, n=name, c=best: self.set_model_context(rt, n, c))
            return False

        threading.Thread(target=work, daemon=True).start()

    def context_for(self, rt, name: str, vram: int):
        """(context now, Fit, error) for one installed model."""
        folder = Path(os.path.expanduser(rt.models_dir)) if rt.models_dir else None
        if folder is None:
            return None, None, "no model folder"
        if rt.key == "llamacpp":
            now, cache = context_ctl.llama_settings(name)
            model_file = context_ctl.llama_model_file(folder, name)
        elif rt.key == "ollama":
            env = install_ctl.read_env(install_ctl.Ollama.env_file)
            cache = env.get("OLLAMA_KV_CACHE_TYPE", "f16")
            default = int(env.get("OLLAMA_CONTEXT_LENGTH", "0") or 0)
            now = context_ctl.ollama_context(folder, name, default)
            model_file = context_ctl.ollama_model_file(folder, name)
        else:
            return None, None, _("set the context in LM Studio itself")
        if model_file is None:
            return now, None, "model file not found"
        return now, context_ctl.fit(model_file, vram, cache), None

    def set_model_context(self, rt, name: str, ctx: int):
        state = self.models_state
        self.models_status.set_label(_("{0}: context → {1}…").format(name, ctx))

        def work():
            try:
                backup = None
                if rt.key == "llamacpp":
                    # Like a connection: written with a backup, and Undo puts it back.
                    ini = context_ctl.MODELS_INI
                    before = connect._read(ini)
                    change = connect.Change(ini, before, context_ctl.llama_context_text(before, name, ctx))
                    backup = connect.apply(connect.Plan("llama.cpp", "llamacpp", "", [change]))
                else:
                    context_ctl.ollama_set_context(name, ctx, rt.address)
                error = None
            except (OSError, ValueError, RuntimeError) as problem:
                error, backup = str(problem), None
            GLib.idle_add(done, error, backup)

        def done(error, backup):
            if self.models_state is not state:
                return False
            if backup is not None:
                self.show_undo(_("{0}: context set to {1}").format(name, ctx), backup)
            if error:
                self.models_status.set_label(_("Not changed: {0}").format(error))
                return False
            note = _(" Restart llama.cpp for it to take effect.") if rt.key == "llamacpp" and rt.running else ""
            self.models_status.set_label(_("{0}: context set to {1}").format(name, ctx) + "." + note)
            self.fill_model_list(state["rt"])
            return False

        threading.Thread(target=work, daemon=True).start()

    def list_row(self, title: str, detail: str, mark: str = "") -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        line = Gtk.Box(spacing=8, margin_start=6, margin_end=6, margin_top=3, margin_bottom=3)
        name = Gtk.Label(label=title, xalign=0, hexpand=True)
        name.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        name.set_max_width_chars(1)
        name.add_css_class("am-detail-value")
        line.append(name)
        if mark:
            badge = Gtk.Label(label=mark)
            badge.add_css_class("am-agent-badge")
            line.append(badge)
        info = Gtk.Label(label=detail, xalign=1)
        info.add_css_class("am-agent-detail")
        line.append(info)
        row.set_child(line)
        return row

    def search_models(self):
        state = self.models_state
        query = self.models_search.get_text().strip()
        if state is None or not query:
            return
        self.models_status.set_label(_("Searching…"))

        def work():
            try:
                result, error = models_ctl.search(query), None
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as problem:
                result, error = [], str(problem)
            GLib.idle_add(done, result, error)

        def done(repos, error):
            if self.models_state is not state:
                return False
            for listbox in (self.models_repos, self.models_files):
                while (row := listbox.get_first_child()) is not None:
                    listbox.remove(row)
            for repo in repos:
                row = self.list_row(repo.id, _("{0} downloads").format(short_number(repo.downloads)))
                row.repo = repo.id
                self.models_repos.append(row)
            self.models_status.set_label(error or ("" if repos else _("Nothing found.")))
            self.update_model_buttons()
            return False

        threading.Thread(target=work, daemon=True).start()

    def show_model_files(self, repo: str):
        state = self.models_state
        state["repo"] = repo
        self.models_status.set_label(_("Reading the files of {0}…").format(repo))

        def work():
            try:
                result, error = models_ctl.files(repo, state["vram"]), None
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as problem:
                result, error = [], str(problem)
            GLib.idle_add(done, result, error)

        def done(entries, error):
            if self.models_state is not state or state["repo"] != repo:
                return False
            listbox = self.models_files
            while (row := listbox.get_first_child()) is not None:
                listbox.remove(row)
            pick = None
            for entry in entries:
                mark = _("recommended") if entry.recommended else ""
                detail = size_text(entry.size) + ("" if entry.fits else "  · " + _("too big for the card"))
                row = self.list_row(entry.quant or entry.name, detail, mark)
                row.entry = entry
                row.set_tooltip_text(", ".join(entry.paths))
                listbox.append(row)
                if entry.recommended:
                    pick = row
            if pick is not None:
                listbox.select_row(pick)
            self.models_status.set_label(error or ("" if entries else _("No GGUF files in {0}.").format(repo)))
            self.update_model_buttons()
            return False

        threading.Thread(target=work, daemon=True).start()

    def update_model_buttons(self):
        state = self.models_state
        busy = bool(state and state["busy"])
        row = self.models_files.get_selected_row()
        self.models_get.set_sensitive(not busy and row is not None and state is not None
                                      and state["rt"].key in ("ollama", "llamacpp", "lmstudio"))
        self.models_cancel.set_label(_("Stop download") if busy else _("Back"))

    def start_model_download(self, pull: bool):
        state = self.models_state
        if state is None or state["busy"]:
            return
        rt = state["rt"]
        entry = None
        if pull:
            name = self.models_pull_entry.get_text().strip()
            if not name:
                return
        else:
            row = self.models_files.get_selected_row()
            if row is None:
                return
            entry = row.entry
            name = models_ctl.ollama_name_for(state["repo"], entry) if rt.key == "ollama" else ""
        if rt.key != "ollama" and not rt.models_dir:
            self.models_status.set_label(_("{0} has no model folder yet.").format(rt.name))
            return
        state["busy"], state["cancel"] = True, False
        self.update_model_buttons()

        def progress(fraction, text):
            GLib.idle_add(show, fraction, text)

        def show(fraction, text):
            self.models_bar.set_fraction(max(0.0, min(1.0, fraction)))
            if text:
                self.models_status.set_label(text)
            elif entry is not None:
                self.models_status.set_label(_("{0}: {1} of {2}").format(
                    entry.name, size_text(int(fraction * entry.size)), size_text(entry.size)))
            return False

        def work():
            try:
                if rt.key == "ollama":
                    if not rt.running:
                        progress(0.0, _("Starting Ollama…"))
                        runtime_ctl.start(rt)
                        for _attempt in range(40):
                            if models_ctl.loopback_listeners(int(rt.address.rsplit(":", 1)[-1])):
                                break
                            time.sleep(0.5)
                    models_ctl.ollama_pull(name, rt.address, progress, lambda: state["cancel"])
                else:
                    models_ctl.download_gguf(rt.key, Path(os.path.expanduser(rt.models_dir)), state["repo"],
                                             entry, progress, lambda: state["cancel"])
                error = None
            except install_ctl.Cancelled:
                error = _("stopped")
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as problem:
                error = str(problem)
            GLib.idle_add(finished, error)

        def finished(error):
            state["busy"] = False
            self.update_model_buttons()
            if error:
                self.models_status.set_label(_("Not downloaded: {0}").format(error))
                return False
            self.models_bar.set_fraction(1.0)
            self.models_status.set_label(_("Downloaded: {0}").format(entry.name if entry else name))
            self.collect()
            fresh = next((r for r in self.inventory.runtimes if r.key == rt.key), rt)
            state["rt"] = fresh
            self.fill_model_list(fresh)
            self.refresh_overview()
            self.refresh_map()
            return False

        threading.Thread(target=work, daemon=True).start()

    def cancel_model_download(self):
        state = self.models_state
        if state is not None and state["busy"]:
            state["cancel"] = True
            return
        self.close_models()

    def close_models(self):
        state = self.models_state
        if state is not None and state["busy"]:
            return                     # downloading: "Stop download" first
        self.models_state = None
        self.show_view(state["from"] if state else self.view, save=False)

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
        self.guard_text_field(entry)
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
                           "cancel": False, "busy": False, "from": self.view, "build": ""}
        self.install_build = None
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
            self.add_build_choice(form, inst)
            if inst.has_service:
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
            self.add_build_choice(form, inst)
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

    def add_build_choice(self, form, inst):
        """llama.cpp comes in builds per graphics interface; the one for this card is preselected."""
        self.install_build = None
        if not inst.builds:
            return
        gpu = install_ctl.detect_gpu()
        keys = [key for key, _label in inst.builds]
        current = self.installing.get("build") or inst.default_build(gpu)
        current = current if current in keys else keys[0]
        self.installing["build"] = current
        notes = {"vulkan": _("Vulkan: any AMD or NVIDIA card, nothing else needed."),
                 "rocm": _("ROCm: AMD, needs ROCm installed (/opt/rocm)."),
                 "cuda": _("CUDA: NVIDIA."), "cpu": _("CPU only: slow, for a test.")}
        self.settings_ready = False
        drop = self.dropdown([label for _key, label in inst.builds], keys.index(current),
                             lambda i: self.choose_build(keys[i]))
        self.setting_line(form, _("Build"), " ".join(notes[k] for k in keys), drop)
        self.settings_ready = True
        self.install_build = drop

    def choose_build(self, build: str):
        info = self.installing
        if info is None or info["busy"] or info.get("build") == build:
            return
        info["build"], info["release"] = build, None
        self.install_intro.set_label(_("Looking for the latest version…"))
        self.install_ok.set_sensitive(False)
        self.fetch_release()

    def fetch_release(self):
        info = self.installing

        def work():
            try:
                gpu = install_ctl.detect_gpu()
                release = info["inst"].latest(gpu, info.get("build", ""))
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
            if info.get("build") and release is not None and not release.version.endswith(info["build"]):
                return False               # an answer for a build chosen before
            info["gpu"], info["release"] = gpu, release
            if gpu.vendor == "cpu":
                card = _("no graphics card found: the processor")
            elif info["inst"].builds:
                # The chosen build names the interface (Vulkan, ROCm, CUDA, CPU).
                label = dict(info["inst"].builds).get(info.get("build"), "")
                card = f"{short_gpu(gpu.name, gpu.vram)} ({label})" if label else short_gpu(gpu.name, gpu.vram)
            elif info["rt"].key == "ollama":
                card = {"amd": _("{0} (ROCm)"), "nvidia": _("{0} (CUDA)")}.get(gpu.vendor, "{0}").format(
                    short_gpu(gpu.name, gpu.vram))
            else:
                card = short_gpu(gpu.name, gpu.vram)
            current = info["inst"].current_version()
            if info["mode"] == "update" and current == release.version:
                self.install_intro.set_label(_("{0} is up to date ({1}).").format(info["rt"].name, current))
                self.install_ok.set_visible(False)
                return False
            download = size_text(release.size) if release.size else _("several GB, through pip")
            self.install_intro.set_label(
                (_("Version {0} ({1} download) for {2}. Installed in your own folder: no password needed.")
                 if info["mode"] == "install" else _("{3} → {0} ({1} download) for {2}. The settings and models stay."))
                .format(release.version, download, card, current))
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
            needed = release.size * 3 if release.size else 15 * 10**9   # archives and what they unpack to; vLLM: pip
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
        for widget in (self.install_folder, self.install_models, self.install_login, self.install_start,
                       getattr(self, "install_build", None)):
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
        """Local LLM: start, stop, start at login. A new agent: look up its setup again."""
        rt = next((r for r in self.inventory.runtimes if f"runtime:{r.key}" == node_id), None)
        if rt is None:
            agent = next((a for a in self.agent_list if f"agent:{a.command}" == node_id), None)
            if agent is not None and self.is_new_agent(agent.command):
                self.show_agent_context(view, agent, rect)
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
        if rt.installed and rt.key in ("ollama", "llamacpp", "lmstudio"):
            actions.insert(0, (_("Models…"), "download"))
        if actions:
            column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            for label, mode in actions:
                button = Gtk.Button(label=label, halign=Gtk.Align.START)
                button.add_css_class("flat")
                button.add_css_class("am-menu-item")
                button.connect("clicked", lambda _b, m=mode: (popover.popdown(), self.open_models(rt) if m == "download"
                                                               else self.open_install(rt, m)))
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

    def show_agent_context(self, view, agent, rect):
        popover = Gtk.Popover()
        popover.set_parent(view)
        area = Gdk.Rectangle()
        area.x, area.y, area.width, area.height = (int(v) for v in rect)
        popover.set_pointing_to(area)
        popover.set_position(Gtk.PositionType.BOTTOM)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.add_css_class("am-provider-list")
        title = Gtk.Label(label=agent.name, xalign=0)
        title.add_css_class("am-agent-name")
        box.append(title)
        label, value, _tip = self.research_row(agent)
        note = Gtk.Label(label=value, xalign=0, wrap=True, max_width_chars=36)
        note.add_css_class("am-agent-detail")
        box.append(note)
        button = Gtk.Button(label=_("Look up again on the web"), halign=Gtk.Align.START)
        button.add_css_class("flat")
        button.add_css_class("am-menu-item")
        button.set_sensitive(agent.command not in self.researching)
        button.connect("clicked", lambda *_: (popover.popdown(), self.start_research(agent)))
        box.append(button)
        popover.set_child(box)
        popover.connect("closed", lambda p: GLib.idle_add(p.unparent))
        popover.popup()

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
        self.window_rounding = ai_core.hyprland_rounding()
        like = _("Like windows ({0} px)").format(self.window_rounding) if self.window_rounding is not None \
            else _("Like windows")
        self.setting_line(box, _("Corners"), self.corners_tip(),
                          self.dropdown([like, _("Rounded"), _("Square")],
                                        corners.CHOICES.index(ai_core.saved_corners()),
                                        lambda i: (self.save_setting(corners=corners.CHOICES[i]),
                                                   self.apply_theme_css())))
        look_up = Gtk.Switch(active=ai_core.load_settings().get("look_up_new_agents", True),
                             valign=Gtk.Align.CENTER)
        look_up.connect("notify::active", lambda w, *_: self.settings_ready
                        and self.save_setting(look_up_new_agents=w.get_active()))
        self.setting_line(box, _("Look up new agents"),
                          _("When a new agent is installed, Claude Code or Codex finds out on the web where it keeps "
                            "MCP servers and skills (once, a few minutes in the background)."), look_up)
        self.settings_ready = True

    @staticmethod
    def corners_tip() -> str:
        """Where the windows' rounding comes from (as in App Manager and Theme Manager)."""
        source = corners.rounding_source()
        if source == "user":
            origin = _("Your windows' rounding is set in ~/.config/hypr/looknfeel.lua.")
        elif source == "theme":
            origin = _("Your windows' rounding is set by the current theme (its hyprland.lua).")
        else:
            origin = _("Your windows use Omarchy's default rounding (square); a theme or "
                       "~/.config/hypr/looknfeel.lua can change it.")
        return _("Corners of the window, its cards and the map's boxes. “Like windows” follows Hyprland's "
                 "window rounding.") + " " + origin

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

    def set_sidebar(self, visible: bool):
        """The cards left of the side map: hidden, the map takes the whole width."""
        self.sidebar_visible = visible
        if getattr(self, "list_pane", None) is None:
            return
        self.list_pane.set_visible(visible or not self.split)
        if self.sidebar_button.get_active() != visible:
            self.sidebar_button.set_active(visible)
        self.update_header()

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
            self.set_sidebar(self.sidebar_visible)
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
        self.window_rounding = ai_core.hyprland_rounding()
        radius = corners.radius(ai_core.saved_corners(), self.window_rounding)
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
        .am-provider-list checkbutton check:indeterminate {{ background: alpha({accent_hex}, 0.45);
            border-color: {accent_hex}; color: {bg_hex}; }}
        .am-provider-list checkbutton.am-list-group {{ margin-top: 6px; }}
        .am-provider-list checkbutton.am-list-group label {{ font-size: 11px; font-weight: 700; color: {dim_hex}; }}
        .am-card entry.am-path {{ font-family: monospace; font-size: 12px; min-height: 0; padding: 3px 8px; }}
        .am-card entry.am-path.error {{ border-color: {red_hex}; }}
        progressbar.am-progress trough {{ min-height: 6px; background: alpha({fg_hex}, 0.10); border-radius: 3px; }}
        progressbar.am-progress progress {{ min-height: 6px; background: {accent_hex}; border-radius: 3px; }}
        .am-card button.am-menu-item {{ padding: 3px 8px; font-size: 12px; }}
        .am-card list.am-list {{ background: alpha({fg_hex}, 0.03); border-radius: {radius}px; }}
        .am-card list.am-list row:selected {{ background: alpha({accent_hex}, 0.22); }}
        .am-card-check {{ color: {accent_hex}; font-weight: 700; }}
        /* Omarchy's default agent: its "Default" badge says so; no border that looks like a selection. */
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
        .am-card .am-leader {{ min-height: 0; border-bottom: 1px dotted alpha({fg_hex}, 0.35); margin: 0 4px; }}
        .am-card .am-choice {{ padding: 4px 6px; border-radius: {radius}px; }}
        .am-card .am-choice:hover {{ background: alpha({fg_hex}, 0.05); }}
        .am-card .am-choice:hover .am-leader {{ border-bottom-color: alpha({accent_hex}, 0.8); }}
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
            elif page == "models":
                self.close_models()
            elif page == "mcp":
                self.close_mcp_details()
            elif page == "addmcp":
                self.close_add_mcp()
            elif page == "addskills":
                self.close_add_skills()
            elif page == "main" and self.selection:
                self.set_selection([])   # the first Esc clears the selection
            else:
                self.close()
            return True
        if page in ("install", "models", "addmcp", "addskills") and keyval == Gdk.KEY_BackSpace and \
                isinstance(self.window.get_focus(), (Gtk.Text, Gtk.TextView)):
            return False          # Backspace edits the folder fields
        if page in ("models", "addmcp", "addskills"):
            if page in ("addmcp", "addskills") and keyval == Gdk.KEY_BackSpace:
                self.close_add_mcp() if page == "addmcp" else self.close_add_skills()
                return True
            return False          # typing in the search fields
        if page in ("settings", "connect", "install", "mcp"):
            if keyval == Gdk.KEY_BackSpace:
                {"settings": self.close_settings, "connect": self.close_connect,
                 "install": self.close_install, "mcp": self.close_mcp_details}[page]()
                return True
            return False          # Tab and arrows move between the controls
        if ctrl and keyval == Gdk.KEY_comma:
            self.open_settings()
            return True
        if ctrl and page == "main" and keyval in (Gdk.KEY_plus, Gdk.KEY_equal, Gdk.KEY_KP_Add,
                                                    Gdk.KEY_minus, Gdk.KEY_KP_Subtract, Gdk.KEY_0, Gdk.KEY_KP_0):
            self.zoom_map(1 if keyval in (Gdk.KEY_plus, Gdk.KEY_equal, Gdk.KEY_KP_Add) else
                          -1 if keyval in (Gdk.KEY_minus, Gdk.KEY_KP_Subtract) else 0)
            return True
        if ctrl and keyval in (Gdk.KEY_b, Gdk.KEY_B) and page == "main" and self.split:
            self.set_sidebar(not self.sidebar_visible)
            return True
        return False

    # --- open / close ------------------------------------------------------------

    def toggle(self):
        if self.window.is_visible():
            self.close()
        else:
            self.open()

    def open(self):
        self.sidebar_visible = True
        self.fit()
        self.collect()
        self.refresh_overview()
        self.refresh_map()
        self.window.present()
        self.research_new_agents()
        self.check_new_apps()
        self.layer_watch.start()
        # Restarted after a language change: back on the settings page.
        view = os.environ.pop("AI_AGENTS_MAP_VIEW", "")
        if view in ai_core.VIEWS:
            self.show_view(view, save=False)
        page = os.environ.pop("AI_AGENTS_MAP_PAGE", "")
        if page == "settings":
            GLib.idle_add(self.open_settings)
        elif page == "addmcp":
            GLib.idle_add(self.open_add_mcp)
        elif page == "addskills":
            GLib.idle_add(self.open_add_skills)
        elif page.startswith("details:") and mcp_ctl.NAME_RE.match(page[8:]):
            GLib.idle_add(self.open_mcp_details, page[8:])

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
