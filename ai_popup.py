#!/usr/bin/env python3
"""AI Agents Map popup: a Wayland layer-shell card that shows the AI setup of
this computer: agents, local LLMs and their models, MCP servers and skills.
Two views (the overview as cards, and a Map drawn at full size) and a
settings page.

A singleton process woken by SIGUSR1 from ai-manager-toggle (as Theme
Manager and App Manager). The card always fills the screen: cards on the
left, the map beside them; it is neither moved nor resized.
"""

import colorsys
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
        window.connect("notify::is-active", self.on_active_changed)

        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        card.add_css_class("am-card")
        frame = Gtk.Overlay()
        frame.set_child(card)
        frame.set_halign(Gtk.Align.CENTER)
        frame.set_valign(Gtk.Align.CENTER)
        overlay.add_overlay(frame)
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

        runtimes = [rt for rt in inv.runtimes if rt.installed]
        self.section(_("Local LLMs ({0})").format(len(runtimes)))
        if not runtimes:
            self.section_hint(_("No local LLM runtime installed (llama.cpp, Ollama, LM Studio, vLLM)."))
        for rt in runtimes:
            state = _("running") if rt.running else _("stopped")
            sub = " · ".join([state, ngettext("1 model", "{0} models", len(rt.models)).format(len(rt.models))])
            box.append(self.expandable_card(f"runtime:{rt.key}", "\U000F061A", "", rt.name, sub,
                                 lambda b, rt=rt: self.fill_runtime(b, rt, names), dim=not rt.running))

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
             dim: bool = False, badge: str = "", badge_tip: str = "", highlight: bool = False) -> Gtk.Widget:
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
            (_("Status"), _("running") if rt.running else _("stopped"), ""),
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
        for view in (self.map_view, self.side_map):
            view.set_graph(built, headers)
            view.set_layers(self.map_view.layers)
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
        on_settings = page == "settings"
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
        .am-empty-icon {{ color: {muted_hex}; opacity: 0.6; }}
        .am-empty-title {{ font-size: 14px; font-weight: 600; }}
        .am-group-heading {{ font-size: 10px; font-weight: 700; letter-spacing: 1px; color: {dim_hex};
            margin-top: 4px; }}
        .am-agent {{ background: alpha({fg_hex}, 0.05); border: 1px solid alpha({fg_hex}, 0.10);
            border-radius: {radius}px; padding: 10px 12px; }}
        .am-agent-missing {{ opacity: 0.5; }}
        .am-agent.am-agent-selected {{ background: alpha({accent_hex}, 0.18); border: 2px solid {accent_hex}; }}
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
            elif page in ("map", "main") and self.selection:
                self.set_selection([])   # the first Esc clears the selection
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
        view = os.environ.pop("AI_MANAGER_VIEW", "")
        if view in ai_core.VIEWS:
            self.show_view(view, save=False)
        if os.environ.pop("AI_MANAGER_PAGE", "") == "settings":
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
        self.window.hide()


def main():
    app = AiManager()
    atexit.register(cleanup_pid)
    return app.run()


if __name__ == "__main__":
    sys.exit(main())
