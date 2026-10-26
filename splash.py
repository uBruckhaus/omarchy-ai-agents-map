"""The first open's splash: an overlay over the window's content while the app looks at what is on
this PC (agents and local LLMs at once, MCP servers and skills in the background scan). Each kind
slides in with a spinner that turns into ✓ and its number when that look is done; then the overlay
fades out. A click dismisses it at once; it never stays longer than MAX_SECONDS."""

from __future__ import annotations

import time

from gi.repository import GLib, Gtk

from i18n import _

STEP_MS = 180          # one row after the other slides in
FADE_MS = 450          # as the CSS transition of .am-splash
HOLD_MS = 900          # the finished list stays this long before it fades
MIN_MS = 1100          # shown at least this long (every row in, the spinners seen)
MAX_SECONDS = 15


class Splash:
    def __init__(self, title: str, icon_name: str, rows):
        """rows: [(key, name)] in the order they are shown."""
        self.gone = False
        self.finished = False
        self.started = time.monotonic()
        self.widget = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, halign=Gtk.Align.FILL,
                              valign=Gtk.Align.FILL, hexpand=True, vexpand=True)
        self.widget.add_css_class("am-splash")
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, halign=Gtk.Align.CENTER,
                        valign=Gtk.Align.CENTER, vexpand=True)
        icon = Gtk.Image.new_from_icon_name(icon_name)
        icon.set_pixel_size(88)
        icon.add_css_class("am-splash-icon")
        inner.append(icon)
        name = Gtk.Label(label=title)
        name.add_css_class("am-splash-title")
        inner.append(name)
        self.status = Gtk.Label(label=_("Looking at what is on this PC…"), wrap=True, max_width_chars=44,
                                justify=Gtk.Justification.CENTER)
        self.status.add_css_class("am-agent-detail")
        inner.append(self.status)
        self.bar = Gtk.ProgressBar(fraction=0.0)
        self.bar.add_css_class("am-splash-bar")
        inner.append(self.bar)
        listing = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, width_request=300)
        self.rows = {}
        for key, label in rows:
            line = Gtk.Box(spacing=10)
            spinner = Gtk.Spinner(spinning=True)
            mark = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, transition_duration=250)
            mark.add_named(spinner, "busy")
            tick = Gtk.Label(label="✓")
            tick.add_css_class("am-splash-tick")
            mark.add_named(tick, "done")
            line.append(mark)
            text = Gtk.Label(label=label, xalign=0, hexpand=True)
            text.add_css_class("am-splash-row")
            line.append(text)
            count = Gtk.Label(label="", xalign=1)
            count.add_css_class("am-splash-count")
            line.append(count)
            reveal = Gtk.Revealer(child=line, transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN,
                                  transition_duration=260, reveal_child=False)
            listing.append(reveal)
            self.rows[key] = {"reveal": reveal, "mark": mark, "spinner": spinner, "count": count, "done": False}
        inner.append(listing)
        self.widget.append(inner)
        click = Gtk.GestureClick()
        click.connect("released", lambda *_a: self.fade())
        self.widget.add_controller(click)
        for index, key in enumerate(self.rows):
            GLib.timeout_add(STEP_MS * (index + 1), self._reveal, key)
        GLib.timeout_add_seconds(MAX_SECONDS, self.fade)

    def _reveal(self, key: str):
        if not self.gone:
            self.rows[key]["reveal"].set_reveal_child(True)
        return False

    def step(self, key: str, count: int | None = None, detail: str = ""):
        """`key`'s look is done: ✓ and its number (and a detail: "3 not connected")."""
        row = self.rows.get(key)
        if row is None or self.gone:
            return
        row["done"] = True
        row["spinner"].set_spinning(False)
        row["mark"].set_visible_child_name("done")
        text = "" if count is None else str(count)
        row["count"].set_label(f"{text} · {detail}" if detail and text else text or detail)
        row["reveal"].set_reveal_child(True)
        done = sum(1 for r in self.rows.values() if r["done"])
        self.bar.set_fraction(done / max(1, len(self.rows)))

    def finish(self, status: str = ""):
        """Every look is done: the closing line, then the fade."""
        if self.gone or self.finished:
            return
        self.finished = True
        GLib.timeout_add(max(0, MIN_MS - int((time.monotonic() - self.started) * 1000)), self._close, status)

    def _close(self, status: str):
        if self.gone:
            return False
        for key, row in self.rows.items():
            if not row["done"]:
                self.step(key)
        self.status.set_label(status or _("Done"))
        GLib.timeout_add(HOLD_MS, self.fade)
        return False

    def fade(self, *_args):
        if self.gone:
            return False
        self.gone = True
        self.widget.add_css_class("am-splash-gone")
        self.widget.set_can_target(False)          # clicks go through while it fades
        GLib.timeout_add(FADE_MS, self._remove)
        return False

    def _remove(self):
        parent = self.widget.get_parent()
        if isinstance(parent, Gtk.Overlay):
            parent.remove_overlay(self.widget)
        return False
