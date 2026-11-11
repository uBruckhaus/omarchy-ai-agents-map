"""Ask AI: Maprisma's central smart card as a page of the overlay window (a mixin of ai_popup.AgentsMap).

One request in one column of the map; the helper answers (smart_card), the window shows what it found:
a question back (clarify), what works instead (redirect, with "Do it with …" after a web look-up), or
proposals. A proposal is never run from here: Review opens the app's own page for it, with its own
review (install, sign in, start, an add-on, a local LLM's details …), as Maprisma's provider and menu
actions do. Tokens are spent only on Ask.
"""

from __future__ import annotations

import threading

from gi.repository import GLib, Gtk, Pango

import ai_core
import addon_store
import harnesses
import smart_card
from i18n import N_, _

# Maprisma's example prompts (map_smart_card.dart smartPromptExamples): (column, label, prompt or "").
EXAMPLES = (
    ("mcp", N_("Find an MCP server for my notes"), ""),
    ("mcp", N_("Connect a skill folder to an agent"), ""),
    ("agents", N_("Which agents need sign-in?"), ""),
    ("agents", N_("Connect an agent to a provider"), ""),
    ("providers", N_("Why is my local LLM slow?"), ""),
    ("providers", N_("Check GPU and context settings"), ""),
    ("models", N_("Find a model that fits my PC"), ""),
    ("models", N_("Disconnect a model from an agent"), ""),
    ("mcp", N_("Add an MCP server…"), N_("Add the MCP server")),
    ("mcp", N_("Remove an MCP server…"), N_("Remove the MCP server")),
    ("mcp", N_("Which MCP servers have updates?"), ""),
    ("agents", N_("Set up a new agent…"), N_("Set up the agent")),
    ("agents", N_("Update an agent…"), N_("Update the agent")),
    ("agents", N_("Which agents have updates?"), ""),
    ("providers", N_("Set up a provider…"), N_("Set up the provider")),
    ("providers", N_("Connect a provider to an agent…"), N_("Connect the provider")),
    ("providers", N_("Which providers are running?"), ""),
    ("models", N_("Download a model…"), N_("Download the model")),
    ("models", N_("Remove a model…"), N_("Remove the model")),
    ("models", N_("Move models to another folder"), ""),
)
COLUMN_TITLES = {"mcp": N_("MCP servers and skills"), "agents": N_("Agents"), "providers": N_("Providers"),
                 "models": N_("Models")}


class AskAiPage:
    # --- building -------------------------------------------------------------------------------------

    def build_askai_page(self) -> Gtk.Widget:
        self.ask_column = "agents"
        self.ask_proposals = smart_card.Proposals()
        self.ask_running = None             # threading.Event of the Ask in progress (its Cancel)
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP)
        header = Gtk.Box(spacing=8)
        header.append(self.back_button(lambda: self.close_ask_ai(), _("Back (Backspace, Esc)")))
        title = Gtk.Label(label=_("Ask AI").upper(), xalign=0, hexpand=True)
        title.add_css_class("am-section-title")
        header.append(title)
        # The helper and what it costs (Maprisma: its name, "Local, no tokens" or "Uses plan/tokens").
        self.ask_cost = Gtk.Label(xalign=1)
        self.ask_cost.add_css_class("am-hint")
        header.append(self.ask_cost)
        self.ask_helper_box = Gtk.DropDown.new_from_strings([])
        self.ask_helper_box.connect("notify::selected", lambda *_: self.on_ask_helper())
        header.append(self.ask_helper_box)
        page.append(header)
        rule = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        rule.add_css_class("am-settings-rule")
        page.append(rule)
        # The column it asks in (Maprisma's category chips inside the window).
        chips = Gtk.Box(spacing=6)
        self.ask_chips = {}
        group = None
        for column in smart_card.COLUMNS:
            chip = Gtk.ToggleButton(label=_(COLUMN_TITLES[column]))
            chip.add_css_class("am-chip")
            chip.add_css_class("am-column-chip")
            if group is None:
                group = chip
            else:
                chip.set_group(group)
            chip.connect("toggled", lambda b, c=column: b.get_active() and self.set_ask_column(c))
            chips.append(chip)
            self.ask_chips[column] = chip
        page.append(chips)
        # The request: Enter asks, Ctrl+Enter a new line.
        row = Gtk.Box(spacing=8)
        self.ask_text = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR, accepts_tab=False, hexpand=True)
        self.guard_text_field(self.ask_text)
        self.ask_text.set_size_request(-1, 64)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self.on_ask_key)
        self.ask_text.add_controller(keys)
        frame = Gtk.Frame(child=self.ask_text, hexpand=True)
        frame.add_css_class("am-ask-field")
        row.append(frame)
        # Dictation (Settings › Dictation in Ask AI): speak the request; a second click stops it.
        self.ask_mic = Gtk.ToggleButton(icon_name="audio-input-microphone-symbolic", valign=Gtk.Align.END)
        self.ask_mic.set_tooltip_text(_("Speak your question into the microphone in Ask AI instead of typing."))
        self.ask_mic.connect("toggled", lambda b: self.on_ask_mic(b.get_active()))
        row.append(self.ask_mic)
        self.ask_dictation = None
        self.ask_button = Gtk.Button(label=_("Ask"), valign=Gtk.Align.END)
        self.ask_button.add_css_class("suggested-action")
        self.ask_button.connect("clicked", lambda *_: self.ask_now())
        row.append(self.ask_button)
        page.append(row)
        note = Gtk.Label(label=_("Only Ask sends the question."), xalign=0)
        note.add_css_class("am-hint")
        page.append(note)
        # Examples of this column (a click fills the field; only Ask sends).
        self.ask_examples = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=6,
                                        column_spacing=6, row_spacing=6, homogeneous=False,
                                        halign=Gtk.Align.START)
        page.append(self.ask_examples)
        # What came back.
        self.ask_result = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP)
        page.append(self.page_scroller(self.ask_result))
        return page

    # --- opening and the helper -----------------------------------------------------------------------

    def ask_helpers(self) -> list:
        """Who can answer: the confirmed local helper, then the online agents (smart_card.helpers)."""
        installed = {a.command for a in self.agent_list if a.installed}
        local = None
        label = ""
        try:
            import local_helper
            found = local_helper.candidates(installed, self.inventory.runtimes)
            local = local_helper.chosen(found, ai_core.setting("ai_local_helper"))
            if local is not None:
                label = local_helper.label(local)
        except Exception:
            local = None
        return smart_card.helpers(installed, local, label)

    def open_ask_ai(self, column: str | None = None):
        """The header's ✦: Ask AI in the overlay window, in the column given or the last one."""
        self.ask_helper_list = self.ask_helpers()
        names = [h.name for h in self.ask_helper_list]
        self.ask_helper_box.set_model(Gtk.StringList.new(names))
        saved = ai_core.setting("smart_helper")
        index = next((i for i, h in enumerate(self.ask_helper_list) if h.command == saved), 0)
        if names:
            self.ask_helper_box.set_selected(index)
        self.ask_helper_box.set_visible(len(names) > 1)
        self.update_ask_cost()
        import dictation
        self.ask_mic.set_visible(ai_core.setting("dictation") is not False
                                 and dictation.chosen(ai_core.setting("dictation_engine")) is not None)
        self.ask_chips[column or self.ask_column].set_active(True)
        self.set_ask_column(column or self.ask_column)
        if not names:
            self.show_ask_state("error", summary=_("No AI helper is set up yet."))
        elif not self.ask_running:
            self.show_ask_state("idle")
        if self.page_name() != "askai":
            self.ask_return = self.page_name()
        self.show_page("askai")
        self.ask_text.grab_focus()

    def close_ask_ai(self):
        back = getattr(self, "ask_return", "main")
        self.show_page(back if back in ("main", "setup") else "main")

    def current_helper(self):
        helpers = getattr(self, "ask_helper_list", [])
        index = self.ask_helper_box.get_selected()
        return helpers[index] if helpers and 0 <= index < len(helpers) else None

    def on_ask_helper(self):
        helper = self.current_helper()
        if helper is not None and helper.command != ai_core.setting("smart_helper"):
            self.save_setting(smart_helper=helper.command)
        self.update_ask_cost()

    def update_ask_cost(self):
        helper = self.current_helper()
        self.ask_cost.set_label("" if helper is None else
                                (helper.name if len(getattr(self, "ask_helper_list", [])) == 1 else "") +
                                (" · " if len(getattr(self, "ask_helper_list", [])) == 1 else "") +
                                (_("Local, no tokens") if helper.local else _("Uses plan/tokens")))

    def set_ask_column(self, column: str):
        self.ask_column = column
        child = self.ask_examples.get_first_child()
        while child is not None:
            self.ask_examples.remove(child)
            child = self.ask_examples.get_first_child()
        for where, label, prompt in EXAMPLES:
            if where != column:
                continue
            button = Gtk.Button(label=_(label))
            button.add_css_class("am-small-btn")
            # A prompt to finish ("Add the MCP server …"): the cursor after it, a space to type on.
            button.connect("clicked", lambda _b, t=(_(prompt) + " " if prompt else _(label)): self.fill_ask(t))
            self.ask_examples.append(button)

    def fill_ask(self, text: str):
        buffer = self.ask_text.get_buffer()
        buffer.set_text(text)
        buffer.place_cursor(buffer.get_end_iter())
        self.ask_text.grab_focus()

    def on_ask_key(self, _controller, keyval, _code, state):
        from gi.repository import Gdk
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) and not state & Gdk.ModifierType.CONTROL_MASK:
            self.ask_now()
            return True
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            buffer = self.ask_text.get_buffer()
            buffer.insert_at_cursor("\n")
            return True
        return False

    # --- asking ---------------------------------------------------------------------------------------

    def ask_now(self, answer: str | None = None):
        """Ask: the helper's answer in the background; Cancel ends its run."""
        helper = self.current_helper()
        buffer = self.ask_text.get_buffer()
        request = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False).strip()
        if helper is None or not request or self.ask_running:
            return
        request = request[:smart_card.MAX_REQUEST]
        facts = self.smart_facts(self.ask_column, request, answer)
        cancel = threading.Event()
        self.ask_running = cancel
        self.show_ask_state("working", request=request, helper=helper.name)

        def work():
            import ai_helper
            with ai_helper.watched(cancel):
                result = smart_card.ask(helper, facts)
            GLib.idle_add(done, result)

        def done(result):
            if self.ask_running is not cancel:
                return False            # cancelled: its answer is not shown
            self.ask_running = None
            result = self.drop_refused(result)
            if result.get("candidates"):
                result["proposal"] = self.ask_proposals.keep(result["candidates"])
            self.show_ask_state(result.get("status", "error"), **result)
            return False

        threading.Thread(target=work, daemon=True).start()

    def drop_refused(self, result: dict) -> dict:
        """Proposals the user's rules refuse are left out (Maprisma's _dropRefused); none left: it is not
        allowed here. Every one is checked again when it is opened (its page asks the rules itself)."""
        import action_rules
        candidates = result.get("candidates")
        if not candidates:
            return result
        rules = ai_core.setting("action_rules")
        kept = [c for c in candidates
                if all(action_rules.allows(rules, *check) for check in action_rules.candidate_checks(c))]
        if kept:
            return {**result, "candidates": kept}
        if result.get("status") == "redirect":
            return {**result, "candidates": []}
        return {**result, "status": "redirect", "candidates": [],
                "instead": {"hint": "This is not allowed here.", "column": None, "request": None}}

    def on_ask_mic(self, on: bool):
        """Start dictating into the request, or stop and put the words in (Omarchy's dictation and
        Superwhisper's auto-paste type them into the field themselves: nothing is put in twice)."""
        import dictation
        if getattr(self, "ask_mic_busy", False):
            return
        if on:
            engine = dictation.chosen(ai_core.setting("dictation_engine"))
            if engine is None:
                self.ask_mic.set_active(False)
                return
            self.ask_dictation = dictation.Dictation(engine)
            self.ask_text.grab_focus()               # where the desktop types the words
            error = self.ask_dictation.start()
            if error:
                self.ask_dictation = None
                self.ask_mic_busy = True
                self.ask_mic.set_active(False)
                self.ask_mic_busy = False
                self.toast(_("Could not start dictation: {0}").format(error), 8)
            return
        running = self.ask_dictation
        self.ask_dictation = None
        if running is None:
            return
        buffer = self.ask_text.get_buffer()
        before = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False)
        self.ask_mic.set_sensitive(False)
        self.ask_text.grab_focus()

        def work():
            text = running.stop()
            GLib.idle_add(done, text)

        def done(text):
            self.ask_mic.set_sensitive(True)
            now = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False)
            if text and now == before:               # not typed in already: put it at the cursor
                buffer.insert_at_cursor(("" if not now or now.endswith((" ", "\n")) else " ") + text)
            self.ask_text.grab_focus()
            return False
        threading.Thread(target=work, daemon=True).start()

    def cancel_ask(self):
        if self.ask_running is not None:
            self.ask_running.set()
            self.ask_running = None
        self.show_ask_state("idle")

    def smart_facts(self, column: str, request: str, answer: str | None) -> dict:
        """What the helper is told about its column (Maprisma's smart_ask facts), from what the app knows."""
        updates = self.open_updates()
        import i18n
        facts = {"column": column, "request": request, "language": i18n.current(),
                 "action_rules": ai_core.setting("action_rules"),
                 "rule_hints": ai_core.setting("action_rule_hints")}
        if answer:
            facts["answer"] = answer[:smart_card.MAX_ANSWER]
        if column == "agents":
            import install_ctl
            import install_recipes
            import terminal_pane
            agents = []
            for h in harnesses.all_harnesses():
                agent = next((a for a in self.agent_list if a.command == h.command), None)
                installed = bool(agent and agent.installed)
                recipe = bool(h.install) or h.command in install_ctl.AGENT_INSTALLERS
                agents.append({
                    "command": h.command, "name": h.name, "installed": installed,
                    "version": getattr(agent, "version", "") or "",
                    "install": not installed and recipe,
                    "update": installed and f"agent:{h.command}" in updates,
                    "remove": installed and (h.command in install_ctl.AGENT_INSTALLERS or (
                        bool(h.install) and install_recipes.choose(h.install, "uninstall", agent.source) is not None)),
                    "login": installed and self.sign_in_access(h.command) is not None,
                    "logout": False, "verify": False, "key": False,
                    "security": installed, "details": installed, "configure": installed,
                    "start": installed and bool(terminal_pane.program(h.command)),
                    "halt": bool(getattr(self, "agent_outside", {}).get(h.command)),
                })
            facts["agents"] = agents
            facts["addons"] = self.smart_addon_facts(agents=True)
            facts["providers_of"] = self.smart_provider_links()
        elif column == "providers":
            import install_ctl
            providers = []
            for rt in self.inventory.runtimes:
                providers.append({
                    "key": rt.key, "name": rt.name, "installed": rt.installed, "running": rt.running,
                    "install": not rt.installed and rt.key in getattr(install_ctl, "INSTALLERS", {}),
                    "update": f"runtime:{rt.key}" in updates,
                    "start": rt.installed and not rt.running, "stop": rt.running,
                    "performance": rt.installed,
                })
            facts["providers"] = providers
            facts["addons"] = self.smart_addon_facts(agents=False)
        elif column == "mcp":
            import mcp_ctl
            installed = [a for a in self.agent_list if a.installed]
            facts["agents"] = [{"command": a.command, "name": a.name, "mcp": bool(mcp_ctl.supported(a.command))}
                               for a in installed]
            facts["mcp"] = {m.name: list(m.agents) for m in self.inventory.mcp}
            # Skill folders as short ids: their paths stay here, never in the prompt.
            sets = list(self.inventory.skill_sets)[:60]
            facts["skill_paths"] = {f"s{i + 1}": s.path for i, s in enumerate(sets)}
            facts["skills"] = {f"s{i + 1}": list(s.agents) for i, s in enumerate(sets)}
        elif column == "models":
            import shutil
            import install_ctl
            import models_ctl
            runtimes = [r for r in self.inventory.runtimes if r.installed]
            facts["runtimes"] = [{"key": r.key, "name": r.name, "models": list(r.models)[:40]} for r in runtimes]
            free = 0
            try:
                free = max((shutil.disk_usage(r.models_dir).free for r in runtimes if r.models_dir), default=0)
            except OSError:
                pass
            facts["limits"] = {"vram": install_ctl.detect_gpu().vram, "ram": models_ctl.system_ram(),
                               "free_disk": free}
            facts["model_users"] = self.smart_model_users()
            facts["agent_names"] = {a.command: a.name for a in self.agent_list}
        return facts

    def smart_provider_links(self) -> dict:
        """The providers an agent can be connected to (the map's online providers and local LLMs) and
        which each agent uses now (its lines), for the Agents column's connect and disconnect."""
        built = getattr(self.side_map, "graph", None)
        if built is None:
            return {"providers": {}, "uses": {}}
        providers = {n.id: n.label for n in built.nodes if n.kind in ("cloud", "runtime")}
        uses = {}
        for edge in built.edges:
            if edge.kind in ("uses", "available") and edge.source.startswith("agent:") and edge.target in providers:
                uses.setdefault(edge.source[6:], []).append(edge.target)
        return {"providers": providers, "uses": uses}

    def smart_model_users(self) -> list:
        """[{key, name, used_by: {model: [agents]}}]: the local LLMs' models agents use (their lines)."""
        built = getattr(self.side_map, "graph", None)
        if built is None:
            return []
        labels = {n.id: n.label for n in built.nodes}
        found = {}
        for edge in built.edges:
            if edge.kind != "default" or not edge.source.startswith("agent:") \
                    or not edge.target.startswith("model:runtime:"):
                continue
            key = edge.target[len("model:runtime:"):].partition(":")[0]
            rt = next((r for r in self.inventory.runtimes if r.key == key), None)
            entry = found.setdefault(key, {"key": key, "name": rt.name if rt else key, "used_by": {}})
            entry["used_by"].setdefault(labels.get(edge.target, edge.target), []).append(edge.source[6:])
        return list(found.values())

    def smart_addon_facts(self, agents: bool) -> list:
        """The add-ons of a column, as Maprisma's _smartAddonFacts: what can be done with each."""
        newer = {e.id: e.version for e in getattr(self, "addons_entries", None) or []}
        found = []
        for addon in getattr(self, "addons", []):
            if (addon.kind == "agent") != agents:
                continue
            on = addon.id in addon_store.enabled()
            found.append({"id": addon.id, "name": addon.name, "enabled": on, "details": True,
                          "on": not on and addon.state in ("verified", "loaded"), "off": on,
                          "remove": True, "install": False,
                          "update": addon.id in newer and newer[addon.id] != addon.version})
        return found

    # --- the states -----------------------------------------------------------------------------------

    def show_ask_state(self, state: str, **result):
        box = self.ask_result
        while (child := box.get_first_child()) is not None:
            box.remove(child)
        self.ask_button.set_sensitive(state != "working")

        def text(label, css="", wrap=True, selectable=False):
            widget = Gtk.Label(label=label, xalign=0, wrap=wrap, selectable=selectable)
            widget.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
            if css:
                widget.add_css_class(css)
            box.append(widget)
            return widget

        if state == "idle":
            return
        if state == "working":
            text(_("{0} is checking {1}…").format(result.get("helper", ""), result.get("request", "")[:120]))
            spinner = Gtk.Spinner(spinning=True, halign=Gtk.Align.START)
            box.append(spinner)
            cancel = Gtk.Button(label=_("Cancel"), halign=Gtk.Align.START)
            cancel.add_css_class("am-small-btn")
            cancel.connect("clicked", lambda *_: self.cancel_ask())
            box.append(cancel)
            return
        if state == "clarify":
            text(result.get("question", ""), "am-setting-heading")
            choices = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=4)
            for choice in result.get("choices", []):
                button = Gtk.Button(label=choice)
                button.add_css_class("am-chip")
                button.connect("clicked", lambda _b, c=choice: self.ask_now(answer=c))
                choices.append(button)
            box.append(choices)
            return
        if state == "redirect":
            instead = result.get("instead") or {}
            text(_(instead.get("hint", "")), selectable=True)     # the helper's words, or the app's own (translated)
            column = instead.get("column")
            if column in smart_card.COLUMNS and column != self.ask_column:
                move = Gtk.Button(label=_("Move to {0}").format(_(COLUMN_TITLES[column])), halign=Gtk.Align.START)
                move.add_css_class("am-small-btn")

                def moved(*_args, c=column, r=instead.get("request") or ""):
                    self.ask_chips[c].set_active(True)
                    if r:
                        self.fill_ask(r)
                move.connect("clicked", moved)
                box.append(move)
            for candidate in result.get("candidates", []):
                if candidate.get("kind") == "agent_task":
                    button = Gtk.Button(label=_("Do it with {0}…").format(candidate.get("title", "")),
                                        halign=Gtk.Align.START)
                    button.add_css_class("am-small-btn")
                    button.connect("clicked", lambda _b, p=result.get("proposal"), c=candidate["id"]:
                                   self.apply_candidate(p, c))
                    box.append(button)
            return
        if state == "proposal":
            if result.get("summary"):
                text(result["summary"])
            for candidate in result.get("candidates", []):
                card = Gtk.Box(spacing=10)
                card.add_css_class("am-help-card")
                words = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
                title = Gtk.Label(label=candidate.get("title", ""), xalign=0)
                title.add_css_class("am-setting-heading")
                words.append(title)
                sub = " · ".join(filter(None, [_(candidate.get("subtitle", "")) if candidate.get("subtitle") else "",
                                               candidate.get("reason", "")]))
                if sub:
                    detail = Gtk.Label(label=sub, xalign=0, wrap=True)
                    detail.add_css_class("am-hint")
                    words.append(detail)
                card.append(words)
                review = Gtk.Button(label=_("Review"), valign=Gtk.Align.CENTER)
                review.add_css_class("am-small-btn")
                review.connect("clicked", lambda _b, p=result.get("proposal"), c=candidate["id"]:
                               self.apply_candidate(p, c))
                card.append(review)
                box.append(card)
            return
        # error
        # The engine's own messages are English (smart_card): shown in the user's language.
        text(_(result.get("summary") or "The action could not be completed."), "am-problem")
        again = Gtk.Button(label=_("Try again"), halign=Gtk.Align.START)
        again.add_css_class("am-small-btn")
        again.connect("clicked", lambda *_: self.show_ask_state("idle"))
        box.append(again)

    # --- a proposal's Review: the app's own page for it ------------------------------------------------

    def apply_candidate(self, proposal: str | None, candidate_id: str):
        candidate = self.ask_proposals.get(proposal or "", candidate_id)
        if candidate is None:
            self.show_ask_state("error", summary=_("This proposal is too old. Ask again."))
            return
        action = candidate["action"]
        kind = action.get("kind")
        # The rules as they are now (they may have changed since the proposal was shown).
        import action_rules
        for check in action_rules.candidate_checks(candidate):
            if not self.rule_allows(*check):
                return
        self.close_ask_ai()
        if kind == "agent_action":
            command, step = action["command"], action["step"]
            if step in ("install", "update", "uninstall"):
                self.offer_install("agent", command, step)
            elif step == "login":
                self.offer_sign_in(command)
        elif kind == "provider_action":
            key, step = action["key"], action["step"]
            rt = next((r for r in self.inventory.runtimes if r.key == key), None)
            if step == "install":
                self.offer_install("runtime", key, "install")
            elif step == "update":
                update = self.open_updates().get(f"runtime:{key}")
                if update is not None:
                    self.run_update(update)
            elif step == "start" and rt is not None:
                import runtime_ctl
                self.control_runtime(rt, runtime_ctl.start, _("Start server"))
            elif step == "stop":
                self.ask_stop_runtime(key)
            elif step == "performance":
                self.open_runtime_status(key)
        elif kind == "menu_action":
            area, step, key = action["area"], action["step"], action["key"]
            if area == "agents":
                agent = next((a for a in self.agent_list if a.command == key), None)
                if agent is None:
                    return
                if step == "start":
                    self.start_agent(agent, None, "here")
                elif step == "halt":
                    self.map_halt(f"agent:{key}")
                elif step == "configure" and self.configure_entries("agent", key):
                    self.offer_configure("agent", key)
                else:
                    self.open_agent_status(agent)        # details, security (a section there), configure
            elif area == "mcp":
                self.open_add_mcp()
            elif area == "models":
                self.open_reorganize()                   # Tidy up: each move shown first
            elif area == "addons":
                # On, off, details, install, update, remove: the Add-ons page with its own switch and
                # confirmations (the lock while its agent runs here, privacy levels).
                self.open_addons(key)
        elif kind == "map_link":
            # The same review as a line drawn (or a hook dragged off) on the map.
            self.on_map_link(action["source"], action["target"], remove_only=action["remove"])
        elif kind == "mcp_add":
            # Add MCP server, the registry searched for it and the agents ticked: its own form and review.
            self.open_add_mcp()
            for command, check in getattr(self, "add_checks", {}).items():
                check.set_active(command in action.get("agents", []))
            self.add_query.set_text(action["name"])
            self.search_mcp()
        elif kind in ("model_download", "model_remove"):
            rt = next((r for r in self.inventory.runtimes if r.key == action["runtime"]), None)
            if rt is not None:
                self.open_models(rt)                     # its models: download (sizes that fit) or delete
                if kind == "model_download":
                    self.models_search.set_text(action["repo"])
                    self.search_models()
        elif kind == "agent_task":
            self.do_with_agent(f"smart-card:{action['command']}", _("Ask AI"), action["prompt"],
                               agent=action["command"])
