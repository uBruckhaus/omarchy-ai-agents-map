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
from page_parts import PageParts
from models_page import ModelsPage
from connect_page import ConnectPage
from skills_page import SkillsPage
from mcp_pages import McpPages
from details_pages import DetailsPages
from chess_page import ChessPage
try:
    from addons_page import AddonsPage      # Asterism only: the Add-ons page (not in the Omarchy plugin)
except ImportError:
    class AddonsPage:                       # the plugin: no add-ons, no page
        pass
from ui_helpers import (MCP_GLYPHS, MCP_GROUP_GLYPHS, RUN_GLYPHS, SpaceBar, glyph_label,  # noqa: F401
                        TokenChart, monitor_height, pick_cards, short_gpu, short_number, size_text, when)

from window_rules import (AGENT_SPACING, CARD_WIDTH, DETAIL_LABEL_WIDTH, LIST_HEIGHT,  # noqa: F401
                          MODEL_LIST_WIDTH, RIGHT_PAGES, SCREEN_MARGIN, SCROLLER_MIN_HEIGHT,
                          SPLIT_WIDTH, WINDOW_SIZE)
import window_rules


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


class AgentsMap(PageParts, SettingsPages, ThemeCss, InstallDialog, SetupPage, AddonsPage, DetailsPages, ChessPage, McpPages, SkillsPage, ConnectPage, ModelsPage,
                Adw.Application):
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
                ai_core.migrate_settings()
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
        enabled = ai_core.setting("addons_enabled")
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
        a hint line, a card on Setup, "Suggested for this PC" on the Add-ons page."""
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
        size = ai_core.setting("window_size")
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
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP, hexpand=True, vexpand=True)
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
            # Our own header row instead of Adw.HeaderBar: a header bar centres its title widget, so the
            # icon and title would not line up with the page (AI SETUP) at the card's 17 px. The handle
            # drags and double-click maximizes like a title bar; the desktop's buttons sit at the right.
            bar = Gtk.WindowHandle()
            bar.add_css_class("am-headerbar")
            row = Gtk.Box(spacing=8)
            row.add_css_class("am-header-row")
            header.set_hexpand(True)
            row.append(header)
            row.append(Gtk.WindowControls(side=Gtk.PackType.END))
            bar.set_child(row)
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
        stack.add_named(self.build_chess_page(), "chess")         # a page of its own, as Settings
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
        # The app's icon before the title (the window buttons do not show it with every layout).
        display = Gdk.Display.get_default()
        if display is not None and Gtk.IconTheme.get_for_display(display).has_icon(desktop.app_id()):
            icon = Gtk.Image.new_from_icon_name(desktop.app_id())
            icon.set_pixel_size(22)
            icon.set_valign(Gtk.Align.CENTER)
            header.append(icon)
        title = Gtk.Label(label=_("My AI Agents Map"), xalign=0)
        title.add_css_class("am-title")
        title.set_ellipsize(Pango.EllipsizeMode.END)
        title.set_max_width_chars(30)
        title.set_width_chars(12)          # in a header bar beside the window buttons: not just "…"
        header.append(title)
        self.title_label = title
        # Too narrow for the map (a tiled window): a hint that also does it, centred in the room between
        # the title and the buttons (the room stays when the hint is hidden: the buttons keep right).
        hint_slot = Gtk.Box(hexpand=True)
        header.append(hint_slot)
        self.widen_hint = Gtk.Button(valign=Gtk.Align.CENTER, halign=Gtk.Align.CENTER, hexpand=True, visible=False)
        content = Gtk.Box(spacing=6)
        content.append(Gtk.Image.new_from_icon_name("view-fullscreen-symbolic"))
        content.append(Gtk.Label(label=_("Maximize for more room")))
        self.widen_hint.set_child(content)
        self.widen_hint.add_css_class("am-small-btn")
        self.widen_hint.add_css_class("am-widen-hint")
        self.widen_hint.set_tooltip_text(desktop.text("widen_tip"))
        self.widen_hint.connect("clicked", lambda *_: self.widen())
        hint_slot.append(self.widen_hint)
        # Setup and Map (setup_page.py: what is on the map is set up, updated and removed there):
        # both on every page, the one in view highlighted.
        self.view_buttons = {}
        for name, icon, label, tip in (
                ("setup", "view-grid-symbolic", _("Setup"),
                 _("Agents, LLMs, MCP servers, skills and models: add, update or remove them; the map shows "
                   "what is set up")),
                ("main", "mark-location-symbolic", _("Map"), _("Back to the map (Esc)"))):
            button = Gtk.ToggleButton(valign=Gtk.Align.CENTER)
            content = Gtk.Box(spacing=6)
            content.append(Gtk.Image.new_from_icon_name(icon))
            content.append(Gtk.Label(label=label))
            button.set_child(content)
            button.add_css_class("am-small-btn")
            button.add_css_class("am-view-btn")
            button.set_tooltip_text(tip)
            button.connect("toggled", self.on_view_button, name)
            header.append(button)
            self.view_buttons[name] = button
        # Open problems: shown only while something needs fixing (health.check), opens their list.
        self.problem_button = Gtk.Button(label="!", valign=Gtk.Align.CENTER, visible=False)
        self.problem_button.add_css_class("flat")
        self.problem_button.add_css_class("am-header-btn")
        self.problem_button.add_css_class("am-problem-btn")
        self.problem_button.connect("clicked", lambda *_: self.open_health())
        header.append(self.problem_button)
        # Chess: a game against the selected agent, where it plays one (else greyed); while a game
        # goes on (the page may be closed), back to it.
        self.chess_button = Gtk.Button(valign=Gtk.Align.CENTER, sensitive=False)
        knight = Gtk.Label(label="\U000F0858")
        knight.add_css_class("am-gear")
        self.chess_button.set_child(knight)
        self.chess_button.add_css_class("flat")
        self.chess_button.add_css_class("am-header-btn")
        self.chess_button.add_css_class("am-gear-btn")
        self.chess_button.connect("clicked", lambda *_: self.on_chess_button())
        header.append(self.chess_button)
        self.chess_mark_map()

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
        # The same gear (Nerd Font U+F0493, stored as the app's own icon) in a small bordered box as the
        # other plugins.
        settings_button = Gtk.Button()
        from ui_helpers import glyph_widget
        gear = glyph_widget("\U000F0493", "", 15, ("am-gear",))
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
                ("line:loaded", _("The provider has this model loaded in GPU memory; no agent uses it now")),
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
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP)
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
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP)
        # Only while the map is in view: the pages opened in its place have a header of their own.
        self.map_heading = self.view_heading(_("Map"), self.refresh_button())
        page.append(self.map_heading)
        # Downloads and installs running in the background (or interrupted): one line each.
        self.jobs_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.jobs_box.add_css_class("am-jobs")
        self.jobs_box.set_visible(False)
        page.append(self.jobs_box)
        self.map_empty = False
        # Above the map: an empty Asterism's welcome, and the hints (new on this PC, new versions…).
        self.empty_area = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, visible=False)
        page.append(self.empty_area)
        self.hint_area = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP_TIGHT, visible=False)
        self.hint_area.add_css_class("am-hint-area")      # shown on the problem page (build_health_page)
        self.health_report = None
        self.health_running = False
        body = Gtk.Box(spacing=0)
        # The map, the whole width (Setup has a card for everything on it).
        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP, hexpand=True)
        side.add_css_class("am-map-pane")
        self.gpu_line = self.build_gpu_line()
        # GPU memory and activity in a card that folds (open until folded; folded, its line keeps
        # the numbers: "1.5 of 24.0 GB in use · 18 %").
        self.gpu_card = self.group_card(side, "map-gpu", _("GPU"), default_open=True)
        self.gpu_card.append(self.gpu_line)
        self.gpu_line.set_visible(True)                    # the card shows or hides now
        self.gpu_card.group_card.set_visible(False)        # until the first reading has a card
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
        for text, tip, step in (("−", _("Zoom out (Ctrl+minus, Ctrl+scroll)"), -1),
                                ("100 %", _("Back to 100 % (Ctrl+0)"), 0),
                                ("+", _("Zoom in (Ctrl+plus, Ctrl+scroll)"), 1)):
            button = Gtk.Button(label=text)            # text, not icons: grey like the rest of the control
            button.add_css_class("flat")
            button.add_css_class("am-small-btn")
            button.add_css_class("am-quiet-btn")      # an option, not a must: grey, regular weight
            button.set_tooltip_text(tip)
            button.connect("clicked", lambda _b, d=step: self.zoom_map(d))
            zoom.append(button)
            if step == 0:
                self.zoom_label = button
        bar.append(zoom)
        side.append(bar)
        side.append(self.build_first_card())
        self.side_map = MapView()
        self.side_map.set_layers(ai_core.map_layers())
        self.side_map.set_animate(ai_core.map_animations())
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
        self.side_map.on_stop = lambda node_id: (self.ask_stop_runtime(node_id.partition(":")[2])
                                                 if node_id.startswith("runtime:") else
                                                 self.stop_agent(node_id.partition(":")[2], end_game=True))
        self.side_map.on_chess = lambda node_id: self.open_chess(
            next(a for a in self.agent_list if a.command == node_id.partition(":")[2]))
        self.side_map.chess_tip = _("Back to the chess game")
        self.side_map.start_tips = (_("Start here"), _("Show terminal"), _("Stop"),
                                    _("Stop it or only free the GPU memory…"))
        map_scroller = self.page_scroller(self.side_map.with_flow_layer(), bar_room=False)   # the lights on running agents' paths on top
        map_scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)   # zoomed in: sideways too
        side.append(map_scroller)
        self.side_map.set_zoom(ai_core.setting("map_zoom")
                               if isinstance(ai_core.setting("map_zoom"), (int, float)) else 1.0)
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
        self.auto_layers()
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
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP_TIGHT)
        box.set_margin_bottom(ai_core.GAP)
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
                                       and ai_core.setting("terminal_tabs") is not False)

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
        line.set_margin_bottom(0)

        def block(title):
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP_TIGHT, hexpand=True)
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
            try:
                stamp = (inventory.models_stamp(runtimes), agents.install_stamp())
            except Exception:
                stamp = None
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
            GLib.idle_add(done, usage, running, working, outside, stamp)

        def done(usage, running, working, outside, stamp):
            self.gpu_reading = False
            self.side_map.set_working(working)
            self.side_map.set_outside({rid: {mid: _("in use · {0}").format(names) for mid, names in models.items()}
                                       for rid, models in outside.items()})
            if working != self.working_agents:
                self.working_agents = set(working)
                self.update_terminal_tabs()
            if usage is not None and self.window.is_visible():
                self.show_gpu(usage, runtimes)
            # A model or an agent came or went (also outside the app: Omarchy's menu, mise, pacman), or
            # LM Studio rewrote its model index: read them again, and check the setup for the header's "!".
            known = getattr(self, "models_stamp", None)
            if stamp is not None and known is not None and stamp != known and runtimes is self.inventory.runtimes \
                    and self.window.is_visible() and self.side_map.link is None and self.page_name() == "main":
                self.collect()
                self.refresh_overview()
                self.refresh_map()
                self.run_health()
            if running != self.running and self.window.is_visible():
                # An agent was opened or closed: its dot, its model and its card change.
                self.running = running
                self.refresh_overview()
                self.refresh_map()
                self.auto_layers()           # narrow window: agents and providers while one runs
            return False

        threading.Thread(target=work, daemon=True).start()
        return True

    def show_gpu(self, usage, runtimes):
        gib = gpu_usage.gib
        total = usage.vram_total
        self.gpu_card.group_card.set_visible(total > 0)
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
        summary = "  ·  ".join(x for x in (self.gpu_memory_text.get_label() if total else "",
                                           self.gpu_activity_text.get_label()) if x)
        self.gpu_card.group_summary.set_label(summary)
        extra = [f"{round(usage.temp)} °C" if usage.temp else "", f"{round(usage.power)} W" if usage.power else ""]
        self.gpu_activity_side.set_label("  ·  ".join(e for e in extra if e))
        self.side_map.set_usage(usage)
        # Running local LLMs: the GPU memory still free as a battery (on the map's box and on Setup's card).
        free = max(0, usage.vram_total - usage.vram_used)
        self.gpu_batteries = {f"runtime:{key}": level for key, use in usage.runtimes.items() if use.running
                              for level in [agent_details.gpu_battery(free, usage.vram_total, use.vram)] if level}
        self.show_batteries()
        # The watcher (or something else) started or stopped a local LLM: show it.
        changed = any(rt.running != usage.runtimes[rt.key].running
                      or (rt.running and rt.server_off == usage.runtimes[rt.key].serving)
                      for rt in runtimes if rt.key in usage.runtimes)
        if changed and runtimes is self.inventory.runtimes and self.side_map.link is None \
                and self.page_name() == "main":
            self.collect()
            self.refresh_overview()
            self.refresh_map()

    def collect(self):
        """Everything both views show (file system and /proc only, ~0.1 s)."""
        self.agent_list = agents.list_agents()
        self.inventory = inventory.collect(self.agent_list)
        try:
            self.models_stamp = (inventory.models_stamp(self.inventory.runtimes), agents.install_stamp())
        except Exception:
            self.models_stamp = None
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
        wanted = bool(ai_core.setting("providers_with_agents")) and \
            any(rt.installed and rt.agents and (rt.service or rt.key == "lmstudio") for rt in self.inventory.runtimes)
        threading.Thread(target=provider_watch.ensure_service, args=(wanted,), daemon=True).start()

    def refresh_overview(self):
        """An empty Asterism's welcome above the map, and the hints (new on this PC, new versions,
        updates) on the problem page, counted by the header's !."""
        while (child := self.hint_area.get_first_child()) is not None:
            self.hint_area.remove(child)
        self.hint_area.set_visible(False)
        self.hint_buttons = Gtk.SizeGroup(mode=Gtk.SizeGroupMode.HORIZONTAL)   # one width for the hints' buttons
        self.map_empty = not harnesses.all_harnesses() and not providers.all_providers() \
            and hasattr(self, "adapter_addons") and bool(self.adapter_addons())
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
        self.update_problem_button()            # problems and hints: the header's !
        if getattr(self, "stack", None) is not None and self.page_name() == "setup":
            GLib.idle_add(self.refresh_setup)       # Setup's cards follow too (a removal, an update)

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
        # Agents and local LLMs on this PC that are not on the map: their add-on is off (Setup
        # switches it on) or not installed yet (the Add-ons page gets it). One line each.
        for state, opener, button in (("off", self.open_my_setup, _("Setup")),
                                      ("new", lambda: self.open_addons(), _("Get add-ons"))):
            waiting = [entry.name for entry, s in getattr(self, "addon_suggestions", []) if s == state]
            if waiting:
                self.hint_line(ngettext("{0} is on this PC but not on the map", "{1} are on this PC but not on the map",
                                        len(waiting)).format(listed(waiting, 1), listed(waiting, 3)),
                               [(button, opener)])
        versions = getattr(self, "new_versions", [])
        if versions:
            self.hint_line(_("New version found: {0} · Add MCP server › Look inside… to use it").format(
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
        if not ai_core.setting("look_up_new_agents"):
            return
        for agent in self.agent_list:
            if agent.installed and self.is_new_agent(agent.command) and agent_profiles.needs_research(agent.command):
                self.start_research(agent)

    def start_research(self, agent):
        command = agent.command
        if command in self.researching:
            return
        self.researching.add(command)
        self.refresh_overview()                      # its card says it is being looked up
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
            (_("Status"), {"running": _("running"), "noserver": _("open, server off"), "stopped": _("installed, not running")}
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

    def stop_agent(self, command: str, end_game: bool = False):
        """End the agent running here; from its card's stop button also its chess game."""
        pane = self.sessions.get(command)
        if pane is not None:
            pane.stop()
        if end_game and self.chess_agent is not None and self.chess_agent.command == command:
            self.end_chess()

    def ask_start_outside(self, agent, path: str, folder: str, anchor):
        """No vte4: ask whether to fix that (its details page shows how) or to start the
        agent in a separate terminal this time."""
        popover = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP_TIGHT)
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
        saved = ai_core.setting("agent_folder")
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

    # --- new apps: programs with an MCP server, skill sets ----------------------------------------

    def new_found(self) -> tuple[list, list]:
        """(MCP programs, skill sets) found and not set up, that the add pages have not shown yet."""
        seen = ai_core.setting("seen_found")
        seen_mcp = set(seen.get("mcp", [])) if isinstance(seen, dict) else set()
        seen_skills = set(seen.get("skills", [])) if isinstance(seen, dict) else set()
        updates = self.mcp_updates()
        mcp = [c for c in self.found_mcp or [] if (not self.installed_mcp(c) or c in updates)
               and c.program not in seen_mcp]
        skills = [s for s in self.found_skills or [] if s.path not in seen_skills]
        return mcp, skills

    def mark_seen(self, kind: str):
        seen = ai_core.setting("seen_found")
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

    # --- the first connection: a card above the map until one is drawn ----------------------------

    def build_first_card(self) -> Gtk.Widget:
        """For someone new: how the first line is drawn, in three steps, with the Help page's animation
        on Watch how. Gone for good after the first connection (first_connection_made) or ×."""
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, visible=False)
        card.add_css_class("am-help-card")
        card.add_css_class("am-first-card")
        top = Gtk.Box(spacing=10)
        heading = Gtk.Label(label=_("Your first connection").upper(), xalign=0, hexpand=True, valign=Gtk.Align.CENTER)
        heading.add_css_class("am-setting-heading")
        top.append(heading)
        watch = Gtk.Button()
        content = Gtk.Box(spacing=6)
        content.append(Gtk.Image.new_from_icon_name("media-playback-start-symbolic"))
        content.append(Gtk.Label(label=_("Watch how")))
        watch.set_child(content)
        watch.add_css_class("am-small-btn")
        watch.add_css_class("am-control-btn")
        top.append(watch)
        close = Gtk.Button(icon_name="window-close-symbolic", valign=Gtk.Align.CENTER)
        close.add_css_class("flat")
        close.set_tooltip_text(_("Hide"))
        close.connect("clicked", lambda *_: self.first_connection_made())
        top.append(close)
        card.append(top)
        steps = [(_("Find an agent"), _("The boxes in the Agents column are the coding agents on this PC.")),
                 (_("Draw a line"), _("Press on an agent's box and drag to a local LLM, an MCP server or a skill "
                                      "folder, then let go.")),
                 (_("Check and confirm"), _("A page shows exactly what changes in the agent's settings. Connect "
                                            "writes it; Undo takes it back."))]
        row = Gtk.Box(spacing=24, homogeneous=True)
        for number, (title, text) in enumerate(steps, 1):
            step = Gtk.Box(spacing=10)
            badge = Gtk.Label(label=str(number), valign=Gtk.Align.START)
            badge.add_css_class("am-step-number")
            step.append(badge)
            words = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
            head = Gtk.Label(label=title, xalign=0, wrap=True, max_width_chars=1)
            head.add_css_class("am-step-title")
            words.append(head)
            line = Gtk.Label(label=text, xalign=0, wrap=True, max_width_chars=1)
            line.add_css_class("am-step-text")
            words.append(line)
            step.append(words)
            row.append(step)
        card.append(row)
        revealer = Gtk.Revealer(transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN)
        card.append(revealer)

        def play(*_args):
            if revealer.get_child() is None:          # made on the first Watch how only
                revealer.set_child(self.drawing_sketch())
            revealer.set_reveal_child(True)
            revealer.get_child().start()
        watch.connect("clicked", play)
        self.first_card = card
        return card

    def update_first_card(self):
        """Shown while no line was ever drawn, nothing is connected yet and the map has an agent."""
        card = getattr(self, "first_card", None)
        if card is None:
            return
        graph_now = getattr(self.side_map, "graph", None)
        if graph_now is None or ai_core.setting("first_connection_done"):
            card.set_visible(False)
            return
        # Someone with an agent already connected to a local LLM or an MCP server needs no first steps
        # (skill folders do not count: many agents read a default one by themselves).
        connected = any(e.source.startswith("agent:") and (e.target.startswith("runtime:") or e.kind == "mcp")
                        for e in graph_now.edges)
        if connected:
            self.first_connection_made()
            return
        card.set_visible(any(n.kind == "agent" for n in graph_now.nodes))

    def first_connection_made(self):
        """The first line was drawn (or the card closed): the card goes, for good."""
        if not ai_core.setting("first_connection_done"):
            self.save_setting(first_connection_done=True)
        self.update_first_card()

    # --- the map: zoom, Show switches, selection ---------------------------------------------------------

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
        # Why the switches changed by themselves (auto_layers: the narrow window picks its own).
        self.layers_note = Gtk.Label(xalign=0, hexpand=True, visible=False)
        self.layers_note.add_css_class("am-line-label")
        self.layers_note.set_ellipsize(Pango.EllipsizeMode.END)
        self.layers_note.set_margin_start(6)
        row.append(self.layers_note)
        return row

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
        if not self.narrow_window():     # the narrow window chooses its own (auto_layers)
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
        self.chess_mark_map()

    def show_batteries(self):
        """Online agents' limits and running local LLMs' free GPU memory, on the map and Setup's cards."""
        self.batteries = {**getattr(self, "agent_batteries", {}), **getattr(self, "gpu_batteries", {})}
        self.side_map.set_batteries(self.batteries)

    def refresh_map(self):
        details = {a.command: agent_details.details_for(a.command) for a in self.agent_list if a.installed}
        # Online agents' limits (a session, a week…): a battery on the map's box and on Setup's card.
        self.agent_batteries = {}
        for command, info in details.items():
            level = agent_details.battery(info.limits)
            if level is not None:
                self.agent_batteries[f"agent:{command}"] = (*level, agent_details.battery_tip(info.limits))
        self.show_batteries()
        labels = {"skills": lambda n: ngettext("1 skill", "{0} skills", n).format(n),
                  "more": lambda n: _("+{0} more").format(n),
                  "running": lambda n: _("running") if n == 1 else _("{0} running").format(n),
                  "in_use": lambda names: _("in use · {0}").format(names),
                  "server_off": _("server off")}
        # Only what is set up: adding, updating and removing happen on Setup.
        built = graph.build(self.agent_list, details, self.inventory, labels, link_hints.load(), self.running)
        headers = [_("MCP servers · skills"), _("Agents"), _("LLM providers"), _("Models")]
        for view in (self.side_map,):
            view.group_titles = {"mcp": _("MCP servers"), "skills": _("Skills"), "online": _("Online")}
            view.set_graph(built, headers)
            view.set_layers(self.map_view.layers)
        ids = {n.id for n in built.nodes}
        self.set_selection([i for i in self.selection if i in ids])
        self.update_first_card()

    # --- views -----------------------------------------------------------------

    def on_view_button(self, button, name: str):
        """Map or Setup, from any page (a click on the one in view keeps it in view)."""
        if self.updating_buttons:
            return
        if name == "setup":
            self.open_my_setup()
        elif self.page_name() == "connect":
            self.close_connect()          # also drops the line being drawn
        elif self.page_name() != "main":
            self.close_setup()
        self.update_header()

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
        # Every header button (Map, Setup, the legend, add-ons, Help, Settings) stays visible and
        # usable on every page; Map or Setup is highlighted while it is in view.
        if getattr(self, "map_heading", None) is not None:
            self.map_heading.set_visible(page == "main")
        self.updating_buttons = True
        for name, button in self.view_buttons.items():
            button.set_active(name == page)
        self.updating_buttons = False

    # --- health check: what works, what to fix, what this setup is good for --------------------------

    def build_health_page(self) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP)
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
        title = Gtk.Label(label=_("Open problems").upper(), xalign=0, hexpand=True)   # and updates, suggestions
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
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP)
        self.health_body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP)
        self.health_body.add_css_class("am-settings")
        content.append(self.health_body)
        # Below the problems: what is new (on this PC, versions), each with its button (overview_hints).
        self.hints_heading = Gtk.Label(label=_("Updates and suggestions").upper(), xalign=0, visible=False)
        self.hints_heading.add_css_class("am-setting-heading")
        content.append(self.hints_heading)
        content.append(self.hint_area)
        page.append(self.page_scroller(content))
        return page

    def update_problem_button(self):
        """The header's "!" shows while something needs fixing, never on an empty map (a clean
        Asterism with nothing switched on yet: there is nothing to check)."""
        report = self.health_report
        problems = report.warn if report is not None and not self.map_empty else 0
        hints = 0
        child = self.hint_area.get_first_child() if getattr(self, "hint_area", None) is not None else None
        while child is not None:
            hints += 1
            child = child.get_next_sibling()
        self.hints_heading.set_visible(hints > 0)
        self.problem_button.set_visible(problems > 0 or hints > 0)
        # Red for something to fix; the accent colour when there are only updates and suggestions.
        (self.problem_button.add_css_class if problems else self.problem_button.remove_css_class)("am-problem-open")
        parts = ([ngettext("1 open problem", "{0} open problems", problems).format(problems)] if problems else []) + \
                ([ngettext("1 suggestion", "{0} suggestions", hints).format(hints)] if hints else [])
        if parts:
            self.problem_button.set_tooltip_text(" · ".join(parts) + " — " + _("Open"))

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
            self.health_again = True        # the setup changed meanwhile: check once more when this one ends
            return
        self.health_running = True
        self.health_again = False
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
            self.update_problem_button()
            if getattr(self, "health_again", False):
                GLib.idle_add(lambda: self.run_health() and False)
            if self.page_name() == "setup":
                self.refill_setup_health()
            if self.page_name() == "health":
                self.fill_health(report)
            return False

        threading.Thread(target=work, daemon=True).start()

    def fill_health(self, report):
        """Open problems only (what needs fixing), each with the way to where it is fixed; tips and
        what works are on the cards themselves now."""
        body = self.health_body
        while (child := body.get_first_child()) is not None:
            body.remove(child)
        problems = [f for area in report.areas for f in area.findings if f.level == "warn"]
        if not problems:
            body.append(self.status_hero("ok", _("Nothing to fix."), _("Everything set up and working")))
            return
        body.append(self.status_hero("missing", ngettext("1 open problem", "{0} open problems",
                                                         len(problems)).format(len(problems)),
                                     _("Each one opens where it is fixed.")))
        listbox = self.boxed_list()
        for finding in problems:
            listbox.append(self.finding_list_row(finding))
        body.append(listbox)

    def section_in(self, body, title: str):
        self.settings_heading(body, title)

    def use_tile(self, use) -> Gtk.Widget:
        tile = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP_TIGHT)
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
                "setup": _("Setup"), "link": _("Map"),
                "addmcp": _("Add MCP server"), "addskills": _("Add skills"), "reorganize": _("Tidy up…")}.get(kind, "")

    def health_action(self, action):
        kind = action[0]
        if kind == "setup":
            self.open_my_setup()
            return
        if kind == "link":                       # the map, its broken line lit (both ends selected)
            self.close_setup()
            self.set_selection([node for node in action[1:] if node])
            return
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
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP)
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
        self.install_form = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP)
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

    # --- right-click menus of a local LLM and an agent ---------------------------------------------------

    def runtime_menu(self, rt, popover) -> tuple[str, list]:
        """A local LLM: install, update, models, start, stop, free GPU memory, start at login."""
        state = {"running": _("running"), "noserver": _("open, server off"), "stopped": _("installed, not running")}.get(graph.runtime_status(rt),
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
            if rt.installed and install_recipes.choose(adapter.install, "update", "package") is not None:
                items.append(self.menu_item(_("Update…"), popover, lambda: self.offer_install("runtime", rt.key, "update")))
        if not runtime_ctl.can_control(rt):
            if rt.installed or inst is None:
                items.append(self.menu_note(_("Not installed.") if not rt.installed else
                                            _("No user service starts it, so it cannot be started from here.")))
            return f"{rt.name} · {state}", items
        if rt.running and self.runtime_users(rt.key):
            items.insert(0, self.menu_note(_("In use by {0}").format(self.runtime_users(rt.key))))
        if rt.server_off:
            items.append(self.menu_item(_("Start server"), popover,
                                        lambda: self.control_runtime(rt, runtime_ctl.start, _("Start server")),
                                        _("The app is open, but agents reach it only through its server (lms server start).")))
        elif rt.running:
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

    def ask_stop_runtime(self, key: str):
        """The stop button of a local LLM holding GPU memory: stop it, or only unload its models."""
        rt = next((r for r in self.inventory.runtimes if r.key == key), None)
        if rt is None:
            return
        users = self.runtime_users(rt.key)
        lms = rt.key == "lmstudio"
        body = _("{0}: {1}").format(_("Free GPU memory"),
                                    _("Unloads its models; it keeps running and loads one again when asked."))
        if runtime_ctl.can_control(rt):
            body += " " + (_("Stop server ends its server; the app stays open with its models loaded.") if lms
                           else _("Stop ends it; agents using it cannot reach it until it is started again."))
        if users:
            body += "\n\n" + _("In use by {0}").format(users)
        dialog = Adw.AlertDialog(heading=_("Stop {0}?").format(rt.name), body=body)
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("free", _("Free GPU memory"))
        if runtime_ctl.can_control(rt):
            dialog.add_response("stop", _("Stop server") if lms else _("Stop"))
            dialog.set_response_appearance("stop", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("free")
        dialog.set_close_response("cancel")

        def answered(_dialog, response):
            if response == "free":
                self.control_runtime(rt, runtime_ctl.unload, _("Free GPU memory"))
            elif response == "stop":
                self.control_runtime(rt, runtime_ctl.stop, _("Stop server") if lms else _("Stop"))
        dialog.connect("response", answered)
        dialog.present(self.window)

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
            status = {"running": _("running"), "noserver": _("open, server off"), "stopped": _("installed, not running")}.get(
                graph.runtime_status(next(r for r in self.inventory.runtimes if r.key == rt.key)), "")
            self.show_note(_("{0}: {1}").format(rt.name, status) if ok else
                           _("{0}: {1} failed ({2})").format(rt.name, label, message))
            return False

        threading.Thread(target=work, daemon=True).start()

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
        Details and what removes it, each group set off by a line. Setup's cards use it too."""
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
                    # How it is updated comes from the same ways (mise, npm, pipx, uv, its own updater).
                    if agent.installed and install_recipes.choose(adapter.install, "update", agent.source) is not None:
                        own = list(own) + [self.menu_item(_("Update…"), popover,
                                                          lambda: self.offer_install("agent", ident, "update"))]
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

    # --- size ----------------------------------------------------------------------

    def launcher_entry_on_open(self):
        """The launcher entry is opt-in: asked once, then kept as the setting says
        (only on a desktop where the app offers its own entry)."""
        if not desktop.launcher_entry():
            return
        wanted = ai_core.setting("launcher_entry")
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
        self.auto_layers()
        return False

    auto_layers_key = None       # the situation the map's layers were last chosen for
    wide_layers = None           # the full-size window's layers, kept while the window is narrow

    def real_width(self) -> int:
        """The width the window really got (0 before its first allocation, when split is still the
        monitor's guess)."""
        return getattr(self.window, "last_width", 0) if self.window is not None else 0

    def narrow_window(self) -> bool:
        return window_rules.narrow(self.real_width())

    def show_layers_note(self, key):
        """A short line beside the Show switches while the narrow window chooses them."""
        note = getattr(self, "layers_note", None)
        if note is None:
            return
        if key[0] != "narrow":
            note.set_visible(False)
            return
        text = (_("Narrow window: agents and providers while an agent runs") if key[1] else
                _("Narrow window: providers and models; agents show while one runs"))
        note.set_label(text)
        note.set_tooltip_text(text + "\n" + _("Maximize the window for your own choice."))
        note.set_visible(True)

    def auto_layers(self):
        """The map's categories follow the window (window_rules.layers_for). Chosen again only when
        the situation changes, so a Show switch clicked in between stays until then; in the narrow
        window it is not saved."""
        if getattr(self, "side_map", None) is None:
            return
        key = window_rules.layer_situation(self.real_width(), bool(getattr(self, "running", None)))
        if key is None or key == self.auto_layers_key:
            return
        if self.auto_layers_key == ("wide",):
            self.wide_layers = set(self.map_view.layers)      # back to it when wide again
        self.auto_layers_key = key
        self.show_layers_note(key)
        layers = window_rules.layers_for(key, self.wide_layers, ai_core.map_layers())
        if layers != set(self.map_view.layers):
            self.side_map.set_layers(layers)
            self.set_selection([i for i in self.selection if i in set(self.map_view.selected)])
            self.update_layer_toggles()

    def widen_mode(self) -> str:
        mode = ai_core.setting("widen")
        return mode if mode in previews.WIDEN_MODES else "workspace"

    def set_widen_mode(self, mode: str):
        self.save_setting(widen=mode)
        self.update_widen_hint()

    def widen(self):
        work = desktop.maximize if self.widen_mode() == "maximize" else desktop.widen
        threading.Thread(target=work, args=(desktop.app_id(),), daemon=True).start()

    def update_widen_hint(self):
        """Shown while the map does not fit, unless the window is maximized already
        (then the screen itself is too narrow and maximizing cannot help): window_rules.widen_hint."""
        geo = self.monitor_geometry() if self.window is not None else None
        width, mode = self.real_width(), self.widen_mode()
        if not window_rules.widen_hint(self.split, mode, None, width, None):
            self.widen_hint.set_visible(False)
            return

        def work():
            state = desktop.is_maximized(desktop.app_id())
            GLib.idle_add(lambda: self.widen_hint.set_visible(window_rules.widen_hint(
                self.split, mode, state, width, geo.width if geo is not None else None)) and False)

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
        if keyval == Gdk.KEY_F5 and (page == "main" or page == "setup" and not self.setup_welcome):
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
            elif page == "chess":
                self.close_chess()
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
        if page in ("settings", "connect", "install", "mcp", "status", "health", "chess"):
            if keyval == Gdk.KEY_BackSpace:
                {"settings": self.close_settings, "connect": self.close_connect,
                 "install": self.close_install, "mcp": self.close_mcp_details,
                 "status": self.close_status, "health": self.close_health, "chess": self.close_chess}[page]()
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
        if width >= 400 and height >= 400 and [width, height] != ai_core.setting("window_size"):
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
