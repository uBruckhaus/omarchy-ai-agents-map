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
import signal
import sys
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

import agents
import ai_core
import i18n
import pidfile
from i18n import _
from layer_watch import LayerWatch
from palette import ThemePalette

# Default outer size as in the other popups: a 420 px card around a 590 px
# page, the same on every page so switching never resizes the popup.
CARD_WIDTH = 420
LIST_HEIGHT = 590
SCREEN_MARGIN = 120    # space kept free above and below the card
CURSOR_SLACK = 24      # px the pointer may move after opening before the keyboard is released
AGENT_TILE_WIDTH = 180   # minimum width of one agent card
AGENT_SPACING = 8
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
        self.cursor_watch = 0
        # Omarchy's menus (Super + Space, …) open underneath this popup: close for them.
        self.layer_watch = LayerWatch("ai-manager", self.on_menu_opened)
        self.position = ai_core.window_position()   # None: centred
        self.card_w, self.list_h = ai_core.window_size() or (CARD_WIDTH, LIST_HEIGHT)
        self.move_start = ((0, 0), None)
        self.moving = False
        self.resize_start = None
        self.scrollers = []        # every page's scroller follows the page height
        self.view = ai_core.last_view() if ai_core.start_view() == "last" else ai_core.start_view()
        self.settings_ready = False

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
        # No anchors: the surface is only the card, so the rest of the screen
        # stays usable. -1: margins are measured from the monitor's corner.
        Gtk4LayerShell.set_exclusive_zone(window, -1)
        self.window = window

        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        card.add_css_class("am-card")
        frame = Gtk.Overlay()
        frame.set_child(card)
        window.set_child(frame)
        self.card = card
        # Resize grip in the bottom-right corner (a layer surface has no frame).
        grip = Gtk.Image.new_from_icon_name("pan-down-symbolic")
        grip.add_css_class("am-grip")
        grip.set_pixel_size(14)
        grip.set_halign(Gtk.Align.END)
        grip.set_valign(Gtk.Align.END)
        grip.set_cursor_from_name("se-resize")
        grip.set_tooltip_text(_("Drag to resize · double-click for the default size"))
        sizer = Gtk.GestureDrag()
        sizer.connect("drag-begin", self.on_resize_begin)
        sizer.connect("drag-update", self.on_resize_update)
        sizer.connect("drag-end", self.on_resize_end)
        grip.add_controller(sizer)
        frame.add_overlay(grip)
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
        mover = Gtk.GestureDrag()
        mover.connect("drag-begin", self.on_move_begin)
        mover.connect("drag-update", self.on_move_update)
        mover.connect("drag-end", self.on_move_end)
        header.add_controller(mover)

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
        self.agents_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.agents_box.add_css_class("am-agents")
        self.agent_grids = []
        page.append(self.page_scroller(self.agents_box))
        return page

    def grid_columns(self) -> int:
        """Agent cards per row: as many as fit the card's inside (17 px padding each side)."""
        return max(1, (self.card_w - 34 + AGENT_SPACING) // (AGENT_TILE_WIDTH + AGENT_SPACING))

    def refresh_agents(self):
        """Rebuild the agent cards (file system only, nothing is started)."""
        box = self.agents_box
        while (child := box.get_first_child()) is not None:
            box.remove(child)
        self.agent_grids = []
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
            grid = Gtk.FlowBox()
            grid.set_selection_mode(Gtk.SelectionMode.NONE)
            grid.set_homogeneous(True)
            grid.set_row_spacing(AGENT_SPACING)
            grid.set_column_spacing(AGENT_SPACING)
            grid.set_valign(Gtk.Align.START)
            grid.set_min_children_per_line(self.grid_columns())
            grid.set_max_children_per_line(self.grid_columns())
            for agent in group:
                grid.append(self.build_agent_card(agent))
            box.append(grid)
            self.agent_grids.append(grid)

    def build_agent_card(self, agent) -> Gtk.Widget:
        card = Gtk.Box(spacing=10)
        card.add_css_class("am-agent")
        if not agent.installed:
            card.add_css_class("am-agent-missing")
        icon = Gtk.Label(label=agent.icon)
        icon.add_css_class("am-agent-icon")
        if agent.icon_font == "omarchy":
            icon.add_css_class("am-agent-icon-omarchy")
        icon.set_valign(Gtk.Align.CENTER)
        card.append(icon)
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
        card.append(text)
        source = {"mise": _("installed with mise"), "package": _("installed as a package"),
                  "own": _("your own install")}.get(agent.source, "")
        tip = _("Command: {0}").format(agent.command)
        if agent.version:
            tip += " · " + agent.version
        if source:
            tip += " · " + source
        card.set_tooltip_text(tip)
        return card

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
        for grid in getattr(self, "agent_grids", []):
            grid.set_min_children_per_line(self.grid_columns())
            grid.set_max_children_per_line(self.grid_columns())
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

    def update_mouse_hint(self):
        if getattr(self, "mouse_hint", None) is None:
            return
        size = self.size_state()
        self.mouse_hint.set_label(_("Double-click: maximize") if size == "default" else
                                  _("Double-click: default size") if size == "max" else
                                  _("Double-click: default size · right double-click: maximize"))
        self.mouse_hint.set_tooltip_text(self.mouse_hint.get_label())

    def on_resize_begin(self, _gesture, _x, _y):
        # A second press on the grip within the double-click time: default size.
        now = time.monotonic()
        if now - getattr(self, "last_grip_press", 0.0) < 0.4:
            self.last_grip_press = 0.0
            self.resize_start = None
            self.resize_to(CARD_WIDTH, LIST_HEIGHT, save=True)
            return
        self.last_grip_press = now
        if self.position is None:
            # Centred: pin the top-left corner, so the card grows right and down.
            self.move_to(self.current_position())
        self.resize_start = ((self.card_w, self.list_h), ai_core.cursor_position())

    def on_resize_update(self, _gesture, dx, dy):
        if self.resize_start is None:
            return
        (width, height), cursor_start = self.resize_start
        cursor = ai_core.cursor_position() if cursor_start is not None else None
        if cursor is not None:
            dx, dy = cursor[0] - cursor_start[0], cursor[1] - cursor_start[1]
        self.resize_to(width + round(dx), height + round(dy))

    def on_resize_end(self, _gesture, _dx, _dy):
        if self.resize_start is None:
            return
        self.resize_start = None
        self.resize_to(self.card_w, self.list_h, save=True)
        if self.position is not None:
            self.move_to(self.position, save=True)

    # --- moving ------------------------------------------------------------------

    def monitor_geometry(self):
        display = Gdk.Display.get_default()
        surface = self.window.get_surface()
        monitor = display.get_monitor_at_surface(surface) if display and surface else None
        if monitor is None and display is not None and display.get_monitors().get_n_items():
            monitor = display.get_monitors().get_item(0)
        return monitor.get_geometry() if monitor is not None else None

    def current_position(self) -> tuple[int, int]:
        if self.position is not None:
            return self.position
        geo = self.monitor_geometry()
        if geo is None:
            return 0, 0
        return (max(0, (geo.width - self.window.get_width()) // 2),
                max(0, (geo.height - self.window.get_height()) // 2))

    def move_to(self, position, save: bool = False):
        """Place the card's top-left at `position` (monitor-relative, kept on
        screen), or centre it for None."""
        if position is not None:
            geo = self.monitor_geometry()
            if geo is not None:
                position = (min(max(0, int(position[0])), max(0, geo.width - self.window.get_width())),
                            min(max(0, int(position[1])), max(0, geo.height - self.window.get_height())))
        self.position = position
        for edge in (Gtk4LayerShell.Edge.TOP, Gtk4LayerShell.Edge.LEFT):
            Gtk4LayerShell.set_anchor(self.window, edge, position is not None)
        if position is not None:
            Gtk4LayerShell.set_margin(self.window, Gtk4LayerShell.Edge.LEFT, position[0])
            Gtk4LayerShell.set_margin(self.window, Gtk4LayerShell.Edge.TOP, position[1])
        if save:
            try:
                ai_core.save_window_position(position)
            except OSError:
                pass

    def on_move_begin(self, _gesture, _x, _y):
        # Seen here rather than by a click gesture, which the drag can win:
        # a second press within the double-click time.
        now = time.monotonic()
        if now - getattr(self, "last_header_press", 0.0) < 0.4:
            self.last_header_press = 0.0
            self.moving = False
            self.on_header_double_click(Gdk.BUTTON_PRIMARY)
            return
        self.last_header_press = now
        self.moving = True
        self.move_start = (self.current_position(), ai_core.cursor_position())

    def on_move_update(self, _gesture, dx, dy):
        if not self.moving:
            return
        # The gesture's offsets are relative to the surface, which moves with
        # the pointer; Hyprland's global cursor position does not drift.
        (x, y), cursor_start = self.move_start
        cursor = ai_core.cursor_position() if cursor_start is not None else None
        if cursor is not None:
            dx, dy = cursor[0] - cursor_start[0], cursor[1] - cursor_start[1]
        else:
            x, y = self.current_position()
        self.move_to((x + round(dx), y + round(dy)))

    def on_move_end(self, _gesture, _dx, _dy):
        if not self.moving:
            return
        self.moving = False
        if self.position is not None:
            self.move_to(self.position, save=True)

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
        self.grab_keyboard()
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

    def grab_keyboard(self):
        """Take the keyboard on open, and give it back as soon as the pointer
        moves away (ON_DEMAND afterwards, so other windows still get keys)."""
        Gtk4LayerShell.set_keyboard_mode(self.window, Gtk4LayerShell.KeyboardMode.EXCLUSIVE)
        if self.cursor_watch:
            GLib.source_remove(self.cursor_watch)
        start = ai_core.cursor_position()

        def watch():
            if not self.window.is_visible():
                self.cursor_watch = 0
                return False
            now = ai_core.cursor_position()
            if start is None or now is None or \
                    abs(now[0] - start[0]) + abs(now[1] - start[1]) > CURSOR_SLACK:
                self.cursor_watch = 0
                self.release_keyboard()
                return False
            return True

        self.cursor_watch = GLib.timeout_add(200, watch)

    def release_keyboard(self):
        if self.cursor_watch:
            GLib.source_remove(self.cursor_watch)
            self.cursor_watch = 0
        Gtk4LayerShell.set_keyboard_mode(self.window, Gtk4LayerShell.KeyboardMode.ON_DEMAND)

    def on_menu_opened(self, _name):
        """Another menu wants the keyboard: get out of its way at once."""
        if self.window is not None and self.window.is_visible():
            self.close()

    def close(self):
        self.layer_watch.stop()
        self.release_keyboard()
        if self.stack.get_visible_child_name() == "settings":
            self.close_settings()   # never reopen onto the settings page
        self.window.hide()


def main():
    app = AiManager()
    atexit.register(cleanup_pid)
    return app.run()


if __name__ == "__main__":
    sys.exit(main())
