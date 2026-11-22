"""AI Agents Map: messages to a running project (Maprisma's project_messages.dart, task 096).

In the open project's Activity tab, while it plans, runs or is reviewed: a box "Message to the team"
with Send (the next assignment gets it; the agent at work keeps working; Ctrl+Enter) and Send now
(stops the agent at work and continues at once with the message, after the Continue question; not
while pausing). Under it the project's messages, newest first: waiting ones with their time and a
remove symbol (also after the run has ended), delivered ones with the agent, role and phase they
went to. The draft and the last note are kept per project by the controller."""

from __future__ import annotations

import json

from gi.repository import Gdk, Gtk

import ai_core
import project_parts as parts
import project_texts as texts
from i18n import _


class MessagesView:
    def __init__(self, ctrl):
        self.ctrl = ctrl
        self.project_id = None
        self.syncing = False
        self.shown_list = None
        self.root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP_TIGHT)
        self.root.add_css_class("am-help-card")
        self.root.append(parts.label(_("Message to the team"), ("heading",)))
        self.compose = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP_TIGHT)
        self.text = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR, accepts_tab=False, top_margin=6,
                                 bottom_margin=6, left_margin=8, right_margin=8)
        self.text.add_css_class("am-prompt")
        self.text.set_size_request(-1, 48)
        self.text.set_tooltip_text(_("Add instructions for the next step…"))
        self.text.get_buffer().connect("changed", self.on_changed)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self.on_key)
        self.text.add_controller(keys)
        self.compose.append(self.text)
        buttons = Gtk.Box(spacing=ai_core.GAP_INLINE, halign=Gtk.Align.END)
        self.send_now = Gtk.Button(label=_("Send now"))
        self.send_now.add_css_class("am-small-btn")
        self.send_now.connect("clicked", lambda *_a: self.send(now=True))
        buttons.append(self.send_now)
        self.send_button = Gtk.Button(label=_("Send"))
        self.send_button.add_css_class("am-small-btn")
        self.send_button.add_css_class("am-primary")
        self.send_button.set_tooltip_text(_("The next agent gets it; the one at work keeps working (Ctrl+Enter)."))
        self.send_button.connect("clicked", lambda *_a: self.send(now=False))
        buttons.append(self.send_button)
        self.compose.append(buttons)
        self.root.append(self.compose)
        self.progress = Gtk.ProgressBar(visible=False)
        self.root.append(self.progress)
        self.note = parts.label("", wrap=True)
        self.root.append(self.note)
        self.rule = Gtk.Separator()
        self.root.append(self.rule)
        self.list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.root.append(self.list)

    # --- state -------------------------------------------------------------------------------------

    @property
    def draft(self) -> str:
        buffer = self.text.get_buffer()
        return buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False)

    def enabled(self, now: bool) -> bool:
        project = self.ctrl.project
        idle = not self.ctrl.busy and self.ctrl.message_sending is None
        ok = texts.can_message(project) and bool(self.draft.strip()) and idle
        return ok and (not now or project.get("status") != "pausing")

    def on_changed(self, _buffer):
        if self.syncing or self.project_id is None:
            return
        self.ctrl.set_message_draft(self.project_id, self.draft)
        self.update_buttons()

    def on_key(self, _controller, keyval, _keycode, state):
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) and state & Gdk.ModifierType.CONTROL_MASK:
            self.send(now=False)
            return True
        return False

    def send(self, now: bool):
        if self.enabled(now):
            self.ctrl.send_message(self.draft, now=now)

    def update_buttons(self):
        project = self.ctrl.project or {}
        self.send_button.set_sensitive(self.enabled(False))
        self.send_now.set_sensitive(self.enabled(True))
        self.send_now.set_tooltip_text(_("The agent is stopping. Send waits for the continuation.")
                                       if project.get("status") == "pausing" else
                                       _("Stops the agent at work and continues at once with your message."))

    # --- drawing -----------------------------------------------------------------------------------

    def render(self, project):
        ctrl = self.ctrl
        pid = project.get("id") if isinstance(project, dict) else None
        messages = [m for m in (project or {}).get("messages") or [] if isinstance(m, dict)]
        note = ctrl.message_notes.get(pid) if pid else None
        can = texts.can_message(project)
        self.root.set_visible(isinstance(pid, str) and (can or bool(messages) or note is not None))
        if not isinstance(pid, str):
            self.project_id = None
            return
        # Each project its own draft; the box takes it when the project changes or the controller
        # cleared it (sent).
        stored = ctrl.message_drafts.get(pid, "")
        if pid != self.project_id or stored != self.draft:
            self.project_id = pid
            self.syncing = True
            self.text.get_buffer().set_text(stored)
            self.syncing = False
        self.compose.set_visible(can)
        self.update_buttons()
        working = ctrl.message_sending == pid and ctrl.busy and not ctrl.confirming
        self.progress.set_visible(working)
        if working:
            self.progress.pulse()
        self.note.set_visible(note is not None)
        if note is not None:
            self.note.set_label(note[0])
            if note[1]:
                self.note.add_css_class("am-project-error")
            else:
                self.note.remove_css_class("am-project-error")
        self.rule.set_visible(bool(messages) and (can or note is not None))
        key = json.dumps([messages, ctrl.busy, ctrl.message_sending], sort_keys=True, default=str)
        if key == self.shown_list:
            return
        self.shown_list = key
        parts.clear(self.list)
        idle = not ctrl.busy and ctrl.message_sending is None
        for message in reversed(messages):
            waiting = message.get("state") != "delivered"
            row = Gtk.Box(spacing=ai_core.GAP_TIGHT)
            row.append(parts.icon("alarm-symbolic" if waiting else "object-select-symbolic", 16))
            text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
            text.append(parts.label(str(message.get("text") or ""), wrap=True, selectable=True))
            text.append(parts.label(texts.message_state(message, ctrl.agent_name), ("am-project-small", "am-project-dim"),
                                    wrap=True))
            row.append(text)
            mid = str(message.get("id") or "")
            if waiting and mid:
                drop = Gtk.Button(icon_name="window-close-symbolic", tooltip_text=_("Remove message"),
                                  valign=Gtk.Align.START, sensitive=idle)
                drop.add_css_class("flat")
                parts.set_accessible(drop, _("Remove message"))
                drop.connect("clicked", lambda *_a, mid=mid: ctrl.drop_message(mid))
                row.append(drop)
            self.list.append(row)
