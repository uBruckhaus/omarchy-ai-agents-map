"""AI Agents Map: the page that sets up a line drawn on the map (a mixin of ai_popup.AgentsMap, split
off to keep ai_popup.py small; its methods use the window's state through self)."""

from gi.repository import GLib, Gdk, Gtk

import agent_security
import ai_core
import connect
import inventory
import link_check
import link_hints
import mcp_ctl
import mcp_library
import mcp_scan
import mcp_tools
import mcp_update
import skill_update
import skills_ctl
import subprocess
import threading
from i18n import _, ngettext
from pathlib import Path


class ConnectPage:
    # --- connecting (a line drawn on the map) -----------------------------------------------

    def build_connect_page(self) -> Gtk.Widget:
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
        self.connect_form = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP)
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
        if not remove:
            self.first_connection_made()        # the first-connection card above the map goes
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
        with_agents = bool(ai_core.setting("providers_with_agents"))

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
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP)
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
