"""AI Agents Map: the Settings, Reorganize models and Help pages of the
window (a mixin of ai_popup.AgentsMap, split off to keep ai_popup.py small)."""

import os
import re
import sys
import threading
import time
from pathlib import Path

from gi.repository import Adw, Gio, GLib, Gtk, Pango

import json

import ai_core
import app_entry
import corners
import desktop
import disk_usage
import i18n
import install_ctl
import inventory
import llm_engine
import model_layout
import previews
import provider_watch
import runtime_ctl
import web_sources
from i18n import _, N_, ngettext
from ui_helpers import SpaceBar, size_text
try:
    import addon_api            # Asterism only (not in the Omarchy plugin); hooks are empty without it
except ImportError:
    addon_api = None


class SettingsPages:
    # --- settings ----------------------------------------------------------------

    def build_settings_page(self) -> Gtk.Widget:
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
        back.connect("clicked", lambda *_: self.close_settings())
        header.append(back)
        title = Gtk.Label(label=_("Settings").upper(), xalign=0, hexpand=True)
        title.add_css_class("am-section-title")
        header.append(title)
        page.append(header)
        rule = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        rule.add_css_class("am-settings-rule")
        page.append(rule)
        self.settings_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP, margin_bottom=12)
        self.settings_box.add_css_class("am-settings")
        # One column: at most 760 px wide, centred. Two columns: the whole width, half and half.
        # Pixels, not libadwaita's default "sp", so the page does not grow and shrink with the text size.
        clamp = Adw.Clamp(maximum_size=760, tightening_threshold=560, child=self.settings_box,
                          unit=Adw.LengthUnit.PX)
        self.settings_clamp = clamp
        page.append(self.page_scroller(clamp))
        return page

    @staticmethod
    def ellipsize_dropdown(drop: Gtk.DropDown):
        """The chosen entry ends in … when it is long (GTK otherwise makes the dropdown as wide as
        its longest entry, and the page wider than the window); the open list shows it whole."""
        def setup(_factory, item, ellipsize):
            label = Gtk.Label(xalign=0)
            if ellipsize:
                label.set_ellipsize(Pango.EllipsizeMode.END)
                label.set_max_width_chars(1)
                label.set_hexpand(True)
            item.set_child(label)

        def bind(_factory, item):
            value = item.get_item()
            text = value.get_string() if hasattr(value, "get_string") else str(value)
            item.get_child().set_label(text)
            item.get_child().set_tooltip_text(text)

        for ellipsize, setter in ((True, drop.set_factory), (False, drop.set_list_factory)):
            factory = Gtk.SignalListItemFactory()
            factory.connect("setup", setup, ellipsize)
            factory.connect("bind", bind)
            setter(factory)

    def setting_line(self, box, title: str, hint: str, control) -> Adw.ActionRow:
        """A form line as a libadwaita row: title (a short explanation under it, a long one on
        hover) and the control at its end. Consecutive lines in a box share one boxed list."""
        last = box.get_last_child()
        if isinstance(last, Gtk.ListBox) and last.has_css_class("am-form-list"):
            listbox = last
        else:
            listbox = self.boxed_list()
            listbox.add_css_class("am-form-list")
            box.append(listbox)
        short = hint if hint and len(hint) <= 70 else ""
        row = self.plain_row(title, short)
        if hint:
            row.set_tooltip_text(hint)
        control.set_valign(Gtk.Align.CENTER)
        # Fields, lists and text areas take the row's width; switches, check boxes and buttons sit at its end.
        wide = not isinstance(control, (Gtk.Switch, Gtk.CheckButton, Gtk.Button, Gtk.Label))
        control.set_hexpand(wide)
        control.set_halign(Gtk.Align.FILL if wide else Gtk.Align.END)
        if wide:
            control.set_size_request(260, -1)
        if isinstance(control, Gtk.DropDown):
            self.ellipsize_dropdown(control)
        row.add_suffix(control)
        listbox.append(row)
        return row

    # --- the Add pages' shared parts: On this PC | From the web, and the site to search ---------

    WHERE_DEFAULT = {"models": "web", "mcp": "local", "skills": "local"}

    def where(self, kind: str) -> str:
        value = ai_core.setting("add_where")
        value = value.get(kind) if isinstance(value, dict) else None
        return value if value in ("local", "web") else self.WHERE_DEFAULT[kind]

    def where_switch(self, kind: str, local_box, web_box, on_change=None) -> Gtk.Widget:
        """The segmented switch at the top of an Add page; the choice is kept per page."""
        switch = Gtk.Box(spacing=0, halign=Gtk.Align.START)
        switch.add_css_class("am-segmented")
        buttons = {}

        def show(where: str, save: bool):
            local_box.set_visible(where == "local")
            web_box.set_visible(where == "web")
            if not save:
                return                       # while the page is built
            stored = ai_core.setting("add_where")
            self.save_setting(add_where={**(stored if isinstance(stored, dict) else {}), kind: where})
            if on_change is not None:
                on_change()

        group = None
        for where, label in (("local", _("On this PC")), ("web", _("From the web"))):
            button = Gtk.ToggleButton(label=label, group=group)
            group = group or button
            button.add_css_class("am-segment")
            switch.append(button)
            buttons[where] = button
        start = self.where(kind)
        buttons[start].set_active(True)
        show(start, False)
        for where, button in buttons.items():
            button.connect("toggled", lambda b, w=where: b.get_active() and show(w, True))
        setattr(self, f"where_{kind}", lambda: "local" if buttons["local"].get_active() else "web")
        return switch

    SOURCE_HINTS = {
        "models": N_("Hugging Face, or a site with the same API (a mirror such as hf-mirror.com)."),
        "mcp": N_("The official MCP registry, or another registry with its API (…/v0/servers)."),
        "skills": N_("skills.sh, a site with its API, or a GitHub repository with skills "
                     "(https://github.com/owner/repo: lists its folders with a SKILL.md)."),
    }

    def source_picker(self, kind: str) -> Gtk.Widget:
        """Which site to search: the default or one the user added (kept in the settings)."""
        column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP_TIGHT)
        row = Gtk.Box(spacing=8)
        row.add_css_class("am-line")         # compact dropdown, as on the settings page
        label = Gtk.Label(label=_("Search on"), xalign=0)
        label.add_css_class("am-agent-detail")
        row.append(label)
        drop = Gtk.DropDown.new_from_strings([""])
        drop.set_hexpand(False)
        row.append(drop)
        row.append(Gtk.Box(hexpand=True))
        add = Gtk.Button(label=_("Add a site…"))
        add.add_css_class("am-small-btn")
        row.append(add)
        remove = Gtk.Button(icon_name="user-trash-symbolic")
        remove.add_css_class("am-small-btn")
        remove.set_tooltip_text(_("Remove this site"))
        row.append(remove)
        column.append(row)
        adder = Gtk.Box(spacing=8, visible=False)
        entry = Gtk.Entry(hexpand=True, max_length=300, placeholder_text="https://")
        entry.add_css_class("am-path")
        self.guard_text_field(entry)
        adder.append(entry)
        save = Gtk.Button(label=_("Save"))
        save.add_css_class("am-small-btn")
        save.add_css_class("am-primary")
        adder.append(save)
        column.append(adder)
        hint = Gtk.Label(label=_(self.SOURCE_HINTS[kind]), xalign=0, wrap=True, max_width_chars=1, visible=False)
        hint.add_css_class("am-agent-detail")
        column.append(hint)
        state = {"urls": [], "filling": False}

        def fill():
            state["filling"] = True
            state["urls"] = web_sources.sources(kind)
            labels = [web_sources.label(u) + (" · " + _("default") if i == 0 else "")
                      for i, u in enumerate(state["urls"])]
            drop.set_model(Gtk.StringList.new(labels))
            picked = web_sources.chosen(kind)
            drop.set_selected(state["urls"].index(picked))
            remove.set_sensitive(picked != web_sources.DEFAULTS[kind])
            state["filling"] = False

        def picked(*_):
            if state["filling"]:
                return
            index = drop.get_selected()
            if 0 <= index < len(state["urls"]):
                web_sources.choose(kind, state["urls"][index])
                remove.set_sensitive(index > 0)

        def toggle_adder(*_):
            showing = not adder.get_visible()
            adder.set_visible(showing)
            hint.set_visible(showing)
            entry.remove_css_class("error")
            if showing:
                entry.grab_focus()

        def save_site(*_args):
            try:
                web_sources.add(kind, entry.get_text())
            except (ValueError, OSError) as error:
                entry.add_css_class("error")
                hint.set_label(_(self.SOURCE_HINTS[kind]) + " " + _("Not added: {0}").format(error))
                return
            entry.set_text("")
            hint.set_label(_(self.SOURCE_HINTS[kind]))
            adder.set_visible(False)
            hint.set_visible(False)
            fill()

        def remove_site(*_):
            index = drop.get_selected()
            if 0 < index < len(state["urls"]):
                web_sources.remove(kind, state["urls"][index])
                fill()

        drop.connect("notify::selected", picked)
        add.connect("clicked", toggle_adder)
        entry.connect("activate", save_site)
        save.connect("clicked", save_site)
        remove.connect("clicked", remove_site)
        fill()
        return column

    @staticmethod
    def settings_heading(box, text: str):
        """A section heading on a page (libadwaita's heading style)."""
        label = Gtk.Label(label=text, xalign=0)
        label.add_css_class("heading")
        box.append(label)

    def dropdown(self, labels: list[str], selected: int, on_change) -> Gtk.DropDown:
        drop = Gtk.DropDown.new_from_strings(labels)
        drop.set_selected(max(0, selected))
        drop.connect("notify::selected", lambda d, _p: self.settings_ready and on_change(d.get_selected()))
        return drop

    def fill_settings(self):
        """The settings as libadwaita preference groups (in the theme's colours on Omarchy)."""
        self.settings_ready = False
        box = self.settings_box
        while (child := box.get_first_child()) is not None:
            box.remove(child)
        # Two columns when the window is wide enough (General left; the app, Models and Window right),
        # else one, so everything fits without scrolling.
        wide = self.window is not None and self.window.get_width() >= 900
        self.settings_two_columns = wide
        self.settings_clamp.set_maximum_size(100000 if wide else 760)
        self.settings_clamp.set_tightening_threshold(100000 if wide else 560)
        columns = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL if wide else Gtk.Orientation.VERTICAL,
                          spacing=24 if wide else 18, homogeneous=wide)
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP, valign=Gtk.Align.START)
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP, valign=Gtk.Align.START)
        columns.append(left)
        columns.append(right)
        box.append(columns)
        self.fill_general_settings(left, right)
        if desktop.launcher_entry():
            self.fill_launcher_setting(right)
            right.reorder_child_after(right.get_last_child(), None)     # the app's own entry first on the right
        if hasattr(self, "open_addons"):
            group = self.preference_group(right, _("Add-ons"))
            self.switch_row(group, _("Look for add-ons online"),
                            _("Reads the list of add-ons (signed by Uwe Bruckhaus) from GitHub every few hours, to "
                              "suggest one when a harness it knows is on this PC. Nothing is sent but the request."),
                            ai_core.setting("addons_online") is not False,
                            lambda on: self.save_setting(addons_online=on))
            self.button_row(group, _("Add-ons"), _("Agents, providers and features: what is built in, installed "
                                                   "and available."), _("Open"), self.open_addons)
            if self.adapter_addons():
                self.button_row(group, _("Setup"), _("Switch agents and providers on or off, update or remove "
                                                         "them, and your MCP servers, skills and models."), _("Open"),
                                self.open_my_setup)
        for addon_id, builder in self.addon_hooks("settings"):
            addon_api.call(addon_id, builder, addon_api.UI(self, addon_id), right)
        group = self.preference_group(right, _("About"))
        self.button_row(group, _("About {0}").format(desktop.app_name()),
                        _("Version, author, license and the project on GitHub."), _("About"), self.show_about)
        self.settings_ready = True

    # --- About: name, version, author, license and the GitHub project ------------------------------

    ASTERISM_URL = "https://github.com/uBruckhaus/Asterism"
    PLUGIN_URL = "https://github.com/uBruckhaus/omarchy-ai-agents-map"

    @classmethod
    def about_facts(cls) -> tuple[str, str]:
        """(version, project page): the plugin's manifest.json when the app runs as the Omarchy
        plugin (it lies next to this file there), else Asterism's own version."""
        try:
            manifest = json.loads((Path(__file__).resolve().parent / "manifest.json").read_text())
            return str(manifest.get("version", "")), cls.PLUGIN_URL
        except (OSError, ValueError):
            pass
        try:
            import addons            # Asterism only
            return addons.ASTERISM_VERSION, cls.ASTERISM_URL
        except ImportError:
            return "", cls.ASTERISM_URL

    def show_about(self):
        version, url = self.about_facts()
        about = Adw.AboutDialog(
            application_name=desktop.app_name(), application_icon=desktop.app_id(), version=version,
            developer_name="Uwe Bruckhaus", copyright="© 2026 Uwe Bruckhaus", license_type=Gtk.License.MIT_X11,
            # No comments: with them libadwaita moves the website into Details; this way the GitHub
            # project page is a row on the dialog's first page.
            website=url, issue_url=url + "/issues")
        about.present(self.window)

    def preference_group(self, box, title: str) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(title=GLib.markup_escape_text(title))
        box.append(group)
        return group

    @staticmethod
    def short_hint(hint: str) -> str:
        """A long explanation's first sentence (the whole one goes into the tooltip)."""
        first = re.split(r"(?<=[.;])\s", hint, maxsplit=1)[0].rstrip(".;") + "."
        return first if len(hint) > 90 and len(first) < len(hint) else hint

    def switch_row(self, group, title: str, hint: str, active: bool, on_toggle) -> Adw.SwitchRow:
        short = self.short_hint(hint)
        row = Adw.SwitchRow(title=title, subtitle=short, active=active, use_markup=False)
        if short != hint:
            row.set_tooltip_text(hint)
        row.connect("notify::active", lambda r, *_: self.settings_ready and on_toggle(r.get_active()))
        group.add(row)
        return row

    def combo_row(self, group, title: str, hint: str, labels: list[str], selected: int, on_change) -> Adw.ComboRow:
        """A long explanation would squeeze the chosen value: its first sentence shows, all of it on hover."""
        short = self.short_hint(hint)
        row = Adw.ComboRow(title=title, subtitle=short, model=Gtk.StringList.new(labels), use_markup=False)
        if short != hint:
            row.set_tooltip_text(hint)
        row.set_selected(max(0, selected))
        row.connect("notify::selected", lambda r, *_: self.settings_ready and on_change(r.get_selected()))
        # The framed value and arrow at their own height, centred, not the row's (see the CSS).
        stack = [row]
        while stack:
            widget = stack.pop()
            if widget.has_css_class("suffixes"):
                widget.set_valign(Gtk.Align.CENTER)
                break
            child = widget.get_first_child()
            while child is not None:
                stack.append(child)
                child = child.get_next_sibling()
        group.add(row)
        return row

    def button_row(self, group, title: str, hint: str, label: str, on_click) -> Adw.ActionRow:
        row = Adw.ActionRow(title=title, subtitle=hint, use_markup=False)
        button = Gtk.Button(label=label, valign=Gtk.Align.CENTER)
        button.connect("clicked", lambda *_: on_click())
        row.add_suffix(button)
        row.set_activatable_widget(button)
        group.add(row)
        return row

    def folder_row(self, group, title: str, hint: str, text: str, on_save) -> Adw.EntryRow:
        """A folder to type: Enter or the apply button saves it (on_save(row) marks errors)."""
        row = Adw.EntryRow(title=title, text=text, show_apply_button=True, use_markup=False)
        row.set_tooltip_text(hint)
        row.connect("apply", lambda r: on_save(r))
        row.connect("entry-activated", lambda r: on_save(r))
        self.guard_text_fields_in(row)
        group.add(row)
        return row

    def fill_launcher_setting(self, box):
        group = self.preference_group(box, _("Omarchy app"))
        self.switch_row(group, _("In the app launcher"),
                        _("AI Agents Map as an app in Omarchy's launcher (Super + Space), besides the bar icon."),
                        ai_core.setting("launcher_entry") is True,
                        lambda on: (self.save_setting(launcher_entry=on), app_entry.ensure_entry(on)))

    def fill_general_settings(self, box, side):
        """General in `box`; Models and Window in `side` (the right column, or below)."""
        group = self.preference_group(box, _("General"))
        codes = ["auto"] + [code for code, _name in i18n.LANGUAGES]
        setting = i18n.language_setting()
        self.combo_row(group, _("Language"), _("AI Agents Map restarts in the new language."),
                       [_("System ({0})").format(i18n.language_name(i18n.system_language()))]
                       + [name for _code, name in i18n.LANGUAGES],
                       codes.index(setting) if setting in codes else 0, lambda i: self.set_language(codes[i]))
        self.button_row(group, _("Open problems"), _("What needs fixing; the ! in the header shows when there is "
                                                    "something"), _("Open"), self.open_health)
        self.window_rounding = desktop.window_rounding()
        like = _("Like windows ({0} px)").format(self.window_rounding) if self.window_rounding is not None \
            else _("Like windows")
        self.combo_row(group, _("Corners"), self.corners_tip(), [like, _("Rounded"), _("Square")],
                       corners.CHOICES.index(ai_core.saved_corners()),
                       lambda i: (self.save_setting(corners=corners.CHOICES[i]), self.apply_theme_css()))
        self.combo_row(group, _("Text in choice boxes"),
                       _("The chosen value in drop-down boxes: bright (white on dark themes, the theme's text colour "
                         "on light ones) or dimmed, as before."),
                       [_("Bright"), _("Dimmed")], 1 if ai_core.setting("choice_text") == "dim" else 0,
                       lambda i: (self.save_setting(choice_text=("bright", "dim")[i]), self.apply_theme_css()))
        if desktop.NAME == "omarchy":
            look = self.combo_row(group, _("Look"),
                           _("The colours, light or dark: from the Omarchy theme, or GNOME's Adwaita (light or dark "
                             "and the accent colour as set in the system). Everything else works the same."),
                           [_("Omarchy theme"), _("GNOME (Adwaita)")], ai_core.LOOKS.index(ai_core.saved_look()),
                           lambda i: self.set_look(ai_core.LOOKS[i]))
            # One column: a line break after "Adwaita", before its bracket (the same place in every
            # language). Two columns are too narrow for it: the text just wraps.
            if not getattr(self, "settings_two_columns", False):
                look.set_subtitle(look.get_subtitle().replace(" (", "\n(", 1))
        self.switch_row(group, _("Look up new agents"),
                        _("When a new agent is installed, Claude Code or Codex finds out on the web where it keeps "
                          "MCP servers and skills (once, a few minutes in the background)."),
                        ai_core.setting("look_up_new_agents"), lambda on: self.save_setting(look_up_new_agents=on))
        self.switch_row(group, _("Keep the MCP server list"),
                        _("When the app starts, the MCP registry's list of servers is saved in the background "
                          "(at most every 12 hours, then only what changed), so searching it takes no time."),
                        ai_core.setting("mcp_list") is not False, lambda on: self.save_setting(mcp_list=on))
        self.switch_row(group, _("Terminal tabs above the map"),
                        _("While agents run in terminals here: a tab for each, with a dot for working, running "
                          "or ended, above the map and the terminals. A click shows the terminal."),
                        ai_core.setting("terminal_tabs") is not False,
                        lambda on: (self.save_setting(terminal_tabs=on), self.update_terminal_tabs()))
        self.switch_row(group, _("Start local LLMs with their agents"),
                        _("When an agent connected to a local LLM starts, its LLM starts too; when the last such "
                          "agent closes, the LLM stops and its GPU memory is free again. Works with this window "
                          "closed and after a reboot (user service {0}).").format(provider_watch.UNIT_NAME),
                        ai_core.setting("providers_with_agents"),
                        lambda on: (self.save_setting(providers_with_agents=on), self.sync_provider_watch()))
        self.folder_row(group, _("Agents start in"),
                        _("The folder an agent started here (Start on its card) works in. Enter saves it."),
                        self.agent_folder(), self.save_agent_folder)
        # The map's animations, with a sketch of what each choice animates (its own group: libadwaita
        # puts what is not a row after a group's list).
        group = self.preference_group(side, _("Map"))
        modes = ("all", "agents", "off")
        self.combo_row(group, _("Map animations"),
                       _("Light runs along the lines on the map: a working agent's line to its provider and "
                         "model and to an MCP server it calls, and a local LLM's line to the model another "
                         "program uses now. Also off when the system turns animations off."),
                       [_("Agents and other programs"), _("Only my agents"), _("Off")],
                       modes.index(ai_core.map_animations()),
                       lambda i: (self.save_setting(map_animations=modes[i]), self.side_map.set_animate(modes[i]),
                                  self.animation_sketch.queue_draw()))
        preview = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        preview.add_css_class("am-help-card")
        preview.set_halign(Gtk.Align.START)
        self.animation_sketch = previews.AnimationSketch(self.side_map.color, ai_core.map_animations,
                                                         (_("agent"), _("local LLM"), _("model"), _("model"),
                                                          _("in use · {0}").format(_("another program"))))
        preview.append(self.animation_sketch)
        group.add(preview)
        models_group = self.preference_group(side, _("Models"))

        group = models_group
        models = self.folder_row(group, _("Models folder"),
                                 _("One folder for the models of every local LLM. On this PC (Add models) looks "
                                   "here too."), self.models_root(), self.save_models_root)
        choose = Gtk.Button(icon_name="folder-open-symbolic", valign=Gtk.Align.CENTER)
        choose.add_css_class("flat")
        choose.set_tooltip_text(_("Choose…"))
        choose.connect("clicked", lambda *_: self.choose_models_root(models))
        models.add_suffix(choose)
        self.button_row(group, _("Tidy up"),
                        _("Moves every local LLM's models into this folder in one layout and points the local LLMs "
                          "there. Shows a preview first; Undo puts everything back."),
                        _("Reorganize models…"), lambda: (self.save_models_root(models), self.open_reorganize()))

    def set_look(self, look: str):
        """Settings › Look: the new colours at once (the cards, the map, the terminal's next start)."""
        self.save_setting(look=look)
        self.theme.reload()
        self.apply_theme_css()
        self.refresh_map()

    def save_agent_folder(self, entry):
        text = entry.get_text().strip()
        path = os.path.realpath(os.path.expanduser(text)) if text else ""
        if not path or not os.path.isdir(path) or "\0" in path:
            entry.add_css_class("error")
            return
        entry.remove_css_class("error")
        if self.settings_ready:
            self.save_setting(agent_folder=path)

    @staticmethod
    def corners_tip() -> str:
        """Where the windows' rounding comes from (as in App Manager and Theme Manager)."""
        return desktop.corners_note()

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
        os.environ["AI_AGENTS_MAP_PAGE"] = "settings"
        os.environ["AI_AGENTS_MAP_VIEW"] = self.view
        sys.stdout.flush()
        sys.stderr.flush()
        os.execv(sys.executable, [sys.executable, *sys.argv])

    def remember_page(self):
        """The page right of the cards before Settings or Help opened (a terminal, details…)."""
        inner = self.right_stack.get_visible_child_name() if self.stack.get_visible_child_name() == "main" else None
        self.return_page = inner if inner and inner != "map" else None

    def back_to_last_page(self):
        page = getattr(self, "return_page", None)
        self.return_page = None
        self.show_view(self.view, save=False)
        if page and self.right_stack.get_child_by_name(page) is not None:
            self.stack.set_visible_child_name("main")
            self.right_stack.set_visible_child_name(page)
            self.place_panes()
            self.update_header()
            command = page[5:] if page.startswith("term:") else ""
            if command in getattr(self, "sessions", {}):
                GLib.idle_add(lambda: self.sessions[command].terminal.grab_focus() and False)

    def open_settings(self):
        if self.page_name() not in ("settings", "help", "info"):
            self.remember_page()
        self.fill_settings()
        self.show_page("settings")

    # --- one models folder, and Reorganize (a preview first) ---------------------------------------

    def models_root(self) -> str:
        """The setting, else where the local LLMs keep models now (in their engines' models_root_rank:
        LM Studio's folder first, then Ollama's)."""
        import llm_engine
        value = ai_core.setting("models_root")
        if isinstance(value, str) and value.startswith(("/", "~")):
            return value
        ranked = sorted((llm_engine.of(r).models_root_rank, i, r.models_dir)
                        for i, r in enumerate(self.inventory.runtimes) if r.installed and r.models_dir)
        return ranked[0][2] if ranked else "~/Models"

    def save_models_root(self, entry):
        text = entry.get_text().strip()
        path = os.path.realpath(os.path.expanduser(text)) if text else ""
        try:
            install_ctl.check_folder(path)      # the local LLMs' settings get it (no quotes, $, control characters)
        except ValueError as error:
            entry.add_css_class("error")
            entry.set_tooltip_text(str(error))
            return
        if os.path.exists(path) and not os.path.isdir(path):
            entry.add_css_class("error")
            return
        entry.set_tooltip_text(None)
        entry.remove_css_class("error")
        self.save_setting(models_root=inventory.tilde(path))

    def choose_models_root(self, entry):
        dialog = Gtk.FileDialog(title=_("Models folder"), modal=True)
        current = Path(os.path.expanduser(entry.get_text().strip() or "~"))
        if current.is_dir():
            dialog.set_initial_folder(Gio.File.new_for_path(str(current)))

        def done(source, result):
            try:
                folder = source.select_folder_finish(result)
            except GLib.Error:
                return                     # cancelled
            if folder is not None and folder.get_path():
                entry.set_text(inventory.tilde(folder.get_path()))
                self.save_models_root(entry)
        dialog.select_folder(self.window, None, done)

    def folder_holders(self, steps) -> list:
        """The running local LLMs whose model folder moves and that hold their files open while they
        run (engine stop_to_move: Ollama): stopped for the move, started again after."""
        moving = {st.provider for st in steps if st.kind == "folder"}
        return [rt for rt in self.inventory.runtimes if rt.key in moving and rt.running
                and llm_engine.of(rt).stop_to_move and runtime_ctl.can_control(rt)]

    @staticmethod
    def stop_holders(holders) -> list:
        """Stop them (on the worker thread); the ones that stopped, to start again."""
        return [rt for rt in holders if runtime_ctl.stop(rt)[0]]

    def layout_providers(self) -> list:
        found = []
        for rt in self.inventory.runtimes:
            if not rt.installed or not llm_engine.of(rt).reorganize:
                continue
            inst = install_ctl.installer(rt.key)
            ours = inst is not None and inst.installed_folder() is not None
            folder = Path(os.path.expanduser(rt.models_dir)) if rt.models_dir else None
            found.append(model_layout.Provider(rt.key, rt.name, folder, ours, rt.running))
        return found

    def set_provider_folder(self, key: str, path: Path):
        """The back reflection: a local LLM's own model folder setting (restarts it when it runs)."""
        inst = install_ctl.installer(key)
        if inst is None:
            raise RuntimeError(f"{key}: no settings to change")
        inst.set_models_folder(path)

    def build_reorganize_page(self) -> Gtk.Widget:
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
        back.connect("clicked", lambda *_: self.close_reorganize())
        header.append(back)
        title = Gtk.Label(label=_("Reorganize models").upper(), xalign=0, hexpand=True)
        title.add_css_class("am-section-title")
        header.append(title)
        page.append(header)
        rule = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        rule.add_css_class("am-settings-rule")
        page.append(rule)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP)
        body.add_css_class("am-settings")
        self.reorg_intro = Gtk.Label(xalign=0, wrap=True, max_width_chars=1)
        self.reorg_intro.add_css_class("am-detail-value")
        body.append(self.reorg_intro)
        self.reorg_keep = Gtk.Switch(active=True, valign=Gtk.Align.CENTER)
        self.reorg_keep.connect("notify::active", lambda *_: self.reorg_ready and self.plan_reorganize())
        self.setting_line(body, _("Keep old paths working"),
                          _("A link at each old place, so scripts and programs that use a model's old path keep "
                            "working. Inside a hand-installed local LLM's folder there is always one."),
                          self.reorg_keep)
        self.reorg_problems = Gtk.Label(xalign=0, wrap=True, max_width_chars=1, visible=False)
        self.reorg_problems.add_css_class("am-state-warn")
        body.append(self.reorg_problems)
        heading = Gtk.Label(label=_("Preview").upper(), xalign=0)
        heading.add_css_class("am-group-heading")
        body.append(heading)
        self.reorg_preview = Gtk.TextView(editable=False, cursor_visible=False, monospace=True)
        self.reorg_preview.add_css_class("am-diff")
        self.reorg_preview.set_wrap_mode(Gtk.WrapMode.CHAR)
        body.append(self.reorg_preview)
        self.reorg_bar = Gtk.ProgressBar(visible=False)
        self.reorg_bar.add_css_class("am-progress")
        body.append(self.reorg_bar)
        self.reorg_status = Gtk.Label(xalign=0, wrap=True, max_width_chars=1)
        self.reorg_status.add_css_class("am-agent-detail")
        body.append(self.reorg_status)
        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        self.reorg_undo = Gtk.Button(label=_("Undo the last reorganize"))
        self.reorg_undo.add_css_class("am-small-btn")
        self.reorg_undo.connect("clicked", lambda *_: self.undo_reorganize())
        buttons.append(self.reorg_undo)
        self.reorg_apply = Gtk.Button(label=_("Reorganize"))
        self.reorg_apply.add_css_class("am-small-btn")
        self.reorg_apply.add_css_class("am-primary")
        self.reorg_apply.connect("clicked", lambda *_: self.apply_reorganize())
        buttons.append(self.reorg_apply)
        body.append(buttons)
        page.append(self.page_scroller(body))
        self.reorg_plan = None
        self.reorg_busy = False
        self.reorg_ready = False
        return page

    def open_reorganize(self):
        self.reorg_ready = True
        self.show_page("reorganize")
        self.update_header()
        self.plan_reorganize()

    def close_reorganize(self):
        if self.reorg_busy:
            return                          # moving files: stays until done
        self.open_settings()

    def plan_reorganize(self):
        root = Path(os.path.expanduser(self.models_root()))
        keep = self.reorg_keep.get_active()
        providers = self.layout_providers()
        self.reorg_plan = None
        self.reorg_apply.set_sensitive(False)
        self.reorg_undo.set_visible(bool(model_layout.load_journal()))
        self.reorg_intro.set_label(_("Everything goes into {0}: <publisher>/<model>/ (LM Studio's layout), local/<name>/ "
                                     "for files without a publisher, ollama/ for Ollama's store, llama.cpp/ with "
                                     "links to every model for llama.cpp and KoboldCpp installed here.")
                                   .format(inventory.tilde(root)))
        self.reorg_status.set_label(_("Working out what would change…"))
        self.reorg_preview.get_buffer().set_text("")

        def work():
            try:
                plan, error = model_layout.plan(root, providers, keep), ""
            except OSError as problem:
                plan, error = None, str(problem)
            GLib.idle_add(done, plan, error)

        def done(plan, error):
            if self.page_name() != "reorganize" or self.reorg_busy:
                return False
            self.reorg_plan = plan
            if plan is None:
                self.reorg_status.set_label(_("Could not plan it: {0}").format(error))
                return False
            self.reorg_preview.get_buffer().set_text(self.reorg_text(plan))
            problems = [self.reorg_problem_text(p) for p in plan.problems[:8]]
            self.reorg_problems.set_label("\n".join(problems))
            self.reorg_problems.set_visible(bool(problems))
            self.reorg_apply.set_sensitive(not plan.problems and not plan.empty)
            self.reorg_status.set_label(_("Nothing to do: the models are already in place.") if plan.empty else
                                        _("Nothing changes until you press Reorganize."))
            return False

        threading.Thread(target=work, daemon=True).start()

    def reorg_problem_text(self, problem: str) -> str:
        if problem == "root":
            return _("Pick a models folder of its own (not / or your home folder).")
        if problem == "space":
            return _("Not enough free space for the copies to another disk.")
        if problem.startswith("running:"):
            return _("Quit {0} first: it writes its settings when it closes.").format(problem[8:])
        if problem.startswith("conflict:"):
            return _("Already there, a different file: {0}").format(inventory.tilde(problem[9:]))
        return problem

    def reorg_text(self, plan) -> str:
        root = str(plan.root)
        short = lambda path: path[len(root) + 1:] if path.startswith(root + "/") else inventory.tilde(path)
        names = {r.key: r.name for r in self.inventory.runtimes}
        lines = []
        moves = [st for st in plan.steps if st.kind == "move"]
        if moves:
            copies = [st for st in moves if st.copy]
            lines.append(_("Move ({0})").format(len(moves)).upper() + " " +
                         (_("· {0} to copy to another disk").format(size_text(plan.copy_bytes)) if copies
                          else _("· same disk, no copying")))
            lines += [f"  {short(st.source)}\n    → {short(st.target)}" for st in moves]
        relinks = [st for st in plan.steps if st.kind == "relink"]
        if relinks:
            lines += ["", _("Links follow ({0})").format(len(relinks)).upper()]
            lines += [f"  {short(st.source)} → {short(st.target)}" for st in relinks]
        old = [st for st in plan.steps if st.kind == "link" and not st.source.startswith(root + "/llama.cpp/")]
        if old:
            lines += ["", _("Old paths keep working ({0} links)").format(len(old)).upper()]
            lines += [f"  {short(st.source)}" for st in old]
        flat = [st for st in plan.steps if st.kind == "link" and st.source.startswith(root + "/llama.cpp/")]
        if flat:
            lines += ["", _("llama.cpp/ links ({0})").format(len(flat)).upper()]
            lines += [f"  {Path(st.source).name}" for st in flat]
        folders = [st for st in plan.steps if st.kind == "folder"]
        if folders:
            lines += ["", _("Local LLM settings").upper()]
            for st in folders:
                lines.append("  " + _("{0}: model folder {1} → {2} (restarts it if it runs)").format(
                    names.get(st.provider, st.provider), inventory.tilde(st.old), inventory.tilde(st.target)))
        notes = [st for st in plan.steps if st.kind == "note"]
        if notes:
            lines += ["", _("Not changed").upper()]
            for st in notes:
                lines.append("  " + _("{0} (installed by hand) keeps its folder {1}; its links follow the moved "
                                      "files.").format(names.get(st.provider, st.provider), inventory.tilde(st.source)))
        return "\n".join(lines) if lines else _("Nothing to do.")

    def apply_reorganize(self):
        plan = self.reorg_plan
        if plan is None or plan.problems or plan.empty or self.reorg_busy:
            return
        self.reorg_busy = True
        self.reorg_apply.set_sensitive(False)
        self.reorg_undo.set_visible(False)
        self.reorg_bar.set_visible(True)
        self.reorg_bar.set_fraction(0)
        self.reorg_status.set_label(_("Reorganizing…"))
        holders = self.folder_holders(plan.steps)

        def progress(fraction, text):
            GLib.idle_add(lambda: (self.reorg_bar.set_fraction(max(0.0, min(1.0, fraction))),
                                   self.reorg_status.set_label(text or "")) and False)

        def work():
            stopped = self.stop_holders(holders)
            try:
                done = model_layout.apply(plan, self.set_provider_folder, progress)
                error = ""
            except (OSError, ValueError, RuntimeError, model_layout.Cancelled) as problem:
                done, error = [], str(problem)
            finally:
                for rt in stopped:
                    runtime_ctl.start(rt)
            GLib.idle_add(finished, done, error)

        def finished(done, error):
            self.reorg_busy = False
            self.reorg_bar.set_visible(False)
            self.collect()
            self.refresh_overview()
            self.refresh_map()
            if error:
                self.reorg_status.set_label(_("Stopped: {0}. What was done is in the journal: Undo puts it back.")
                                            .format(error))
                self.reorg_undo.set_visible(True)
                return False
            self.reorg_preview.get_buffer().set_text(self.reorg_text(plan))
            self.reorg_status.set_label(ngettext("Done: 1 step.", "Done: {0} steps.", len(done)).format(len(done)))
            self.reorg_undo.set_visible(True)
            self.show_notice(_("Models reorganized. Undo is on the Reorganize page."))
            return False

        threading.Thread(target=work, daemon=True).start()

    def undo_reorganize(self):
        if self.reorg_busy:
            return
        self.reorg_busy = True
        self.reorg_undo.set_sensitive(False)
        self.reorg_apply.set_sensitive(False)
        self.reorg_status.set_label(_("Putting everything back…"))
        holders = self.folder_holders(model_layout.load_journal())

        def work():
            stopped = self.stop_holders(holders)
            try:
                problems = model_layout.undo(self.set_provider_folder)
            except (OSError, RuntimeError) as error:
                problems = [str(error)]
            for rt in stopped:
                runtime_ctl.start(rt)
            GLib.idle_add(finished, problems)

        def finished(problems):
            self.reorg_busy = False
            self.reorg_undo.set_sensitive(True)
            self.collect()
            self.refresh_overview()
            self.refresh_map()
            self.reorg_status.set_label(_("Everything is back.") if not problems else
                                        _("Back, but not all: {0}").format("; ".join(problems[:3])))
            self.plan_reorganize()
            if not problems:
                self.reorg_status.set_label(_("Everything is back."))
            return False

        threading.Thread(target=work, daemon=True).start()

    # --- help: the manual (for now: setting up by drawing) ---------------------------------------

    def build_help_page(self) -> Gtk.Widget:
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
        back.connect("clicked", lambda *_: self.close_help())
        header.append(back)
        title = Gtk.Label(label=_("Help").upper(), xalign=0, hexpand=True)
        title.add_css_class("am-section-title")
        header.append(title)
        page.append(header)
        rule = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        rule.add_css_class("am-settings-rule")
        page.append(rule)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP)
        body.add_css_class("am-settings")
        manual = self.help_manual_card()
        if manual is not None:
            body.append(manual)
        body.append(self.help_disk_card())
        body.append(self.help_setup_card())
        monitor = self.help_llm_monitor_card()
        if monitor is not None:
            body.append(monitor)
        for addon_id, builder in self.addon_hooks("help"):
            card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP)
            card.add_css_class("am-help-card")
            addon_api.call(addon_id, builder, addon_api.UI(self, addon_id), card)
            if card.get_first_child() is not None:
                body.append(card)
        page.append(self.page_scroller(body))
        return page

    @staticmethod
    def manual_path() -> Path | None:
        """The user manual that comes with this version (assets/ in the plugin, data/ in Asterism), in
        the interface language: user-manual.<language>.pdf, else the English user-manual.pdf."""
        here = Path(__file__).resolve().parent
        lang = i18n.current()
        names = ([f"user-manual.{lang}.pdf"] if lang and lang != "en" else []) + ["user-manual.pdf"]
        return next((folder / name for name in names for folder in (here / "assets", here / "data")
                     if (folder / name).is_file()), None)

    def help_manual_card(self) -> Gtk.Widget | None:
        """The user manual (a PDF of this version), opened in the desktop's PDF viewer."""
        path = self.manual_path()
        if path is None:
            return None
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP)
        card.add_css_class("am-help-card")
        top = Gtk.Box(spacing=10)
        heading = Gtk.Label(label=_("User manual").upper(), xalign=0, hexpand=True)
        heading.add_css_class("am-setting-heading")
        top.append(heading)
        button = Gtk.Button()
        content = Gtk.Box(spacing=6)
        content.append(Gtk.Image.new_from_icon_name("x-office-document-symbolic"))
        content.append(Gtk.Label(label=_("Open user manual")))
        button.set_child(content)
        button.add_css_class("am-small-btn")
        top.append(button)
        card.append(top)
        intro = Gtk.Label(label=_("Everything this app does, step by step: the map, agents, local LLMs and models, "
                                  "connecting by drawing, MCP servers, skills, settings and troubleshooting "
                                  "(a PDF, in English, for this version)."),
                          xalign=0, wrap=True, max_width_chars=1)
        intro.add_css_class("am-agent-detail")
        card.append(intro)
        note = Gtk.Label(xalign=0, wrap=True, max_width_chars=1, visible=False)
        note.add_css_class("am-agent-detail")
        card.append(note)

        def open_manual(*_args):
            # The desktop's default PDF viewer, started from its desktop entry by GIO (no shell, no
            # command line built here). Not Gtk.FileLauncher: it goes through the desktop portal,
            # which on Hyprland cannot attach to the window and silently opens nothing.
            try:
                Gio.AppInfo.launch_default_for_uri(path.as_uri(), None)
                note.set_visible(False)
            except GLib.Error as error:
                note.set_label(_("Could not open the manual: {0}").format(error.message))
                note.set_visible(True)

        button.connect("clicked", open_manual)
        return card

    SPACE_PARTS = (("agents", N_("Agent programs")), ("agent_data", N_("Agent data")),
                   ("runtimes", N_("Local LLMs")), ("models", N_("Models")))
    SPACE_KINDS = {"program": N_("Program"), "data": N_("Data"), "models": N_("Models")}

    def help_disk_card(self) -> Gtk.Widget:
        """Disk space of the agents, local LLMs and models: how much, where, on which disk.
        Measured when Help opens (in the background); nothing is changed or deleted."""
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        card.add_css_class("am-help-card")
        top = Gtk.Box(spacing=10)
        # The heading with the totals in one line; a click shows or hides the details.
        toggle = Gtk.Box(spacing=8, hexpand=True)
        chevron = Gtk.Image.new_from_icon_name("pan-down-symbolic")       # open at first; the sections inside start folded
        chevron.set_valign(Gtk.Align.START)
        toggle.append(chevron)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
        heading = Gtk.Label(label=_("Disk space: agents, local LLMs and models").upper(), xalign=0)
        heading.add_css_class("am-setting-heading")
        texts.append(heading)
        self.disk_summary = Gtk.Label(label="", xalign=0, wrap=True, max_width_chars=1)
        self.disk_summary.add_css_class("am-agent-detail")
        texts.append(self.disk_summary)
        toggle.append(texts)
        toggle.set_cursor_from_name("pointer")
        toggle.set_tooltip_text(_("Hide details"))
        top.append(toggle)
        again = Gtk.Button(valign=Gtk.Align.START)
        content = Gtk.Box(spacing=6)
        content.append(Gtk.Image.new_from_icon_name("view-refresh-symbolic"))
        content.append(Gtk.Label(label=_("Measure again")))
        again.set_child(content)
        again.add_css_class("am-small-btn")
        again.connect("clicked", lambda *_: self.measure_disk(force=True))
        top.append(again)
        card.append(top)
        details = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP)
        self.note(details, _("How much room your AI setup takes and where it is. Programs can be installed again; data "
                             "holds settings, sessions and history; models are the largest files, and several local "
                             "LLMs can share one models folder."))
        self.disk_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP)
        details.append(self.disk_box)
        revealer = Gtk.Revealer(child=details, reveal_child=True,
                                transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN)
        card.append(revealer)

        def flip(*_args):
            shown = not revealer.get_reveal_child()
            revealer.set_reveal_child(shown)
            chevron.set_from_icon_name("pan-down-symbolic" if shown else "pan-end-symbolic")
            toggle.set_tooltip_text(_("Hide details") if shown else _("Show details"))
        click = Gtk.GestureClick()
        click.connect("released", flip)
        toggle.add_controller(click)
        self.disk_result, self.disk_time = None, 0.0
        self.disk_busy = False
        return card

    def measure_disk(self, force: bool = False):
        if self.disk_busy or (self.disk_result is not None and not force
                              and time.monotonic() - self.disk_time < 600):
            return
        self.disk_busy = True
        self.disk_summary.set_label(_("Measuring…"))
        box = self.disk_box
        while (child := box.get_first_child()) is not None:
            box.remove(child)
        self.note(box, _("Measuring…"))
        agent_list, inv = list(self.agent_list), self.inventory

        def work():
            try:
                items = disk_usage.plan(agent_list, inv)
                disks = disk_usage.measure(items)
                result = (items, disks, "")
            except Exception as error:      # shown, never fatal
                result = ([], [], str(error))
            GLib.idle_add(done, result)

        def done(result):
            self.disk_busy = False
            self.disk_result, self.disk_time = result, time.monotonic()
            self.fill_disk(*result)
            return False

        threading.Thread(target=work, daemon=True).start()

    def fill_disk(self, items, disks, error):
        box = self.disk_box
        while (child := box.get_first_child()) is not None:
            box.remove(child)
        if error:
            self.note(box, _("Could not measure: {0}").format(error))
            self.disk_summary.set_label(_("Could not measure: {0}").format(error))
            return
        sums = disk_usage.totals(items)
        total = sum(sums.values())
        partial = any(i.partial() for i in items)
        at_least = (lambda n: _("at least {0}").format(size_text(n))) if partial else size_text
        self.disk_summary.set_label("  ·  ".join(_("{0} {1}").format(label, size) for label, size in (
            (_("Agents"), size_text(sums["agents"] + sums["agent_data"])), (_("Local LLMs"), size_text(sums["runtimes"])),
            (_("Models"), size_text(sums["models"])), (_("In all"), at_least(total)))))
        box.append(self.stat_tiles([
            (_("In all"), at_least(total), ngettext("on 1 disk", "on {0} disks", len(disks)).format(len(disks))),
            (_("Agents"), size_text(sums["agents"] + sums["agent_data"]),
             _("{0} programs · {1} data").format(size_text(sums["agents"]), size_text(sums["agent_data"]))),
            (_("Local LLMs"), size_text(sums["runtimes"]), _("programs and their data")),
            (_("Models"), size_text(sums["models"]), ngettext("1 folder", "{0} folders", sum(
                1 for i in items if i.group == "models")).format(sum(1 for i in items if i.group == "models"))),
        ]))
        # The bar is the whole space of the disks the AI setup is on: its parts, then what other
        # files take, then what is free (the empty track).
        size = sum(d.size for d in disks)
        free = sum(d.free for d in disks)
        other = max(0, size - free - total)
        if size:
            line = Gtk.Label(xalign=0, label=_("{0} of {1} on {2} for AI ({3}) · {4} free").format(
                size_text(total), size_text(size), ngettext("this disk", "these {0} disks", len(disks)).format(len(disks)),
                f"{100 * total / size:.1f} %", size_text(free)))
            line.add_css_class("am-agent-detail")
            box.append(line)
        colors = {"agents": "accent", "agent_data": "green", "runtimes": "yellow", "models": "outside", "other": "muted"}
        parts = [(_(label), sums[key], colors[key]) for key, label in self.SPACE_PARTS]
        if size:
            parts.append((_("Other files"), other, "muted"))
        box.append(SpaceBar(self.side_map.color, parts, whole=size, rest=_("Free")))
        legend = Gtk.Box(spacing=14)
        entries = [(key, _(label), sums[key]) for key, label in self.SPACE_PARTS]
        if size:
            entries += [("other", _("Other files"), other), ("free", _("Free"), free)]
        for key, label, amount in entries:
            if not amount:
                continue
            part = Gtk.Box(spacing=4)
            dot = Gtk.Label(label="○" if key == "free" else "●")
            dot.add_css_class(f"am-space-{key}")
            part.append(dot)
            text = Gtk.Label(label=f"{label} {size_text(amount)}")
            text.add_css_class("am-agent-detail")
            part.append(text)
            legend.append(part)
        box.append(legend)

        if disks:
            section = self.fold_section(box, _("Disks"), "  ·  ".join(
                f"{d.mount} " + _("{0} free").format(size_text(d.free)) for d in disks))
            listbox = self.boxed_list()
            for disk in disks:
                row = self.plain_row(disk.mount, _("{0} for AI · {1} free of {2}").format(
                    size_text(disk.ai), size_text(disk.free), size_text(disk.size)))
                row.add_prefix(Gtk.Image.new_from_icon_name("drive-harddisk-symbolic"))
                level = Gtk.LevelBar(min_value=0, max_value=1, valign=Gtk.Align.CENTER)
                level.set_value(1 - disk.free / disk.size if disk.size else 0)
                level.set_size_request(120, -1)
                level.set_tooltip_text(_("{0} free").format(size_text(disk.free)))
                row.add_suffix(level)
                listbox.append(row)
            section.append(listbox)

        for group, title in (("agent", _("Agents")), ("runtime", _("Local LLMs")), ("models", _("Models folders"))):
            rows = sorted((i for i in items if i.group == group), key=lambda i: -i.total())
            if not rows:
                continue
            # Folded: the sum and the three largest in the heading's line.
            largest = [f"{i.name} {size_text(i.total())}" for i in rows[:3]] + (["…"] if len(rows) > 3 else [])
            section = self.fold_section(box, title, "  ·  ".join(
                [size_text(sum(i.total() for i in rows))] + largest))
            listbox = self.boxed_list()
            for item in rows:
                listbox.append(self.disk_item_row(item))
            section.append(listbox)
            if group == "agent":
                for item in [i for i in rows if i.versions > 3][:4]:
                    self.note(section, "→ " + _("{0} keeps {1} versions; “mise prune” removes the ones no longer "
                                                "used.").format(item.name, item.versions))
        self.note(box, _("Space on disk; a file kept in two places counts once, links are not followed. Nothing is "
                         "deleted from here."))

    def fold_section(self, box, title: str, summary: str, shown: bool = False) -> Gtk.Box:
        """A heading that folds what is under it: chevron, title and (folded) a one-line summary.
        Returns the box to fill."""
        head = Gtk.Box(spacing=8)
        chevron = Gtk.Image.new_from_icon_name("pan-down-symbolic" if shown else "pan-end-symbolic")
        head.append(chevron)
        label = Gtk.Label(label=title.upper(), xalign=0)      # the group cards' heading style
        label.add_css_class("am-setting-heading")
        head.append(label)
        line = Gtk.Label(label=summary, xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END,
                         max_width_chars=1, visible=not shown)
        line.add_css_class("am-agent-detail")
        line.set_tooltip_text(summary)
        head.append(line)
        head.set_cursor_from_name("pointer")
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)       # folded: no gap for the hidden part
        outer.append(head)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP)
        revealer = Gtk.Revealer(child=content, reveal_child=shown,
                                transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN)
        outer.append(revealer)
        box.append(outer)

        def flip(*_args):
            now = not revealer.get_reveal_child()
            revealer.set_reveal_child(now)
            chevron.set_from_icon_name("pan-down-symbolic" if now else "pan-end-symbolic")
            line.set_visible(not now)
        click = Gtk.GestureClick()
        click.connect("released", flip)
        head.add_controller(click)
        return content

    def disk_item_row(self, item) -> Gtk.Widget:
        kinds = {}
        for place in item.places:
            kinds[place.kind] = kinds.get(place.kind, 0) + place.size
        parts = [_("{0} {1}").format(_(self.SPACE_KINDS[kind]), size_text(size)) for kind, size in kinds.items()]
        if item.group == "models":
            place = item.places[0]
            parts = [ngettext("1 file", "{0} files", place.files).format(place.files)]
            if place.links and place.size < 1_000_000:
                parts = [ngettext("only 1 link to a model in another folder",
                                  "only links to models in other folders ({0})", place.links).format(place.links)]
            if item.users:
                parts.append(_("used by {0}").format(", ".join(item.users)))
        row = Adw.ExpanderRow(title=item.name, subtitle=" · ".join(parts), use_markup=False)
        row.set_subtitle_lines(0)
        size = Gtk.Label(label=(_("at least {0}") if item.partial() else "{0}").format(size_text(item.total())),
                         valign=Gtk.Align.CENTER)
        size.add_css_class("heading")
        row.add_suffix(size)
        for place in item.places:
            details = [_(self.SPACE_KINDS[place.kind]), size_text(place.size),
                       ngettext("1 file", "{0} files", place.files).format(place.files)]
            if place.kind == "program" and item.versions > 1:
                details.append(ngettext("1 version kept", "{0} versions kept", item.versions).format(item.versions))
            line = self.plain_row(inventory.tilde(place.path), " · ".join(details))
            line.set_title_selectable(True)
            line.add_prefix(Gtk.Image.new_from_icon_name(
                "folder-symbolic" if place.path.is_dir() else "text-x-generic-symbolic"))
            show = Gtk.Button(icon_name="folder-open-symbolic", valign=Gtk.Align.CENTER)
            show.add_css_class("flat")
            show.set_tooltip_text(_("Open the folder"))
            folder = place.path if place.path.is_dir() else place.path.parent
            show.connect("clicked", lambda _b, f=folder: self.open_folder(f))
            line.add_suffix(show)
            row.add_row(line)
        return row

    def open_folder(self, folder: Path):
        """The desktop's file manager on a folder (GIO, no command line built here)."""
        try:
            Gio.AppInfo.launch_default_for_uri(folder.as_uri(), None)
        except GLib.Error as error:
            self.show_note(_("Could not open the folder: {0}").format(error.message))

    LLM_MONITOR_ID = "ubruckhaus.llm-usage"
    LLM_MONITOR_LISTING = "https://omarchyplugins.com/plugin.html?id=ubruckhaus.llm-usage"
    LLM_MONITOR_ADD = "omarchy plugin add https://github.com/uBruckhaus/omarchy-llm-usage.git --enable"

    def help_llm_monitor_card(self) -> Gtk.Widget | None:
        """On Omarchy: LLM Monitor, the bar plugin that shows what a local LLM is doing (the same
        author's): its picture, the marketplace page and the command that installs it."""
        if desktop.NAME != "omarchy":
            return None
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP)
        card.add_css_class("am-help-card")
        top = Gtk.Box(spacing=10)
        heading = Gtk.Label(label=_("Also for your bar: LLM Monitor").upper(), xalign=0, hexpand=True)
        heading.add_css_class("am-setting-heading")
        top.append(heading)
        installed = (Path.home() / ".config/omarchy/plugins" / self.LLM_MONITOR_ID).is_dir()
        if installed:
            mark = Gtk.Label(label=_("Installed"))
            mark.add_css_class("am-agent-badge")
            top.append(mark)
        card.append(top)
        intro = Gtk.Label(label=_("A plugin of its own for Omarchy's bar: it shows when llama.cpp, Ollama, LM Studio or "
                                  "vLLM holds a model on the graphics card, how full its context is, GPU memory, load "
                                  "and speed, and frees GPU memory with one click."),
                          xalign=0, wrap=True, max_width_chars=1)
        intro.add_css_class("am-agent-detail")
        card.append(intro)
        labels = {"gpu": _("GPU memory"), "context": _("Context"), "activity": _("GPU activity"),
                  "generating": _("generating"), "running": _("running"), "stopped": _("stopped"),
                  "llms": _("Local LLMs"), "desktop": _("Desktop & apps"), "free": _("Free"),
                  "free_gpu": _("Free GPU memory")}
        sketch = previews.LlmMonitorSketch(lambda: self.theme.colors, labels)
        sketch.update_property([Gtk.AccessibleProperty.DESCRIPTION], [_("LLM Monitor in the bar and its panel")])
        card.append(sketch)
        listbox = self.boxed_list()
        listbox.append(self.command_row(self.LLM_MONITOR_ADD))
        card.append(listbox)
        hint = Gtk.Label(label=_("Run it in a terminal, or install it from Omarchy's plugin marketplace."), xalign=0,
                         wrap=True, max_width_chars=1)
        hint.add_css_class("am-agent-detail")
        card.append(hint)
        link = Gtk.LinkButton(uri=self.LLM_MONITOR_LISTING, label=_("Open LLM Monitor in the marketplace"),
                              halign=Gtk.Align.START)
        card.append(link)
        return card

    def help_setup_card(self) -> Gtk.Widget:
        """Setting up by drawing: an animation with dummy boxes, played on Start."""
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP)
        card.add_css_class("am-help-card")
        top = Gtk.Box(spacing=10)
        heading = Gtk.Label(label=_("Set up by drawing").upper(), xalign=0, hexpand=True)
        heading.add_css_class("am-setting-heading")
        top.append(heading)
        play = Gtk.Button()
        content = Gtk.Box(spacing=6)
        content.append(Gtk.Image.new_from_icon_name("media-playback-start-symbolic"))
        play_label = Gtk.Label(label=_("Start"))
        content.append(play_label)
        play.set_child(content)
        play.add_css_class("am-small-btn")
        top.append(play)
        card.append(top)
        intro = Gtk.Label(label=_("Agents get their LLM, their model, MCP servers and skills by lines you draw on the "
                                  "map. The animation shows it with made-up boxes; nothing on your computer changes."),
                          xalign=0, wrap=True, max_width_chars=1)
        intro.add_css_class("am-agent-detail")
        card.append(intro)
        sketch = self.drawing_sketch(on_end=lambda: play_label.set_label(_("Replay")))
        play.connect("clicked", lambda *_: sketch.start())
        self.setup_sketch = sketch
        card.append(sketch)
        return card

    def drawing_sketch(self, on_end=None):
        """The animation of setting an agent up by drawing (made-up boxes), as Help and the map's
        first-connection card show it; start() plays it."""
        labels = {"h_mcp": _("MCP servers · skills"), "h_agents": _("Agents"), "h_providers": _("LLM providers"),
                  "h_models": _("Models"), "mcp": _("an MCP server"), "skills": _("a skill folder"),
                  "agent": _("your agent"), "online": _("an online provider"), "local": _("a local LLM"),
                  "model_a": "model-a", "model_b": "model-b", "m_start": _("Start here"),
                  "m_terminal": _("Start in terminal"), "m_details": _("Details"), "m_hide": _("Hide")}
        captions = {0: _("Press Start to see how an agent is set up by drawing."),
                    1: _("Drag from the agent to a provider: a line connects them."),
                    2: _("Click a model of that provider: the agent uses it now."),
                    3: _("Drag from the agent to an MCP server: it can use its tools."),
                    4: _("Drag from the agent to a skill folder: it can use those skills."),
                    5: _("Click the pin at a box's corner: its lines stay lit while you move over other boxes."),
                    6: _("Right-click a box for what its card offers: start an agent here, in a terminal, or open its details."),
                    7: _("Every change shows a preview first and keeps a backup; Undo takes it back.")}
        return previews.SetupSketch(lambda: self.theme.colors, labels, captions, on_end=on_end)

    def open_help(self):
        if self.page_name() not in ("settings", "help", "info"):
            self.remember_page()
        self.show_page("help")
        self.measure_disk()

    def close_help(self):
        self.back_to_last_page()

    def close_settings(self):
        self.back_to_last_page()
