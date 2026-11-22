"""Ask AI: Maprisma's central smart card as a page of the overlay window (a mixin of ai_popup.AgentsMap).

Maprisma task 078: one plain-language question about the whole AI setup. No category to choose: the
helper's first answer decides (smart_card, column "auto") whether it answers directly, asks back (with
suggested replies and a field for any other answer), looks something up on the web, or proposes a step
of one area of the map. Answers can be followed up; a follow-up carries a bounded summary of the
previous exchange, in memory only, and only to the same helper unless the user shares it with another.
A proposal is never run from here: Review opens the app's own page for it, with its own review
(install, sign in, start, an add-on, a local LLM's details …), as Maprisma's provider and menu actions
do. Tokens are spent only on Ask; Cancel ends the Ask before its next helper run.
"""

from __future__ import annotations

import threading

from gi.repository import Gio, GLib, Gtk, Pango

import ai_core
import addon_store
import harnesses
import smart_card
from i18n import N_, _

# Maprisma's example prompts (map_smart_card.dart smartPromptExamples): (column, label, prompt or "").
# Their positions are the built-in ids the setting ask_prompts keeps, as in Maprisma.
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
# The examples under the field, phrased as goals (Maprisma 078): (built-in id, question). One shows
# while its built-in stays among the suggestions of its column.
GOAL_EXAMPLES = ((6, N_("Find a model that fits my PC")), (4, N_("Why are answers slow on this PC?")),
                 (0, N_("Help my agent search my notes")), (2, N_("Which agents need sign-in?")))
# My suggestions: the user's own saved questions, then each column's (built-in and own).
GROUPS = ("general",) + smart_card.COLUMNS
MAX_PROMPTS = 12
WORKING_STAGES = {"Ask AI", ""}          # the helper's own run: "{helper} is checking the AI Map…"


# --- suggestions (setting ask_prompts, as Maprisma's smartPromptsFromSetting) ---------------------------

def builtin_prompts(group: str) -> list[int]:
    return [i for i, (column, _label, _prompt) in enumerate(EXAMPLES) if column == group]


def prompts_from_setting(value) -> dict:
    """{group: [int | str]}: an int keeps that built-in example of the group, a string is the user's own
    question. Anything that does not fit is left out."""
    out = {}
    if not isinstance(value, dict):
        return out
    for group, items in value.items():
        if not isinstance(group, str) or not isinstance(items, list):
            continue
        builtins = builtin_prompts(group)
        out[group] = [item if isinstance(item, int) else item.strip()
                      for item in items[:MAX_PROMPTS]
                      if (isinstance(item, int) and not isinstance(item, bool) and item in builtins)
                      or (isinstance(item, str) and item.strip())]
    return out


def prompt_items(saved: dict, group: str) -> list:
    return list(saved.get(group, builtin_prompts(group)))


def prompt_text(item) -> str:
    """What a suggestion puts in the field (a started one ends in a space: the user names the item)."""
    if isinstance(item, int):
        _column, label, prompt = EXAMPLES[item]
        return _(prompt) + " " if prompt else _(label)
    return item


def one_line(text: str) -> str:
    return " ".join(text.split())


def stored_prompts(raw_now, shown: dict, group: str, items) -> dict:
    """The setting to write when `group` becomes `items` (None: its built-ins again). Merged with what
    is stored now, so other groups stay as they are; refused (ValueError) when the group changed since
    it was shown (another window saved it)."""
    if prompts_from_setting(raw_now).get(group) != shown.get(group):
        raise ValueError("suggestions changed")
    stored = dict(raw_now) if isinstance(raw_now, dict) else {}
    if items is None or list(items) == builtin_prompts(group):
        stored.pop(group, None)
    else:
        stored[group] = list(items)
    return stored


def review_label(candidate: dict) -> str:
    """The proposal's button names its effect (Maprisma 078)."""
    action = candidate.get("action") or {}
    kind = action.get("kind") or candidate.get("kind")
    if kind == "model_download":
        return N_("Review download")
    if kind == "provider_action" and action.get("step") == "performance":
        return N_("Open Performance")
    if kind in ("map_link", "mcp_add"):
        return N_("Review connection")
    return N_("Review")


class AskAiPage:
    # --- building -------------------------------------------------------------------------------------

    def build_askai_page(self) -> Gtk.Widget:
        self.ask_column = smart_card.AUTO
        self.ask_proposals = smart_card.Proposals()
        self.ask_running = None             # threading.Event of the Ask in progress (its Cancel)
        self.reset_ask_exchange()
        self.ask_saved = prompts_from_setting(ai_core.setting("ask_prompts"))
        self.ask_editing = None             # (group, position) of the suggestion being edited
        self.ask_filling = False
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP)
        header = Gtk.Box(spacing=8)
        header.append(self.back_button(lambda: self.close_ask_ai(), _("Back (Backspace, Esc)")))
        title = Gtk.Label(label=_("Ask AI").upper(), xalign=0, hexpand=True)
        title.add_css_class("am-section-title")
        header.append(title)
        # The helper and what it costs (Maprisma: "Local, no tokens" or "Online · uses plan/tokens").
        self.ask_cost = Gtk.Label(xalign=1)
        self.ask_cost.add_css_class("am-hint")
        header.append(self.ask_cost)
        self.ask_helper_box = Gtk.DropDown.new_from_strings([])
        self.ask_helper_box.set_tooltip_text(_("Choose a helper"))
        self.ask_helper_box.connect("notify::selected", lambda *_: self.on_ask_helper())
        header.append(self.ask_helper_box)
        page.append(header)
        rule = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        rule.add_css_class("am-settings-rule")
        page.append(rule)
        # The question: Enter asks, Ctrl+Enter a new line. No category to choose.
        row = Gtk.Box(spacing=8)
        self.ask_text = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR, accepts_tab=False, hexpand=True)
        self.guard_text_field(self.ask_text)
        self.ask_text.set_size_request(-1, 64)
        self.ask_text.update_property([Gtk.AccessibleProperty.LABEL], [_("Ask about your AI setup…")])
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self.on_ask_key)
        self.ask_text.add_controller(keys)
        self.ask_text.get_buffer().connect("changed", lambda *_: self.update_ask_button())
        frame = Gtk.Frame(child=self.ask_text, hexpand=True)
        frame.add_css_class("am-ask-field")
        field = Gtk.Overlay(child=frame, hexpand=True)
        self.ask_placeholder = Gtk.Label(label=_("Ask about your AI setup…"), xalign=0,
                                         halign=Gtk.Align.START, valign=Gtk.Align.START,
                                         margin_start=8, margin_top=6, can_target=False)
        self.ask_placeholder.add_css_class("am-hint")
        field.add_overlay(self.ask_placeholder)
        row.append(field)
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
        self.ask_limit = Gtk.Label(label=_("Keep your question within 2,000 characters."), xalign=0,
                                   visible=False)
        self.ask_limit.add_css_class("am-problem")
        page.append(self.ask_limit)
        # Without a ready helper the field and the examples stay usable (Maprisma 078).
        self.ask_helper_note = Gtk.Box(spacing=8, visible=False)
        self.ask_helper_text = Gtk.Label(xalign=0, wrap=True, hexpand=True)
        self.ask_helper_text.add_css_class("am-hint")
        self.ask_helper_note.append(self.ask_helper_text)
        self.ask_get_started = Gtk.Button(label=_("Get started"), valign=Gtk.Align.CENTER)
        self.ask_get_started.add_css_class("am-small-btn")
        self.ask_get_started.connect("clicked", lambda *_: self.ask_set_up_helper())
        self.ask_helper_note.append(self.ask_get_started)
        page.append(self.ask_helper_note)
        note = Gtk.Label(label=_("Only Ask sends the question."), xalign=0)
        note.add_css_class("am-hint")
        page.append(note)
        # Examples phrased as goals (a click fills the field; only Ask sends).
        self.ask_examples = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=6,
                                        column_spacing=6, row_spacing=6, homogeneous=False,
                                        halign=Gtk.Align.START)
        page.append(self.ask_examples)
        # My suggestions (all saved questions, editable) and Save as suggestion.
        tools = Gtk.Box(spacing=6)
        self.ask_show_saved = Gtk.ToggleButton(label=_("My suggestions"))
        self.ask_show_saved.add_css_class("am-small-btn")
        self.ask_show_saved.connect("toggled", lambda *_: self.fill_ask_suggestions())
        tools.append(self.ask_show_saved)
        save = Gtk.Button(label=_("Save as suggestion"))
        save.add_css_class("am-small-btn")
        save.connect("clicked", lambda *_: self.save_ask_prompt())
        tools.append(save)
        page.append(tools)
        self.ask_suggestions = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=3,
                                           column_spacing=6, row_spacing=6, homogeneous=False,
                                           halign=Gtk.Align.START, visible=False)
        page.append(self.ask_suggestions)
        self.ask_prompt_note = Gtk.Label(xalign=0, visible=False, wrap=True)
        self.ask_prompt_note.add_css_class("am-hint")
        page.append(self.ask_prompt_note)
        # What came back.
        self.ask_result = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP)
        page.append(self.page_scroller(self.ask_result))
        self.fill_ask_examples()
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

    @staticmethod
    def helper_index(helpers: list, saved) -> int | None:
        """The helper to show selected: the saved one; the first when none was chosen yet; None when the
        chosen one is not ready (the user picks a replacement: no silent fallback, Maprisma 078)."""
        if not helpers:
            return None
        found = next((i for i, h in enumerate(helpers) if h.command == saved), None)
        if found is not None:
            return found
        return 0 if not saved else None

    def open_ask_ai(self, column: str | None = None, prompt: str | None = None):
        """The header's ✦: Ask AI in the overlay window. `prompt`: text to put into the field for the
        user to edit (a map item's or a finding's name); nothing is sent. `column` is kept for callers
        that name one: the question is asked across the whole map either way."""
        self.ask_helper_list = self.ask_helpers()
        names = [h.name for h in self.ask_helper_list]
        index = self.helper_index(self.ask_helper_list, ai_core.setting("smart_helper"))
        # A Gtk.DropDown always selects an item: "Choose a helper" stands first while the chosen one is
        # not ready, so nothing is selected for the user.
        choose = bool(names) and index is None
        self.ask_helper_offset = 1 if choose else 0
        self.ask_filling = True
        try:
            self.ask_helper_box.set_model(Gtk.StringList.new(([_("Choose a helper")] if choose else []) + names))
            self.ask_helper_box.set_selected(0 if choose else index or 0)
        finally:
            self.ask_filling = False
        self.ask_helper_box.set_visible(len(names) > 1 or choose)
        self.update_ask_cost()
        import dictation
        self.ask_mic.set_visible(ai_core.setting("dictation") is not False
                                 and dictation.chosen(ai_core.setting("dictation_engine")) is not None)
        self.ask_saved = prompts_from_setting(ai_core.setting("ask_prompts"))
        self.fill_ask_examples()
        self.fill_ask_suggestions()
        if prompt:
            self.reset_ask_exchange()
            self.fill_ask(prompt)
        if not self.ask_running:
            self.show_ask_state("idle")
        if self.page_name() != "askai":
            self.ask_return = self.page_name()
        self.show_page("askai")
        self.update_ask_button()
        self.ask_text.grab_focus()

    def close_ask_ai(self):
        """Esc or Back: to the map. An Ask under way is cancelled, the exchange forgotten; the draft stays."""
        if self.ask_running is not None:
            self.ask_running.set()
            self.ask_running = None
        self.stop_ask_dictation()
        self.reset_ask_exchange()
        self.show_ask_state("idle")
        back = getattr(self, "ask_return", "main")
        self.show_page(back if back in ("main", "setup") else "main")

    def ask_set_up_helper(self):
        """Get started: the first-agent setup; the helpers are read again when Ask AI opens next."""
        self.close_ask_ai()
        self.open_first_agent()

    def current_helper(self):
        helpers = getattr(self, "ask_helper_list", [])
        index = self.ask_helper_box.get_selected() - getattr(self, "ask_helper_offset", 0)
        return helpers[index] if helpers and 0 <= index < len(helpers) else None

    def on_ask_helper(self):
        if self.ask_filling:
            return
        helper = self.current_helper()
        if helper is not None and helper.command != ai_core.setting("smart_helper"):
            self.save_setting(smart_helper=helper.command)
        # Another helper: the previous exchange goes to it only when the user shares it.
        self.ask_share = False
        if helper is None or helper.command != self.ask_context_helper:
            self.ask_context = None
        self.update_ask_cost()
        self.update_ask_button()
        if self.ask_state in ("answer", "redirect", "proposal"):
            self.show_ask_state(self.ask_state, **self.ask_reply)

    def update_ask_cost(self):
        helper = self.current_helper()
        helpers = getattr(self, "ask_helper_list", [])
        if helper is None:
            self.ask_cost.set_label("")
        else:
            cost = _("Local, no tokens") if helper.local else _("Online · uses plan/tokens")
            self.ask_cost.set_label(f"{helper.name} · {cost}" if len(helpers) == 1 else cost)
        if not helpers:
            note = _("No AI helper is set up yet.")
        elif helper is None:
            note = _("Choose a helper")
        else:
            note = ""
        self.ask_helper_text.set_label(note)
        self.ask_get_started.set_visible(not helpers)
        self.ask_helper_note.set_visible(bool(note))

    def ask_draft(self) -> str:
        buffer = self.ask_text.get_buffer()
        return buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False)

    def update_ask_button(self):
        """Ask only for a question that can be sent: not blank, within the core's limit, a helper ready."""
        text = self.ask_draft().strip()
        self.ask_placeholder.set_visible(not self.ask_draft())
        self.ask_limit.set_visible(len(text) > smart_card.MAX_REQUEST)
        self.ask_button.set_sensitive(self.ask_running is None and self.current_helper() is not None
                                      and smart_card.request_problem(text) is None)

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

    # --- examples and My suggestions ------------------------------------------------------------------

    # clear_box: PageParts (page_parts.py), another part of the same window.

    def fill_ask_examples(self):
        self.clear_box(self.ask_examples)
        for builtin, question in GOAL_EXAMPLES:
            if builtin not in prompt_items(self.ask_saved, EXAMPLES[builtin][0]):
                continue
            button = Gtk.Button(label=_(question))
            button.add_css_class("am-small-btn")
            button.connect("clicked", lambda _b, t=_(question): self.pick_ask_prompt(t))
            self.ask_examples.append(button)

    def pick_ask_prompt(self, text: str):
        """A suggestion fills the field; a new question starts (only Ask sends it)."""
        self.reset_ask_exchange()
        if self.ask_running is None:
            self.show_ask_state("idle")
        self.fill_ask(text)

    def fill_ask_suggestions(self):
        box = self.ask_suggestions
        self.clear_box(box)
        box.set_visible(self.ask_show_saved.get_active())
        if not self.ask_show_saved.get_active():
            return
        for group in GROUPS:
            for position, item in enumerate(prompt_items(self.ask_saved, group)):
                box.append(self.ask_suggestion_chip(group, position, item))
            if group != "general" and group in self.ask_saved:
                back = Gtk.Button(label=f"{_('Back to the built-in suggestions')} · {_(COLUMN_TITLES[group])}")
                back.add_css_class("am-small-btn")
                back.connect("clicked", lambda _b, g=group: self.store_ask_prompts(g, None))
                box.append(back)

    def ask_suggestion_chip(self, group: str, position: int, item) -> Gtk.Widget:
        chip = Gtk.Box(spacing=2)
        if self.ask_editing == (group, position):
            entry = Gtk.Entry(text=prompt_text(item).strip(), max_length=smart_card.MAX_REQUEST, width_chars=28)
            self.guard_text_field(entry)
            entry.connect("activate", lambda e: self.finish_ask_prompt_edit(e.get_text()))
            keys = Gtk.EventControllerKey()
            keys.connect("key-pressed", self.on_ask_edit_key)
            entry.add_controller(keys)
            chip.append(entry)
            done = Gtk.Button(icon_name="object-select-symbolic", tooltip_text=_("Save"))
            done.add_css_class("am-small-btn")
            done.connect("clicked", lambda _b, e=entry: self.finish_ask_prompt_edit(e.get_text()))
            chip.append(done)
            GLib.idle_add(lambda: entry.grab_focus() and False)
            return chip
        label = _(EXAMPLES[item][1]) if isinstance(item, int) else item
        pick = Gtk.Button(label=label, tooltip_text=label)
        pick.get_child().set_ellipsize(Pango.EllipsizeMode.END)
        pick.get_child().set_max_width_chars(36)
        pick.add_css_class("am-chip")
        pick.connect("clicked", lambda _b, t=prompt_text(item): self.pick_ask_prompt(t))
        chip.append(pick)
        edit = Gtk.Button(icon_name="document-edit-symbolic", tooltip_text=_("Edit suggestion"))
        edit.add_css_class("am-small-btn")
        edit.connect("clicked", lambda _b: self.start_ask_prompt_edit(group, position))
        chip.append(edit)
        remove = Gtk.Button(icon_name="window-close-symbolic", tooltip_text=_("Remove suggestion"))
        remove.add_css_class("am-small-btn")
        remove.connect("clicked", lambda _b: self.remove_ask_prompt(group, position))
        chip.append(remove)
        return chip

    def on_ask_edit_key(self, _controller, keyval, _code, _state):
        from gi.repository import Gdk
        if keyval == Gdk.KEY_Escape:
            self.ask_editing = None
            self.fill_ask_suggestions()
            return True
        return False

    def ask_note(self, text: str):
        self.ask_prompt_note.set_label(text)
        self.ask_prompt_note.set_visible(bool(text))

    def store_ask_prompts(self, group: str, items) -> bool:
        """Keep `items` as the group's suggestions (None: the built-ins again), merged with what is stored
        now; another window's change of the same group is not overwritten."""
        try:
            stored = stored_prompts(ai_core.setting("ask_prompts"), self.ask_saved, group, items)
            ai_core.update_settings(ask_prompts=stored)
        except (OSError, ValueError):
            self.ask_saved = prompts_from_setting(ai_core.setting("ask_prompts"))
            self.ask_note(_("Could not save the suggestions."))
            ok = False
        else:
            self.ask_saved = prompts_from_setting(stored)
            ok = True
        self.ask_editing = None
        self.fill_ask_examples()
        self.fill_ask_suggestions()
        return ok

    def save_ask_prompt(self):
        """Save as suggestion: the question becomes one of the user's own (group general)."""
        text = one_line(self.ask_draft())
        items = prompt_items(self.ask_saved, "general")
        if not text:
            note = _("Write a request first, then save it as a suggestion.")
        elif len(text) > smart_card.MAX_REQUEST:
            note = _("Too long for a suggestion.")
        elif text in [prompt_text(i).strip() for i in items]:
            note = _("That is already a suggestion.")
        elif len(items) >= MAX_PROMPTS:
            note = _("At most {0} suggestions: remove one first.").format(MAX_PROMPTS)
        else:
            note = _("Saved as suggestion.") if self.store_ask_prompts("general", items + [text]) \
                else _("Could not save the suggestions.")
        self.ask_note(note)

    def remove_ask_prompt(self, group: str, position: int):
        items = prompt_items(self.ask_saved, group)
        if 0 <= position < len(items):
            del items[position]
            self.store_ask_prompts(group, items)

    def start_ask_prompt_edit(self, group: str, position: int):
        self.ask_editing = (group, position)
        self.ask_note("")
        self.fill_ask_suggestions()

    def finish_ask_prompt_edit(self, text: str):
        """✓ or Enter keeps the edit (empty removes the suggestion)."""
        if self.ask_editing is None:
            return
        group, position = self.ask_editing
        items = prompt_items(self.ask_saved, group)
        text = one_line(text)
        if position >= len(items) or text == prompt_text(items[position]).strip():
            self.ask_editing = None
            self.fill_ask_suggestions()
            return
        if not text:
            del items[position]
        elif len(text) > smart_card.MAX_REQUEST:
            self.ask_note(_("Too long for a suggestion."))
            return
        elif text in [prompt_text(i).strip() for n, i in enumerate(items) if n != position]:
            self.ask_note(_("That is already a suggestion."))
            return
        else:
            items[position] = text
        self.store_ask_prompts(group, items)

    # --- asking ---------------------------------------------------------------------------------------

    def reset_ask_exchange(self):
        """Forget the previous exchange (New question, a picked suggestion, closing the window)."""
        self.ask_context = None             # the bounded summary a follow-up carries
        self.ask_context_helper = None      # the helper it was made with
        self.ask_share = False              # share it with another helper (the user's tick)
        self.ask_reply = {}
        self.ask_reply_helper = None
        self.ask_request = ""
        self.ask_clarification = None
        self.ask_state = "idle"

    def ask_now(self, answer: str | None = None):
        """Ask the question in the field across the whole map, in the background; Cancel ends it."""
        helper = self.current_helper()
        request = self.ask_draft().strip()
        if helper is None or self.ask_running is not None:
            return
        context = self.ask_context if self.ask_context and helper.command == self.ask_context_helper else None
        if smart_card.request_problem(request, answer, context) is not None:
            self.update_ask_button()
            return
        facts = self.smart_facts(smart_card.AUTO, request, answer)
        if context:
            facts["context"] = context
        self.ask_request = request
        self.ask_clarification = answer
        self.ask_reply_helper = helper.command
        cancel = threading.Event()
        self.ask_running = cancel
        self.show_ask_state("working", helper=helper.name)

        def progress(text):
            GLib.idle_add(staged, text)

        def staged(text):
            if self.ask_running is cancel and self.ask_state == "working":
                self.show_ask_state("working", helper=helper.name, stage=text)
            return False

        def work():
            import ai_helper
            try:
                with ai_helper.watched(cancel):
                    result = smart_card.ask(helper, facts, progress=progress, cancel=cancel)
            except Exception:
                result = {"status": "error", "summary": smart_card.NOT_POSSIBLE}
            GLib.idle_add(done, result)

        def done(result):
            if self.ask_running is not cancel:
                return False            # cancelled: its answer is not shown
            self.ask_running = None
            self.ask_done(result)
            return False

        threading.Thread(target=work, daemon=True).start()

    def ask_done(self, result: dict):
        if result.get("status") == "cancelled":
            self.show_ask_state("idle")
            return
        result = self.drop_refused(result)
        if result.get("candidates"):
            result["proposal"] = self.ask_proposals.keep(result["candidates"])
        self.ask_reply = result
        self.show_ask_state(result.get("status", "error"), **result)

    def answer_clarification(self, text: str):
        """A suggested reply or the user's own words: the original question goes again with the answer;
        the question back becomes part of the exchange."""
        text = text.strip()
        helper = self.current_helper()
        if not text or len(text) > smart_card.MAX_ANSWER or helper is None or self.ask_running is not None:
            return
        self.ask_context = smart_card.clarify_context(
            self.ask_context if self.ask_context_helper == helper.command else None, self.ask_reply.get("question", ""))
        self.ask_context_helper = helper.command
        self.ask_now(answer=text)

    def ask_follow_up(self, text: str):
        """A follow-up about the answer: the previous exchange goes with it (bounded, in memory), to
        another helper only when the user ticked Share."""
        text = text.strip()
        helper = self.current_helper()
        if not text or len(text) > smart_card.MAX_REQUEST or helper is None or self.ask_running is not None:
            return
        evidence = smart_card.follow_context(self.ask_request, self.ask_clarification, self.ask_reply)
        self.ask_context = evidence if self.ask_reply_helper == helper.command or self.ask_share else None
        self.ask_context_helper = helper.command
        self.ask_share = False
        self.fill_ask(text)
        self.ask_now()

    def new_ask_question(self):
        """New question: an empty field, the previous exchange forgotten."""
        self.ask_text.get_buffer().set_text("")
        self.reset_ask_exchange()
        self.show_ask_state("idle")
        self.ask_text.grab_focus()

    def edit_ask_question(self):
        """Edit question: back to the field with the draft."""
        self.reset_ask_exchange()
        self.show_ask_state("idle")
        self.ask_text.grab_focus()

    def retry_ask(self):
        """Try again: the same question (and answer to a question back) once more, on this click."""
        answer = self.ask_clarification
        self.show_ask_state("idle")
        self.ask_now(answer=answer)

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
        if kept or result.get("status") in ("redirect", "answer"):
            return {**result, "candidates": kept}
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
            text = ""
            try:
                text = running.stop()
            except Exception:                        # whatever the engine answered: the button comes back
                text = ""
            finally:
                GLib.idle_add(done, text if isinstance(text, str) else "")

        def done(text):
            self.ask_mic.set_sensitive(True)
            now = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False)
            if text and now == before:               # not typed in already: put it at the cursor
                buffer.insert_at_cursor(("" if not now or now.endswith((" ", "\n")) else " ") + text)
            self.ask_text.grab_focus()
            return False
        threading.Thread(target=work, daemon=True).start()

    def stop_ask_dictation(self):
        """Closing Ask AI while recording stops the microphone."""
        if getattr(self, "ask_dictation", None) is not None and self.ask_mic.get_active():
            self.ask_mic.set_active(False)

    def cancel_ask(self):
        """Cancel: the Ask ends before its next helper run; its answer is not shown; the draft stays."""
        if self.ask_running is not None:
            self.ask_running.set()
            self.ask_running = None
        self.show_ask_state("idle")

    # --- the facts ------------------------------------------------------------------------------------

    def smart_facts(self, column: str, request: str, answer: str | None) -> dict:
        """What the helper is told (Maprisma's smart_ask facts), from what the app knows. The automatic
        column gets the facts of all four, the MCP ones under mcp_facts, plus the observed sign-in and
        update evidence and the facts of the built-in diagnostic questions."""
        auto = column == smart_card.AUTO
        updates = self.open_updates()
        facts = {"column": column, "request": request,
                 "action_rules": ai_core.setting("action_rules"),
                 "rule_hints": ai_core.setting("action_rule_hints")}
        if answer:
            facts["answer"] = answer[:smart_card.MAX_ANSWER]
        if auto or column == "agents":
            import install_ctl
            import install_recipes
            import terminal_pane
            agents = []
            fresh = self.agent_updates_fresh() if auto else False
            for h in harnesses.all_harnesses():
                agent = next((a for a in self.agent_list if a.command == h.command), None)
                installed = bool(agent and agent.installed)
                recipe = bool(h.install) or h.command in install_ctl.AGENT_INSTALLERS
                entry = {
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
                }
                if auto and installed:
                    entry["signed_in"] = self.sign_in_observed(h.command)
                    entry["update_available"] = (f"agent:{h.command}" in updates) if fresh else None
                agents.append(entry)
            facts["agents"] = agents
            facts["addons"] = (self.smart_addon_facts(agents=True) + self.smart_addon_facts(agents=False)) \
                if auto else self.smart_addon_facts(agents=True)
            facts["providers_of"] = self.smart_provider_links()
            if auto:
                mcp_open = self.mcp_updates_known()
                facts["mcp_updates"] = mcp_open
                facts["diagnostics"] = {
                    "agents": agents,
                    "agent_updates": [{"name": u.name, "current": u.current, "available": u.available}
                                      for k, u in updates.items() if k.startswith("agent:")] if fresh else None,
                    "mcp_updates": mcp_open}
        if auto or column == "providers":
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
            if not auto:
                facts["addons"] = self.smart_addon_facts(agents=False)
        if auto or column == "mcp":
            import mcp_ctl
            installed = [a for a in self.agent_list if a.installed]
            # Skill folders as short ids: their paths stay here, never in the prompt.
            sets = list(self.inventory.skill_sets)[:60]
            mcp = {"agents": [{"command": a.command, "name": a.name, "mcp": bool(mcp_ctl.supported(a.command))}
                              for a in installed],
                   "mcp": {m.name: list(m.agents) for m in self.inventory.mcp},
                   "skills": {f"s{i + 1}": list(s.agents) for i, s in enumerate(sets)}}
            facts["skill_paths"] = {f"s{i + 1}": s.path for i, s in enumerate(sets)}
            if auto:
                facts["mcp_facts"] = mcp
            else:
                facts.update(mcp)
        if auto or column == "models":
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

    @staticmethod
    def sign_in_observed(command: str):
        """Whether the agent is signed in as the map sees it (graph.signed_in), or None when its
        adapter names neither sign-in files nor keys: the app cannot tell (Maprisma's _signInObserved)."""
        import agent_status
        import graph
        access = agent_status.access_table().get(command)
        if access is None or (not access.files and not access.env):
            return None
        try:
            return bool(graph.signed_in(command))
        except OSError:
            return None

    @staticmethod
    def agent_updates_fresh(now: float | None = None) -> bool:
        """The update check of agents and local LLMs ran in the last six hours (agent_updates' cache)."""
        import time
        import agent_updates
        checked = ai_core.load_settings(agent_updates.CACHE).get("checked")
        now = time.time() if now is None else now
        return isinstance(checked, (int, float)) and 0 <= now - checked < agent_updates.CHECK_EVERY

    def mcp_updates_known(self):
        """The MCP server updates of a recent daily AI check, or None when there is no recent check."""
        import mcp_updates
        try:
            ok = mcp_updates._read(mcp_updates.CACHE).get("ok") is True and not mcp_updates.due()
        except (OSError, ValueError):
            ok = False
        if not ai_core.setting("ai_checks") or not ok:
            return None
        return [{"name": u.name, "current": u.current, "latest": u.latest} for u in self.open_ai_mcp_updates()]

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
        self.clear_box(box)
        self.ask_state = state
        self.update_ask_button()
        self.ask_helper_box.set_sensitive(state != "working")

        def text(label, css="", wrap=True, selectable=False):
            widget = Gtk.Label(label=label, xalign=0, wrap=wrap, selectable=selectable)
            widget.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
            if css:
                widget.add_css_class(css)
            box.append(widget)
            return widget

        def small(label, action, css="am-small-btn"):
            button = Gtk.Button(label=label, halign=Gtk.Align.START)
            button.add_css_class(css)
            button.connect("clicked", lambda *_: action())
            return button

        def footer(*buttons):
            row = Gtk.Box(spacing=8)
            for button in buttons:
                if button is not None:
                    row.append(button)
            box.append(row)

        if state == "idle":
            return
        if state == "working":
            stage = result.get("stage") or ""
            text(_(stage) if stage not in WORKING_STAGES else
                 _("{0} is checking {1}…").format(result.get("helper", ""), _("AI Map")))
            spinner = Gtk.Spinner(spinning=True, halign=Gtk.Align.START)
            box.append(spinner)
            footer(small(_("Cancel"), self.cancel_ask))
            return
        new = small(_("New question"), self.new_ask_question)
        if state == "clarify":
            text(result.get("question", ""), "am-setting-heading")
            choices = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=4)
            for choice in result.get("choices", []):
                button = Gtk.Button(label=choice)
                button.add_css_class("am-chip")
                button.connect("clicked", lambda _b, c=choice: self.answer_clarification(c))
                choices.append(button)
            box.append(choices)
            # Any other answer in the user's own words (Maprisma 078: also with no suggested replies).
            box.append(self.ask_response_field(clarification=True))
            footer(new)
            return
        if state in ("answer", "redirect"):
            instead = result.get("instead") or {}
            text(self.ask_request, "am-setting-heading")
            # The helper's words, or the app's own (translated).
            text(_(result.get("summary") or instead.get("hint", "")), selectable=True)
            for source in result.get("sources") or []:
                if smart_card.source_ok(source):
                    link = small(source, lambda u=source: self.open_ask_source(u), "flat")
                    link.get_child().set_ellipsize(Pango.EllipsizeMode.MIDDLE)
                    box.append(link)
            for candidate in result.get("candidates", []):
                if candidate.get("kind") == "agent_task":
                    box.append(small(_("Do it with {0}…").format(candidate.get("title", "")),
                                     lambda p=result.get("proposal"), c=candidate["id"]: self.apply_candidate(p, c)))
            box.append(self.ask_response_field())
            footer(new)
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
                review = Gtk.Button(label=_(review_label(candidate)), valign=Gtk.Align.CENTER)
                review.add_css_class("am-small-btn")
                review.connect("clicked", lambda _b, p=result.get("proposal"), c=candidate["id"]:
                               self.apply_candidate(p, c))
                card.append(review)
                box.append(card)
            box.append(self.ask_response_field())
            footer(new)
            return
        # error: the draft stays; Edit question, Try again (only on its click) or set up a helper.
        # The engine's own messages and the interface use English.
        text(_(result.get("summary") or "The action could not be completed."), "am-problem")
        helpers = getattr(self, "ask_helper_list", [])
        retry = small(_("Try again"), self.retry_ask)
        retry.set_sensitive(self.current_helper() is not None)
        footer(small(_("Edit question"), self.edit_ask_question), retry,
               None if helpers else small(_("Set up helper"), self.ask_set_up_helper))

    def ask_response_field(self, clarification: bool = False) -> Gtk.Widget:
        """The follow-up field under an answer, or the answer field under a question back."""
        column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        helper = self.current_helper()
        if not clarification and helper is not None and self.ask_reply_helper not in (None, helper.command):
            share = Gtk.CheckButton(label=_("Share the previous exchange with {0}").format(helper.name),
                                    active=self.ask_share)
            share.connect("toggled", lambda b: setattr(self, "ask_share", b.get_active()))
            column.append(share)
        row = Gtk.Box(spacing=8)
        limit = smart_card.MAX_ANSWER if clarification else smart_card.MAX_REQUEST
        entry = Gtk.Entry(hexpand=True, max_length=limit,
                          placeholder_text=_("Your answer") if clarification else _("Ask a follow-up…"))
        self.guard_text_field(entry)
        send = Gtk.Button(icon_name="mail-send-symbolic", tooltip_text=_("Ask"), sensitive=False)
        send.add_css_class("am-small-btn")

        def submit(*_args):
            words = entry.get_text().strip()
            if not words or self.current_helper() is None or self.ask_running is not None:
                return
            if clarification:
                self.answer_clarification(words)
            else:
                self.ask_follow_up(words)

        entry.connect("changed", lambda e: send.set_sensitive(bool(e.get_text().strip())
                                                              and self.current_helper() is not None))
        entry.connect("activate", submit)
        send.connect("clicked", submit)
        row.append(entry)
        row.append(send)
        column.append(row)
        return column

    def open_ask_source(self, url: str):
        if not smart_card.source_ok(url):
            return
        try:
            Gio.AppInfo.launch_default_for_uri(url, None)
        except GLib.Error:
            self.toast(_("Could not open the link."), 5)

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
