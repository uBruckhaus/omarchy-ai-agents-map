"""Setup: one page of cards for everything the map shows (a mixin of ai_popup.AgentsMap).

The map shows only what is set up; here it is set up, updated and removed:

- AI agents, local LLMs and online providers. In Asterism each comes from an add-on: a click ticks or
  unticks its card, Apply switches the add-ons on and off (nothing is deleted). Without add-ons (the
  Omarchy plugin) every one is listed, the missing ones greyed out with Install, the others with
  Uninstall. Every section folds away (its heading).
- MCP servers, skill folders and models: a card for each one that is installed. A click opens its
  details (a model: its menu); the ⋯ button or a right-click opens the same menu as its box on the map
  (update, start, details, remove…). A dashed card at the end of a section adds one.
- A card with an update waiting says so (↑ and the new version); its menu has Update.

A fresh Asterism opens here as the welcome page: the app's icon and greeting, the agents and local
LLMs found on this PC already ticked, and Switch on.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

import agent_updates
import ai_core
import desktop
from glyphs import MCP_GLYPHS, MCP_GROUP_GLYPHS, glyph
from i18n import _, ngettext
from ui_helpers import size_text

LOCAL_GLYPH, ONLINE_GLYPH, AGENT_GLYPH = "\U000F061A", "\U000F015F", "\U000f06a9"


class SetupPage:
    # --- the page ---------------------------------------------------------------------------------

    def build_setup_page(self) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP)
        # The same heading as the map's (title, Refresh, rule). No Back here: the header's Map
        # button (and Esc) go back, as Setup does on the map.
        self.setup_header = self.view_heading(_("Setup"), self.refresh_button())
        page.append(self.setup_header)
        self.setup_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP, margin_bottom=12)
        clamp = Adw.Clamp(maximum_size=980, child=self.setup_box, unit=Adw.LengthUnit.PX)
        page.append(self.page_scroller(clamp))
        page.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
        buttons = Gtk.Box(spacing=8, margin_bottom=6)
        self.setup_count = Gtk.Label(xalign=0, hexpand=True, wrap=True)
        self.setup_count.add_css_class("am-agent-detail")
        buttons.append(self.setup_count)
        self.setup_later = Gtk.Button(label=_("Later"))
        self.setup_later.set_tooltip_text(_("Start with an empty map; Setup in the header brings this page back."))
        self.setup_later.connect("clicked", lambda *_a: self.finish_setup(False))
        buttons.append(self.setup_later)
        self.setup_ok = Gtk.Button()
        self.setup_ok.add_css_class("suggested-action")
        self.setup_ok.connect("clicked", lambda *_a: self.finish_setup(True))
        buttons.append(self.setup_ok)
        self.setup_bar = buttons
        page.append(buttons)
        self.setup_checks = {}
        self.setup_welcome = False
        return page

    def with_addons(self) -> bool:
        """Asterism with adapter add-ons: agents and providers are switched on and off here."""
        return hasattr(self, "adapter_addons") and bool(self.adapter_addons())

    def open_setup(self):
        """The welcome page of a fresh Asterism (and Later's way back)."""
        self.setup_installed = self.installed_adapters(self.adapter_addons())
        self.setup_welcome = True
        self.fill_setup()
        self.show_view("setup", save=False)

    def open_my_setup(self):
        """Setup in the header: the cards, with what was found on this PC looked up again."""
        if self.with_addons():
            self.setup_installed = self.installed_adapters(self.adapter_addons())
        self.setup_welcome = False
        self.fill_setup()
        self.show_view("setup", save=False)

    def close_setup(self):
        self.setup_welcome = False
        self.show_view("main", save=False)

    def refresh_setup(self):
        """After a change made from a card (removed, updated, switched on): the cards again, with the
        ticks not applied yet and the scroll position kept."""
        if self.page_name() != "setup" or self.setup_welcome:
            return False
        ticks = {i: check.get_active() for i, (check, addon) in self.setup_checks.items()
                 if check.get_active() != (addon.state == "loaded")}
        scroller = self.setup_box.get_ancestor(Gtk.ScrolledWindow)
        top = scroller.get_vadjustment().get_value() if scroller is not None else 0
        self.fill_setup()
        for addon_id, active in ticks.items():
            if addon_id in self.setup_checks:
                self.setup_checks[addon_id][0].set_active(active)
        if scroller is not None:
            GLib.idle_add(lambda: scroller.get_vadjustment().set_value(top) and False)
        return False

    def refresh_button(self) -> Gtk.Widget:
        """Refresh (F5), at the right of Setup's and the map's heading: a symbol, like the header's
        (an option, not a must); its tooltip says what it does."""
        refresh = Gtk.Button(icon_name="view-refresh-symbolic", valign=Gtk.Align.CENTER)
        refresh.add_css_class("flat")
        refresh.add_css_class("am-header-btn")
        refresh.add_css_class("am-refresh-btn")
        refresh.set_tooltip_text(_("Refresh") + " (F5) — " + _("Look again at what is on this PC (F5)").replace(" (F5)", ""))
        refresh.connect("clicked", lambda *_a: self.reload_setup())
        return refresh

    def reload_setup(self):
        """Refresh (F5, on the map and on Setup): what is installed, set up and signed in looked
        up again; the map, the hints and Setup's cards follow. Ticks not applied yet stay."""
        self.collect()
        if self.with_addons():
            self.setup_installed = self.installed_adapters(self.adapter_addons())
        self.refresh_overview()
        self.refresh_map()
        self.refresh_setup()
        self.toast(_("Looked again."), 2)

    def fill_setup(self):
        box = self.setup_box
        while (child := box.get_first_child()) is not None:
            box.remove(child)
        self.setup_checks = {}
        welcome = self.setup_welcome
        self.setup_header.set_visible(not welcome)
        self.setup_later.set_visible(welcome)
        if not welcome:
            box.append(self.setup_explainer())
            self.setup_uses(box)
        self.setup_problem_keys = set(self.setup_problems())
        if self.with_addons():
            self.fill_addon_sections(box, welcome)
        else:
            self.fill_program_sections(box)
        if not welcome:
            self.fill_tool_sections(box)
            self.fill_model_sections(box)
        self.update_setup_button()
        self.setup_just_filled = self.stack.get_visible_child_name() != "setup"   # about to be shown

    # --- agents and providers: add-on cards that are ticked (Asterism) ------------------------------

    def setup_hero(self, found: int) -> Gtk.Widget:
        hero = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP_TIGHT, margin_top=18, margin_bottom=6)
        icon = Gtk.Image.new_from_icon_name(desktop.app_id())
        icon.set_pixel_size(72)
        hero.append(icon)
        title = Gtk.Label(label=_("Welcome to {0}").format(desktop.app_name()))
        title.add_css_class("am-setup-title")
        hero.append(title)
        text = _("Pick the AI agents and local LLMs you use. The ones found on this PC are already ticked: "
                 "one click on Switch on puts them on your map.") if found else \
            _("No agent or local LLM was found on this PC yet. Pick what you want to use: after Switch on, "
              "Asterism shows you how to install it.")
        line = Gtk.Label(label=text, wrap=True, justify=Gtk.Justification.CENTER, max_width_chars=60)
        line.add_css_class("am-hint")
        hero.append(line)
        return hero

    def fill_addon_sections(self, box, welcome: bool):
        """What Asterism found on this PC comes first; on the welcome page it comes ticked."""
        adapters = self.adapter_addons()
        online = [a for a in adapters if a.kind == "provider" and len(a.provides) > 1]
        programs = [a for a in adapters if a not in online]
        installed = getattr(self, "setup_installed", set())
        found = [a for a in programs if a.id in installed]
        on = {a.id for a in adapters if a.state == "loaded"}
        if welcome:
            box.append(self.setup_hero(len(found)))
        import addon_store
        before = [a for a in programs if a not in found and addon_store.present(a)]
        missing = [a for a in programs if a not in found and a not in before]
        state = {**{a.id: _("Installed") for a in found}, **{a.id: _("Used before") for a in before},
                 **{a.id: _("Not installed") for a in missing}, **{a.id: _("Online") for a in online}}
        ordered = found + before + missing
        newer = self.addon_updates() if not welcome else set()
        updates = self.open_updates() if not welcome else {}
        outer = box
        for key, title, hint, group, more in (
                ("agents", _("AI agents"), _("The coding agents you work with in a terminal."),
                 [a for a in ordered if a.kind == "agent"], _("More agents")),
                ("local", _("Local LLMs"), _("Programs that run language models on this PC."),
                 [a for a in ordered if a.kind == "provider"], _("More local LLMs")),
                ("online", _("Online providers"), _("The models your agents use on the web."), online, "")):
            if not group:
                continue
            if welcome:
                box = outer
                self.settings_heading(box, title)
            else:
                box = self.setup_section(outer, key, title, len(group))
            self.note(box, hint if welcome else hint + " " + _("Click a card to switch it on or off."))
            flow = self.setup_flow()
            for addon in group:
                ticked = (addon in found or addon in online or addon.id in on) if welcome else addon.id in on
                program = updates.get(self.addon_node(addon))
                flow.append(self.setup_tile(addon, ticked, state[addon.id], addon in online,
                                            here=addon in found or addon in online, welcome=welcome,
                                            update=program.available if program else
                                            _("Add-on update") if addon.id in newer else ""))
            if more and not welcome and hasattr(self, "open_addons"):
                flow.append(self.add_tile(_("Get add-ons"), more, "package", self.open_addons))
            box.append(flow)
        if welcome and (missing or before):
            self.note(outer, _("Ticked but not installed: after Switch on, Asterism shows you how to install it."))

    @staticmethod
    def setup_flow() -> Gtk.FlowBox:
        return Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True, column_spacing=ai_core.GAP,
                           row_spacing=ai_core.GAP, min_children_per_line=2, max_children_per_line=5, valign=Gtk.Align.START)

    def setup_tile(self, addon, ticked: bool, state: str, online: bool, here: bool, welcome: bool = True,
                   update: str = "") -> Gtk.Widget:
        """One add-on card: the agent's or provider's icon, its name and whether it is on this PC; the
        whole card is the tick (click, or Space on the focused card). On Setup its ⋯ menu has what
        its box on the map has, and the add-on's own page."""
        tile = Gtk.ToggleButton(active=ticked)
        tile.add_css_class("am-setup-tile")
        if here and not online:
            tile.add_css_class("am-setup-installed")      # highlighted: installed, whether ticked or not
        elif not here:
            tile.add_css_class("am-setup-absent")
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        top = Gtk.Box(spacing=8)
        glyph_text, font = getattr(self, "setup_glyphs", {}).get(addon.id) or \
            ((ONLINE_GLYPH if online else LOCAL_GLYPH) if addon.kind == "provider" else AGENT_GLYPH, "")
        icon = self.setup_glyph(glyph_text, font)
        top.append(icon)
        if update:
            top.append(self.update_badge(update))
        problem = None if welcome else self.problem_badge(self.addon_node(addon))
        if problem is not None:
            top.append(problem)
        battery = None if welcome else self.battery_badge(self.addon_node(addon))
        if battery is not None:
            top.append(battery)
        mark = Gtk.Label(label="✓", valign=Gtk.Align.START, visible=ticked)
        mark.add_css_class("am-setup-check")
        top.append(mark)
        body.append(top)
        body.append(self.tile_name(addon.name))
        sub = self.tile_sub(state)
        if here and not online:
            sub.add_css_class("am-setup-here")
        body.append(sub)
        tile.set_child(body)
        tile.set_tooltip_text(addon.summary or addon.name)

        def toggled(button):
            mark.set_visible(button.get_active())
            if not welcome:
                was = addon.state == "loaded"
                sub.set_label(state if button.get_active() == was else
                              _("Switches on") if button.get_active() else _("Switches off"))
            self.update_setup_button()

        tile.connect("toggled", toggled)
        self.setup_checks[addon.id] = (tile, addon)
        if welcome:
            return tile
        node_id = self.addon_node(addon)
        return self.with_menu(tile, lambda widget, rect: self.show_context(
            widget, node_id, rect, extra=[self.addon_entry(addon)]) if node_id else self.addon_menu(widget, rect, addon))

    def addon_node(self, addon) -> str:
        """The map box of an add-on that is on: agent:… or runtime:… ("" for an online provider)."""
        if addon.state != "loaded" or not addon.provides or (addon.kind == "provider" and len(addon.provides) > 1):
            return ""
        key = addon.provides[0]
        if addon.kind == "agent":
            return f"agent:{key}" if any(a.command == key for a in self.agent_list) else ""
        return f"runtime:{key}" if any(r.key == key for r in self.inventory.runtimes) else ""

    def addon_entry(self, addon):
        return lambda popover: self.menu_item(_("Add-on…"), popover, lambda: self.open_addons(select=addon.id),
                                              _("What it reads, sends and may do; update or remove it"))

    def addon_menu(self, widget, rect, addon):
        popover = self.context_popover(widget, rect)
        self.show_menu(popover, addon.name, [self.sign_in_entries(addon, popover), [self.addon_entry(addon)(popover)]])

    def sign_in_entries(self, addon, popover) -> list:
        """An online provider's add-on: Sign in to … for each installed agent of its providers."""
        import providers
        if addon.kind != "provider" or len(addon.provides) <= 1:
            return []
        commands = {c for key in addon.provides if (p := providers.get(key)) is not None for c in p.agents}
        return [self.menu_item(_("Sign in to {0}…").format(a.name), popover,
                               lambda c=a.command: self.offer_sign_in(c), _("Subscription, account or API key"))
                for a in self.agent_list if a.installed and a.command in commands and self.sign_in_access(a.command)]

    def addon_updates(self) -> set:
        """Ids of the add-ons the signed catalog has a newer version of."""
        try:
            from addons_page import build_items
            items = build_items(self.addons, getattr(self, "addons_entries", []), self.agent_list,
                                self.inventory.runtimes)
        except Exception:
            return set()
        return {i.id for i in items if i.update}

    def update_setup_button(self):
        if self.setup_welcome:
            n = sum(1 for check, _addon in self.setup_checks.values() if check.get_active())
            self.setup_ok.set_label(_("Switch on {0}").format(n) if n else _("Switch on"))
            self.setup_ok.set_sensitive(n > 0)
            self.setup_ok.set_visible(True)
            self.setup_count.set_label(_("Click a card to tick it or untick it."))
            return
        on = sum(1 for check, addon in self.setup_checks.values() if check.get_active() and addon.state != "loaded")
        off = sum(1 for check, addon in self.setup_checks.values() if not check.get_active() and addon.state == "loaded")
        parts = ([_("{0} on").format(on)] if on else []) + ([_("{0} off").format(off)] if off else [])
        self.setup_ok.set_label(_("Apply: {0}").format(" · ".join(parts)) if parts else _("Apply"))
        self.setup_ok.set_visible(bool(parts))
        self.setup_count.set_label(
            _("Switching an add-on off takes it off the map; nothing is deleted.") if parts else
            _("The map shows what is set up here. ⋯ or a right-click on a card: update, start, details, remove."))

    def finish_setup(self, switch_on: bool):
        if not self.setup_welcome:
            self.apply_setup()
            return
        self.save_setting(setup_done=True)
        self.setup_welcome = False
        chosen = [addon for check, addon in self.setup_checks.values() if check.get_active()] if switch_on else []
        if not chosen:
            self.show_view("main", save=False)
            return
        import addon_store
        for check, addon in self.setup_checks.values():
            addon_store.set_enabled(addon.id, check.get_active())
        self.reload_addons()
        absent = [a for a in chosen if (a.kind == "agent" or a.commands) and a.id not in self.setup_installed]
        self.toast(_("{0} add-ons are on.").format(len(chosen)), 4)
        if absent:
            # Ticked but not installed: their cards on Setup, whose menu installs them.
            self.open_my_setup()
            self.note_absent(absent)
        else:
            self.show_view("main", save=False)

    def apply_setup(self):
        """Setup's Apply: only the add-ons whose tick changed are switched."""
        import addon_store
        changed = [(check.get_active(), addon) for check, addon in self.setup_checks.values()
                   if check.get_active() != (addon.state == "loaded")]
        if not changed:
            return
        for on, addon in changed:
            addon_store.set_enabled(addon.id, on)
        self.reload_addons()
        on = [a.name for wanted, a in changed if wanted]
        off = [a.name for wanted, a in changed if not wanted]
        self.toast(" · ".join(([_("On: {0}").format(", ".join(on))] if on else []) +
                              ([_("Off: {0}").format(", ".join(off))] if off else [])), 5)
        self.fill_setup()
        absent = [a for wanted, a in changed if wanted and (a.kind == "agent" or a.commands)
                  and a.id not in getattr(self, "setup_installed", set())]
        self.note_absent(absent)

    def note_absent(self, absent):
        if absent:
            self.setup_count.set_label(ngettext(
                "{0} is on but not installed: ⋯ on its card › Install…",
                "{0} are on but not installed: ⋯ on their cards › Install…", len(absent)).format(
                ", ".join(a.name for a in absent)))

    # --- agents and local LLMs without add-ons (the Omarchy plugin) ----------------------------------

    def fill_program_sections(self, box):
        """Every agent and local LLM the app knows: the installed ones first, then the missing ones
        greyed out. Each card has Uninstall or Install in the same place."""
        names = {a.command: a.name for a in self.agent_list}
        updates = self.open_updates()
        agents = sorted(self.agent_list, key=lambda a: not a.installed)
        if agents:
            flow = self.setup_flow()
            for agent in agents:
                node_id = f"agent:{agent.command}"
                if agent.installed:
                    on_open = lambda a=agent: self.open_agent_status(a)    # noqa: E731
                else:
                    on_open = lambda a=agent: self.offer_install("agent", a.command, "install")   # noqa: E731
                flow.append(self.item_card(node_id, agent.icon, agent.name,
                                           (agent.version or _("Installed")) if agent.installed else _("Not installed"),
                                           on_open, font=agent.icon_font, update=updates.get(node_id),
                                           dim=not agent.installed, installed=agent.installed,
                                           action=self.program_button("agent", agent.command, agent.name,
                                                                      agent.installed)))
            section = self.setup_section(box, "agents", _("AI agents"), sum(1 for a in agents if a.installed),
                                         len(agents))
            section.append(flow)
        runtimes = sorted(self.inventory.runtimes, key=lambda r: not r.installed)
        if runtimes:
            flow = self.setup_flow()
            for rt in runtimes:
                node_id = f"runtime:{rt.key}"
                used = ", ".join(names.get(a, a) for a in rt.agents)
                sub = " · ".join(filter(None, [_("running") if rt.running else _("not running"), used])) \
                    if rt.installed else _("Not installed")
                flow.append(self.item_card(node_id, LOCAL_GLYPH, rt.name, sub,
                                           (lambda key=rt.key: self.open_runtime_status(key)) if rt.installed else
                                           (lambda key=rt.key: self.offer_install("runtime", key, "install")),
                                           update=updates.get(node_id), dim=not rt.installed,
                                           installed=rt.installed,
                                           action=self.program_button("runtime", rt.key, rt.name, rt.installed)))
            section = self.setup_section(box, "local", _("Local LLMs"), sum(1 for r in runtimes if r.installed),
                                         len(runtimes))
            section.append(flow)

    def program_button(self, kind: str, key: str, name: str, installed: bool) -> Gtk.Widget | None:
        """Uninstall on an installed agent's or local LLM's card, Install on a missing one's: the same
        button in the same place. Greyed out (with why) when there is no way to do it here."""
        import install_recipes
        action = "uninstall" if installed else "install"
        button = Gtk.Button(label=_("Uninstall") if installed else _("Install"))
        button.add_css_class("am-small-btn")
        button.add_css_class("am-setup-action")
        subject = self.install_subject(kind, key)
        step = install_recipes.choose(subject[3], action, subject[2]) if subject else None
        if step is None:
            button.set_sensitive(False)
            button.set_tooltip_text(_("AI Agents Map knows no way to change {0} here.").format(name))
        else:
            button.set_tooltip_text(_("Opens the install page") if step.installer else _("Shows the command first"))
            if not installed:
                button.add_css_class("suggested-action")
            button.connect("clicked", lambda *_a: self.offer_install(kind, key, action))
        return button

    # --- MCP servers, skills and models: a card for each, and one to add more -----------------------

    def fill_tool_sections(self, box):
        names = {a.command: a.name for a in self.agent_list}
        new_mcp, new_skills = self.new_found()
        updates = self.open_updates()
        outer = box
        box = self.setup_section(outer, "mcp", _("MCP servers"), len(self.inventory.mcp))
        self.note(box, _("Tools your agents can call. A server no agent uses is not on the map."))
        flow = self.setup_flow()
        for server in sorted(self.inventory.mcp, key=lambda s: s.name.lower()):
            node_id = f"mcp:{server.name}"
            users = ", ".join(names.get(a, a) for a in server.agents) or _("not used by any agent")
            key = server.name.lower()
            icon = MCP_GLYPHS.get(key) or next((g for k, g in MCP_GROUP_GLYPHS.items() if k in key), "app")
            flow.append(self.item_card(node_id, glyph(icon), server.name, users,
                                       lambda n=server.name: self.open_mcp_details(n), update=updates.get(node_id),
                                       dim=not server.agents))
        flow.append(self.add_tile(_("Add MCP server"), _("{0} new on this PC").format(len(new_mcp)) if new_mcp
                                  else _("Recommended, registry, this PC"), "app", self.open_add_mcp))
        box.append(flow)

        box = self.setup_section(outer, "skills", _("Skills"), len(self.inventory.skill_sets))
        self.note(box, _("Folders of instructions your agents read."))
        flow = self.setup_flow()
        for skill_set in sorted(self.inventory.skill_sets, key=lambda s: s.path.lower()):
            sub = ngettext("1 skill", "{0} skills", len(skill_set.skills)).format(len(skill_set.skills)) \
                + (" · " + ", ".join(names.get(a, a) for a in skill_set.agents) if skill_set.agents else "")
            flow.append(self.item_card(f"skills:{skill_set.path}", glyph("skill"), skill_set.path, sub,
                                       lambda p=skill_set.path: self.open_skills_details(p), path=True))
        flow.append(self.add_tile(_("Add skills"), _("{0} new on this PC").format(len(new_skills)) if new_skills
                                  else _("This PC or the web"), "skill", self.open_add_skills))
        box.append(flow)

    def fill_model_sections(self, box):
        """The models as the map groups them: a card per folder (and one for the online models), titled
        with the folder and, in brackets, who serves its models; folders added by hand that the map
        does not show yet get a card of their own."""
        import install_ctl
        import llm_engine
        import model_folders
        built = getattr(self.side_map, "graph", None)
        nodes = {n.id: n for n in built.nodes} if built is not None else {}
        groups: dict[str, list] = {}                      # folder ("" = online) -> model nodes, in map order
        for node in (built.column(3) if built is not None else []):
            if node.kind in ("model", "more"):
                groups.setdefault(node.group, []).append(node)
        servers: dict[str, list] = {}                     # folder -> provider nodes serving its models
        for edge in (built.edges if built is not None else []):
            model, provider = nodes.get(edge.target), nodes.get(edge.source)
            if edge.kind == "serves" and model is not None and provider is not None and model.group in groups:
                if provider not in servers.setdefault(model.group, []):
                    servers[model.group].append(provider)
        runtimes = [r for r in self.inventory.runtimes if r.installed]
        added = self.added_model_folders()
        shown = {model_folders.real(g) for g in groups if g}
        # Only folders added by hand: the local LLMs' own folders are the map's groups above.
        extra = [m for m in model_folders.main_folders(model_folders.discover([], added), [], added)
                 if m.path not in shown]
        count = sum(1 for nodes_ in groups.values() for n in nodes_ if n.kind == "model") + \
            sum(len(m.models) for m in extra)
        if not groups and not extra:
            return
        box = self.setup_section(box, "models", _("Models"), count)
        self.note(box, _("Your models as the map shows them: one card per folder, with who serves them in "
                         "brackets."))
        add = Gtk.Button(label=_("Add folder…"), halign=Gtk.Align.START)
        add.add_css_class("am-small-btn")
        add.set_tooltip_text(_("Choose or make a folder for models; it gets a card of its own here"))
        add.connect("clicked", lambda *_a: self.add_model_folder())
        box.append(add)
        for folder, models in groups.items():
            providers = servers.get(folder, [])
            title = (self.tilde_path(folder) if folder else _("Online")) + \
                (f" ({', '.join(p.label for p in providers)})" if providers else "")
            real_models = [n for n in models if n.kind == "model"]
            card = self.group_card(box, f"setup-models:{folder or 'online'}", title,
                                   summary=ngettext("1 model", "{0} models", len(real_models)).format(len(real_models)),
                                   default_open=True)
            flow = self.setup_flow()
            for node in models:
                if node.kind == "more":
                    flow.append(self.item_card(node.id, glyph("model"), node.label, "", None, dim=True, small=True))
                else:
                    flow.append(self.item_card(node.id, glyph("model"), node.label, "", None, small=True))
            # Last: a new model for a local LLM of this folder, and its folder somewhere else.
            for provider in providers:
                rt = next((r for r in runtimes if f"runtime:{r.key}" == provider.id), None)
                if rt is None or not llm_engine.of(rt).models_page:
                    continue
                if llm_engine.of(rt).own_model_folder or model_folders.reads_gguf(rt):
                    flow.append(self.add_tile(_("Add model"), _("Download for {0}").format(rt.name), "model",
                                              lambda r=rt: self.open_models(r), small=True))
                inst = install_ctl.installer(rt.key)
                if llm_engine.of(rt).own_model_folder and inst is not None and inst.installed_folder() is not None:
                    flow.append(self.add_tile(_("Model folder…"), _("Another or a new folder for {0}").format(rt.name),
                                              "folder", lambda r=rt: self.open_install(r, "models"), small=True))
            card.append(flow)
        gguf = [r for r in runtimes if model_folders.reads_gguf(r)]
        for folder in extra:                    # added by hand, not on the map (no local LLM reads it yet)
            card = self.group_card(box, f"setup-folder:{folder.path}", self.tilde_path(str(folder.path)),
                                   summary=ngettext("1 model", "{0} models", len(folder.models)).format(len(folder.models)),
                                   default_open=True)
            flow = self.setup_flow()
            for model, part in folder.models:
                flow.append(self.item_card(f"file:{part.path}/{model}", glyph("model"), model, "", None, small=True))
            if gguf:
                flow.append(self.add_tile(_("Add model"), _("Download into this folder"), "model",
                                          lambda f=folder: self.open_models(gguf[0], into=f.path), small=True))
            own = folder.own
            if own is not None and own.files:
                flow.append(self.add_tile(_("Change folder…"), _("Its models move along"), "folder",
                                          lambda f=own: self.change_model_folder(f), small=True))
            card.append(flow)

    # --- model folders: Add folder, Change folder (model_folders) -----------------------------------

    @staticmethod
    def added_model_folders() -> list:
        saved = ai_core.setting("model_folders")
        return [p for p in saved if isinstance(p, str)][:100] if isinstance(saved, list) else []

    def remember_model_folders(self, add=(), drop=()):
        import inventory
        drop = {str(Path(os.path.expanduser(p))) for p in drop}
        kept = [p for p in self.added_model_folders() if str(Path(os.path.expanduser(p))) not in drop]
        for path in add:
            text = inventory.tilde(str(path))
            if text not in kept:
                kept.append(text)
        self.save_setting(model_folders=kept)

    def pick_folder(self, title: str, start, done):
        """A folder picker (it can make new folders too); done(Path) when one is chosen."""
        dialog = Gtk.FileDialog(title=title, modal=True)
        start = Path(os.path.expanduser(str(start))) if start else None
        if start is not None and start.is_dir():
            dialog.set_initial_folder(Gio.File.new_for_path(str(start)))

        def chosen(source, result):
            try:
                folder = source.select_folder_finish(result)
            except GLib.Error:
                return                         # cancelled
            if folder is not None and folder.get_path():
                done(Path(folder.get_path()))
        dialog.select_folder(self.window, None, chosen)

    def add_model_folder(self):
        def done(path):
            import install_ctl
            try:
                install_ctl.check_folder(str(path))
                path.mkdir(parents=True, exist_ok=True)
            except (OSError, ValueError) as error:
                self.show_note(_("Not added: {0}").format(error))
                return
            self.remember_model_folders(add=[path])
            self.refresh_setup()
            self.toast(_("{0} added: its Add model downloads into it").format(path.name), 6)
        roots = [r.models_dir for r in self.inventory.runtimes if r.installed and r.models_dir]
        self.pick_folder(_("Add folder"), self.added_model_folders()[-1] if self.added_model_folders()
                         else (roots[0] if roots else "~"), done)

    def change_model_folder(self, folder):
        """Pick the new place, show what moving it does, then move (Undo afterwards)."""
        import install_ctl
        import model_folders

        def repointable(key: str) -> bool:
            inst = install_ctl.installer(key)
            return inst is not None and inst.installed_folder() is not None

        def picked(new):
            try:
                install_ctl.check_folder(str(new))
                change = model_folders.plan(folder, new, [r for r in self.inventory.runtimes if r.installed],
                                            repointable)
            except (OSError, ValueError) as error:
                self.show_note(_("Not moved: {0}").format(error))
                return
            self.confirm_folder_change(folder, new, change)
        self.pick_folder(_("Change folder"), folder.path.parent, picked)

    def confirm_folder_change(self, folder, new, change):
        import inventory
        names = {r.key: r.name for r in self.inventory.runtimes}
        move = change.move
        lines = [ngettext("1 file ({1}) moves from {2} to {3}.", "{0} files ({1}) move from {2} to {3}.",
                          len(move.steps)).format(len(move.steps), size_text(move.size),
                                                  inventory.tilde(str(folder.path)), inventory.tilde(str(new)))]
        if move.copy:
            lines.append(_("The new folder is on another disk: copied first, then removed here."))
        if change.links:
            lines.append(ngettext("1 link of a local LLM is pointed at the new place.",
                                  "{0} links of local LLMs are pointed at the new place.",
                                  len(change.links)).format(len(change.links)))
        if change.ini:
            lines.append(_("llama.cpp's models.ini names the new place (it reads it at its next start)."))
        for key in change.repoint:
            lines.append(_("{0} is pointed at the new folder.").format(names.get(key, key)))
        for key in change.lost:
            lines.append(_("{0} will not see these models any more: it reads only its own folder.").format(
                names.get(key, key)))
        dialog = Adw.AlertDialog(heading=_("Move {0}?").format(folder.path.name), body="\n".join(lines))
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("move", _("Move"))
        dialog.set_response_appearance("move", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("move")
        dialog.set_close_response("cancel")
        dialog.connect("response", lambda _d, answer: answer == "move" and self.run_folder_change(folder, new, change))
        dialog.present(self.window)

    def run_folder_change(self, folder, new, change):
        import install_ctl
        import model_folders
        import model_move
        move = change.move
        self.show_note(_("Moving {0}…").format(folder.path.name))

        def repoint(path):
            for key in change.repoint:
                install_ctl.installer(key).set_models_folder(path)

        def work():
            try:
                done = model_move.apply(move, progress=lambda f, _t: GLib.idle_add(
                    self.show_note, _("Moving {0}… {1} %").format(folder.path.name, round(f * 100))))
                problems = model_folders.fix(change)
                repoint(new)
                error = "; ".join(problems)
            except (OSError, ValueError, RuntimeError, model_move.model_layout.Cancelled) as problem:
                done, error = getattr(problem, "done", []), str(problem)
                if done:
                    model_move.undo(move, done)
                done = None
            GLib.idle_add(finished, done, error)

        def finished(done, error):
            if done is not None:
                self.remember_model_folders(add=[new], drop=[folder.path])
            self.collect()
            self.refresh_overview()
            self.refresh_map()
            self.refresh_setup()
            if done is None:
                self.show_note(_("Not moved: {0}").format(error))
            else:
                if error:
                    self.show_note(_("Moved, but: {0}").format(error))
                self.toast(_("{0} moved to {1}").format(folder.path.name, new), 10, _("Undo"),
                           lambda: self.undo_folder_change(folder, new, change, done))
            return False
        threading.Thread(target=work, daemon=True).start()

    def undo_folder_change(self, folder, new, change, done):
        import install_ctl
        import model_folders
        import model_move

        def work():
            problems = model_move.undo(change.move, done) + model_folders.fix(change, back=True)
            for key in change.repoint:
                try:
                    install_ctl.installer(key).set_models_folder(folder.path)
                except (OSError, ValueError, RuntimeError) as error:
                    problems.append(str(error))
            GLib.idle_add(finished, problems)

        def finished(problems):
            self.remember_model_folders(add=[folder.path], drop=[new])
            self.collect()
            self.refresh_overview()
            self.refresh_map()
            self.refresh_setup()
            self.show_note(_("Undo did not work: {0}").format("; ".join(problems)) if problems
                           else _("{0} is back").format(folder.path.name))
            return False
        threading.Thread(target=work, daemon=True).start()

    def setup_section(self, box, key: str, title: str, count: int | None, total: int | None = None,
                      closed: bool = False) -> Gtk.Box:
        """A section heading that folds the section away and back (a click; remembered in the
        setting setup_collapsed; closed=True: folded until unfolded), and the box the section's notes
        and cards go into."""
        saved = set(self.setup_folded())
        # Folded by default (closed): remembered as "<key>:open" once unfolded; else as "<key>" once folded.
        folded = f"{key}:open" not in saved if closed else key in saved
        head = Gtk.Button(halign=Gtk.Align.FILL, hexpand=True)
        head.add_css_class("flat")
        head.add_css_class("am-setup-section")
        row = Gtk.Box(spacing=8)
        arrow = Gtk.Image.new_from_icon_name("pan-end-symbolic" if folded else "pan-down-symbolic")
        row.append(arrow)
        label = Gtk.Label(label=title.upper(), xalign=0, valign=Gtk.Align.CENTER)   # the group cards' heading
        label.add_css_class("am-setting-heading")
        row.append(label)
        if count is not None:
            number = Gtk.Label(label=str(count) if total is None or total == count else
                               _("{0} of {1} installed").format(count, total), valign=Gtk.Align.CENTER)
            number.add_css_class("am-agent-detail")
            row.append(number)
        head.set_child(row)
        head.set_tooltip_text(_("Fold or unfold"))
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP, visible=not folded)

        def toggle(_button):
            shown = not body.get_visible()
            body.set_visible(shown)
            arrow.set_from_icon_name("pan-down-symbolic" if shown else "pan-end-symbolic")
            now = set(self.setup_folded())
            if closed:
                (now.add if shown else now.discard)(f"{key}:open")
            else:
                (now.discard if shown else now.add)(key)
            self.save_setting(setup_collapsed=sorted(now))

        head.connect("clicked", toggle)
        if key == "explain":                   # already inside its own card
            box.append(head)
            box.append(body)
            return body
        # A category: a group card holding its heading and its content.
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP)
        card.add_css_class("am-help-card")
        card.add_css_class("am-group-card")
        card.add_css_class("am-setup-card")
        card.append(head)
        card.append(body)
        box.append(card)
        return body

    def setup_problems(self) -> dict:
        """{card's node id: what needs fixing there} from the last health check: an agent's problems
        on its card, a local LLM's on its card."""
        report = getattr(self, "health_report", None)
        found = {}
        for area in report.areas if report is not None else ():
            for finding in area.findings:
                if finding.level != "warn" or not finding.action:
                    continue
                node = {"details": "agent:", "runtime": "runtime:"}.get(finding.action[0])
                if node and len(finding.action) > 1:
                    found.setdefault(node + finding.action[1], finding.title)
        return found

    def battery_badge(self, node_id: str) -> Gtk.Widget | None:
        """An online agent's limits, or a running local LLM's free GPU memory, as a small battery
        (the map's box has the same)."""
        level = getattr(self, "batteries", {}).get(node_id) if node_id else None
        if level is None:
            return None
        from ui_helpers import Battery
        return Battery(self.side_map.color, *level)

    def problem_badge(self, node_id: str) -> Gtk.Widget | None:
        """A red ! on a card with an open problem (the header's ! lists them all)."""
        title = self.setup_problems().get(node_id) if node_id else None
        if not title:
            return None
        badge = Gtk.Label(label="!", valign=Gtk.Align.START)
        badge.add_css_class("am-setup-problem")
        badge.set_tooltip_text(title)
        return badge

    def refill_setup_health(self):
        """A new health check on Setup: its cards again when their problems changed."""
        if set(self.setup_problems()) != getattr(self, "setup_problem_keys", set()):
            self.refresh_setup()
        self.fill_setup_uses()

    def setup_uses(self, box):
        """What the setup is good for (everyday help, coding, images, speech, research): one tile each
        with what is there and the next steps; folded, its line says how many are ready."""
        self.uses_card = self.group_card(box, "setup-uses", _("What you can do with this"))
        self.fill_setup_uses()

    def fill_setup_uses(self):
        content = getattr(self, "uses_card", None)
        if content is None:
            return
        while (child := content.get_first_child()) is not None:
            content.remove(child)
        report = getattr(self, "health_report", None)
        if report is None:
            self.note(content, _("Looking at the setup…"))
            if not getattr(self, "health_running", False):
                self.run_health()
            return
        ready = sum(1 for use in report.uses if use.level == "ready")
        content.group_summary.set_label(_("{0} of {1} ready").format(ready, len(report.uses)))
        tiles = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=3, min_children_per_line=1,
                            column_spacing=ai_core.GAP, row_spacing=ai_core.GAP, homogeneous=True)
        for use in report.uses:
            tiles.append(self.use_tile(use))
        content.append(tiles)

    def setup_explainer(self) -> Gtk.Widget:
        """The card on top of Setup, for someone new: what puts an agent, a provider, an MCP server
        or a model on the map, as numbered steps (folded until opened, like a section)."""
        if self.with_addons():
            first = (_("Switch on what you use"),
                     _("Every agent and local LLM Asterism knows is a card below. A blue frame means it is on "
                       "this PC: tick its card and Apply, and it is on the map. A program on this PC without "
                       "an add-on gets one suggested."))
        else:
            first = (_("Everything on this PC is here"),
                     _("Every agent and local LLM the app knows is a card below. A blue frame means it is on "
                       "this PC and on the map; a grey card is not installed."))
        steps = [first,
                 (_("Install what is missing"),
                  _("A grey card is not on this PC yet: ⋯ on it › Install… runs the installer in a terminal you "
                    "can watch.")),
                 (_("Sign in to an online agent"),
                  _("Sign in… on an agent's card starts its own login for a subscription, or puts your API key "
                    "into the agent's own settings, readable only by you.")),
                 (_("Connect by drawing"),
                  _("On the map, drag from an agent to a local LLM, an MCP server or a skill folder. You see "
                    "every change before it is written, and can undo it.")),
                 (_("What shows up by itself"),
                  _("Local LLMs as soon as they are installed; online providers once an agent is signed in; MCP "
                    "servers and models as soon as an agent uses them or a local LLM has them."))]
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        card.add_css_class("am-setup-explain")
        body = self.setup_section(card, "explain", _("How things get onto your map"), None, closed=True)
        card.get_first_child().set_margin_top(0)
        body.set_spacing(14)
        body.set_margin_start(8)
        body.set_margin_top(10)
        body.set_margin_bottom(12)
        for number, (title, text) in enumerate(steps, 1):
            step = Gtk.Box(spacing=14)
            badge = Gtk.Label(label=str(number), valign=Gtk.Align.START)
            badge.add_css_class("am-step-number")
            step.append(badge)
            words = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3, hexpand=True)
            heading = Gtk.Label(label=title, xalign=0, wrap=True, max_width_chars=1)
            heading.add_css_class("am-step-title")
            words.append(heading)
            line = Gtk.Label(label=text, xalign=0, wrap=True, max_width_chars=1)
            line.add_css_class("am-step-text")
            words.append(line)
            step.append(words)
            body.append(step)
        return card

    @staticmethod
    def setup_folded() -> list:
        folded = ai_core.setting("setup_collapsed")
        return [k for k in folded if isinstance(k, str)] if isinstance(folded, list) else []

    @staticmethod
    def tilde_path(path: str) -> str:
        from pathlib import Path
        home = str(Path.home())
        return "~" + path[len(home):] if path and path.startswith(home) else path or ""

    # --- the cards ----------------------------------------------------------------------------------

    def setup_glyph(self, text: str, font: str = "", small: bool = False) -> Gtk.Widget:
        from ui_helpers import glyph_widget
        icon = glyph_widget(text, font, 18 if small else 28,
                            ("am-agent-icon", "am-setup-glyph-sm" if small else "am-setup-glyph"))
        icon.set_halign(Gtk.Align.START)
        icon.set_hexpand(True)
        return icon

    @staticmethod
    def tile_name(text: str, path: bool = False, lines: int = 1) -> Gtk.Label:
        name = Gtk.Label(label=text, xalign=0, ellipsize=1 if path else 3, max_width_chars=1)
        if lines > 1:
            # Long model file names: two lines, broken anywhere, before the end is cut.
            name.set_wrap(True)
            name.set_wrap_mode(2)
            name.set_lines(lines)
            name.set_margin_end(18)         # the last line ends before ⋯
        name.add_css_class("am-addon-name")
        return name

    @staticmethod
    def tile_sub(text: str, room: bool = True) -> Gtk.Label:
        sub = Gtk.Label(label=text, xalign=0, ellipsize=3, max_width_chars=1, margin_end=18 if room else 0)   # ⋯
        sub.add_css_class("am-agent-detail")
        return sub

    @staticmethod
    def update_badge(text: str) -> Gtk.Label:
        badge = Gtk.Label(label="↑ " + text, valign=Gtk.Align.START, ellipsize=3, max_width_chars=14)
        badge.add_css_class("am-setup-update")
        return badge

    def item_card(self, node_id: str, icon: str, title: str, sub: str, on_open, font: str = "", update=None,
                  dim: bool = False, small: bool = False, path: bool = False,
                  action: Gtk.Widget | None = None, installed: bool = False) -> Gtk.Widget:
        """A card of something installed: a click opens its details (no details: its menu), ⋯ and a
        right-click its menu, the one its box on the map has."""
        card = Gtk.Button()
        card.add_css_class("am-setup-tile")
        if small:
            card.add_css_class("am-setup-small")
        if dim:
            card.add_css_class("am-setup-absent")
        elif installed:
            card.add_css_class("am-setup-installed")
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        top = Gtk.Box(spacing=8)
        top.append(self.setup_glyph(icon, font, small))
        if update is not None:
            top.append(self.update_badge(update.available))
        problem = self.problem_badge(node_id)
        if problem is not None:
            top.append(problem)
        battery = self.battery_badge(node_id)
        if battery is not None:
            top.append(battery)
        body.append(top)
        body.append(self.tile_name(title, path, 2 if small else 1))
        if sub:
            body.append(self.tile_sub(sub))
        if action is not None:
            body.append(Gtk.Box(height_request=30))        # room for the button over the card
        card.set_child(body)
        card.set_tooltip_text(title if update is None else
                              _("{0} · version {1} is available (you have {2})").format(
                                  title, update.available, update.current or "?"))
        menu = lambda widget, rect: self.show_context(widget, node_id, rect)   # noqa: E731
        if on_open is not None:
            card.connect("clicked", lambda *_a: on_open())
        else:
            card.connect("clicked", lambda b: menu(b, (0, 0, b.get_width(), b.get_height())))
        overlay = self.with_menu(card, menu)
        if action is not None:
            action.set_halign(Gtk.Align.START)
            action.set_valign(Gtk.Align.END)
            action.set_margin_start(14)
            action.set_margin_bottom(10)
            overlay.add_overlay(action)
        return overlay

    def add_tile(self, title: str, sub: str, icon: str, on_click, small: bool = False) -> Gtk.Widget:
        """The dashed card at the end of a section: add one."""
        card = Gtk.Button()
        card.add_css_class("am-setup-tile")
        card.add_css_class("am-setup-add")
        if small:
            card.add_css_class("am-setup-small")
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        top = Gtk.Box(spacing=8)
        top.append(self.setup_glyph("+", small=small))
        body.append(top)
        body.append(self.tile_name(title))
        body.append(self.tile_sub(sub, room=False))
        card.set_child(body)
        card.set_tooltip_text(sub)
        card.connect("clicked", lambda *_a: on_click())
        return card

    def with_menu(self, card: Gtk.Widget, menu) -> Gtk.Widget:
        """The card with a ⋯ button in its corner; a right-click on the card does the same.
        menu(widget, rectangle) opens the menu."""
        overlay = Gtk.Overlay(child=card)
        more = Gtk.Button(label="⋯", halign=Gtk.Align.END, valign=Gtk.Align.END, margin_end=6, margin_bottom=6)
        more.add_css_class("flat")
        more.add_css_class("am-setup-more")
        more.set_tooltip_text(_("Update, start, details, remove…"))
        more.connect("clicked", lambda b: menu(b, (0, 0, b.get_width(), b.get_height())))
        overlay.add_overlay(more)
        click = Gtk.GestureClick(button=Gdk.BUTTON_SECONDARY)
        click.connect("pressed", lambda _g, _n, x, y: menu(overlay, (x, y, 1, 1)))
        overlay.add_controller(click)
        return overlay

    # --- updates --------------------------------------------------------------------------------

    def open_updates(self) -> dict:
        """Box id -> the update waiting for it: agents and local LLMs (agent_updates), MCP servers
        whose app has a newer version on this PC."""
        found = {}
        for update in agent_updates.still_open(getattr(self, "updates", []), self.agent_list,
                                               self.inventory.runtimes):
            found[f"{update.kind}:{update.key}"] = update
        for candidate in self.mcp_updates():
            found.setdefault(f"mcp:{candidate.name}", agent_updates.Update(
                "mcp", candidate.name, candidate.name, candidate.update_from or "", candidate.version or ""))
        return found

    def item_update(self, node_id: str):
        """(menu label, action) of the update waiting for a box, or None. Our own installs of local
        LLMs and agents have Update… in their menu already."""
        if node_id.startswith("mcp:"):
            candidate = next((c for c in self.mcp_updates() if f"mcp:{c.name}" == node_id), None)
            return (_("Update to {0}…").format(candidate.version), lambda: self.open_mcp_update(candidate)) \
                if candidate is not None else None
        update = self.open_updates().get(node_id)
        if update is None or update.page:
            return None
        return _("Update to {0}").format(update.available), lambda: self.run_update(update)

    def on_setup_shown(self):
        """Back on Setup from a page opened from a card: the cards again (it may have changed something)."""
        if self.stack.get_visible_child_name() != "setup" or self.setup_welcome:
            return
        if getattr(self, "setup_just_filled", False):
            self.setup_just_filled = False          # opened just now: filled already
            return
        GLib.idle_add(self.refresh_setup)
