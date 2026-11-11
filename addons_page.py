"""The Add-ons page (a mixin of ai_popup.AgentsMap): the signed add-ons, as in Maprisma.

One place for everything the app understands: the agents and providers built in, the add-ons
installed (on, off, refused), the ones in the signed catalog, and the ones suggested because their
harness is on this PC. Cards with search and filters; a details view per add-on with what it reads,
sends and may do; install, switch on/off, update, remove, install from a file. Every change goes
through addon_store (signature and hashes first) and reload_addons (no restart).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path

from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk

import addon_api
import addon_store
import addons
import ai_core
import desktop
import harnesses
import install_recipes
import providers
import setup_page
from i18n import _

KIND_ICONS = {"agent": "system-users-symbolic", "provider": "network-server-symbolic",
              "feature": "application-x-addon-symbolic", "extension": "list-add-symbolic"}
FILTERS = ("all", "agent", "provider", "feature", "installed", "updates")


@dataclass
class Item:
    id: str
    name: str
    kind: str                         # agent | provider | feature | extension
    state: str                        # builtin | on | off | failed | available | suggested
    summary: str = ""
    description: str = ""
    version: str = ""
    icon: str = ""                    # icon name, or a glyph with glyph_font
    glyph: str = ""
    glyph_font: str = ""
    privacy: int = 1
    capabilities: list = field(default_factory=list)
    reads: list = field(default_factory=list)
    sends: list = field(default_factory=list)
    reason: str = ""
    entry: addon_store.Entry | None = None
    addon: addons.Addon | None = None
    update: bool = False
    installed: bool | None = None     # built-in agents and local LLMs: is the program itself here
    target: str = ""                  # "agent" | "runtime": what install_dialog installs or removes
    homepage: str = ""                # the web page of the agent, provider or add-on
    key: str = ""                     # the agent command or provider key install_dialog gets (else id)
    here: bool = False                # an add-on (off) whose program is on this PC: suggested
    words: str = ""                   # what search looks through
    extends: list = field(default_factory=list)   # an extension: the agent commands it adds to


def privacy_name(level: int) -> str:
    return {1: _("Numbers only"), 2: _("Session titles"), 3: _("Titles and a summary by Claude")}.get(level, "")


def capability_text(capability: str) -> str:
    return {"agent": _("Adds an agent to the map and the cards"),
            "provider": _("Adds an LLM provider to the map"),
            "installer": _("Can install a program (from a download checked against its release's SHA-256)")
            }.get(capability, capability)


def harness_summary(h) -> str:
    parts = [part for part, has in ((_("sessions"), h.sessions), (_("security"), h.security),
                                    (_("MCP servers"), h.mcp), (_("skills"), h.skills),
                                    (_("local LLMs"), h.local_llm), (_("updates"), h.self_update))
             if has]
    return " · ".join(parts) if parts else _("Shown on the map")


def build_items(installed: list, entries: list, agent_list=(), runtimes=(), glyphs=None) -> list:
    """Built-ins, installed add-ons and catalog entries as one list of cards; glyphs: add-on id ->
    (Nerd Font glyph, font) of the agent or provider it brings."""
    glyphs = glyphs or {}
    agent_on = {a.command: a.installed for a in agent_list}
    runtime_on = {r.key: r.installed for r in runtimes}
    items = []
    by_id = {e.id: e for e in entries}
    for addon in installed:
        entry = by_id.pop(addon.id, None)
        if addon.state in ("refused", "failed"):
            state = "failed"
        else:
            state = "on" if addon.state == "loaded" else "off"
        kind = addon.kind if addon.kind != "feature" else entry.kind if entry else "feature"
        item = Item(addon.id, addon.name or addon.id, kind, state,
                    (entry.summary if entry else "") or addon.summary,
                    (entry.description if entry else "") or addon.description, addon.version,
                    (entry.icon if entry else "") or addon.icon or KIND_ICONS[kind], privacy=addon.privacy or 1,
                    capabilities=list(addon.capabilities), reads=list(addon.reads), sends=list(addon.sends),
                    extends=list(getattr(addon, "extends", [])),
                    reason=addon.reason, entry=entry, addon=addon,
                    update=bool(entry and addon.version and addon_store.newer(entry.version, addon.version)),
                    homepage=(entry.homepage if entry else "") or addon.homepage)
        if entry and item.kind == "feature" and "agent" in addon.capabilities:
            item.kind = "agent"
        if addon.id in glyphs:
            item.glyph, item.glyph_font = glyphs[addon.id]
        if addon.kind in ("agent", "provider") and len(addon.provides) == 1:
            # An adapter: the program it brings, installed and removed from its card once it is on.
            key = item.key = addon.provides[0]
            if addon.kind == "agent" or addon.commands or "installer" in addon.capabilities:     # a program
                known = agent_on if addon.kind == "agent" else runtime_on
                item.installed = known[key] if key in known else addon_store.present(addon)
                item.target = ("agent" if addon.kind == "agent" else "runtime") if state == "on" else ""
                item.here = state == "off" and bool(item.installed)
        items.append(item)
    for entry in by_id.values():
        items.append(Item(entry.id, entry.name, entry.kind, "suggested" if addon_store.present(entry) else "available",
                          entry.summary, entry.description, entry.version, entry.icon or KIND_ICONS[entry.kind],
                          privacy=entry.privacy, capabilities=list(entry.capabilities), reads=list(entry.reads),
                          sends=list(entry.sends), entry=entry, homepage=entry.homepage))
    for h in harnesses._built_in():
        items.append(Item(h.command, h.full_name or h.name, "agent", "builtin", harness_summary(h),
                          glyph=h.icon, glyph_font=h.icon_font, installed=agent_on.get(h.command, False),
                          target="agent" if h.install else "", homepage=h.homepage))
    for p in providers._built_in_local():
        items.append(Item(p.key, p.name, "provider", "builtin", _("Local LLM: found, installed, started and stopped "
                                                                  "here, its models on the map"),
                          icon=KIND_ICONS["provider"], installed=runtime_on.get(p.key, False),
                          target="runtime" if p.install else "", homepage=p.homepage))
    for p in providers._built_in_online():
        items.append(Item(p.key, p.name, "provider", "builtin", _("Online provider"), icon="network-workgroup-symbolic"))
    for item in items:
        item.words = " ".join([item.id, item.name, item.summary, item.description] + item.reads).lower()
    return items


def visible(item: Item, flt: str, query: str) -> bool:
    if query and query.lower() not in item.words:
        return False
    if flt == "installed":
        return item.state in ("on", "off", "failed")
    if flt == "updates":
        return item.update
    return flt == "all" or item.kind == flt or flt == "feature" and item.kind == "extension"


class AddonsPage:
    """The page; needs AgentsMap's helpers (page_scroller, boxed_list, plain_row, note, toast, …)."""

    def build_addons_page(self) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP)
        header = Gtk.Box(spacing=8)
        back = self.back_button(lambda: self.close_addons(), _("Back (Backspace, Esc)"))
        header.append(back)
        title = Gtk.Label(label=_("Add-ons").upper(), xalign=0)
        title.add_css_class("am-section-title")
        header.append(title)
        self.addons_search = Gtk.SearchEntry(placeholder_text=_("Search add-ons, agents, providers…"),
                                             hexpand=True, max_width_chars=40, halign=Gtk.Align.END)
        self.addons_search.connect("search-changed", lambda *_a: self.fill_addons_list())
        header.append(self.addons_search)
        from_file = Gtk.Button(label=_("Install from file…"))
        from_file.add_css_class("am-small-btn")
        from_file.connect("clicked", lambda *_a: self.addon_from_file())
        header.append(from_file)
        page.append(header)
        rule = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        rule.add_css_class("am-settings-rule")
        page.append(rule)
        chips = Gtk.Box(spacing=6)
        self.addons_filter = "all"
        self.addons_chips = {}
        first = None
        labels = {"all": _("All"), "agent": _("Agents"), "provider": _("Providers"), "feature": _("Features"),
                  "installed": _("Installed"), "updates": _("Updates")}
        for key in FILTERS:
            chip = Gtk.ToggleButton(label=labels[key])
            chip.add_css_class("am-addon-chip")
            if first is None:
                first = chip
                chip.set_active(True)
            else:
                chip.set_group(first)
            chip.connect("toggled", lambda button, k=key: button.get_active() and self.set_addons_filter(k))
            chips.append(chip)
            self.addons_chips[key] = chip
        page.append(chips)
        line = Gtk.Box(spacing=8)
        line.append(Gtk.Box(hexpand=True))
        inside = Gtk.Label(label=_("Run installs in the window's terminal"))
        inside.add_css_class("am-agent-detail")
        line.append(inside)
        switch = Gtk.Switch(active=self.installs_in_window(), valign=Gtk.Align.CENTER)
        switch.set_tooltip_text(_("On: installs and removals run in a terminal tab here (sudo asks for your "
                                  "password in it). Off: in a terminal window of its own."))
        switch.connect("notify::active", lambda s, *_a: self.save_setting(installs_in_window=s.get_active()))
        line.append(switch)
        page.append(line)
        self.addons_status = Gtk.Label(xalign=0, wrap=True)
        self.addons_status.add_css_class("am-agent-detail")
        page.append(self.addons_status)
        # Newer versions in the signed catalog: what to do, and one click for all of them.
        self.addons_update_bar = Gtk.Box(spacing=8, visible=False)
        self.addons_update_bar.add_css_class("am-addon-update-bar")
        self.addons_update_text = Gtk.Label(xalign=0, wrap=True, hexpand=True)
        self.addons_update_bar.append(self.addons_update_text)
        update_all = Gtk.Button(label=_("Update all"), valign=Gtk.Align.CENTER)
        update_all.add_css_class("suggested-action")
        update_all.connect("clicked", lambda *_a: self.update_all_addons())
        self.addons_update_bar.append(update_all)
        page.append(self.addons_update_bar)
        self.addons_stack = Gtk.Stack(vexpand=True, transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.addons_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP, margin_bottom=12)
        self.addons_stack.add_named(self.page_scroller(self.addons_list), "list")
        self.addons_detail = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP, margin_bottom=12)
        clamp = Adw.Clamp(maximum_size=820, child=self.addons_detail, unit=Adw.LengthUnit.PX)
        self.addons_stack.add_named(self.page_scroller(clamp), "detail")
        page.append(self.addons_stack)
        self.addons_items = []
        self.addons_entries = []
        return page

    # --- at every start: what is installed is on the map (Maprisma task 055) ---
    def enable_found_addons(self, reload: bool = True):
        """The add-ons of agents and local LLMs found on this PC are switched on, except the ones the user
        switched off; the map then shows them. At the start once setup was done (not during the first-run
        setup, whose welcome page ticks what it found)."""
        try:
            import action_rules
            rules = ai_core.setting("action_rules")
            kinds = {a.id: a.kind for a in self.adapter_addons()}
            found = [i for i in self.installed_adapters(self.adapter_addons())
                     if action_rules.allows(rules, *action_rules.addon_check("on", i, kinds.get(i, "")))]
            switched = addon_store.enable_found(found)
        except Exception:               # never keep the app from starting
            switched = []
        if switched:
            self.reload_addons() if reload else self.load_addons()
        return False

    # --- first start: "Set up AI Agents Map" ---
    def adapter_addons(self) -> list:
        """The installed, valid add-ons that bring an agent or a provider."""
        return [a for a in self.addons if a.kind in ("agent", "provider") and a.state in ("verified", "loaded")]

    def setup_needed(self) -> bool:
        """Adapter add-ons are there and none is on: the map would be empty, so AI Agents Map starts on
        the welcome page with the cards (also after Later, until something is switched on)."""
        adapters = self.adapter_addons()
        return bool(adapters) and not any(a.state == "loaded" for a in adapters)

    def installed_adapters(self, adapters: list) -> set:
        """Ids of the adapter add-ons whose program is really installed: every adapter is loaded for a
        moment and the app's own detection runs (a leftover settings folder or an empty shim does not
        count); then only the add-ons switched on are loaded again."""
        import agents
        import inventory
        enabled = ai_core.setting("addons_enabled")
        enabled = {e for e in enabled if isinstance(e, str)} if isinstance(enabled, list) else set()
        found = set()
        self.setup_glyphs = {}
        try:
            addons.load_all(enabled | {a.id for a in adapters})
            processes = inventory.own_processes()
            self.collect_glyphs(adapters)
            for addon in adapters:
                if addon.kind == "provider" and len(addon.provides) > 1:
                    found.add(addon.id)             # the online providers: always there (Maprisma's setupInstalled)
                for key in addon.provides[:1]:
                    if addon.kind == "agent" and agents.detect(key)[0]:
                        found.add(addon.id)
                    p = providers.get(key) if addon.kind == "provider" and len(addon.provides) == 1 else None
                    if p is not None and p.detect is not None and p.detect(processes).installed:
                        found.add(addon.id)
        except Exception:
            found = {a.id for a in adapters if addon_store.present(a)}
        finally:
            self.load_addons()
        return found

    LOCAL_GLYPH, ONLINE_GLYPH, AGENT_GLYPH = setup_page.LOCAL_GLYPH, setup_page.ONLINE_GLYPH, setup_page.AGENT_GLYPH

    def collect_glyphs(self, adapters: list):
        """The icons of the agents and providers the adapters bring (while they are all loaded)."""
        import agents
        for addon in adapters:
            if addon.kind == "provider":
                online = len(addon.provides) > 1
                self.setup_glyphs[addon.id] = (self.ONLINE_GLYPH if online else self.LOCAL_GLYPH, "")
                continue
            h = harnesses.get(addon.provides[0]) if addon.provides else None
            if h is not None and h.icon:
                # Omarchy's own font is only there on Omarchy: elsewhere the robot.
                omarchy = h.icon_font == "omarchy"
                self.setup_glyphs[addon.id] = (agents.ROBOT, "") if omarchy and desktop.NAME != "omarchy" \
                    else (h.icon, h.icon_font)

    def adapter_glyphs(self) -> dict:
        """The adapters' icons for the Add-ons page: looked up once (every adapter loaded for a moment,
        as for the welcome page), then kept."""
        if getattr(self, "setup_glyphs", None):
            return self.setup_glyphs
        adapters = self.adapter_addons()
        self.setup_glyphs = {}
        if not adapters:
            return self.setup_glyphs
        enabled = ai_core.setting("addons_enabled")
        enabled = {e for e in enabled if isinstance(e, str)} if isinstance(enabled, list) else set()
        try:
            addons.load_all(enabled | {a.id for a in adapters})
            self.collect_glyphs(adapters)
        except Exception:
            pass
        finally:
            self.load_addons()
        return self.setup_glyphs

    # --- open, close, data ---
    def open_addons(self, select: str = ""):
        if self.page_name() not in ("settings", "help", "addons"):
            self.remember_page()
        self.addons_stack.set_visible_child_name("list")
        self.show_page("addons")
        self.addons_select = select
        self.refresh_addons()

    def close_addons(self):
        if self.addons_stack.get_visible_child_name() == "detail":
            self.addons_stack.set_visible_child_name("list")
            return
        self.back_to_last_page()

    def refresh_addons(self):
        """The cards from what is installed now; the catalog read in the background."""
        glyphs = self.adapter_glyphs()      # first: the lookup loads the add-ons again
        self.addons_items = build_items(self.addons, self.addons_entries, self.agent_list, self.inventory.runtimes,
                                        glyphs)
        self.fill_addons_list()
        online = addon_store.online()
        self.addons_status.set_label(_("Looking for add-ons…") if online else
                                     _("Looking for add-ons online is off (Settings › Add-ons): only what is "
                                       "installed is shown."))

        def work():
            try:
                return addon_store.catalog(online)
            except Exception:
                return []

        def done(entries):
            self.addons_entries = entries
            glyphs = self.adapter_glyphs()
            self.addons_items = build_items(self.addons, entries, self.agent_list, self.inventory.runtimes, glyphs)
            if online:
                self.addons_status.set_label(
                    _("Only add-ons signed by Uwe Bruckhaus are offered and loaded.") if entries else
                    _("The add-on catalog could not be read right now."))
            self.fill_addons_list()
            select = getattr(self, "addons_select", "")
            self.addons_select = ""
            item = next((i for i in self.addons_items if i.id == select), None)
            if item is not None:
                self.show_addon(item)
            return False

        threading.Thread(target=lambda: GLib.idle_add(done, work()), daemon=True).start()

    def set_addons_filter(self, key: str):
        self.addons_filter = key
        self.fill_addons_list()

    # --- the list ---
    def fill_addons_list(self):
        box = self.addons_list
        while (child := box.get_first_child()) is not None:
            box.remove(child)
        query = self.addons_search.get_text().strip()
        shown = [i for i in self.addons_items if visible(i, self.addons_filter, query)]
        updates = sum(1 for i in self.addons_items if i.update)
        self.addons_chips["updates"].set_label(_("Updates") + (f" {updates}" if updates else ""))
        self.sync_update_bar()
        self.addons_update_text.set_label(_("Newer versions of your add-ons are ready: {0}. Update all of them "
                                            "here, or one at a time with Update on its card.").format(updates))
        sections = (("suggested", _("Suggested for this PC"), "list-add-symbolic",
                     [i for i in shown if i.state == "suggested" or i.here]),
                    ("installed", _("Installed add-ons"), "application-x-addon-symbolic",
                     [i for i in shown if i.state in ("on", "off", "failed") and not i.here]),
                    ("available", _("Available"), "web-browser-symbolic", [i for i in shown if i.state == "available"]),
                    ("builtin", _("Built into AI Agents Map"), "emblem-system-symbolic",
                     [i for i in shown if i.state == "builtin"]))
        for key, title, icon, items in sections:
            if not items:
                continue
            # A category as a group card: open, folds away with a click on its heading (remembered).
            group = self.group_card(box, f"addons-{key}", title, icon, str(len(items)), default_open=True)
            # Installed add-ons: agents, then providers, then the rest, a line between them.
            parts = [[i for i in items if i.kind == "agent"], [i for i in items if i.kind == "provider"],
                     [i for i in items if i.kind not in ("agent", "provider")]] if key == "installed" else [items]
            first = True
            for part in parts:
                if not part:
                    continue
                if not first:
                    group.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
                first = False
                # One width for every part's cards (at most three a row), so the parts line up.
                flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True,
                                   column_spacing=ai_core.GAP, row_spacing=ai_core.GAP, min_children_per_line=1,
                                   max_children_per_line=3)
                for item in part:
                    flow.append(self.addon_card(item))
                group.append(flow)
        if not shown:
            self.note(box, _("Nothing matches."))

    def addon_icon(self, item: Item, size: int = 24) -> Gtk.Widget:
        if item.glyph:
            from ui_helpers import glyph_widget
            return glyph_widget(item.glyph, item.glyph_font, size, ("am-agent-icon",))
        image = Gtk.Image.new_from_icon_name(item.icon or KIND_ICONS.get(item.kind, KIND_ICONS["feature"]))
        image.set_pixel_size(size)
        return image

    def state_badge(self, item: Item) -> Gtk.Label:
        text, css = {"builtin": (_("Built in"), "am-addon-builtin"), "on": (_("On"), "am-addon-on"),
                     "off": (_("Off"), "am-addon-off"), "failed": (_("Not loaded"), "am-addon-failed"),
                     "available": (_("Available"), "am-addon-available"),
                     "suggested": (_("For this PC"), "am-addon-on")}[item.state]
        if item.update:
            text, css = _("Update"), "am-addon-on"
        badge = Gtk.Label(label=text, valign=Gtk.Align.CENTER)
        badge.add_css_class("am-addon-badge")
        badge.add_css_class(css)
        return badge

    def addon_card(self, item: Item) -> Gtk.Widget:
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP_TIGHT)
        card.add_css_class("am-addon-card")
        if item.state == "suggested" or item.here:
            card.add_css_class("am-addon-suggested")
        if item.state == "failed":
            card.add_css_class("am-addon-refused")
        top = Gtk.Box(spacing=8)
        top.append(self.addon_icon(item))
        names = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        name = Gtk.Label(label=item.name, xalign=0, ellipsize=3)
        name.add_css_class("am-addon-name")
        names.append(name)
        kind = {"agent": _("Agent"), "provider": _("Provider"), "feature": _("Feature"),
                "extension": _("Extension")}.get(item.kind, _("Feature"))
        if item.installed is not None and (item.target or item.key):
            kind += " · " + (_("installed") if item.installed else _("not installed"))
        sub = Gtk.Label(label=kind + (f" · {item.version}" if item.version else ""), xalign=0)
        sub.add_css_class("am-agent-detail")
        names.append(sub)
        top.append(names)
        if item.homepage:
            top.append(self.homepage_button(item))
        top.append(self.state_badge(item))
        card.append(top)
        summary = Gtk.Label(label=item.reason if item.state == "failed" else item.summary, xalign=0, wrap=True,
                            max_width_chars=1, lines=2, ellipsize=3)
        summary.add_css_class("am-agent-detail")
        card.append(summary)
        bottom = Gtk.Box(spacing=6)
        if item.state != "builtin":
            chip = Gtk.Label(label=privacy_name(item.privacy), xalign=0, hexpand=True)
            chip.add_css_class("am-addon-privacy")
            bottom.append(chip)
        else:
            bottom.append(Gtk.Box(hexpand=True))
        if item.target:
            bottom.append(self.program_buttons(item))
        else:
            action = self.addon_action(item)
            if action is not None:
                bottom.append(action)
        card.append(bottom)
        click = Gtk.GestureClick()
        click.connect("released", lambda *_a: self.show_addon(item))
        card.add_controller(click)
        card.set_tooltip_text(_("Click for details"))
        return card

    def homepage_button(self, item: Item, small: bool = True) -> Gtk.Button:
        """Opens the web page of the agent, provider or add-on in the browser."""
        if small:
            button = Gtk.Button(icon_name="web-browser-symbolic", valign=Gtk.Align.CENTER)
            button.add_css_class("flat")
            button.add_css_class("am-small-btn")
        else:
            button = Gtk.Button(label=_("Homepage"))
        button.set_tooltip_text(_("Open {0}").format(item.homepage))
        button.connect("clicked", lambda *_a: self.open_homepage(item.homepage))
        return button

    def open_homepage(self, url: str):
        if not addons.homepage(url):
            return
        try:
            Gio.AppInfo.launch_default_for_uri(url, None)
        except GLib.Error:
            self.toast(_("No browser could be opened."), 5)

    def addon_action(self, item: Item) -> Gtk.Widget | None:
        label, run = {"suggested": (_("Get add-on"), self.install_addon), "available": (_("Install"), self.install_addon),
                      "off": (_("Switch on"), self.switch_addon_on), "on": (_("Switch off"), self.switch_addon_off),
                      "failed": (_("Remove"), self.remove_addon)}.get(item.state, (None, None))
        if item.update:
            label, run = _("Update"), self.update_addon
        if label is None:
            return None
        button = Gtk.Button(label=label, valign=Gtk.Align.CENTER)
        button.add_css_class("am-small-btn")
        if item.state in ("suggested", "available") or item.update or item.state == "builtin":
            button.add_css_class("suggested-action")
        button.connect("clicked", lambda *_a: run(item))
        return button

    def program_buttons(self, item: Item, small: bool = True) -> Gtk.Widget:
        """Install and Uninstall for the program itself, always both; the one that does not apply now
        (or that has no way on this PC) is greyed out, with why in its tooltip."""
        box = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
        subject = self.install_subject(item.target, item.key or item.id)
        installed = bool(subject and subject[1])
        for action, label in (("install", _("Install")), ("uninstall", _("Uninstall"))):
            button = Gtk.Button(label=label if small else label + "…")
            if small:
                button.add_css_class("am-small-btn")
            step = install_recipes.choose(subject[3], action, subject[2]) if subject else None
            if action == "install" and installed:
                why = _("{0} is installed.").format(item.name)
            elif action == "uninstall" and not installed:
                why = _("{0} is not installed on this PC.").format(item.name)
            elif step is None:
                why = _("AI Agents Map knows no way to change {0} here.").format(item.name)
            else:
                why = ""
            button.set_sensitive(not why)
            button.set_tooltip_text(why or (_("Shows the command first") if not step.installer else
                                            _("Opens the install page")))
            if not why and action == "install":
                button.add_css_class("suggested-action")
            button.connect("clicked", lambda *_a, a=action: self.offer_install(item.target, item.key or item.id, a))
            box.append(button)
        return box

    # --- details ---
    def show_addon(self, item: Item):
        box = self.addons_detail
        while (child := box.get_first_child()) is not None:
            box.remove(child)
        top = Gtk.Box(spacing=12)
        top.append(self.addon_icon(item, 48))
        names = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, valign=Gtk.Align.CENTER)
        name = Gtk.Label(label=item.name, xalign=0)
        name.add_css_class("title-2")
        names.append(name)
        kind = {"agent": _("Agent"), "provider": _("Provider"), "feature": _("Feature"),
                "extension": _("Extension")}.get(item.kind, _("Feature"))
        line = Gtk.Label(label=kind + (f" · {item.version}" if item.version else ""), xalign=0)
        line.add_css_class("am-agent-detail")
        names.append(line)
        top.append(names)
        top.append(self.state_badge(item))
        box.append(top)
        if item.description or item.summary:
            text = Gtk.Label(label=item.description or item.summary, xalign=0, wrap=True, max_width_chars=1,
                             selectable=True)
            box.append(text)
        if item.state == "failed":
            self.settings_heading(box, _("Why it is not loaded"))
            self.note(box, item.reason)
        if item.state == "builtin":
            self.note(box, _("Part of AI Agents Map itself: always there, nothing to install or remove for it."))
            if item.target:
                self.settings_heading(box, _("The program"))
                self.note(box, (_("{0} is installed on this PC.") if item.installed else
                                _("{0} is not installed on this PC.")).format(item.name))
        else:
            if item.key and item.installed is not None:
                self.settings_heading(box, _("The program"))
                self.note(box, (_("{0} is installed on this PC.") if item.installed else
                                _("{0} is not installed on this PC.")).format(item.name)
                          + ("" if item.state == "on" else " " + _("Switch the add-on on to install or remove it here.")))
            if item.extends:
                # An extension: which agents it adds to, and what it adds that is not used.
                self.settings_heading(box, _("Adds to"))
                rows = self.boxed_list()
                for command in item.extends:
                    h = harnesses.get(command)
                    rows.append(self.plain_row(h.full_name or h.name if h else command,
                                               "" if h else _("Not on the map now: its add-on is off.")))
                box.append(rows)
                unused = harnesses.conflicts().get(item.id, [])
                if unused:
                    self.note(box, _("Not used, because the agent or an earlier extension has it already: {0}")
                              .format(", ".join(unused)))
            self.settings_heading(box, _("What it reads"))
            rows = self.boxed_list()
            for text in item.reads or [_("Nothing beyond what AI Agents Map shows anyway.")]:
                rows.append(self.plain_row(text))
            box.append(rows)
            self.settings_heading(box, _("What it sends"))
            rows = self.boxed_list()
            for text in item.sends or [_("Nothing: it sends nothing anywhere.")]:
                rows.append(self.plain_row(text))
            box.append(rows)
            self.settings_heading(box, _("What it may do"))
            rows = self.boxed_list()
            rows.append(self.plain_row(_("Privacy level: {0}").format(privacy_name(item.privacy))))
            for capability in item.capabilities:
                rows.append(self.plain_row(capability_text(capability)))
            addon = item.addon
            if addon is not None and addon.state in ("verified", "loaded"):     # its files are known then
                files = addon.files
                text = (_("Brings code of its own (addon.py)") if "addon.py" in files else
                        _("Facts about the agent, and a little code for what facts cannot say (hooks.py)")
                        if "hooks.py" in files else _("Brings no code: only facts about the agent (agent.toml)"))
                rows.append(self.plain_row(text))
            box.append(rows)
            self.note(box, _("Signed by Uwe Bruckhaus (key {0}). AI Agents Map checks the signature and every file "
                             "before anything of it runs.").format("SHA256:e5cC634Ke3Our5LAeSc88qs22NKZEHTncBW5Ig3amWQ"))
        buttons = Gtk.Box(spacing=8, margin_top=6)
        back = Gtk.Button(label=_("All add-ons"))
        back.connect("clicked", lambda *_a: self.addons_stack.set_visible_child_name("list"))
        buttons.append(back)
        if item.homepage:
            buttons.append(self.homepage_button(item, small=False))
        actions = []
        if item.update:
            actions.append((_("Update"), self.update_addon, True))
        actions += {"suggested": [(_("Get add-on"), self.install_addon, True)],
                    "available": [(_("Install"), self.install_addon, True)],
                    "off": [(_("Switch on"), self.switch_addon_on, False)],
                    "on": [(_("Switch off"), self.switch_addon_off, False)]}.get(item.state, [])
        for label, run, suggested in actions:
            button = Gtk.Button(label=label)
            if suggested:
                button.add_css_class("suggested-action")
            button.connect("clicked", lambda *_a, r=run: r(item))
            buttons.append(button)
        if item.target:
            buttons.append(self.program_buttons(item, small=False))
        if item.state == "on" and not item.key:
            # A feature add-on made for an agent: install the program itself too.
            for command in (item.entry.harnesses if item.entry else []):
                h = harnesses.get(command)
                if h is not None and h.install and not any(a.command == command and a.installed
                                                           for a in self.agent_list):
                    button = Gtk.Button(label=_("Install {0} too").format(h.full_name or h.name))
                    button.add_css_class("suggested-action")
                    button.connect("clicked", lambda *_a, c=command: self.offer_install("agent", c))
                    buttons.append(button)
        if item.state in ("on", "off", "failed") and item.addon is not None:
            remove = Gtk.Button(label=_("Remove"))
            remove.add_css_class("destructive-action")
            remove.connect("clicked", lambda *_a: self.remove_addon(item))
            buttons.append(remove)
        box.append(buttons)
        self.addons_stack.set_visible_child_name("detail")

    # --- actions ---
    def addon_confirm(self, item: Item, heading: str, ok_label: str, on_ok):
        """Before switching an add-on on: what it reads, sends and may do."""
        lines = [privacy_name(item.privacy)] + [capability_text(c) for c in item.capabilities]
        body = _("{0} reads: {1}").format(item.name, "; ".join(item.reads) or _("nothing beyond what AI Agents Map shows")) \
            + "\n\n" + _("It sends: {0}").format("; ".join(item.sends) or _("nothing")) + "\n\n" + " · ".join(lines)
        dialog = Adw.AlertDialog(heading=heading, body=body)
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("ok", ok_label)
        dialog.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("cancel")
        dialog.connect("response", lambda _d, response: response == "ok" and on_ok())
        dialog.present(self.window)

    def addons_changed(self, message: str = "", select: str = ""):
        self.reload_addons()
        if message:
            self.toast(message, 4)
        self.addons_select = select
        self.refresh_addons()
        if hasattr(self, "refresh_setup"):
            self.refresh_setup()          # an update from a Setup card: its badge and menu follow

    def addons_busy(self) -> bool:
        """An install or update runs: one at a time (two writing the same add-on folder collide)."""
        if getattr(self, "addons_updating", False):
            self.toast(_("Add-ons are being updated. Try again in a moment."), 4)
            return True
        return False

    def run_addon_job(self, work, done):
        """`work` in the background, `done(result)` on the GTK thread; the busy flag is set until then,
        whatever `work` raises."""
        self.addons_updating = True

        def run():
            try:
                result = work()
            except Exception as error:      # never leave the flag set
                result = error
            GLib.idle_add(finish, result)

        def finish(result):
            self.addons_updating = False
            done(result)
            self.sync_update_bar()          # a failed job leaves the list as it was: the bar comes back
            return False
        try:
            threading.Thread(target=run, daemon=True).start()
        except RuntimeError as error:
            finish(error)

    def sync_update_bar(self):
        bar = getattr(self, "addons_update_bar", None)
        if bar is not None:
            bar.set_visible(any(i.update for i in getattr(self, "addons_items", []))
                            and not getattr(self, "addons_updating", False))

    def install_addon(self, item: Item):
        if item.entry is None or not self.addon_rule_allows("install", item.id, item.kind):
            return

        def go():
            if self.addons_busy():
                return
            self.toast(_("Installing {0}…").format(item.name), 3)

            def work():
                try:
                    addon_store.install(item.entry)
                    addon_store.set_enabled(item.id, True)
                    return ""
                except addon_store.InstallError as error:
                    return str(error)

            def done(error):
                if error:
                    self.toast(_("{0} was not installed: {1}").format(item.name, error), 8)
                else:
                    self.addons_changed(_("{0} is installed and on.").format(item.name), item.id)
            self.run_addon_job(work, done)
        self.addon_confirm(item, _("Install {0}?").format(item.name), _("Install and switch on"), go)

    def addon_rule_allows(self, step: str, addon_id: str, kind: str = "") -> bool:
        """Settings › What may be done, for an add-on change (action_rules.addon_check)."""
        import action_rules
        check = action_rules.addon_check(step, addon_id, kind)
        return check is None or self.rule_allows(*check)

    def addon_locked(self, ids) -> bool:
        """Maprisma's add-on lock (tasks 027/028): the add-on of an agent running in a terminal here waits;
        switching it off, updating or removing it is refused with why. Others stay free."""
        running = {command for command, pane in getattr(self, "sessions", {}).items()
                   if getattr(pane, "running", False)}
        locked = [a for a in getattr(self, "addons", []) if a.id in set(ids) and a.kind == "agent"
                  and running.intersection(a.provides)]
        if not locked:
            return False
        names = [self.item_name(f"agent:{a.provides[0]}") for a in locked if a.provides]
        self.toast(_("{0} is running in a terminal. Its add-on can be switched off, updated or removed when it has "
                     "finished.").format(names[0]) if len(names) == 1 else
                   _("{0} are running in terminals. Their add-ons can be switched off, updated or removed when "
                     "they have finished.").format(", ".join(names)), 8)
        return True

    def update_addon(self, item: Item):
        if item.entry is None or self.addons_busy() or self.addon_locked([item.id]) \
                or not self.addon_rule_allows("update", item.id, item.kind):
            return

        def work():
            try:
                addon_store.install(item.entry)
                return ""
            except addon_store.InstallError as error:
                return str(error)

        def done(error):
            if error:
                self.toast(_("{0} was not updated: {1}").format(item.name, error), 8)
            else:
                self.addons_changed(_("{0} is updated to {1}.").format(item.name, item.entry.version), item.id)
        self.toast(_("Updating {0}…").format(item.name), 3)
        self.run_addon_job(work, done)

    def addon_update_items(self) -> list:
        """The installed add-ons the signed catalog has a newer version of (empty until the catalog
        was read, and in the plugin edition, which has no add-ons)."""
        try:
            items = build_items(self.addons, getattr(self, "addons_entries", []), self.agent_list,
                                self.inventory.runtimes)
        except Exception:
            return []
        return [i for i in items if i.update and i.entry is not None]

    def update_all_addons(self):
        """Every add-on with a newer version, one after the other in the background; then they
        are loaded again (no restart) and one toast says what happened."""
        import action_rules
        rules = ai_core.setting("action_rules")
        # Only those the rules allow (each refused one is left as it is, quietly: one toast at the end).
        items = [i for i in self.addon_update_items()
                 if action_rules.allows(rules, *action_rules.addon_check("update", i.id, i.kind))]
        if not items or self.addons_busy():
            return
        # A running agent's own add-on waits; the others are updated (Maprisma's Update all).
        running = {c for c, p in getattr(self, "sessions", {}).items() if getattr(p, "running", False)}
        waiting = {a.id for a in getattr(self, "addons", []) if a.kind == "agent" and running.intersection(a.provides)}
        if waiting:
            self.addon_locked(waiting)
            items = [i for i in items if i.id not in waiting]
            if not items:
                return
        if hasattr(self, "addons_update_bar"):
            self.addons_update_bar.set_visible(False)

        def work():
            done, failed = [], []
            for item in items:
                try:
                    addon_store.install(item.entry)
                    done.append(item.name)
                except Exception as error:      # one bad download never stops the others
                    failed.append(f"{item.name} ({error})")
            return done, failed

        def finished(result):
            done, failed = result if isinstance(result, tuple) else ([], [str(result)])
            message = _("Add-ons updated: {0}").format(", ".join(done)) if done else ""
            if failed:
                message = (message + " · " if message else "") + _("Not updated: {0}").format("; ".join(failed))
            self.addons_changed(message)
            self.refresh_overview()
        self.toast(_("Updating add-ons…"), 3)
        self.run_addon_job(work, finished)

    def switch_addon_on(self, item: Item):
        if not self.addon_rule_allows("on", item.id, item.kind):
            return

        def go():
            addon_store.set_enabled(item.id, True)
            self.addons_changed(_("{0} is on.").format(item.name), item.id)
        self.addon_confirm(item, _("Switch on {0}?").format(item.name), _("Switch on"), go)

    def switch_addon_off(self, item: Item):
        if self.addon_locked([item.id]) or not self.addon_rule_allows("off", item.id, item.kind):
            return
        addon_store.set_enabled(item.id, False)
        self.addons_changed(_("{0} is off.").format(item.name), item.id)

    def remove_addon(self, item: Item):
        if self.addon_locked([item.id]) or not self.addon_rule_allows("remove", item.id, item.kind):
            return
        dialog = Adw.AlertDialog(heading=_("Remove {0}?").format(item.name),
                                 body=_("It goes to the trash; you can install it again any time."))
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("remove", _("Remove"))
        dialog.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")

        def answered(_d, response):
            if response != "remove":
                return
            if addon_store.remove(item.id):
                self.addons_changed(_("{0} is removed (in the trash).").format(item.name))
            else:
                self.toast(_("{0} is installed for every user; remove its package instead.").format(item.name), 8)
        dialog.connect("response", answered)
        dialog.present(self.window)

    def addon_from_file(self):
        # Its kind is known only once it is read: installing must be allowed for agents and providers.
        if not (self.rule_allows("install", "agents") and self.rule_allows("install", "providers")):
            return
        dialog = Gtk.FileDialog(title=_("Install an add-on from a file"))
        zips = Gtk.FileFilter(name=_("Add-on (.zip)"))
        zips.add_pattern("*.zip")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(zips)
        dialog.set_filters(filters)

        def chosen(_dialog, result):
            try:
                file = dialog.open_finish(result)
            except GLib.Error:
                return
            path = file.get_path() if file is not None else None
            if not path or self.addons_busy():
                return

            import action_rules
            rules, hints = ai_core.setting("action_rules"), ai_core.setting("action_rule_hints") is not False

            def allow(aid, kind, replacing):
                # The add-on's own rule, known once the zip is verified (before anything is replaced).
                text = action_rules.refusal(rules, hints, *action_rules.addon_check("install", aid, kind))
                if text is None and replacing:
                    text = action_rules.refusal(rules, hints, *action_rules.addon_check("update", aid, kind))
                return text

            def work():
                try:
                    return addon_store.install_file(Path(path), allow=allow)
                except addon_store.InstallError as error:
                    return error

            def done(addon):
                if isinstance(addon, Exception):
                    self.toast(_("Not installed: {0}").format(addon), 8)
                else:
                    self.addons_changed(_("{0} is installed; switch it on on its card.").format(addon.name),
                                        addon.id)
            self.run_addon_job(work, done)
        dialog.open(self.window, None, chosen)
