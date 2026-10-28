"""The box an AI help run shows while it works: centred over the window like the first open's splash,
with who is asked (the agent, its model), the steps (each a spinner that turns into ✓, or ✗ when it
failed), the time it has taken so far, and two buttons: In the background (the box goes, the run goes
on and a toast says when it is done) and Cancel (the agent's program and its children are ended).
When it is done the box says so for a moment and fades; a failure stays until it is closed.

Only the box: the run itself is PageParts.run_ai_help, which drives it from the worker thread through
GLib.idle_add."""

from __future__ import annotations

import time

from gi.repository import GLib, Gtk

from i18n import _

FADE_MS = 450          # as the CSS transition of .am-splash
HOLD_MS = 1400         # the finished box stays this long before it fades


def elapsed_text(seconds: float) -> str:
    seconds = int(seconds)
    return _("{0} s").format(seconds) if seconds < 60 else _("{0} min {1} s").format(seconds // 60, seconds % 60)


class AiProgress:
    def __init__(self, title: str, who: str, icon_name: str, steps, on_cancel=None, on_background=None):
        """steps: [(key, text)] in their order; the first one starts at once."""
        self.gone = False
        self.ended = False
        self.started = time.monotonic()
        self.on_cancel, self.on_background = on_cancel, on_background
        self.widget = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, halign=Gtk.Align.CENTER,
                              valign=Gtk.Align.CENTER)
        self.widget.add_css_class("am-splash")
        self.widget.add_css_class("am-ai-progress")
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, halign=Gtk.Align.CENTER,
                        width_request=340)
        icon = Gtk.Image.new_from_icon_name(icon_name)
        icon.set_pixel_size(56)
        icon.add_css_class("am-splash-icon")
        inner.append(icon)
        name = Gtk.Label(label=title, wrap=True, max_width_chars=40, justify=Gtk.Justification.CENTER)
        name.add_css_class("am-splash-title")
        inner.append(name)
        self.who = Gtk.Label(label=who, wrap=True, max_width_chars=48, justify=Gtk.Justification.CENTER)
        self.who.add_css_class("am-agent-detail")
        inner.append(self.who)
        listing = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_top=4)
        self.rows: dict = {}
        self.order = [key for key, _text in steps]
        for key, text in steps:
            line = Gtk.Box(spacing=10)
            mark = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, transition_duration=250)
            waiting = Gtk.Label(label="·")
            waiting.add_css_class("am-splash-count")
            mark.add_named(waiting, "waiting")
            spinner = Gtk.Spinner(spinning=False)
            mark.add_named(spinner, "busy")
            for state, sign in (("done", "✓"), ("failed", "✗")):
                label = Gtk.Label(label=sign)
                label.add_css_class("am-splash-tick" if state == "done" else "am-ai-failed")
                mark.add_named(label, state)
            mark.set_visible_child_name("waiting")
            line.append(mark)
            label = Gtk.Label(label=text, xalign=0, hexpand=True, wrap=True, max_width_chars=40)
            label.add_css_class("am-splash-row")
            line.append(label)
            detail = Gtk.Label(label="", xalign=1)
            detail.add_css_class("am-splash-count")
            line.append(detail)
            listing.append(line)
            self.rows[key] = {"mark": mark, "spinner": spinner, "label": label, "detail": detail, "state": "waiting"}
        inner.append(listing)
        self.status = Gtk.Label(label="", wrap=True, max_width_chars=48, justify=Gtk.Justification.CENTER)
        self.status.add_css_class("am-agent-detail")
        inner.append(self.status)
        self.buttons = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER, margin_top=4)
        # The pages' button look (PageParts.action_button: page size, am-danger for stopping).
        self.background = Gtk.Button(label=_("In the background"))
        self.background.connect("clicked", lambda _b: self._to_background())
        self.cancel = Gtk.Button(label=_("Cancel"))
        self.cancel.add_css_class("am-danger")
        self.cancel.connect("clicked", lambda _b: self._cancel())
        self.close = Gtk.Button(label=_("Close"), visible=False)
        self.close.connect("clicked", lambda _b: self.fade())
        for button in (self.background, self.cancel, self.close):
            button.add_css_class("am-page-btn")
            self.buttons.append(button)
        inner.append(self.buttons)
        self.widget.append(inner)
        if self.order:
            self.start(self.order[0])
        self._tick()
        GLib.timeout_add_seconds(1, self._tick)

    # --- what the run reports (main thread) ---------------------------------------------------------

    def start(self, key: str, text: str = ""):
        """`key` is under way (the ones before it are done); text: a new wording for its row."""
        row = self.rows.get(key)
        if row is None or self.ended:
            return
        for before in self.order[:self.order.index(key)]:
            if self.rows[before]["state"] in ("waiting", "busy"):
                self.done(before)
        if text:
            row["label"].set_label(text)
        row["state"] = "busy"
        row["spinner"].set_spinning(True)
        row["mark"].set_visible_child_name("busy")

    def done(self, key: str, detail: str = ""):
        row = self.rows.get(key)
        if row is None:
            return
        row["state"] = "done"
        row["spinner"].set_spinning(False)
        row["mark"].set_visible_child_name("done")
        if detail:
            row["detail"].set_label(detail)

    def succeed(self, summary: str = ""):
        """All done: every row ✓, the summary and the time, then the fade."""
        if self.ended:
            return
        for key in self.order:
            if self.rows[key]["state"] != "done":
                self.done(key)
        self._end(" · ".join(filter(None, [summary, _("done in {0}").format(elapsed_text(self.seconds()))])))
        GLib.timeout_add(HOLD_MS, self.fade)

    def fail(self, message: str, cancelled: bool = False):
        """The step under way ✗, what went wrong; the box stays until Close."""
        if self.ended:
            return
        busy = next((k for k in self.order if self.rows[k]["state"] == "busy"), None)
        if busy is not None:
            row = self.rows[busy]
            row["state"] = "failed"
            row["spinner"].set_spinning(False)
            row["mark"].set_visible_child_name("failed")
        self._end(_("Cancelled") if cancelled else message)
        if cancelled:
            GLib.timeout_add(HOLD_MS, self.fade)
        else:
            self.status.set_selectable(True)         # the message, to copy into a report
            self.close.set_visible(True)
            self.close.grab_focus()

    def seconds(self) -> float:
        return time.monotonic() - self.started

    # --- inside -------------------------------------------------------------------------------------

    def _end(self, text: str):
        self.ended = True
        self.status.set_label(text)
        self.background.set_visible(False)
        self.cancel.set_visible(False)

    def _tick(self):
        if self.gone or self.ended:
            return False
        self.status.set_label(elapsed_text(self.seconds()))
        return True

    def _cancel(self):
        self.cancel.set_sensitive(False)
        self.cancel.set_label(_("Stopping…"))
        if self.on_cancel is not None:
            self.on_cancel()

    def _to_background(self):
        if self.on_background is not None:
            self.on_background()
        self.fade()

    def fade(self, *_args):
        if self.gone:
            return False
        self.gone = True
        self.widget.add_css_class("am-splash-gone")
        self.widget.set_can_target(False)
        GLib.timeout_add(FADE_MS, self._remove)
        return False

    def _remove(self):
        parent = self.widget.get_parent()
        if isinstance(parent, Gtk.Overlay):
            parent.remove_overlay(self.widget)
        return False
