"""AI Agents Map: the MCP server details and Add MCP server pages (a mixin of ai_popup.AgentsMap,
split off to keep ai_popup.py small; its methods use the window's state through self)."""

from gi.repository import Adw, GLib, Gtk, Pango

import ai_core
import connect
import harnesses
import mcp_catalog
import mcp_ctl
import mcp_library
import mcp_registry
import mcp_scan
import mcp_tools
import mcp_update
import os
import re
import shutil
import skill_update
import skills_ctl
import subprocess
import threading
import urllib.parse
import web_sources
from i18n import _, N_, ngettext
from pathlib import Path
from ui_helpers import glyph_label, MCP_GLYPHS, MCP_GROUP_GLYPHS, pick_cards, RUN_GLYPHS
from window_rules import MODEL_LIST_WIDTH


def agent_name(command: str) -> str:
    harness = harnesses.get(command)
    return harness.name if harness is not None else command



def registry_range(page: int, shown: int, total, more: bool) -> str:
    """"21 – 40 / 200"; without a total (the registry's own API): "21 – 40 / 40+" while there are more."""
    first = page * mcp_registry.PAGE + 1 if shown else 0
    last = page * mcp_registry.PAGE + shown
    of = str(total) if total is not None else f"{last}+" if more else str(last)
    return f"{first} – {last} / {of}"

class McpPages:
    # --- MCP server details (the (i) on its card) ---------------------------------------------------

    def build_mcp_page(self) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP)
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
        self.mcp_body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP)
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
        names.update(mcp_ctl.llm_clients())
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
        harness = harnesses.get(command)
        file = skills_ctl.tilde(files[0]) if files else harness.mcp.path if harness and harness.mcp else ""
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
                    " / ".join(agent_name(c) for c in skill_update.writers(self.installed_commands())
                               or [h.command for h in harnesses.all_harnesses() if h.rewrite])))
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
                " / ".join(agent_name(c) for c in skill_update.writers(installed))))
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
        self.connect_reset()
        form = self.connect_form
        self.connect_status.set_label("")
        self.connect_preview_heading.set_visible(True)
        self.connect_preview_scroller.set_visible(True)
        self.connect_ok.set_visible(True)
        self.connect_ok.set_label(_("Update"))
        self.connect_remove.set_visible(False)
        self.connect_intro.set_label(_(
            "{0} rewrote {1} to match the tools {2} offers now ({3}). Read the change before you take it: it "
            "was written by an AI. Undo brings the old skill back.").format(
            agent_name(writer), skills_ctl.tilde(file),
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

    ADD_MCP_TABS = (("installed", N_("Installed")), ("picks", N_("Recommended")), ("web", N_("MCP registry")),
                    ("local", N_("On this PC")), ("hand", N_("By hand")))
    # How a server runs, as a tag on its row (the models page's capability chips): text, colour, hint.
    RUN_CHIPS = {"npm": (N_("Program"), "program", N_("Runs on this PC (Node.js).")),
                 "pypi": (N_("Program"), "program", N_("Runs on this PC (Python, uv).")),
                 "oci": ("Docker", "docker", N_("Runs on this PC in a Docker container.")),
                 "remote": (N_("Web"), "web", N_("A service on the web: nothing is installed.")),
                 "command": (N_("Program"), "program", N_("Runs on this PC.")),
                 "url": (N_("Web"), "web", N_("A service on the web."))}
    RUN_TEXT = {
        "npm": N_("Runs on this PC with Node.js. npx downloads {0} on its first start and keeps it in its cache."),
        "pypi": N_("Runs on this PC with uv. uvx downloads {0} on its first start into an environment of its own."),
        "oci": N_("Runs on this PC in a Docker container ({0}), apart from your files unless you share a folder."),
        "remote": N_("Runs at {0}. Nothing is installed; what the agents send to it leaves your PC."),
    }
    RUN_SHORT = {"npm": "npm", "pypi": "Python", "oci": "Docker", "remote": N_("Web")}
    RUN_KIND = {"npm": N_("Program · npm"), "pypi": N_("Program · Python"), "oci": N_("Program · Docker"),
                "remote": N_("Web service")}
    RUNNERS = {"npx": N_("Node.js (npx) is not installed yet: sudo pacman -S nodejs npm"),
               "uvx": N_("uv (uvx) is not installed yet: sudo pacman -S uv"),
               "docker": N_("Docker is not installed yet: sudo pacman -S docker")}

    def build_add_mcp_page(self) -> Gtk.Widget:
        """Laid out like the models page: tabs (Installed, Recommended, the MCP registry, On this
        PC, By hand), a list with tags on the left, the chosen server's page on the right (facts,
        how it runs, what it needs, the agents that get it), Test and Add in the bar below."""
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP)
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
        back.set_tooltip_text(_("Back without changing anything (Backspace, Esc)"))
        back.connect("clicked", lambda *_: self.close_add_mcp())
        header.append(back)
        title = Gtk.Label(label=_("MCP servers").upper(), xalign=0)
        title.add_css_class("am-section-title")
        header.append(title)
        # One line, as the models page's model folder; the whole explanation on hover.
        about = Gtk.Label(label=_("New tools for your agents: files, web pages, a browser, GitHub…"), xalign=0,
                          hexpand=True, ellipsize=Pango.EllipsizeMode.END)
        about.add_css_class("am-agent-detail")
        about.set_tooltip_text("\n\n".join([
            _("A server can read your files, open web pages, drive a browser or work with GitHub. It is a small "
              "program on this PC or a service on the web; the agents start or call it when a task needs it."),
            _("Pick a server, fill in what it needs (a folder, a key), tick the agents, then Test and Add. "
              "To give it to more agents later, draw a line on the map from the agent to the server.")]))
        header.append(about)
        page.append(header)
        rule = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        rule.add_css_class("am-settings-rule")
        page.append(rule)

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
            detail = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP)
            scroller = self.scrolled(detail, hexpand=True)
            pane.append(scroller)
            self.add_details[key] = (scroller, detail)
            self.add_stack.add_named(pane, key)

        # Installed: the servers the agents have (and the list keeps), each with its page.
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP)
        self.add_installed_heading = Gtk.Label(xalign=0)
        self.add_installed_heading.add_css_class("am-group-heading")
        left.append(self.add_installed_heading)
        self.add_installed = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.add_installed.add_css_class("am-list")
        pick_cards(self.add_installed)
        self.add_installed.connect("row-selected",
                                   lambda _l, row: row is not None and self.show_installed_mcp(row.server))
        left.append(self.scrolled(self.add_installed))
        two_panes("installed", left)

        # Recommended: the reference servers and official ones of well-known makers.
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP)
        self.add_picks = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.add_picks.add_css_class("am-list")
        pick_cards(self.add_picks)
        self.add_picks.connect("row-selected", lambda _l, row: row is not None and self.show_pick(row.pick))
        left.append(self.scrolled(self.add_picks))
        two_panes("picks", left)

        # The MCP registry (or a registry the user added).
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP)
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
        # One page of 20 at a time, paged in one line: ‹ Previous 20 · 21 – 40 / 200 · Next 20 ›
        self.registry_pager = Gtk.CenterBox(visible=False)
        self.registry_prev = Gtk.Button(label=_("‹ Previous {0}").format(mcp_registry.PAGE))
        self.registry_next = Gtk.Button(label=_("Next {0} ›").format(mcp_registry.PAGE))
        for button, step in ((self.registry_prev, -1), (self.registry_next, 1)):
            button.add_css_class("am-small-btn")
            button.connect("clicked", lambda *_a, s=step: self.search_mcp(page=self.registry_page + s))
        self.registry_range = Gtk.Label()
        self.registry_range.add_css_class("am-agent-detail")
        self.registry_pager.set_start_widget(self.registry_prev)
        self.registry_pager.set_center_widget(self.registry_range)
        self.registry_pager.set_end_widget(self.registry_next)
        left.append(self.registry_pager)
        self.registry_cursors, self.registry_page = [""], 0
        two_panes("web", left)

        # On this PC: programs that offer an MCP server and are not set up yet.
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP)
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
        hand = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP)
        self.note(hand, _("For a server you know the command or address of, e.g. from its README."))
        hand_scroller = self.scrolled(hand, hexpand=True)
        self.add_details["hand"] = (hand_scroller, hand)
        self.add_stack.add_named(hand_scroller, "hand")

        # The setup, moved to the page shown: what it needs, the agents, the command, Test / Add.
        self.add_setup = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP)
        self.add_form_heading = Gtk.Label(label=_("Set it up"), xalign=0)
        self.add_form_heading.add_css_class("am-group-heading")
        self.add_setup.append(self.add_form_heading)
        self.add_needs = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP)
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
        self.add_form = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP)
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

        changes = Gtk.Label(label=_("What changes"), xalign=0)
        changes.add_css_class("am-group-heading")
        self.add_setup.append(changes)
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

        # Below every tab, as on the models page: what happened, Test and Add.
        rule = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        rule.add_css_class("am-settings-rule")
        page.append(rule)
        self.add_bar = Gtk.Box(spacing=8)
        self.add_status = Gtk.Label(xalign=0, wrap=True, max_width_chars=1, hexpand=True)
        self.add_status.add_css_class("am-agent-detail")
        self.add_bar.append(self.add_status)
        self.add_test = Gtk.Button(label=_("Test"), valign=Gtk.Align.CENTER, sensitive=False)   # update_add_test
        self.add_test.add_css_class("am-action-btn")
        self.add_test.set_tooltip_text(_("Start it once and ask for its tools"))
        self.add_test.connect("clicked", lambda *_: self.test_new_mcp())
        self.add_bar.append(self.add_test)
        self.add_ok = Gtk.Button(label=_("Add"), valign=Gtk.Align.CENTER)
        self.add_ok.add_css_class("am-primary")
        self.add_ok.add_css_class("am-action-btn")
        self.add_ok.connect("clicked", lambda *_: self.apply_new_mcp())
        self.add_bar.append(self.add_ok)
        page.append(self.add_bar)
        self.fill_picks()
        return page

    def add_where(self) -> str:
        stored = ai_core.setting("add_where")
        value = stored.get("mcp") if isinstance(stored, dict) else None
        return value if value in self.add_tab_buttons else "picks"

    def show_add_tab(self, key: str, save: bool = False):
        self.add_tab = key
        self.add_stack.set_visible_child_name(key)
        # Installed has nothing to add: the bar keeps only its status line there.
        self.add_test.set_visible(key != "installed")
        self.add_ok.set_visible(key != "installed")
        if save:
            stored = ai_core.setting("add_where")
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
        if key == "installed":
            self.add_status.set_label("")          # the form's hints belong to the other tabs

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
                                     chips=self.run_chips(self.pick_kinds(pick), self.pick_needs_key(pick)),
                                     icon=MCP_GLYPHS.get(pick.key, MCP_GROUP_GLYPHS.get(group, "package")))
                row.pick = pick
                box.append(row)

    # Tags and the Installed tab ----------------------------------------------------------------

    def tag_chip(self, text: str, tone: str, tip: str = "") -> Gtk.Widget:
        chip = Gtk.Label(label=text)
        chip.add_css_class("am-cap")
        chip.add_css_class(f"am-cap-{tone}")
        if tip:
            chip.set_tooltip_text(tip)
        return chip

    def run_chips(self, kinds, needs_key: bool = False) -> list:
        """How it runs (Program, Docker, Web; each once) and Key when it needs one."""
        chips, seen = [], set()
        for kind in kinds:
            text, tone, tip = self.RUN_CHIPS.get(kind, ("", "", ""))
            if text and text not in seen:
                seen.add(text)
                chips.append(self.tag_chip(_(text), tone, _(tip)))
        if needs_key:
            chips.append(self.tag_chip(_("Key"), "key", _("Needs a key or token of yours.")))
        return chips

    @staticmethod
    def pick_kinds(pick) -> list:
        kinds = [p.get("registryType", "") for p in pick.spec.get("packages", [])]
        return kinds + (["remote"] if pick.spec.get("remotes") else [])

    @staticmethod
    def pick_needs_key(pick) -> bool:
        secret = any(v.get("isSecret") for p in pick.spec.get("packages", [])
                     for v in p.get("environmentVariables", []))
        return bool(pick.key_url) or secret

    def fill_installed_mcp(self):
        listbox = self.add_installed
        self.clear_box(listbox)
        servers = sorted(getattr(self, "inventory", None) and self.inventory.mcp or [], key=lambda m: m.name)
        self.add_installed_heading.set_label(ngettext("1 server set up", "{0} servers set up", len(servers))
                                             .format(len(servers)).upper())
        names = self.agent_names()
        for server in servers:
            users = [names.get(a, a) for a in server.agents]
            tools = mcp_tools.cached(server.name) or {}
            count = len(tools.get("tools", [])) if tools.get("status") == "ok" else 0
            right = ngettext("1 tool", "{0} tools", count).format(count) if count else ""
            row = self.model_row(server.name, ", ".join(users) if users else _("not used by any agent"),
                                 chips=self.run_chips([server.kind]), right=right,
                                 icon=MCP_GLYPHS.get(server.name, RUN_GLYPHS.get(
                                     "remote" if server.kind == "url" else "npm", "package")))
            row.server = server
            listbox.append(row)
        if not servers:
            self.add_empty("installed", _("No MCP server is set up yet: pick one under Recommended."))

    def show_installed_mcp(self, server):
        """A server the agents have: what it is, who uses it, its tools; Details and Remove."""
        _scroller, box = self.add_details["installed"]
        if self.add_setup.get_parent() is box:
            box.remove(self.add_setup)
        self.clear_box(box)
        title = Gtk.Label(label=server.name, xalign=0, wrap=True, max_width_chars=1, selectable=True)
        title.add_css_class("am-model-title")
        box.append(title)
        names = self.agent_names()
        users = [names.get(a, a) for a in server.agents]
        tools = mcp_tools.cached(server.name) or {}
        count = len(tools.get("tools", [])) if tools.get("status") == "ok" else None
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP)
        card.add_css_class("am-model-card")
        self.facts_grid(card, [(_("Runs as"), _(self.RUN_CHIPS.get(server.kind, ("?",))[0])),
                               (_("Agents"), str(len(users))),
                               (_("Tools"), str(count) if count is not None else "–")])
        what = Gtk.Label(label=(_("Program: {0}") if server.kind == "command" else _("Address: {0}"))
                         .format(server.target), xalign=0, wrap=True, max_width_chars=1, selectable=True)
        what.add_css_class("am-detail-value")
        card.append(what)
        box.append(card)
        self.detail_heading(box, _("Used by"))
        self.note(box, ", ".join(users) if users else _("No agent uses it: it stays in the list until you remove "
                                                      "it. Draw a line on the map to give it to an agent."))
        if count is not None:
            self.detail_heading(box, _("Tools"))
            self.note(box, ", ".join(str(t.get("name", "")) for t in tools.get("tools", [])[:40]
                                     if isinstance(t, dict)) or _("none"))
        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        details = Gtk.Button(label=_("Details…"))
        details.add_css_class("am-small-btn")
        details.connect("clicked", lambda *_: self.open_mcp_details(server.name))
        buttons.append(details)
        remove = Gtk.Button(label=_("Remove…"))
        remove.add_css_class("am-small-btn")
        remove.add_css_class("am-danger")
        remove.set_tooltip_text(_("Take it from every agent and from the list"))
        remove.connect("clicked", lambda *_: self.open_mcp_delete(server.name))
        buttons.append(remove)
        box.append(buttons)

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
        self.fill_installed_mcp()
        for key in ("installed", "picks", "web", "local"):
            if self.add_setup.get_parent() is not self.add_details[key][1]:
                if not self.add_details[key][1].get_first_child():
                    self.add_empty(key, {
                        "installed": _("Pick a server on the left to see what it is, which agents use it and "
                                       "its tools."),
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
        # Who made it (and how much that is checked), when it changed, and its page on the web:
        # one line, as the models page's downloads · updated · Open on the web.
        meta = Gtk.Box(spacing=10)
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
        meta.append(chip)
        if server.updated:
            label = Gtk.Label(label=_("updated {0}").format(self.age_text(self.iso_time(server.updated))))
            label.add_css_class("am-agent-detail")
            meta.append(label)
        meta.append(Gtk.Box(hexpand=True))
        for uri, label in ((server.website, _("Website")), (server.repository, _("Open on the web"))):
            if uri.startswith("https://"):
                link = Gtk.LinkButton(uri=uri, label=label)
                link.set_tooltip_text(uri)
                meta.append(link)
        box.append(meta)
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

        # The facts card: big values on top (as a model's parameters, context, format), the tags,
        # then what it does, what it needs and what to ask an agent.
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP)
        card.add_css_class("am-model-card")
        kinds = list(dict.fromkeys(_(self.RUN_CHIPS[o.kind][0]) for o in server.options if o.kind in self.RUN_CHIPS))
        needs = sorted({n.name for o in server.options for n in o.needs if n.required})
        secret = any(n.required and n.secret for o in server.options for n in o.needs)
        self.facts_grid(card, [(_("Runs as"), " | ".join(kinds)), (_("Version"), server.version),
                               (_("Ways to run"), str(len(server.options)) if len(server.options) > 1 else "")])
        if secret or (pick is not None and self.pick_needs_key(pick)):
            chips = Gtk.Box(spacing=6)          # how it runs is in the facts above: only what it adds
            for chip in self.run_chips([], True):
                chips.append(chip)
            card.append(chips)
        about = Gtk.Label(label=_(pick.about) if pick else server.description or _("No description."),
                          xalign=0, wrap=True, max_width_chars=1, selectable=True)
        about.add_css_class("am-detail-value")
        card.append(about)
        need_line = Gtk.Box(spacing=8)
        key_label = Gtk.Label(label=_("Needs").upper(), xalign=0, valign=Gtk.Align.START)
        key_label.add_css_class("am-fact-key")
        need_line.append(key_label)
        value_label = Gtk.Label(label=_(pick.needs) if pick else (", ".join(needs) if needs else
                                                                  _("nothing to fill in")),
                                xalign=0, wrap=True, max_width_chars=1, hexpand=True, selectable=True)
        value_label.add_css_class("am-detail-value")
        need_line.append(value_label)
        card.append(need_line)
        if pick is not None and pick.tries:
            tries = Gtk.Label(label=_("Then ask an agent, e.g.:") + "\n" + "\n".join(
                "“" + _(t) + "”" for t in pick.tries), xalign=0, wrap=True, max_width_chars=1, selectable=True)
            tries.add_css_class("am-agent-detail")
            card.append(tries)
        box.append(card)

        # How it runs: one line per way (as the download options), the best one chosen.
        self.detail_heading(box, _("How it runs"))
        options = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        options.add_css_class("am-list")
        options.add_css_class("boxed-list")
        best = None
        rows = [self.run_option_row(option, server) for option in server.options[:mcp_registry.MAX_OPTIONS]]
        best = next((row for row in rows if row.option.runnable), None)
        for row in rows:
            if row is best and len(rows) > 1:
                row.badge.set_visible(True)
            options.append(row)
        box.append(options)
        self.add_server, self.add_options = server, options
        self.add_pages[key] = (server, pick)
        options.connect("row-selected", lambda _l, row: row is not None and self.use_registry(
            server, row.option, pick=pick))
        self.place_setup(key)
        self.add_advanced.set_expanded(False)
        options.select_row(best or options.get_row_at_index(0))

    def run_option_row(self, option, server) -> Gtk.ListBoxRow:
        """One way to run it on one line, as a download option: the kind, what that means, the
        package or host at the right; a line under it when its runner is missing."""
        row = Gtk.ListBoxRow()
        column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin_start=10, margin_end=10,
                         margin_top=5, margin_bottom=5)
        line = Gtk.Box(spacing=10)
        kind = Gtk.Label(label=_(self.RUN_SHORT[option.kind]), xalign=0, width_chars=7)
        kind.add_css_class("am-name-part")
        kind.set_tooltip_text(_(self.RUN_KIND[option.kind]))
        line.append(kind)
        if option.kind == "remote":
            ident = urllib.parse.urlsplit(option.entry["url"]).hostname or ""
            text = _(self.RUN_TEXT["remote"]).format(ident)
        else:
            ident = option.label.split(" · ", 1)[-1]
            text = _(self.RUN_TEXT[option.kind]).format(ident)
        explain = Gtk.Label(label=text, xalign=0, hexpand=True)
        explain.set_ellipsize(Pango.EllipsizeMode.END)
        explain.set_max_width_chars(1)
        explain.add_css_class("am-agent-detail")
        line.append(explain)
        row.badge = Gtk.Label(label=_("recommended"), visible=False)
        row.badge.add_css_class("am-agent-badge")
        line.append(row.badge)
        what = Gtk.Label(label=ident, xalign=1)
        what.set_ellipsize(Pango.EllipsizeMode.START)
        what.set_max_width_chars(28)
        what.add_css_class("am-agent-detail")
        line.append(what)
        column.append(line)
        if not option.runnable:
            missing = Gtk.Label(label=_(self.RUNNERS.get(option.entry["command"], N_("Needs {0}, not installed")))
                                .format(option.entry["command"]), xalign=0, wrap=True, max_width_chars=1,
                                selectable=True)
            missing.add_css_class("am-warning-text")
            column.append(missing)
        row.set_child(column)
        row.set_tooltip_text(text)
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
        seen_found = ai_core.setting("seen_found")
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
                if self.page_name() != "addmcp":            # started from a hint or Smart Check
                    self.toast(_("Every app with an MCP server you use is up to date."), 6)
            self.refresh_overview()
            if self.page_name() == "smartcheck":
                self.fill_smart_check()
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
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP)
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

    def search_mcp(self, page: int = 0):
        """The registry's servers for the query, one page of 20 at a time (page 0: a new search).
        Each page's cursor is kept, so Previous goes back without the registry's help."""
        query = self.add_query.get_text().strip()
        if not query:
            return
        if page == 0:
            self.registry_cursors = [""]
            if self.add_pages.pop("web", None) is not None:     # a new list: nothing picked from it yet
                self.add_server = self.add_from = None
                self.add_empty("web", _("Pick a server on the left to see who published it, what it needs and "
                                        "how it runs."))
                self.update_add_test()
            self.registry_pager.set_visible(False)
        if not 0 <= page < len(self.registry_cursors):
            return
        cursor = self.registry_cursors[page]
        self.add_web_status.set_label(_("Searching… The registry often takes 10 seconds or more."))
        base = web_sources.chosen("mcp")
        self.registry_prev.set_sensitive(False)
        self.registry_next.set_sensitive(False)

        def work():
            try:
                result, following, total = mcp_registry.search_page(
                    query, base=base if base != web_sources.DEFAULTS["mcp"] else "", cursor=cursor)
                error = ""
            except (OSError, ValueError) as failure:
                result, following, total, error = [], "", None, str(failure)
            GLib.idle_add(done, result, following, total, error)

        def done(result, following, total, error):
            box = self.add_web_list
            if self.add_query.get_text().strip() != query:
                return False                 # a newer search runs
            if error:
                self.add_web_status.set_label(_("Search failed: {0}").format(error))
                self.update_registry_pager(box.observe_children().get_n_items(), total)   # the old page stays
                return False
            self.clear_box(box)
            self.registry_page = page
            del self.registry_cursors[page + 1:]
            if following:
                self.registry_cursors.append(following)
            result = [s for s in result if s.status in ("", "active")] or result
            if total is not None:
                self.add_web_status.set_label(ngettext("1 server found.", "{0} servers found.", total).format(total))
            elif not result:
                self.add_web_status.set_label(_("No server found."))
            else:
                self.add_web_status.set_label(_("Servers found: {0}").format(_("more than {0}").format(
                    page * mcp_registry.PAGE + len(result)) if following else page * mcp_registry.PAGE + len(result)))
            for server in result[:mcp_registry.PAGE]:
                secret = any(n.required and getattr(n, "secret", False) for o in server.options for n in o.needs)
                row = self.model_row(server.title or server.short, server.description or server.name,
                                     right=self.age_text(self.iso_time(server.updated)),
                                     chips=self.run_chips([o.kind for o in server.options], secret),
                                     icon=RUN_GLYPHS.get(server.options[0].kind, "package")
                                     if server.options else "package")
                row.set_tooltip_text(server.name)
                row.server = server
                box.append(row)
            self.update_registry_pager(len(result[:mcp_registry.PAGE]), total)
            scroller = box.get_ancestor(Gtk.ScrolledWindow)
            if scroller is not None:
                scroller.get_vadjustment().set_value(0)       # a new page starts at its top
            return False

        threading.Thread(target=work, daemon=True).start()

    def update_registry_pager(self, shown: int, total):
        """‹ Previous 20 · 21 – 40 / 200 · Next 20 › under the list (only while there is more than one page)."""
        page, more = self.registry_page, len(self.registry_cursors) > self.registry_page + 1
        self.registry_prev.set_sensitive(page > 0)
        self.registry_next.set_sensitive(more)
        self.registry_range.set_label(registry_range(page, shown, total, more))
        self.registry_pager.set_visible(page > 0 or more)

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
        for need in needs[:mcp_registry.MAX_FIELDS]:      # the registry offers no more (second guard)
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
            if getattr(self, "add_tab", "") != "installed":     # Installed has no form
                self.add_status.set_label("⚠ " + text)
        self.add_preview.get_buffer().set_text(text)
        self.update_add_test()
        return False

    def update_add_test(self):
        """Test only for a server picked from the list (or, By hand, a filled-in form)."""
        if getattr(self, "add_testing", False):
            return
        picked = self.add_tab == "hand" or self.add_tab in self.add_pages
        try:
            ready = picked and bool(self.new_mcp_entry())
            why = "" if picked else _("Pick a server from the list first")
        except (OSError, ValueError) as error:
            ready, why = False, str(error)
        self.add_test.set_sensitive(ready)
        self.add_test.set_tooltip_text(_("Start it once and ask for its tools") if ready else why)

    def test_new_mcp(self):
        try:
            entry = self.new_mcp_entry()
        except ValueError as error:
            self.add_status.set_label(str(error))
            return
        found = (self.add_from or {})
        self.add_status.set_label(_("Asking the server…"))
        self.add_testing = True
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
            self.add_testing = False
            self.update_add_test()
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

    # --- closing the details page ------------------------------------------------------------------------

    def close_mcp_details(self):
        self.show_view(getattr(self, "mcp_from", self.view), save=False)
