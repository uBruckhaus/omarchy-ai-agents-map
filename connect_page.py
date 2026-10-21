"""AI Agents Map: the page that sets up a line drawn on the map (a mixin of ai_popup.AgentsMap, split
off to keep ai_popup.py small; its methods use the window's state through self)."""

from gi.repository import Adw, GLib, Gdk, Gtk

import agent_security
import ai_core
import connect
import harnesses
import inventory
import link_check
import link_hints
import llm_engine
import mcp_ctl
import mcp_library
import mcp_scan
import mcp_tools
import mcp_update
import runtime_ctl
import skill_update
import skills_ctl
import subprocess
import threading
from i18n import _, ngettext
from pathlib import Path


class ConnectPage:
    # --- connecting (a line drawn on the map) -----------------------------------------------

    def build_connect_page(self) -> Gtk.Widget:
        """The page that reviews a change before it is written (a line drawn or removed, a security
        setting, a skill or MCP update), built from the layout kit (page_parts, docs/layout.md):
        what happens, notes, settings, the check, the buttons under them, then what changes in
        the files. The openers fill connect_intro, connect_form and the buttons; connect_reset()
        empties the page, connect_note() adds a note card, show_page("connect") tidies it."""
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP)
        self.connect_title = Gtk.Label()
        page.append(self.page_header(self.connect_title, self.close_connect,
                                     _("Back without changing anything (Backspace, Esc)")))
        column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP_TIGHT * 2)
        column.add_css_class("am-settings")

        self.connect_about = self.info_card(_("What happens"), "info")
        self.connect_intro = self.card_text(self.connect_about.body, "")
        column.append(self.connect_about)
        self.connect_notes = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP_TIGHT * 2)
        column.append(self.connect_notes)
        self.connect_settings = self.info_card(_("Settings"))
        self.connect_form = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP)
        self.connect_settings.body.append(self.connect_form)
        column.append(self.connect_settings)
        self.connect_check = self.info_card(_("Before it works"), "info")
        self.connect_check.set_visible(False)
        column.append(self.connect_check)

        self.connect_status = Gtk.Label(xalign=0, wrap=True, max_width_chars=1, hexpand=True)
        self.connect_status.add_css_class("am-agent-detail")
        cancel = Gtk.Button(label=_("Cancel"))
        cancel.add_css_class("am-small-btn")
        cancel.connect("clicked", lambda *_: self.close_connect())
        self.connect_remove = Gtk.Button(label=_("Remove connection"))
        self.connect_remove.add_css_class("am-small-btn")
        self.connect_remove.add_css_class("am-danger")
        self.connect_remove.connect("clicked", lambda *_: self.apply_connect(remove=True))
        self.connect_ok = Gtk.Button(label=_("Connect"))
        self.connect_ok.add_css_class("am-small-btn")
        self.connect_ok.add_css_class("am-primary")
        self.connect_ok.connect("clicked", lambda *_: self.apply_connect(remove=False))
        column.append(self.action_row(cancel, self.connect_remove, self.connect_ok, start=(self.connect_status,)))

        changes = self.info_card(_("What changes"))
        self.connect_preview_heading = changes            # shown or hidden as a whole, as before
        self.connect_preview = Gtk.TextView(editable=False, cursor_visible=False, monospace=True)
        self.connect_preview.add_css_class("am-diff")
        self.connect_preview.set_wrap_mode(Gtk.WrapMode.CHAR)
        self.connect_preview_scroller = Gtk.ScrolledWindow(propagate_natural_height=True, max_content_height=460)
        self.connect_preview_scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.connect_preview_scroller.set_child(self.connect_preview)
        changes.body.append(self.connect_preview_scroller)
        column.append(changes)
        page.append(self.scrolled(column))
        return page

    def connect_reset(self):
        """An empty page for the next opener: no form rows, notes or check, no status."""
        for box in (self.connect_form, self.connect_notes, self.connect_check.body):
            self.clear_box(box)
        self.connect_check.set_visible(False)
        self.connect_check_token = object()        # a check still running for the last page is ignored
        self.connect_about.set_level("info", _("What happens"))
        self.connect_status.set_label("")

    def connect_note(self, level: str, text: str, title: str = ""):
        self.note_card(self.connect_notes, level, text, title)

    def connect_tidy(self):
        """Before the page shows: cards with nothing in them are hidden."""
        self.connect_about.set_visible(bool(self.connect_intro.get_label()))
        self.connect_settings.set_visible(self.connect_form.get_first_child() is not None)

    def connect_show_check(self, hints, title: str = ""):
        """The check before connecting (link_check): hint rows in one card, its level the worst."""
        self.clear_box(self.connect_check.body)
        level = self.worst(hints)
        self.connect_check.set_level(level, title or (_("Ready") if level == "ok" else _("Before it works")))
        self.hint_rows(self.connect_check.body, hints)
        self.connect_check.set_visible(bool(hints))

    def on_map_relink(self, agent_id: str, old_id: str, new_id: str | None):
        """A line moved to another local LLM: the old provider goes, the new one
        comes. Dropped on empty space (new_id None): the connection is removed (an
        online provider's: the agent signs out, after a question)."""
        if old_id.startswith("cloud:"):
            if new_id is None:
                self.offer_sign_out(agent_id, old_id)
            return
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

    def on_map_remodel(self, runtime_id: str, old_id: str, new_id: str | None):
        """A local LLM's model line moved: every agent that uses the old model uses the new one
        (backups, one Undo for all). Dropped on empty space (new_id None): after a question, those
        agents are disconnected from the local LLM. Either way nothing uses the old model then: when
        the local LLM still holds it, it is unloaded (its GPU memory freed) in the same step."""
        users = self.side_map.model_users(runtime_id, old_id)
        agents = [a for a in self.agent_list if f"agent:{a.command}" in users]
        if runtime_id.startswith("cloud:"):
            # An online model line: its agents switch to the other model; into empty space there is
            # nothing to do (an online agent always has a model; its provider line signs it out).
            new = next((n.label for n in self.side_map.graph.nodes if n.id == new_id), "") if new_id else ""
            if new and agents:
                self.switch_online(agents, new, runtime_id)
            elif not new_id:
                self.show_note(_("An online agent always has a model: drag the line onto another model. To sign "
                                 "out, drag the agent's line to the provider into empty space."))
            return
        runtime = next((r for r in self.inventory.runtimes if f"runtime:{r.key}" == runtime_id), None)
        if runtime is None:
            return
        if not agents:
            self.swap_loaded(runtime, runtime_id, old_id, new_id)    # only loaded: unload, or load another
            return
        stuck = [a.name for a in agents if a.command not in connect.SUPPORTED]
        agents = [a for a in agents if a.command in connect.SUPPORTED]
        if stuck:
            self.show_note(_("{0} cannot be changed from here yet.").format(", ".join(stuck)))
        if not agents:
            return
        names = ", ".join(a.name for a in agents)
        old = next((n.label for n in self.side_map.graph.nodes if n.id == old_id), old_id)
        busy = self.side_map.model_busy(runtime_id, old_id)
        if busy:
            # Only while the model is free: not loaded, no program and no running agent using it.
            reasons = [_("{0} is running").format(why) if why in [a.name for a in self.agent_list] else why
                       for why in busy]
            self.show_note(_("{0} is in use ({1}): it can be moved or disconnected once it is free.")
                           .format(old, "; ".join(reasons)))
            return
        node = next((n for n in self.side_map.graph.nodes if n.id == old_id), None)
        loaded = node is not None and self.side_map.model_state(node) in ("loaded", "loading")
        if new_id is None:
            question = (_("Disconnect {0} from {1} and free the GPU memory {2} holds? They use {2}: choose another "
                          "model for them afterwards.") if loaded else
                        _("Disconnect {0} from {1}? They use {2}: choose another model for them afterwards."))
            self.show_offer(question.format(names, runtime.name, old), _("Disconnect"), _("Keep"),
                            lambda: self.remodel_apply(agents, runtime, None, names, old, loaded), lambda: None)
            return
        model = next((n.label for n in self.side_map.graph.nodes if n.id == new_id), "")
        if model:
            self.remodel_apply(agents, runtime, model, names, old, loaded)

    def on_map_attach(self, runtime_id: str, model_id: str):
        """A line drawn from a local LLM to one of its models: the model is loaded there (ready),
        and the agents ticked (none at first) get the local LLM with that model."""
        runtime = next((r for r in self.inventory.runtimes if f"runtime:{r.key}" == runtime_id), None)
        model = next((n.label for n in self.side_map.graph.nodes if n.id == model_id), "")
        agents = [a for a in self.agent_list if a.installed and a.command in connect.SUPPORTED
                  and not connect.FIXED.get(a.command)]
        if runtime is None or not model:
            return
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        checks = []
        dialog = Adw.AlertDialog(heading=_("Load {0} in {1}?").format(model, runtime.name),
                                 body=_("It is loaded so that it is ready. Tick the agents that should use it too: "
                                        "each gets {0} as a provider with this model (backup, Undo).")
                                 .format(runtime.name))
        for agent in agents:
            check = Gtk.CheckButton(label=agent.name, active=False)
            check.connect("toggled", lambda *_a: dialog.set_response_label(
                "connect", _("Connect and load") if any(c.get_active() for _a2, c in checks) else _("Load")))
            box.append(check)
            checks.append((agent, check))
        dialog.set_extra_child(box)
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("connect", _("Load"))
        dialog.set_response_appearance("connect", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("connect")
        dialog.set_close_response("cancel")

        def answered(_d, answer):
            if answer == "connect":
                self.attach_apply([agent for agent, check in checks if check.get_active()], runtime, model)
        dialog.connect("response", answered)
        dialog.present(self.window)

    def attach_apply(self, agents: list, runtime, model: str):
        """The ticked agents get the model (one Undo for all); then the model is loaded (the local
        LLM started first if needed), on a thread; one message says both."""
        context = llm_engine.of(runtime).agent_context(runtime)
        backups = []
        try:
            for agent in agents:
                plan_now = connect.plan(agent.command, runtime, model, use_now=True, context=context)
                if plan_now.changes:
                    backups.append(connect.apply(plan_now))
        except (OSError, ValueError, RuntimeError) as error:
            self.show_note(_("Not changed: {0}").format(error))
        self.collect()
        self.refresh_overview()
        self.refresh_map()
        message = _("{0} use {1} ({2}) now").format(", ".join(a.name for a in agents), model, runtime.name) \
            if backups else ""
        slots, slot_backups = self.sync_model_slots() if backups else ("", [])
        backups += slot_backups
        message = " · ".join(t for t in (message, slots) if t)
        self.show_note(_("{0}: loading {1}…").format(runtime.name, model))

        def work():
            ok, why = runtime_ctl.load(runtime, model)
            GLib.idle_add(done, ok, why)

        def done(ok, why):
            self.collect()
            self.refresh_overview()
            self.refresh_map()
            loaded = _("{0} loaded in {1}").format(model, runtime.name) if ok else \
                _("{0} not loaded ({1})").format(model, why)
            if backups:
                self.toast(message + " · " + loaded, 30, _("Undo"), lambda: self.undo_many(backups))
            else:
                self.show_note(loaded)
            return False
        threading.Thread(target=work, daemon=True).start()

    def switch_online(self, agents: list, model: str, cloud_id: str):
        """Agents switch to an online model (their own setting; backup, one Undo); it counts from
        each agent's next start."""
        import online_models
        provider = next((n.label for n in self.side_map.graph.nodes if n.id == cloud_id), cloud_id)
        backups, stuck = [], []
        try:
            for agent in agents:
                if not online_models.supported(agent.command):
                    stuck.append(agent.name)
                    continue
                plan_now = online_models.plan(agent.command, model)
                if plan_now.changes:
                    backups.append(connect.apply(plan_now))
        except (OSError, ValueError, RuntimeError) as error:
            self.show_note(_("Not changed: {0}").format(error))
            return
        self.collect()
        self.refresh_overview()
        self.refresh_map()
        if stuck:
            self.show_note(_("{0} cannot be changed from here yet.").format(", ".join(stuck)))
        if backups:
            self.toast(_("{0} use {1} ({2}) from their next start").format(
                ", ".join(a.name for a in agents if a.name not in stuck), model, provider),
                30, _("Undo"), lambda: self.undo_many(backups))

    def offer_sign_out(self, agent_id: str, cloud_id: str):
        """An agent's line to an online provider dropped into empty space: after a question, the
        agent's own sign-out runs in a terminal (Claude Code, Codex); others say where to do it."""
        agent = next((a for a in self.agent_list if f"agent:{a.command}" == agent_id), None)
        provider = next((n.label for n in self.side_map.graph.nodes if n.id == cloud_id), cloud_id)
        if agent is None:
            return
        harness = harnesses.get(agent.command)
        argv = harness.logout if harness is not None else ()
        if not argv:
            self.show_note(_("{0} signs out from inside itself (its own logout command).").format(agent.name))
            return

        def sign_out():
            import desktop
            if not desktop.run_in_terminal(list(argv)):
                self.show_note(_("No terminal found (xdg-terminal-exec)"))
                return
            GLib.timeout_add(4000, lambda: (self.collect(), self.refresh_overview(), self.refresh_map(), False)[3])
        self.show_offer(_("Sign {0} out of {1}? It can no longer use {1} until it signs in again (draw the line "
                          "again).").format(agent.name, provider), _("Sign out"), _("Keep"), sign_out, lambda: None)

    def swap_loaded(self, runtime, runtime_id: str, old_id: str, new_id: str | None):
        """A loaded model no agent uses: into empty space, it is unloaded; onto another model, that
        one is loaded instead (unless a program outside the map uses the old one)."""
        nodes = {n.id: n.label for n in self.side_map.graph.nodes}
        old, new = nodes.get(old_id, ""), nodes.get(new_id, "") if new_id else ""
        busy = self.side_map.model_busy(runtime_id, old_id)
        if busy:
            self.show_note(_("{0} is in use ({1}): it can be moved or disconnected once it is free.")
                           .format(old, "; ".join(busy)))
            return
        self.show_note(_("{0}: loading {1}…").format(runtime.name, new) if new else
                       _("{0}: unloading {1}…").format(runtime.name, old))

        def work():
            ok, why = runtime_ctl.unload_model(runtime, old)
            if ok and new:
                ok, why = runtime_ctl.load(runtime, new)
            GLib.idle_add(done, ok, why)

        def done(ok, why):
            self.collect()
            self.refresh_overview()
            self.refresh_map()
            if new:
                self.show_note(_("{0} loaded in {1}").format(new, runtime.name) if ok else
                               _("{0} not loaded ({1})").format(new, why))
            else:
                self.show_note(_("{0} unloaded from {1}: its GPU memory is free").format(old, runtime.name) if ok else
                               _("{0} not unloaded ({1})").format(old, why))
            return False
        threading.Thread(target=work, daemon=True).start()

    def remodel_apply(self, agents: list, runtime, model: str | None, names: str, old: str, free: bool = False):
        context = llm_engine.of(runtime).agent_context(runtime)
        backups = []
        try:
            for agent in agents:
                if model is None:
                    plan_now = connect.plan(agent.command, runtime, "", use_now=False, remove=True, context=context)
                else:
                    plan_now = connect.plan(agent.command, runtime, model, use_now=True, context=context)
                if plan_now.changes:
                    backups.append(connect.apply(plan_now))
        except (OSError, ValueError, RuntimeError) as error:
            self.show_note(_("Not changed: {0}").format(error))
        self.collect()
        self.refresh_overview()
        self.refresh_map()
        if not backups:
            return
        message = (_("{0}: no longer connected to {1}").format(names, runtime.name) if model is None else
                   _("{0} use {1} ({2}) now instead of {3}").format(names, model, runtime.name, old))
        slots, slot_backups = self.sync_model_slots()
        backups += slot_backups
        message = " · ".join(t for t in (message, slots) if t)
        if not free:
            self.toast(message, 30, _("Undo"), lambda: self.undo_many(backups))
            return

        # Nothing uses the old model any more: unload it (the local LLM keeps running; the next
        # request loads what it needs), then one message with Undo (Undo does not load it again).
        def work():
            ok, why = runtime_ctl.unload(runtime)
            GLib.idle_add(done, ok, why)

        def done(ok, why):
            self.collect()
            self.refresh_overview()
            self.refresh_map()
            note = _("GPU memory freed") if ok else _("GPU memory not freed ({0})").format(why)
            self.toast(message + " · " + note, 30, _("Undo"), lambda: self.undo_many(backups))
            return False
        threading.Thread(target=work, daemon=True).start()

    def sync_model_slots(self) -> tuple[str, list]:
        """After a connection or a model change: each local LLM keeps as many models loaded as its
        agents use, where it lets the app set that (model_slots). Returns a note for the change's
        message and the backups, so one Undo takes both back; swapping and GPU memory warnings
        are shown after it, and a running local LLM is offered a restart (it reads the number when
        it starts)."""
        import gpu_usage
        import llm_engine
        import model_slots
        try:
            needs = model_slots.plan(self.inventory.runtimes, model_slots.models_in_use(self.side_map.graph))
            vram = gpu_usage.gpu_totals()[1]
        except (OSError, ValueError):
            return "", []
        notes, backups, warnings, restart = [], [], [], []
        for need in needs:
            rt = next((r for r in self.inventory.runtimes if r.key == need.key), None)
            if rt is None:
                continue
            if need.change:
                engine = llm_engine.of(rt)
                try:
                    changes = engine.slot_changes(rt, need.count)
                    if changes:
                        backups.append(connect.apply(connect.Plan(rt.name, rt.key, "", changes)))
                        engine.after_slot_change(rt)
                        if need.kind == "switch":
                            notes.append(_("{0} keeps one model loaded at a time").format(rt.name) if need.count == 1
                                         else _("{0} keeps every model its agents use loaded").format(rt.name))
                        else:
                            notes.append(ngettext("{0} keeps 1 model loaded at a time",
                                                  "{0} keeps {1} models loaded at once", need.count)
                                         .format(rt.name, need.count))
                        restart.append(rt.key)
                        need.slots = need.count if need.kind != "switch" or need.count == 1 else 0
                except (OSError, ValueError, RuntimeError) as error:
                    warnings.append(_("{0}: the number of models at once was not changed: {1}").format(rt.name, error))
            warnings += [hint["text"] for hint in model_slots.hints(need, vram)]
        for text in warnings:
            self.show_notice(text, 12)
        for key in restart:
            self.offer_restart(key)
        return " · ".join(notes), backups

    def undo_many(self, backups: list):
        """Undo of a change to several agents at once: the last one first."""
        try:
            for backup in reversed(backups):
                connect.undo(backup)
        except (OSError, ValueError) as error:
            self.show_notice(_("Undo failed: {0}").format(error))
        # A local LLM's service file may be among them (models at once): systemd reads it again.
        runtime_ctl._run(["systemctl", "--user", "daemon-reload"])
        self.collect()
        self.refresh_overview()
        self.refresh_map()

    def on_map_pick_model(self, agent_id: str, runtime_id: str, model: str):
        """A model clicked while an agent connected to its local LLM is selected:
        the agent uses it from now on (backup, Undo). An online model: the same through
        online_models."""
        if runtime_id.startswith("cloud:"):
            agent = next((a for a in self.agent_list if f"agent:{a.command}" == agent_id), None)
            if agent is not None:
                self.switch_online([agent], model, runtime_id)
            return
        agent = next((a for a in self.agent_list if f"agent:{a.command}" == agent_id), None)
        runtime = next((r for r in self.inventory.runtimes if f"runtime:{r.key}" == runtime_id), None)
        if agent is None or runtime is None:
            return
        if agent.command not in connect.SUPPORTED:
            self.show_note(self.not_connectable_text(agent, runtime, model))
            return
        try:
            context = llm_engine.of(runtime).agent_context(runtime)
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
        slots, slot_backups = self.sync_model_slots()
        message = " · ".join(t for t in (_("{0} uses {1} ({2}) now").format(agent.name, model, runtime.name), slots)
                             if t)
        self.toast(message, 30, _("Undo"), lambda: self.undo_many([backup] + slot_backups))

    @staticmethod
    def not_connectable_parts(agent, runtime, model: str = "") -> tuple[str, str]:
        """An agent the app cannot point at a local LLM (its add-on says nothing about how): what the
        user can do by hand, and which agents the app can set up."""
        import harnesses
        able = sorted(h.name for c in connect.SUPPORTED if (h := harnesses.get(c)) is not None)
        model = model or next(iter(connect.chat_models(runtime.models)), "")
        how = _("Set it up in {0}'s own settings (right-click {0} → Configure…): an OpenAI-compatible provider "
                "at {1}, no API key needed, model {2}.").format(
            agent.name, connect.base_url(runtime.address), model or _("(one of {0}'s models)").format(runtime.name))
        return how, _("These agents can be connected from here: {0}.").format(", ".join(able))

    def not_connectable_text(self, agent, runtime, model: str = "") -> str:
        how, able = self.not_connectable_parts(agent, runtime, model)
        return _("{0} cannot be connected from here yet.").format(agent.name) + " " + how + " " + able

    def connect_run_check(self):
        """The line's check before it is written (link_check, before=True), on a thread: is the
        local LLM there, does it have the model, can it be started, is the context big enough."""
        info = self.connecting or {}
        runtime, agent = info.get("runtime"), info.get("agent")
        if runtime is None or agent is None or info.get("remove_only"):
            return
        models = self.connect_models
        model = models[self.connect_model.get_selected()] if models else ""
        token = self.connect_check_token = object()
        with_agents = bool(ai_core.setting("providers_with_agents"))
        # The models the other agents use on it, with this agent's choice in place of its own.
        import model_slots
        in_use = model_slots.models_in_use(self.side_map.graph).get(f"runtime:{runtime.key}", {})
        in_use = {m: [a for a in agents if a != agent.name] for m, agents in in_use.items()}
        in_use = {m: agents for m, agents in in_use.items() if agents}
        if model and self.connect_use_now.get_active():
            in_use.setdefault(model, []).append(agent.name)

        def work():
            try:
                hints = link_check.check_provider(agent.command, agent.name, runtime, model, with_agents, before=True)
                hints += link_check.slot_hints(runtime, in_use)
            except Exception as error:      # a check must never take the window down
                hints = [link_check.hint("warning", _("Could not check it: {0}").format(str(error)[:200]))]
            GLib.idle_add(done, hints)

        def done(hints):
            if self.connect_check_token is token and self.connecting is info:
                self.connect_show_check(hints)
            return False
        threading.Thread(target=work, daemon=True).start()

    def on_map_link(self, agent_id: str, runtime_id: str, replaces_id: str | None = None,
                    remove_only: bool = False):
        """A line drawn from an agent to a local LLM: ask how, show what changes. To an online
        provider: the agent's own sign-in."""
        if runtime_id.startswith("cloud:"):
            agent = next((a for a in self.agent_list if f"agent:{a.command}" == agent_id), None)
            if agent is not None:
                self.offer_sign_in(agent.command)
            return
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
        self.connect_reset()
        form = self.connect_form
        supported = agent.command in connect.SUPPORTED
        fixed = connect.FIXED.get(agent.command)
        self.connect_preview_heading.set_visible(supported)
        self.connect_preview_scroller.set_visible(supported)
        self.connect_ok.set_visible(supported)
        self.connect_remove.set_visible(False)
        self.connect_status.set_label("")
        if fixed:
            fixed_name = next((r.name for r in self.inventory.runtimes if r.key == fixed), fixed)
            self.connect_about.set_level("ok" if runtime.key == fixed else "warning",
                                         _("Nothing to connect") if runtime.key == fixed else _("Not possible"))
            self.connect_intro.set_label(
                (_("{0} uses {1}'s models by itself: there is nothing to connect.") if runtime.key == fixed else
                 _("{0} can only use {1}'s models (and its own cloud); it cannot be connected to {2}."))
                .format(agent.name, fixed_name, runtime.name))
        elif not supported:
            how, able = self.not_connectable_parts(agent, runtime)
            self.connect_about.set_level("warning", _("Not from here yet"))
            self.connect_intro.set_label(_("{0} cannot be connected from here yet.").format(agent.name))
            card = self.note_card(self.connect_notes, "info", how, _("What you can do"))
            self.card_text(card.body, able)
        else:
            already = connect.is_connected(agent.command, runtime.key)
            # Set to use it, with the provider gone (a broken line): removing clears that setting.
            dangling = not already and connect.is_active(agent.command, runtime.key)
            if remove_only and dangling:
                self.connect_intro.set_label(_("{0} is set to use a model on {1}, but {1} is not set up in {0} any "
                                               "more. Removing the connection switches {0} to another local LLM "
                                               "it has, or clears the setting.").format(agent.name, runtime.name))
            elif remove_only:
                self.connect_intro.set_label(_("The {0} provider is removed from {1}'s settings.")
                                             .format(runtime.name, agent.name))
                if connect.is_active(agent.command, runtime.key):
                    self.connect_note("warning", _("{0} uses it right now: choose another model in {0} afterwards "
                                                   "(or draw a line to another local LLM instead).").format(agent.name))
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
            self.connect_remove.set_visible((already or dangling) and replaces is None)
            self.connect_ok.set_visible(not remove_only)
            self.connect_ok.set_label(_("Move") if replaces else _("Update") if already else _("Connect"))
            self.settings_ready = False
            models = connect.chat_models(runtime.models)
            self.connect_models = models
            context = llm_engine.of(runtime).agent_context(runtime)
            self.connect_context = context
            self.connect_model = self.dropdown(models or [_("(no models found)")], 0,
                                               lambda _i: (self.update_connect_preview(), self.connect_run_check()))
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
            self.connect_use_now.connect("notify::active", lambda *_: self.settings_ready and (
            self.update_connect_preview(), self.connect_run_check()))
            if not remove_only:
                self.setting_line(form, _("Use it now"),
                                  _("Make it the agent's model right away. Off: only set it up, to switch later."),
                                  self.connect_use_now)
            self.settings_ready = True
            self.update_connect_preview()
            if not remove_only:
                self.connect_run_check()
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
        self.connect_reset()
        form = self.connect_form
        supported = mcp_ctl.supported(agent.command)
        self.connect_preview_heading.set_visible(supported)
        self.connect_preview_scroller.set_visible(supported)
        self.connect_status.set_label("")
        # As for a local LLM: the red "Remove connection" removes, "Connect" adds.
        self.connect_remove.set_visible(supported and already)
        self.connect_ok.set_visible(supported and not remove_only and not already)
        self.connect_ok.set_label(_("Connect"))
        if agent.command in mcp_ctl.IN_APP or not supported:
            self.connect_about.set_level("warning", _("Not from here yet"))
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
        self.connect_reset()
        form = self.connect_form
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
            self.connect_about.set_level("warning", _("Not from here yet"))
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
        self.connect_reset()
        form = self.connect_form
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
        self.connect_reset()
        form = self.connect_form
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
        slots, slot_backups = self.sync_model_slots() if info.get("runtime") is not None else ("", [])
        message = " · ".join(t for t in (message, slots) if t)
        self.toast(message, 30, _("Undo"), lambda: self.undo_many([backup] + slot_backups))
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
        line = next((e for e in self.side_map.graph.edges if (e.source, e.target) == tuple(edge)), None) \
            if self.side_map.graph else None
        if entry is None and line is not None and line.dangling and agent is not None:
            # Set to a model of a local LLM it has no provider for: repair it, or clear it.
            runtime = next((n.label for n in self.side_map.graph.nodes if n.id == edge[1]), edge[1])
            entry = {"applied": True, "kind": "provider", "dangling": True, "hints": [{"level": "error", "text": _(
                "{0} is set to use {1} on {2}, but {2} is not set up in {0} any more: the line does not work. "
                "Repair connects {2} again with that model; Remove connection clears the setting.").format(
                agent.name, line.dangling, runtime)}]}
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
        self.hint_rows(box, entry["hints"], width=56)
        if entry.get("when"):
            when = Gtk.Label(label=_("Checked {0}").format(entry["when"].replace("T", " ")), xalign=0)
            when.add_css_class("am-agent-detail")
            box.append(when)
        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        remove = Gtk.Button(label=_("Remove connection"))
        remove.add_css_class("am-small-btn")
        remove.add_css_class("am-danger")
        again = Gtk.Button(label=_("Repair…") if entry.get("dangling") else
                           _("Test again") if entry["applied"] else _("Try again…"))
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
            if entry.get("dangling"):
                self.on_map_link(edge[0], edge[1])                        # connect it again (preview, Undo)
            elif entry["applied"]:
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
