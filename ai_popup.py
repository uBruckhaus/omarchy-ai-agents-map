#!/usr/bin/env python3
"""AI Agents Map: an app window that shows the AI setup of this computer:
agents, local LLMs and their models, MCP servers and skills, as cards with a
map beside them, details pages and agents running in terminals of their own.

A normal window (app id org.omarchy.aiagentsmap): Hyprland tiles or floats it,
Alt+Tab and workspaces work as for any app. One process: the bar icon and the
launcher entry run ai-agents-map-toggle, which wakes it with SIGUSR1; closing
the window hides it, so agents started in it keep running.
"""

import colorsys
import re
import os
import datetime as dt
import signal
import sys
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gio, GLib, Gtk, Pango

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
import link_check
import link_hints
import jan_ctl
import kobold_ctl
import mcp_update
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
import gpu_usage
import agent_status
import terminal_pane
import previews
import inventory
import provider_watch
import i18n
import pidfile
from i18n import _, ngettext
import app_entry
from map_view import MapView, Meter
from gi.repository import PangoCairo  # noqa: E402  (map_view asked for its version)
from palette import ThemePalette

# Smallest card (a small monitor); normally the card fills the screen.
# Default outer size as in the other popups: a 420 px card around a 590 px
# page, the same on every page so switching never resizes the popup.
CARD_WIDTH = 420
LIST_HEIGHT = 590
SCROLLER_MIN_HEIGHT = 120
SCREEN_MARGIN = 120    # space kept free above and below the card
WINDOW_SIZE = (1600, 960)   # first open; afterwards the size it was closed with
AGENT_SPACING = 8
SPLIT_WIDTH = 1300        # from this window width the overview shows the map beside the cards
LIST_PANE_WIDTH = 440
RIGHT_PAGES = ("connect", "install", "models", "mcp", "addmcp", "addskills", "status", "terminal")   # open beside the cards
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


class AppWindow(Gtk.ApplicationWindow):
    """Tells when the compositor gives it another width (Hyprland tiles the window
    without touching GTK's default size; its content may be wider than the window
    while the cards and the map still sit side by side)."""

    def __init__(self, on_width, **kwargs):
        super().__init__(**kwargs)
        self.on_width = on_width
        self.last_width = 0

    def do_size_allocate(self, width, height, baseline):
        Gtk.ApplicationWindow.do_size_allocate(self, width, height, baseline)
        if width != self.last_width:
            self.last_width = width
            GLib.idle_add(self.on_width, width)


class AgentsMap(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="org.omarchy.aiagentsmap")
        self.window = None
        self.theme = ThemePalette()
        self.style_provider = None
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
        self.sessions = {}           # agent command -> terminal_pane.TerminalPane (an agent started here)

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
        window = AppWindow(self.width_changed, application=self)
        window.set_decorated(False)       # Hyprland draws the border; the header is our own
        window.set_title("AI Agents Map")
        window.set_name("aiagentsmap-window")
        window.set_icon_name(app_entry.ICON_NAME)
        size = ai_core.load_settings().get("window_size")
        if isinstance(size, list) and len(size) == 2 and all(isinstance(v, int) and 400 <= v <= 10000 for v in size):
            window.set_default_size(*size)
        else:
            window.set_default_size(*WINDOW_SIZE)
        window.set_size_request(ai_core.MIN_WINDOW_SIZE[0], ai_core.MIN_WINDOW_SIZE[1] + 200)
        # Closing hides it: the process stays, with any agent running in it.
        window.connect("close-request", lambda *_: (self.close(), True)[1])
        self.window = window

        # The cards beside the map need room: the card follows its own width.
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, hexpand=True, vexpand=True)
        card.add_css_class("am-card")
        frame = Gtk.Overlay()
        frame.set_child(card)
        # The content's minimum width (cards + map) must not become the window's: Hyprland may
        # tile it narrower, and only then does the window learn its real width (and drop the map).
        holder = Gtk.ScrolledWindow()
        holder.set_policy(Gtk.PolicyType.EXTERNAL, Gtk.PolicyType.NEVER)
        holder.set_child(frame)
        window.set_child(holder)
        frame.add_overlay(self.build_undo_bar())
        self.card = card
        self.card_frame = frame

        card.append(self.build_header())

        stack = Gtk.Stack()
        stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
        stack.set_transition_duration(140)
        stack.set_interpolate_size(False)
        stack.set_hhomogeneous(True)
        stack.set_vhomogeneous(True)
        stack.add_named(self.build_main_page(), "main")
        stack.add_named(self.build_settings_page(), "settings")
        stack.add_named(self.build_help_page(), "help")
        # Details and the pages that change something open right of the cards, where the map is.
        right = self.right_stack
        right.add_named(self.build_connect_page(), "connect")
        right.add_named(self.build_install_page(), "install")
        right.add_named(self.build_models_page(), "models")
        right.add_named(self.build_mcp_page(), "mcp")
        right.add_named(self.build_add_mcp_page(), "addmcp")
        right.add_named(self.build_add_skills_page(), "addskills")
        right.add_named(self.build_status_page(), "status")
        right.connect("notify::visible-child-name", lambda *_: self.update_header())
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
        # Not focusable: as the first control it would hold the focus on open, and a stray
        # Space (typing meant for another window) would hide the cards. Ctrl+B toggles it.
        sidebar_button.set_focusable(False)
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
        # Too narrow for the map (a tiled window): a hint that also does it.
        self.widen_hint = Gtk.Button(valign=Gtk.Align.CENTER, visible=False)
        content = Gtk.Box(spacing=6)
        content.append(Gtk.Image.new_from_icon_name("view-fullscreen-symbolic"))
        content.append(Gtk.Label(label=_("Maximize to see the map")))
        self.widen_hint.set_child(content)
        self.widen_hint.add_css_class("am-small-btn")
        self.widen_hint.add_css_class("am-widen-hint")
        self.widen_hint.set_tooltip_text(_("Only the cards fit at this width. A click moves the window to a free "
                                           "workspace, where it gets the whole width (or press Super + Alt + F)."))
        self.widen_hint.connect("clicked", lambda *_: self.widen())
        header.append(self.widen_hint)
        # One view (the cards with the map beside them): no view switch any more.
        self.view_buttons = {}
        # Which setup to show, for the cards and the map alike: everything, only online
        # providers (no local LLMs), or only local LLMs (and the agents that can use them).
        self.view_switch = Gtk.Box(spacing=0, valign=Gtk.Align.CENTER)
        self.view_switch.add_css_class("am-segmented")
        self.mode_buttons = {}
        tips = {"all": _("Everything: online providers and local LLMs"),
                "online": _("Only online providers: no local LLMs, models or GPU meters"),
                "local": _("Only local LLMs, and the agents that can use them")}
        group = None
        for mode, label in (("all", _("All")), ("online", _("Online")), ("local", _("Local LLMs"))):
            button = Gtk.ToggleButton(label=label, group=group)
            group = group or button
            button.add_css_class("am-segment")
            button.set_tooltip_text(tips[mode])
            button.connect("toggled", lambda b, m=mode: b.get_active() and self.set_setup_mode(m))
            self.view_switch.append(button)
            self.mode_buttons[mode] = button
        self.mode_buttons[self.setup_mode()].set_active(True)
        header.append(self.view_switch)

        # Help (the manual: setting up by drawing, …), left of the gear, in the same bordered box.
        help_button = Gtk.Button()
        mark = Gtk.Label(label="?")
        mark.add_css_class("am-gear")
        mark.add_css_class("am-help-mark")
        help_button.set_child(mark)
        help_button.set_tooltip_text(_("Help (F1)"))
        help_button.add_css_class("flat")
        help_button.add_css_class("am-header-btn")
        help_button.add_css_class("am-gear-btn")
        help_button.connect("clicked", lambda *_: self.open_help())
        header.append(help_button)
        self.help_button = help_button
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
        scroller.set_vexpand(True)
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
        body = Gtk.Box(spacing=0)
        self.agents_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=AGENT_SPACING)
        self.agents_box.add_css_class("am-agents")
        scroller = self.page_scroller(self.agents_box)
        scroller.set_vexpand(True)
        # The cards as a panel of their own: a heading line with its filter, a frame,
        # and a rule between it and the map.
        self.list_pane = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, hexpand=True)
        self.list_pane.add_css_class("am-sidebar")
        top = Gtk.Box(spacing=8)
        heading = Gtk.Label(label=_("Overview").upper(), xalign=0, hexpand=True)
        heading.add_css_class("am-section-title")
        top.append(heading)
        self.card_filter = Gtk.Button()
        self.card_filter.add_css_class("am-small-btn")
        self.card_filter.add_css_class("am-filter-btn")
        self.card_filter.set_tooltip_text(_("Choose which cards the overview shows"))
        self.card_filter.connect("clicked", lambda b: self.show_card_list(b))
        top.append(self.card_filter)
        self.list_pane.append(top)
        self.list_pane.append(scroller)
        body.append(self.list_pane)
        self.side_rule = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        self.side_rule.add_css_class("am-side-rule")
        body.append(self.side_rule)
        # Wide: the map beside the cards; a click on a card selects it there.
        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, hexpand=True)
        side.add_css_class("am-map-pane")
        self.gpu_line = self.build_gpu_line()
        side.append(self.gpu_line)
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
        self.side_map.on_failed_edge = self.show_link_hints
        self.side_map.on_context = lambda node_id, rect, v=self.side_map: self.show_context(v, node_id, rect)
        self.side_map.on_header = lambda column, rect: self.show_column_list(self.side_map, column, rect)
        map_scroller = self.page_scroller(self.side_map)
        map_scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)   # zoomed in: sideways too
        side.append(map_scroller)
        self.side_map.set_zoom(ai_core.load_settings().get("map_zoom", 1.0)
                               if isinstance(ai_core.load_settings().get("map_zoom"), (int, float)) else 1.0)
        self.zoom_label.set_label(f"{round(self.side_map.zoom * 100)} %")
        # The right of the Overview: the map, or a page opened from a card or from the map.
        right = Gtk.Stack(hexpand=True)
        right.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        right.set_transition_duration(120)
        right.add_named(side, "map")
        right.set_visible(False)
        self.right_stack = right
        self.side_pane = right
        body.append(right)
        page.append(body)
        return page

    def page_name(self) -> str:
        """The page in view: "main" (cards + map), "settings", or a page right of the cards."""
        outer = self.stack.get_visible_child_name()
        if outer != "main":
            return outer
        inner = self.right_stack.get_visible_child_name() or ""
        if inner.startswith("term:"):
            return "terminal"
        return inner if inner in RIGHT_PAGES else "main"

    def show_page(self, name: str):
        if name in RIGHT_PAGES:
            self.stack.set_visible_child_name("main")
            self.right_stack.set_visible_child_name(name)
        else:
            if name == "main":
                self.right_stack.set_visible_child_name("map")
            self.stack.set_visible_child_name(name)
        self.place_panes()
        self.update_header()

    def place_panes(self):
        """Wide: the cards stay beside whatever is on the right. Narrow (cards only):
        a page from the right takes the whole width while it is open."""
        if getattr(self, "right_stack", None) is None:
            return
        on_right = self.page_name() in RIGHT_PAGES
        if self.split:
            self.right_stack.set_visible(True)
            self.list_pane.set_visible(self.sidebar_visible)
            self.side_rule.set_visible(self.sidebar_visible)
        else:
            self.right_stack.set_visible(on_right)
            self.list_pane.set_visible(not on_right)
            self.side_rule.set_visible(False)

    # --- GPU memory and activity (as LLM Monitor shows them) --------------------------------------

    def build_gpu_line(self) -> Gtk.Widget:
        """Above the map, as in LLM Monitor: GPU MEMORY (in use, free, a bar split into
        local LLMs / desktop & apps / free, with a legend) and GPU ACTIVITY (load,
        temperature, power, a bar; green while a model is generating)."""
        colors = lambda name, alpha=1.0: self.side_map.color(name, alpha)
        line = Gtk.Box(spacing=24, homogeneous=True)
        line.set_margin_bottom(18)       # air before the Show line

        def block(title):
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, hexpand=True)
            heading = Gtk.Label(label=title.upper(), xalign=0)
            heading.add_css_class("am-section-title")
            box.append(heading)
            top = Gtk.Box(spacing=8)
            main = Gtk.Label(xalign=0, hexpand=True)
            main.add_css_class("am-agent-name")
            side = Gtk.Label(xalign=1)
            side.add_css_class("am-agent-detail")
            top.append(main)
            top.append(side)
            box.append(top)
            meter = Meter(colors, 100, 10)
            meter.set_hexpand(True)
            box.append(meter)
            line.append(box)
            return box, main, side, meter

        memory, self.gpu_memory_text, self.gpu_free_text, self.gpu_memory = block(_("GPU memory"))
        legend = Gtk.Box(spacing=16)
        legend.set_margin_top(4)
        self.gpu_legend = {}
        for key, name, alpha, outlined in (("llm", _("Local LLMs"), 1.0, False), ("other", _("Desktop & apps"), 0.45, False),
                                           ("free", _("Free"), 1.0, True)):
            item = Gtk.Box(spacing=6)
            swatch = Gtk.DrawingArea(content_width=10, content_height=10, valign=Gtk.Align.CENTER)
            swatch.set_draw_func(lambda _a, cr, w, h, k=key, a=alpha, o=outlined: self.draw_swatch(cr, w, h, k, a, o))
            label = Gtk.Label(xalign=0)
            label.add_css_class("am-agent-detail")
            item.append(swatch)
            item.append(label)
            legend.append(item)
            self.gpu_legend[key] = (name, label)
        memory.append(legend)
        _activity, self.gpu_activity_text, self.gpu_activity_side, self.gpu_activity = block(_("GPU activity"))
        line.set_visible(False)        # until the first reading (no GPU: never)
        self.gpu_line = line
        self.gpu_timer = None
        self.gpu_reading = False
        self.running = {}        # agent command -> the model of each open instance ("" = its settings')
        return line

    def draw_swatch(self, cr, width, height, key, alpha, outlined):
        MapView.rounded(cr, 0, 0, width, height, 2)
        if outlined:
            cr.set_source_rgba(*self.side_map.color("dim"))
            cr.set_line_width(1)
            cr.stroke()
        else:
            cr.set_source_rgba(*self.side_map.color("accent" if key == "llm" else "fg", alpha))
            cr.fill()

    def start_gpu_poll(self):
        self.stop_gpu_poll()
        self.poll_gpu()
        self.gpu_timer = GLib.timeout_add_seconds(2, self.poll_gpu)

    def stop_gpu_poll(self):
        if self.gpu_timer:
            GLib.source_remove(self.gpu_timer)
            self.gpu_timer = None

    def poll_gpu(self):
        if self.gpu_reading:
            return True
        self.gpu_reading = True
        runtimes = list(self.inventory.runtimes)

        def work():
            try:
                usage = gpu_usage.snapshot(runtimes)
            except Exception:
                usage = None
            GLib.idle_add(done, usage, self.running_now())

        def done(usage, running):
            self.gpu_reading = False
            if usage is not None and self.window.is_visible():
                self.show_gpu(usage, runtimes)
            if running != self.running and self.window.is_visible():
                # An agent was opened or closed: its dot, its model and its card change.
                self.running = running
                self.refresh_overview()
                self.refresh_map()
            return False

        threading.Thread(target=work, daemon=True).start()
        return True

    def show_gpu(self, usage, runtimes):
        gib = gpu_usage.gib
        total = usage.vram_total
        self.gpu_line.set_visible(total > 0 and "local" not in self.mode_rules()[0])
        if total:
            free = max(0, total - usage.vram_used)
            self.gpu_memory.set_parts([(usage.llm_vram / total, "accent", 1.0), (usage.other_vram / total, "fg", 0.45)])
            self.gpu_memory_text.set_label(_("{0} of {1} GB in use").format(gib(usage.vram_used), gib(total)))
            self.gpu_free_text.set_label(_("{0} GB free").format(gib(free)))
            for key, size in (("llm", usage.llm_vram), ("other", usage.other_vram), ("free", free)):
                name, label = self.gpu_legend[key]
                label.set_label(f"{name}  {gib(size)} GB")
        busy = usage.busy or 0
        self.gpu_activity.set_parts([(busy / 100, "green" if usage.generating else "accent", 1.0)])
        self.gpu_activity_text.set_label(f"{round(busy)} %" + ("  ·  " + _("generating") if usage.generating else ""))
        extra = [f"{round(usage.temp)} °C" if usage.temp else "", f"{round(usage.power)} W" if usage.power else ""]
        self.gpu_activity_side.set_label("  ·  ".join(e for e in extra if e))
        self.side_map.set_usage(usage)
        # The watcher (or something else) started or stopped a local LLM: show it.
        changed = any(rt.running != usage.runtimes[rt.key].running for rt in runtimes if rt.key in usage.runtimes)
        if changed and runtimes is self.inventory.runtimes and self.side_map.link is None \
                and self.page_name() == "main":
            self.collect()
            self.refresh_overview()
            self.refresh_map()

    def collect(self):
        """Everything both views show (file system and /proc only, ~0.1 s)."""
        self.agent_list = agents.list_agents()
        self.inventory = inventory.collect(self.agent_list)
        self.configured_now = None     # installed_mcp() reads the settings again
        self.running = self.running_now()
        try:
            changed = provider_watch.write_config(self.inventory)
        except OSError:
            changed = False
        if changed:
            self.sync_provider_watch()

    def running_now(self) -> dict:
        """Agent command -> the model each open instance runs with (/proc only)."""
        names = [a.command for a in getattr(self, "agent_list", []) if a.installed]
        try:
            found = provider_watch.agent_instances(names)
        except OSError:
            return {}
        return {name: sorted(provider_watch.instance_model(name, args) for args in found[name]) for name in found}

    def instance_label(self, agent) -> str:
        """"running · opus" for an agent with an open instance, "" otherwise."""
        models = self.running.get(agent.command)
        if not models:
            return ""
        configured = graph.split_model(agent_details.details_for(agent.command).model)[0]
        names = sorted({graph.split_model(m)[0] if m else configured for m in models} - {""})
        state = _("running") if len(models) == 1 else _("{0} running").format(len(models))
        return " · ".join([state] + names)

    def sync_provider_watch(self):
        """The watcher that starts an agent's local LLMs with it (and stops them when it
        closes) runs while the setting is on and some agent is connected to a local LLM.
        It is a user service, so it works with this window closed and after a reboot."""
        wanted = bool(ai_core.load_settings().get("providers_with_agents", True)) and \
            any(rt.installed and rt.agents and (rt.service or rt.key == "lmstudio") for rt in self.inventory.runtimes)
        threading.Thread(target=provider_watch.ensure_service, args=(wanted,), daemon=True).start()

    def refresh_overview(self):
        """Rebuild the main view: agents, local LLMs, MCP servers, skills."""
        box = self.agents_box
        while (child := box.get_first_child()) is not None:
            box.remove(child)
        self.detail_boxes = {}
        self.card_widgets = {}
        off, mode_hidden = self.mode_rules()
        hidden = self.card_hidden() | mode_hidden
        installed = [a for a in self.agent_list if a.installed and f"agent:{a.command}" not in hidden]
        missing = [a for a in self.agent_list if not a.installed and f"agent:{a.command}" not in hidden]
        inv = self.inventory
        names = self.agent_names()
        self.update_card_filter()

        if installed:
            self.section(_("Agents ({0})").format(len(installed)))
        for agent in installed:
            box.append(self.agent_card(agent))

        runtimes = [] if "local" in off else \
            [rt for rt in sorted(inv.runtimes, key=lambda r: not r.installed) if f"runtime:{rt.key}" not in hidden]
        if runtimes:
            self.section(_("Local LLMs ({0})").format(len([rt for rt in runtimes if rt.installed])))
        # Installed ones first; the others too, grey, as something to install and connect.
        for rt in runtimes:
            status = graph.runtime_status(rt)
            state = {"running": _("running"), "stopped": _("installed, not running")}.get(status, _("not installed"))
            sub = state if not rt.installed else \
                " · ".join([state, ngettext("1 model", "{0} models", len(rt.models)).format(len(rt.models))])
            box.append(self.expandable_card(f"runtime:{rt.key}", "\U000F061A", "", rt.name, sub,
                                            lambda b, rt=rt: self.fill_runtime(b, rt, names),
                                            dim=not rt.installed, status=status))

        servers = [m for m in inv.mcp if f"mcp:{m.name}" not in hidden]
        self.section(_("MCP servers ({0})").format(len(servers)))
        if not inv.mcp:
            self.section_hint(_("No agent has an MCP server set up."))
        for server in servers:
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

        skill_sets = [s for s in inv.skill_sets if f"skills:{s.path}" not in hidden]
        self.section(_("Skills ({0})").format(sum(len(s.skills) for s in skill_sets)))
        if not inv.skill_sets:
            self.section_hint(_("No skills found for the installed agents."))
        for skill_set in skill_sets:
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
        if agent.installed and self.instance_label(agent):
            subtitle = " · ".join(filter(None, [agent.version, self.instance_label(agent)]))
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

    def fill_agent(self, box, agent, summary: bool = False):
        """The agent card's details; summary=True: its configuration only (the details
        page shows usage, plan and limits in its own summary above)."""
        command = agent.command
        info = agent_details.details_for(command)
        inv = self.inventory
        rows = []
        if agent.installed and self.is_new_agent(command):
            rows.append(self.research_row(agent))
        if not agent.installed:
            rows.append((_("Status"), _("Not installed"),
                         _("Install it from Omarchy's menu: Setup → Defaults → Agent.")))
        if not summary:
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
        if not summary and (info.today_prompts is not None or info.today_tokens is not None):
            today = []
            if info.today_prompts is not None:
                today.append(ngettext("1 prompt", "{0} prompts", info.today_prompts).format(info.today_prompts))
            if info.today_sessions is not None:
                today.append(ngettext("1 session", "{0} sessions", info.today_sessions).format(info.today_sessions))
            if info.today_tokens:
                today.append(_("{0} tokens").format(short_number(info.today_tokens)))
            rows.append((_("Today"), " · ".join(today), ""))
        if info.total_sessions and not summary:
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

        if summary:
            return
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
        if not summary:
            buttons = Gtk.Box(spacing=8)
            if agent.installed and terminal_pane.program(agent.command):
                buttons.append(self.start_button(agent))
            buttons.append(self.details_button(lambda *_: self.open_agent_status(agent)))
            box.append(buttons)

    # --- runtime, MCP and skill cards ----------------------------------------------------

    def fill_runtime(self, box, rt, names, summary: bool = False):
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
        if not summary:
            box.append(self.details_button(lambda *_: self.open_runtime_status(rt.key)))

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

    # --- an agent started here, in its own terminal ------------------------------------------

    def ask_start_outside(self, agent, path: str, folder: str, anchor):
        """No vte4: ask whether to fix that (its details page shows how) or to start the
        agent in a separate terminal this time."""
        popover = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_top(10)
        box.set_margin_bottom(10)
        box.set_margin_start(12)
        box.set_margin_end(12)
        text = Gtk.Label(label=_("{0} cannot run inside the map yet: the vte4 package is missing.").format(agent.name),
                         xalign=0, wrap=True, max_width_chars=42)
        box.append(text)
        row = Gtk.Box(spacing=8)
        fix = Gtk.Button(label=_("Show how to fix"))
        fix.add_css_class("suggested-action")

        def show_fix(*_):
            popover.popdown()
            self.open_agent_status(agent)
        fix.connect("clicked", show_fix)
        row.append(fix)
        outside = Gtk.Button(label=_("Start in a separate terminal"))

        def start_outside(*_):
            popover.popdown()
            ok = terminal_pane.open_outside(path, folder)
            if not ok:
                try:
                    agent_status.remember_issue(agent.command, "terminal")
                except OSError:
                    pass
            self.show_notice(_("{0} opened in your terminal").format(agent.name) if ok
                             else _("No terminal found (xdg-terminal-exec)"))
        outside.connect("clicked", start_outside)
        row.append(outside)
        box.append(row)
        popover.set_child(box)
        popover.set_parent(anchor if anchor is not None else self.title_label)
        popover.connect("closed", lambda p: GLib.idle_add(p.unparent))
        popover.popup()

    def start_agent(self, agent, anchor=None):
        """Its terminal right of the cards (a running one is shown again)."""
        pane = self.sessions.get(agent.command)
        if pane is not None:
            if not pane.running:
                pane.start()
            self.show_terminal(agent.command)
            return
        path = terminal_pane.program(agent.command)
        if path is None:
            self.show_notice(_("{0} cannot be started from here").format(agent.name))
            return
        folder = self.agent_folder()
        if terminal_pane.vte() is None:
            try:
                agent_status.remember_issue(agent.command, "vte4")
            except OSError:
                pass
            self.ask_start_outside(agent, path, folder, anchor)
            return
        pane = terminal_pane.TerminalPane(agent, folder, self.theme.colors,
                                          on_back=lambda: self.show_view("main", save=False),
                                          on_state=lambda _p: GLib.idle_add(self.terminal_changed))
        self.sessions[agent.command] = pane
        self.right_stack.add_named(pane, f"term:{agent.command}")
        self.show_terminal(agent.command)

    def show_terminal(self, command: str):
        self.stack.set_visible_child_name("main")
        self.right_stack.set_visible_child_name(f"term:{command}")
        self.place_panes()
        self.update_header()
        GLib.idle_add(lambda: self.sessions[command].terminal.grab_focus() and False)

    def terminal_changed(self):
        """An agent started or ended here: its card's button and the running marks follow.
        A start that worked clears the problem an earlier one ran into."""
        for command, pane in self.sessions.items():
            if pane.running and pane.pid:
                try:
                    agent_status.forget_issue(command)
                except OSError:
                    pass
        self.running = self.running_now()
        self.refresh_overview()
        self.refresh_map()
        return False

    def agent_folder(self) -> str:
        """Where agents start: the folder set in Settings, else home."""
        saved = ai_core.load_settings().get("agent_folder")
        if isinstance(saved, str) and saved and os.path.isdir(os.path.expanduser(saved)):
            return os.path.realpath(os.path.expanduser(saved))
        return str(Path.home())

    def start_button(self, agent) -> Gtk.Widget:
        pane = self.sessions.get(agent.command)
        shown = pane is not None and pane.running
        button = Gtk.Button(halign=Gtk.Align.START)
        content = Gtk.Box(spacing=6)
        content.append(Gtk.Image.new_from_icon_name("utilities-terminal-symbolic"))
        content.append(Gtk.Label(label=_("Show terminal") if shown else _("Start")))
        button.set_child(content)
        button.add_css_class("flat")
        button.add_css_class("am-small-btn")
        button.set_tooltip_text(_("Run {0} in a terminal here, in {1}").format(agent.name, self.agent_folder()))
        button.connect("clicked", lambda b: self.start_agent(agent, b))
        return button

    # --- agent and local LLM details (the (i) on their cards) -----------------------------------

    def build_status_page(self) -> Gtk.Widget:
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
        back.connect("clicked", lambda *_: self.close_status())
        header.append(back)
        self.status_title = Gtk.Label(xalign=0, hexpand=True)
        self.status_title.add_css_class("am-section-title")
        header.append(self.status_title)
        page.append(header)
        rule = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        rule.add_css_class("am-settings-rule")
        page.append(rule)
        self.status_body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.status_body.add_css_class("am-settings")
        page.append(self.page_scroller(self.status_body))
        self.status_showing = None
        return page

    def clear_status(self, title: str):
        self.status_title.set_label(title.upper())
        while (child := self.status_body.get_first_child()) is not None:
            self.status_body.remove(child)

    def close_status(self):
        self.status_showing = None
        self.show_view("main", save=False)

    def open_agent_status(self, agent):
        """Usage over time, whether it is set up (and what to do if not), its configuration."""
        self.clear_status(agent.name)
        self.status_showing = ("agent", agent.command)
        self.note(self.status_body, _("Reading…"))
        self.show_page("status")
        inventory = self.inventory

        def work():
            info = agent_details.details_for(agent.command)
            record = agent_details.usage_record(agent.command)
            try:
                usage = agent_status.usage_for(agent.command, record)
            except Exception:
                usage = None
            logged_in = agent_status.login_state(agent.command) if agent.installed else None
            checks = agent_status.agent_checks(agent, info, record, inventory, link_hints.load(), logged_in,
                                               issue=agent_status.load_issues().get(agent.command))
            GLib.idle_add(done, info, usage, checks)

        def done(info, usage, checks):
            if self.status_showing == ("agent", agent.command):
                self.fill_agent_status(agent, info, usage, checks)
            return False

        threading.Thread(target=work, daemon=True).start()

    def fill_agent_status(self, agent, info, usage, checks):
        self.clear_status(agent.name)
        body = self.status_body
        # At the top: one sentence, then what it is doing now.
        state, sentence = agent_status.summary_line(checks)
        models = self.running.get(agent.command)
        if models:
            now = self.instance_label(agent)
        elif info.last_used or (usage and usage.last):
            now = _("Not running · last used {0}").format(when(max(x for x in (info.last_used, usage and usage.last) if x)))
        else:
            now = _("Not running")
        body.append(self.status_hero(state, sentence, now))

        tiles = []
        if usage is not None and usage.has_tokens:
            tiles.append((_("Today"), _("{0} tokens").format(short_number(usage.today_tokens)),
                          ngettext("1 session", "{0} sessions", usage.sessions_today).format(usage.sessions_today)
                          if usage.sessions_today is not None else ""))
            tiles.append((_("Last 7 days"), _("{0} tokens").format(short_number(usage.week_tokens)),
                          ngettext("1 session", "{0} sessions", usage.sessions_week).format(usage.sessions_week)
                          if usage.sessions_week is not None else ""))
        elif usage is not None:
            tiles.append((_("Last 7 days"), ngettext("1 session", "{0} sessions", usage.sessions_week or 0)
                          .format(usage.sessions_week or 0), _("tokens not recorded")))
        if usage is not None and usage.sessions_total is not None:
            tiles.append((_("In total"), ngettext("1 session", "{0} sessions", usage.sessions_total)
                          .format(usage.sessions_total), _("{0} active days").format(info.active_days)
                          if info.active_days else ""))
        if info.plan:
            tiles.append((_("Plan"), info.plan, ""))
        elif usage is not None and usage.cost:
            tiles.append((_("Cost"), f"$ {usage.cost:.2f}", _("as the agent estimates it")))
        if tiles:
            body.append(self.stat_tiles(tiles))
        if usage is not None and usage.has_tokens and any(t for _d, t in usage.days):
            self.settings_heading(body, _("Tokens per day"))
            body.append(TokenChart(self.side_map.color, usage.days))
        if info.limits:
            self.settings_heading(body, _("Limits"))
            for limit in info.limits:
                body.append(self.limit_row(limit))
        if usage is not None and usage.by_model:
            self.settings_heading(body, _("Tokens by model"))
            rows = sorted(usage.by_model.items(), key=lambda item: -sum(item[1]))[:8]
            self.detail_grid(body, [(model, _("{0} in · {1} out · {2} cache").format(
                short_number(i), short_number(o), short_number(c)), "") for model, (i, o, c) in rows])
        if usage is not None and usage.source:
            source = _("Usage from Omarchy, updated {0}").format(when(info.usage_updated)) \
                if usage.source == "Omarchy" and info.usage_updated else \
                _("Usage from {0}'s own session records (numbers and times only)").format(usage.source)
            self.note(body, source)

        self.settings_heading(body, _("Setup"))
        body.append(self.check_list(checks))
        self.settings_heading(body, _("Configuration"))
        config = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.fill_agent(config, agent, summary=True)
        body.append(config)

    def open_runtime_status(self, key: str):
        rt = next((r for r in self.inventory.runtimes if r.key == key), None)
        if rt is None:
            return
        self.clear_status(rt.name)
        self.status_showing = ("runtime", key)
        body = self.status_body
        names = self.agent_names()
        try:
            login = runtime_ctl.start_at_login(rt) if rt.installed else None
        except Exception:
            login = None
        checks = agent_status.runtime_checks(rt, link_hints.load(), names, login)
        state, sentence = agent_status.summary_line(checks)
        use = self.side_map.runtime_use(graph.Node(f"runtime:{key}", 2, rt.name, kind="runtime"))
        if rt.running:
            now = _("Running") + (" · " + _("{0} GB of GPU memory").format(f"{use.vram / 1024 ** 3:.1f}")
                                   if use is not None and use.vram >= 64 * 1024 ** 2 else "")
            loaded = [m for m, state_ in (use.models.items() if use is not None else []) if state_ == "loaded"]
            if loaded:
                now += " · " + _("loaded: {0}").format(", ".join(loaded))
        else:
            now = _("Not running") if rt.installed else _("Not installed")
        body.append(self.status_hero(state, sentence, now))
        tiles = [(_("Models"), str(len(rt.models)), rt.models_dir),
                 (_("Agents"), str(len(rt.agents)), ", ".join(names.get(a, a) for a in rt.agents))]
        if rt.address:
            tiles.append((_("Address"), rt.address, rt.service))
        body.append(self.stat_tiles(tiles))
        self.settings_heading(body, _("Setup"))
        body.append(self.check_list(checks))
        self.settings_heading(body, _("Configuration"))
        config = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.fill_runtime(config, rt, names, summary=True)
        body.append(config)
        self.show_page("status")

    def status_hero(self, state: str, sentence: str, now: str) -> Gtk.Widget:
        box = Gtk.Box(spacing=12)
        box.add_css_class("am-status-hero")
        mark = Gtk.Label(label={"ok": "✓", "warn": "!", "missing": "✕"}.get(state, "·"))
        mark.add_css_class("am-status-mark")
        mark.add_css_class(f"am-state-{state}")
        mark.set_valign(Gtk.Align.CENTER)
        box.append(mark)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
        title = Gtk.Label(label=sentence, xalign=0, wrap=True, max_width_chars=1)
        title.add_css_class("am-status-title")
        text.append(title)
        line = Gtk.Label(label=now, xalign=0, wrap=True, max_width_chars=1)
        line.add_css_class("am-agent-detail")
        text.append(line)
        box.append(text)
        return box

    def command_line(self, command: str) -> Gtk.Widget:
        """A command to run, in monospace, with a button that copies it."""
        row = Gtk.Box(spacing=6, halign=Gtk.Align.START)
        row.add_css_class("am-command-line")
        label = Gtk.Label(label=command, xalign=0, selectable=True)
        label.add_css_class("am-command")
        row.append(label)
        copy = Gtk.Button(icon_name="edit-copy-symbolic")
        copy.add_css_class("flat")
        copy.add_css_class("am-small-btn")
        copy.set_tooltip_text(_("Copy"))

        def copied(button):
            Gdk.Display.get_default().get_clipboard().set(command)
            button.set_icon_name("object-select-symbolic")
            GLib.timeout_add(1200, lambda: button.set_icon_name("edit-copy-symbolic") or False)
        copy.connect("clicked", copied)
        row.append(copy)
        return row

    def stat_tiles(self, tiles) -> Gtk.Widget:
        """(heading, big value, small line) side by side."""
        row = Gtk.Box(spacing=10, homogeneous=True)
        for heading, value, small in tiles:
            tile = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            tile.add_css_class("am-stat-tile")
            top = Gtk.Label(label=heading.upper(), xalign=0)
            top.add_css_class("am-group-heading")
            tile.append(top)
            big = Gtk.Label(label=value, xalign=0, ellipsize=Pango.EllipsizeMode.END, max_width_chars=1)
            big.add_css_class("am-stat-value")
            tile.append(big)
            if small:
                line = Gtk.Label(label=small, xalign=0, ellipsize=Pango.EllipsizeMode.END, max_width_chars=1)
                line.add_css_class("am-agent-detail")
                line.set_tooltip_text(small)
                tile.append(line)
            row.append(tile)
        return row

    def check_list(self, checks) -> Gtk.Widget:
        """One line per check: a mark, what it is, and under it what to do when it is not done."""
        grid = Gtk.Grid(column_spacing=10, row_spacing=6)
        row = 0
        for check in checks:
            mark = Gtk.Label(label={"ok": "✓", "warn": "!", "missing": "✕", "info": "·"}[check.state], yalign=0)
            mark.add_css_class(f"am-state-{check.state}")
            mark.add_css_class("am-check-mark")
            grid.attach(mark, 0, row, 1, 1)
            text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1, hexpand=True)
            title = Gtk.Label(label=check.title + (f" · {check.detail}" if check.detail else ""), xalign=0,
                              wrap=True, max_width_chars=1)
            title.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
            title.add_css_class("am-detail-value")
            text.append(title)
            if check.step and check.state != "ok":
                step = Gtk.Label(label="→ " + check.step, xalign=0, wrap=True, max_width_chars=1, selectable=True)
                step.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
                step.add_css_class("am-check-step")
                step.add_css_class(f"am-state-{check.state}")
                text.append(step)
            if check.command and check.state != "ok":
                text.append(self.command_line(check.command))
            grid.attach(text, 1, row, 1, 1)
            row += 1
        return grid

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
        if self.page_name() != "mcp":
            self.mcp_from = self.view if self.page_name() in ai_core.VIEWS else "main"
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
        self.show_page("mcp")
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
        if self.page_name() != "mcp":
            self.mcp_from = self.view if self.page_name() in ai_core.VIEWS else "main"
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
        self.show_page("mcp")
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
            if self.page_name() == "mcp" and getattr(self, "mcp_showing", None) == name:
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
        if self.page_name() != "addmcp":
            self.add_return = self.view if self.page_name() in ai_core.VIEWS else "main"
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
        self.show_page("addmcp")
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
            if self.page_name() == "addmcp":
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
        names = self.agent_names()
        for candidate in self.mcp_updates():
            users = [names.get(a, a) for a in candidate.users if a != "list"] or [_("the list of MCP servers")] \
                if candidate.users else [skills_ctl.tilde(link) for link, _o, _n in mcp_update.links(candidate)]
            detail = "↑ " + _("Update · used by {0}").format(", ".join(users))
            self.add_found_list.append(self.choice_row(
                "{0}  {1} → {2}".format(candidate.name, candidate.update_from, candidate.version), detail,
                lambda *_, c=candidate: self.open_mcp_update(c), _("Update…")))
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

    def mcp_updates(self) -> list:
        """Found apps in a newer version whose older one is still started
        (by an agent, the list or a link in ~/.local/bin)."""
        return [c for c in self.found_mcp or [] if c.update_from and (c.users or mcp_update.links(c))]

    def choice_row(self, title: str, detail: str, on_use, button: str = "") -> Gtk.Widget:
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
        use = Gtk.Button(label=button or _("Add…"), valign=Gtk.Align.CENTER)
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
        updates = self.mcp_updates()
        mcp = [c for c in self.found_mcp or [] if (not self.installed_mcp(c) or c in updates)
               and c.program not in seen_mcp]
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
            connect.prune_retired()
            kobold = install_ctl.installer("koboldcpp")
            if kobold.installed_folder() is not None and kobold.models_folder():
                try:
                    kobold.sync(Path(kobold.models_folder()))      # models added since: router ids
                except (OSError, ValueError):
                    pass
            try:
                # New versions of apps looked into before: looked into again without asking.
                mcp_scan.look_inside(mcp_scan.updates_to_look_into())
            except OSError:
                pass
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
            page = self.page_name()
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
        if self.page_name() != "addskills":
            self.skills_return = self.view if self.page_name() in ai_core.VIEWS else "main"
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
        self.show_page("addskills")
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

    # --- the setup choice in the header (All · Online · Local LLMs) ----------------------------

    def setup_mode(self) -> str:
        mode = ai_core.load_settings().get("setup_mode", "all")
        return mode if mode in ("all", "online", "local") else "all"

    def local_capable(self, agent) -> bool:
        """An agent that can work with a local LLM: one AI Agents Map connects, one tied
        to a local LLM, or one whose settings have a local provider already."""
        if agent.command in connect.SUPPORTED or agent.command in connect.FIXED:
            return True
        return any(agent.command in rt.agents for rt in self.inventory.runtimes)

    def mode_rules(self, mode: str | None = None) -> tuple[set, set]:
        """(map categories left out, node ids left out) for a setup choice."""
        mode = mode or self.setup_mode()
        if mode == "online":
            return {"local"}, set()
        if mode == "local":
            return {"online"}, {f"agent:{a.command}" for a in self.agent_list if not self.local_capable(a)}
        return set(), set()

    def set_setup_mode(self, mode: str):
        if getattr(self, "side_map", None) is None:
            return                 # the header is built before the map: the saved choice is applied with it
        if mode == self.setup_mode() and getattr(self, "mode_applied", False):
            return
        self.save_setting(setup_mode=mode)
        self.apply_setup_mode()
        self.refresh_overview()

    def apply_setup_mode(self):
        """The choice on the map: the Show chip of a category it leaves out goes, the
        GPU meters too when there are no local LLMs."""
        self.mode_applied = True
        off, hidden = self.mode_rules()
        self.side_map.set_mode(off, hidden)
        for layer, button in getattr(self, "layer_buttons", {}).items():
            button.set_visible(layer not in off)
        if getattr(self, "gpu_line", None) is not None:
            self.gpu_line.set_visible("local" not in off)
        self.set_selection([i for i in self.selection if i not in hidden])

    # --- the overview's own filter (like the map's column lists) ------------------------------

    def card_hidden(self) -> set:
        value = ai_core.load_settings().get("card_hidden", [])
        return {str(v) for v in value[:500]} if isinstance(value, list) else set()

    def card_groups(self) -> list:
        """(heading, [(card id, label)]) of every card the overview can show."""
        inv, names = self.inventory, self.agent_names()
        off, mode_hidden = self.mode_rules()
        agents = [a for a in self.agent_list if a.installed and f"agent:{a.command}" not in mode_hidden]
        groups = [(_("Agents"), [(f"agent:{a.command}", a.name) for a in agents])]
        if "local" not in off:
            groups.append((_("Local LLMs"), [(f"runtime:{rt.key}", rt.name) for rt in
                                             sorted(inv.runtimes, key=lambda r: not r.installed)]))
        groups.append((_("MCP servers"), [(f"mcp:{m.name}", m.name) for m in inv.mcp]))
        groups.append((_("Skills"), [(f"skills:{s.path}", s.path) for s in inv.skill_sets]))
        missing = [a for a in self.agent_list if not a.installed and f"agent:{a.command}" not in mode_hidden]
        if missing:
            groups.append((_("Not installed"), [(f"agent:{a.command}", a.name) for a in missing]))
        return groups

    def update_card_filter(self):
        hidden = self.card_hidden()
        ids = {i for _title, items in self.card_groups() for i, _label in items}
        count = len(hidden & ids)
        self.card_filter.set_label(_("Show") + " ▾" if not count else
                                   ngettext("Show · 1 hidden", "Show · {0} hidden", count).format(count) + " ▾")

    def show_card_list(self, button):
        def changed(ids, shown):
            hidden = self.card_hidden()
            hidden.difference_update(ids) if shown else hidden.update(ids)
            self.save_setting(card_hidden=sorted(hidden))
            self.refresh_overview()
        self.tick_list(button, None, self.card_groups(), self.card_hidden(), changed)

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
        groups = [(title, [(node.id, label) for node, label in items])
                  for title, items in self.column_list_groups(graph_now, column)]
        self.tick_list(view, rect, groups, set(view.hidden_ids), self.set_nodes_shown)

    def tick_list(self, parent, rect, groups, hidden: set, changed):
        """A popover of tick boxes in groups (a group's own box ticks all of it), with
        All and None: the map's column lists and the overview's Show list.
        changed(ids, shown) is called for every change."""
        popover = Gtk.Popover()
        popover.set_parent(parent)
        if rect is not None:
            area = Gdk.Rectangle()
            area.x, area.y, area.width, area.height = (int(v) for v in rect)
            popover.set_pointing_to(area)
        popover.set_position(Gtk.PositionType.BOTTOM)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.add_css_class("am-provider-list")
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
            changed(members, shown)

        def on_check(item_id, shown, heading, members):
            if syncing[0]:
                return
            changed([item_id], shown)
            sync_heading(heading, members)

        for title, items in groups:
            if not items:
                continue
            members = [item_id for item_id, _label in items]
            heading = Gtk.CheckButton(label=title)
            heading.add_css_class("am-list-group")
            box.append(heading)
            for item_id, label in items:
                check = Gtk.CheckButton(label=label, margin_start=18)
                check.set_active(item_id not in hidden)
                check.connect("toggled", lambda b, i=item_id, h=heading, m=members: on_check(i, b.get_active(), h, m))
                box.append(check)
                checks[item_id] = check
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
            changed(list(checks), shown)

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
                  "more": lambda n: _("+{0} more").format(n),
                  "running": lambda n: _("running") if n == 1 else _("{0} running").format(n),
                  "in_use": lambda names: _("in use · {0}").format(names)}
        new_mcp, new_skills = self.new_found()
        labels["unused"] = _("not used by any agent")
        labels["add_mcp"] = _("+ MCP server") + (" · " + _("{0} new").format(len(new_mcp)) if new_mcp else "")
        labels["add_skills"] = _("+ Skills") + (" · " + _("{0} new").format(len(new_skills)) if new_skills else "")
        labels["add_model"] = _("+ Model")
        built = graph.build(self.agent_list, details, self.inventory, labels, link_hints.load(), self.running)
        headers = [_("MCP servers · skills"), _("Agents"), _("LLM providers"), _("Models")]
        hidden = set(ai_core.hidden_nodes())
        for view in (self.side_map,):
            view.group_titles = {"mcp": _("MCP servers"), "skills": _("Skills"), "online": _("Online")}
            view.set_graph(built, headers)
            view.set_layers(self.map_view.layers)
            view.set_hidden_ids(hidden)
        self.apply_setup_mode()
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
        self.show_page(name)
        self.update_header()
        if save and ai_core.start_view() == "last":
            try:
                ai_core.update_settings(last_view=name)
            except OSError:
                pass

    def update_header(self):
        page = self.page_name()
        on_settings = page in ("settings", "help", "connect", "install", "models", "mcp", "addmcp", "addskills")
        # Faded out rather than hidden: the header keeps its height, so the
        # card does not jump when the settings page opens.
        for widget in (self.settings_button, self.help_button, self.view_switch):
            widget.set_opacity(0 if on_settings else 1)
            widget.set_can_target(not on_settings)
            widget.set_can_focus(not on_settings)
            widget.set_sensitive(not on_settings)
        # The sidebar button only where there is a sidebar: the Overview with the map beside the cards.
        self.sidebar_button.set_visible((page == "main" or page in RIGHT_PAGES) and self.split)
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
        entry = link_hints.load().get(link_hints.key(agent_id, old_id))
        if entry is not None and not entry["applied"]:
            # A red line that was never written: only it goes (and maybe a new try elsewhere).
            link_hints.forget(link_hints.key(agent_id, old_id))
            self.refresh_map()
            if new_id is not None:
                self.on_map_link(agent_id, new_id)
            return
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
        fixed = connect.FIXED.get(agent.command)
        self.connect_preview_heading.set_visible(supported)
        self.connect_preview_scroller.set_visible(supported)
        self.connect_ok.set_visible(supported)
        self.connect_remove.set_visible(False)
        self.connect_status.set_label("")
        if fixed:
            fixed_name = next((r.name for r in self.inventory.runtimes if r.key == fixed), fixed)
            self.connect_intro.set_label(
                (_("{0} uses {1}'s models by itself: there is nothing to connect.") if runtime.key == fixed else
                 _("{0} can only use {1}'s models (and its own cloud); it cannot be connected to {2}."))
                .format(agent.name, fixed_name, runtime.name))
        elif not supported:
            self.connect_intro.set_label(_("{0} cannot be connected from here yet. Hermes, Pi, OpenCode and "
                                           "DeepSeek Harness can.").format(agent.name))
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
        self.show_page("connect")

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
        if agent.command in mcp_ctl.IN_APP:
            self.connect_intro.set_label(_("{0} sets up its MCP servers in its own app ({1}): add {2} there.")
                                         .format(agent.name, mcp_ctl.IN_APP[agent.command], name))
        elif not supported:
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
        self.show_page("connect")

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
        self.show_page("connect")

    def open_mcp_update(self, candidate):
        """An app's newer version: its users move to it (preview, backup, Undo)."""
        names = self.agent_names()
        self.connecting = {"update": candidate, "from": self.view, "remove_only": False}
        self.connect_title.set_label(_("Update {0}").format(candidate.name).upper())
        form = self.connect_form
        while (child := form.get_first_child()) is not None:
            form.remove(child)
        self.connect_status.set_label("")
        self.connect_preview_heading.set_visible(True)
        self.connect_preview_scroller.set_visible(True)
        self.connect_ok.set_visible(True)
        self.connect_ok.set_label(_("Update"))
        self.connect_remove.set_visible(False)
        folder = mcp_update.new_folder(candidate)
        text = _("{0} {1} is replaced by {2}.").format(candidate.name, candidate.update_from, candidate.version)
        if candidate.appimage and not folder.exists():
            text += " " + _("Unpacks {0} into {1} first (the server lives inside it).").format(
                Path(candidate.appimage).name, skills_ctl.tilde(folder))
        users = [names.get(a, a) for a in candidate.users if a != "list"]
        if users:
            text += " " + _("{0} start the new version from then on.").format(", ".join(users))
        text += " " + _("The old version is kept with the backup for {0} days: Undo brings everything back.").format(
            connect.RETIRED_DAYS)
        self.connect_intro.set_label(text)
        self.update_connect_preview()
        self.show_page("connect")

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
        self.show_page("connect")

    def current_connect_plan(self, remove: bool = False):
        info = self.connecting
        if "update" in info:
            return mcp_update.plan(info["update"], [a.command for a in self.agent_list if a.installed])
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
        candidate = info.get("update")
        if candidate is not None and candidate.appimage and not info.get("unpacked"):
            # The new version unpacked first (in the background), then the plan.
            self.connect_ok.set_sensitive(False)
            self.connect_status.set_label(_("Unpacking {0}…").format(Path(candidate.appimage).name))

            def work():
                try:
                    mcp_scan.unpack(Path(candidate.appimage))
                    error = ""
                except (OSError, RuntimeError, subprocess.SubprocessError) as failure:
                    error = str(failure)
                GLib.idle_add(done, error)

            def done(error):
                self.connect_ok.set_sensitive(True)
                if self.connecting is not info:
                    return False
                if error:
                    self.connect_status.set_label(_("Not changed: {0}").format(error))
                else:
                    info["unpacked"] = True
                    self.apply_connect(remove)
                return False

            threading.Thread(target=work, daemon=True).start()
            return
        link = self.link_of(info)
        drawn = link is not None and not remove and not info.get("remove_only")
        try:
            plan_now = self.current_connect_plan(remove=remove)
            if plan_now.empty():
                if link is not None and (remove or info.get("remove_only")) and link_hints.forget(link[0]):
                    self.refresh_map()            # nothing left to remove: the line's hints go
                self.close_connect()
                return
            backup = connect.apply(plan_now)
        except (OSError, ValueError, RuntimeError) as error:
            if not drawn:
                self.connect_status.set_label(_("Not changed: {0}").format(error))
                return
            # A drawn line that could not be written: it stays on the map, dotted red,
            # with why, until it is removed by hand (or drawn again and it works).
            link_key, kind, target = link
            link_hints.remember(link_key, kind, False, [
                link_check.hint("error", _("Not connected: {0}").format(str(error)[:300])),
                link_check.hint("warning", _("Nothing in {0}'s settings was changed. Fix the cause, then click the "
                                             "line → Try again, or drag it off to remove it.").format(info["agent"].name))],
                target)
            self.close_connect()
            self.refresh_map()
            self.show_note(_("{0} could not be connected: see the red line on the map.").format(info["agent"].name))
            return
        remove = remove or bool(info.get("remove_only"))
        if link is not None and remove:
            link_hints.forget(link[0])              # removed by hand: its hints go with it
        if info.get("replaces") is not None and not remove:
            link_hints.forget(link_hints.key(f"agent:{info['agent'].command}", f"runtime:{info['replaces'].key}"))
        if candidate is not None:
            self.found_mcp = None
            self.configured_now = None
            self.close_connect()
            self.collect()
            self.refresh_overview()
            self.refresh_map()
            self.show_undo(_("{0} updated to {1}").format(candidate.name, candidate.version), backup)
            self.check_new_apps()
            return
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
        if drawn:
            self.check_link(link, info["agent"], quiet_ok=True)

    def link_of(self, info):
        """(hint key, kind, target) of the connection on the connect page, or None."""
        if not info or "agent" not in info:
            return None
        agent_id = f"agent:{info['agent'].command}"
        if info.get("mcp"):
            return link_hints.key(agent_id, f"mcp:{info['mcp']}"), "mcp", info["mcp"]
        if info.get("skills"):
            return link_hints.key(agent_id, f"skills:{info['skills']}"), "skills", info["skills"]
        if info.get("runtime") is not None:
            models = getattr(self, "connect_models", None) or []
            model = models[self.connect_model.get_selected()] if models and not info.get("remove_only") else ""
            return link_hints.key(agent_id, f"runtime:{info['runtime'].key}"), "provider", model
        return None

    def check_link(self, link, agent, quiet_ok: bool = False):
        """Right after a line was drawn and written (or on Test again): will it work?
        Problems stay with the line (dotted red or yellow) until it is removed by hand;
        a check that passes takes them away."""
        link_key, kind, target = link
        runtime_key = link_hints.split(link_key)[1].partition(":")[2]
        rt = next((r for r in self.inventory.runtimes if r.key == runtime_key), None) if kind == "provider" else None
        with_agents = bool(ai_core.load_settings().get("providers_with_agents", True))

        def work():
            try:
                if kind == "provider":
                    hints = link_check.check_provider(agent.command, agent.name, rt, target, with_agents) if rt else \
                        [link_check.hint("error", _("This local LLM is not on the map any more."))]
                elif kind == "mcp":
                    hints = link_check.check_mcp(agent.command, agent.name, target)
                else:
                    hints = link_check.check_skills(agent.command, agent.name, target)
            except Exception as error:      # a check must never take the window down
                hints = [link_check.hint("warning", _("Could not check it: {0}").format(str(error)[:200]))]
            GLib.idle_add(done, hints)

        def done(hints):
            if link_hints.failing(hints):
                link_hints.remember(link_key, kind, True, hints, target)
                worst = "error" if any(h["level"] == "error" for h in hints) else "warning"
                self.show_note((_("{0}: connected, but it will not work yet: click the red line for why.")
                                if worst == "error" else
                                _("{0}: connected, with a warning: click the yellow line.")).format(agent.name))
            else:
                had = link_hints.forget(link_key)
                if had or not quiet_ok:
                    self.show_note(next((h["text"] for h in hints if h["level"] == "ok"), _("It works.")))
            self.refresh_map()
            return False

        threading.Thread(target=work, daemon=True).start()

    def show_link_hints(self, edge, rect):
        """A dotted red or yellow line clicked: what went wrong, and what to do."""
        link_key = link_hints.key(*edge)
        entry = link_hints.load().get(link_key)
        agent = next((a for a in self.agent_list if f"agent:{a.command}" == edge[0]), None)
        if entry is None or agent is None:
            return
        popover = Gtk.Popover()
        popover.set_parent(self.side_map)
        area = Gdk.Rectangle()
        area.x, area.y, area.width, area.height = (int(v) for v in rect)
        popover.set_pointing_to(area)
        popover.set_position(Gtk.PositionType.BOTTOM)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.add_css_class("am-provider-list")
        box.set_size_request(420, -1)
        title = (_("Not connected") if not entry["applied"] else
                 _("Connected, but not working") if link_hints.level(entry) == "error" else _("Connected, with a warning"))
        heading = Gtk.Label(label=title.upper(), xalign=0)
        heading.add_css_class("am-group-heading")
        box.append(heading)
        for item in entry["hints"]:
            row = Gtk.Box(spacing=8)
            mark = Gtk.Label(label={"error": "✕", "warning": "!", "ok": "✓"}[item["level"]], valign=Gtk.Align.START)
            mark.add_css_class({"error": "am-hint-error", "warning": "am-hint-warning", "ok": "am-hint-ok"}[item["level"]])
            row.append(mark)
            text = Gtk.Label(label=item["text"], xalign=0, wrap=True, max_width_chars=56)
            text.add_css_class("am-detail-value")
            row.append(text)
            box.append(row)
        if entry.get("when"):
            when = Gtk.Label(label=_("Checked {0}").format(entry["when"].replace("T", " ")), xalign=0)
            when.add_css_class("am-agent-detail")
            box.append(when)
        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        remove = Gtk.Button(label=_("Remove connection"))
        remove.add_css_class("am-small-btn")
        remove.add_css_class("am-danger")
        again = Gtk.Button(label=_("Test again") if entry["applied"] else _("Try again…"))
        again.add_css_class("am-small-btn")
        again.add_css_class("am-primary")
        link = (link_key, entry["kind"], entry.get("target", ""))

        def on_remove(_button):
            popover.popdown()
            if entry["applied"]:
                self.on_map_link(edge[0], edge[1], remove_only=True)     # the usual preview, backup, Undo
            else:
                link_hints.forget(link_key)                               # nothing was written: only the line goes
                self.refresh_map()

        def on_again(_button):
            popover.popdown()
            if entry["applied"]:
                self.show_note(_("Testing…"))
                self.check_link(link, agent)
            else:
                self.on_map_link(edge[0], edge[1])                        # the connect page again

        remove.connect("clicked", on_remove)
        again.connect("clicked", on_again)
        buttons.append(remove)
        buttons.append(again)
        box.append(buttons)
        popover.set_child(box)
        popover.connect("closed", lambda p: GLib.idle_add(p.unparent))
        popover.popup()

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
            (" " + _("Ollama is started for a download if it is not running.") if rt.key == "ollama" else
             " " + _("Jan adds models in its own app (Hub); here you set their context.") if rt.key == "jan" else ""))
        self.models_pull_row.set_visible(rt.key == "ollama")
        self.models_status.set_label("")
        self.models_bar.set_fraction(0)
        self.fill_model_list(rt)
        for listbox in (self.models_repos, self.models_files):
            while (row := listbox.get_first_child()) is not None:
                listbox.remove(row)
        self.update_model_buttons()
        self.show_page("models")
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
                if best and best != now and rt.key in ("llamacpp", "ollama", "lmstudio", "jan", "koboldcpp"):
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
        elif rt.key == "lmstudio":
            entry = context_ctl.lmstudio_entry(name)
            now, cache = context_ctl.lmstudio_settings(entry)
            model_file = context_ctl.lmstudio_model_file(entry)
        elif rt.key == "jan":
            now, cache, model_file = jan_ctl.settings(name)
        elif rt.key == "koboldcpp":
            (now, model_file), cache = kobold_ctl.settings(name), "f16"
        else:
            return None, None, _("set the context in vLLM itself")
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
                elif rt.key == "lmstudio":
                    # LM Studio's own per-model defaults file, the same way (backup, Undo).
                    path = context_ctl.lmstudio_defaults_file(context_ctl.lmstudio_entry(name))
                    if path is None:
                        raise ValueError(_("LM Studio does not list this model"))
                    before = connect._read(path)
                    change = connect.Change(path, before, context_ctl.lmstudio_context_text(before, ctx))
                    backup = connect.apply(connect.Plan("LM Studio", "lmstudio", "", [change]))
                elif rt.key == "jan":
                    # Jan's preset (the engine) and the model's model.yml (the app), one backup.
                    backup = connect.apply(connect.Plan("Jan", "jan", "", jan_ctl.context_changes(name, ctx)))
                elif rt.key == "koboldcpp":
                    path = kobold_ctl.config_path(name)
                    if path is None:
                        raise ValueError(_("KoboldCpp does not list this model"))
                    before = connect._read(path)
                    change = connect.Change(path, before, kobold_ctl.context_text(before, ctx))
                    backup = connect.apply(connect.Plan("KoboldCpp", "koboldcpp", "", [change]))
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
            note = _(" Restart {0} for it to take effect.").format(rt.name) \
                if rt.key in ("llamacpp", "jan") and rt.running else \
                _(" It takes effect the next time {0} loads the model.").format(rt.name) \
                if rt.key in ("lmstudio", "koboldcpp") else ""
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
                                      and state["rt"].key in ("ollama", "llamacpp", "lmstudio", "koboldcpp"))
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
                    if rt.key == "koboldcpp":
                        # The new model becomes a .kcpps: an id the router answers to.
                        install_ctl.installer("koboldcpp").sync(Path(os.path.expanduser(rt.models_dir)))
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
            if not inst.uses_models:
                pass                       # an agent: no model folder
            elif rt.key == "jan":
                # Jan's data folder holds its models, chats and settings; Jan moves it later itself.
                models = [inventory.jan_data_folder()]
                self.install_models = self.folder_field(
                    form, _("Data folder"), _("Jan's models, chats and settings. Change it later in Jan "
                                              "(Settings › General)."), str(models[0]), models)
            else:
                models = install_ctl.models_suggestions(runtimes, folders[0])
                self.install_models = self.folder_field(
                    form, _("Model folder"), _("Where the models go. You can change it later (right-click → Model folder…)."),
                    str(models[0]), models)
            self.add_build_choice(form, inst)
            if inst.has_service:
                self.install_login = Gtk.Switch(active=inst.login_default)
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
        self.show_page("install")
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

    def show_install_agent_context(self, view, agent, rect):
        """An agent AI Agents Map installs itself (DeepSeek Harness, LM Studio Bionic)."""
        import types
        inst = install_ctl.AGENT_INSTALLERS[agent.command]
        ours = inst.installed_folder() is not None
        target = types.SimpleNamespace(key=agent.command, name=agent.name, installed=agent.installed)
        popover = Gtk.Popover()
        popover.set_parent(view)
        area = Gdk.Rectangle()
        area.x, area.y, area.width, area.height = (int(v) for v in rect)
        popover.set_pointing_to(area)
        popover.set_position(Gtk.PositionType.BOTTOM)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.add_css_class("am-provider-list")
        title = Gtk.Label(label=f"{agent.name} · " + (agent.version or _("installed") if agent.installed
                                                      else _("not installed")), xalign=0)
        title.add_css_class("am-agent-name")
        box.append(title)
        actions = [(_("Update…"), "update"), (_("Uninstall…"), "uninstall")] if ours else \
            [] if agent.installed else [(_("Install…"), "install")]
        if agent.installed and not ours:
            note = Gtk.Label(label=_("Installed by hand: update it the way you installed it."), xalign=0, wrap=True)
            note.add_css_class("am-agent-detail")
            box.append(note)
        for label, mode in actions:
            button = Gtk.Button(label=label, halign=Gtk.Align.START)
            button.add_css_class("flat")
            button.add_css_class("am-menu-item")
            button.connect("clicked", lambda _b, m=mode: (popover.popdown(), self.open_install(target, m)))
            box.append(button)
        popover.set_child(box)
        popover.connect("closed", lambda p: GLib.idle_add(p.unparent))
        popover.popup()

    def show_context(self, view, node_id: str, rect):
        """Local LLM: start, stop, start at login. A new agent: look up its setup again."""
        rt = next((r for r in self.inventory.runtimes if f"runtime:{r.key}" == node_id), None)
        if rt is None:
            agent = next((a for a in self.agent_list if f"agent:{a.command}" == node_id), None)
            if agent is not None and agent.command in install_ctl.AGENT_INSTALLERS:
                self.show_install_agent_context(view, agent, rect)
            elif agent is not None and self.is_new_agent(agent.command):
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
            actions += [(_("Update…"), "update")] + ([] if rt.key == "jan" else [(_("Model folder…"), "models")]) \
                + [(_("Uninstall…"), "uninstall")]
        if rt.installed and rt.key in ("ollama", "llamacpp", "lmstudio", "jan", "koboldcpp"):
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
            free = Gtk.Button(label=_("Free GPU memory"))
            free.add_css_class("am-small-btn")
            free.set_sensitive(rt.running)
            free.set_tooltip_text(_("Unloads its models; it keeps running and loads one again when asked."))
            free.connect("clicked", lambda _b: (popover.popdown(),
                                                self.control_runtime(rt, runtime_ctl.unload, _("Free GPU memory"))))
            row.append(free)
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
        # A question with two answers (show_offer), e.g. the launcher entry on the first open.
        self.offer_yes = Gtk.Button()
        self.offer_yes.add_css_class("am-small-btn")
        self.offer_yes.add_css_class("suggested-action")
        self.offer_no = Gtk.Button()
        self.offer_no.add_css_class("am-small-btn")
        self.offer_no.add_css_class("flat")
        self.offer_handlers = []
        for button in (self.offer_yes, self.offer_no):
            button.set_visible(False)
            line.append(button)
        bar.set_child(line)
        self.undo_bar = bar
        self.undo_backup = None
        self.undo_timer = 0
        return bar

    def show_offer(self, message: str, yes: str, no: str, on_yes, on_no):
        """The bar as a question: stays until it is answered."""
        for button, handler in self.offer_handlers:
            button.disconnect(handler)
        self.undo_label.set_label(message)
        self.undo_backup = None
        self.undo_button.set_visible(False)

        def answer(callback):
            self.hide_undo()
            callback()
        self.offer_yes.set_label(yes)
        self.offer_no.set_label(no)
        self.offer_handlers = [(self.offer_yes, self.offer_yes.connect("clicked", lambda *_: answer(on_yes))),
                               (self.offer_no, self.offer_no.connect("clicked", lambda *_: answer(on_no)))]
        self.offer_yes.set_visible(True)
        self.offer_no.set_visible(True)
        if self.undo_timer:
            GLib.source_remove(self.undo_timer)
            self.undo_timer = 0
        self.undo_bar.set_reveal_child(True)

    def show_undo(self, message: str, backup):
        self.offer_yes.set_visible(False)
        self.offer_no.set_visible(False)
        self.undo_label.set_label(message)
        self.undo_backup = backup
        self.undo_button.set_visible(True)
        self.undo_bar.set_reveal_child(True)
        if self.undo_timer:
            GLib.source_remove(self.undo_timer)
        self.undo_timer = GLib.timeout_add_seconds(30, self.hide_undo)

    def show_notice(self, message: str, seconds: int = 8):
        """The undo bar as a plain message (nothing to undo)."""
        self.offer_yes.set_visible(False)
        self.offer_no.set_visible(False)
        self.undo_label.set_label(message)
        self.undo_backup = None
        self.undo_button.set_visible(False)
        self.undo_bar.set_reveal_child(True)
        if self.undo_timer:
            GLib.source_remove(self.undo_timer)
        self.undo_timer = GLib.timeout_add_seconds(seconds, self.hide_undo)

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
        with_agents = Gtk.Switch(active=ai_core.load_settings().get("providers_with_agents", True),
                                 valign=Gtk.Align.CENTER)
        with_agents.connect("notify::active", lambda w, *_: self.settings_ready
                            and (self.save_setting(providers_with_agents=w.get_active()), self.sync_provider_watch()))
        self.setting_line(box, _("Start local LLMs with their agents"),
                          _("When an agent connected to a local LLM starts, its LLM starts too; when the last such "
                            "agent closes, the LLM stops and its GPU memory is free again. Works with this window "
                            "closed and after a reboot (user service {0}).").format(provider_watch.UNIT_NAME),
                          with_agents)
        folder = Gtk.Entry(text=self.agent_folder(), hexpand=True)
        folder.set_max_length(1024)
        folder.connect("activate", self.save_agent_folder)
        leave = Gtk.EventControllerFocus()
        leave.connect("leave", lambda *_: self.save_agent_folder(folder))
        folder.add_controller(leave)
        self.guard_text_fields_in(folder)
        modes = previews.WIDEN_MODES
        mode = self.widen_mode()
        self.setting_line(box, _("Too narrow for the map"),
                          _("What “Maximize to see the map” in the header does when the window is tiled too narrow "
                            "for the map beside the cards."),
                          self.dropdown([_("Move to a free workspace"), _("Maximize where it is"), _("No hint")],
                                        modes.index(mode), lambda i: self.set_widen_mode(modes[i])))
        preview = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        preview.add_css_class("am-help-card")
        preview.set_halign(Gtk.Align.START)
        preview.append(previews.WidenSketch(lambda: self.theme.colors, self.widen_mode))
        box.append(preview)
        launcher = Gtk.Switch(active=ai_core.load_settings().get("launcher_entry") is True, valign=Gtk.Align.CENTER)
        launcher.connect("notify::active", lambda w, *_: self.settings_ready
                         and (self.save_setting(launcher_entry=w.get_active()), app_entry.ensure_entry(w.get_active())))
        self.setting_line(box, _("In the app launcher"),
                          _("AI Agents Map as an app in Omarchy's launcher (Super + Space), besides the bar icon."),
                          launcher)
        self.setting_line(box, _("Agents start in"),
                          _("The folder an agent started here (Start on its card) works in. Enter saves it."), folder)
        self.settings_ready = True

    def save_agent_folder(self, entry):
        text = entry.get_text().strip()
        path = os.path.realpath(os.path.expanduser(text)) if text else ""
        if not path or not os.path.isdir(path) or "\0" in path:
            entry.add_css_class("error")
            return
        entry.remove_css_class("error")
        if self.settings_ready:
            self.save_setting(agent_folder=path)

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
        self.show_page("settings")

    # --- help: the manual (for now: setting up by drawing) ---------------------------------------

    def build_help_page(self) -> Gtk.Widget:
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
        back.connect("clicked", lambda *_: self.close_help())
        header.append(back)
        title = Gtk.Label(label=_("Help").upper(), xalign=0, hexpand=True)
        title.add_css_class("am-section-title")
        header.append(title)
        page.append(header)
        rule = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        rule.add_css_class("am-settings-rule")
        page.append(rule)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        body.add_css_class("am-settings")
        body.append(self.help_setup_card())
        page.append(self.page_scroller(body))
        return page

    def help_setup_card(self) -> Gtk.Widget:
        """Setting up by drawing: an animation with dummy boxes, played on Start."""
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        card.add_css_class("am-help-card")
        top = Gtk.Box(spacing=10)
        heading = Gtk.Label(label=_("Set up by drawing").upper(), xalign=0, hexpand=True)
        heading.add_css_class("am-setting-heading")
        top.append(heading)
        play = Gtk.Button()
        content = Gtk.Box(spacing=6)
        content.append(Gtk.Image.new_from_icon_name("media-playback-start-symbolic"))
        play_label = Gtk.Label(label=_("Start"))
        content.append(play_label)
        play.set_child(content)
        play.add_css_class("am-small-btn")
        top.append(play)
        card.append(top)
        intro = Gtk.Label(label=_("Agents get their LLM, their model, MCP servers and skills by lines you draw on the "
                                  "map. The animation shows it with made-up boxes; nothing on your computer changes."),
                          xalign=0, wrap=True, max_width_chars=1)
        intro.add_css_class("am-agent-detail")
        card.append(intro)
        labels = {"h_mcp": _("MCP servers · skills"), "h_agents": _("Agents"), "h_providers": _("LLM providers"),
                  "h_models": _("Models"), "mcp": _("an MCP server"), "skills": _("a skill folder"),
                  "agent": _("your agent"), "online": _("an online provider"), "local": _("a local LLM"),
                  "model_a": "model-a", "model_b": "model-b"}
        captions = {0: _("Press Start to see how an agent is set up by drawing."),
                    1: _("Drag from the agent to a provider: a line connects them."),
                    2: _("Click a model of that provider: the agent uses it now."),
                    3: _("Drag from the agent to an MCP server: it can use its tools."),
                    4: _("Drag from the agent to a skill folder: it can use those skills."),
                    5: _("Every change shows a preview first and keeps a backup; Undo takes it back.")}
        sketch = previews.SetupSketch(lambda: self.theme.colors, labels, captions,
                                      on_end=lambda: play_label.set_label(_("Replay")))
        play.connect("clicked", lambda *_: sketch.start())
        self.setup_sketch = sketch
        card.append(sketch)
        return card

    def open_help(self):
        self.show_page("help")

    def close_help(self):
        self.show_view(self.view, save=False)

    def close_settings(self):
        self.show_view(self.view, save=False)

    # --- size ----------------------------------------------------------------------

    def launcher_entry_on_open(self):
        """The launcher entry is opt-in: asked once, then kept as the setting says."""
        wanted = ai_core.load_settings().get("launcher_entry")
        if isinstance(wanted, bool):
            app_entry.ensure_entry(wanted)
            return

        def answer(on: bool):
            self.save_setting(launcher_entry=on)
            app_entry.ensure_entry(on)
        GLib.timeout_add(1500, lambda: self.show_offer(
            _("Add AI Agents Map to Omarchy's app launcher (Super + Space)?"), _("Add"), _("Not now"),
            lambda: answer(True), lambda: answer(False)) or False)

    def width_changed(self, _width):
        split = self.split
        self.fit()
        if self.split != split:
            self.place_panes()
            self.update_header()
        self.update_widen_hint()
        return False

    def widen_mode(self) -> str:
        mode = ai_core.load_settings().get("widen", "workspace")
        return mode if mode in previews.WIDEN_MODES else "workspace"

    def set_widen_mode(self, mode: str):
        self.save_setting(widen=mode)
        self.update_widen_hint()

    def widen(self):
        work = app_entry.maximize_here if self.widen_mode() == "maximize" else app_entry.widen
        threading.Thread(target=work, daemon=True).start()

    def update_widen_hint(self):
        """Shown while the map does not fit, unless the window is maximized already
        (then the screen itself is too narrow and maximizing cannot help)."""
        if self.split or self.widen_mode() == "off":
            self.widen_hint.set_visible(False)
            return

        def work():
            state = app_entry.maximized()
            GLib.idle_add(lambda: self.widen_hint.set_visible(not self.split and state is not True) and False)

        threading.Thread(target=work, daemon=True).start()

    def size_limits(self) -> tuple[int, int]:
        width = getattr(self.window, "last_width", 0) or (self.window.get_width() if self.window is not None else 0)
        if width <= 0:
            geo = self.monitor_geometry() if self.window is not None else None
            width = geo.width if geo is not None else 1920
        return (max(CARD_WIDTH, width),
                max(ai_core.MIN_WINDOW_SIZE[1], monitor_height() - 2 * SCREEN_MARGIN))

    def set_sidebar(self, visible: bool):
        """The cards left of the side map: hidden, the map takes the whole width."""
        self.sidebar_visible = visible
        if getattr(self, "list_pane", None) is None:
            return
        self.place_panes()
        if self.sidebar_button.get_active() != visible:
            self.sidebar_button.set_active(visible)
        self.update_header()

    def fit(self):
        """As large as the screen allows (checked on every open: another monitor)."""
        self.card_w, self.list_h = self.size_limits()
        if getattr(self, "side_pane", None) is not None:
            # A very small screen keeps the cards alone.
            self.split = self.card_w >= SPLIT_WIDTH
            self.list_pane.set_hexpand(not self.split)
            self.list_pane.set_size_request(LIST_PANE_WIDTH if self.split else -1, -1)
            self.set_sidebar(self.sidebar_visible)
        for scroller in self.scrollers:
            # The card fills the window's height; the scrollers only need a small minimum.
            scroller.set_max_content_height(-1)
            scroller.set_min_content_height(SCROLLER_MIN_HEIGHT)
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
        window#aiagentsmap-window {{ background: {bg_hex}; }}
        .am-card {{ background: {bg_hex}; color: {fg_hex}; border: none;
            border-radius: 0; padding: 17px; }}
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
        .am-status-hero {{ background: alpha({fg_hex}, 0.04); border: 1px solid alpha({fg_hex}, 0.10);
            border-radius: {radius}px; padding: 12px 14px; }}
        .am-status-mark {{ font-size: 22px; font-weight: 700; min-width: 30px; }}
        .am-status-title {{ font-size: 14px; font-weight: 600; }}
        .am-state-ok {{ color: {green_hex}; }}
        .am-state-warn {{ color: {yellow_hex}; }}
        .am-state-missing {{ color: {red_hex}; }}
        .am-state-info {{ color: {dim_hex}; }}
        .am-check-mark {{ font-weight: 700; min-width: 14px; }}
        .am-check-step {{ font-size: 11px; }}
        .am-check-step.am-state-info {{ color: {dim_hex}; }}
        .am-stat-tile {{ background: alpha({fg_hex}, 0.03); border: 1px solid alpha({fg_hex}, 0.08);
            border-radius: {radius}px; padding: 8px 10px; }}
        .am-command-line {{ background: alpha({fg_hex}, 0.06); border-radius: 6px; padding: 2px 4px 2px 10px;
            margin-top: 3px; }}
        .am-command {{ font-family: "JetBrainsMono Nerd Font", monospace; font-size: 12px; }}
        .am-card button.am-widen-hint {{ color: {accent_hex}; border: 1px solid alpha({accent_hex}, 0.45);
            padding: 1px 10px; }}
        .am-undo button.suggested-action {{ background: {accent_hex}; color: {bg_hex}; border: none; }}
        .am-help-card {{ background: alpha({fg_hex}, 0.03); border: 1px solid alpha({fg_hex}, 0.10);
            border-radius: {radius}px; padding: 12px 14px; }}
        .am-help-mark {{ font-weight: 700; }}
        .am-stat-value {{ font-size: 17px; font-weight: 600; }}
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
        popover button.am-danger {{ color: {red_hex}; border-color: alpha({red_hex}, 0.6); }}
        .am-hint-error {{ color: {red_hex}; font-weight: bold; }}
        .am-hint-warning {{ color: {yellow_hex}; font-weight: bold; }}
        .am-hint-ok {{ color: {green_hex}; font-weight: bold; }}
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
        .am-sidebar {{ background: alpha({fg_hex}, 0.03); border: 1px solid alpha({fg_hex}, 0.10);
            border-radius: {radius}px; padding: 10px 10px 0 10px; }}
        .am-side-rule {{ margin: 0 14px; background: alpha({fg_hex}, 0.14); min-width: 1px; }}
        .am-segmented {{ border: 1px solid alpha({fg_hex}, 0.22); border-radius: 999px; padding: 2px; }}
        .am-card button.am-segment {{ font-size: 11px; min-height: 0; padding: 2px 12px; border-radius: 999px;
            border: none; background: none; box-shadow: none; color: {dim_hex}; }}
        .am-card button.am-segment:checked {{ background: alpha({accent_hex}, 0.22); color: {fg_hex}; }}
        .am-card button.am-segment:hover:not(:checked) {{ color: {fg_hex}; }}
        .am-card button.am-filter-btn {{ font-size: 11px; }}
        .am-card .am-leader {{ min-height: 0; border-bottom: 1px dotted alpha({fg_hex}, 0.35); margin: 0 4px; }}
        .am-card .am-choice {{ padding: 4px 6px; border-radius: {radius}px; }}
        .am-card .am-choice:hover {{ background: alpha({fg_hex}, 0.05); }}
        .am-card .am-choice:hover .am-leader {{ border-bottom-color: alpha({accent_hex}, 0.8); }}
        .am-canvas {{ border: 1px dashed alpha({fg_hex}, 0.25); border-radius: {radius}px; padding: 12px; }}
        """
        for view in (getattr(self, "map_view", None), getattr(self, "side_map", None)):
            if view is not None:
                view.set_palette({"bg": bg_hex, "fg": fg_hex, "dim": dim_hex, "muted": muted_hex,
                                  "accent": accent_hex, "green": green_hex, "yellow": yellow_hex, "red": red_hex},
                                 radius)
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
        page = self.page_name()
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        if page == "terminal":
            return False          # Esc, Backspace, Ctrl+… belong to the agent
        if keyval == Gdk.KEY_F1 and page != "help":
            self.open_help()
            return True
        if page == "help" and keyval in (Gdk.KEY_Escape, Gdk.KEY_BackSpace):
            self.close_help()
            return True
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
            elif page == "status":
                self.close_status()
            elif page == "addmcp":
                self.close_add_mcp()
            elif page == "addskills":
                self.close_add_skills()
            elif page == "main" and self.selection:
                self.set_selection([])   # Esc clears the selection; the window stays (an app)
            return True
        if page in ("install", "models", "addmcp", "addskills") and keyval == Gdk.KEY_BackSpace and \
                isinstance(self.window.get_focus(), (Gtk.Text, Gtk.TextView)):
            return False          # Backspace edits the folder fields
        if page in ("models", "addmcp", "addskills"):
            if page in ("addmcp", "addskills") and keyval == Gdk.KEY_BackSpace:
                self.close_add_mcp() if page == "addmcp" else self.close_add_skills()
                return True
            return False          # typing in the search fields
        if page in ("settings", "connect", "install", "mcp", "status"):
            if keyval == Gdk.KEY_BackSpace:
                {"settings": self.close_settings, "connect": self.close_connect,
                 "install": self.close_install, "mcp": self.close_mcp_details,
                 "status": self.close_status}[page]()
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
        if ctrl and keyval in (Gdk.KEY_b, Gdk.KEY_B) and (page == "main" or page in RIGHT_PAGES) and self.split:
            self.set_sidebar(not self.sidebar_visible)
            return True
        return False

    # --- open / close ------------------------------------------------------------

    def toggle(self):
        """The bar icon: open it, bring it to the front, or (already in front) hide it."""
        if not self.window.is_visible():
            self.open()
        elif self.window.is_active():
            self.close()
        else:
            self.window.present()
            app_entry.focus_window()

    def open(self):
        self.sidebar_visible = True
        self.fit()
        self.collect()
        self.refresh_overview()
        self.refresh_map()
        self.window.present()
        self.sync_provider_watch()
        self.start_gpu_poll()
        self.research_new_agents()
        self.check_new_apps()
        self.launcher_entry_on_open()
        # Restarted after a language change: back on the settings page.
        view = os.environ.pop("AI_AGENTS_MAP_VIEW", "")
        if view in ai_core.VIEWS:
            self.show_view(view, save=False)
        page = os.environ.pop("AI_AGENTS_MAP_PAGE", "")
        if page == "settings":
            GLib.idle_add(self.open_settings)
        elif page in ("help", "help:play"):
            GLib.idle_add(self.open_help)
            if page == "help:play":
                GLib.timeout_add(600, lambda: self.setup_sketch.start() or False)
        elif page == "addmcp":
            GLib.idle_add(self.open_add_mcp)
        elif page == "addskills":
            GLib.idle_add(self.open_add_skills)
        elif page.startswith("agent:"):
            agent = next((x for x in self.agent_list if x.command == page[6:]), None)
            if agent is not None:
                GLib.idle_add(self.open_agent_status, agent)
        elif page.startswith("runtime:"):
            GLib.idle_add(self.open_runtime_status, page[8:])
        elif page.startswith("details:") and mcp_ctl.NAME_RE.match(page[8:]):
            GLib.idle_add(self.open_mcp_details, page[8:])

    def close(self):
        """Hide the window (agents running in it keep running) and remember its size."""
        width, height = self.window.get_width(), self.window.get_height()
        if width >= 400 and height >= 400 and [width, height] != ai_core.load_settings().get("window_size"):
            try:
                ai_core.update_settings(window_size=[width, height])
            except OSError:
                pass
        self.stop_gpu_poll()
        self.map_view.set_shift(False)   # Shift may have been let go elsewhere
        if self.page_name() == "settings":
            self.close_settings()   # never reopen onto the settings page
        elif self.page_name() == "connect":
            self.close_connect()    # nor onto a half-made connection
        elif self.page_name() == "install" and not (self.installing or {}).get("busy"):
            self.close_install()
        self.window.hide()


def main():
    app = AgentsMap()
    atexit.register(cleanup_pid)
    return app.run()


if __name__ == "__main__":
    sys.exit(main())
