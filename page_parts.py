"""AI Agents Map: building blocks several pages share (group cards, tiles, headings, intro cards,
scrolled lists) and the notices at the bottom of the window (a mixin of ai_popup.AgentsMap)."""

from gi.repository import Adw, GLib, Gdk, Gtk, Pango

import ai_core
import connect
import datetime as dt
from i18n import _, ngettext
from ui_helpers import when


class PageParts:

    # --- page layout -------------------------------------------------------------------------------------

    @staticmethod
    def scrolled(child, hexpand: bool = False) -> Gtk.ScrolledWindow:
        scroller = Gtk.ScrolledWindow(hexpand=hexpand, vexpand=True)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_child(child)
        return scroller

    @staticmethod
    def clear_box(box):
        while (child := box.get_first_child()) is not None:
            box.remove(child)

    def intro_card(self, box, title: str, lines):
        """A short explanation at the top of a page: what this is and how it is used."""
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP_TIGHT)
        card.add_css_class("am-model-card")
        head = Gtk.Label(label=title, xalign=0, wrap=True, max_width_chars=1)
        head.add_css_class("am-intro-title")
        card.append(head)
        for line in lines:
            text = Gtk.Label(label=line, xalign=0, wrap=True, max_width_chars=1)
            text.add_css_class("am-agent-detail")
            card.append(text)
        box.append(card)
        return card

    @staticmethod
    def section_heading(box, text: str):
        """A section heading of the Add pages and the MCP / skills pages: one style for all."""
        label = Gtk.Label(label=text.upper(), xalign=0)
        label.add_css_class("am-section-heading")
        box.append(label)
        return label

    @staticmethod
    def view_heading(text: str, end: Gtk.Widget | None = None) -> Gtk.Widget:
        """A view's title over a rule; end goes at the right of the title (the map's Refresh)."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP_TIGHT)
        row = Gtk.Box(spacing=8)
        label = Gtk.Label(label=text.upper(), xalign=0, hexpand=True)
        label.add_css_class("am-section-title")
        row.append(label)
        if end is not None:
            row.append(end)
        box.append(row)
        rule = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        rule.add_css_class("am-settings-rule")
        box.append(rule)
        return box

    # --- the layout kit: one way to build a page ---------------------------------------------------------
    #
    # A page that reviews or explains something is built from the same parts, top to bottom
    # (docs/layout.md): page_header (Back + title over a rule), then cards in a scroller: what
    # happens (info_card), notes (note_card: a warning or a hint, one card each), a form (setting
    # rows in an info_card), checks (hint rows in an info_card whose level is the worst hint's),
    # the action_row right under them, and long details (a preview, a list) last.

    LEVEL_MARKS = {"error": ("✕", "am-hint-error"), "warning": ("!", "am-hint-warning"),
                   "ok": ("✓", "am-hint-ok"), "info": ("i", "am-hint-info")}

    @staticmethod
    def page_header(title: Gtk.Label, on_back, tip: str) -> Gtk.Box:
        """Back · ⌫ and the page title (a label the page sets), over a rule."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP)
        header = Gtk.Box(spacing=8)
        back = Gtk.Button()
        for css in ("flat", "am-header-btn", "am-back-bordered"):
            back.add_css_class(css)
        back_box = Gtk.Box(spacing=6)
        back_box.append(Gtk.Image.new_from_icon_name("go-previous-symbolic"))
        back_box.append(Gtk.Label(label=_("Back")))
        back_box.append(Gtk.Label(label="· ⌫"))
        back.set_child(back_box)
        back.set_tooltip_text(tip)
        back.connect("clicked", lambda *_: on_back())
        header.append(back)
        title.set_xalign(0)
        title.set_hexpand(True)
        title.add_css_class("am-section-title")
        header.append(title)
        box.append(header)
        rule = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        rule.add_css_class("am-settings-rule")
        box.append(rule)
        return box

    @classmethod
    def info_card(cls, title: str, level: str = "") -> Gtk.Box:
        """A card with a heading (a mark before it for a level: error, warning, ok, info); fill
        card.body. card.set_level() changes the level later (a check that has finished)."""
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP_TIGHT)
        card.add_css_class("am-help-card")
        card.add_css_class("am-info-card")
        head = Gtk.Box(spacing=8)
        mark = Gtk.Label(valign=Gtk.Align.CENTER)
        head.append(mark)
        heading = Gtk.Label(label=title.upper(), xalign=0, hexpand=True)
        heading.add_css_class("am-setting-heading")
        head.append(heading)
        card.append(head)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP_TIGHT)
        card.append(body)

        def set_level(new: str, new_title: str | None = None):
            for name in cls.LEVEL_MARKS:
                card.remove_css_class(f"am-level-{name}")
            for _text, css in cls.LEVEL_MARKS.values():
                mark.remove_css_class(css)
            text, css = cls.LEVEL_MARKS.get(new, ("", ""))
            mark.set_label(text)
            mark.set_visible(bool(text))
            if css:
                mark.add_css_class(css)
                card.add_css_class(f"am-level-{new}")
            if new_title is not None:
                heading.set_label(new_title.upper())
        set_level(level)
        card.body, card.heading, card.set_level = body, heading, set_level
        return card

    @staticmethod
    def card_text(box, text: str, css: str = "am-card-text") -> Gtk.Label:
        label = Gtk.Label(label=text, xalign=0, wrap=True, max_width_chars=1, selectable=True)
        label.add_css_class(css)
        box.append(label)
        return label

    def note_card(self, box, level: str, text: str, title: str = "") -> Gtk.Box:
        """One warning or hint as a card of its own."""
        card = self.info_card(title or {"error": _("Problem"), "warning": _("Note"), "ok": _("Ready")}.get(
            level, _("Good to know")), level)
        self.card_text(card.body, text)
        box.append(card)
        return card

    @classmethod
    def hint_rows(cls, box, hints, width: int = 0):
        """link_check hints ({level, text}) as rows: the level's mark, then the text."""
        for item in hints:
            row = Gtk.Box(spacing=8)
            mark_text, css = cls.LEVEL_MARKS.get(item.get("level"), ("·", ""))
            mark = Gtk.Label(label=mark_text, valign=Gtk.Align.START)
            if css:
                mark.add_css_class(css)
            row.append(mark)
            text = Gtk.Label(label=item.get("text", ""), xalign=0, wrap=True, hexpand=True, selectable=True,
                             max_width_chars=width or 1)
            text.add_css_class("am-card-text")
            row.append(text)
            box.append(row)

    @staticmethod
    def worst(hints) -> str:
        levels = [h.get("level") for h in hints]
        return next((lv for lv in ("error", "warning", "ok") if lv in levels), "info")

    @staticmethod
    def action_row(*buttons, start=()) -> Gtk.Box:
        """The page's buttons, right under its cards: `start` at the left (a status), the buttons at
        the right, the main one last."""
        row = Gtk.Box(spacing=8)
        row.add_css_class("am-button-row")
        for widget in start:
            row.append(widget)
        row.append(Gtk.Box(hexpand=True))
        for button in buttons:
            # A page's own actions: one size on every page (am-page-btn), whatever they were made with.
            button.remove_css_class("am-small-btn")
            button.add_css_class("am-page-btn")
            row.append(button)
        return row

    # --- cards and tiles ---------------------------------------------------------------------------------

    def group_card(self, box, key: str, title: str, icon: str = "", summary: str = "", badges=(),
                   named_by_addon: bool = False, default_open: bool = False) -> Gtk.Box:
        """A category of a details page as a card that folds: its heading (icon, title, the summary
        while folded, badges) opens and closes it. Folded by default; a category once opened stays
        open on the next details pages (setting details_open). Returns the box to fill.
        named_by_addon: an add-on's first heading in it becomes the card's title (Topics).
        default_open: open until folded (the Add-ons page's sections); remembered as "<key>:closed"."""
        saved = ai_core.setting("details_open")
        saved = saved if isinstance(saved, list) else []
        opened = f"{key}:closed" not in saved if default_open else key in saved
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        card.add_css_class("am-help-card")
        card.add_css_class("am-group-card")
        head = Gtk.Box(spacing=8)
        chevron = Gtk.Image.new_from_icon_name("pan-down-symbolic" if opened else "pan-end-symbolic")
        head.append(chevron)
        if icon:
            head.append(Gtk.Image.new_from_icon_name(icon))
        heading = Gtk.Label(label=title.upper(), xalign=0)
        heading.add_css_class("am-setting-heading")
        head.append(heading)
        line = Gtk.Label(label=summary, xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END,
                         max_width_chars=1, visible=not opened)
        line.add_css_class("am-agent-detail")
        line.set_tooltip_text(summary or None)
        head.append(line)
        filler = Gtk.Box(hexpand=True, visible=opened)
        head.append(filler)
        badge_box = Gtk.Box(spacing=6)
        for text, level in badges:
            badge_box.append(self.risk_badge(text, level))
        head.append(badge_box)
        head.set_cursor_from_name("pointer")
        card.append(head)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP, margin_top=ai_core.GAP,
                          margin_bottom=6)       # the low header's padding is small: some room under the content
        revealer = Gtk.Revealer(child=content, reveal_child=opened,
                                transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN)
        card.append(revealer)
        box.append(card)

        def flip(*_args):
            now = not revealer.get_reveal_child()
            revealer.set_reveal_child(now)
            chevron.set_from_icon_name("pan-down-symbolic" if now else "pan-end-symbolic")
            line.set_visible(not now)
            filler.set_visible(now)
            keys = ai_core.setting("details_open")
            mark = f"{key}:closed" if default_open else key
            keys = [k for k in keys if isinstance(k, str) and k != mark] if isinstance(keys, list) else []
            remember = (not now) if default_open else now
            self.save_setting(details_open=keys + [mark] if remember else keys)
        click = Gtk.GestureClick()
        click.connect("released", flip)
        head.add_controller(click)
        content.group_badges = badge_box
        content.group_summary = line      # set_label() keeps a live summary (the map's GPU card)
        content.group_card = card         # the whole card (hidden when there is nothing to show)
        if named_by_addon:
            content.set_name(f"am-group:{key}")
            self.group_namers = getattr(self, "group_namers", {})
            self.group_namers[key] = lambda text: heading.set_label(text.upper())
        return content

    def name_group(self, widget, text: str) -> bool:
        """An add-on's heading inside a card that it names (Topics): the card's title becomes it,
        once. False: not in such a card, or named already."""
        while widget is not None:
            name = widget.get_name() or ""
            if name.startswith("am-group:"):
                namer = getattr(self, "group_namers", {}).pop(name[9:], None)
                if namer is None:
                    return False
                namer(text)
                return True
            widget = widget.get_parent()
        return False

    def status_hero(self, state: str, sentence: str, now: str) -> Gtk.Widget:
        box = Gtk.Box(spacing=12)
        box.add_css_class("card")
        box.add_css_class("am-status-hero")
        mark = Gtk.Label(label={"ok": "✓", "warn": "!", "missing": "✕"}.get(state, "·"))
        mark.add_css_class("am-status-mark")
        mark.add_css_class(f"am-state-{state}")
        mark.set_valign(Gtk.Align.CENTER)
        box.append(mark)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
        title = Gtk.Label(label=sentence, xalign=0, wrap=True, max_width_chars=1)
        title.add_css_class("am-status-title")
        text.append(title)
        line = Gtk.Label(label=now, xalign=0, wrap=True, max_width_chars=1)
        line.add_css_class("am-agent-detail")
        text.append(line)
        box.append(text)
        return box

    def stat_tiles(self, tiles) -> Gtk.Widget:
        """(heading, big value, small line) side by side."""
        row = Gtk.Box(spacing=10, homogeneous=True)
        for heading, value, small in tiles:
            tile = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            tile.add_css_class("card")
            tile.add_css_class("am-stat-tile")
            top = Gtk.Label(label=heading.upper(), xalign=0)
            top.add_css_class("am-group-heading")
            tile.append(top)
            big = Gtk.Label(label=value, xalign=0, ellipsize=Pango.EllipsizeMode.END, max_width_chars=1)
            big.add_css_class("am-stat-value")
            tile.append(big)
            if small:
                line = Gtk.Label(label=small, xalign=0, ellipsize=Pango.EllipsizeMode.END, max_width_chars=1)
                line.add_css_class("am-agent-detail")
                line.set_tooltip_text(small)
                tile.append(line)
            row.append(tile)
        return row

    def command_row(self, command: str) -> Adw.ActionRow:
        """A command to run, in monospace and selectable, with a button that copies it."""
        row = self.plain_row(command)
        row.add_css_class("monospace")
        row.set_title_selectable(True)
        row.add_prefix(Gtk.Image.new_from_icon_name("utilities-terminal-symbolic"))
        copy = Gtk.Button(icon_name="edit-copy-symbolic", valign=Gtk.Align.CENTER)
        copy.add_css_class("flat")
        copy.set_tooltip_text(_("Copy"))

        def copied(button):
            Gdk.Display.get_default().get_clipboard().set(command)
            button.set_icon_name("object-select-symbolic")
            GLib.timeout_add(1200, lambda: button.set_icon_name("edit-copy-symbolic") or False)
        copy.connect("clicked", copied)
        row.add_suffix(copy)
        return row

    def limit_row(self, limit) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        top = Gtk.Box(spacing=8)
        label = Gtk.Label(label=limit.label, xalign=0, hexpand=True)
        label.set_ellipsize(Pango.EllipsizeMode.END)
        label.set_max_width_chars(1)
        label.add_css_class("am-detail-value")
        top.append(label)
        used = round(limit.percent * 100)
        value = _("{0} % used · {1} % left").format(used, max(0, 100 - used))
        now = dt.datetime.now(dt.timezone.utc)
        if limit.resets_at is not None and limit.resets_at > now:
            minutes = int((limit.resets_at - now).total_seconds() // 60)
            left = _("{0} min").format(minutes) if minutes < 60 else \
                _("{0} h {1} min").format(minutes // 60, minutes % 60) if minutes < 24 * 60 else \
                ngettext("1 day", "{0} days", minutes // 1440).format(minutes // 1440)
            value += " · " + _("resets in {0} ({1})").format(left, when(limit.resets_at, future=True))
        elif limit.resets_at is not None:
            value += " · " + _("reset passed: numbers are older")
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

    # --- notices -----------------------------------------------------------------------------------------

    def toast(self, message: str, seconds: int, button: str = "", on_button=None, on_dismissed=None) -> Adw.Toast:
        """One toast at a time (a new one replaces the last). Plain text: messages carry file names,
        program output and web answers, which must never be read as markup."""
        if self.current_toast is not None:
            self.current_toast.dismiss()
        toast = Adw.Toast(title=message, timeout=seconds)
        toast.set_use_markup(False)
        if button:
            toast.set_button_label(button)
            toast.connect("button-clicked", lambda *_: on_button and on_button())
        clicked = {"done": False}
        toast.connect("button-clicked", lambda *_: clicked.update(done=True))

        def dismissed(t):
            if self.current_toast is t:
                self.current_toast = None
            if on_dismissed is not None and not clicked["done"]:
                on_dismissed()
        toast.connect("dismissed", dismissed)
        self.current_toast = toast
        self.toasts.add_toast(toast)
        return toast

    def show_error(self, message: str, seconds: int = 12):
        """An error as a toast with a copy button (the copy symbol) that puts the whole text on the
        clipboard: errors from programs and web services are long and are pasted into a report."""
        toast = self.toast(message, seconds)
        title = Gtk.Box(spacing=8)
        label = Gtk.Label(label=message, wrap=True, max_width_chars=70, xalign=0, lines=3)
        label.set_ellipsize(Pango.EllipsizeMode.END)
        label.set_use_markup(False)
        title.append(label)
        copy = Gtk.Button(icon_name="edit-copy-symbolic", valign=Gtk.Align.CENTER)
        copy.add_css_class("flat")
        copy.set_tooltip_text(_("Copy the message"))

        def copied(button):
            Gdk.Display.get_default().get_clipboard().set(message)
            button.set_icon_name("object-select-symbolic")
            GLib.timeout_add(1200, lambda: button.set_icon_name("edit-copy-symbolic") or False)
        copy.connect("clicked", copied)
        title.append(copy)
        toast.set_custom_title(title)
        return toast

    def show_note(self, message: str):
        """A short message."""
        self.toast(message, 6)

    def show_notice(self, message: str, seconds: int = 8):
        """A message that stays a little longer (nothing to undo)."""
        self.toast(message, seconds)

    def show_undo(self, message: str, backup):
        """What a change did, with Undo for 30 s."""
        self.toast(message, 30, _("Undo"), lambda: self.undo_last(backup))

    def show_offer(self, message: str, yes: str, no: str, on_yes, on_no):
        """A question that stays until it is answered: the button says yes (`yes`), closing the
        toast says no (`no` is that answer's name, kept for callers; the close button says it)."""
        self.toast(message, 0, yes, on_yes, on_no)

    def undo_last(self, backup):
        try:
            connect.undo(backup)
        except (OSError, ValueError) as error:
            self.show_notice(_("Undo failed: {0}").format(error))
            return
        self.collect()
        self.refresh_overview()
        self.refresh_map()
        showing = getattr(self, "status_showing", None)
        if showing and showing[0] == "agent" and self.page_name() == "status":
            agent = next((a for a in self.agent_list if a.command == showing[1]), None)
            if agent is not None:
                self.open_agent_status(agent)          # its security settings as they are again
