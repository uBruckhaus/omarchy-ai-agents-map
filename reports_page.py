"""AI Agents Map: the Reports page (Maprisma tasks 075/076: reports_page.dart, activity_log_pane.dart,
reports_output.dart, reports_help.dart, history_import_controller.dart; a mixin of ai_popup.AgentsMap).

A page of its own with three tabs:
- Log: the log's settings (the same keys as everywhere else), Collect history, and the retained
  entries with their totals, a date filter, Copy and TXT/JSON export; refreshed on open, after a
  settings change, with Refresh and every 30 s while the tab is shown.
- Topic reports: date range, project and agent filters, a focus line and the helper; Generate asks
  the helper (reports_ctl.generate_topic_report) in the background, Cancel report stops it; the draft
  is shown as cards and can be copied, exported (TXT, Markdown, JSON) or saved to the library.
- Library: saved reports (reports_ctl.ReportsLibrary), search, open, rename and note, export, delete.
The ? button (and F1 on this page) opens "How reporting works".

The poll (ai_popup.poll_gpu) hands the working agents to observe_activity(), which feeds the log and,
once after the window opened, starts an automatic history import (throttled to once an hour).
"""

from __future__ import annotations

import datetime as dt
import json
import threading
import time
from pathlib import Path

from gi.repository import Adw, Gdk, GLib, Gtk

import activity_log
import agent_activity_report
import ai_core
import history_import
import reports_ctl
from i18n import _

TABS = (("log", "Log"), ("topics", "Topic reports"), ("library", "Library"))
KEEP_CHOICES = (1, 2, 7, 30, 90, 365)
REFRESH_SECONDS = 30
LOCAL_TOPICS_KEPT = 500        # entries whose local-LLM topic is remembered
CLASSIFY_BACKOFF = 300         # seconds before the local LLM is asked again after it did not answer


def keep_choices(current: int) -> list[int]:
    return sorted(set(KEEP_CHOICES) | {current})


def keep_label(days: int) -> str:
    return f"{days} · {_('Default')}" if days == 7 else str(days)


class HistoryJob:
    """One Collect history run shared by the start-up run and the button (history_import_controller)."""

    def __init__(self):
        self.running = False
        self.auto_tried = False
        self.progress = ""
        self.error: str | None = None
        self.result: dict | None = None
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None

    def start(self, log, sources, auto: bool, on_change) -> bool:
        with self.lock:
            if self.running or (auto and self.auto_tried):
                return False
            if auto:
                self.auto_tried = True
            self.running, self.error, self.progress = True, None, ""

        def progress(name):
            self.progress = name
            GLib.idle_add(on_change)

        def work():
            try:
                result = history_import.HistoryImporter(log, sources).run(auto=auto, progress=progress)
                if "code" in result:
                    self.error = None if auto else result["code"]
                elif "sources" in result:
                    self.result = result
            except activity_log.ActivityError as error:
                self.error = None if auto else error.code
            except Exception:
                self.error = None if auto else "operation_failed"
            finally:
                self.running = False
                GLib.idle_add(on_change)
        self.thread = threading.Thread(target=work, daemon=True)
        self.thread.start()
        return True


class ReportsPage:
    # --- the shared log ----------------------------------------------------------------------------

    def activity_store(self) -> activity_log.ActivityLog:
        store = getattr(self, "_activity_store", None)
        if store is None:
            store = self._activity_store = activity_log.ActivityLog(timeline=agent_activity_report.activity_timeline)
        return store

    def history_job(self) -> HistoryJob:
        job = getattr(self, "_history_job", None)
        if job is None:
            job = self._history_job = HistoryJob()
        return job

    def observe_activity(self, working, pids: dict, usage=None, runtimes=()):
        """From the poll's worker: the agents (and the one running local LLM) working now."""
        store = self.activity_store()
        if not store.enabled:
            store.observe(set())
            return
        active = {f"agent:{c}" for c in working}
        dirs = {}
        for command in working:
            found = pids.get(command) or []
            if len(found) == 1:
                cwd = agent_activity_report._cwd(found[0])
                if cwd:
                    dirs[f"agent:{command}"] = cwd
        running = [rt for rt in runtimes if usage is not None and usage.runtimes.get(rt.key) is not None
                   and usage.runtimes[rt.key].running]
        if usage is not None and getattr(usage, "generating", False) and len(running) == 1:
            active.add(f"runtime:{running[0].key}")
        store.observe(active, dirs, self.activity_usage_reader(pids))
        job = self.history_job()
        if not job.auto_tried and ai_core.setting("ai_activity_import") is True:
            GLib.idle_add(self.start_history, True)

    def activity_usage_reader(self, pids: dict):
        """usage_for(source) for the log: the token totals of an agent's one running instance, read from
        that instance's own session file (agent_activity_report.session_usage); asked only when a span
        starts or ends, and a file unchanged since is not read again."""
        caches = self.__dict__.setdefault("_activity_usage_caches", {})

        def usage_for(source: str):
            kind, _, command = source.partition(":")
            if kind != "agent":
                return None
            try:
                return agent_activity_report.session_usage(command, pids.get(command) or [],
                                                           cache=caches.setdefault(command, {}))
            except Exception:
                return None
        return usage_for

    # --- the page --------------------------------------------------------------------------------

    def build_reports_page(self) -> Gtk.Widget:
        self.reports_tab = "log"
        self.reports_range = {"log": None, "topics": None}
        self.reports_cursor = None
        self.reports_log_report = None
        self.reports_draft = self.reports_draft_id = None
        self.reports_drafts = reports_ctl.Drafts()
        self.reports_running = None           # the Cancel event of a report being generated
        self.reports_topic_error = None
        self.reports_helpers = None
        self.reports_library_page = None
        self.reports_library_error = None
        self.reports_opened = None
        self.reports_timer = None
        self.reports_help_open = False
        self.reports_fields = {}
        self.reports_title = ""
        self.reports_helper = None
        self.reports_query = ""
        self.reports_pulse = None
        self.reports_local_topics = {}        # entry id -> topic the local LLM gave (Topic summaries by local LLM)
        self.reports_classifying = False
        self.reports_classify_after = 0.0
        self.reports_log_deferred = None
        self.reports_confirming = False
        self.reports_saving = False
        self.reports_library_loading = False
        self.reports_library_results = None
        self.reports_library_errors = None
        self.reports_requests = {"log": 0, "library": 0}     # the newest read per tab: older answers are dropped
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP)
        title = Gtk.Label(label=_("Reports").upper())
        help_button = Gtk.Button(icon_name="help-browser-symbolic", valign=Gtk.Align.CENTER)
        help_button.add_css_class("flat")
        help_button.set_tooltip_text(_("How reporting works (F1)"))
        help_button.connect("clicked", lambda _b: self.show_reporting_help())
        page.append(self.page_header(title, self.close_reports, _("Back (Backspace, Esc)"), end=[help_button]))
        tabs = Gtk.Box(spacing=ai_core.GAP_INLINE)
        self.reports_tab_buttons = {}
        group = None
        for key, label in TABS:
            button = Gtk.ToggleButton(label=_(label))
            button.add_css_class("am-small-btn")
            if group is None:
                group = button
            else:
                button.set_group(group)
            button.connect("toggled", lambda b, k=key: b.get_active() and self.show_reports_tab(k))
            tabs.append(button)
            self.reports_tab_buttons[key] = button
        page.append(tabs)
        self.reports_stack = Gtk.Stack(vexpand=True)
        self.reports_boxes = {}
        for key, _label in TABS:
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP)
            self.reports_boxes[key] = box
            self.reports_stack.add_named(self.scrolled(box), key)
        page.append(self.reports_stack)
        self.reports_footer = Gtk.Box()
        page.append(self.reports_footer)
        self.reports_tab_buttons["log"].set_active(True)
        return page

    def open_reports(self, tab: str = "log"):
        if self.page_name() != "reports":
            self.reports_return = self.view if self.page_name() in ai_core.VIEWS else "main"
        self.show_page("reports")
        key = tab if tab in dict(TABS) else "log"
        button = self.reports_tab_buttons[key]
        if button.get_active():
            self.show_reports_tab(key)        # already that tab: show it again (toggled does not fire)
        else:
            button.set_active(True)           # toggled shows it, once

    def close_reports(self):
        """Back, Esc: a report being generated asks first whether to cancel it."""
        if self.reports_running is not None:
            self.confirm_leave_reports(self.leave_reports)
            return
        self.leave_reports()

    def leave_reports(self):
        self.stop_reports_timer()
        self.show_view(getattr(self, "reports_return", self.view), save=False)

    def confirm_leave_reports(self, then):
        if self.reports_confirming:
            return
        self.reports_confirming = True
        dialog = Adw.AlertDialog(heading=_("Cancel the report?"),
                                 body=_("A topic report is being generated. Leaving the page cancels it."))
        dialog.add_response("stay", _("Back"))
        dialog.add_response("cancel", _("Cancel report"))
        dialog.set_response_appearance("cancel", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("stay")

        def answered(_dialog, response):
            self.reports_confirming = False
            if response == "cancel":
                self.cancel_report()
                then()
        dialog.connect("response", answered)
        dialog.present(self.window)

    def show_reports_tab(self, key: str):
        self.reports_tab = key
        self.reports_stack.set_visible_child_name(key)
        self.stop_reports_timer()
        if key == "log":
            self.fill_log_settings()          # values changed in Settings meanwhile
            self.refresh_reports_log()
            self.reports_timer = GLib.timeout_add_seconds(REFRESH_SECONDS, self.reports_tick)
        elif key == "topics":
            self.load_report_helpers()
            self.fill_reports_topics()
        else:
            self.load_reports_library()
        self.fill_reports_footer()

    def reports_tick(self):
        window = getattr(self, "window", None)
        if self.page_name() != "reports" or self.reports_tab != "log" or window is None or not window.get_visible():
            self.reports_timer = None
            return False
        self.refresh_reports_log(keep_scroll=True)
        return True

    def stop_reports_timer(self):
        """The Log tab's 30 s refresh and the progress bar's pulse (tab change, leaving, hiding the window)."""
        for name in ("reports_timer", "reports_pulse"):
            source = getattr(self, name, None)
            if source:
                GLib.source_remove(source)
                setattr(self, name, None)

    def reports_shown(self):
        """The window is shown again: the Log tab's refresh goes on where it stopped."""
        if self.page_name() != "reports":
            return
        if self.reports_tab == "log" and not getattr(self, "reports_timer", None):
            self.refresh_reports_log(keep_scroll=True)
            self.reports_timer = GLib.timeout_add_seconds(REFRESH_SECONDS, self.reports_tick)
        elif self.reports_tab == "topics" and self.reports_running is not None and not self.reports_pulse:
            self.fill_reports_topics()            # its progress bar pulses again

    def reports_hidden(self):
        """The window is hidden: nothing of this page keeps running in the background but a report
        being generated (it ends by itself; the next open shows it)."""
        self.stop_reports_timer()

    # --- small parts -------------------------------------------------------------------------------

    def report_row(self, box, title: str, control: Gtk.Widget, hint: str = ""):
        row = Gtk.Box(spacing=ai_core.GAP_INLINE)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        label = Gtk.Label(label=title, xalign=0, wrap=True, max_width_chars=1)
        texts.append(label)
        if hint:
            small = Gtk.Label(label=hint, xalign=0, wrap=True, max_width_chars=1)
            small.add_css_class("dim-label")
            texts.append(small)
        row.append(texts)
        control.set_valign(Gtk.Align.CENTER)
        row.append(control)
        box.append(row)
        return row

    def report_switch(self, box, title: str, key: str, hint: str = "", sensitive: bool = True):
        switch = Gtk.Switch(active=ai_core.setting(key) is True, sensitive=sensitive)
        switch.connect("notify::active", lambda s, *_a: self.change_report_setting(key, s.get_active()))
        self.report_row(box, title, switch, hint)
        return switch

    def change_report_setting(self, key: str, value):
        if ai_core.setting(key) == value:
            return
        self.save_setting(**{key: value})

        def again():
            if self.reports_tab == "log":
                switch = getattr(self, "reports_topic_switch", None)
                if switch is not None:            # the controls stay; only what depends on them changes
                    switch.set_sensitive(ai_core.setting("ai_activity_import") is True)
                self.refresh_reports_log()
            elif self.reports_tab == "topics":
                self.load_report_helpers()      # online helpers on or off: the list and the choice follow
                self.fill_reports_topics()
            return False
        GLib.idle_add(again)

    @staticmethod
    def error_label(box, text: str):
        label = Gtk.Label(label=text, xalign=0, wrap=True, max_width_chars=1)
        label.add_css_class("am-hint-error")
        box.append(label)
        return label

    def range_button(self, which: str, on_change) -> Gtk.Widget:
        """Date range: a popover with From and Through calendars, Apply and Clear."""
        chosen = self.reports_range.get(which)
        text = _("Date range") if chosen is None else f"{chosen[0]} – {chosen[1]}"
        button = Gtk.MenuButton(label=text)
        button.add_css_class("am-small-btn")
        pop = Gtk.Popover()
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP_TIGHT, margin_start=8,
                       margin_end=8, margin_top=8, margin_bottom=8)
        calendars = Gtk.Box(spacing=ai_core.GAP)
        pickers = []
        today = dt.date.today()
        for index, heading in enumerate((_("From"), _("Through"))):
            column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            column.append(Gtk.Label(label=heading, xalign=0))
            calendar = Gtk.Calendar()
            day = dt.date.fromisoformat(chosen[index]) if chosen else today
            calendar.select_day(GLib.DateTime.new_local(day.year, day.month, day.day, 0, 0, 0))
            column.append(calendar)
            calendars.append(column)
            pickers.append(calendar)
        body.append(calendars)
        error = Gtk.Label(xalign=0, visible=False)
        error.add_css_class("am-hint-error")
        body.append(error)
        buttons = Gtk.Box(spacing=ai_core.GAP_INLINE, halign=Gtk.Align.END)

        def picked(calendar) -> str:
            date = calendar.get_date()
            return f"{date.get_year():04d}-{date.get_month():02d}-{date.get_day_of_month():02d}"

        def apply(_b):
            first, last = picked(pickers[0]), picked(pickers[1])
            if first > last:
                error.set_label(_("The first day comes after the last."))
                error.set_visible(True)
                return
            pop.popdown()
            self.reports_range[which] = (first, last)
            on_change()

        def clear(_b):
            pop.popdown()
            self.reports_range[which] = None
            on_change()
        for label, handler in ((_("Clear"), clear), (_("Apply"), apply)):
            b = Gtk.Button(label=label)
            b.add_css_class("am-small-btn")
            b.connect("clicked", handler)
            buttons.append(b)
        body.append(buttons)
        pop.set_child(body)
        button.set_popover(pop)
        return button

    def save_export(self, name: str, text: str):
        """Export: a save dialog (or, where none can open, the Documents folder)."""
        def write(path: Path):
            try:
                ai_core.atomic_write(path, text.encode("utf-8"))
                self.show_notice(_("Saved {0}").format(str(path)))
            except OSError:
                self.show_error(_("Could not export the report."))
        try:
            dialog = Gtk.FileDialog(initial_name=name, modal=True)
        except (AttributeError, TypeError):
            write(Path.home() / "Documents" / name)
            return

        def done(source, result):
            try:
                chosen = source.save_finish(result)
            except GLib.Error as error:
                if not error.matches(Gtk.dialog_error_quark(), Gtk.DialogError.DISMISSED) and \
                        not error.matches(Gtk.dialog_error_quark(), Gtk.DialogError.CANCELLED):
                    write(Path.home() / "Documents" / name)
                return
            if chosen is not None and chosen.get_path():
                write(Path(chosen.get_path()))
        dialog.save(self.window, None, done)

    def export_buttons(self, report: dict) -> list:
        copy = self.copy_button_for(lambda: reports_ctl.format_topic_report(report, "text"))
        menu = Gtk.MenuButton(label=_("Export"))
        menu.add_css_class("am-small-btn")
        pop = Gtk.Popover()
        items = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        for ext, form, label in (("txt", "text", "TXT"), ("md", "markdown", _("Markdown")), ("json", "json", "JSON")):
            item = Gtk.Button(label=label)
            item.add_css_class("flat")
            item.connect("clicked", lambda _b, e=ext, f=form: (pop.popdown(), self.save_export(
                reports_ctl.export_name(report, e), reports_ctl.format_topic_report(report, f))))
            items.append(item)
        pop.set_child(items)
        menu.set_popover(pop)
        return [copy, menu]

    @staticmethod
    def copy_button_for(get_text):
        import ui_helpers
        return ui_helpers.copy_button(get_text, label=_("Copy"))

    def report_output(self, box, report: dict, show_title: bool = True):
        """A report as cards: its range and helper, one card per topic, then its text and note."""
        head = self.info_card(report["title"] if show_title else _("Report"))
        self.card_text(head.body, f"{report['from_day']} – {report['through_day']} · {report.get('helper', '')}")
        box.append(head)
        for topic in report["topics"]:
            card = self.info_card(topic["name"])
            self.card_text(card.body, topic["summary"])
            facts = " · ".join(list(topic.get("sources") or []) + list(topic.get("projects") or []))
            if facts:
                self.card_text(card.body, facts, "am-agent-detail")
            numbers = [f"{_('Spans')} {topic['spans']}", activity_log.duration_text(topic["elapsed_ms"])]
            if topic.get("tokens_in") is not None or topic.get("tokens_out") is not None:
                numbers.append(f"{_('Tokens')} ↑{topic.get('tokens_in') if topic.get('tokens_in') is not None else '–'} "
                               f"↓{topic.get('tokens_out') if topic.get('tokens_out') is not None else '–'}")
            if topic.get("usage_unknown"):
                numbers.append(f"{_('Unknown usage')} {topic['usage_unknown']}")
            self.card_text(card.body, " · ".join(numbers), "am-agent-detail")
            box.append(card)
        text = self.info_card(_("Summary"))
        self.card_text(text.body, report["text"])
        box.append(text)
        if report.get("note"):
            note = self.info_card(_("Note"))
            self.card_text(note.body, report["note"])
            box.append(note)

    # --- Log ---------------------------------------------------------------------------------------

    def reports_settings_card(self, box):
        card = self.info_card(_("Settings"))
        body = card.body
        self.report_switch(body, _("AI activity log"), "ai_activity_log",
                           _("Off by default. Records when agents work, their project and file names and token "
                             "counts, never file contents."))
        days = ai_core.setting("ai_activity_log_days")
        choices = keep_choices(days)
        drop = Gtk.DropDown.new_from_strings([keep_label(d) for d in choices])
        drop.set_selected(choices.index(days))
        drop.connect("notify::selected", lambda d, *_a: self.change_report_setting(
            "ai_activity_log_days", choices[d.get_selected()]))
        self.report_row(body, _("Keep log for (days)"), drop)
        importing = ai_core.setting("ai_activity_import") is True
        self.report_switch(body, _("Collect activity from agent history"), "ai_activity_import")
        self.reports_topic_switch = self.report_switch(body, _("Use the agent's request as topic"),
                                                       "ai_activity_import_topics", sensitive=importing)
        ready = self.local_ai_helper() is not None
        self.report_switch(body, _("Topic summaries by local LLM"), "ai_activity_local_topics",
                           "" if ready else _("No report helper is available."), sensitive=ready)
        box.append(card)

    def start_history(self, auto: bool = False):
        installed = self.installed_commands()
        sources = history_import.history_sources(installed)
        started = self.history_job().start(self.activity_store(), sources, auto, self.history_changed)
        if started:
            self.history_changed()
        return False

    def history_changed(self):
        if self.page_name() == "reports" and self.reports_tab == "log":
            self.refresh_reports_log(keep_scroll=True)
        return False

    def collect_history(self):
        """Collect history: when the log or the import is off, offer to switch it on first."""
        missing = [k for k in ("ai_activity_log", "ai_activity_import") if ai_core.setting(k) is not True]
        if not missing:
            self.start_history(False)
            return
        dialog = Adw.AlertDialog(heading=_("Collect history"),
                                 body=_("Turn on the activity log to collect entries.") if "ai_activity_log" in missing
                                 else _("Collect activity from agent history"))
        dialog.add_response("back", _("Back"))
        dialog.add_response("on", _("Switch on"))
        dialog.set_response_appearance("on", Adw.ResponseAppearance.SUGGESTED)

        def answered(_d, response):
            if response == "on":
                self.save_setting(**{k: True for k in missing})
                self.fill_log_settings()          # the switches above show what was just switched on
                self.start_history(False)
        dialog.connect("response", answered)
        dialog.present(self.window)

    def history_status(self, box):
        job = self.history_job()
        row = Gtk.Box(spacing=ai_core.GAP_INLINE)
        button = Gtk.Button(sensitive=not job.running)
        content = Gtk.Box(spacing=6)
        if job.running:
            spinner = Gtk.Spinner(spinning=True)
            content.append(spinner)
        else:
            content.append(Gtk.Image.new_from_icon_name("document-open-recent-symbolic"))
        content.append(Gtk.Label(label=_("Collect history")))
        button.set_child(content)
        button.add_css_class("am-small-btn")
        button.set_tooltip_text(_("Collect activity from outside AI Agents Map"))
        button.connect("clicked", lambda _b: self.collect_history())
        row.append(button)
        if job.running and job.progress:
            row.append(Gtk.Label(label=job.progress, xalign=0))
        box.append(row)
        if job.error:
            self.error_label(box, reports_ctl.import_error(job.error))
        for source in (job.result or {}).get("sources", []):
            self.card_text(box, reports_ctl.import_line(source), "am-agent-detail")

    def refresh_reports_log(self, keep_scroll: bool = False):
        """Read the log on a worker (up to the retention's day files), then show it."""
        store = self.activity_store()
        if self.log_parts()["entries"].get_first_child() is None:
            self.render_reports_log(None, keep_scroll, loading=store.enabled)
        if not store.enabled:
            self.reports_requests["log"] = self.reports_requests.get("log", 0) + 1     # drop a read under way
            self.render_reports_log(None, keep_scroll)
            return
        params = {"cursor": self.reports_cursor}
        chosen = self.reports_range["log"]
        if chosen:
            params.update(from_day=chosen[0], through_day=chosen[1])

        def done(result):
            report = result if isinstance(result, dict) else None
            if report is not None:
                self.reports_log_report = report
                self.local_topics(report)
            self.render_reports_log(report, keep_scroll)
        self.reports_worker("log", lambda: store.report(params), done)

    def log_parts(self):
        """The Log tab's three parts, made once: its settings (kept while the tab is shown, so a switch
        keeps the keyboard), Collect history, and the entries (the only part a refresh replaces)."""
        parts = getattr(self, "reports_log_parts", None)
        if parts is None:
            box = self.reports_boxes["log"]
            self.clear_box(box)
            parts = self.reports_log_parts = {k: Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP)
                                              for k in ("settings", "history", "entries")}
            for part in parts.values():
                box.append(part)
        return parts

    def fill_log_settings(self):
        part = self.log_parts()["settings"]
        self.clear_box(part)
        self.reports_settings_card(part)

    def log_busy(self, area) -> bool:
        """Text selected in the entries: a timed refresh waits until it is not (as Maprisma's does)."""
        stack = [area]
        while stack:
            widget = stack.pop()
            if isinstance(widget, Gtk.Label) and widget.get_selectable() and widget.get_selection_bounds()[0]:
                return True
            child = widget.get_first_child()
            while child is not None:
                stack.append(child)
                child = child.get_next_sibling()
        return False

    def render_reports_log(self, report: dict | None, keep_scroll: bool = False, loading: bool = False):
        parts = self.log_parts()
        if parts["settings"].get_first_child() is None:
            self.fill_log_settings()
        self.clear_box(parts["history"])
        self.history_status(parts["history"])
        area = parts["entries"]
        if keep_scroll and area.get_first_child() is not None and self.log_busy(area):
            self.reports_log_deferred = report       # shown on the next refresh once nothing is selected
            return
        self.reports_log_deferred = None
        box = self.reports_boxes["log"]
        scroller = box.get_parent().get_parent() if box.get_parent() is not None else None
        adjustment = scroller.get_vadjustment() if isinstance(scroller, Gtk.ScrolledWindow) else None
        position = adjustment.get_value() if adjustment is not None and keep_scroll else 0
        self.clear_box(area)
        if not self.activity_store().enabled:
            self.card_text(area, _("Turn on the activity log to collect entries."))
        elif loading:
            area.append(Gtk.Spinner(spinning=True, halign=Gtk.Align.START))
        else:
            self.log_pane(area, report)
        if adjustment is not None and keep_scroll:
            GLib.idle_add(lambda: adjustment.set_value(position) or False)
        self.fill_reports_footer()

    def local_topics(self, report: dict):
        """Topic summaries by local LLM: topics classified earlier are shown; entries not asked yet are
        classified in the background (reports_ctl.classify_topics), then the tab is shown again."""
        known = self.reports_local_topics
        for row in report["entries"]:
            if known.get(row["id"]) and (row["topic"] is None or row["topic"] in activity_log.HEURISTIC):
                row["topic"] = known[row["id"]]
        if ai_core.setting("ai_activity_local_topics") is not True or self.reports_classifying \
                or time.monotonic() < self.reports_classify_after:
            return
        candidate = self.local_ai_helper()
        fresh = [dict(r) for r in reports_ctl.topic_candidates(report["entries"]) if r["id"] not in known][:8]
        if candidate is None or not fresh:
            return
        self.reports_classifying = True

        def apply(answers):
            self.reports_classifying = False      # the window's thread only: the page reads `known` there
            if answers is None:                   # the local LLM did not answer: try again in a while
                self.reports_classify_after = time.monotonic() + CLASSIFY_BACKOFF
                return False
            changed = False
            for entry_id, topic in answers.items():   # only what it answered for is remembered
                known[entry_id] = topic
                changed = changed or topic is not None
            while len(known) > LOCAL_TOPICS_KEPT:
                del known[next(iter(known))]
            if changed:
                self.history_changed()
            return False

        def work():
            try:
                answers = reports_ctl.classify_entries(fresh, candidate)
            except Exception:
                answers = None
            GLib.idle_add(apply, answers)
        threading.Thread(target=work, daemon=True).start()

    def log_pane(self, box, report: dict | None):
        actions = Gtk.Box(spacing=ai_core.GAP_INLINE)
        actions.append(self.range_button("log", lambda: (setattr(self, "reports_cursor", None),
                                                          self.refresh_reports_log())))
        for label, handler, shown in (
                (_("Refresh"), lambda: self.refresh_reports_log(), True),
                (_("First page"), lambda: (setattr(self, "reports_cursor", None), self.refresh_reports_log()),
                 self.reports_cursor is not None),
                (_("Next"), lambda: (setattr(self, "reports_cursor", report["next_cursor"]), self.refresh_reports_log()),
                 report is not None and report.get("next_cursor") is not None)):
            if shown:
                actions.append(self.action_button(label, on_click=handler))
        if report is not None:
            actions.append(self.copy_button_for(lambda: activity_log.log_text(report)))
            actions.append(self.action_button("TXT", on_click=lambda: self.save_export(
                "ai-agents-map-activity.txt", activity_log.log_text(report))))
            actions.append(self.action_button("JSON", on_click=lambda: self.save_export(
                "ai-agents-map-activity.json", json.dumps(report, indent=2, ensure_ascii=False))))
        box.append(actions)
        if report is None:
            self.card_text(box, _("No details available"))
            return
        card = self.info_card(f"{report['from_day']} – {report['through_day']}")
        if not report["entries"]:
            self.card_text(card.body, _("No details available"))
        for row in report["entries"]:
            line = Gtk.Box(spacing=ai_core.GAP_INLINE)
            if row["origin"] == "history":
                mark = Gtk.Image.new_from_icon_name("document-open-recent-symbolic")
                mark.set_tooltip_text(_("From the agent's own history"))
                mark.set_valign(Gtk.Align.START)
                line.append(mark)
            text = Gtk.Label(label=activity_log.row_text(row), xalign=0, wrap=True, hexpand=True, selectable=True,
                             max_width_chars=1)
            text.add_css_class("am-card-text")
            line.append(text)
            card.body.append(line)
        totals = "\n".join(activity_log.totals_text(report)).strip()
        if totals:
            self.card_text(card.body, totals)
        box.append(card)

    # --- Topic reports -----------------------------------------------------------------------------

    def load_report_helpers(self):
        installed = self.installed_commands()
        candidate = self.local_ai_helper()
        try:
            self.reports_helpers = reports_ctl.report_helpers(installed, candidate)
        except Exception:
            self.reports_helpers = {"helpers": [], "chosen": None, "online_allowed": reports_ctl.online_allowed()}
        chosen = self.reports_helper
        if not any(h["command"] == chosen for h in self.reports_helpers["helpers"]):
            self.reports_helper = self.reports_helpers["chosen"]

    def fill_reports_topics(self):
        box = self.reports_boxes["topics"]
        self.clear_box(box)
        running = self.reports_running is not None
        card = self.info_card(_("Topic reports"))
        body = card.body
        row = Gtk.Box(spacing=ai_core.GAP_INLINE)
        picker = self.range_button("topics", self.fill_reports_topics)
        picker.set_sensitive(not running)
        row.append(picker)
        body.append(row)
        self.reports_entries = {}
        for key, label, limit in (("project", _("Project filter"), 64), ("source", _("Agent or runtime filter"), 130),
                                  ("focus", _("Report focus"), 500)):
            entry = Gtk.Entry(placeholder_text=label, max_length=limit, sensitive=not running, hexpand=True)
            entry.set_text(self.reports_fields.get(key, ""))
            entry.connect("changed", lambda e, k=key: self.reports_fields.update({k: e.get_text()}))
            if key == "focus":
                entry.set_tooltip_text(_("What should the report look at?"))
            body.append(entry)
            self.reports_entries[key] = entry
        helpers = (self.reports_helpers or {}).get("helpers", [])
        if helpers:
            names = [h["name"] for h in helpers]
            drop = Gtk.DropDown.new_from_strings(names)
            commands = [h["command"] for h in helpers]
            if self.reports_helper in commands:
                drop.set_selected(commands.index(self.reports_helper))
            drop.set_sensitive(not running)
            drop.connect("notify::selected", lambda d, *_a: setattr(self, "reports_helper", commands[d.get_selected()]))
            self.report_row(body, _("Helper"), drop)
        self.report_switch(body, _("Online helpers may write topic reports"), "ai_activity_log_online",
                           _("Off: only a local helper is offered, and the log never leaves this PC."))
        if self.reports_helpers is not None and not self.reports_helpers.get("online_allowed"):
            self.card_text(body, _("Online helpers are off for activity reports."), "am-agent-detail")
        if self.reports_helpers is not None and self.reports_helper is None:
            self.card_text(body, _("No report helper is available."), "am-agent-detail")
        box.append(card)
        if running:
            bar = Gtk.ProgressBar()
            bar.pulse()
            self.reports_bar = bar
            if self.reports_pulse is None:
                def pulse():
                    if self.reports_running is None:
                        self.reports_pulse = None
                        return False
                    self.reports_bar.pulse()
                    return True
                self.reports_pulse = GLib.timeout_add(200, pulse)
            box.append(bar)
            self.card_text(box, _("Generating report…"))
        if self.reports_topic_error:
            self.error_label(box, reports_ctl.message(self.reports_topic_error))
        draft = self.reports_draft
        if draft is not None:
            title = Gtk.Entry(text=self.reports_title or draft["title"], max_length=200)
            title.set_placeholder_text(_("Title"))
            title.connect("changed", lambda e: (setattr(self, "reports_title", e.get_text()), self.fill_reports_footer()))
            box.append(title)
            if draft.get("capped"):
                self.card_text(box, _("Only part of the matching log fit in this report."), "am-agent-detail")
            self.report_output(box, draft, show_title=False)
        self.fill_reports_footer()

    def generate_report(self):
        helpers = (self.reports_helpers or {}).get("helpers", [])
        helper = next((h for h in helpers if h["command"] == self.reports_helper), None)
        if helper is None:
            self.reports_topic_error = "no_helper"
            self.fill_reports_topics()
            return
        fields = self.reports_fields
        params = {"helper": helper["command"], **{k: v.strip() for k, v in fields.items() if v.strip()}}
        chosen = self.reports_range["topics"]
        if chosen:
            params.update(from_day=chosen[0], through_day=chosen[1])
        local = helper["local"]

        def gate():
            if not local and not reports_ctl.online_allowed():
                raise reports_ctl.ReportError("online_blocked")
        try:
            gate()
        except reports_ctl.ReportError as error:
            self.reports_topic_error = error.code
            self.fill_reports_topics()
            return
        cancel = threading.Event()
        self.reports_running = cancel
        self.reports_topic_error = None
        store = self.activity_store()

        def work():
            import ai_helper
            try:
                with ai_helper.watched(cancel):
                    draft = reports_ctl.generate_topic_report(
                        store, params, lambda prompt: reports_ctl.ask_with(helper["helper"], prompt), before_run=gate)
                result = ("draft", draft)
            except reports_ctl.ReportError as error:
                result = ("error", error.code)
            except ValueError:
                result = ("error", "invalid_request")
            except Exception:
                result = ("error", "helper_failed")
            GLib.idle_add(finished, result)

        def finished(result):
            if self.reports_running is not cancel:
                return False                     # cancelled: the answer is dropped
            self.reports_running = None
            kind, value = result
            if kind == "draft":
                self.reports_draft = value
                self.reports_draft_id = self.reports_drafts.add(value)
                self.reports_title = value["title"]
            else:
                self.reports_topic_error = value
            if self.page_name() == "reports":
                self.fill_reports_topics()
            return False
        threading.Thread(target=work, daemon=True).start()
        self.fill_reports_topics()

    def cancel_report(self):
        cancel, self.reports_running = self.reports_running, None
        if cancel is not None:
            cancel.set()
        if getattr(self, "reports_tab", None) == "topics":
            self.fill_reports_topics()

    def save_report(self):
        try:
            title = reports_ctl.report_field(self.reports_title, 200, required=True)
            draft = self.reports_drafts.take(self.reports_draft_id)
        except ValueError:
            return
        except reports_ctl.ReportError as error:
            self.reports_topic_error = error.code
            self.fill_reports_topics()
            return
        handle = self.reports_draft_id
        self.reports_saving = True
        self.fill_reports_footer()

        def done(result):
            self.reports_saving = False
            if isinstance(result, reports_ctl.ReportError):
                self.reports_topic_error = result.code
            elif isinstance(result, Exception):
                self.reports_topic_error = "reports_db_unavailable"
            else:
                self.reports_drafts.saved(handle)
                if self.reports_draft_id == handle:
                    self.reports_draft_id = None
                self.show_notice(_("Report saved"))
            if self.reports_tab == "topics":
                self.fill_reports_topics()
        self.reports_worker("save", lambda: reports_ctl.ReportsLibrary().save(draft, title), done)

    # --- running library and log reads off the window's thread ---------------------------------------

    def reports_worker(self, kind: str, work, done):
        """work() on a worker thread, done(result or the exception) back on the window's thread; only the
        newest request of each kind is delivered (a slower, older search never overwrites a newer one)."""
        self.reports_requests[kind] = number = self.reports_requests.get(kind, 0) + 1

        def deliver(result):
            if self.reports_requests.get(kind) == number:
                done(result)
            return False

        def run():
            try:
                result = work()
            except Exception as error:      # ReportError, OSError, ValueError: shown by done()
                result = error
            GLib.idle_add(deliver, result)
        threading.Thread(target=run, daemon=True).start()

    # --- Library -----------------------------------------------------------------------------------

    def library_failed(self, result) -> bool:
        if isinstance(result, reports_ctl.ReportError):
            self.reports_library_error = result.code
            return True
        if isinstance(result, Exception):
            self.reports_library_error = "reports_db_unavailable"
            return True
        return False

    def load_reports_library(self, cursor: int | None = None, results_only: bool = False):
        query = self.reports_query
        self.reports_library_loading = True
        if not results_only:
            self.fill_reports_library()

        def done(result):
            self.reports_library_loading = False
            if self.library_failed(result):
                self.reports_library_page = None
            else:
                self.reports_library_page, self.reports_library_error = result, None
            if results_only and self.reports_opened is None and self.reports_library_results is not None:
                self.fill_library_results()
            else:
                self.fill_reports_library()
        self.reports_worker("library", lambda: reports_ctl.ReportsLibrary().list(query, cursor), done)

    def library_search(self) -> Gtk.SearchEntry:
        """The search field lives as long as the page: typing never loses focus or the caret."""
        search = getattr(self, "reports_library_search", None)
        if search is None:
            search = self.reports_library_search = Gtk.SearchEntry(placeholder_text=_("Search reports"))
            search.connect("search-changed", lambda e: (setattr(self, "reports_query", e.get_text()),
                                                         self.load_reports_library(results_only=True)))
        return search

    def fill_reports_library(self):
        box = self.reports_boxes["library"]
        search = self.library_search()
        self.clear_box(box)
        self.reports_library_errors = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP_TIGHT)
        box.append(self.reports_library_errors)
        opened = self.reports_opened
        self.reports_library_results = None
        if opened is None:
            box.append(search)
            self.reports_library_results = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP)
            box.append(self.reports_library_results)
        else:
            back = Gtk.Button()
            back.set_child(Adw.ButtonContent(icon_name="go-previous-symbolic", label=_("Library")))
            back.add_css_class("flat")
            back.set_halign(Gtk.Align.START)
            back.connect("clicked", lambda _b: (setattr(self, "reports_opened", None), self.load_reports_library()))
            box.append(back)
            self.report_output(box, opened)
        self.fill_library_results()

    def fill_library_results(self):
        """The errors and the list under the search field, replaced without touching the field."""
        errors = getattr(self, "reports_library_errors", None)
        if errors is not None:
            self.clear_box(errors)
            page = self.reports_library_page or {}
            if page.get("read_only"):
                self.error_label(errors, reports_ctl.message("reports_db_read_only"))
            if self.reports_library_error:
                self.error_label(errors, reports_ctl.message(self.reports_library_error))
        results = getattr(self, "reports_library_results", None)
        if results is not None:
            self.clear_box(results)
            if getattr(self, "reports_library_loading", False):
                results.append(Gtk.Spinner(spinning=True, halign=Gtk.Align.START))
            rows = (self.reports_library_page or {}).get("reports")
            if rows is not None and not rows:
                self.card_text(results, _("No saved reports."))
            elif rows:
                listing = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
                listing.add_css_class("boxed-list")
                for row in rows:
                    item = Adw.ActionRow(title=row["title"], use_markup=False, activatable=True,
                                         subtitle=f"{row['from_day']} – {row['through_day']}  ·  "
                                                  f"{str(row['saved_at']).split('T')[0]}")
                    item.add_prefix(Gtk.Image.new_from_icon_name("x-office-document-symbolic"))
                    item.connect("activated", lambda _r, i=row["id"]: self.open_saved_report(i))
                    listing.append(item)
                results.append(listing)
        self.fill_reports_footer()

    def open_saved_report(self, report_id: int):
        def done(result):
            if not self.library_failed(result):
                self.reports_opened, self.reports_library_error = result, None
            self.fill_reports_library()
        self.reports_worker("library_open", lambda: reports_ctl.ReportsLibrary().get(report_id), done)

    def delete_saved_report(self, report_id: int):
        dialog = Adw.AlertDialog(heading=_("Delete this report?"),
                                 body=_("It is removed from the library. This cannot be undone."))
        dialog.add_response("back", _("Back"))
        dialog.add_response("delete", _("Delete"))
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)

        def done(result):
            if self.library_failed(result):
                self.fill_reports_library()       # the error stays in view
                return
            self.reports_opened = None
            self.load_reports_library()

        def answered(_d, response):
            if response == "delete":
                self.reports_worker("library_write", lambda: reports_ctl.ReportsLibrary().delete(report_id), done)
        dialog.connect("response", answered)
        dialog.present(self.window)

    def edit_saved_report(self, report: dict):
        dialog = Adw.AlertDialog(heading=_("Edit title and note"))
        fields = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP_TIGHT)
        title = Gtk.Entry(text=report["title"], max_length=200, placeholder_text=_("Title"))
        note = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR, height_request=90)
        note.get_buffer().set_text(report.get("note") or "")
        fields.append(title)
        fields.append(note)
        dialog.set_extra_child(fields)
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("save", _("Save"))
        dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)

        def done(result):
            if not self.library_failed(result):
                self.reports_opened, self.reports_library_error = result, None
            self.fill_reports_library()

        def answered(_d, response):
            if response != "save":
                return
            buffer = note.get_buffer()
            try:
                new_title = reports_ctl.report_field(title.get_text(), 200, required=True)
                new_note = reports_ctl.report_field(buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False),
                                                    2000, multiline=True)
            except ValueError:
                return
            self.reports_worker("library_write", lambda: reports_ctl.ReportsLibrary().update(
                report["id"], title=new_title, note=new_note), done)
        dialog.connect("response", answered)
        dialog.present(self.window)

    def move_library_aside(self):
        """Move aside and start new, after asking: the damaged file is renamed, never deleted."""
        dialog = Adw.AlertDialog(heading=_("Move the damaged library aside?"),
                                 body=_("Its files are renamed and kept, and an empty library is started. "
                                        "Reports saved in it no longer appear here."))
        dialog.add_response("back", _("Back"))
        dialog.add_response("move", _("Move aside and start new"))
        dialog.set_response_appearance("move", Adw.ResponseAppearance.DESTRUCTIVE)

        def done(result):
            if self.library_failed(result):
                self.fill_reports_library()
                return
            self.reports_library_error = None
            self.reports_opened = None
            self.show_notice(_("The damaged library was moved to {0}.").format(result))
            self.load_reports_library()

        def answered(_d, response):
            if response == "move":
                self.reports_worker("library_write", lambda: reports_ctl.ReportsLibrary().move_aside(), done)
        dialog.connect("response", answered)
        dialog.present(self.window)

    # --- the footer --------------------------------------------------------------------------------

    def fill_reports_footer(self):
        footer = getattr(self, "reports_footer", None)
        if footer is None:
            return
        self.clear_box(footer)
        buttons, tab = [], self.reports_tab
        if tab == "topics":
            running = self.reports_running is not None
            if self.reports_draft is not None:
                buttons += self.export_buttons({**self.reports_draft, "title": self.reports_title})
            if running:
                buttons.append(self.action_button(_("Cancel report"), on_click=self.cancel_report))
            if self.reports_draft_id is not None:
                save = self.action_button(_("Save to library"), on_click=self.save_report)
                save.set_sensitive(not running and not self.reports_saving and bool(self.reports_title.strip()))
                buttons.append(save)
            generate = self.action_button(_("Generate"), role="secondary" if self.reports_draft_id else "primary",
                                          on_click=self.generate_report)
            generate.set_sensitive(not running and self.reports_helper is not None)
            buttons.append(generate)
        elif tab == "library":
            opened = self.reports_opened
            if self.reports_library_error == "reports_db_corrupt":
                buttons.append(self.action_button(_("Move aside and start new"), on_click=self.move_library_aside))
            if opened is not None:
                buttons += self.export_buttons(opened)
                if not (self.reports_library_page or {}).get("read_only"):
                    buttons.append(self.action_button(_("Delete"), role="danger",
                                                      on_click=lambda: self.delete_saved_report(opened["id"])))
                    buttons.append(self.action_button(_("Edit title and note"), on_click=lambda: self.edit_saved_report(opened)))
            else:
                buttons.append(self.action_button(_("Refresh"), on_click=self.load_reports_library))
                cursor = (self.reports_library_page or {}).get("next_cursor")
                if cursor is not None:
                    buttons.append(self.action_button(_("Next"), on_click=lambda: self.load_reports_library(cursor)))
        if buttons:
            row = self.action_row(*buttons)
            row.set_hexpand(True)
            footer.append(row)

    # --- How reporting works -----------------------------------------------------------------------

    def show_reporting_help(self):
        """The help dialog (F1 on this page): once at a time, Esc closes it."""
        if self.reports_help_open:
            return
        self.reports_help_open = True
        sources = history_import.public_sources(history_import.history_sources(self.installed_commands()))
        window = Gtk.Window(title=_("How reporting works"), transient_for=self.window, modal=True,
                            default_width=640, default_height=560)
        window.add_css_class("am-card")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP, margin_start=18, margin_end=18,
                      margin_top=14, margin_bottom=14)
        sections = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP_TIGHT)
        for title, body in reports_ctl.HELP_SECTIONS:
            self.section_heading(sections, _(title), muted=False)
            self.card_text(sections, _(body))
            if title == "Outside AI Agents Map":
                for source in sources:
                    state = _("Supported") if source["supported"] else _("Unsupported")
                    self.card_text(sections, f"{source['name']} · {state}", "am-agent-detail")
        box.append(self.scrolled(sections))
        buttons = Gtk.Box(spacing=ai_core.GAP_INLINE, halign=Gtk.Align.END)
        buttons.append(self.copy_button_for(lambda: reports_ctl.help_text(sources)))
        manual = self.action_button(_("Open user manual"), on_click=lambda: self.open_reports_manual())
        buttons.append(manual)
        close = self.action_button(_("Close"), role="primary", on_click=window.close)
        buttons.append(close)
        box.append(buttons)
        window.set_child(box)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", lambda _c, key, _code, _state: (window.close(), True)[1]
                     if key in (Gdk.KEY_Escape, Gdk.KEY_F1) else False)
        window.add_controller(keys)
        window.connect("close-request", lambda _w: setattr(self, "reports_help_open", False) or False)
        window.present()

    def open_reports_manual(self):
        from gi.repository import Gio
        path = self.manual_path()
        if path is None:
            self.show_error(_("The action could not be completed."))
            return
        try:
            Gio.AppInfo.launch_default_for_uri(path.as_uri(), None)
        except GLib.Error:
            self.show_error(_("The action could not be completed."))
