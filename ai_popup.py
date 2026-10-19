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
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango

import atexit

import agent_activity
import agent_details
import agent_security
import disk_usage
import claude_sessions
import edition
import harnesses
import herdr_state
try:
    import addon_api            # Asterism only: signed add-ons (not in the Omarchy plugin)
    import addons
except ImportError:
    addon_api = addons = None
import desktop
import agent_updates
import agent_profiles
import agents
import ai_core
import connect
import corners
import mcp_catalog
import mcp_ctl
import mcp_library
import mcp_registry
import mcp_scan
import mcp_tools
import link_check
import link_hints
import llm_clients
import jan_ctl
import kobold_ctl
import mcp_update
import skill_update
import skills_ctl
import skills_scan
import skills_web
import runtime_ctl
import install_ctl
import install_recipes
import model_move
import models_ctl
import model_names
import context_ctl
import time
import struct
import shutil
import subprocess
from pathlib import Path
import urllib.parse
import graph
import gpu_usage
import agent_status
import terminal_pane
import previews
import inventory
import provider_watch
import providers
import i18n
import pidfile
from i18n import _, N_, ngettext
import app_entry
import model_layout
import jobs
import health
import web_sources
from map_view import LegendSample, MapView, Meter
from gi.repository import PangoCairo  # noqa: E402  (map_view asked for its version)
from palette import ThemePalette, outside_color
from settings_pages import SettingsPages
from theme_css import ThemeCss
from install_dialog import InstallDialog
from setup_page import SetupPage
try:
    from addons_page import AddonsPage      # Asterism only: the Add-ons page (not in the Omarchy plugin)
except ImportError:
    class AddonsPage:                       # the plugin: no add-ons, no page
        pass
from ui_helpers import MCP_GLYPHS, MCP_GROUP_GLYPHS, RUN_GLYPHS, SpaceBar, glyph_label, pick_cards, size_text

# Smallest card (a small monitor); normally the card fills the screen.
# Default outer size as in the other popups: a 420 px card around a 590 px
# page, the same on every page so switching never resizes the popup.
CARD_WIDTH = 420
LIST_HEIGHT = 590
SCROLLER_MIN_HEIGHT = 120
SCREEN_MARGIN = 120    # space kept free above and below the card
WINDOW_SIZE = (1600, 960)   # first open; afterwards the size it was closed with
AGENT_SPACING = 8
SPLIT_WIDTH = 1300        # from this window width the whole map fits (narrower: the widen hint)
MODEL_LIST_WIDTH = 460       # the models page's list (search results, installed models)
RIGHT_PAGES = ("connect", "install", "models", "mcp", "addmcp", "addskills", "status", "health", "terminal")   # open in the map's place
DETAIL_LABEL_WIDTH = 90   # the dim labels of a compact detail grid


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


class AppWindow(Adw.ApplicationWindow):
    """Tells when the compositor gives it another width (Hyprland tiles the window
    without touching GTK's default size; its content may be wider than the window
    while the cards and the map still sit side by side)."""

    def __init__(self, on_width, **kwargs):
        super().__init__(**kwargs)
        self.on_width = on_width
        self.last_width = 0

    def do_size_allocate(self, width, height, baseline):
        Adw.ApplicationWindow.do_size_allocate(self, width, height, baseline)
        if width != self.last_width:
            self.last_width = width
            GLib.idle_add(self.on_width, width)


class AgentsMap(SettingsPages, ThemeCss, InstallDialog, SetupPage, AddonsPage, Adw.Application):
    def __init__(self):
        super().__init__(application_id=desktop.app_id())
        self.window = None
        self.theme = ThemePalette()
        self.style_provider = None
        self.card_w, self.list_h = CARD_WIDTH, LIST_HEIGHT   # fit() makes it fill the screen
        self.scrollers = []        # every page's scroller follows the page height
        self.view = ai_core.last_view() if ai_core.start_view() == "last" else ai_core.start_view()
        self.settings_ready = False
        self.refreshing = set()    # commands whose usage Omarchy is collecting right now
        self.researching = set()   # new agents being looked up on the web (agent_profiles)
        self.asking_tools = set()  # MCP servers being asked for their tools (mcp_tools)
        self.updating_skills = {}  # real SKILL.md path -> what its update is doing (skill_update)
        self.found_mcp = None      # mcp_scan results (None: not scanned in this session)
        self.configured_now = None
        self.scanning_mcp = False
        self.found_skills = None   # skills_scan results
        self.checking_new = False
        self.agent_list = []
        self.inventory = inventory.Inventory()
        self.selection = []        # node ids selected on the map
        self.split = False         # the window is wide enough for the whole map (else the widen hint shows)
        self.sessions = {}           # agent command -> terminal_pane.TerminalPane (an agent started here)
        # Downloads and installs run on as background jobs when their page is left (jobs.py).
        self.jobs = jobs.Jobs(lambda job: GLib.idle_add(self.job_changed, job))
        self.job_rows = {}           # job id -> (row, detail label, progress bar)
        self.job_layout = None       # (id, state) of the rows built last

    def do_shutdown(self):
        cleanup_pid()
        Adw.Application.do_shutdown(self)

    def do_activate(self):
        if self.window is None:
            try:
                ai_core.drop_retired_settings()
            except OSError:
                pass
            self.load_addons()
            self.build_window()
            self.hold()  # Keep the singleton alive while its popup is hidden.
            signal.signal(signal.SIGUSR1, lambda *_: GLib.idle_add(self.toggle))
            for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
                signal.signal(sig, lambda *_: sys.exit(0))
            GLib.timeout_add(100, lambda: True)  # Let Python dispatch SIGUSR1.
            self.watch_theme()
            write_pid_file()
            if hasattr(self, "setup_needed") and self.setup_needed():
                GLib.idle_add(self.open_setup)      # a fresh Asterism: pick what goes on the map
        self.open()

    # --- add-ons (Asterism only) ---------------------------------------------------------------

    def load_addons(self):
        """Verify every add-on found and load the ones switched on (Settings keeps the list)."""
        self.addons = []
        if addons is None:
            return
        enabled = ai_core.load_settings().get("addons_enabled", [])
        try:
            self.addons = addons.load_all(set(e for e in enabled if isinstance(e, str)) if isinstance(enabled, list)
                                          else set())
        except Exception:               # never keep the app from starting
            self.addons = []

    def reload_addons(self):
        """After an add-on was installed, switched on or off, updated or removed: load them again and
        show their agents, providers, sections and cards everywhere, without a restart."""
        self.load_addons()
        old = self.stack.get_child_by_name("help")
        if old is not None:
            self.stack.remove(old)
        self.stack.add_named(self.build_help_page(), "help")      # Help cards come from add-ons too
        self.collect()
        self.refresh_overview()
        self.refresh_map()
        self.look_for_addons()          # what is still missing (from the cached catalog)

    def look_for_addons(self):
        """In the background: the signed add-on catalog (cached, read again every few hours) and its
        detect hints (read-only) → add-ons whose agent or provider is on this PC but not shown yet:
        a hint line, a card on My setup, "Suggested for this PC" on the Add-ons page."""
        if addons is None or getattr(self, "addons_looking", False):
            return
        self.addons_looking = True
        import addon_store

        def work():
            try:
                entries = addon_store.catalog(addon_store.online())
                return entries, {e.id for e in entries if e.kind in ("agent", "provider") and addon_store.present(e)}
            except Exception:
                return [], set()

        def done(found):
            entries, present = found
            self.addons_looking = False
            installed = {a.id: a for a in self.addons}
            suggestions = []
            for entry in entries:
                addon = installed.get(entry.id)
                if entry.id in present and (addon is None or addon.state == "verified"):
                    suggestions.append((entry, "off" if addon is not None else "new"))
            changed = [(e.id, s) for e, s in suggestions] != [(e.id, s) for e, s in
                                                              getattr(self, "addon_suggestions", [])]
            self.addon_suggestions = suggestions
            self.addons_entries = entries
            if changed:
                self.refresh_overview()
                self.refresh_map()
            return False

        threading.Thread(target=lambda: GLib.idle_add(done, work()), daemon=True).start()

    def addon_hooks(self, kind: str) -> list:
        return list(getattr(addon_api.HOOKS, kind)) if addon_api is not None else []

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
        # The app's own icon from data/icons when no package put it into the icon theme
        # (the AppImage, a source checkout): window, welcome page and About find it by name.
        own_icons = Path(__file__).resolve().parent / "data/icons"
        if own_icons.is_dir():
            Gtk.IconTheme.get_for_display(Gdk.Display.get_default()).add_search_path(str(own_icons))
        window = AppWindow(self.width_changed, application=self)
        window.set_decorated(desktop.decorated())       # Omarchy: Hyprland draws the border, the header is our own
        desktop.attach_window(window)
        window.set_title(desktop.app_name())
        window.set_name("aiagentsmap-window")
        window.set_icon_name(desktop.app_id())
        size = ai_core.load_settings().get("window_size")
        if isinstance(size, list) and len(size) == 2 and all(isinstance(v, int) and 400 <= v <= 10000 for v in size):
            window.set_default_size(*size)
        else:
            window.set_default_size(*WINDOW_SIZE)
        window.set_size_request(ai_core.MIN_WINDOW_SIZE[0], ai_core.MIN_WINDOW_SIZE[1] + 200)
        # Closing hides it: the process stays, with any agent running in it.
        window.connect("close-request", lambda *_: (self.close(), True)[1])
        # A click on the bar icon can take the focus just before the toggle arrives: remember
        # when the window was last in front, so that click still hides it.
        self.left_front = 0.0
        window.connect("notify::is-active", lambda w, _p: not w.is_active()
                       and setattr(self, "left_front", time.monotonic()))
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
        # Notes, Undo and questions as libadwaita toasts, over the whole window.
        self.toasts = Adw.ToastOverlay(child=holder)
        self.current_toast = None
        self.card = card
        self.card_frame = frame

        header = self.build_header()
        if desktop.decorated():
            # Other desktops: the header row is the header bar's content, beside the window buttons.
            bar = Adw.HeaderBar()
            bar.add_css_class("am-headerbar")
            header.set_hexpand(True)
            bar.set_title_widget(header)
            toolbar = Adw.ToolbarView()
            toolbar.add_top_bar(bar)
            toolbar.set_content(self.toasts)
            window.set_content(toolbar)
        else:
            window.set_content(self.toasts)
            card.append(header)

        stack = Gtk.Stack()
        stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
        stack.set_transition_duration(140)
        stack.set_interpolate_size(False)
        stack.set_hhomogeneous(True)
        stack.set_vhomogeneous(True)
        stack.add_named(self.build_main_page(), "main")
        stack.add_named(self.build_settings_page(), "settings")
        stack.add_named(self.build_help_page(), "help")
        stack.add_named(self.build_reorganize_page(), "reorganize")
        stack.add_named(self.build_setup_page(), "setup")
        if hasattr(self, "build_addons_page"):
            stack.add_named(self.build_addons_page(), "addons")
            self.setup_addon_drop(self.window)
        # Details and the pages that change something open right of the cards, where the map is.
        right = self.right_stack
        right.add_named(self.build_connect_page(), "connect")
        right.add_named(self.build_install_page(), "install")
        right.add_named(self.build_models_page(), "models")
        right.add_named(self.build_mcp_page(), "mcp")
        right.add_named(self.build_add_mcp_page(), "addmcp")
        right.add_named(self.build_add_skills_page(), "addskills")
        right.add_named(self.build_status_page(), "status")
        right.add_named(self.build_health_page(), "health")
        right.connect("notify::visible-child-name", lambda *_: self.update_header())
        self.guard_text_fields_in(window)
        stack.connect("notify::visible-child-name", lambda *_: (self.update_header(), self.on_setup_shown()))
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
        self.render_jobs()            # downloads a previous run left unfinished: Continue / Discard

    def build_header(self) -> Gtk.Widget:
        header = Gtk.Box(spacing=8)
        self.header = header
        # Show or hide the cards beside the map (Overview, wide screens only).
        title = Gtk.Label(label=_("My AI Agents Map"), xalign=0)
        title.add_css_class("am-title")
        title.set_ellipsize(Pango.EllipsizeMode.END)
        title.set_max_width_chars(30)
        title.set_width_chars(12)          # in a header bar beside the window buttons: not just "…"
        title.set_hexpand(True)
        header.append(title)
        self.title_label = title
        # Too narrow for the map (a tiled window): a hint that also does it.
        self.widen_hint = Gtk.Button(valign=Gtk.Align.CENTER, visible=False)
        content = Gtk.Box(spacing=6)
        content.append(Gtk.Image.new_from_icon_name("view-fullscreen-symbolic"))
        content.append(Gtk.Label(label=_("Maximize for more room")))
        self.widen_hint.set_child(content)
        self.widen_hint.add_css_class("am-small-btn")
        self.widen_hint.add_css_class("am-widen-hint")
        self.widen_hint.set_tooltip_text(desktop.text("widen_tip"))
        self.widen_hint.connect("clicked", lambda *_: self.widen())
        header.append(self.widen_hint)
        # One view (the cards with the map beside them): no view switch any more.
        self.view_buttons = {}
        # My setup: what is on the map is set up, updated and removed there (setup_page.py).
        self.setup_button = Gtk.Button(valign=Gtk.Align.CENTER)
        content = Gtk.Box(spacing=6)
        content.append(Gtk.Image.new_from_icon_name("view-grid-symbolic"))
        content.append(Gtk.Label(label=_("My setup")))
        self.setup_button.set_child(content)
        self.setup_button.add_css_class("am-small-btn")
        self.setup_button.set_tooltip_text(_("Agents, LLMs, MCP servers, skills and models: add, update or remove "
                                             "them; the map shows what is set up"))
        self.setup_button.connect("clicked", lambda *_: self.open_my_setup())
        header.append(self.setup_button)
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

        # The legend (as in App Manager): what the map's dots, lines and marks mean.
        legend_button = Gtk.MenuButton()
        mark = Gtk.Label(label="ⓘ")
        mark.add_css_class("am-gear")
        legend_button.set_child(mark)
        legend_button.set_tooltip_text(_("What the marks mean"))
        legend_button.add_css_class("flat")
        legend_button.add_css_class("am-header-btn")
        legend_button.add_css_class("am-gear-btn")
        legend_button.set_create_popup_func(lambda button: button.set_popover(self.legend_popover()))
        header.append(legend_button)
        self.legend_button = legend_button

        # Add-ons (Asterism only): everything the app understands, and what can be added.
        self.addons_button = None
        if hasattr(self, "open_addons"):
            addons_button = Gtk.Button()
            addons_button.set_child(Gtk.Image.new_from_icon_name("application-x-addon-symbolic"))
            addons_button.set_tooltip_text(_("Add-ons"))
            addons_button.add_css_class("flat")
            addons_button.add_css_class("am-header-btn")
            addons_button.add_css_class("am-gear-btn")
            addons_button.connect("clicked", lambda *_: self.open_addons())
            header.append(addons_button)
            self.addons_button = addons_button
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

    def legend_popover(self) -> Gtk.Popover:
        """The legend: every dot, line and mark of the map, drawn as there, with what it means."""
        colors = lambda name, alpha=1.0: self.side_map.color(name, alpha)
        sections = [
            (_("Boxes"), [
                ("dot:green", _("Running")),
                ("dot:yellow", _("Installed, not running")),
                ("dot:muted", _("Not installed")),
                ("ring", _("A local LLM server generating right now")),
                ("model:loaded", _("Model loaded in GPU memory")),
                ("model:outside", _("Model in use by an app that is not on the map (its name in the box)")),
                ("model:loading", _("Model loading")),
                ("model:sleeping", _("Model asleep: listed, GPU memory free")),
                ("check", _("Selected (Shift + click selects more)")),
            ]),
            (_("Lines"), [
                ("line:active", _("In use now: light flows while the agent works")),
                ("line:outside", _("Used by an app that is not on the map: light flows from the local LLM to its model "
                                   "(the app is named in the local LLM's menu and details)")),
                ("line:uses", _("The agent uses this provider")),
                ("line:available", _("Set up, but not in use")),
                ("line:serves", _("A provider or server offers this model")),
                ("line:mcp", _("MCP server connected to the agent")),
                ("line:skills", _("The agent's skills")),
                ("line:error", _("Does not work: click the line for hints")),
                ("line:warning", _("Works with warnings: click the line for hints")),
            ]),
        ]
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_margin_top(8)
        box.set_margin_bottom(8)
        box.set_margin_start(12)
        box.set_margin_end(12)
        for heading, rows in sections:
            title = Gtk.Label(label=heading.upper(), xalign=0)
            title.add_css_class("am-section-title")
            title.set_margin_top(4)
            box.append(title)
            for sample, text in rows:
                line = Gtk.Box(spacing=12)
                line.append(LegendSample(colors, sample))
                label = Gtk.Label(label=text, xalign=0, wrap=True, max_width_chars=44)
                label.add_css_class("am-legend-text")
                line.append(label)
                box.append(line)
        more = Gtk.Label(label=_("A column heading such as “Models (3/5) ▾”: some boxes are hidden; click it to choose."),
                         xalign=0, wrap=True, max_width_chars=50)
        more.add_css_class("am-hint")
        more.set_margin_top(6)
        box.append(more)
        popover = Gtk.Popover()

        def to_help(*_):
            popover.popdown()
            self.open_help()
        help_link = Gtk.Button(label=_("More in the Help (F1)"))
        help_link.add_css_class("flat")
        help_link.add_css_class("am-legend-help")
        help_link.set_halign(Gtk.Align.START)
        help_link.connect("clicked", to_help)
        box.append(help_link)
        # The popover is its own surface: the window's keys (F1) don't reach it while it is open.
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", lambda _c, keyval, *_: keyval == Gdk.KEY_F1 and (to_help() or True))
        popover.add_controller(keys)
        popover.set_child(box)
        return popover

    def page_scroller(self, child: Gtk.Widget, bar_room: bool = True) -> Gtk.ScrolledWindow:
        """bar_room: keep the right edge free for the scroll bar, which overlays the content
        (it covered the cards' ▾ in the narrow window)."""
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_vexpand(True)
        if bar_room:
            child.set_margin_end(max(child.get_margin_end(), 14))
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
        # Downloads and installs running in the background (or interrupted): one line each.
        self.jobs_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.jobs_box.add_css_class("am-jobs")
        self.jobs_box.set_visible(False)
        page.append(self.jobs_box)
        # The health check in one line: how things stand, and the page with the details.
        self.health_line = Gtk.Button()
        self.health_line.add_css_class("flat")
        self.health_line.add_css_class("am-health-line")
        line = Gtk.Box(spacing=8)
        self.health_mark = Gtk.Label(label="♥")
        self.health_mark.add_css_class("am-health-mark")
        line.append(self.health_mark)
        heading = Gtk.Label(label=_("Health check"))
        heading.add_css_class("am-agent-name")
        line.append(heading)
        self.health_text = Gtk.Label(label=_("Looking…"), xalign=0, hexpand=True)
        self.health_text.add_css_class("am-agent-detail")
        self.health_text.set_ellipsize(Pango.EllipsizeMode.END)
        line.append(self.health_text)
        line.append(Gtk.Label(label=_("Open") + " ›"))
        self.health_line.set_child(line)
        self.health_line.set_tooltip_text(_("What works, what to fix, and what you can do with this setup"))
        self.health_line.connect("clicked", lambda *_: self.open_health())
        # Shown only when something needs fixing; × hides it until the next start (Settings › Health check).
        self.health_bar = Gtk.Box(spacing=4, visible=False)
        self.health_line.set_hexpand(True)
        self.health_bar.append(self.health_line)
        dismiss = Gtk.Button(icon_name="window-close-symbolic", valign=Gtk.Align.CENTER)
        dismiss.add_css_class("flat")
        dismiss.add_css_class("circular")
        dismiss.set_tooltip_text(_("Hide until the next start"))
        dismiss.connect("clicked", lambda *_: self.dismiss_health_line())
        self.health_bar.append(dismiss)
        self.health_dismissed = False
        self.map_empty = False
        page.append(self.health_bar)
        # Above the map: an empty Asterism's welcome, and the hints (new on this PC, new versions…).
        self.empty_area = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, visible=False)
        page.append(self.empty_area)
        self.hint_area = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, visible=False)
        self.hint_area.add_css_class("am-hint-area")
        page.append(self.hint_area)
        self.health_report = None
        self.health_running = False
        body = Gtk.Box(spacing=0)
        # The map, the whole width (My setup has a card for everything on it).
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
        self.side_map.set_animate(ai_core.load_settings().get("animate_lines", True) is not False)
        self.map_view = self.side_map      # the one map (code below still says map_view in places)
        self.side_map.on_zoom = lambda _z: self.zoom_changed()
        self.update_layer_toggles()
        self.side_map.on_selection = self.on_map_selection
        self.side_map.move_target_ok = self.move_target_ok
        self.side_map.on_move = lambda node_id, folder, rect: self.confirm_move(self.side_map, rect, node_id, folder)
        self.side_map.pin_tips = (_("Pin: keep its connections lit while the pointer moves over other boxes"),
                                  _("Unpin"))
        self.side_map.on_link = self.on_map_link
        self.side_map.on_relink = self.on_map_relink
        self.side_map.on_pick_model = self.on_map_pick_model
        self.side_map.on_failed_edge = self.show_link_hints
        self.side_map.on_context = lambda node_id, rect, v=self.side_map: self.show_context(v, node_id, rect)
        self.side_map.start_state = self.map_start_state
        self.side_map.on_start = self.map_start
        self.side_map.start_tips = (_("Start here"), _("Show terminal"))
        map_scroller = self.page_scroller(self.side_map.with_flow_layer(), bar_room=False)   # the lights on running agents' paths on top
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
        self.right_stack = right
        self.side_pane = right
        # The terminal tabs over the map and over the terminals (not over other pages).
        column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        column.append(self.build_terminal_tabs())
        column.append(right)
        column.set_visible(False)
        self.right_column = column
        right.connect("notify::visible-child-name", lambda *_: self.update_terminal_tabs())
        body.append(column)
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
        if getattr(self, "jobs_box", None) is not None:
            self.render_jobs()

    def place_panes(self):
        """The map, or the page opened in its place, takes the whole width."""
        if getattr(self, "right_stack", None) is None:
            return
        # No list beside the map any more: the map (or a page in its place) takes the whole width.
        self.right_column.set_visible(True)

    # --- the terminals started here, as tabs above the map ---------------------------------------

    def build_terminal_tabs(self) -> Gtk.Widget:
        """Above the map and the terminals, only while an agent has a terminal here: a legend
        (as in App Manager) over one tab per terminal with a dot: working, running or ended.
        A click shows it; the terminal in view has its tab marked."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_bottom(12)
        top = Gtk.Box(spacing=10)
        heading = Gtk.Label(label=_("Terminals").upper(), xalign=0, hexpand=True)
        heading.add_css_class("am-section-title")
        top.append(heading)
        for state, text in (("working", _("working")), ("running", _("running")), ("ended", _("ended"))):
            item = Gtk.Label(label="● " + text)
            item.add_css_class("am-tab-legend")
            item.add_css_class(f"am-tab-{state}")
            top.append(item)
        box.append(top)
        self.terminal_tab_row = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=False,
                                            column_spacing=6, row_spacing=6, max_children_per_line=50)
        box.append(self.terminal_tab_row)
        box.set_visible(False)
        self.terminal_tabs = box
        self.working_agents = set()
        return box

    def update_terminal_tabs(self):
        if getattr(self, "right_stack", None) is None:
            return
        row = self.terminal_tab_row
        shown = self.right_stack.get_visible_child_name() or ""
        while (child := row.get_first_child()) is not None:
            row.remove(child)
        for command, pane in self.sessions.items():
            state = ("working" if command in self.working_agents else "running") if pane.running else "ended"
            tab = Gtk.Box(spacing=0)
            tab.add_css_class("am-term-tab")
            if shown == f"term:{command}":
                tab.add_css_class("am-term-tab-current")
            button = Gtk.Button()
            button.add_css_class("flat")
            content = Gtk.Box(spacing=6)
            dot = Gtk.Label(label="●")
            dot.add_css_class("am-tab-dot")
            dot.add_css_class(f"am-tab-{state}")
            content.append(dot)
            content.append(Gtk.Label(label=pane.agent.name))
            button.set_child(content)
            button.set_tooltip_text(pane.state_label.get_label())
            button.connect("clicked", lambda _b, c=command: self.show_terminal(c))
            tab.append(button)
            if not pane.running:
                close = Gtk.Button(icon_name="window-close-symbolic")
                close.add_css_class("flat")
                close.set_tooltip_text(_("Close this terminal"))
                close.connect("clicked", lambda _b, c=command: self.close_terminal(c))
                tab.append(close)
            row.append(tab)
        self.terminal_tabs.set_visible(bool(self.sessions) and (shown == "map" or shown.startswith("term:"))
                                       and ai_core.load_settings().get("terminal_tabs", True) is not False)

    def close_terminal(self, command: str):
        """An ended agent's terminal: its tab and the terminal go away."""
        pane = self.sessions.get(command)
        if pane is None or pane.running:
            return
        if self.right_stack.get_visible_child() is pane:
            back = getattr(pane, "back", None)       # an install: where it was started from
            back() if back is not None else self.show_view("main", save=False)
        del self.sessions[command]
        self.right_stack.remove(pane)
        self.update_terminal_tabs()
        self.refresh_overview()

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
            pids = {}
            running = self.running_now(pids)
            try:
                working = self.working_now(pids, usage, runtimes)
            except Exception:
                working = set(running)
            try:
                outside = self.outside_now(usage, runtimes)
            except Exception:
                outside = {}
            GLib.idle_add(done, usage, running, working, outside)

        def done(usage, running, working, outside):
            self.gpu_reading = False
            self.side_map.set_working(working)
            self.side_map.set_outside({rid: {mid: _("in use · {0}").format(names) for mid, names in models.items()}
                                       for rid, models in outside.items()})
            if working != self.working_agents:
                self.working_agents = set(working)
                self.update_terminal_tabs()
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

    def working_now(self, pids: dict, usage, runtimes) -> set:
        """The running agents that work right now (agent_activity): the map's lines flow for them."""
        if not hasattr(self, "activity"):
            self.activity = agent_activity.Activity()
        states = {}
        if any(herdr_state.server_running(sock) for _name, sock in herdr_state.sessions()):
            for pane in herdr_state.snapshot() or []:
                states.setdefault(pane.agent, set()).add(pane.state)
        generating = set()
        if usage is not None and usage.generating:
            for rt in runtimes:
                use = usage.runtimes.get(rt.key)
                if use is not None and use.running:
                    generating.update(rt.active_agents)
        return self.activity.working(pids, states, generating)

    def outside_now(self, usage, runtimes) -> dict:
        """Who uses the running local LLMs (llm_clients, kept for the menu and the details page),
        and {runtime id: {model id: "Video Trans"}} where an app outside the map generates now: its light
        and its name on the map."""
        if not hasattr(self, "llm_clients"):
            self.llm_clients = llm_clients.Clients()
        if usage is None:
            return {}
        ports = {rt.key: gpu_usage.port_of(rt) for rt in runtimes
                 if (use := usage.runtimes.get(rt.key)) is not None and use.running}
        # ~1.5 s of this poll's 2 s: requests and short replies fall between single samples.
        peak = self.llm_clients.update(ports, self.agent_names(), samples=15, probe=gpu_usage.busy_now)
        # Generating counts for llm_clients.ACTIVE seconds after it was last seen, like the clients.
        now = time.monotonic()
        if usage.generating or (usage.loaded and (peak or 0) >= gpu_usage.BUSY_PERCENT):
            self.generated_at = now
        recently = now - getattr(self, "generated_at", -1e9) <= llm_clients.ACTIVE
        outside, agents = {}, set(self.agent_names().values())
        for key in ports:
            loaded = [m for m, state in usage.runtimes[key].models.items() if state == "loaded"]
            names = [name for name, age in self.llm_clients.recent(key) if age <= llm_clients.ACTIVE
                     and name not in agents]
            if loaded and names and recently:
                outside[f"runtime:{key}"] = {f"model:runtime:{key}:{m}": ", ".join(names[:3]) for m in loaded}
        return outside

    def runtime_users(self, key: str) -> str:
        """"Video Trans · now, Hermes · 12 s ago": who used this local LLM lately ("" nobody)."""
        clients = getattr(self, "llm_clients", None)
        rows = clients.recent(key) if clients is not None else []
        return ", ".join(f"{name} · " + (_("now") if age <= llm_clients.ACTIVE else
                                        _("{0} s ago").format(round(age))) for name, age in rows[:6])

    def running_now(self, pids: dict | None = None) -> dict:
        """Agent command -> the model each open instance runs with (/proc only); pids gets
        {agent: [process ids]} when given."""
        names = [a.command for a in getattr(self, "agent_list", []) if a.installed]
        try:
            found = provider_watch.agent_instances(names, pids=pids)
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
        pane = getattr(self, "sessions", {}).get(agent.command)
        here = pane is not None and pane.running and pane.pid
        if len(models) == 1:
            state = _("running here") if here else _("running")
        else:
            state = _("{0} running").format(len(models)) + (" · " + _("one here") if here else "")
        return " · ".join([state] + names)

    def sync_provider_watch(self):
        """The watcher that starts an agent's local LLMs with it (and stops them when it
        closes) runs while the setting is on and some agent is connected to a local LLM.
        It is a user service, so it works with this window closed and after a reboot."""
        wanted = bool(ai_core.load_settings().get("providers_with_agents", True)) and \
            any(rt.installed and rt.agents and (rt.service or rt.key == "lmstudio") for rt in self.inventory.runtimes)
        threading.Thread(target=provider_watch.ensure_service, args=(wanted,), daemon=True).start()

    def refresh_overview(self):
        """What the main view shows above the map: the health line, an empty Asterism's welcome,
        and the hints (new on this PC, new versions, updates)."""
        while (child := self.hint_area.get_first_child()) is not None:
            self.hint_area.remove(child)
        self.hint_area.set_visible(False)
        self.hint_buttons = Gtk.SizeGroup(mode=Gtk.SizeGroupMode.HORIZONTAL)   # one width for the hints' buttons
        self.map_empty = not harnesses.all_harnesses() and not providers.all_providers() \
            and hasattr(self, "adapter_addons") and bool(self.adapter_addons())
        self.update_health_line()
        while (child := self.empty_area.get_first_child()) is not None:
            self.empty_area.remove(child)
        self.empty_area.set_visible(self.map_empty)
        if self.map_empty:
            # An empty Asterism: nothing switched on yet (above the empty map).
            empty = self.empty_state("application-x-addon-symbolic", _("Nothing on the map yet"),
                                     _("Asterism shows the agents and local LLMs you switch on. Set up switches on "
                                       "the ones found on this PC with one click."))
            row = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER, margin_top=12)
            for label, run, main in ((_("Set up Asterism"), self.open_setup, True), (_("Add-ons"), self.open_addons, False)):
                button = Gtk.Button(label=label)
                if main:
                    button.add_css_class("suggested-action")
                button.connect("clicked", lambda *_a, r=run: r())
                row.append(button)
            empty.append(row)
            self.empty_area.append(empty)
        self.overview_hints(*self.new_found())
        if getattr(self, "stack", None) is not None and self.page_name() == "setup":
            GLib.idle_add(self.refresh_setup)       # My setup's cards follow too (a removal, an update)

    def overview_hints(self, new_mcp, new_skills):
        """The hint area above the map, one line each."""
        inv = self.inventory
        # The hint area: what was found on this PC, then what else could be set up.
        def listed(names, most):
            return ", ".join(names[:most]) + ("…" if len(names) > most else "")
        if new_mcp or new_skills:
            buttons = ([(_("Add MCP server…"), self.open_add_mcp)] if new_mcp else []) + \
                      ([(_("Add skills…"), self.open_add_skills)] if new_skills else [])
            self.hint_line(_("New on this PC: {0}").format(
                listed([c.name for c in new_mcp] + [s.origin for s in new_skills], 5)), buttons)
        # Agents and local LLMs on this PC that are not on the map: their add-on is off (My setup
        # switches it on) or not installed yet (the Add-ons page gets it). One line each.
        for state, opener, button in (("off", self.open_my_setup, _("My setup")),
                                      ("new", lambda: self.open_addons(), _("Get add-ons"))):
            waiting = [entry.name for entry, s in getattr(self, "addon_suggestions", []) if s == state]
            if waiting:
                self.hint_line(ngettext("{0} is on this PC but not on the map", "{1} are on this PC but not on the map",
                                        len(waiting)).format(listed(waiting, 1), listed(waiting, 3)),
                               [(button, opener)])
        versions = getattr(self, "new_versions", [])
        if versions:
            self.hint_line(_("New version found: {0} · + MCP server › Look inside… to use it").format(
                listed(versions, 3)), [(_("Add MCP server…"), self.open_add_mcp)])

        for update in agent_updates.still_open(getattr(self, "updates", []), self.agent_list, inv.runtimes):
            self.hint_line(_("{0} {1} is available (you have {2})").format(update.name, update.available, update.current),
                           [(_("Update"), lambda u=update: self.run_update(u))])


    def agent_names(self) -> dict:
        names = {a.command: a.name for a in self.agent_list}
        names["lmstudio"] = "LM Studio"
        return names

    def run_update(self, update):
        """An update from the hint area: ours on the Update page, the others in a terminal of their own
        (the card shows the new version on the next open)."""
        if update.page:
            import types
            target = next((rt for rt in self.inventory.runtimes if rt.key == update.key), None) \
                or types.SimpleNamespace(key=update.key, name=update.name, installed=True)
            self.open_install(target, "update")
            return
        if not agent_updates.run_in_terminal(update.command):
            self.show_notice(_("No terminal found (xdg-terminal-exec)"))
            return
        self.updates = [u for u in getattr(self, "updates", []) if u is not update]
        agent_updates.forget()
        self.refresh_overview()
        self.show_notice(_("Updating {0} in a terminal: {1}").format(update.name, " ".join(update.command)))

    def hint_line(self, text: str, buttons=(), tip: str = ""):
        """A hint in the area below the cards, with buttons that act on it: every line's
        buttons in one column at the right edge, all of one width (hint_buttons)."""
        line = Gtk.Box(spacing=8)
        if tip:
            line.set_tooltip_text(tip)
        label = Gtk.Label(label=text, xalign=0, wrap=True, max_width_chars=1, hexpand=True)
        label.add_css_class("am-agent-detail")
        line.append(label)
        for title, action in buttons:
            button = Gtk.Button(label=title, valign=Gtk.Align.CENTER)
            button.add_css_class("am-small-btn")
            button.add_css_class("am-quiet-btn")
            button.connect("clicked", lambda *_a, act=action: act())
            self.hint_buttons.add_widget(button)
            line.append(button)
        self.hint_area.append(line)
        self.hint_area.set_visible(True)

    def set_selection(self, ids):
        """The map's selection; one agent at most (the last one picked)."""
        ids = list(dict.fromkeys(ids))
        agents = [i for i in ids if i.startswith("agent:")]
        self.selection = [i for i in ids if not i.startswith("agent:") or i == agents[-1]]
        for view in (self.side_map,):
            view.set_selected(self.selection)
        self.update_selection_labels()

    @staticmethod
    def boxed_list() -> Gtk.ListBox:
        listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        listbox.add_css_class("boxed-list")
        return listbox

    @staticmethod
    def plain_row(title: str, subtitle: str = "") -> Adw.ActionRow:
        """A row whose texts are never markup (they carry paths, names, settings values, errors)."""
        row = Adw.ActionRow(title=title, subtitle=subtitle, use_markup=False)
        row.set_title_lines(0)
        row.set_subtitle_lines(0)
        return row

    def detail_grid(self, box, rows):
        """rows: (label, value, tooltip); empty values are left out. In a box marked am-boxed
        (the details pages) as libadwaita property rows, else as a compact grid."""
        if box.has_css_class("am-boxed"):
            listbox = self.boxed_list()
            for label, value, tip in rows:
                if not value:
                    continue
                row = self.plain_row(label, value)
                row.add_css_class("property")
                row.set_subtitle_selectable(True)
                if tip:
                    row.set_tooltip_text(tip)
                listbox.append(row)
            if listbox.get_first_child() is not None:
                box.append(listbox)
            return
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
                         desktop.text("install_agent")))
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
        if rt.models and box.has_css_class("am-boxed"):
            # The details page: a heading and one row per model.
            self.settings_heading(box, ngettext("1 model", "{0} models", len(rt.models)).format(len(rt.models)))
            listbox = self.boxed_list()
            for model in rt.models:
                row = self.plain_row(model)
                row.set_title_selectable(True)
                listbox.append(row)
            box.append(listbox)
        elif rt.models:
            heading = Gtk.Label(label=ngettext("1 model", "{0} models", len(rt.models)).format(len(rt.models)).upper(),
                                xalign=0)
            heading.add_css_class("am-group-heading")
            box.append(heading)
            self.note(box, "\n".join(rt.models))
        if not summary:
            buttons = Gtk.Box(spacing=8)
            buttons.append(self.details_button(lambda *_: self.open_runtime_status(rt.key)))
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

    def start_outside(self, agent, path: str, folder: str):
        """The agent in a terminal window of its own (the desktop's terminal)."""
        ok = terminal_pane.open_outside(path, folder)
        if not ok:
            try:
                agent_status.remember_issue(agent.command, "terminal")
            except OSError:
                pass
        self.show_notice(_("{0} opened in your terminal").format(agent.name) if ok
                         else _("No terminal found (xdg-terminal-exec)"))

    def stop_agent(self, command: str):
        pane = self.sessions.get(command)
        if pane is not None:
            pane.stop()

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

        def start_outside(*_args):
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

    def start_agent(self, agent, anchor=None, where: str = "here"):
        """Start an agent: here (its terminal right of the cards; a running one is shown again)
        or in a terminal window of its own ("terminal")."""
        pane = self.sessions.get(agent.command)
        if pane is not None and pane.running:
            self.show_terminal(agent.command)
            return
        path = terminal_pane.program(agent.command)
        if path is None:
            self.show_notice(_("{0} cannot be started from here").format(agent.name))
            return
        folder = self.agent_folder()
        if where == "terminal":
            self.start_outside(agent, path, folder)
            return
        if pane is not None:
            pane.start()
            self.show_terminal(agent.command)
            return
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
        self.update_terminal_tabs()
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
        """Start (or Show terminal and Stop while it runs here)."""
        pane = self.sessions.get(agent.command)
        shown = pane is not None and pane.running
        if shown:
            both = Gtk.Box(spacing=6)
            both.append(self.start_button_only(agent, shown))
            stop = Gtk.Button(halign=Gtk.Align.START)
            content = Gtk.Box(spacing=6)
            content.append(Gtk.Image.new_from_icon_name("process-stop-symbolic"))
            content.append(Gtk.Label(label=_("Stop")))
            stop.set_child(content)
            stop.add_css_class("flat")
            stop.add_css_class("am-small-btn")
            stop.add_css_class("am-danger")
            stop.set_tooltip_text(_("End {0} and what it started (its MCP servers)").format(agent.name))
            stop.connect("clicked", lambda *_: self.stop_agent(agent.command))
            both.append(stop)
            return both
        return self.start_button_only(agent, shown)

    def start_button_only(self, agent, shown: bool) -> Gtk.Widget:
        """Not running here: Start here and Start in terminal. Running here: Show terminal."""
        def button(icon, label, tip, where):
            b = Gtk.Button(halign=Gtk.Align.START)
            content = Gtk.Box(spacing=6)
            content.append(Gtk.Image.new_from_icon_name(icon))
            content.append(Gtk.Label(label=label))
            b.set_child(content)
            b.add_css_class("flat")
            b.add_css_class("am-small-btn")
            b.set_tooltip_text(tip)
            b.connect("clicked", lambda w: self.start_agent(agent, w, where))
            return b

        if shown:
            return button("utilities-terminal-symbolic", _("Show terminal"),
                          _("Show the terminal {0} runs in here").format(agent.name), "here")
        both = Gtk.Box(spacing=6)
        both.append(button("utilities-terminal-symbolic", _("Start here"),
                           _("Start {0} in {1}, in a terminal in this window; Stop ends it").format(
                               agent.name, self.agent_folder()), "here"))
        both.append(button("window-new-symbolic", _("Start in terminal"),
                           _("Start {0} in {1}, in a terminal window of its own").format(
                               agent.name, self.agent_folder()), "terminal"))
        return both

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
        self.show_view(self.view, save=False)

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
            sessions = None
            harness = harnesses.get(agent.command)
            if harness is not None and harness.sessions is not None and agent.installed:
                try:
                    sessions = harness.sessions.collect()           # numbers only
                except Exception:
                    sessions = None
            try:
                panes = herdr_state.snapshot()
            except Exception:
                panes = None
            try:
                servers = sum(1 for m in inventory.mcp if agent.command in m.agents)
                security = agent_security.report_for(agent.command, servers) if agent.installed else None
            except Exception:
                security = None
            GLib.idle_add(done, info, usage, checks, sessions, panes, security)

        def done(info, usage, checks, sessions, panes, security):
            if self.status_showing == ("agent", agent.command):
                self.fill_agent_status(agent, info, usage, checks, sessions, panes, security)
            return False

        threading.Thread(target=work, daemon=True).start()

    def fill_agent_status(self, agent, info, usage, checks, sessions=None, panes=None, security=None):
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
        if panes is not None:
            # herdr's live state, read again every few seconds while this page shows.
            self.herdr_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            body.append(self.herdr_box)
            self.fill_herdr_panes(agent, panes)
            self.watch_herdr(agent)

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

        harness = harnesses.get(agent.command)
        if sessions and harness is not None and harness.sessions is not None:
            self.fill_session_projects(body, sessions, harness)
        if harness is None and hasattr(self, "open_addons"):
            # No adapter, built in or from an add-on: say so, and where to ask for one.
            self.note(body, _("No add-on for {0} yet: Asterism only shows that it is installed (no sessions, "
                              "security settings, MCP servers or skills).").format(agent.name))
            import urllib.parse
            title = urllib.parse.quote(f"Add-on for {agent.name} ({agent.command})")
            body.append(Gtk.LinkButton(uri=f"https://github.com/uBruckhaus/asterism-addons/issues/new?title={title}",
                                       label=_("Ask for an add-on"), halign=Gtk.Align.START))

        body.append(self.health_security_card(agent, checks, security))
        self.settings_heading(body, _("Configuration"))
        config = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        config.add_css_class("am-boxed")
        self.fill_agent(config, agent, summary=True)
        body.append(config)

    HERDR_STATES = {"blocked": (N_("Waiting for you"), "dialog-question-symbolic"),
                    "working": (N_("Working"), "content-loading-symbolic"),
                    "idle": (N_("Idle"), "media-playback-pause-symbolic"),
                    "done": (N_("Done"), "object-select-symbolic"),
                    "unknown": (N_("Running"), "media-playback-start-symbolic")}
    HERDR_EVERY = 4                # seconds between reads while an agent's page shows

    def fill_herdr_panes(self, agent, panes):
        """The agent's panes in herdr now: state, folder and workspace (nothing of the screen)."""
        box = self.herdr_box
        while (child := box.get_first_child()) is not None:
            box.remove(child)
        mine = herdr_state.for_agent(panes, agent.command)
        if not mine:
            return
        self.settings_heading(box, _("In herdr now"))
        listbox = self.boxed_list()
        for pane in mine[:12]:
            label, icon = self.HERDR_STATES[pane.state]
            where = inventory.tilde(pane.cwd) if pane.cwd else _("Unknown folder")
            space = " › ".join(x for x in (pane.session, pane.workspace) if x)
            row = self.plain_row(_(label), _("{0} · workspace {1}").format(where, space) if space else where)
            row.add_prefix(Gtk.Image.new_from_icon_name(icon))
            if pane.state == "blocked":
                row.add_css_class("warning")
            if pane.pane_id:
                row.set_activatable(True)
                row.set_tooltip_text(_("Show this pane in herdr"))
                row.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
                row.connect("activated", lambda _r, p=pane: self.focus_herdr_pane(p))
            listbox.append(row)
        box.append(listbox)
        if len(mine) > 12:
            self.note(box, ngettext("1 more pane", "{0} more panes", len(mine) - 12).format(len(mine) - 12))

    def focus_herdr_pane(self, pane):
        """herdr switches to the pane; its terminal window comes to the front (where the desktop can)."""
        def work():
            try:
                switched = herdr_state.focus(pane)
                shown = switched and desktop.focus_process(
                    herdr_state.client_pids(pane.session, inventory.own_processes()))
            except Exception:
                switched = shown = False
            GLib.idle_add(done, switched, shown)

        def done(switched, shown):
            if not switched:
                self.toast(_("herdr could not switch to this pane"), 4)
            elif not shown:
                self.toast(_("Switched in herdr; open herdr in a terminal to see it"), 5)
            return False

        threading.Thread(target=work, daemon=True).start()

    def watch_herdr(self, agent):
        """While this agent's page shows: herdr's state again every HERDR_EVERY seconds (on a thread)."""
        showing, box, busy = ("agent", agent.command), self.herdr_box, {"on": False}

        def current() -> bool:
            return self.status_showing == showing and getattr(self, "herdr_box", None) is box

        def tick():
            if not current():
                return False
            if not busy["on"]:
                busy["on"] = True
                threading.Thread(target=work, daemon=True).start()
            return True

        def work():
            try:
                panes = herdr_state.snapshot()
            except Exception:
                panes = None
            GLib.idle_add(done, panes)

        def done(panes):
            busy["on"] = False
            if current():
                self.fill_herdr_panes(agent, panes or [])
            return False

        GLib.timeout_add_seconds(self.HERDR_EVERY, tick)

    WEEKDAYS = (N_("Monday"), N_("Tuesday"), N_("Wednesday"), N_("Thursday"), N_("Friday"), N_("Saturday"),
                N_("Sunday"))

    @staticmethod
    def duration_text(seconds: float) -> str:
        minutes = int(seconds // 60)
        return _("{0} h {1} min").format(minutes // 60, minutes % 60) if minutes >= 60 else \
            _("{0} min").format(minutes)

    def fill_session_projects(self, body, sessions, harness):
        """A harness's last 30 days per project folder: working time, sessions, tokens, lines
        changed, API value (numbers from its own session store; no text)."""
        since = time.time() - 30 * 86400
        rows = claude_sessions.projects(list(sessions.values()), since)
        if not rows:
            return
        self.settings_heading(body, _("Projects, last 30 days"))
        listbox = self.boxed_list()
        for project in rows[:10]:
            parts = [_("{0} working").format(self.duration_text(project.active)),
                     ngettext("1 session", "{0} sessions", project.sessions).format(project.sessions),
                     _("{0} tokens").format(short_number(project.tokens))]
            if project.subagent_runs:
                parts.append(ngettext("1 subagent run", "{0} subagent runs", project.subagent_runs)
                             .format(project.subagent_runs))
            if project.lines_added or project.lines_removed:
                parts.append(_("+{0} −{1} lines").format(project.lines_added, project.lines_removed))
            if project.cost:
                parts.append(_("API value {0}").format(f"$ {project.cost:.2f}"))
            row = self.plain_row(inventory.tilde(project.cwd) if project.cwd != "?" else _("Unknown folder"),
                                 " · ".join(parts))
            row.set_title_selectable(True)
            row.add_prefix(Gtk.Image.new_from_icon_name("folder-symbolic"))
            listbox.append(row)
        body.append(listbox)
        runs = claude_sessions.subagents(list(sessions.values()), since)
        if runs:
            self.settings_heading(body, _("Subagents, last 30 days"))
            listbox = self.boxed_list()
            for kind, count, tokens, active in runs[:10]:
                row = self.plain_row(kind if kind != "?" else _("Unknown type"), " · ".join((
                    ngettext("1 run", "{0} runs", count).format(count),
                    _("{0} tokens").format(short_number(tokens)),
                    _("{0} working").format(self.duration_text(active)))))
                row.add_prefix(Gtk.Image.new_from_icon_name("system-run-symbolic"))
                listbox.append(row)
            body.append(listbox)
        peak = claude_sessions.busiest(list(sessions.values()), since)
        if peak is not None:
            self.note(body, _("Most active: {0}, around {1}:00").format(_(self.WEEKDAYS[peak[0]]), peak[1]))
        self.note(body, harness.sessions.note())
        for addon_id, builder, privacy in self.addon_hooks("details"):
            if edition.allows(privacy):
                addon_api.call(addon_id, builder, addon_api.UI(self, addon_id), body, harness, sessions, since)

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
            if self.runtime_users(key):
                now += "\n" + _("In use by {0}").format(self.runtime_users(key))
        else:
            now = _("Not running") if rt.installed else _("Not installed")
        body.append(self.status_hero(state, sentence, now))
        tiles = [(_("Models"), str(len(rt.models)), rt.models_dir),
                 (_("Agents"), str(len(rt.agents)), ", ".join(names.get(a, a) for a in rt.agents))]
        if rt.address:
            tiles.append((_("Address"), rt.address, rt.service))
        body.append(self.stat_tiles(tiles))
        body.append(self.health_security_card(None, checks, agent_security.runtime_report(rt)))
        self.settings_heading(body, _("Configuration"))
        config = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        config.add_css_class("am-boxed")
        self.fill_runtime(config, rt, names, summary=True)
        body.append(config)
        self.show_page("status")

    def status_hero(self, state: str, sentence: str, now: str) -> Gtk.Widget:
        box = Gtk.Box(spacing=12)
        box.add_css_class("card")
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

    def stat_tiles(self, tiles) -> Gtk.Widget:
        """(heading, big value, small line) side by side."""
        row = Gtk.Box(spacing=10, homogeneous=True)
        for heading, value, small in tiles:
            tile = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            tile.add_css_class("card")
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
        """One row per check: a mark, what it is, and under it what to do when it is not done;
        a command to run gets a row of its own with a copy button."""
        listbox = self.boxed_list()
        for check in checks:
            lines = [check.detail] if check.detail else []
            if check.step and check.state != "ok":
                lines.append("→ " + check.step)
            row = self.plain_row(check.title, "\n".join(lines))
            row.set_subtitle_selectable(True)
            mark = Gtk.Label(label={"ok": "✓", "warn": "!", "missing": "✕", "info": "·"}[check.state])
            mark.add_css_class(f"am-state-{check.state}")
            mark.add_css_class("am-check-mark")
            row.add_prefix(mark)
            listbox.append(row)
            if check.command and check.state != "ok":
                listbox.append(self.command_row(check.command))
        return listbox

    def command_row(self, command: str) -> Adw.ActionRow:
        """A command to run, in monospace and selectable, with a button that copies it."""
        row = self.plain_row(command)
        row.add_css_class("monospace")
        row.set_title_selectable(True)
        row.add_prefix(Gtk.Image.new_from_icon_name("utilities-terminal-symbolic"))
        copy = Gtk.Button(icon_name="edit-copy-symbolic", valign=Gtk.Align.CENTER)
        copy.add_css_class("flat")
        copy.set_tooltip_text(_("Copy"))

        def copied(button):
            Gdk.Display.get_default().get_clipboard().set(command)
            button.set_icon_name("object-select-symbolic")
            GLib.timeout_add(1200, lambda: button.set_icon_name("edit-copy-symbolic") or False)
        copy.connect("clicked", copied)
        row.add_suffix(copy)
        return row

    # --- Healthy & Security check (agent and local LLM pages) ------------------------------------

    RISK_MARKS = {"high": ("!", "am-state-missing"), "medium": ("!", "am-state-warn"), "low": ("·", "am-state-info"),
                  "ok": ("✓", "am-state-ok")}
    RISK_NAMES = {"high": N_("High risk"), "medium": N_("Medium risk"), "low": N_("Low risk")}
    RISK_SENTENCES = {
        "high": N_("It can do serious things on this PC without asking you. What lowers the risk is listed below."),
        "medium": N_("Some things happen without asking you. What lowers the risk is listed below."),
        "low": N_("It asks before it does anything that matters, or cannot do it at all."),
    }
    HEALTH_NAMES = {"ok": N_("Healthy"), "warn": N_("Needs a look"), "missing": N_("Not set up")}

    def risk_mark(self, level: str) -> Gtk.Label:
        text, css = self.RISK_MARKS.get(level, ("·", "am-state-info"))
        mark = Gtk.Label(label=text)
        mark.add_css_class(css)
        mark.add_css_class("am-check-mark")
        return mark

    @staticmethod
    def risk_badge(text: str, level: str) -> Gtk.Label:
        badge = Gtk.Label(label=text, valign=Gtk.Align.CENTER)
        badge.add_css_class("am-risk-badge")
        badge.add_css_class(f"am-risk-{level}")
        return badge

    def health_security_card(self, agent, checks, security) -> Gtk.Widget:
        """Health (is it set up and working), security settings (what it may do without asking,
        each choice explained, changeable with a preview), risk management (what the settings mean
        together and what lowers each risk) and the words explained."""
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        card.add_css_class("am-help-card")
        top = Gtk.Box(spacing=8)
        top.append(Gtk.Image.new_from_icon_name("security-high-symbolic"))
        heading = Gtk.Label(label=_("Healthy & Security check").upper(), xalign=0, hexpand=True)
        heading.add_css_class("am-setting-heading")
        top.append(heading)
        state, _sentence = agent_status.summary_line(checks)
        top.append(self.risk_badge(_(self.HEALTH_NAMES.get(state, "Needs a look")),
                                   {"ok": "ok", "warn": "medium", "missing": "high"}.get(state, "medium")))
        level = security.level() if security is not None and (security.settings or security.risks) else ""
        if level:
            top.append(self.risk_badge(_(self.RISK_NAMES[level]), level))
        card.append(top)
        self.note(card, _("Whether it is set up and works, what it may do on this PC without asking you, and what "
                          "lowers the risks."))

        self.settings_heading(card, _("Health"))
        card.append(self.check_list(checks))
        if security is None:
            return card
        if security.settings:
            self.settings_heading(card, _("Security settings"))
            listbox = self.boxed_list()
            for setting in security.settings:
                listbox.append(self.security_setting_row(agent, setting))
            card.append(listbox)
            if agent is not None:
                self.note(card, _("Open a setting to see every choice. A change is shown first, backed up and can "
                                  "be undone; it applies the next time {0} starts.").format(agent.name))
        for title, _key, entries in security.rules:
            listbox = self.boxed_list()
            rules = Adw.ExpanderRow(title=title, subtitle=ngettext("1 rule", "{0} rules", len(entries))
                                    .format(len(entries)), use_markup=False)
            rules.add_prefix(Gtk.Image.new_from_icon_name("view-list-symbolic"))
            for entry in entries[:60]:
                row = self.plain_row(entry)
                row.add_css_class("monospace")
                row.set_title_selectable(True)
                rules.add_row(row)
            listbox.append(rules)
            card.append(listbox)
        if security.note:
            self.note(card, security.note)
        if security.files:
            self.note(card, ngettext("Settings file: {0}", "Settings files: {0}", len(security.files)).format(
                ", ".join(inventory.tilde(f) for f in security.files)))

        if security.risks:
            self.settings_heading(card, _("Risk management"))
            if level:
                self.note(card, _(self.RISK_SENTENCES[level]))
            listbox = self.boxed_list()
            for risk in security.risks:
                lines = [risk.why] + (["→ " + risk.lower] if risk.lower and risk.level != "ok" else [])
                row = self.plain_row(risk.title, "\n".join(lines))
                row.add_prefix(self.risk_mark(risk.level))
                setting = next((s for s in security.settings if s.key == risk.fix_key), None)
                fix = next((c for c in setting.choices if agent_security._same(c.value, risk.fix_value)), None) \
                    if setting is not None else None
                if agent is not None and fix is not None and setting.editable \
                        and not agent_security._same(setting.value(), fix.value):
                    button = Gtk.Button(label=_("Lower it…"), valign=Gtk.Align.CENTER)
                    button.add_css_class("am-small-btn")
                    button.set_tooltip_text(_("{0}: {1}").format(setting.title, fix.label))
                    button.connect("clicked", lambda _b, s=setting, c=fix: self.open_security_change(agent, s, c))
                    row.add_suffix(button)
                listbox.append(row)
            card.append(listbox)

        self.settings_heading(card, _("What the words mean"))
        listbox = self.boxed_list()
        words = agent_security.terms()
        for term in security.terms():
            if term not in words:
                continue
            title, text = words[term]
            row = Adw.ExpanderRow(title=title, use_markup=False)
            row.add_prefix(Gtk.Image.new_from_icon_name("help-about-symbolic"))
            explain = self.plain_row("", text)
            row.add_row(explain)
            listbox.append(row)
        card.append(listbox)
        return card

    def security_setting_row(self, agent, setting) -> Gtk.Widget:
        """One setting: its value now and what it means; opened, every choice with its risk and Use."""
        chosen = setting.choice()
        row = Adw.ExpanderRow(title=setting.title, subtitle=f"{chosen.label} — {chosen.explain}", use_markup=False)
        row.set_subtitle_lines(0)
        row.add_prefix(self.risk_mark(agent_security.LEVELS[chosen.risk] if chosen.risk else "ok"))
        about = setting.explain
        if setting.current is None:
            about += " " + _("Not set: the agent's own default applies.")
        explain = self.plain_row("", about)
        row.add_row(explain)
        for choice in setting.choices:
            if choice.value is None and setting.current is None and choice is not chosen:
                continue
            title = choice.label
            if agent_security._same(choice.value, setting.recommended):
                title += " · " + _("recommended")
            line = self.plain_row(title, choice.explain)
            line.add_prefix(self.risk_mark(agent_security.LEVELS[choice.risk] if choice.risk else "ok"))
            if choice.risk:
                line.add_suffix(self.risk_badge(_(self.RISK_NAMES[agent_security.LEVELS[choice.risk]]),
                                                agent_security.LEVELS[choice.risk]))
            if choice is chosen:
                mark = Gtk.Label(label=_("In use"), valign=Gtk.Align.CENTER)
                mark.add_css_class("am-agent-badge")
                line.add_suffix(mark)
            elif setting.editable and agent is not None:
                use = Gtk.Button(label=_("Use…"), valign=Gtk.Align.CENTER)
                use.add_css_class("am-small-btn")
                use.connect("clicked", lambda _b, c=choice: self.open_security_change(agent, setting, c))
                line.add_suffix(use)
            row.add_row(line)
        return row

    def open_security_change(self, agent, setting, choice):
        """The change of one security setting on the connect page: what it means, the diff, Change."""
        self.connecting = {"agent": agent, "security": setting.key, "value": choice.value, "title": setting.title,
                           "label": choice.label, "from": self.view}
        self.connect_title.set_label(_("{0}: {1}").format(agent.name, setting.title).upper())
        form = self.connect_form
        while (child := form.get_first_child()) is not None:
            form.remove(child)
        old = setting.choice()
        levels = agent_security.LEVELS
        self.connect_intro.set_label(_("{0} → {1}: {2}").format(old.label, choice.label, choice.explain) + " "
                                     + _("It applies the next time {0} starts; Undo puts it back.").format(agent.name))
        if old.risk != choice.risk:
            line = Gtk.Box(spacing=8)
            line.append(Gtk.Label(label=_("Risk"), xalign=0))
            line.append(self.risk_badge(_(self.RISK_NAMES[levels[old.risk]]), levels[old.risk]))
            line.append(Gtk.Label(label="→"))
            line.append(self.risk_badge(_(self.RISK_NAMES[levels[choice.risk]]), levels[choice.risk]))
            form.append(line)
        self.connect_preview_heading.set_visible(True)
        self.connect_preview_scroller.set_visible(True)
        self.connect_status.set_label("")
        self.connect_remove.set_visible(False)
        self.connect_ok.set_visible(True)
        self.connect_ok.set_label(_("Change"))
        self.update_connect_preview()
        self.show_page("connect")

    # --- MCP server details (the (i) on its card) ---------------------------------------------------

    def build_mcp_page(self) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        page.add_css_class("am-add-page")       # one text scale (see the CSS)
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
        self.intro_card(body, _("An MCP server gives agents tools."), [
            _("The agents below have this server: each starts or calls it when a task needs its tools (listed at "
              "the end). You do not call it yourself; ask the agent for the task. To give it to another agent, "
              "draw a line on the map from that agent to the server."),
        ])
        names = {a.command: a.name for a in self.agent_list}
        names["lmstudio"] = "LM Studio"
        for command in server.agents:
            self.section_heading(body, names.get(command, command))
            entry = mcp_ctl.normal(mcp_ctl.raw_entries(command).get(name))
            self.detail_grid(body, self.mcp_rows(command, entry))
        missing = [a.name for a in self.agent_list
                   if a.installed and a.command not in server.agents and mcp_ctl.supported(a.command)]
        if missing:
            self.section_heading(body, _("Not set up"))
            self.note(body, ", ".join(missing) + " · "
                      + _("draw a line from the agent to this box on the map to add it."))
        self.mcp_skills_section(body, server)
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
        self.intro_card(body, _("Skills teach your agents how to do a task."), [
            _("The agents below read this folder. Each one uses a skill from it when a task fits: you just "
              "ask for the task. To give the folder to another agent, draw a line on the map from that agent "
              "to it."),
        ])
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
        self.section_heading(body, _("Used by"))
        self.detail_grid(body, rows)
        missing = [a.name for a in self.agent_list
                   if a.installed and a.command not in skill_set.agents and skills_ctl.own_dir(a.command)]
        if missing:
            self.section_heading(body, _("Not set up"))
            self.note(body, ", ".join(missing) + " · "
                      + _("draw a line from the agent to this box on the map to add it."))
        self.section_heading(body, ngettext("1 skill", "{0} skills", len(skill_set.skills))
                              .format(len(skill_set.skills)))
        folder = skills_ctl.expand(path)
        located = {p.name: p for p in skills_ctl.skills_of(folder)}
        self.detail_grid(body, [(name, (skills_ctl.description(located[name]) if name in located else "") or "—", "")
                                for name in skill_set.skills])
        self.show_page("mcp")
        self.update_header()

    def skills_of_server(self, name: str) -> list:
        """The skill folders written for this MCP server (one per real folder)."""
        folders, seen = [], set()
        for skill_set in self.inventory.skill_sets:
            for folder in skills_ctl.skills_of(skills_ctl.expand(skill_set.path)):
                real = os.path.realpath(folder)
                if real not in seen:
                    seen.add(real)
                    folders.append(folder)
        return skill_update.based_on(folders, [name]).get(name, [])

    def mcp_skills_section(self, body, server):
        """Skills written for this server: do they still match its tools? Update skill… rewrites one."""
        folders = self.skills_of_server(server.name)
        if not folders:
            return
        self.section_heading(body, _("Skills for this server"))
        tools = mcp_tools.cached(server.name)
        texts = {"current": _("Matches the server's tools as last asked."),
                 "changed": _("The server's tools changed since the skill was updated: update it."),
                 "unknown": _("Not compared with the server's tools yet.")}
        for folder in folders:
            line = Gtk.Box(spacing=10)
            file = skill_update.own_skill_file(folder)
            if file is None:
                text = _("Comes with its app (not your own file): it is updated with the app.")
            elif str(file) in self.updating_skills:
                text = self.updating_skills[str(file)]
            else:
                text = texts[skill_update.status(file, server.name, tools)]
            label = Gtk.Label(label=f"{folder.name} · {text}", xalign=0, wrap=True, max_width_chars=1, hexpand=True)
            label.add_css_class("am-agent-detail")
            line.append(label)
            if file is not None:
                button = Gtk.Button(label=_("Update skill…"), valign=Gtk.Align.CENTER)
                button.add_css_class("am-small-btn")
                button.set_tooltip_text(_(
                    "Asks the server for its tools, then {0} rewrites the skill to match them (without any "
                    "tools of its own). You see the change first; Undo brings the old skill back.").format(
                    " / ".join(skill_update.writers(self.installed_commands())) or "Claude Code / Codex"))
                button.set_sensitive(str(file) not in self.updating_skills
                                     and bool(skill_update.writers(self.installed_commands())))
                button.connect("clicked", lambda _b, f=file: self.update_skill(server, f))
                line.append(button)
            body.append(line)

    def installed_commands(self) -> list:
        return [a.command for a in self.agent_list if a.installed]

    def update_skill(self, server, file: Path):
        """Update skill…: ask the server, let an agent rewrite the SKILL.md, then the preview."""
        name = server.name
        entry = None
        for command in server.agents:
            entry = mcp_ctl.normal(mcp_ctl.raw_entries(command).get(name))
            if entry is not None:
                break
        if entry is None or str(file) in self.updating_skills:
            return
        installed = self.installed_commands()

        def say(text):
            self.updating_skills[str(file)] = text
            if self.page_name() == "mcp" and getattr(self, "mcp_showing", None) == name:
                self.open_mcp_details(name)
            return False

        say(_("Asking the server for its tools…"))

        def work():
            tools = mcp_tools.ask_and_keep(name, entry)
            if tools.get("status") != "ok":
                GLib.idle_add(done, None, None, tools, tools.get("error", ""))
                return
            GLib.idle_add(say, _("{0} rewrites the skill (this can take a few minutes)…").format(
                " / ".join(skill_update.writers(installed))))
            try:
                old = file.read_text(encoding="utf-8")
                new, writer = skill_update.rewrite(file, name, tools, installed)
            except (OSError, RuntimeError) as error:
                GLib.idle_add(done, None, None, tools, str(error))
                return
            GLib.idle_add(done, old, (new, writer), tools, "")

        def done(old, answer, tools, error):
            self.updating_skills.pop(str(file), None)
            if error:
                self.show_note(_("Skill {0} not updated: {1}").format(file.parent.name, error[:300]))
            elif answer[0] == old:
                skill_update.remember(file, name, tools)
                self.show_note(_("Skill {0} already matches the server's tools.").format(file.parent.name))
            else:
                self.open_skill_update(name, file, old, answer[0], answer[1], tools)
                return False
            if self.page_name() == "mcp" and getattr(self, "mcp_showing", None) == name:
                self.open_mcp_details(name)
            return False

        threading.Thread(target=work, daemon=True).start()

    def open_skill_update(self, server: str, file: Path, old: str, new: str, writer: str, tools: dict):
        self.connecting = {"skill_update": (file, old, new), "server": server, "tools": tools,
                           "from": self.view, "remove_only": False}
        self.connect_title.set_label(_("Update skill {0}").format(file.parent.name).upper())
        form = self.connect_form
        while (child := form.get_first_child()) is not None:
            form.remove(child)
        self.connect_status.set_label("")
        self.connect_preview_heading.set_visible(True)
        self.connect_preview_scroller.set_visible(True)
        self.connect_ok.set_visible(True)
        self.connect_ok.set_label(_("Update"))
        self.connect_remove.set_visible(False)
        self.connect_intro.set_label(_(
            "{0} rewrote {1} to match the tools {2} offers now ({3}). Read the change before you take it: it "
            "was written by an AI. Undo brings the old skill back.").format(
            {"claude": "Claude Code", "codex": "Codex"}.get(writer, writer), skills_ctl.tilde(file),
            server, tools.get("server") or server))
        self.update_connect_preview()
        self.show_page("connect")

    def mcp_tools_section(self, body, server):
        """The tools (and prompts) the server offers: asked on request, kept with the date."""
        self.section_heading(body, _("Tools"))
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
                self.section_heading(body, _("Prompts"))
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

    ADD_MCP_TABS = (("picks", N_("Recommended")), ("web", N_("MCP registry")), ("local", N_("On this PC")),
                    ("hand", N_("By hand")))
    RUN_TEXT = {
        "npm": N_("Runs on this PC with Node.js. npx downloads {0} on its first start and keeps it in its cache."),
        "pypi": N_("Runs on this PC with uv. uvx downloads {0} on its first start into an environment of its own."),
        "oci": N_("Runs on this PC in a Docker container ({0}), apart from your files unless you share a folder."),
        "remote": N_("Runs at {0}. Nothing is installed; what the agents send to it leaves your PC."),
    }
    RUN_KIND = {"npm": N_("Program · npm"), "pypi": N_("Program · Python"), "oci": N_("Program · Docker"),
                "remote": N_("Web service")}
    RUNNERS = {"npx": N_("Node.js (npx) is not installed yet: sudo pacman -S nodejs npm"),
               "uvx": N_("uv (uvx) is not installed yet: sudo pacman -S uv"),
               "docker": N_("Docker is not installed yet: sudo pacman -S docker")}

    @staticmethod
    def section_heading(box, text: str):
        """A section heading of the Add pages and the MCP / skills pages: one style for all."""
        label = Gtk.Label(label=text.upper(), xalign=0, margin_top=6)
        label.add_css_class("am-section-heading")
        box.append(label)
        return label

    def intro_card(self, box, title: str, lines):
        """A short explanation at the top of a page: what this is and how it is used."""
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        card.add_css_class("am-model-card")
        head = Gtk.Label(label=title, xalign=0, wrap=True, max_width_chars=1)
        head.add_css_class("am-intro-title")
        card.append(head)
        for line in lines:
            text = Gtk.Label(label=line, xalign=0, wrap=True, max_width_chars=1)
            text.add_css_class("am-agent-detail")
            card.append(text)
        box.append(card)
        return card

    def build_add_mcp_page(self) -> Gtk.Widget:
        """Laid out like the models page: what MCP is, tabs (Recommended, the MCP registry, On this
        PC, By hand), a list on the left and the chosen server's page on the right, with how it
        runs, what it needs and the agents that get it."""
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        page.add_css_class("am-add-page")       # one text scale (see the CSS)
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
        self.intro_card(page, _("MCP servers give your agents new tools."), [
            _("A server can read your files, open web pages, drive a browser or work with GitHub. It is a small "
              "program on this PC or a service on the web; the agents start or call it when a task needs it."),
            _("Pick a server, fill in what it needs (a folder, a key), tick the agents, then Test and Add. "
              "To give it to more agents later, draw a line on the map from the agent to the server."),
        ])

        switch = Gtk.Box(spacing=0, halign=Gtk.Align.START)
        switch.add_css_class("am-segmented")
        self.add_tab_buttons = {}
        group = None
        for key, label in self.ADD_MCP_TABS:
            button = Gtk.ToggleButton(label=_(label), group=group)
            group = group or button
            button.add_css_class("am-segment")
            button.connect("toggled", lambda b, k=key: b.get_active() and self.show_add_tab(k, save=True))
            switch.append(button)
            self.add_tab_buttons[key] = button
        page.append(switch)
        self.add_stack = Gtk.Stack(vexpand=True, transition_type=Gtk.StackTransitionType.CROSSFADE)
        page.append(self.add_stack)
        self.add_details = {}

        def two_panes(key, left):
            pane = Gtk.Box(spacing=18)
            left.set_size_request(MODEL_LIST_WIDTH, -1)
            pane.append(Adw.Clamp(maximum_size=MODEL_LIST_WIDTH, child=left, unit=Adw.LengthUnit.PX,
                                  halign=Gtk.Align.START, hexpand=False))
            detail = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
            scroller = self.scrolled(detail, hexpand=True)
            pane.append(scroller)
            self.add_details[key] = (scroller, detail)
            self.add_stack.add_named(pane, key)

        # Recommended: the reference servers and official ones of well-known makers.
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.add_picks = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.add_picks.add_css_class("am-list")
        pick_cards(self.add_picks)
        self.add_picks.connect("row-selected", lambda _l, row: row is not None and self.show_pick(row.pick))
        left.append(self.scrolled(self.add_picks))
        two_panes("picks", left)

        # The MCP registry (or a registry the user added).
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        left.append(self.source_picker("mcp"))
        self.add_query = Gtk.SearchEntry(hexpand=True, placeholder_text=_("Search servers, e.g. postgres, notion"))
        self.add_query.connect("activate", lambda *_: self.search_mcp())
        left.append(self.add_query)
        self.add_web_status = Gtk.Label(xalign=0, wrap=True, max_width_chars=1,
                                        label=_("Type what a server should work with, e.g. postgres, notion, slack, "
                                                "home assistant. Anyone can publish to the registry: each server's "
                                                "page says who published it and where it runs."))
        self.add_web_status.add_css_class("am-agent-detail")
        left.append(self.add_web_status)
        self.add_web_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.add_web_list.add_css_class("am-list")
        pick_cards(self.add_web_list)
        self.add_web_list.connect("row-selected",
                                  lambda _l, row: row is not None and self.show_registry_server(row.server))
        left.append(self.scrolled(self.add_web_list))
        two_panes("web", left)

        # On this PC: programs that offer an MCP server and are not set up yet.
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        top = Gtk.Box(spacing=8)
        self.add_found_status = Gtk.Label(xalign=0, wrap=True, max_width_chars=1, hexpand=True)
        self.add_found_status.add_css_class("am-agent-detail")
        top.append(self.add_found_status)
        rescan = Gtk.Button(label=_("Scan again"), valign=Gtk.Align.START)
        rescan.add_css_class("am-small-btn")
        rescan.connect("clicked", lambda *_: self.scan_mcp(force=True))
        top.append(rescan)
        self.add_check_updates = Gtk.Button(label=_("Check for updates"), valign=Gtk.Align.START)
        self.add_check_updates.add_css_class("am-small-btn")
        self.add_check_updates.set_tooltip_text(_(
            "Looks inside new versions of the apps whose MCP servers you use, then moves every agent to them "
            "in one step (preview, backup, Undo), tests the servers and removes the old versions."))
        self.add_check_updates.connect("clicked", lambda *_: self.check_mcp_updates())
        top.append(self.add_check_updates)
        left.append(top)
        self.add_found_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        left.append(self.scrolled(self.add_found_list))
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
        left.append(self.add_inside_row)
        two_panes("local", left)

        # By hand: only the form.
        hand = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.note(hand, _("For a server you know the command or address of, e.g. from its README."))
        hand_scroller = self.scrolled(hand, hexpand=True)
        self.add_details["hand"] = (hand_scroller, hand)
        self.add_stack.add_named(hand_scroller, "hand")

        # The setup, moved to the page shown: what it needs, the agents, the command, Test / Add.
        self.add_setup = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.add_form_heading = self.section_heading(self.add_setup, _("Set it up"))
        self.add_needs = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.add_setup.append(self.add_needs)
        self.add_key_link = Gtk.LinkButton(label=_("Where do I get it?"), halign=Gtk.Align.START, visible=False)
        self.add_setup.append(self.add_key_link)
        self.add_agents = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=4,
                                      column_spacing=8, row_spacing=4)
        self.setting_line(self.add_setup, _("For"),
                          _("The agents that get it. More later: draw lines on the map."), self.add_agents)
        self.add_agents.set_halign(Gtk.Align.FILL)
        self.add_name = Gtk.Entry(placeholder_text="my-server")
        self.add_name.connect("changed", lambda *_: self.add_changed())
        self.setting_line(self.add_setup, _("Name"), _("Letters, digits, - _ . (the agents know it by this name)"),
                          self.add_name)
        self.add_name.set_halign(Gtk.Align.FILL)
        self.add_form = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.add_advanced = Gtk.Expander(label=_("Command and settings"))
        self.add_advanced.set_child(self.add_form)
        self.add_setup.append(self.add_advanced)
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
        self.add_source = Gtk.Label(xalign=0, wrap=True, max_width_chars=1)
        self.add_source.add_css_class("am-agent-detail")
        self.add_form.append(self.add_source)

        buttons = Gtk.Box(spacing=10)
        self.add_status = Gtk.Label(xalign=0, wrap=True, max_width_chars=1, hexpand=True)
        self.add_status.add_css_class("am-agent-detail")
        buttons.append(self.add_status)
        self.add_test = Gtk.Button(label=_("Test"), valign=Gtk.Align.CENTER)
        self.add_test.add_css_class("am-small-btn")
        self.add_test.set_tooltip_text(_("Start it once and ask for its tools"))
        self.add_test.connect("clicked", lambda *_: self.test_new_mcp())
        buttons.append(self.add_test)
        self.add_ok = Gtk.Button(label=_("Add"), valign=Gtk.Align.CENTER)
        self.add_ok.add_css_class("am-primary")
        self.add_ok.add_css_class("am-download-btn")
        self.add_ok.connect("clicked", lambda *_: self.apply_new_mcp())
        buttons.append(self.add_ok)
        self.add_setup.append(buttons)
        self.section_heading(self.add_setup, _("What changes"))
        self.add_preview = Gtk.TextView(editable=False, cursor_visible=False, monospace=True)
        self.add_preview.add_css_class("am-diff")
        self.add_preview.set_wrap_mode(Gtk.WrapMode.CHAR)
        self.add_setup.append(self.add_preview)
        self.add_from = None       # {"appimage": path} or {"registry": Option} behind the form
        self.add_server = None     # the registry Server shown, its options in add_options
        self.add_options = None
        self.add_checks = {}
        self.add_need_entries = {}
        self.add_timer = 0
        self.add_tab = "picks"
        self.add_pages = {}        # tab -> (Server, Pick or None) shown on its right
        self.fill_picks()
        return page

    def add_where(self) -> str:
        stored = ai_core.load_settings().get("add_where", {})
        value = stored.get("mcp") if isinstance(stored, dict) else None
        return value if value in self.add_tab_buttons else "picks"

    def show_add_tab(self, key: str, save: bool = False):
        self.add_tab = key
        self.add_stack.set_visible_child_name(key)
        if save:
            stored = ai_core.load_settings().get("add_where", {})
            self.save_setting(add_where={**(stored if isinstance(stored, dict) else {}), "mcp": key})
        if key == "hand":
            if self.add_from is not None:          # a server picked elsewhere: start empty here
                self.fill_add_form("", 0, "", "", "", [], "")
            self.add_from = self.add_server = None
            self.place_setup("hand")
            self.add_advanced.set_expanded(True)
            self.add_key_link.set_visible(False)
            self.add_source.set_label("")
            self.add_status.set_label("")
        elif key in self.add_pages:
            self.server_page(key, *self.add_pages[key])     # its setup comes back under it
        elif key == "web":
            GLib.idle_add(lambda: self.add_query.grab_focus() and False)
        elif key == "local":
            if self.found_mcp is None:
                self.scan_mcp()
            else:
                self.show_found_mcp()
                self.mark_seen("mcp")
        self.add_changed()

    def place_setup(self, key: str):
        """The setup goes under the page of the tab shown."""
        box = self.add_details[key][1]
        if self.add_setup.get_parent() is not box:
            if self.add_setup.get_parent() is not None:
                self.add_setup.get_parent().remove(self.add_setup)
            box.append(self.add_setup)

    def add_empty(self, key: str, text: str):
        _scroller, box = self.add_details[key]
        if self.add_setup.get_parent() is box:
            box.remove(self.add_setup)
        self.clear_box(box)
        box.append(self.models_empty(text))

    def fill_picks(self):
        box = self.add_picks
        self.clear_box(box)
        names = {m.name for m in getattr(self, "inventory", None) and self.inventory.mcp or []}
        for group, label in mcp_catalog.GROUPS:
            heading = Gtk.ListBoxRow(selectable=False, activatable=False)
            heading.add_css_class("am-pick-heading")
            text = Gtk.Label(label=_(label).upper(), xalign=0, margin_start=4, margin_top=8, margin_bottom=2)
            text.add_css_class("am-section-heading")
            heading.set_child(text)
            box.append(heading)
            for pick in (p for p in mcp_catalog.PICKS if p.group == group):
                maker = _("set up") if pick.key in names else pick.maker
                row = self.model_row(pick.title, _(pick.about), right=maker,
                                     icon=MCP_GLYPHS.get(pick.key, MCP_GROUP_GLYPHS.get(group, "package")))
                row.pick = pick
                box.append(row)

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
        self.fill_picks()
        for key in ("picks", "web", "local"):
            if self.add_setup.get_parent() is not self.add_details[key][1]:
                if not self.add_details[key][1].get_first_child():
                    self.add_empty(key, {
                        "picks": _("Pick a server on the left to see what it does, what it needs and how it runs."),
                        "web": _("Pick a server on the left to see who published it, what it needs and how "
                                 "it runs."),
                        "local": _("Programs on this PC that offer an MCP server are listed on the left.")}[key])
        self.show_page("addmcp")
        self.update_header()
        start = self.add_where()
        if self.add_tab_buttons[start].get_active():
            self.show_add_tab(start)
        else:
            self.add_tab_buttons[start].set_active(True)
        if self.found_mcp is None:
            self.scan_mcp()
        self.add_kind_changed()

    def close_add_mcp(self):
        self.show_view(getattr(self, "add_return", self.view), save=False)

    # A server's page ------------------------------------------------------------------------------

    def show_pick(self, pick):
        key = "picks"
        _scroller, box = self.add_details[key]
        if self.add_setup.get_parent() is box:
            box.remove(self.add_setup)
        self.clear_box(box)
        title = Gtk.Label(label=pick.title, xalign=0, wrap=True, max_width_chars=1)
        title.add_css_class("am-model-title")
        box.append(title)
        loading = Gtk.Label(label=_("Asking for the newest version…"), xalign=0)
        loading.add_css_class("am-agent-detail")
        box.append(loading)
        self.add_showing = pick.key

        def work():
            try:
                server = mcp_catalog.resolve(pick)
            except (OSError, ValueError, IndexError):
                server = None
            GLib.idle_add(done, server)

        def done(server):
            if getattr(self, "add_showing", None) != pick.key or server is None:
                if server is None:
                    loading.set_label(_("This server cannot be shown."))
                return False
            self.server_page(key, server, pick)
            return False

        threading.Thread(target=work, daemon=True).start()

    def show_registry_server(self, server):
        self.add_showing = server.name
        self.server_page("web", server, None)

    def server_page(self, key: str, server, pick):
        """What it is, who made it, how it runs (the options), what it needs, and the setup."""
        _scroller, box = self.add_details[key]
        if self.add_setup.get_parent() is box:
            box.remove(self.add_setup)
        self.clear_box(box)
        title = Gtk.Label(label=server.title or server.short, xalign=0, wrap=True, max_width_chars=1,
                          selectable=True)
        title.add_css_class("am-model-title")
        box.append(title)
        # Who made it, and how much that is checked.
        trust = Gtk.Box(spacing=8)
        if pick is not None:
            chip = Gtk.Label(label=_(mcp_catalog.TRUST[pick.trust]).format(pick.maker))
            chip.set_tooltip_text(_(mcp_catalog.TRUST_HINTS[pick.trust]))
        else:
            kind, who = mcp_registry.publisher(server.name)
            chip = Gtk.Label(label={"github": _("GitHub account {0}"), "domain": _("Domain {0}")}
                             .get(kind, "{0}").format(who or server.name))
            chip.set_tooltip_text({"github": _("The registry checked that the publisher owns this GitHub account."),
                                   "domain": _("The registry checked that the publisher owns this domain.")}
                                  .get(kind, ""))
        chip.add_css_class("am-agent-badge")
        trust.append(chip)
        stats = [server.version and _("version {0}").format(server.version),
                 server.updated and _("updated {0}").format(self.age_text(self.iso_time(server.updated)))]
        for text in stats:
            if text:
                label = Gtk.Label(label=text)
                label.add_css_class("am-agent-detail")
                trust.append(label)
        box.append(trust)
        links = Gtk.Box(spacing=4)
        for uri, label in ((server.repository, _("Repository")), (server.website, _("Website"))):
            if uri.startswith("https://"):
                link = Gtk.LinkButton(uri=uri, label=label)
                link.set_tooltip_text(uri)
                links.append(link)
        if links.get_first_child() is not None:
            box.append(links)
        if pick is None:
            note = Gtk.Label(label=_("Anyone can publish to the registry. Look at the repository before you add a "
                                     "server: a program from it runs with your rights."),
                             xalign=0, wrap=True, max_width_chars=1)
            note.add_css_class("am-agent-detail")
            box.append(note)
        if server.status and server.status != "active":
            warn = Gtk.Label(label=_("The publisher marked it {0}.").format(server.status), xalign=0)
            warn.add_css_class("am-warning-text")
            box.append(warn)

        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        card.add_css_class("am-model-card")
        about = Gtk.Label(label=_(pick.about) if pick else server.description or _("No description."),
                          xalign=0, wrap=True, max_width_chars=1, selectable=True)
        about.add_css_class("am-detail-value")
        card.append(about)
        kinds = list(dict.fromkeys(_(self.RUN_KIND[o.kind]) for o in server.options))
        needs = sorted({n.name for o in server.options for n in o.needs if n.required})
        for key_text, value in ((_("Runs as"), " | ".join(kinds)),
                                (_("Needs"), _(pick.needs) if pick else
                                 (", ".join(needs) if needs else _("nothing to fill in")))):
            line = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            key_label = Gtk.Label(label=key_text.upper(), xalign=0)
            key_label.add_css_class("am-fact-key")
            line.append(key_label)
            value_label = Gtk.Label(label=value, xalign=0, wrap=True, max_width_chars=1, selectable=True)
            value_label.add_css_class("am-detail-value")
            line.append(value_label)
            card.append(line)
        if pick is not None and pick.tries:
            tries = Gtk.Label(label=_("Then ask an agent, e.g.:") + "\n" + "\n".join(
                "“" + _(t) + "”" for t in pick.tries), xalign=0, wrap=True, max_width_chars=1, selectable=True)
            tries.add_css_class("am-agent-detail")
            card.append(tries)
        box.append(card)

        # How it runs: one row per way, the best one chosen.
        self.section_heading(box, _("How it runs"))
        options = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        options.add_css_class("am-list")
        options.add_css_class("boxed-list")
        best = None
        for option in server.options[:6]:
            row = self.run_option_row(option, server)
            options.append(row)
            if best is None and option.runnable:
                best = row
        box.append(options)
        self.add_server, self.add_options = server, options
        self.add_pages[key] = (server, pick)
        options.connect("row-selected", lambda _l, row: row is not None and self.use_registry(
            server, row.option, pick=pick))
        self.place_setup(key)
        self.add_advanced.set_expanded(False)
        options.select_row(best or options.get_row_at_index(0))

    def run_option_row(self, option, server) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3, margin_start=10, margin_end=10,
                         margin_top=6, margin_bottom=6)
        top = Gtk.Box(spacing=10)
        kind = Gtk.Label(label=_(self.RUN_KIND[option.kind]), xalign=0, hexpand=True)
        kind.add_css_class("am-model-name")
        top.append(kind)
        if option.kind != "remote":
            ident = option.label.split(" · ", 1)[-1]
            what = Gtk.Label(label=ident, xalign=1)
            what.set_ellipsize(Pango.EllipsizeMode.START)
            what.set_max_width_chars(36)
            what.add_css_class("am-agent-detail")
            top.append(what)
        column.append(top)
        if option.kind == "remote":
            text = _(self.RUN_TEXT["remote"]).format(urllib.parse.urlsplit(option.entry["url"]).hostname or "")
        else:
            text = _(self.RUN_TEXT[option.kind]).format(option.label.split(" · ", 1)[-1])
        explain = Gtk.Label(label=text, xalign=0, wrap=True, max_width_chars=1)
        explain.add_css_class("am-agent-detail")
        column.append(explain)
        if not option.runnable:
            missing = Gtk.Label(label=_(self.RUNNERS.get(option.entry["command"], N_("Needs {0}, not installed")))
                                .format(option.entry["command"]), xalign=0, wrap=True, max_width_chars=1,
                                selectable=True)
            missing.add_css_class("am-warning-text")
            column.append(missing)
        row.set_child(column)
        row.option = option
        return row

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
                lambda *_, c=candidate: self.open_mcp_update(c), _("Update…"), icon="app"))
        for candidate in sorted(fresh, key=lambda c: (c.program in seen, c.name)):
            where = Path(candidate.appimage).name if candidate.appimage else skills_ctl.tilde(candidate.program)
            if candidate.program not in seen:
                where = "★ " + _("new") + " · " + where
            self.add_found_list.append(self.choice_row(candidate.name, where,
                                                       lambda *_, c=candidate: self.use_found(c), icon="app"))

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

    def check_mcp_updates(self):
        """On this PC › Check for updates: look inside the new versions of known
        apps (the user's click: an AppImage's own --appimage-extract), scan, and
        offer every update at once."""
        if self.scanning_mcp:
            return
        self.scanning_mcp = True
        self.add_check_updates.set_sensitive(False)
        newer = mcp_scan.new_versions_found()
        self.add_found_status.set_label(ngettext(
            "Looking inside 1 new version…", "Looking inside {0} new versions…", len(newer)).format(len(newer))
            if newer else _("Looking for updates…"))
        configured = self.configured_programs()
        commands = [a.command for a in self.agent_list]

        def work():
            try:
                if newer:
                    mcp_scan.look_inside(newer)
                found = mcp_scan.scan(configured, commands)
            except (OSError, RuntimeError, subprocess.SubprocessError):
                found = []
            GLib.idle_add(done, found)

        def done(found):
            self.scanning_mcp = False
            self.add_check_updates.set_sensitive(True)
            self.found_mcp = found
            self.configured_now = None
            self.show_found_mcp()
            updates = self.mcp_updates()
            if updates:
                self.open_mcp_updates(updates)
            else:
                self.add_found_status.set_label(_("Every app with an MCP server you use is up to date."))
            return False

        threading.Thread(target=work, daemon=True).start()

    def mcp_updates(self) -> list:
        """Found apps in a newer version whose older one is still started
        (by an agent, the list or a link in ~/.local/bin)."""
        return [c for c in self.found_mcp or [] if c.update_from and (c.users or mcp_update.links(c))]

    def choice_row(self, title: str, detail: str, on_use, button: str = "", icon: str = "") -> Gtk.Widget:
        """name ······ [Add…] on one line (a dotted line leads the eye to the
        button), the details under it; with an icon, a card with the icon at its start."""
        row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
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
        if not icon:
            return row
        card = Gtk.Box(spacing=10)
        card.add_css_class("am-choice")
        card.add_css_class("am-choice-card")
        row.remove_css_class("am-choice")
        card.append(glyph_label(icon))
        card.append(row)
        return card

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
        key = "local"
        _scroller, box = self.add_details[key]
        if self.add_setup.get_parent() is box:
            box.remove(self.add_setup)
        self.clear_box(box)
        title = Gtk.Label(label=candidate.name, xalign=0, wrap=True, max_width_chars=1)
        title.add_css_class("am-model-title")
        box.append(title)
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        card.add_css_class("am-model-card")
        self.note(card, _("A program on this PC that offers an MCP server. It runs with your rights, like the "
                          "program itself."))
        self.note(card, source)
        box.append(card)
        self.place_setup(key)
        self.add_advanced.set_expanded(True)
        self.add_key_link.set_visible(False)
        self.add_from = {"appimage": candidate.appimage, "inner": candidate.inner} if candidate.appimage else None
        self.fill_add_form(candidate.name, 0, " ".join(shlex.quote(c) for c in command), "", "", [], source)

    def search_mcp(self):
        query = self.add_query.get_text().strip()
        if not query:
            return
        self.add_web_status.set_label(_("Searching… The registry often takes 10 seconds or more."))
        base = web_sources.chosen("mcp")

        def work():
            try:
                result = mcp_registry.search(query, base=base if base != web_sources.DEFAULTS["mcp"] else "")
                error = ""
            except (OSError, ValueError) as failure:
                result, error = [], str(failure)
            GLib.idle_add(done, result, error)

        def done(result, error):
            box = self.add_web_list
            self.clear_box(box)
            result = [s for s in result if s.status in ("", "active")] or result
            if self.add_query.get_text().strip() != query:
                return False                 # a newer search runs
            if error:
                self.add_web_status.set_label(_("Search failed: {0}").format(error))
            else:
                self.add_web_status.set_label(ngettext("1 server found.", "{0} servers found.", len(result))
                                              .format(len(result)))
            for server in result[:30]:
                kind, who = mcp_registry.publisher(server.name)
                kinds = " | ".join(dict.fromkeys(_(self.RUN_KIND[o.kind]) for o in server.options))
                row = self.model_row(server.title or server.short, server.description or server.name,
                                     right=kinds, icon=RUN_GLYPHS.get(server.options[0].kind, "package")
                                     if server.options else "package")
                row.set_tooltip_text(server.name)
                row.server = server
                box.append(row)
            return False

        threading.Thread(target=work, daemon=True).start()

    def use_registry(self, server, option, pick=None):
        import shlex
        entry = option.entry
        if pick is not None:
            source = _("Recommended: {0}").format(pick.repository)
        else:
            source = _("From the MCP registry: {0}").format(server.name)
            if server.repository:
                source += " · " + server.repository
        self.add_from = {"registry": option}
        if entry["transport"] == "stdio":
            command = " ".join(shlex.quote(c) for c in [entry["command"], *entry["args"]])
            pairs = "\n".join(f"{k}={v}" for k, v in entry["env"].items() if not v.startswith("{"))
            self.fill_add_form(server.short, 0, command, "", pairs, option.needs, source)
        else:
            pairs = "\n".join(f"{k}: {v}" for k, v in entry["headers"].items() if "{" not in v)
            self.fill_add_form(server.short, 1 if entry["transport"] == "http" else 2, "", entry["url"], pairs,
                               option.needs, source)
        url = pick.key_url if pick is not None else ""
        self.add_key_link.set_visible(bool(url))
        if url:
            self.add_key_link.set_uri(url)

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
                              placeholder_text=need.hint or (_("needed") if need.required else _("optional")))
            entry.connect("changed", lambda *_: self.add_changed())
            self.guard_text_field(entry)
            label = _(need.description) if need.name.startswith("arg:") else need.name
            hint = _(need.description) if need.description and not need.name.startswith("arg:") else ""
            hint = (hint + " · " if hint else "") + (_("needed") if need.required else _("optional"))
            self.setting_line(box, label[:40], hint, entry)
            entry.set_halign(Gtk.Align.FILL)
            self.add_need_entries[need.name] = (entry, need)
        self.add_source.set_label(source)
        self.add_filling = False
        self.add_kind_changed()
        self.add_status.set_label(_("Fill in what it needs, tick the agents, then Test or Add.") if needs else
                                  _("Tick the agents, then Test or Add."))
        self.add_timer and GLib.source_remove(self.add_timer)
        self.add_timer = GLib.timeout_add(1500, self.update_add_preview)    # the hint first, then why Add is off

    # The form ------------------------------------------------------------------------------------

    def add_kind_changed(self):
        program = self.add_kind.get_selected() == 0
        self.add_command_row.set_visible(program)
        self.add_url_row.set_visible(not program)
        self.add_pairs_row.set_title(_("Environment") if program else _("Headers"))
        hint = _("One per line: NAME=value") if program else _("One per line: Name: value")
        self.add_pairs_row.set_subtitle(hint)
        self.add_pairs_row.set_tooltip_text(hint)
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
        values = {name: os.path.expanduser(v) if (v := entry.get_text().strip()).startswith("~/") else v
                  for name, (entry, _need) in self.add_need_entries.items()}
        optional = [name for name, (_entry, need) in self.add_need_entries.items()
                    if not need.required and not values[name]]
        values = {k: v for k, v in values.items() if v}
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
        secrets = [name for name, (_entry, need) in self.add_need_entries.items() if need.secret and values.get(name)]
        if leaked := mcp_registry.secret_in_args(entry, secrets):
            raise ValueError(_("{0} would be on the command line, where other users of this PC can read it.")
                             .format(", ".join(leaked)))
        entry = mcp_registry.drop_unfilled(mcp_registry.fill(entry, values), optional)
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
        agent_list, runtimes = list(self.agent_list), list(self.inventory.runtimes)

        def work():
            mcp_scan.cleanup_old_cache()
            connect.prune_retired()
            kobold = install_ctl.installer("koboldcpp")
            if kobold is not None and kobold.installed_folder() is not None and kobold.models_folder():
                try:
                    kobold.sync(Path(kobold.models_folder()))      # models added since: router ids
                except (OSError, ValueError):
                    pass
            try:
                # New versions of apps looked into before: only named (nothing is run on open);
                # looking inside them is the user's click on + MCP server.
                versions = mcp_scan.new_versions_found()
            except OSError:
                versions = []
            try:
                mcp = mcp_scan.scan(configured, commands)
            except OSError:
                mcp = self.found_mcp
            try:
                skills = skills_scan.scan(known)
            except OSError:
                skills = self.found_skills
            try:
                updates = agent_updates.check(agent_list, runtimes)     # cached, asked every 6 h at most
            except (OSError, ValueError, RuntimeError):
                updates = []
            GLib.idle_add(done, mcp, skills, versions, updates)

        def done(mcp, skills, versions, updates):
            self.checking_new = False
            self.updates = updates
            self.found_mcp, self.found_skills = mcp, skills
            # Shown in the hint area below the cards (refresh_overview), not as a note over them.
            self.new_versions = sorted(p.name for p in versions)
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
        page.add_css_class("am-add-page")       # one text scale (see the CSS)
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
        self.intro_card(page, _("Skills teach your agents how to do a task."), [
            _("A skill is a folder with a SKILL.md: instructions, sometimes with scripts or templates, for one kind "
              "of work, e.g. making a PDF or releasing your app. An agent reads it only when a task fits, so many "
              "skills cost little."),
            _("Skills live in a folder; every agent that reads the folder has them at once. Pick skills from this "
              "PC or the web, choose the folder, then Add. Then just ask for the task: the agent finds the skill."),
        ])
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        body.add_css_class("am-settings")
        scroller_holder = [self.page_scroller(body)]
        page.append(scroller_holder[0])

        local_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        web_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        body.append(self.where_switch("skills", local_box, web_box))
        body.append(local_box)
        body.append(web_box)
        self.section_heading(local_box, _("Found on this PC"))
        self.skills_found_status = Gtk.Label(xalign=0, wrap=True, max_width_chars=1)
        self.skills_found_status.add_css_class("am-agent-detail")
        local_box.append(self.skills_found_status)
        self.skills_found_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        local_box.append(self.skills_found_list)

        self.section_heading(web_box, _("Search the web"))
        web_box.append(self.source_picker("skills"))
        line = Gtk.Box(spacing=8)
        self.skills_query = Gtk.Entry(hexpand=True, placeholder_text=_("e.g. pdf, docker, testing"))
        self.skills_query.connect("activate", lambda *_: self.search_skills())
        line.append(self.skills_query)
        search = Gtk.Button(label=_("Search"))
        search.add_css_class("am-small-btn")
        search.connect("clicked", lambda *_: self.search_skills())
        line.append(search)
        web_box.append(line)
        self.skills_web_status = Gtk.Label(xalign=0, wrap=True, max_width_chars=1,
                                           label=_("From the skills.sh directory; the files come from the skill's "
                                                   "GitHub repository. Skills can hold scripts an agent may run: "
                                                   "check where one comes from before adding it."))
        self.skills_web_status.add_css_class("am-agent-detail")
        web_box.append(self.skills_web_status)
        self.skills_web_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        web_box.append(self.skills_web_list)

        self.skills_selected_heading = Gtk.Label(label=_("Selected").upper(), xalign=0)
        self.skills_selected_heading.add_css_class("am-section-heading")
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
        buttons = Gtk.Box(spacing=10)
        self.skills_status = Gtk.Label(xalign=0, wrap=True, max_width_chars=1, hexpand=True)
        self.skills_status.add_css_class("am-agent-detail")
        buttons.append(self.skills_status)
        self.skills_ok = Gtk.Button(label=_("Add"), valign=Gtk.Align.CENTER)
        self.skills_ok.add_css_class("am-primary")
        self.skills_ok.add_css_class("am-download-btn")
        self.skills_ok.connect("clicked", lambda *_: self.apply_skills())
        self.skills_scroller = scroller_holder[0]
        self.skills_body = body
        buttons.append(self.skills_ok)
        body.append(buttons)
        self.section_heading(body, _("What changes"))
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
            box.append(self.choice_row(skill_set.origin, detail, lambda *_, s=skill_set: self.use_skill_set(s),
                                       icon="folder"))
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
        base = web_sources.chosen("skills")
        if not query and not web_sources.github_repo(base):
            return                    # a repository lists all its skills without a search word
        self.skills_web_status.set_label(_("Searching…"))

        def work():
            try:
                result, error = skills_web.search(query, base=base), ""
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
                detail = f"github.com/{skill.repo}" + (" · " + ngettext("1 install", "{0} installs", skill.installs)
                                                       .format(f"{skill.installs:,}") if skill.installs else "")
                box.append(self.choice_row(skill.name, detail, lambda *_, k=skill: self.use_web_skill(k),
                                           icon="skill"))
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

    def model_users(self) -> dict:
        """Model box id -> names of the agents that use it (their default model)."""
        built = getattr(getattr(self, "side_map", None), "graph", None)
        if built is None:
            return {}
        agents = {f"agent:{a.command}": a.name for a in self.agent_list if a.installed}
        users = {}
        for edge in built.edges:
            if edge.kind == "default" and edge.source in agents:
                users.setdefault(edge.target, []).append(agents[edge.source])
        return users

    def item_name(self, node_id: str) -> str:
        key, _sep, ident = node_id.partition(":")
        if key == "agent":
            return next((a.name for a in self.agent_list if a.command == ident), ident)
        if key == "runtime":
            return next((r.name for r in self.inventory.runtimes if r.key == ident), ident)
        built = getattr(self.side_map, "graph", None)
        node = next((n for n in built.nodes if n.id == node_id), None) if built is not None else None
        return node.label if node is not None else ident

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
            self.side_filter.set_label(_("Click a box to see only its connections; Shift+click selects more than one; "
                                         "▶ starts an agent."))
        self.side_filter.set_tooltip_text(self.side_filter.get_label())
        self.map_show_all.set_visible(bool(nodes))

    def refresh_map(self):
        details = {a.command: agent_details.details_for(a.command) for a in self.agent_list if a.installed}
        labels = {"skills": lambda n: ngettext("1 skill", "{0} skills", n).format(n),
                  "more": lambda n: _("+{0} more").format(n),
                  "running": lambda n: _("running") if n == 1 else _("{0} running").format(n),
                  "in_use": lambda names: _("in use · {0}").format(names)}
        # Only what is set up: adding, updating and removing happen on My setup.
        built = graph.build(self.agent_list, details, self.inventory, labels, link_hints.load(), self.running)
        headers = [_("MCP servers · skills"), _("Agents"), _("LLM providers"), _("Models")]
        for view in (self.side_map,):
            view.group_titles = {"mcp": _("MCP servers"), "skills": _("Skills"), "online": _("Online")}
            view.set_graph(built, headers)
            view.set_layers(self.map_view.layers)
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
        # My setup is a view of its own: the header's buttons stay on it.
        on_settings = page in ("settings", "help", "addons", "reorganize", "connect", "install", "models", "mcp",
                               "addmcp", "addskills", "health")
        # Faded out rather than hidden: the header keeps its height, so the
        # card does not jump when the settings page opens.
        for widget in [self.settings_button, self.help_button, self.view_switch, self.setup_button] + \
                ([self.addons_button] if self.addons_button is not None else []):
            widget.set_opacity(0 if on_settings else 1)
            widget.set_can_target(not on_settings)
            widget.set_can_focus(not on_settings)
            widget.set_sensitive(not on_settings)
        # The legend explains the map's marks: only while the map is in view (faded, so nothing moves).
        legend = getattr(self, "legend_button", None)
        if legend is not None:
            on_map = page == "main"
            legend.set_opacity(1 if on_map else 0)
            legend.set_can_target(on_map)
            legend.set_can_focus(on_map)
            legend.set_sensitive(on_map)
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
        self.open_mcp_updates([candidate])

    def open_mcp_updates(self, candidates):
        """Apps' newer versions: their users move to them (one preview, backup, Undo)."""
        names = self.agent_names()
        self.connecting = {"updates": list(candidates), "from": self.view, "remove_only": False}
        self.connect_title.set_label((_("Update {0}").format(candidates[0].name) if len(candidates) == 1 else
                                      _("Update {0} apps").format(len(candidates))).upper())
        form = self.connect_form
        while (child := form.get_first_child()) is not None:
            form.remove(child)
        self.connect_status.set_label("")
        self.connect_preview_heading.set_visible(True)
        self.connect_preview_scroller.set_visible(True)
        self.connect_ok.set_visible(True)
        self.connect_ok.set_label(_("Update"))
        self.connect_remove.set_visible(False)
        users = []
        for candidate in candidates:
            users += [names.get(a, a) for a in candidate.users if a != "list" and names.get(a, a) not in users]
        unpack = [c for c in candidates if c.appimage and not mcp_update.new_folder(c).exists()]
        if len(candidates) == 1:
            candidate = candidates[0]
            text = _("{0} {1} is replaced by {2}.").format(candidate.name, candidate.update_from, candidate.version)
            if unpack:
                text += " " + _("Unpacks {0} into {1} first (the server lives inside it).").format(
                    Path(candidate.appimage).name, skills_ctl.tilde(mcp_update.new_folder(candidate)))
        else:
            text = "\n".join("{0}  {1} → {2}".format(c.name, c.update_from, c.version) for c in candidates) + "\n"
            if unpack:
                text += ngettext("Unpacks 1 AppImage into ~/Applications first.",
                                 "Unpacks {0} AppImages into ~/Applications first.", len(unpack)).format(len(unpack))
        if users:
            text += " " + _("{0} start the new version from then on.").format(", ".join(users))
        text += " " + _("The old version is kept with the backup for {0} days: Undo brings everything back.").format(
            connect.RETIRED_DAYS)
        text += " " + _("Afterwards each server is tested.")
        self.connect_intro.set_label(text)
        self.update_connect_preview()
        self.show_page("connect")

    def test_updated_mcp(self, updates, backup):
        """After an update: every server that starts a new version answers? (one
        after the other, in the background; the Tools of each are kept)."""
        agents = [a.command for a in self.agent_list if a.installed]

        def work():
            failed, count, servers = [], 0, []
            for name, entry in mcp_update.updated_entries(updates, agents).items():
                count += 1
                result = mcp_tools.ask_and_keep(name, entry)
                if result.get("status") != "ok":
                    failed.append(f"{name}: {result.get('error', '')}"[:200])
                else:
                    servers.append(name)
            GLib.idle_add(done, count, failed, servers)

        def done(count, failed, servers):
            # Skills written for an updated server whose tools changed: say so (Update skill… on its page).
            stale = [f.name for name in servers for f in self.skills_of_server(name)
                     if (file := skill_update.own_skill_file(f)) is not None
                     and skill_update.status(file, name, mcp_tools.cached(name)) != "current"]
            extra = (" " + ngettext("The skill {1} may need an update: MCP server page › Update skill…",
                                    "{0} skills may need an update ({1}): MCP server page › Update skill…",
                                    len(stale)).format(len(stale), ", ".join(stale))) if stale else ""
            if failed:
                self.toast(_("Not working after the update: {0}").format("; ".join(failed)) + extra, 30, _("Undo"),
                           lambda: self.undo_last(backup))
            elif count:
                self.toast(ngettext("Tested: the updated server works.", "Tested: all {0} updated servers work.",
                                    count).format(count) + extra, 30, _("Undo"), lambda: self.undo_last(backup))
            return False

        threading.Thread(target=work, daemon=True).start()

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
        if "security" in info:
            return agent_security.plan(info["agent"].command, info["security"], info["value"])
        if "skill_update" in info:
            file, old, new = info["skill_update"]
            return connect.Plan("", "", info["server"], changes=[connect.Change(file, old, new)])
        if "updates" in info:
            return mcp_update.plan_all(info["updates"], [a.command for a in self.agent_list if a.installed])
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
        updates = info.get("updates")
        images = [Path(c.appimage) for c in updates or [] if c.appimage]
        if images and not info.get("unpacked"):
            # The new versions unpacked first (in the background), then the plan.
            self.connect_ok.set_sensitive(False)

            def work():
                error = ""
                for number, image in enumerate(images, 1):
                    GLib.idle_add(self.connect_status.set_label, _("Unpacking {0}…").format(image.name) +
                                  (f" ({number}/{len(images)})" if len(images) > 1 else ""))
                    try:
                        mcp_scan.unpack(image)
                    except (OSError, RuntimeError, subprocess.SubprocessError) as failure:
                        error = str(failure)
                        break
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
        if "security" in info:
            self.close_connect()
            self.show_undo(_("{0}: {1} is now {2}").format(info["agent"].name, info["title"], info["label"]), backup)
            return
        if "skill_update" in info:
            file = info["skill_update"][0]
            try:
                skill_update.remember(file, info["server"], info["tools"])
            except OSError:
                pass
            self.close_connect()
            self.show_undo(_("Skill {0} updated").format(file.parent.name), backup)
            return
        remove = remove or bool(info.get("remove_only"))
        if link is not None and remove:
            link_hints.forget(link[0])              # removed by hand: its hints go with it
        if info.get("replaces") is not None and not remove:
            link_hints.forget(link_hints.key(f"agent:{info['agent'].command}", f"runtime:{info['replaces'].key}"))
        if updates:
            self.found_mcp = None
            self.configured_now = None
            self.close_connect()
            self.collect()
            self.refresh_overview()
            self.refresh_map()
            self.show_undo(_("{0} updated to {1}").format(updates[0].name, updates[0].version) if len(updates) == 1
                           else _("{0} apps updated").format(len(updates)), backup)
            self.test_updated_mcp(updates, backup)
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
        if info and "security" in info:
            self.open_agent_status(info["agent"])      # back to its page, read again
            return
        if info and "skill_update" in info:
            self.open_mcp_details(info["server"])      # back to the server's page
            return
        self.show_view(info["from"] if info else self.view, save=False)

    # --- models of a local LLM (right-click → Models…) ---------------------------------------------

    MODEL_TABS = (("installed", N_("Installed")), ("web", N_("Get from Hugging Face")), ("local", N_("On this PC")))
    FIT_TEXT = {"fits": N_("Fits your graphics card"), "partly": N_("Partly in system memory: works, slower"),
                "too_large": N_("Likely too large")}

    def build_models_page(self) -> Gtk.Widget:
        """A local LLM's models, laid out like LM Studio's model search: a list on the left, the
        chosen model's page on the right. Tabs: Installed (info, context, Remove to the trash),
        Get from Hugging Face (search, model page, download options), On this PC (GGUF files
        other local LLMs or Downloads have)."""
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
        self.models_title = Gtk.Label(xalign=0)
        self.models_title.add_css_class("am-section-title")
        header.append(self.models_title)
        self.models_folder_label = Gtk.Label(xalign=0, hexpand=True)
        self.models_folder_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self.models_folder_label.add_css_class("am-agent-detail")
        header.append(self.models_folder_label)
        page.append(header)
        rule = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        rule.add_css_class("am-settings-rule")
        page.append(rule)

        # The tabs (the choice is kept, as on the other Add pages).
        switch = Gtk.Box(spacing=0, halign=Gtk.Align.START)
        switch.add_css_class("am-segmented")
        self.models_tab_buttons = {}
        group = None
        for key, label in self.MODEL_TABS:
            button = Gtk.ToggleButton(label=_(label), group=group)
            group = group or button
            button.add_css_class("am-segment")
            button.connect("toggled", lambda b, k=key: b.get_active() and self.show_models_tab(k, save=True))
            switch.append(button)
            self.models_tab_buttons[key] = button
        page.append(switch)
        self.where_models = lambda: next((k for k, b in self.models_tab_buttons.items() if b.get_active()), "web")
        self.models_stack = Gtk.Stack(vexpand=True, transition_type=Gtk.StackTransitionType.CROSSFADE)
        page.append(self.models_stack)

        # Installed: the list, and the chosen model's page.
        installed = Gtk.Box(spacing=18)
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        left.set_size_request(MODEL_LIST_WIDTH, -1)
        self.models_have_heading = Gtk.Label(xalign=0)
        self.models_have_heading.add_css_class("am-group-heading")
        left.append(self.models_have_heading)
        self.models_installed = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.models_installed.add_css_class("am-list")
        pick_cards(self.models_installed)
        self.models_installed.connect("row-selected", lambda _l, row: row is not None and self.show_installed_model(row))
        left.append(self.scrolled(self.models_installed))
        installed.append(Adw.Clamp(maximum_size=MODEL_LIST_WIDTH, child=left, unit=Adw.LengthUnit.PX,
                               halign=Gtk.Align.START, hexpand=False))
        self.models_installed_detail = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        installed.append(self.scrolled(self.models_installed_detail, hexpand=True))
        self.models_stack.add_named(installed, "installed")

        # Get from Hugging Face: search on the left, the model's page on the right.
        web = Gtk.Box(spacing=18)
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        left.set_size_request(MODEL_LIST_WIDTH, -1)
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
        left.append(self.models_pull_row)
        left.append(self.source_picker("models"))
        self.models_search = Gtk.SearchEntry(hexpand=True,
                                             placeholder_text=_("Search GGUF models, e.g. qwen3 coder"))
        self.models_search.connect("activate", lambda *_: self.search_models())
        left.append(self.models_search)
        self.models_results_heading = Gtk.Label(xalign=0)
        self.models_results_heading.add_css_class("am-group-heading")
        left.append(self.models_results_heading)
        self.models_repos = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.models_repos.add_css_class("am-list")
        pick_cards(self.models_repos)
        self.models_repos.connect("row-selected", lambda _l, row: row is not None and self.show_model_files(row.repo))
        left.append(self.scrolled(self.models_repos))
        web.append(Adw.Clamp(maximum_size=MODEL_LIST_WIDTH, child=left, unit=Adw.LengthUnit.PX,
                               halign=Gtk.Align.START, hexpand=False))
        self.models_web_detail = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        web.append(self.scrolled(self.models_web_detail, hexpand=True))
        self.models_stack.add_named(web, "web")
        # Kept across fills of the model page: the download options and their button.
        self.models_files = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.models_files.add_css_class("am-list")
        self.models_files.add_css_class("boxed-list")
        self.models_files.connect("row-selected", lambda *_: self.update_model_buttons())
        self.models_options = Gtk.ScrolledWindow(min_content_height=250, max_content_height=250)
        self.models_options.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.models_options.set_child(self.models_files)
        self.models_fit_label = Gtk.Label(xalign=0, wrap=True, max_width_chars=1, hexpand=True)
        self.models_fit_label.add_css_class("am-agent-detail")
        self.models_get = Gtk.Button(label=_("Download"), valign=Gtk.Align.CENTER)
        self.models_get.add_css_class("am-primary")
        self.models_get.add_css_class("am-download-btn")
        self.models_get.connect("clicked", lambda *_: self.start_model_download(pull=False))

        # On this PC: GGUF files the local LLM does not have yet (other LLMs' folders, Downloads, …).
        local_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        top = Gtk.Box(spacing=8)
        self.models_local_status = Gtk.Label(xalign=0, wrap=True, max_width_chars=1, hexpand=True)
        self.models_local_status.add_css_class("am-agent-detail")
        top.append(self.models_local_status)
        rescan = Gtk.Button(label=_("Scan again"), valign=Gtk.Align.START)
        rescan.add_css_class("am-small-btn")
        rescan.connect("clicked", lambda *_: self.scan_local_models())
        top.append(rescan)
        local_box.append(top)
        self.models_local = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.models_local.add_css_class("am-list")
        pick_cards(self.models_local)
        self.models_local.connect("row-selected", lambda *_: self.update_model_buttons())
        local_box.append(self.scrolled(self.models_local, hexpand=True))
        add_row = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        self.models_add = Gtk.Button(label=_("Add"))
        self.models_add.add_css_class("am-small-btn")
        self.models_add.add_css_class("am-primary")
        self.models_add.connect("clicked", lambda *_: self.add_local_model())
        add_row.append(self.models_add)
        local_box.append(add_row)
        self.models_stack.add_named(local_box, "local")

        # Below every tab: progress, what happened, Back / Stop download.
        self.models_bar = Gtk.ProgressBar()
        self.models_bar.add_css_class("am-progress")
        page.append(self.models_bar)
        bottom = Gtk.Box(spacing=8)
        self.models_status = Gtk.Label(xalign=0, wrap=True, max_width_chars=1, hexpand=True)
        self.models_status.add_css_class("am-agent-detail")
        bottom.append(self.models_status)
        self.models_cancel = Gtk.Button(label=_("Cancel"))
        self.models_cancel.add_css_class("am-action-btn")
        self.models_cancel.set_valign(Gtk.Align.START)
        self.models_cancel.connect("clicked", lambda *_: self.cancel_model_download())
        bottom.append(self.models_cancel)
        page.append(bottom)
        self.models_state = None
        return page

    @staticmethod
    def scrolled(child, hexpand: bool = False) -> Gtk.ScrolledWindow:
        scroller = Gtk.ScrolledWindow(hexpand=hexpand, vexpand=True)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_child(child)
        return scroller

    def show_models_tab(self, key: str, save: bool = False):
        self.models_stack.set_visible_child_name(key)
        if save:
            stored = ai_core.load_settings().get("add_where", {})
            self.save_setting(add_where={**(stored if isinstance(stored, dict) else {}), "models": key})
        state = self.models_state
        if state is not None and key == "web" and not state.get("searched"):
            self.search_models()                 # trending models to start with
        self.update_model_buttons()

    def open_models(self, rt):
        self.models_state = {"rt": rt, "busy": False, "cancel": False, "from": self.view, "repo": None,
                             "vram": install_ctl.detect_gpu().vram, "ram": models_ctl.system_ram(),
                             "scanned": False, "scanning": False, "searched": False}
        while (row := self.models_local.get_first_child()) is not None:
            self.models_local.remove(row)
        self.models_local_status.set_label("")
        self.models_title.set_label(_("Models of {0}").format(rt.name).upper())
        self.models_folder_label.set_label(_("Model folder: {0}").format(inventory.tilde(rt.models_dir))
                                           if rt.models_dir else "")
        self.models_pull_row.set_visible(rt.key == "ollama")
        self.models_status.set_label(
            _("Ollama is started for a download if it is not running.") if rt.key == "ollama" else
            _("Jan adds models in its own app (Hub); here you set their context.") if rt.key == "jan" else "")
        self.models_bar.set_fraction(0)
        for listbox in (self.models_repos, self.models_files):
            while (row := listbox.get_first_child()) is not None:
                listbox.remove(row)
        self.clear_box(self.models_web_detail)
        self.models_web_detail.append(self.models_empty(_("Pick a model on the left to see what it is, how big "
                                                          "it is and which size fits your computer.")))
        self.fill_model_list(rt)
        stored = ai_core.load_settings().get("add_where", {})
        start = stored.get("models") if isinstance(stored, dict) else None
        start = start if start in self.models_tab_buttons else ("installed" if rt.models else "web")
        self.show_page("models")
        if self.models_tab_buttons[start].get_active():
            self.show_models_tab(start)
        else:
            self.models_tab_buttons[start].set_active(True)
        if start == "web":
            self.models_search.grab_focus()

    @staticmethod
    def clear_box(box):
        while (child := box.get_first_child()) is not None:
            box.remove(child)

    @staticmethod
    def models_empty(text: str) -> Gtk.Widget:
        label = Gtk.Label(label=text, wrap=True, max_width_chars=1, xalign=0.5, vexpand=True,
                          valign=Gtk.Align.CENTER, justify=Gtk.Justification.CENTER)
        label.add_css_class("am-agent-detail")
        return label

    def model_row(self, title: str, subtitle: str, chips=(), right: str = "", icon: str = "") -> Gtk.ListBoxRow:
        """A row of the model lists: the name, a line below, capability chips, a value on the right;
        with an icon (a ui_helpers.GLYPHS name) at its start, as on the cards of the pick lists."""
        row = Gtk.ListBoxRow()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3, margin_start=10, margin_end=10,
                      margin_top=7, margin_bottom=7, hexpand=True)
        top = Gtk.Box(spacing=6)
        name = Gtk.Label(label=title, xalign=0, hexpand=True)
        name.set_ellipsize(Pango.EllipsizeMode.END)
        name.set_max_width_chars(1)
        name.add_css_class("am-model-name")
        top.append(name)
        for chip in chips:
            top.append(self.capability_chip(chip))
        box.append(top)
        line = Gtk.Box(spacing=6)
        sub = Gtk.Label(label=subtitle, xalign=0, hexpand=True)
        sub.set_ellipsize(Pango.EllipsizeMode.END)
        sub.set_max_width_chars(1)
        sub.add_css_class("am-agent-detail")
        line.append(sub)
        row.value_label = Gtk.Label(label=right, xalign=1)
        row.value_label.add_css_class("am-agent-detail")
        line.append(row.value_label)
        box.append(line)
        if icon:
            outer = Gtk.Box(spacing=0)
            mark = glyph_label(icon)
            mark.set_margin_start(8)
            outer.append(mark)
            outer.append(box)
            row.set_child(outer)
        else:
            row.set_child(box)
        return row

    CAPABILITIES = {"vision": N_("Vision"), "tools": N_("Tools"), "reasoning": N_("Reasoning"), "mtp": "MTP"}
    CAPABILITY_HINTS = {"vision": N_("Understands images (with its mmproj file)."),
                        "tools": N_("Can call tools: what coding agents need."),
                        "reasoning": N_("Thinks before it answers."),
                        "mtp": N_("Multi-token prediction: faster answers where the local LLM supports it.")}

    def capability_chip(self, key: str) -> Gtk.Widget:
        chip = Gtk.Label(label=_(self.CAPABILITIES[key]))
        chip.add_css_class("am-cap")
        chip.add_css_class(f"am-cap-{key}")
        chip.set_tooltip_text(_(self.CAPABILITY_HINTS[key]))
        return chip

    @staticmethod
    def repo_chips(repo) -> list:
        return [k for k in ("vision", "tools", "reasoning", "mtp") if getattr(repo, k)]

    @staticmethod
    def params_text(n: int) -> str:
        return f"{n / 1e9:.1f} B".replace(".0 B", " B") if n >= 1e9 else f"{n / 1e6:.0f} M" if n else ""

    @staticmethod
    def iso_time(text: str) -> float:
        """Epoch seconds of an ISO time such as 2025-09-14T15:20:36.371442Z (0 if it is none)."""
        try:
            return dt.datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
        except (ValueError, AttributeError):
            return 0

    @staticmethod
    def age_text(epoch: float) -> str:
        if not epoch:
            return ""
        days = int((time.time() - epoch) // 86400)
        if days < 1:
            return _("today")
        return ngettext("1 day ago", "{0} days ago", days).format(days) if days < 60 else \
            ngettext("1 month ago", "{0} months ago", days // 30).format(days // 30) if days < 730 else \
            ngettext("1 year ago", "{0} years ago", days // 365).format(days // 365)

    def option_row(self, entry, fit: str) -> Gtk.ListBoxRow:
        """One download option on one line: the quantisation, how it fits, its size."""
        row = Gtk.ListBoxRow()
        line = Gtk.Box(spacing=10, margin_start=10, margin_end=10, margin_top=5, margin_bottom=5)
        quant = Gtk.Label(label=entry.quant or entry.name, xalign=0, width_chars=10)
        quant.set_ellipsize(Pango.EllipsizeMode.END)
        quant.add_css_class("am-name-part")
        line.append(quant)
        how = Gtk.Label(label=_(self.FIT_TEXT[fit]), xalign=0, hexpand=True)
        how.set_ellipsize(Pango.EllipsizeMode.END)
        how.set_max_width_chars(1)
        how.add_css_class("am-agent-detail")
        line.append(how)
        if entry.recommended:
            badge = Gtk.Label(label=_("recommended"))
            badge.add_css_class("am-agent-badge")
            line.append(badge)
        size = Gtk.Label(label=size_text(entry.size), xalign=1)
        size.add_css_class("am-fact-value")
        line.append(size)
        row.set_child(line)
        row.entry, row.fit = entry, fit
        row.add_css_class(f"am-fit-{fit}")
        row.set_tooltip_text(", ".join(entry.paths))
        return row

    def scroll_option_into_view(self, row):
        """The preselected size in the middle of its box (the list starts with the smallest)."""
        adjustment = self.models_options.get_vadjustment()
        if row.get_parent() is not self.models_files:
            return False                     # another model was chosen meanwhile
        point = row.translate_coordinates(self.models_files, 0, 0)     # (x, y), None before layout
        if point is None or row.get_height() == 0 or adjustment.get_upper() <= adjustment.get_page_size():
            tries = getattr(row, "scroll_tries", 0)
            row.scroll_tries = tries + 1
            return tries < 20                # not laid out yet: again in a moment
        adjustment.set_value(max(0, point[1] - adjustment.get_page_size() / 2 + row.get_height() / 2))
        return False

    def detail_heading(self, box, text: str):
        label = Gtk.Label(label=text, xalign=0)
        label.add_css_class("am-group-heading")
        box.append(label)

    def name_meaning(self, box, *names: str):
        """What the name means: each recognised part with one line about it."""
        seen, rows = set(), []
        for name in names:
            for part, text, values in model_names.parts(name):
                if text not in seen:
                    seen.add(text)
                    rows.append((part, _(text).format(part, *values)))
        if not rows:
            return
        self.detail_heading(box, _("What the name means"))
        grid = Gtk.Grid(column_spacing=14, row_spacing=6)
        for i, (part, text) in enumerate(rows):
            key = Gtk.Label(label=part, xalign=0, valign=Gtk.Align.START)
            key.add_css_class("am-name-part")
            grid.attach(key, 0, i, 1, 1)
            value = Gtk.Label(label=text, xalign=0, wrap=True, max_width_chars=1, hexpand=True)
            value.add_css_class("am-detail-value")
            grid.attach(value, 1, i, 1, 1)
        box.append(grid)

    def facts_grid(self, box, facts):
        grid = Gtk.Grid(column_spacing=18, row_spacing=4)
        column = 0
        for title, value in facts:
            if not value:
                continue
            key = Gtk.Label(label=title.upper(), xalign=0)
            key.add_css_class("am-fact-key")
            grid.attach(key, column, 0, 1, 1)
            val = Gtk.Label(label=value, xalign=0, selectable=True)
            val.add_css_class("am-fact-value")
            grid.attach(val, column, 1, 1, 1)
            column += 1
        box.append(grid)

    def fill_model_list(self, rt):
        """Installed: each model with its size and context; its page on the right when chosen."""
        listbox = self.models_installed
        while (row := listbox.get_first_child()) is not None:
            listbox.remove(row)
        self.models_have_heading.set_label(
            ngettext("1 model installed", "{0} models installed", len(rt.models)).format(len(rt.models)).upper())
        self.clear_box(self.models_installed_detail)
        if not rt.models:
            self.models_installed_detail.append(self.models_empty(
                _("No models yet. Get one from Hugging Face, or add one another local LLM has (On this PC).")))
            return
        rows = {}
        for name in rt.models:
            quant = models_ctl.QUANT_RE.search(name)
            row = self.model_row(name, quant.group(1).upper() if quant else "", right="", icon="model")
            row.model_name = name
            row.context = None
            listbox.append(row)
            rows[name] = row
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
            for name, result in results.items():
                row = rows[name]
                row.context = result
                now, fit_info, _error = result
                if fit_info is not None and fit_info.known:
                    row.value_label.set_label(_("context {0}").format(context_ctl.short_context(now) if now
                                                                      else _("default")))
            selected = listbox.get_selected_row()
            if selected is not None:
                self.show_installed_model(selected)
            return False

        threading.Thread(target=work, daemon=True).start()
        listbox.select_row(listbox.get_row_at_index(0))

    def show_installed_model(self, row):
        """An installed model's page: what its name means, file and size, context, Remove."""
        state = self.models_state
        if state is None:
            return
        rt, name = state["rt"], row.model_name
        box = self.models_installed_detail
        self.clear_box(box)
        title = Gtk.Label(label=name, xalign=0, wrap=True, max_width_chars=1, selectable=True)
        title.add_css_class("am-model-title")
        box.append(title)
        facts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.append(facts)
        if rt.key in ("llamacpp", "koboldcpp", "lmstudio"):
            try:
                files = models_ctl.removal(rt.key, rt.models_dir, name)
                where = Path(os.path.realpath(files.paths[0]))
                size = sum(Path(os.path.realpath(p)).stat().st_size for p in files.paths)
                self.facts_grid(facts, [(_("Size"), size_text(size)),
                                        (_("Files"), str(len(files.paths)) if len(files.paths) > 1 else "")])
                path = Gtk.Label(label=inventory.tilde(str(where)) + ("  ← " + _("linked") if files.links_only else ""),
                                 xalign=0, wrap=True, max_width_chars=1, selectable=True)
                path.add_css_class("am-agent-detail")
                facts.append(path)
            except (OSError, ValueError):
                pass
        result = row.context
        if result is not None:
            now, fit_info, error = result
            if fit_info is None:
                note = Gtk.Label(label=_("context: ?") + (f" ({error})" if error else ""), xalign=0, wrap=True,
                                 max_width_chars=1)
                note.add_css_class("am-agent-detail")
                facts.append(note)
            else:
                best = fit_info.best
                self.facts_grid(facts, [
                    (_("Context now"), context_ctl.short_context(now) if now else _("default")),
                    (_("Best for your card"), (context_ctl.short_context(best) if best else _("too big"))
                     if fit_info.known else ""),
                    (_("Trained for"), context_ctl.short_context(fit_info.native) if fit_info.native else ""),
                ])
                if fit_info.known and best and best != now and rt.key in ("llamacpp", "ollama", "lmstudio",
                                                                             "jan", "koboldcpp"):
                    set_row = Gtk.Box(spacing=8)
                    hint = Gtk.Label(label=_("Each token of context takes {0} of video memory.")
                                     .format(size_text(fit_info.per_token)), xalign=0, hexpand=True, wrap=True,
                                     max_width_chars=1)
                    hint.add_css_class("am-agent-detail")
                    set_row.append(hint)
                    button = Gtk.Button(label=_("Set context to {0}").format(context_ctl.short_context(best)))
                    button.add_css_class("am-small-btn")
                    button.connect("clicked", lambda *_: self.set_model_context(rt, name, best))
                    set_row.append(button)
                    facts.append(set_row)
        self.name_meaning(box, name)
        self.removal_box(box, rt, name)

    def removal_box(self, box, rt, name: str):
        """Remove: asks first, says what goes where (the trash; links only; Ollama's own store)."""
        self.detail_heading(box, _("Remove"))
        area = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.append(area)
        if rt.key not in ("llamacpp", "koboldcpp", "lmstudio", "ollama"):
            note = Gtk.Label(label=_("Remove it in {0} itself.").format(rt.name), xalign=0)
            note.add_css_class("am-agent-detail")
            area.append(note)
            return
        others = {r.name: (r.key, r.models_dir) for r in self.inventory.runtimes
                  if r.installed and r.models_dir and r.key != rt.key}

        def ask(_button):
            self.clear_box(area)
            try:
                plan = None if rt.key == "ollama" else models_ctl.removal(rt.key, rt.models_dir, name, others)
            except (OSError, ValueError) as problem:
                note = Gtk.Label(label=_("Cannot remove it here: {0}").format(problem), xalign=0, wrap=True,
                                 max_width_chars=1)
                note.add_css_class("am-agent-detail")
                area.append(note)
                return
            if plan is None:
                text = _("Ollama deletes {0} from its own store (Ollama has no trash).").format(name)
            elif plan.links_only:
                text = _("Only the link in the model folder goes to the trash; the file itself stays where it is.")
            else:
                text = _("{0} go to the trash ({1} files); you can restore them from there.") \
                    .format(size_text(plan.size), len(plan.paths))
            if plan is not None and plan.shared:
                text += " " + _("{0} reads the same file and loses it too.").format(", ".join(plan.shared))
            note = Gtk.Label(label=text, xalign=0, wrap=True, max_width_chars=1)
            note.add_css_class("am-detail-value")
            area.append(note)
            buttons = Gtk.Box(spacing=8)
            go = Gtk.Button(label=_("Delete in Ollama") if plan is None else _("Move to trash"))
            go.add_css_class("am-small-btn")
            go.add_css_class("am-danger")
            go.connect("clicked", lambda *_: self.remove_model(rt, name, plan))
            buttons.append(go)
            keep = Gtk.Button(label=_("Keep"))
            keep.add_css_class("am-small-btn")
            keep.connect("clicked", lambda *_: (self.clear_box(area), area.append(start_button())))
            buttons.append(keep)
            area.append(buttons)

        def start_button():
            button = Gtk.Button(label=_("Remove {0}…").format(name), halign=Gtk.Align.START)
            button.add_css_class("am-small-btn")
            button.connect("clicked", ask)
            return button

        area.append(start_button())

    def remove_model(self, rt, name: str, plan):
        state = self.models_state
        self.models_status.set_label(_("Removing {0}…").format(name))

        def work():
            try:
                if plan is None:
                    models_ctl.ollama_delete(name, rt.address)
                else:
                    models_ctl.trash(plan.paths)
                error = None
            except (OSError, ValueError, RuntimeError) as problem:
                error = str(problem)
            GLib.idle_add(done, error)

        def done(error):
            if error:
                self.models_status.set_label(_("Not removed: {0}").format(error))
                return False
            self.collect()
            self.refresh_overview()
            self.refresh_map()
            if self.models_state is state and state is not None:
                fresh = next((r for r in self.inventory.runtimes if r.key == rt.key), rt)
                state["rt"] = fresh
                self.fill_model_list(fresh)
                self.models_status.set_label(_("Removed: {0}").format(name) +
                                             ("" if plan is None else " " + _("(in the trash)")))
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

    def model_job(self, state) -> "jobs.Job | None":
        """The download running for the models page's local LLM (started here or earlier)."""
        if state is None:
            return None
        found = [j for j in self.jobs.active(f"runtime:{state['rt'].key}") if j.kind in ("model", "pull")]
        return found[-1] if found else None

    def search_models(self):
        """Search (or, with nothing typed, the trending GGUF models) on the chosen site."""
        state = self.models_state
        if state is None:
            return
        query = self.models_search.get_text().strip()
        state["searched"] = True
        state["query"] = query
        self.models_status.set_label(_("Searching…"))
        state["base"] = base = web_sources.chosen("models")

        def work():
            try:
                result, error = models_ctl.search(query, limit=25, base=base), None
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as problem:
                result, error = [], str(problem)
            GLib.idle_add(done, result, error)

        def done(repos, error):
            if self.models_state is not state or state.get("query") != query:
                return False
            while (row := self.models_repos.get_first_child()) is not None:
                self.models_repos.remove(row)
            self.models_results_heading.set_label((_("Results") if query else _("Trending now")).upper())
            for repo in repos:
                line = " · ".join(x for x in (repo.author, self.params_text(repo.params),
                                              "↓ " + short_number(repo.downloads), "♥ " + short_number(repo.likes))
                                  if x)
                row = self.model_row(repo.name, line, self.repo_chips(repo), self.age_text(repo.updated),
                                     icon="model")
                row.repo = repo.id
                self.models_repos.append(row)
            self.models_status.set_label(error or ("" if repos else _("Nothing found.")))
            self.update_model_buttons()
            return False

        threading.Thread(target=work, daemon=True).start()

    def show_model_files(self, repo: str):
        """The model's page: numbers, facts, capabilities, what the name means, the download
        options with how each fits, and the model card's text."""
        state = self.models_state
        state["repo"] = repo
        box = self.models_web_detail
        self.clear_box(box)
        title = Gtk.Label(label=repo, xalign=0, wrap=True, max_width_chars=1, selectable=True)
        title.add_css_class("am-model-title")
        box.append(title)
        loading = Gtk.Label(label=_("Reading {0}…").format(repo), xalign=0)
        loading.add_css_class("am-agent-detail")
        box.append(loading)
        while (row := self.models_files.get_first_child()) is not None:
            self.models_files.remove(row)
        base = state.get("base") or models_ctl.HF

        def work():
            info = entries = None
            errors = []
            try:
                info = models_ctl.details(repo, base=base)
            except (OSError, ValueError, RuntimeError) as problem:
                errors.append(str(problem))
            try:
                entries = models_ctl.files(repo, state["vram"], base=base)
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as problem:
                errors.append(str(problem))
            card = models_ctl.readme(repo, base=base) if info is not None else ""
            GLib.idle_add(done, info, entries or [], card, "; ".join(errors))

        def done(info, entries, card, error):
            if self.models_state is not state or state["repo"] != repo:
                return False
            box.remove(loading)
            if info is not None:
                stats = Gtk.Box(spacing=14)
                for text in ("↓ " + short_number(info.downloads), "♥ " + short_number(info.likes),
                             _("updated {0}").format(self.age_text(info.updated)) if info.updated else "",
                             info.license and _("license {0}").format(info.license)):
                    if text:
                        label = Gtk.Label(label=text)
                        label.add_css_class("am-agent-detail")
                        stats.append(label)
                link = Gtk.LinkButton(uri=f"{base}/{repo}", label=_("Open on the web"), hexpand=True,
                                      halign=Gtk.Align.END)
                stats.append(link)
                box.append(stats)
                if info.gated:
                    note = Gtk.Label(label=_("Gated: accept its terms on Hugging Face first, or the download "
                                             "is refused."), xalign=0, wrap=True, max_width_chars=1)
                    note.add_css_class("am-warning-text")
                    box.append(note)
                card_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
                card_box.add_css_class("am-model-card")
                active = model_names.SIZE_MOE.search(repo) or re.search(r"[-_]A(\d+(?:\.\d+)?)B(?![A-Za-z])", repo)
                self.facts_grid(card_box, [
                    (_("Parameters"), self.params_text(info.params)),
                    (_("Active"), f"{active.groups()[-1]} B" if active else ""),
                    (_("Architecture"), info.architecture),
                    (_("Context"), context_ctl.short_context(info.context) if info.context else ""),
                    (_("Format"), "GGUF"),
                ])
                chips = Gtk.Box(spacing=6)
                for key in self.repo_chips(info):
                    chips.append(self.capability_chip(key))
                if chips.get_first_child() is not None:
                    card_box.append(chips)
                if info.base_model:
                    based = Gtk.Label(label=_("Based on {0}").format(info.base_model), xalign=0, selectable=True)
                    based.add_css_class("am-agent-detail")
                    card_box.append(based)
                box.append(card_box)
            # Download options: every size, how it fits, the best one chosen.
            self.detail_heading(box, _("Download options"))
            pick = None
            for entry in entries:
                fit = models_ctl.fit(entry.size, state["vram"], state["ram"])
                row = self.option_row(entry, fit)
                self.models_files.append(row)
                if entry.recommended or (pick is None and fit != "too_large"):
                    pick = row if entry.recommended or pick is None else pick
            if entries:
                if self.models_options.get_parent() is not None:
                    self.models_options.get_parent().remove(self.models_options)
                box.append(self.models_options)
                action = Gtk.Box(spacing=10)
                for widget in (self.models_fit_label, self.models_get):
                    if widget.get_parent() is not None:
                        widget.get_parent().remove(widget)
                    action.append(widget)
                box.append(action)
                chosen = pick or self.models_files.get_row_at_index(0)
                self.models_files.select_row(chosen)
                GLib.timeout_add(120, self.scroll_option_into_view, chosen)
            else:
                none = Gtk.Label(label=_("No GGUF files in {0}.").format(repo), xalign=0)
                none.add_css_class("am-agent-detail")
                box.append(none)
            self.name_meaning(box, repo)          # the chosen size is explained next to Download
            if card:
                self.detail_heading(box, _("About the model (from its page)"))
                text = Gtk.Label(label=card, xalign=0, wrap=True, max_width_chars=1, selectable=True,
                                 wrap_mode=Pango.WrapMode.WORD_CHAR)
                text.add_css_class("am-readme")
                box.append(text)
            self.models_status.set_label(error)
            self.update_model_buttons()
            return False

        threading.Thread(target=work, daemon=True).start()

    def update_model_buttons(self):
        state = self.models_state
        job = self.model_job(state)
        busy = bool(state and (state["busy"] or job is not None))
        rt_ok = state is not None and state["rt"].key in ("ollama", "llamacpp", "lmstudio", "koboldcpp")
        file_row = self.models_files.get_selected_row()
        if file_row is not None and getattr(file_row, "entry", None) is not None:
            entry = file_row.entry
            self.models_get.set_label(_("Download {0}").format(size_text(entry.size)))
            quant = next((_(t).format(p, *v) for p, t, v in model_names.parts(entry.name)
                          if t in [q for _p, q in model_names.QUANTS]), "")
            self.models_fit_label.set_label(" ".join(x for x in (_(self.FIT_TEXT[file_row.fit]) + ".", quant) if x))
        else:
            self.models_get.set_label(_("Download"))
            self.models_fit_label.set_label("")
        self.models_get.set_sensitive(not busy and file_row is not None and rt_ok)
        self.models_add.set_sensitive(not busy and self.models_local.get_selected_row() is not None and rt_ok)
        where = self.where_models()
        if where == "local" and state is not None and self.models_local.get_first_child() is None \
                and not state.get("scanned") and not state.get("scanning"):
            GLib.idle_add(lambda: self.scan_local_models() and False)
        self.models_cancel.set_label(_("Stop download") if busy else _("Back"))
        (self.models_cancel.add_css_class if busy else self.models_cancel.remove_css_class)("am-danger")

    def start_model_download(self, pull: bool):
        state = self.models_state
        if state is None or state["busy"] or self.model_job(state) is not None:
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
            if rt.key == "ollama" and (state.get("base") or models_ctl.HF) != models_ctl.HF:
                self.models_status.set_label(_("Ollama downloads Hugging Face models only from huggingface.co."))
                return
        if rt.key != "ollama" and not rt.models_dir:
            self.models_status.set_label(_("{0} has no model folder yet.").format(rt.name))
            return
        spec = {"kind": "pull" if rt.key == "ollama" else "model", "runtime": rt.key, "name": name,
                "repo": state["repo"] or "", "base": state.get("base") or models_ctl.HF,
                "entry": {"name": entry.name, "paths": list(entry.paths), "size": entry.size} if entry else None}
        job = self.start_model_job(spec)
        if job is not None:
            state["job"] = job.id
        self.update_model_buttons()

    def start_model_job(self, spec: dict, replaces: str = ""):
        """A model download (Hugging Face file or Ollama pull) as a background job.
        spec is plain JSON, kept by jobs.py so an interrupted download can continue."""
        rt = next((r for r in self.inventory.runtimes if r.key == spec.get("runtime") and r.installed), None)
        entry = None
        try:
            if rt is None:
                raise ValueError(_("{0} is not installed any more").format(spec.get("runtime")))
            if spec.get("entry"):
                raw = spec["entry"]
                paths = [str(p) for p in raw.get("paths", [])][:64]
                if not models_ctl.REPO_RE.fullmatch(str(spec.get("repo"))) or not paths \
                        or any(".." in p.split("/") or p.startswith("/") for p in paths) \
                        or str(spec.get("base", "")) not in web_sources.sources("models"):
                    raise ValueError(_("this download cannot be continued"))
                entry = models_ctl.GgufFile(str(raw.get("name", ""))[:200], paths, int(raw.get("size", 0)))
            elif not str(spec.get("name", "")).strip():
                raise ValueError(_("this download cannot be continued"))
        except (ValueError, TypeError, AttributeError) as error:
            self.show_note(_("Not downloaded: {0}").format(error))
            if replaces:
                self.jobs.discard(replaces)
                self.render_jobs()
            return None
        label = entry.name if entry is not None else spec["name"]
        models_dir = Path(os.path.expanduser(rt.models_dir)) if rt.models_dir else None

        def work(progress, cancelled):
            def show(fraction, text=None):
                if not text and entry is not None and entry.size:
                    text = _("{0} of {1}").format(size_text(int(fraction * entry.size)), size_text(entry.size))
                progress(fraction, text)

            if rt.key == "ollama":
                if not models_ctl.loopback_listeners(int(rt.address.rsplit(":", 1)[-1])):
                    show(0.0, _("Starting Ollama…"))
                    runtime_ctl.start(rt)
                    for _attempt in range(40):
                        if models_ctl.loopback_listeners(int(rt.address.rsplit(":", 1)[-1])):
                            break
                        time.sleep(0.5)
                models_ctl.ollama_pull(spec["name"], rt.address, show, cancelled)
            else:
                if models_dir is None:
                    raise RuntimeError(_("{0} has no model folder yet.").format(rt.name))
                models_ctl.download_gguf(rt.key, models_dir, spec["repo"], entry, show, cancelled,
                                         base=spec["base"])
                if rt.key == "koboldcpp":
                    # The new model becomes a .kcpps: an id the router answers to.
                    install_ctl.installer("koboldcpp").sync(models_dir)

        return self.jobs.start(spec["kind"], f"{label} · {rt.name}", work, spec, f"runtime:{rt.key}",
                               on_end=lambda job: GLib.idle_add(self.model_job_ended, job, rt.key, label),
                               replaces=replaces)

    def model_job_ended(self, job, key: str, label: str):
        state = self.models_state
        here = state is not None and state["rt"].key == key and state.get("job") == job.id
        if job.state == "done":
            self.collect()
            self.refresh_overview()
            self.refresh_map()
            self.show_note(_("Downloaded: {0}").format(label))
            GLib.timeout_add_seconds(10, self.dismiss_done_job, job.id)
            self.run_health()
            if here:
                fresh = next((r for r in self.inventory.runtimes if r.key == key), state["rt"])
                state["rt"] = fresh
                self.models_bar.set_fraction(1.0)
                self.models_status.set_label(_("Downloaded: {0}").format(label))
                self.fill_model_list(fresh)
        elif job.state == "failed":
            if here:
                self.models_status.set_label(_("Not downloaded: {0}").format(job.error))
            else:
                self.show_note(_("Not downloaded: {0}").format(job.error))
        elif here:
            self.models_status.set_label(_("Not downloaded: {0}").format(_("stopped")))
        if state is not None:
            self.update_model_buttons()
        self.render_jobs()
        return False

    def scan_local_models(self):
        """On this PC: GGUF files in the other local LLMs' folders, the models folder and
        Downloads that this one does not read yet (in the background; walk_gguf is capped
        in depth and number of files)."""
        state = self.models_state
        if state is None or state.get("scanning"):
            return
        rt = state["rt"]
        state["scanning"] = True
        while (row := self.models_local.get_first_child()) is not None:
            self.models_local.remove(row)
        self.models_local_status.set_label(_("Looking for GGUF models on this PC…"))
        own = Path(os.path.expanduser(rt.models_dir)) if rt.models_dir else None
        roots = [Path(os.path.expanduser(r.models_dir)) for r in self.inventory.runtimes
                 if r.installed and r.models_dir and r.key != rt.key]
        roots += [Path(os.path.expanduser(self.models_root())), Path.home() / "Downloads"]
        roots = [r for r in dict.fromkeys(Path(os.path.realpath(r)) for r in roots) if r != own and r.is_dir()]

        def work():
            try:
                have = models_ctl.seen_by(rt.key, own) if own is not None and own.is_dir() else set()
                found = models_ctl.local_ggufs(roots, have)
            except OSError:
                found = []
            GLib.idle_add(done, found)

        def done(found):
            if self.models_state is not state:
                return False
            state["scanning"], state["scanned"] = False, True
            for item in found[:500]:
                row = Gtk.ListBoxRow()
                row.local = item
                card = Gtk.Box(spacing=8, margin_start=8, margin_end=8, margin_top=6, margin_bottom=6)
                card.append(glyph_label("file"))
                line = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
                card.append(line)
                name = Gtk.Label(label=item.path.name, xalign=0)
                name.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
                name.set_max_width_chars(1)
                line.append(name)
                where = Gtk.Label(label=f"{size_text(item.size)} · {inventory.tilde(str(item.path.parent))}", xalign=0)
                where.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
                where.set_max_width_chars(1)
                where.add_css_class("am-agent-detail")
                line.append(where)
                row.set_child(card)
                self.models_local.append(row)
            if found:
                self.models_local_status.set_label(ngettext(
                    "1 model on this PC that {1} does not have yet. Adding it makes no second copy.",
                    "{0} models on this PC that {1} does not have yet. Adding it makes no second copy.",
                    len(found)).format(len(found), rt.name))
            else:
                self.models_local_status.set_label(_("No other GGUF models on this PC (looked in: {0}).").format(
                    ", ".join(inventory.tilde(str(r)) for r in roots) or "—"))
            self.update_model_buttons()
            return False

        threading.Thread(target=work, daemon=True).start()

    def add_local_model(self):
        state = self.models_state
        row = self.models_local.get_selected_row()
        if state is None or row is None:
            return
        rt, item = state["rt"], row.local
        if rt.key != "ollama" and not rt.models_dir:
            self.models_status.set_label(_("{0} has no model folder yet.").format(rt.name))
            return
        state["busy"] = True
        self.update_model_buttons()
        self.models_status.set_label(_("Adding {0}…").format(item.path.name))

        def work():
            try:
                if rt.key == "ollama":
                    models_ctl.ollama_create(item.path, rt.address)
                else:
                    models_ctl.add_local(rt.key, Path(os.path.expanduser(rt.models_dir)), item.path)
                    if rt.key == "koboldcpp":
                        install_ctl.installer("koboldcpp").sync(Path(os.path.expanduser(rt.models_dir)))
                error = None
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as problem:
                error = str(problem)
            GLib.idle_add(finished, error)

        def finished(error):
            state["busy"] = False
            self.update_model_buttons()
            if error:
                self.models_status.set_label(_("Not added: {0}").format(error))
                return False
            self.models_status.set_label(_("Added: {0}").format(item.path.name))
            self.collect()
            fresh = next((r for r in self.inventory.runtimes if r.key == rt.key), rt)
            state["rt"] = fresh
            self.fill_model_list(fresh)
            state["scanned"] = False
            self.scan_local_models()
            self.refresh_overview()
            self.refresh_map()
            return False

        threading.Thread(target=work, daemon=True).start()

    def cancel_model_download(self):
        job = self.model_job(self.models_state)
        if job is not None:
            self.jobs.stop(job.id)
            return
        self.close_models()

    def close_models(self):
        """Back: a download keeps running in the background (the main page shows it)."""
        state = self.models_state
        self.models_state = None
        self.show_view(state["from"] if state else self.view, save=False)

    # --- health check: what works, what to fix, what this setup is good for --------------------------

    def build_health_page(self) -> Gtk.Widget:
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
        back.connect("clicked", lambda *_: self.close_health())
        header.append(back)
        title = Gtk.Label(label=_("Health check").upper(), xalign=0, hexpand=True)
        title.add_css_class("am-section-title")
        header.append(title)
        again = Gtk.Button(label=_("Check again"))
        again.add_css_class("am-small-btn")
        again.connect("clicked", lambda *_: self.run_health(show=True))
        header.append(again)
        page.append(header)
        rule = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        rule.add_css_class("am-settings-rule")
        page.append(rule)
        self.health_body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        self.health_body.add_css_class("am-settings")
        page.append(self.page_scroller(self.health_body))
        return page

    def set_health_line(self, on: bool):
        self.save_setting(health_line=on)
        self.update_health_line()

    def update_health_line(self):
        """The health line shows only when something needs fixing, and never on an empty map
        (a clean Asterism with nothing switched on yet: there is nothing to check)."""
        report = self.health_report
        self.health_bar.set_visible(report is not None and report.level == "warn" and not self.health_dismissed
                                    and not self.map_empty
                                    and ai_core.load_settings().get("health_line", True) is not False)

    def dismiss_health_line(self):
        self.health_dismissed = True
        self.health_bar.set_visible(False)

    def open_health(self):
        # Where it was opened from (the update page, a terminal, Settings…): Back returns there.
        here = (self.stack.get_visible_child_name(), self.right_stack.get_visible_child_name())
        if here != ("main", "health"):
            self.health_from = here
        self.show_page("health")
        self.update_header()
        if self.health_report is not None:
            self.fill_health(self.health_report)
        self.run_health(show=True)

    def close_health(self):
        """Back (button, Backspace, Esc): the page it was opened from."""
        outer, inner = getattr(self, "health_from", None) or ("main", "map")
        self.health_from = None
        if outer != "main" and self.stack.get_child_by_name(outer) is not None:
            self.stack.set_visible_child_name(outer)
        elif inner and inner not in ("map", "health") and self.right_stack.get_child_by_name(inner) is not None \
                and not (inner == "install" and self.installing is None):
            self.stack.set_visible_child_name("main")
            self.right_stack.set_visible_child_name(inner)
        else:
            self.show_view("main", save=False)
            return
        self.place_panes()
        self.update_header()
        command = inner[5:] if outer == "main" and inner and inner.startswith("term:") else ""
        if command in getattr(self, "sessions", {}):
            GLib.idle_add(lambda: self.sessions[command].terminal.grab_focus() and False)

    def run_health(self, show: bool = False):
        """In the background: the agents' and local LLMs' own setup checks, the PC, then health.check."""
        if self.health_running:
            return
        self.health_running = True
        if show and self.health_report is None:
            while (child := self.health_body.get_first_child()) is not None:
                self.health_body.remove(child)
            self.note(self.health_body, _("Looking at the setup…"))
        agent_list, inv = list(self.agent_list), self.inventory
        built = self.side_map.graph

        def work():
            hints = link_hints.load()
            names = {a.command: a.name for a in agent_list}
            details, agent_checks, runtime_checks = {}, {}, {}
            for agent in agent_list:
                if not agent.installed:
                    continue
                try:
                    details[agent.command] = agent_details.details_for(agent.command)
                    agent_checks[agent.command] = agent_status.agent_checks(
                        agent, details[agent.command], agent_details.usage_record(agent.command), inv, hints,
                        agent_status.login_state(agent.command), issue=agent_status.load_issues().get(agent.command))
                except Exception:          # one agent's odd settings must not stop the check
                    pass
            for rt in inv.runtimes:
                if rt.installed:
                    try:
                        runtime_checks[rt.key] = agent_status.runtime_checks(rt, hints, names, None)
                    except Exception:
                        pass
            pc = health.pc_info([rt.models_dir for rt in inv.runtimes if rt.installed and rt.models_dir])
            report = health.check(agent_list, details, inv, built, pc, agent_checks, runtime_checks, hints)
            GLib.idle_add(done, report)

        def done(report):
            self.health_running = False
            self.health_report = report
            self.health_mark.set_label({"ok": "✓", "hint": "♥", "warn": "!"}[report.level])
            for level in ("ok", "hint", "warn"):
                self.health_mark.remove_css_class(f"am-health-{level}")
            self.health_mark.add_css_class(f"am-health-{report.level}")
            self.health_text.set_label(report.summary())
            self.update_health_line()
            if self.page_name() == "health":
                self.fill_health(report)
            return False

        threading.Thread(target=work, daemon=True).start()

    def fill_health(self, report):
        body = self.health_body
        while (child := body.get_first_child()) is not None:
            body.remove(child)
        state = {"ok": "ok", "hint": "warn", "warn": "missing"}[report.level]
        sentence = {"ok": _("Your AI setup is in good shape."),
                    "hint": _("Your AI setup works. A few tips make it better."),
                    "warn": _("Something needs fixing.")}[report.level]
        body.append(self.status_hero(state, sentence, report.summary()))

        # What you can do with it: one tile per use.
        self.section_in(body, _("What you can do with it"))
        tiles = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=3, min_children_per_line=1,
                            column_spacing=10, row_spacing=10, homogeneous=True)
        for use in report.uses:
            tiles.append(self.use_tile(use))
        body.append(tiles)

        for area in report.areas:
            self.section_in(body, area.title)
            listbox = self.boxed_list()
            for finding in sorted(area.findings, key=lambda f: {"warn": 0, "hint": 1, "ok": 2}[f.level]):
                listbox.append(self.finding_list_row(finding))
            body.append(listbox)

    def section_in(self, body, title: str):
        self.settings_heading(body, title)

    def use_tile(self, use) -> Gtk.Widget:
        tile = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        tile.add_css_class("card")
        tile.add_css_class("am-use-tile")
        top = Gtk.Box(spacing=8)
        icon = Gtk.Label(label=use.icon)
        icon.add_css_class("am-use-icon")
        top.append(icon)
        title = Gtk.Label(label=use.title, xalign=0, hexpand=True, wrap=True, max_width_chars=1)
        title.add_css_class("am-agent-name")
        top.append(title)
        pill = Gtk.Label(label={"ready": _("Ready"), "almost": _("Almost"), "missing": _("Not yet")}[use.level])
        pill.add_css_class("am-use-pill")
        pill.add_css_class(f"am-use-{use.level}")
        pill.set_valign(Gtk.Align.START)
        top.append(pill)
        tile.append(top)
        blurb = Gtk.Label(label=use.blurb, xalign=0, wrap=True, max_width_chars=1)
        blurb.add_css_class("am-agent-detail")
        tile.append(blurb)
        if use.have:
            have = Gtk.Label(label="✓ " + ", ".join(use.have[:5]), xalign=0, wrap=True, max_width_chars=1)
            have.add_css_class("am-use-have")
            tile.append(have)
        for step in use.steps[:2]:
            tile.append(self.finding_row(step, compact=True))
        return tile

    def finding_list_row(self, finding) -> Adw.ActionRow:
        """A finding in an area's list: mark, title, what to do, and its action button."""
        row = self.plain_row(finding.title, finding.text or "")
        mark = Gtk.Label(label={"ok": "✓", "hint": "→", "warn": "!"}[finding.level])
        mark.add_css_class("am-finding-mark")
        mark.add_css_class({"ok": "am-state-ok", "hint": "am-state-hint", "warn": "am-state-missing"}[finding.level])
        row.add_prefix(mark)
        label = self.health_action_label(finding.action)
        if label:
            button = Gtk.Button(label=label, valign=Gtk.Align.CENTER)
            button.connect("clicked", lambda *_, a=finding.action: self.health_action(a))
            row.add_suffix(button)
        return row

    def finding_row(self, finding, compact: bool = False) -> Gtk.Widget:
        row = Gtk.Box(spacing=10)
        row.add_css_class("am-finding")
        mark = Gtk.Label(label={"ok": "✓", "hint": "→", "warn": "!"}[finding.level])
        mark.add_css_class("am-finding-mark")
        mark.add_css_class({"ok": "am-state-ok", "hint": "am-state-hint", "warn": "am-state-missing"}[finding.level])
        mark.set_valign(Gtk.Align.START)
        row.append(mark)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1, hexpand=True)
        title = Gtk.Label(label=finding.title, xalign=0, wrap=True, max_width_chars=1)
        title.add_css_class("am-finding-title")
        text.append(title)
        if finding.text:
            detail = Gtk.Label(label=finding.text, xalign=0, wrap=True, max_width_chars=1)
            detail.add_css_class("am-agent-detail")
            text.append(detail)
        row.append(text)
        label = self.health_action_label(finding.action)
        if label:
            button = Gtk.Button(label=label, valign=Gtk.Align.CENTER)
            button.add_css_class("am-small-btn")
            button.connect("clicked", lambda *_, a=finding.action: self.health_action(a))
            row.append(button)
        return row

    def health_action_label(self, action) -> str:
        if not action:
            return ""
        kind = action[0]
        if kind in ("models", "runtime", "install"):
            rt = next((r for r in self.inventory.runtimes if r.key == action[1]), None)
            if rt is None or (kind == "install" and rt.installed) or (kind == "models" and not rt.installed):
                return ""
        return {"details": _("Details"), "runtime": _("Details"), "models": _("Models…"), "install": _("Install…"),
                "addmcp": _("+ MCP server"), "addskills": _("+ Skills"), "reorganize": _("Tidy up…")}.get(kind, "")

    def health_action(self, action):
        kind = action[0]
        if kind == "details":
            agent = next((a for a in self.agent_list if a.command == action[1]), None)
            if agent is not None:
                self.open_agent_status(agent)
        elif kind == "runtime":
            self.open_runtime_status(action[1])
        elif kind in ("models", "install"):
            rt = next((r for r in self.inventory.runtimes if r.key == action[1]), None)
            if rt is None:
                return
            if kind == "install":
                self.open_install(rt, "install")
                return
            self.open_models(rt)
            if len(action) > 2 and action[2]:
                self.models_search.set_text(action[2])
                self.search_models()
        elif kind == "addmcp":
            self.open_add_mcp()
            if len(action) > 1 and action[1]:
                self.add_tab_buttons["web"].set_active(True)
                self.add_query.set_text(action[1])
                self.search_mcp()
        elif kind == "addskills":
            self.open_add_skills()
        elif kind == "reorganize":
            self.open_reorganize()

    # --- background jobs: downloads and installs go on when their page is left ------------------------

    def job_detail(self, job) -> str:
        percent = f"{round(job.fraction * 100)} %"
        if job.state == "running":
            return " · ".join(part for part in (percent, job.text) if part)
        if job.state == "waiting":
            return _("Connection lost · trying again in {0} s (it goes on where it stopped)").format(job.retry_in)
        if job.state == "interrupted":
            return _("Interrupted at {0} · Continue goes on where it stopped").format(percent)
        if job.state == "failed":
            return _("Failed: {0}").format(job.error)
        if job.state == "stopped":
            return _("Stopped at {0}").format(percent)
        return _("Done")

    def job_changed(self, job):
        """A job moved on (from its thread, through the main loop)."""
        if getattr(self, "jobs_box", None) is None:
            return False
        self.render_jobs()
        state = self.models_state
        if state is not None and state.get("job") == job.id and self.page_name() == "models":
            self.models_bar.set_fraction(job.fraction)
            self.models_status.set_label(self.job_detail(job))
            self.update_model_buttons()
        info = self.installing
        if info is not None and info.get("job") == job.id:
            self.install_bar.set_fraction(job.fraction)
            self.install_status.set_label(self.job_detail(job))
        return False

    def render_jobs(self):
        """The main page's job lines: rebuilt when a job comes, goes or changes state,
        otherwise only their text and bar move."""
        # A job its own page shows (install, models) with its bar there: no second bar above it.
        page = self.page_name() if getattr(self, "stack", None) is not None else ""
        own = (getattr(self, "installing", None) or {}).get("job") if page == "install" else \
            (getattr(self, "models_state", None) or {}).get("job") if page == "models" else None
        shown = [j for j in self.jobs.shown() if own is None or j.id != own]
        layout = [(j.id, j.state) for j in shown]
        if layout != self.job_layout:
            self.job_layout = layout
            while (child := self.jobs_box.get_first_child()) is not None:
                self.jobs_box.remove(child)
            self.job_rows = {}
            for job in shown:
                self.jobs_box.append(self.job_row(job))
        for job in shown:
            row = self.job_rows.get(job.id)
            if row is not None:
                row[1].set_label(self.job_detail(job))
                row[2].set_fraction(job.fraction)
        self.jobs_box.set_visible(bool(shown))

    def job_row(self, job) -> Gtk.Widget:
        row = Gtk.Box(spacing=10)
        row.add_css_class("am-job")
        row.add_css_class(f"am-job-{job.state}")
        icons = {"running": "⬇" if job.kind != "install" else "⚙", "waiting": "⟳", "interrupted": "⏸",
                 "failed": "⚠", "stopped": "⏹", "done": "✓"}
        mark = Gtk.Label(label=icons.get(job.state, "•"))
        mark.add_css_class("am-job-mark")
        row.append(mark)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
        title = Gtk.Label(label=job.title, xalign=0)
        title.add_css_class("am-agent-name")
        title.set_ellipsize(Pango.EllipsizeMode.END)
        text.append(title)
        detail = Gtk.Label(label=self.job_detail(job), xalign=0)
        detail.add_css_class("am-agent-detail")
        detail.set_ellipsize(Pango.EllipsizeMode.END)
        text.append(detail)
        bar = Gtk.ProgressBar(fraction=job.fraction)
        bar.add_css_class("am-progress")
        bar.set_visible(job.state in ("running", "waiting", "interrupted", "stopped"))
        text.append(bar)
        row.append(text)
        buttons = []
        if job.active:
            if job.kind in ("model", "pull") and job.target.startswith("runtime:"):
                buttons.append((_("Show"), lambda j=job: self.show_job(j)))
            buttons.append((_("Stop"), lambda j=job: self.jobs.stop(j.id)))
        else:
            if job.spec is not None and job.state in ("interrupted", "failed", "stopped"):
                buttons.append((_("Continue") if job.state == "interrupted" else _("Try again"),
                                lambda j=job: self.continue_job(j)))
            buttons.append((_("Discard") if job.state == "interrupted" else _("Dismiss"),
                            lambda j=job: (self.jobs.discard(j.id), self.render_jobs())))
        for label, action in buttons:
            button = Gtk.Button(label=label, valign=Gtk.Align.CENTER)
            button.add_css_class("am-small-btn")
            button.connect("clicked", lambda _b, a=action: a())
            row.append(button)
        self.job_rows[job.id] = (row, detail, bar)
        return row

    def continue_job(self, job):
        if self.jobs.active(job.target):
            self.show_note(_("Something else runs for it right now; try again when it is done."))
            return
        if job.kind in ("model", "pull"):
            self.start_model_job(job.spec, replaces=job.id)
        else:
            self.start_install_job(job.spec, replaces=job.id)
        self.render_jobs()

    def show_job(self, job):
        """Show: the models page of its local LLM, following the download."""
        rt = next((r for r in self.inventory.runtimes if f"runtime:{r.key}" == job.target), None)
        if rt is None:
            return
        self.open_models(rt)
        if self.models_state is not None:
            self.models_state["job"] = job.id
            self.job_changed(job)

    def dismiss_done_job(self, job_id: str):
        job = self.jobs.find(job_id)
        if job is not None and job.state == "done":
            self.jobs.discard(job_id)
            self.render_jobs()
        return False

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
        # The choices right under the progress, on the right: Cancel (Stop while it runs), then the action.
        buttons = Gtk.Box(spacing=10, halign=Gtk.Align.END, margin_top=4)
        self.install_cancel = Gtk.Button(label=_("Cancel"))
        self.install_cancel.add_css_class("am-action-btn")
        self.install_cancel.connect("clicked", lambda *_: self.cancel_install())
        buttons.append(self.install_cancel)
        self.install_ok = Gtk.Button()
        self.install_ok.add_css_class("am-action-btn")
        self.install_ok.add_css_class("am-primary")
        self.install_ok.connect("clicked", lambda *_: self.run_install())
        buttons.append(self.install_ok)
        page.append(buttons)
        page.append(Gtk.Box(vexpand=True))
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
        if inst is None or (mode in ("install", "update") and not inst.can_install):
            self.show_note(_("{0} cannot be installed from here yet.").format(rt.name))
            return
        if self.jobs.active(self.install_target(rt.key)):
            self.show_note(_("{0} is busy: see the top of the main page.").format(rt.name))
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
        self.install_cancel.remove_css_class("am-danger")
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
                models = install_ctl.models_suggestions(runtimes, folders[0], rt.key)
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
                                                    install_ctl.models_suggestions(runtimes, inst.installed_folder(), rt.key))
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

    @staticmethod
    def install_target(key: str) -> str:
        return f"agent:{key}" if key in install_ctl.AGENT_INSTALLERS else f"runtime:{key}"

    def run_install(self):
        info = self.installing
        if info is None or info["busy"]:
            return
        inst, mode = info["inst"], info["mode"]
        try:
            folder = install_ctl.check_folder(self.install_folder.get_text()) if self.install_folder else None
            models = install_ctl.check_folder(self.install_models.get_text()) if self.install_models else None
        except ValueError as error:
            self.install_status.set_label(str(error))
            return
        params = {"key": inst.key, "name": info["rt"].name, "mode": mode,
                  "folder": str(folder) if folder else "", "models": str(models) if models else "",
                  "login": self.install_login.get_active() if self.install_login else False,
                  "start_now": self.install_start.get_active() if self.install_start else False,
                  "build": info.get("build", ""),
                  "release": info["release"].to_json() if info.get("release") is not None else None}
        job = self.start_install_job(params)
        if job is None:
            return
        info["busy"], info["job"] = True, job.id
        self.install_ok.set_sensitive(False)
        for widget in (self.install_folder, self.install_models, self.install_login, self.install_start,
                       getattr(self, "install_build", None)):
            if widget is not None:
                widget.set_sensitive(False)
        self.install_cancel.set_label(_("Stop"))
        self.install_cancel.add_css_class("am-danger")

    def start_install_job(self, params: dict, replaces: str = ""):
        """Install, update, model folder or uninstall as a background job. An install or
        update is kept by jobs.py (its release with the checksums) to continue after an
        interruption; the files it already downloaded and checked are not fetched again."""
        inst = install_ctl.installer(str(params.get("key")))
        mode = params.get("mode")
        try:
            if inst is None or mode not in ("install", "update", "models", "uninstall"):
                raise ValueError(_("this install cannot be continued"))
            release = install_ctl.Release.from_json(params["release"]) if params.get("release") else None
            if mode in ("install", "update") and (release is None or not inst.can_install):
                raise ValueError(_("this install cannot be continued"))
            build = str(params.get("build") or "")
            if build and build not in dict(inst.builds):
                raise ValueError(_("this install cannot be continued"))
            folder = install_ctl.check_folder(params["folder"]) if params.get("folder") else None
            models = install_ctl.check_folder(params["models"]) if params.get("models") else None
            if mode == "install" and folder is None:
                raise ValueError(_("this install cannot be continued"))
        except (ValueError, TypeError, KeyError, IndexError) as error:
            self.show_note(_("Not done: {0}").format(error))
            if replaces:
                self.jobs.discard(replaces)
                self.render_jobs()
            return None
        name = str(params.get("name") or inst.name)[:80]
        login, start_now = bool(params.get("login")), bool(params.get("start_now"))

        def work(progress, cancelled):
            nonlocal release
            gpu = install_ctl.detect_gpu()
            if replaces and release is not None:
                # Continued from jobs.json: the assets and checksums come from the release again,
                # not from the file (it only names the version that was being installed).
                fresh = inst.latest(gpu, build)
                if fresh.version != release.version:
                    raise ValueError(_("{0} {1} is out now: start the update again").format(inst.name, fresh.version))
                release = fresh
            if mode == "install":
                inst.install(folder, models, login, start_now, gpu, release, progress, cancelled)
            elif mode == "update":
                inst.update(release, gpu, progress, cancelled)
            elif mode == "models":
                inst.set_models_folder(models)
            else:
                inst.uninstall()

        titles = {"install": _("Installing {0}"), "update": _("Updating {0}"),
                  "models": _("Moving the model folder of {0}"), "uninstall": _("Uninstalling {0}")}
        spec = dict(params, kind="install") if mode in ("install", "update") else None
        return self.jobs.start("install", titles[mode].format(name), work, spec, self.install_target(inst.key),
                               on_end=lambda job: GLib.idle_add(self.install_job_ended, job, mode, name),
                               replaces=replaces)

    def install_job_ended(self, job, mode: str, name: str):
        info = self.installing
        here = info is not None and info.get("job") == job.id
        if here:
            info["busy"] = False
            self.install_cancel.set_label(_("Cancel"))
            self.install_cancel.remove_css_class("am-danger")
        if job.state == "done":
            messages = {"install": _("{0} installed"), "update": _("{0} updated"),
                        "models": _("{0}: model folder changed"), "uninstall": _("{0} uninstalled")}
            if here:
                self.close_install()
            self.collect()
            self.refresh_overview()
            self.refresh_map()
            self.show_note(messages[mode].format(name))
            GLib.timeout_add_seconds(10, self.dismiss_done_job, job.id)
            self.run_health()
        else:
            error = job.error if job.state == "failed" else _("cancelled")
            if here:
                self.install_status.set_label(_("Not done: {0}").format(error))
                self.install_ok.set_sensitive(True)
                for widget in (self.install_folder, self.install_models, self.install_login, self.install_start):
                    if widget is not None:
                        widget.set_sensitive(True)
            elif job.state == "failed":
                self.show_note(_("Not done: {0}").format(error))
        self.render_jobs()
        return False

    def cancel_install(self):
        info = self.installing
        if info is not None and info["busy"] and info.get("job"):
            self.jobs.stop(info["job"])      # the download stops; the page stays to show it
            return
        self.close_install()

    def close_install(self):
        """Back: a running install goes on in the background (the main page shows it)."""
        info = self.installing
        self.installing = None
        self.show_view(info["from"] if info else self.view, save=False)

    # --- right-click on a box ---------------------------------------------------------------

    # --- right-click menus on the map ---------------------------------------------------------

    def context_popover(self, view, rect) -> Gtk.Popover:
        """An empty right-click menu below the box (framed: popover.am-menu)."""
        popover = Gtk.Popover()
        popover.add_css_class("am-menu")
        popover.set_parent(view)
        area = Gdk.Rectangle()
        area.x, area.y, area.width, area.height = (int(v) for v in rect)
        popover.set_pointing_to(area)
        popover.set_position(Gtk.PositionType.BOTTOM)
        popover.connect("closed", lambda p: GLib.idle_add(p.unparent))
        return popover

    def show_menu(self, popover, title: str, groups):
        """The box's name, then its groups of entries with a line between groups; empty
        groups are left out, and a menu with no entries at all is not shown."""
        groups = [g for g in groups if g]
        if not groups:
            popover.unparent()
            return
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        label = Gtk.Label(label=title, xalign=0)
        label.set_ellipsize(Pango.EllipsizeMode.END)
        label.set_max_width_chars(36)
        label.add_css_class("am-agent-name")
        label.add_css_class("am-menu-title")
        box.append(label)
        for group in groups:
            rule = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
            rule.add_css_class("am-menu-rule")
            box.append(rule)
            for widget in group:
                box.append(widget)
        popover.set_child(box)
        popover.popup()

    def menu_item(self, label: str, popover, action, tip: str = "", enabled: bool = True) -> Gtk.Widget:
        """One entry: the full width of the menu, its text on the left, as every other entry."""
        item = Gtk.Button()
        text = Gtk.Label(label=label, xalign=0)
        item.set_child(text)
        item.add_css_class("flat")
        item.add_css_class("am-menu-item")
        if tip:
            item.set_tooltip_text(tip)
        item.set_sensitive(enabled)
        item.connect("clicked", lambda *_: (popover.popdown(), action()))
        return item

    @staticmethod
    def menu_note(text: str) -> Gtk.Widget:
        note = Gtk.Label(label=text, xalign=0, wrap=True, max_width_chars=36)
        note.add_css_class("am-agent-detail")
        note.add_css_class("am-menu-note")
        return note

    def map_start_state(self, node_id: str) -> str:
        """An agent box's start button: "show" while it runs here, "start" when it can be started,
        else "" (no button)."""
        kind, _sep, ident = node_id.partition(":")
        if kind != "agent":
            return ""
        agent = next((a for a in self.agent_list if a.command == ident), None)
        if agent is None or not agent.installed or not terminal_pane.program(agent.command):
            return ""
        pane = self.sessions.get(agent.command)
        return "show" if pane is not None and pane.running else "start"

    def map_start(self, node_id: str):
        """The start button on an agent's box: start it here, or show its terminal while it runs."""
        state = self.map_start_state(node_id)
        ident = node_id.partition(":")[2]
        if state == "show":
            self.show_terminal(ident)
        elif state == "start":
            agent = next(a for a in self.agent_list if a.command == ident)
            self.start_agent(agent, None, "here")

    def card_actions(self, node_id: str, popover) -> tuple[list, list]:
        """The buttons of a box's card, as menu entries: (start entries, Details entries).
        An agent: Start here / Start in terminal (Show terminal / Stop while it runs here),
        Details. A local LLM, MCP server or skill folder: Details; an MCP server also Remove."""
        kind, _sep, ident = node_id.partition(":")
        starts, details = [], []
        if kind == "agent":
            agent = next((a for a in self.agent_list if a.command == ident), None)
            if agent is None or not agent.installed:
                return starts, details
            if terminal_pane.program(agent.command):
                pane = self.sessions.get(agent.command)
                if pane is not None and pane.running:
                    starts.append(self.menu_item(_("Show terminal"), popover, lambda: self.show_terminal(ident),
                                                 _("Show the terminal {0} runs in here").format(agent.name)))
                    starts.append(self.menu_item(_("Stop"), popover, lambda: self.stop_agent(ident),
                                                 _("End {0} and what it started (its MCP servers)").format(agent.name)))
                else:
                    starts.append(self.menu_item(_("Start here"), popover,
                                                 lambda: self.start_agent(agent, None, "here"),
                                                 _("Start {0} in {1}, in a terminal in this window; Stop ends it")
                                                 .format(agent.name, self.agent_folder())))
                    starts.append(self.menu_item(_("Start in terminal"), popover,
                                                 lambda: self.start_agent(agent, None, "terminal"),
                                                 _("Start {0} in {1}, in a terminal window of its own")
                                                 .format(agent.name, self.agent_folder())))
            details.append(self.menu_item(_("Details"), popover, lambda: self.open_agent_status(agent)))
        elif kind == "runtime":
            details.append(self.menu_item(_("Details"), popover, lambda: self.open_runtime_status(ident)))
        elif kind == "mcp" and any(m.name == ident for m in self.inventory.mcp):
            details.append(self.menu_item(_("Details"), popover, lambda: self.open_mcp_details(ident)))
            details.append(self.menu_item(_("Remove"), popover, lambda: self.open_mcp_delete(ident),
                                          _("Take it from every agent and from the list")))
        elif kind == "skills" and any(s.path == ident for s in self.inventory.skill_sets):
            details.append(self.menu_item(_("Details"), popover, lambda: self.open_skills_details(ident)))
        return starts, details

    def show_context(self, view, node_id: str, rect, extra=()):
        """A box's right-click menu: what only it has (a local LLM's install, start and stop, an
        agent's install or web look-up), an update when there is one, its card's actions, then
        Details and what removes it, each group set off by a line. My setup's cards use it too."""
        kind, _sep, ident = node_id.partition(":")
        title, own = self.item_name(node_id), []
        popover = self.context_popover(view, rect)
        if kind == "runtime":
            rt = next((r for r in self.inventory.runtimes if r.key == ident), None)
            if rt is None:
                popover.unparent()
                return
            title, own = self.runtime_menu(rt, popover)
        elif kind == "agent":
            agent = next((a for a in self.agent_list if a.command == ident), None)
            if agent is None:
                popover.unparent()
                return
            if agent.command in install_ctl.AGENT_INSTALLERS:
                title, own = self.install_agent_menu(agent, popover)
            else:
                if agent.installed and self.is_new_agent(agent.command):
                    own = self.research_menu(agent, popover)
                adapter = harnesses.get(agent.command)
                if adapter is not None and adapter.install:
                    action = "uninstall" if agent.installed else "install"
                    own = list(own) + [self.menu_item(_("Uninstall…") if agent.installed else _("Install…"), popover,
                                                      lambda a=action: self.offer_install("agent", ident, a))]
            if agent.installed and self.sign_in_access(agent.command) is not None:
                own = list(own) + [self.menu_item(_("Sign in…"), popover, lambda: self.offer_sign_in(ident),
                                                  _("Subscription, account or API key"))]
        danger = []
        if kind == "model":
            own, danger = self.model_menu(view, rect, node_id, popover)
        update = self.item_update(node_id)
        newer = [self.menu_item(update[0], popover, update[1])] if update else []
        starts, details = self.card_actions(node_id, popover)
        self.show_menu(popover, title, [newer, own, starts, details, [e(popover) for e in extra], danger])

    # --- a model: move to another local LLM's folder, delete -----------------------------------

    def model_runtime(self, node_id: str):
        """(local LLM, model name) of a model box of a local LLM, else (None, "")."""
        rest = node_id.partition(":")[2]
        if not rest.startswith("runtime:"):
            return None, ""
        key = rest.split(":")[1]
        rt = next((r for r in self.inventory.runtimes if r.key == key and r.installed), None)
        if rt is None:
            return None, ""
        # Every model of the local LLM, also those past the map's ten (My setup lists them all).
        wanted = rest.split(":", 2)[2] if rest.count(":") >= 2 else ""
        name = next((m for m in rt.models if m.lower() == wanted), None)
        return (rt, name) if name is not None else (None, "")

    def folder_owners(self, folder: str) -> list:
        """The installed local LLMs (that read GGUF files) whose model folder this is."""
        return [r for r in self.inventory.runtimes
                if r.installed and r.models_dir == folder and r.key in model_move.MOVABLE]

    def move_target_ok(self, node_id: str, folder: str) -> bool:
        rt, _name = self.model_runtime(node_id)
        return rt is not None and rt.key in model_move.MOVABLE and bool(rt.models_dir) \
            and folder != rt.models_dir and bool(self.folder_owners(folder))

    def model_menu(self, view, rect, node_id: str, popover) -> tuple[list, list]:
        """A model's own entries: (Move to…, Delete…)."""
        rt, name = self.model_runtime(node_id)
        if rt is None:
            return [], []
        built = getattr(self.side_map, "graph", None)
        folders = [f for f in dict.fromkeys(r.models_dir for r in self.inventory.runtimes if r.models_dir)
                   if self.move_target_ok(node_id, f)] if built is not None else []
        move = [self.menu_item(_("Move to…"), popover, lambda: self.pick_move_folder(view, rect, node_id, folders),
                               _("Into the model folder of another local LLM; or drag the box onto that folder"))] \
            if folders else []
        delete = [self.menu_item(_("Delete…"), popover, lambda: self.confirm_delete(view, rect, rt, name))] \
            if rt.key in ("llamacpp", "koboldcpp", "lmstudio", "ollama") else []
        return move, delete

    def pick_move_folder(self, view, rect, node_id: str, folders):
        _rt, name = self.model_runtime(node_id)
        popover = self.context_popover(view, rect)
        items = []
        for folder in folders:
            owners = ", ".join(r.name for r in self.folder_owners(folder))
            items.append(self.menu_item(owners, popover, lambda f=folder: self.confirm_move(view, rect, node_id, f),
                                        inventory.tilde(folder)))
        self.show_menu(popover, _("Move {0} to").format(name), [items])

    def confirm_move(self, view, rect, node_id: str, folder: str):
        """What the move does (size, another disk, agents that use the model), then Move or Keep."""
        rt, name = self.model_runtime(node_id)
        owners = self.folder_owners(folder)
        if rt is None or not owners:
            return
        popover = self.context_popover(view, rect)
        names = ", ".join(r.name for r in owners)
        try:
            move = model_move.plan(rt.key, rt.models_dir, name, owners[0].key, folder)
        except (OSError, ValueError) as problem:
            self.show_menu(popover, _("Move {0}?").format(name),
                           [[self.menu_note(_("Cannot move it: {0}").format(problem))]])
            return
        text = ngettext("{0} in 1 file goes to {2}; {3} lists it there.",
                        "{0} in {1} files go to {2}; {3} lists it there.", len(move.steps)).format(
            size_text(move.size), len(move.steps), inventory.tilde(folder), names)
        if move.copy:
            text += " " + _("That folder is on another disk: copied first, then removed here.")
        users = self.model_users().get(node_id)
        if users:
            text += " " + _("{0} uses it through {1}: choose another model for it afterwards.").format(
                ", ".join(users), rt.name)
        self.show_menu(popover, _("Move {0}?").format(name), [
            [self.menu_note(text)],
            [self.menu_item(_("Move"), popover, lambda: self.run_move(move, names, folder)),
             self.menu_item(_("Keep"), popover, lambda: None)]])

    def run_move(self, move, names: str, folder: str):
        self.show_note(_("Moving {0}…").format(move.name))

        def work():
            try:
                done, error = model_move.apply(move, progress=lambda f, _t: GLib.idle_add(
                    self.show_note, _("Moving {0}… {1} %").format(move.name, round(f * 100)))), None
            except (OSError, ValueError, model_move.model_layout.Cancelled) as problem:
                done, error = getattr(problem, "done", []), str(problem)
            GLib.idle_add(finished, done, error)

        def finished(done, error):
            self.collect()
            self.refresh_overview()
            self.refresh_map()
            if error:
                if done:
                    model_move.undo(move, done)
                    self.collect()
                    self.refresh_map()
                self.show_note(_("Not moved: {0}").format(error))
            else:
                self.toast(_("{0} moved to {1}").format(move.name, names), 10, _("Undo"),
                           lambda: self.undo_move(move, done))
            return False

        threading.Thread(target=work, daemon=True).start()

    def undo_move(self, move, done):
        def work():
            problems = model_move.undo(move, done)
            GLib.idle_add(finished, problems)

        def finished(problems):
            self.collect()
            self.refresh_overview()
            self.refresh_map()
            self.show_note(_("Undo did not work: {0}").format("; ".join(problems)) if problems
                           else _("{0} is back").format(move.name))
            return False

        threading.Thread(target=work, daemon=True).start()

    def confirm_delete(self, view, rect, rt, name: str):
        """What Delete does (the trash; links only; Ollama's own store), then do it or Keep."""
        popover = self.context_popover(view, rect)
        others = {r.name: (r.key, r.models_dir) for r in self.inventory.runtimes
                  if r.installed and r.models_dir and r.key != rt.key}
        try:
            plan = None if rt.key == "ollama" else models_ctl.removal(rt.key, rt.models_dir, name, others)
        except (OSError, ValueError) as problem:
            self.show_menu(popover, _("Delete {0}?").format(name),
                           [[self.menu_note(_("Cannot remove it here: {0}").format(problem))]])
            return
        if plan is None:
            text = _("Ollama deletes {0} from its own store (Ollama has no trash).").format(name)
        elif plan.links_only:
            text = _("Only the link in the model folder goes to the trash; the file itself stays where it is.")
        else:
            text = _("{0} go to the trash ({1} files); you can restore them from there.") \
                .format(size_text(plan.size), len(plan.paths))
        if plan is not None and plan.shared:
            text += " " + _("{0} reads the same file and loses it too.").format(", ".join(plan.shared))
        self.show_menu(popover, _("Delete {0}?").format(name), [
            [self.menu_note(text)],
            [self.menu_item(_("Delete in Ollama") if plan is None else _("Move to trash"), popover,
                            lambda: self.delete_model(rt, name, plan)),
             self.menu_item(_("Keep"), popover, lambda: None)]])

    def delete_model(self, rt, name: str, plan):
        self.show_note(_("Removing {0}…").format(name))

        def work():
            try:
                if plan is None:
                    models_ctl.ollama_delete(name, rt.address)
                else:
                    models_ctl.trash(plan.paths)
                error = None
            except (OSError, ValueError, RuntimeError) as problem:
                error = str(problem)
            GLib.idle_add(finished, error)

        def finished(error):
            self.collect()
            self.refresh_overview()
            self.refresh_map()
            self.show_note(_("Not removed: {0}").format(error) if error else
                           _("Removed: {0}").format(name) + ("" if plan is None else " " + _("(in the trash)")))
            return False

        threading.Thread(target=work, daemon=True).start()

    def runtime_menu(self, rt, popover) -> tuple[str, list]:
        """A local LLM: install, update, models, start, stop, free GPU memory, start at login."""
        state = {"running": _("running"), "stopped": _("installed, not running")}.get(graph.runtime_status(rt),
                                                                                       _("not installed"))
        items = []
        inst = install_ctl.installer(rt.key)
        ours = inst is not None and inst.installed_folder() is not None
        actions = []
        if not rt.installed and inst is not None and inst.can_install:
            actions.append((_("Install…"), "install"))
        elif ours:
            actions += ([(_("Update…"), "update")] if inst.can_install else []) \
                + ([] if rt.key == "jan" else [(_("Model folder…"), "models")]) + [(_("Uninstall…"), "uninstall")]
        if rt.installed and rt.key in ("ollama", "llamacpp", "lmstudio", "jan", "koboldcpp"):
            actions.insert(0, (_("Models…"), "download"))
        if inst is not None and not inst.can_install and (ours or not rt.installed):
            items.append(self.menu_note(_("{0} publishes no checksum for its download, so AI Agents Map does not "
                                          "install or update it. Get it from {1}.").format(rt.name, inst.site)))
        for label, mode in actions:
            items.append(self.menu_item(label, popover, lambda m=mode: self.open_models(rt) if m == "download"
                                        else self.open_install(rt, m)))
        if not actions and rt.installed and inst is not None:
            items.append(self.menu_note(_("Installed by hand: AI Agents Map does not update or remove it.")))
        adapter = providers.get(rt.key)
        if not actions and adapter is not None and adapter.install and \
                install_recipes.choose(adapter.install, "uninstall" if rt.installed else "install",
                                       "package" if rt.installed else "") is not None:
            # Not one of the app's own installs: its recipe (a package command, a web page).
            items.append(self.menu_item(_("Uninstall…") if rt.installed else _("Install…"), popover,
                                        lambda: self.offer_install("runtime", rt.key,
                                                                   "uninstall" if rt.installed else "install")))
        if not runtime_ctl.can_control(rt):
            if rt.installed or inst is None:
                items.append(self.menu_note(_("Not installed.") if not rt.installed else
                                            _("No user service starts it, so it cannot be started from here.")))
            return f"{rt.name} · {state}", items
        if rt.running and self.runtime_users(rt.key):
            items.insert(0, self.menu_note(_("In use by {0}").format(self.runtime_users(rt.key))))
        if rt.running:
            items.append(self.menu_item(_("Stop"), popover, lambda: self.control_runtime(rt, runtime_ctl.stop, _("Stop"))))
            items.append(self.menu_item(_("Free GPU memory"), popover,
                                        lambda: self.control_runtime(rt, runtime_ctl.unload, _("Free GPU memory")),
                                        _("Unloads its models; it keeps running and loads one again when asked.")))
        else:
            items.append(self.menu_item(_("Start"), popover,
                                        lambda: self.control_runtime(rt, runtime_ctl.start, _("Start"))))
        login = runtime_ctl.start_at_login(rt)
        if login is not None:
            check = Gtk.CheckButton(label=_("Start at login"), active=login)
            check.add_css_class("am-menu-check")
            check.set_tooltip_text(_("Enables or disables the user service {0}.").format(rt.service))
            check.connect("toggled", lambda b: self.control_runtime(
                rt, lambda r: runtime_ctl.set_start_at_login(r, b.get_active()), _("Start at login")))
            items.append(check)
        return f"{rt.name} · {state}", items

    def install_agent_menu(self, agent, popover) -> tuple[str, list]:
        """An agent AI Agents Map installs itself (DeepSeek Harness, LM Studio Bionic)."""
        import types
        inst = install_ctl.AGENT_INSTALLERS[agent.command]
        ours = inst.installed_folder() is not None
        target = types.SimpleNamespace(key=agent.command, name=agent.name, installed=agent.installed)
        title = f"{agent.name} · " + (agent.version or _("installed") if agent.installed else _("not installed"))
        items = []
        update = [(_("Update…"), "update")] if inst.can_install else []
        actions = update + [(_("Uninstall…"), "uninstall")] if ours else \
            [] if agent.installed or not inst.can_install else [(_("Install…"), "install")]
        if not inst.can_install:
            items.append(self.menu_note(_("{0} publishes no checksum for its download, so AI Agents Map does not "
                                          "install or update it. Get it from {1}.").format(agent.name, inst.site)))
        elif agent.installed and not ours:
            items.append(self.menu_note(_("Installed by hand: update it the way you installed it.")))
        for label, mode in actions:
            items.append(self.menu_item(label, popover, lambda m=mode: self.open_install(target, m)))
        return title, items

    def research_menu(self, agent, popover) -> list:
        """A new agent: what the web look-up found, and Look up again."""
        _label, value, _tip = self.research_row(agent)
        return [self.menu_note(value),
                self.menu_item(_("Look up again on the web"), popover, lambda: self.start_research(agent),
                               enabled=agent.command not in self.researching)]

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

    def toast(self, message: str, seconds: int, button: str = "", on_button=None, on_dismissed=None) -> Adw.Toast:
        """One toast at a time (a new one replaces the last). Plain text: messages carry file names,
        program output and web answers, which must never be read as markup."""
        if self.current_toast is not None:
            self.current_toast.dismiss()
        toast = Adw.Toast(title=message, timeout=seconds)
        toast.set_use_markup(False)
        if button:
            toast.set_button_label(button)
            toast.connect("button-clicked", lambda *_: on_button and on_button())
        clicked = {"done": False}
        toast.connect("button-clicked", lambda *_: clicked.update(done=True))

        def dismissed(t):
            if self.current_toast is t:
                self.current_toast = None
            if on_dismissed is not None and not clicked["done"]:
                on_dismissed()
        toast.connect("dismissed", dismissed)
        self.current_toast = toast
        self.toasts.add_toast(toast)
        return toast

    def show_note(self, message: str):
        """A short message."""
        self.toast(message, 6)

    def show_notice(self, message: str, seconds: int = 8):
        """A message that stays a little longer (nothing to undo)."""
        self.toast(message, seconds)

    def show_undo(self, message: str, backup):
        """What a change did, with Undo for 30 s."""
        self.toast(message, 30, _("Undo"), lambda: self.undo_last(backup))

    def show_offer(self, message: str, yes: str, no: str, on_yes, on_no):
        """A question that stays until it is answered: the button says yes (`yes`), closing the
        toast says no (`no` is that answer's name, kept for callers; the close button says it)."""
        self.toast(message, 0, yes, on_yes, on_no)

    def undo_last(self, backup):
        try:
            connect.undo(backup)
        except (OSError, ValueError) as error:
            self.show_notice(_("Undo failed: {0}").format(error))
            return
        self.collect()
        self.refresh_overview()
        self.refresh_map()
        showing = getattr(self, "status_showing", None)
        if showing and showing[0] == "agent" and self.page_name() == "status":
            agent = next((a for a in self.agent_list if a.command == showing[1]), None)
            if agent is not None:
                self.open_agent_status(agent)          # its security settings as they are again


    # --- size ----------------------------------------------------------------------

    def launcher_entry_on_open(self):
        """The launcher entry is opt-in: asked once, then kept as the setting says
        (only on a desktop where the app offers its own entry)."""
        if not desktop.launcher_entry():
            return
        wanted = ai_core.load_settings().get("launcher_entry")
        if isinstance(wanted, bool):
            app_entry.ensure_entry(wanted)
            return

        def answer(on: bool):
            self.save_setting(launcher_entry=on)
            app_entry.ensure_entry(on)
            if not on:
                self.show_notice(_("You can add it later in Settings › Omarchy app."))
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
        work = desktop.maximize if self.widen_mode() == "maximize" else desktop.widen
        threading.Thread(target=work, args=(desktop.app_id(),), daemon=True).start()

    def update_widen_hint(self):
        """Shown while the map does not fit, unless the window is maximized already
        (then the screen itself is too narrow and maximizing cannot help)."""
        if self.split or self.widen_mode() == "off":
            self.widen_hint.set_visible(False)
            return

        def work():
            state = desktop.is_maximized(desktop.app_id())
            GLib.idle_add(lambda: self.widen_hint.set_visible(not self.split and state is not True) and False)

        threading.Thread(target=work, daemon=True).start()

    def size_limits(self) -> tuple[int, int]:
        width = getattr(self.window, "last_width", 0) or (self.window.get_width() if self.window is not None else 0)
        if width <= 0:
            geo = self.monitor_geometry() if self.window is not None else None
            width = geo.width if geo is not None else 1920
        return (max(CARD_WIDTH, width),
                max(ai_core.MIN_WINDOW_SIZE[1], monitor_height() - 2 * SCREEN_MARGIN))

    def fit(self):
        """As large as the screen allows (checked on every open: another monitor)."""
        self.card_w, self.list_h = self.size_limits()
        if getattr(self, "side_pane", None) is not None:
            self.split = self.card_w >= SPLIT_WIDTH     # narrower: the map scrolls, the widen hint shows
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
        if keyval == Gdk.KEY_F5 and page == "setup" and not self.setup_welcome:
            self.reload_setup()
            return True
        if page == "help" and keyval in (Gdk.KEY_Escape, Gdk.KEY_BackSpace):
            self.close_help()
            return True
        if page == "addons" and (keyval == Gdk.KEY_Escape or keyval == Gdk.KEY_BackSpace
                                 and not self.addons_search.has_focus()
                                 and not self.addons_search.get_delegate().has_focus()):
            self.close_addons()
            return True
        if page == "setup" and keyval == Gdk.KEY_Escape:
            self.close_setup()
            return True
        if page == "reorganize" and keyval == Gdk.KEY_Escape:
            self.close_reorganize()
            return True
        if keyval == Gdk.KEY_Escape:
            if page == "settings":
                self.close_settings()
            elif page == "connect":
                self.close_connect()
            elif page == "install":
                self.close_install()       # a running install goes on in the background
            elif page == "models":
                self.close_models()
            elif page == "mcp":
                self.close_mcp_details()
            elif page == "status":
                self.close_status()
            elif page == "health":
                self.close_health()
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
        if page in ("settings", "connect", "install", "mcp", "status", "health"):
            if keyval == Gdk.KEY_BackSpace:
                {"settings": self.close_settings, "connect": self.close_connect,
                 "install": self.close_install, "mcp": self.close_mcp_details,
                 "status": self.close_status, "health": self.close_health}[page]()
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
        return False

    # --- open / close ------------------------------------------------------------

    def toggle(self):
        """The bar icon: open it, bring it to the front, or (already in front) hide it."""
        if not self.window.is_visible():
            self.open()
        elif self.window.is_active() or time.monotonic() - self.left_front < 1.0:
            self.hide_window()          # the bar icon only hides: agents running here go on
        else:
            self.window.present()
            desktop.focus_window(desktop.app_id())

    def open(self):
        self.fit()
        self.collect()
        self.refresh_overview()
        self.refresh_map()
        self.window.present()
        self.sync_provider_watch()
        self.start_gpu_poll()
        self.research_new_agents()
        self.check_new_apps()
        self.look_for_addons()
        self.run_health()
        self.launcher_entry_on_open()
        # Restarted after a language change: back on the settings page.
        view = os.environ.pop("AI_AGENTS_MAP_VIEW", "")
        if view in ai_core.VIEWS:
            self.show_view(view, save=False)
        page = os.environ.pop("AI_AGENTS_MAP_PAGE", "")
        if page == "settings":
            GLib.idle_add(self.open_settings)
        elif page == "reorganize":
            GLib.idle_add(self.open_reorganize)
        elif page.startswith("agent:"):
            agent = next((a for a in self.agent_list if a.command == page[6:]), None)
            if agent is not None:
                GLib.idle_add(lambda: self.open_agent_status(agent) and False)
        elif page.startswith("runtime:"):
            GLib.idle_add(lambda: self.open_runtime_status(page[8:]) and False)
        elif page == "health":
            GLib.idle_add(lambda: self.open_health() and False)
        elif page == "help":
            GLib.idle_add(lambda: self.open_help() and False)
        elif page == "addmcp":
            GLib.idle_add(lambda: self.open_add_mcp() and False)
        elif page == "addskills":
            GLib.idle_add(lambda: self.open_add_skills() and False)
        elif page.startswith("install:"):
            rt = next((r for r in self.inventory.runtimes if r.key == page[8:]), None)
            if rt is not None:
                GLib.idle_add(lambda: self.open_install(rt, "update" if rt.installed else "install") and False)
        elif page.startswith("models:"):
            rt = next((r for r in self.inventory.runtimes if r.key == page[7:] and r.installed), None)
            if rt is not None:
                GLib.idle_add(lambda: self.open_models(rt) and False)
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
        """Closing the window: while agents run here, ask first (stop them, or keep them running
        in the background); then hide."""
        running = [pane for pane in self.sessions.values() if pane.running]
        if running and not getattr(self, "close_asked", False):
            self.ask_close(running)
            return
        self.close_asked = False
        self.hide_window()

    def ask_close(self, running):
        names = ", ".join(pane.agent.name for pane in running)
        dialog = Adw.AlertDialog(heading=_("{0} is still running here").format(names),
                                 body=_("Stop it and close? Its local LLM is then stopped too and frees the graphics "
                                        "card's memory (when no other agent uses it). Or keep it running with the "
                                        "window closed."))
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("keep", _("Keep running"))
        dialog.add_response("stop", _("Stop and close"))
        dialog.set_response_appearance("stop", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("stop")
        dialog.set_close_response("cancel")

        def answered(_dialog, response):
            if response == "stop":
                for pane in running:
                    pane.stop()
            if response in ("stop", "keep"):
                self.close_asked = True
                self.close()
        dialog.connect("response", answered)
        dialog.present(self.window)

    def hide_window(self):
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
        elif self.page_name() == "install":
            self.close_install()    # a running install goes on in the background
        self.window.hide()
        busy = any(pane.running for pane in self.sessions.values()) or bool(self.jobs.active())
        if not desktop.stays_in_background() and not busy:
            self.quit()             # other desktops: closing quits, unless agents or downloads still run here


def main():
    ai_core.drop_agent_session_vars()       # started from an agent's terminal: nothing here inherits its session
    i18n.set_name("AI Agents Map", desktop.app_name())     # "Asterism" outside Omarchy
    app = AgentsMap()
    atexit.register(cleanup_pid)
    # Ending AI Agents Map ends the agents running in it too (and so frees their local LLMs).
    atexit.register(lambda: [pane.stop_now() for pane in getattr(app, "sessions", {}).values()])
    return app.run()


if __name__ == "__main__":
    sys.exit(main())
