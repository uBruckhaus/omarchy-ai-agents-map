#!/usr/bin/env python3
"""AI Manager popup: a Wayland layer-shell card with two views (AI Manager
and Builder) and a settings page.

Same house layout as Theme Manager: a singleton process woken by SIGUSR1
from ai-manager-toggle, a themed card that can be moved by its header and
resized by the corner grip (position and size are remembered), and a
size-dependent header double-click.
"""

import colorsys
import os
import datetime as dt
import signal
import sys
import threading
import time

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
import i18n
import pidfile
from i18n import _, ngettext
from layer_watch import LayerWatch
from palette import ThemePalette

# Default outer size as in the other popups: a 420 px card around a 590 px
# page, the same on every page so switching never resizes the popup.
CARD_WIDTH = 420
LIST_HEIGHT = 590
SCREEN_MARGIN = 120    # space kept free above and below the card
CURSOR_SLACK = 24      # px the pointer may move after opening before the keyboard is released
AGENT_SPACING = 8
DETAIL_LABEL_WIDTH = 90   # the dim labels in an opened agent card
VIEW_TITLES = {"main": "AI Manager", "builder": "Builder"}   # names, not translated


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


def monitor_height() -> int:
    """Logical height of the smallest monitor, so the card always fits."""
    display = Gdk.Display.get_default()
    heights = []
    if display is not None:
        monitors = display.get_monitors()
        for i in range(monitors.get_n_items()):
            heights.append(monitors.get_item(i).get_geometry().height)
    return min(heights) if heights else 1080


class AiManager(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="org.omarchy.aimanager")
        self.window = None
        self.theme = ThemePalette()
        self.style_provider = None
        # Omarchy's menus (Super + Space, …) open underneath this popup: close for them.
        self.layer_watch = LayerWatch("ai-manager", self.on_menu_opened)
        self.position = ai_core.window_position()   # None: centred
        self.card_w, self.list_h = ai_core.window_size() or (CARD_WIDTH, LIST_HEIGHT)
        self.drag_mode = None       # "move" or "resize" while the header or grip is dragged
        self.drag_start = None
        self.resized_at = 0.0
        self.scrollers = []        # every page's scroller follows the page height
        self.view = ai_core.last_view() if ai_core.start_view() == "last" else ai_core.start_view()
        self.settings_ready = False
        self.expanded = set()      # commands of the agent cards shown with their details
        self.refreshing = set()    # commands whose usage Omarchy is collecting right now
        self.detail_boxes = {}     # command -> details box of the card on screen

    def do_shutdown(self):
        cleanup_pid()
        super().do_shutdown()

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
        window.set_title("AI Manager")
        window.set_name("aimanager-window")
        Gtk4LayerShell.init_for_window(window)
        Gtk4LayerShell.set_namespace(window, "ai-manager")
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
        # Moving (header) and resizing (grip) are tracked on the overlay, which
        # never moves, so the pointer's way is exact.
        dragger = Gtk.GestureDrag()
        dragger.connect("drag-begin", self.on_drag_begin)
        dragger.connect("drag-update", self.on_drag_update)
        dragger.connect("drag-end", self.on_drag_end)
        overlay.add_controller(dragger)
        window.connect("notify::is-active", self.on_active_changed)

        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        card.add_css_class("am-card")
        frame = Gtk.Overlay()
        frame.set_child(card)
        overlay.add_overlay(frame)
        self.card = card
        self.card_frame = frame
        # Resize grip in the bottom-right corner (a layer surface has no frame).
        grip = Gtk.Image.new_from_icon_name("pan-down-symbolic")
        grip.add_css_class("am-grip")
        grip.set_pixel_size(14)
        grip.set_halign(Gtk.Align.END)
        grip.set_valign(Gtk.Align.END)
        grip.set_cursor_from_name("se-resize")
        grip.set_tooltip_text(_("Drag to resize · double-click for the default size"))
        frame.add_overlay(grip)
        self.grip = grip
        self.list_h = max(ai_core.MIN_WINDOW_SIZE[1], min(self.list_h, monitor_height() - 2 * SCREEN_MARGIN))

        card.append(self.build_header())

        stack = Gtk.Stack()
        stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
        stack.set_transition_duration(140)
        stack.set_interpolate_size(False)
        stack.set_hhomogeneous(True)
        stack.set_vhomogeneous(True)
        stack.add_named(self.build_main_page(), "main")
        stack.add_named(self.build_builder_page(), "builder")
        stack.add_named(self.build_settings_page(), "settings")
        stack.connect("notify::visible-child-name", lambda *_: self.update_header())
        card.append(stack)
        self.stack = stack

        # Capture phase: a focused control must not see Esc/Backspace first.
        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self.on_key)
        window.add_controller(keys)

        self.resize_to(self.card_w, self.list_h)
        self.apply_theme_css()
        self.show_view(self.view, save=False)

    def build_header(self) -> Gtk.Widget:
        header = Gtk.Box(spacing=8)
        self.header = header
        title = Gtk.Label(label="AI Manager", xalign=0)
        title.add_css_class("am-title")
        title.set_ellipsize(Pango.EllipsizeMode.END)
        title.set_max_width_chars(30)
        title.set_tooltip_text(_("Drag to move"))
        header.append(title)
        self.title_label = title
        # Centred between the title and the buttons: what a header double-click
        # does at this size. max_width_chars(1): no natural width of its own.
        mouse_hint = Gtk.Label(xalign=0.5)
        mouse_hint.add_css_class("am-header-hint")
        mouse_hint.set_ellipsize(Pango.EllipsizeMode.END)
        mouse_hint.set_max_width_chars(1)
        mouse_hint.set_hexpand(True)
        mouse_hint.set_margin_start(6)
        mouse_hint.set_margin_end(6)
        header.append(mouse_hint)
        self.mouse_hint = mouse_hint

        # Drag the header to move the card (Hyprland keeps Super + drag for itself).
        header.set_cursor_from_name("grab")
        right_clicks = Gtk.GestureClick(button=Gdk.BUTTON_SECONDARY)
        right_clicks.connect("pressed", lambda _g, count, _x, _y: count == 2
                             and self.on_header_double_click(Gdk.BUTTON_SECONDARY))
        header.add_controller(right_clicks)
        # Left drag / double-click: on_drag_begin (the overlay sees them).

        # The two views, as a pair of flat toggle buttons.
        self.view_buttons = {}
        views = Gtk.Box(spacing=0)
        views.add_css_class("am-view-switch")
        for name, icon, tip in (("main", "view-list-symbolic", _("AI Manager view (Ctrl+1)")),
                                ("builder", "applications-engineering-symbolic", _("Builder view (Ctrl+2)"))):
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
        page.append(self.view_heading(_("Agents")))
        self.agents_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=AGENT_SPACING)
        self.agents_box.add_css_class("am-agents")
        page.append(self.page_scroller(self.agents_box))
        return page

    def refresh_agents(self):
        """Rebuild the agent cards (file system only, nothing is started)."""
        box = self.agents_box
        while (child := box.get_first_child()) is not None:
            box.remove(child)
        self.detail_boxes = {}
        found = agents.list_agents()
        installed = [a for a in found if a.installed]
        missing = [a for a in found if not a.installed]
        for title, group in ((_("Installed ({0})").format(len(installed)), installed),
                             (_("Not installed ({0})").format(len(missing)), missing)):
            if not group:
                continue
            heading = Gtk.Label(label=title.upper(), xalign=0)
            heading.add_css_class("am-group-heading")
            box.append(heading)
            for agent in group:
                box.append(self.build_agent_card(agent))

    def build_agent_card(self, agent) -> Gtk.Widget:
        """One agent: icon, name, version; a click shows or hides its details."""
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        card.add_css_class("am-agent")
        if not agent.installed:
            card.add_css_class("am-agent-missing")
        top = Gtk.Box(spacing=10)
        top.set_cursor_from_name("pointer")
        icon = Gtk.Label(label=agent.icon)
        icon.add_css_class("am-agent-icon")
        if agent.icon_font == "omarchy":
            icon.add_css_class("am-agent-icon-omarchy")
        icon.set_valign(Gtk.Align.CENTER)
        top.append(icon)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        text.set_hexpand(True)
        text.set_valign(Gtk.Align.CENTER)
        name = Gtk.Label(label=agent.name, xalign=0)
        name.add_css_class("am-agent-name")
        name.set_ellipsize(Pango.EllipsizeMode.END)
        name.set_max_width_chars(1)
        name.set_hexpand(True)
        text.append(name)
        status = agent.version if agent.installed else _("Not installed")
        line = Gtk.Box(spacing=6)
        if agent.default:
            # In the version line, so the name keeps the card's full width.
            card.add_css_class("am-agent-default")
            badge = Gtk.Label(label=_("Default"))
            badge.add_css_class("am-agent-badge")
            badge.set_tooltip_text(_("Omarchy's default agent"))
            line.append(badge)
        detail = Gtk.Label(label=status or _("Installed"), xalign=0)
        detail.add_css_class("am-agent-detail")
        detail.set_ellipsize(Pango.EllipsizeMode.END)
        detail.set_max_width_chars(1)
        detail.set_hexpand(True)
        line.append(detail)
        text.append(line)
        top.append(text)
        expanded = agent.command in self.expanded
        chevron = Gtk.Image.new_from_icon_name("pan-up-symbolic" if expanded else "pan-down-symbolic")
        chevron.add_css_class("am-agent-chevron")
        chevron.set_valign(Gtk.Align.CENTER)
        top.append(chevron)
        top.set_tooltip_text(_("Click for details"))
        card.append(top)

        revealer = Gtk.Revealer()
        revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        revealer.set_transition_duration(150)
        details = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        details.add_css_class("am-agent-details")
        revealer.set_child(details)
        card.append(revealer)
        self.detail_boxes[agent.command] = (details, agent)
        if expanded:
            self.fill_details(agent.command)
            revealer.set_reveal_child(True)
            if agent.installed and agent_details.usage_is_stale(agent.command):
                self.refresh_usage(agent.command)

        click = Gtk.GestureClick()
        click.connect("released", lambda *_: self.toggle_card(agent, revealer, chevron))
        top.add_controller(click)
        return card

    def toggle_card(self, agent, revealer, chevron):
        if agent.command in self.expanded:
            self.expanded.discard(agent.command)
            revealer.set_reveal_child(False)
            chevron.set_from_icon_name("pan-down-symbolic")
            return
        self.expanded.add(agent.command)
        self.fill_details(agent.command)
        revealer.set_reveal_child(True)
        chevron.set_from_icon_name("pan-up-symbolic")
        if agent.installed and agent_details.usage_is_stale(agent.command):
            self.refresh_usage(agent.command)

    def refresh_usage(self, command: str):
        """Ask Omarchy for fresh usage on a thread; fill the card again after."""
        if command in self.refreshing:
            return
        self.refreshing.add(command)
        self.fill_details(command)

        def work():
            agent_details.refresh_usage(command)
            GLib.idle_add(done)

        def done():
            self.refreshing.discard(command)
            self.fill_details(command)
            return False

        threading.Thread(target=work, daemon=True).start()

    def fill_details(self, command: str):
        entry = self.detail_boxes.get(command)
        if entry is None:
            return
        box, agent = entry
        while (child := box.get_first_child()) is not None:
            box.remove(child)
        info = agent_details.details_for(command)
        grid = Gtk.Grid(column_spacing=10, row_spacing=4)
        rows = []

        def row(label: str, value: str, tip: str = ""):
            if value:
                rows.append((label, value, tip))

        if not agent.installed:
            row(_("Status"), _("Not installed"),
                _("Install it from Omarchy's menu: Setup → Defaults → Agent."))
        row(_("Plan"), info.plan, _("Your subscription, as Omarchy's usage data reports it."))
        row(_("Model"), info.model, _("The model set in the agent's settings."))
        for provider in info.providers:
            parts = [provider.name]
            if provider.local:
                parts.append(_("local · {0}").format(provider.runtime) if provider.runtime else _("local"))
            if provider.models:
                parts.append(ngettext("1 model", "{0} models", provider.models).format(provider.models))
            row(_("Provider"), " · ".join(parts))
        if info.today_prompts is not None or info.today_tokens is not None:
            today = []
            if info.today_prompts is not None:
                today.append(ngettext("1 prompt", "{0} prompts", info.today_prompts).format(info.today_prompts))
            if info.today_sessions is not None:
                today.append(ngettext("1 session", "{0} sessions", info.today_sessions).format(info.today_sessions))
            if info.today_tokens:
                today.append(_("{0} tokens").format(short_number(info.today_tokens)))
            row(_("Today"), " · ".join(today))
        if info.total_sessions:
            total = ngettext("1 session", "{0} sessions", info.total_sessions).format(info.total_sessions)
            if info.active_days:
                total += " · " + ngettext("1 active day", "{0} active days", info.active_days).format(info.active_days)
            row(_("In total"), total)
        if agent.installed:
            row(_("Last used"), when(info.last_used))
            source = {"mise": _("with mise"), "package": _("as a package"),
                      "own": _("your own install")}.get(agent.source, "")
            row(_("Installed"), " · ".join(x for x in (source, agent.version) if x))
        row(_("Command"), agent.command)
        row(_("Settings"), info.config_dir)

        for index, (label, value, tip) in enumerate(rows):
            name = Gtk.Label(label=label, xalign=0, yalign=0)
            name.add_css_class("am-line-label")
            name.set_size_request(DETAIL_LABEL_WIDTH, -1)
            text = Gtk.Label(label=value, xalign=0, wrap=True, max_width_chars=1, hexpand=True)
            text.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
            text.add_css_class("am-detail-value")
            text.set_selectable(False)
            if tip:
                name.set_tooltip_text(tip)
                text.set_tooltip_text(tip)
            grid.attach(name, 0, index, 1, 1)
            grid.attach(text, 1, index, 1, 1)
        box.append(grid)

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
            note = Gtk.Label(label=status, xalign=0, wrap=True, max_width_chars=1)
            note.add_css_class("am-agent-detail")
            box.append(note)

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

    def build_builder_page(self) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        page.append(self.view_heading(_("Builder")))
        canvas = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        canvas.add_css_class("am-canvas")
        canvas.set_vexpand(True)
        canvas.append(self.empty_state("applications-engineering-symbolic", _("Empty canvas"),
                                       _("Build something here.")))
        page.append(self.page_scroller(canvas))
        return page

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
        on_settings = page == "settings"
        # Faded out rather than hidden: the header keeps its height, so the
        # card does not jump when the settings page opens.
        for widget in (self.settings_button, self.view_switch):
            widget.set_opacity(0 if on_settings else 1)
            widget.set_can_target(not on_settings)
            widget.set_can_focus(not on_settings)
            widget.set_sensitive(not on_settings)
        self.mouse_hint.set_visible(not on_settings)
        # A sub-page hides the hint: the title takes the room again.
        self.title_label.set_hexpand(on_settings)
        self.updating_buttons = True
        for name, button in self.view_buttons.items():
            button.set_active(name == page)
        self.updating_buttons = False

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
        self.setting_line(box, _("Language"), _("AI Manager restarts in the new language."),
                          self.dropdown([_("System ({0})").format(i18n.language_name(i18n.system_language()))]
                                        + [name for _code, name in i18n.LANGUAGES],
                                        codes.index(setting) if setting in codes else 0,
                                        lambda i: self.set_language(codes[i])))
        starts = ["main", "builder", "last"]
        self.setting_line(box, _("Open on"), _("The view shown when AI Manager opens."),
                          self.dropdown([_("AI Manager view"), _("Builder view"), _("Last used view")],
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
        os.environ["AI_MANAGER_PAGE"] = "settings"
        os.environ["AI_MANAGER_VIEW"] = self.view
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

    def resize_to(self, width: int, height: int, save: bool = False):
        """Card width and page height, kept within the monitor."""
        max_w, max_h = self.size_limits()
        self.card_w = max(CARD_WIDTH, min(int(width), max_w))
        self.list_h = max(ai_core.MIN_WINDOW_SIZE[1], min(int(height), max_h))
        self.card.set_size_request(self.card_w, -1)
        for scroller in self.scrollers:
            # Raise the maximum first: GTK refuses a minimum above the maximum.
            scroller.set_max_content_height(100000)
            scroller.set_min_content_height(self.list_h)
            scroller.set_max_content_height(self.list_h)
        if self.window is not None:
            # A layer surface does not shrink by itself once content gets smaller.
            self.window.set_default_size(1, 1)
        self.update_mouse_hint()
        if save:
            default = (self.card_w, self.list_h) == (CARD_WIDTH, LIST_HEIGHT)
            try:
                ai_core.save_window_size(None if default else (self.card_w, self.list_h))
            except OSError:
                pass

    def size_state(self) -> str:
        """"default", "max" or "between"."""
        if (self.card_w, self.list_h) == (CARD_WIDTH, LIST_HEIGHT):
            return "default"
        if (self.card_w, self.list_h) == self.size_limits():
            return "max"
        return "between"

    def on_header_double_click(self, button):
        """At the default size either button maximizes, at the largest either
        one goes back; in between left = default size, right = maximize."""
        size = self.size_state()
        if size == "max" or (size == "between" and button == Gdk.BUTTON_PRIMARY):
            self.resize_to(CARD_WIDTH, LIST_HEIGHT, save=True)
        else:
            self.resize_to(*self.size_limits(), save=True)
        self.move_to(None, save=True)
        self.resized_at = time.monotonic()

    def update_mouse_hint(self):
        if getattr(self, "mouse_hint", None) is None:
            return
        size = self.size_state()
        self.mouse_hint.set_label(_("Double-click: maximize") if size == "default" else
                                  _("Double-click: default size") if size == "max" else
                                  _("Double-click: default size · right double-click: maximize"))
        self.mouse_hint.set_tooltip_text(self.mouse_hint.get_label())

    # --- moving ------------------------------------------------------------------

    def monitor_geometry(self):
        display = Gdk.Display.get_default()
        surface = self.window.get_surface()
        monitor = display.get_monitor_at_surface(surface) if display and surface else None
        if monitor is None and display is not None and display.get_monitors().get_n_items():
            monitor = display.get_monitors().get_item(0)
        return monitor.get_geometry() if monitor is not None else None

    def current_position(self) -> tuple[int, int]:
        """Top-left of the card in the overlay."""
        if self.position is not None:
            return self.position
        ok, bounds = self.card_frame.compute_bounds(self.overlay)
        return (round(bounds.get_x()), round(bounds.get_y())) if ok else (0, 0)

    def move_to(self, position, save: bool = False):
        """Place the card's top-left at `position` (kept on screen), or centre it for None."""
        frame = self.card_frame
        if position is not None:
            width, height = self.overlay.get_width(), self.overlay.get_height()
            frame_w, frame_h = frame.get_width(), frame.get_height()
            if width > 0 and height > 0:
                position = (min(max(0, int(position[0])), max(0, width - frame_w)),
                            min(max(0, int(position[1])), max(0, height - frame_h)))
        self.position = position
        if position is None:
            frame.set_halign(Gtk.Align.CENTER)
            frame.set_valign(Gtk.Align.CENTER)
            frame.set_margin_start(0)
            frame.set_margin_top(0)
        else:
            frame.set_halign(Gtk.Align.START)
            frame.set_valign(Gtk.Align.START)
            frame.set_margin_start(position[0])
            frame.set_margin_top(position[1])
        if save:
            try:
                ai_core.save_window_position(position)
            except OSError:
                pass

    def inside(self, widget, ancestor) -> bool:
        return widget is not None and (widget == ancestor or widget.is_ancestor(ancestor))

    def on_drag_begin(self, gesture, x, y):
        """A press on the grip resizes, on the header (not a button) moves the
        card; anything else is not ours. A second press within 0.4 s is a
        double-click."""
        picked = self.overlay.pick(x, y, Gtk.PickFlags.DEFAULT)
        if self.inside(picked, self.grip):
            mode = "resize"
        elif self.inside(picked, self.header) and not self.on_button(picked):
            mode = "move"
        else:
            gesture.set_state(Gtk.EventSequenceState.DENIED)
            self.drag_mode = None
            return
        now = time.monotonic()
        last_mode, last_time = getattr(self, "last_press", (None, 0.0))
        self.last_press = (mode, now)
        if last_mode == mode and now - last_time < 0.4:
            self.last_press = (None, 0.0)
            self.drag_mode = None
            if mode == "resize":
                self.resize_to(CARD_WIDTH, LIST_HEIGHT, save=True)
            else:
                self.on_header_double_click(Gdk.BUTTON_PRIMARY)
            self.resized_at = now
            return
        if mode == "resize" and self.position is None:
            # Centred: pin the top-left corner, so the card grows right and down.
            self.move_to(self.current_position())
        self.drag_mode = mode
        self.drag_start = (self.current_position(), (self.card_w, self.list_h))

    def on_button(self, widget) -> bool:
        while widget is not None and widget != self.header:
            if isinstance(widget, Gtk.Button):
                return True
            widget = widget.get_parent()
        return False

    def on_drag_update(self, _gesture, dx, dy):
        if self.drag_mode is None or self.drag_start is None:
            return
        (x, y), (width, height) = self.drag_start
        if self.drag_mode == "move":
            self.move_to((x + round(dx), y + round(dy)))
        else:
            self.resize_to(width + round(dx), height + round(dy))

    def on_drag_end(self, _gesture, dx, dy):
        mode, self.drag_mode = self.drag_mode, None
        if mode is None:
            return
        if abs(dx) + abs(dy) > 2:
            self.resized_at = time.monotonic()   # not a click beside the card
        if mode == "resize":
            self.resize_to(self.card_w, self.list_h, save=True)
        if self.position is not None and abs(dx) + abs(dy) > 2:
            self.move_to(self.position, save=True)

    def on_overlay_click(self, _gesture, _count, x, y):
        if self.drag_mode is not None or time.monotonic() < self.resized_at + 0.3:
            return   # the end of a move or resize, not a click beside the card
        picked = self.overlay.pick(x, y, Gtk.PickFlags.DEFAULT)
        if not self.inside(picked, self.card_frame):
            self.close()

    def on_active_changed(self, window, _property):
        if window.get_visible() and not window.is_active():
            GLib.timeout_add(100, self.close_if_inactive)

    def close_if_inactive(self):
        if self.window.get_visible() and not self.window.is_active() and self.drag_mode is None:
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
        window#aimanager-window {{ background: transparent; }}
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
        .am-grip {{ color: {muted_hex}; margin: 3px; opacity: 0.7; }}
        .am-grip:hover {{ color: {accent_hex}; opacity: 1; }}
        .am-empty-icon {{ color: {muted_hex}; opacity: 0.6; }}
        .am-empty-title {{ font-size: 14px; font-weight: 600; }}
        .am-group-heading {{ font-size: 10px; font-weight: 700; letter-spacing: 1px; color: {dim_hex};
            margin-top: 4px; }}
        .am-agent {{ background: alpha({fg_hex}, 0.05); border: 1px solid alpha({fg_hex}, 0.10);
            border-radius: {radius}px; padding: 10px 12px; }}
        .am-agent-missing {{ opacity: 0.5; }}
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
        .am-canvas {{ border: 1px dashed alpha({fg_hex}, 0.25); border-radius: {radius}px; padding: 12px; }}
        """
        provider = Gtk.CssProvider()
        provider.load_from_string(css)
        Gtk.StyleContext.add_provider_for_display(display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.style_provider = provider

    # --- keys ------------------------------------------------------------------------

    def on_key(self, _controller, keyval, _keycode, state):
        page = self.stack.get_visible_child_name()
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        if keyval == Gdk.KEY_Escape:
            if page == "settings":
                self.close_settings()
            else:
                self.close()
            return True
        if page == "settings":
            if keyval == Gdk.KEY_BackSpace:
                self.close_settings()
                return True
            return False          # Tab and arrows move between the controls
        if ctrl and keyval in (Gdk.KEY_1, Gdk.KEY_KP_1):
            self.show_view("main")
            return True
        if ctrl and keyval in (Gdk.KEY_2, Gdk.KEY_KP_2):
            self.show_view("builder")
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
        # Another monitor may allow less: clamp the saved size again.
        self.resize_to(self.card_w, self.list_h)
        self.refresh_agents()
        self.move_to(self.position)
        self.window.present()
        self.layer_watch.start()
        # Restarted after a language change: back on the settings page.
        view = os.environ.pop("AI_MANAGER_VIEW", "")
        if view in ai_core.VIEWS:
            self.view = view
            self.show_view(view, save=False)
        if os.environ.pop("AI_MANAGER_PAGE", "") == "settings":
            GLib.idle_add(self.open_settings)

    def on_menu_opened(self, _name):
        """Another menu wants the keyboard: get out of its way at once."""
        if self.window is not None and self.window.is_visible():
            self.close()

    def close(self):
        self.layer_watch.stop()
        self.drag_mode = None
        if self.stack.get_visible_child_name() == "settings":
            self.close_settings()   # never reopen onto the settings page
        self.window.hide()


def main():
    app = AiManager()
    atexit.register(cleanup_pid)
    return app.run()


if __name__ == "__main__":
    sys.exit(main())
