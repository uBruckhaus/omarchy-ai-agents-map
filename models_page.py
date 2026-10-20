"""AI Agents Map: the models page of a local LLM, and moving or deleting a model (a mixin of
ai_popup.AgentsMap, split off to keep ai_popup.py small; its methods use the window's state
through self)."""

from gi.repository import Adw, GLib, Gtk, Pango

import ai_core
import connect
import context_ctl
import datetime as dt
import install_ctl
import inventory
import jan_ctl
import kobold_ctl
import model_move
import model_names
import models_ctl
import os
import re
import runtime_ctl
import struct
import subprocess
import threading
import time
import web_sources
from i18n import _, N_, ngettext
from pathlib import Path
from ui_helpers import glyph_label, pick_cards, short_number, size_text
from window_rules import MODEL_LIST_WIDTH


class ModelsPage:
    # --- models of a local LLM (right-click → Models…) ---------------------------------------------

    MODEL_TABS = (("installed", N_("Installed")), ("web", N_("Get from Hugging Face")), ("local", N_("On this PC")))
    FIT_TEXT = {"fits": N_("Fits your graphics card"), "partly": N_("Partly in system memory: works, slower"),
                "too_large": N_("Likely too large")}

    def build_models_page(self) -> Gtk.Widget:
        """A local LLM's models, laid out like LM Studio's model search: a list on the left, the
        chosen model's page on the right. Tabs: Installed (info, context, Remove to the trash),
        Get from Hugging Face (search, model page, download options), On this PC (GGUF files
        other local LLMs or Downloads have)."""
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP)
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
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP)
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
        self.models_installed_detail = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP)
        installed.append(self.scrolled(self.models_installed_detail, hexpand=True))
        self.models_stack.add_named(installed, "installed")

        # Get from Hugging Face: search on the left, the model's page on the right.
        web = Gtk.Box(spacing=18)
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP)
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
        self.models_web_detail = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP)
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
        local_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP)
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

    def show_models_tab(self, key: str, save: bool = False):
        self.models_stack.set_visible_child_name(key)
        if save:
            stored = ai_core.setting("add_where")
            self.save_setting(add_where={**(stored if isinstance(stored, dict) else {}), "models": key})
        state = self.models_state
        if state is not None and key == "web" and not state.get("searched"):
            self.search_models()                 # trending models to start with
        self.update_model_buttons()

    def open_models(self, rt, into=None):
        """into: download into this folder (a folder card's Add model on Setup), else rt's own."""
        self.models_state = {"rt": rt, "busy": False, "cancel": False, "from": self.view, "repo": None,
                             "vram": install_ctl.detect_gpu().vram, "ram": models_ctl.system_ram(),
                             "scanned": False, "scanning": False, "searched": False,
                             "into": str(into) if into else ""}
        while (row := self.models_local.get_first_child()) is not None:
            self.models_local.remove(row)
        self.models_local_status.set_label("")
        self.models_title.set_label(_("Models of {0}").format(rt.name).upper())
        self.models_folder_label.set_label(_("Model folder: {0}").format(inventory.tilde(into or rt.models_dir))
                                           if into or rt.models_dir else "")
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
        stored = ai_core.setting("add_where")
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
        facts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP)
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
        area = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP)
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
                card_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP)
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
                "into": state.get("into") or "",
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
        try:                                   # a folder chosen on Setup (its card's Add model)
            into = install_ctl.check_folder(spec["into"]) if spec.get("into") and rt.key != "ollama" else None
        except ValueError as error:
            self.show_note(_("Not downloaded: {0}").format(error))
            return None

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
                                         base=spec["base"], into=into)
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
        # Every model of the local LLM, also those past the map's ten (Setup lists them all).
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

