"""AI Agents Map: "What is it doing?" for a running agent (Maprisma task 074: agent_activity_dialog.dart;
a mixin of ai_popup.AgentsMap).

A window over the map with agent_activity_report's report for one running instance: its phase,
whether it may be stuck, the current request, answers, tools and edited files, the progress it
reported, the changed-file timeline, its helper processes and what started it. It is read again
every 3 s on a worker thread while it is open (the scroll position stays), Copy takes the whole
report as text, Esc or Close closes it and stops the refresh. With several instances it first asks
which one.
"""

from __future__ import annotations

import threading

from gi.repository import Gdk, GLib, Gtk

import agent_activity_report
import ai_core
from i18n import _

REFRESH_SECONDS = 3


class ActivityView:
    """The dialog's state without its widgets (kept apart so it can be checked headless)."""

    def __init__(self, command: str, fetch):
        self.command = command
        self.fetch = fetch            # fetch(instance or None) -> agents.activity_report result
        self.instance: str | None = None
        self.data: dict | None = None
        self.loading = False
        self.closed = False

    def load(self) -> dict | None:
        if self.loading or self.closed:
            return None
        self.loading = True
        asked = self.instance
        try:
            data = self.fetch(asked)
        except Exception:
            data = {} if self.data is None else None
        finally:
            self.loading = False
        if data is None or self.closed or asked != self.instance:
            return None
        self.data = data
        report = data.get("report")
        if isinstance(report, dict):
            self.instance = report["instance"]["id"]
        return data

    def text(self) -> str:
        report = (self.data or {}).get("report")
        return agent_activity_report.report_text(report) if isinstance(report, dict) else ""

    def choose(self, instance: str):
        self.instance = instance


class AgentActivityDialog:
    def agent_activity_fetch(self, command: str):
        def fetch(instance):
            pids = dict(getattr(self, "agent_pids", {}) or {}).get(command, [])
            try:
                import desktop
                windows = desktop.windows()
            except Exception:
                windows = {}
            if not hasattr(self, "activity_idle"):
                self.activity_idle = agent_activity_report.ActivityIdle()
            return agent_activity_report.agent_activity_report(command, instance, pids, windows, self.activity_idle)
        return fetch

    def open_agent_activity(self, command: str, instance: str | None = None):
        """What is it doing? for one agent (its menu on the map, its details, a terminal's activity button)."""
        agent = next((a for a in getattr(self, "agent_list", []) if a.command == command), None)
        name = agent.name if agent is not None else command
        view = ActivityView(command, self.agent_activity_fetch(command))
        view.instance = instance
        window = Gtk.Window(title=_("What is it doing?") + f" · {name}", transient_for=self.window, modal=True,
                            default_width=660, default_height=520)
        window.add_css_class("am-card")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP, margin_start=18, margin_end=18,
                      margin_top=14, margin_bottom=14)
        heading = Gtk.Label(label=_("What is it doing?").upper(), xalign=0)
        heading.add_css_class("am-section-title")
        box.append(heading)
        content = Gtk.Stack(vexpand=True)
        content.add_named(Gtk.Spinner(spinning=True, halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER), "loading")
        text = Gtk.TextView(editable=False, cursor_visible=False, wrap_mode=Gtk.WrapMode.WORD_CHAR,
                            left_margin=6, right_margin=6, top_margin=6, bottom_margin=6)
        text.add_css_class("am-card-text")
        scroller = Gtk.ScrolledWindow(vexpand=True, child=text)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        content.add_named(scroller, "report")
        choices = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        choices.add_css_class("boxed-list")
        content.add_named(self.scrolled(choices), "choose")
        empty = Gtk.Label(label=_("No details available"), wrap=True)
        content.add_named(empty, "empty")
        box.append(content)
        buttons = Gtk.Box(spacing=ai_core.GAP_INLINE, halign=Gtk.Align.END)
        import ui_helpers
        copy = ui_helpers.copy_button(view.text, label=_("Copy"))
        buttons.append(copy)
        close = self.action_button(_("Close"), role="primary", on_click=window.close)
        buttons.append(close)
        box.append(buttons)
        window.set_child(box)
        shown = {"text": None}

        def show(data):
            if view.closed or data is None:
                return False
            report = data.get("report")
            if isinstance(report, dict):
                value = view.text()
                if value != shown["text"]:
                    adjustment = scroller.get_vadjustment()
                    position = adjustment.get_value()
                    text.get_buffer().set_text(value)
                    shown["text"] = value
                    GLib.idle_add(lambda: adjustment.set_value(position) or False)
                content.set_visible_child_name("report")
            elif data.get("status") == "choose_instance":
                while (row := choices.get_first_child()) is not None:
                    choices.remove(row)
                for item in data.get("instances", []):
                    button = Gtk.Button(label=item["label"])
                    button.add_css_class("flat")
                    button.connect("clicked", lambda _b, i=item["id"]: (view.choose(i), refresh()))
                    choices.append(button)
                content.set_visible_child_name("choose")
            else:
                content.set_visible_child_name("empty")
            copy.set_sensitive(bool(view.text()))
            return False

        def refresh():
            if view.closed:
                return False
            threading.Thread(target=lambda: GLib.idle_add(show, view.load()), daemon=True).start()
            return True

        timer = GLib.timeout_add_seconds(REFRESH_SECONDS, refresh)

        def closing(_w):
            view.closed = True
            GLib.source_remove(timer)
            return False
        window.connect("close-request", closing)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", lambda _c, key, _code, _state: (window.close(), True)[1]
                     if key == Gdk.KEY_Escape else False)
        window.add_controller(keys)
        copy.set_sensitive(False)
        refresh()
        window.present()
        return view
