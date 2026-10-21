"""Text fields that take text from other clients only through bounded reads (a mixin of the app
window: AI Agents Map / Asterism). GTK's own paste, middle-click paste and text drops buffer the
whole offer in memory with no limit or deadline; these go through ai_core.clipboard_text
(wl-paste, byte cap, deadline) instead. The window shows notes with its show_note."""

from __future__ import annotations

import threading

from gi.repository import Gdk, GLib, Gtk

import ai_core
from i18n import _


class TextGuard:
    def guard_text_field(self, field):
        """Route every way text can enter a field from another client through a
        bounded read. GTK's own paste, middle-click paste and text drops buffer
        the whole offer in memory with no limit or deadline (as in Theme
        Manager). `field`: a Gtk.Entry (or its Gtk.Text) or a Gtk.TextView."""
        text = field.get_delegate() if isinstance(field, Gtk.Entry) else field
        text = text or field
        if getattr(text, "_am_guarded", False):
            return
        text._am_guarded = True
        if isinstance(text, Gtk.Text) and text.get_max_length() == 0:
            text.set_max_length(4096)
        # Ctrl+V, Shift+Insert and the context menu's Paste all emit this.
        text.connect("paste-clipboard", self.on_field_paste, field)
        middle = Gtk.GestureClick(button=Gdk.BUTTON_MIDDLE)
        middle.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        middle.connect("pressed", lambda gesture, *_: gesture.set_state(Gtk.EventSequenceState.CLAIMED))
        field.add_controller(middle)
        controllers = text.observe_controllers()
        for i in reversed(range(controllers.get_n_items())):
            controller = controllers.get_item(i)
            if isinstance(controller, (Gtk.DropTarget, Gtk.DropTargetAsync)):
                text.remove_controller(controller)

    def guard_text_fields_in(self, root):
        guarded = 0
        stack = [root]
        while stack:
            widget = stack.pop()
            if isinstance(widget, (Gtk.Text, Gtk.TextView)):
                self.guard_text_field(widget)
                guarded += 1
                continue
            child = widget.get_first_child()
            while child is not None:
                stack.append(child)
                child = child.get_next_sibling()
        return guarded

    def on_field_paste(self, text, field):
        text.stop_emission_by_name("paste-clipboard")
        if isinstance(field, Gtk.TextView) and not field.get_editable():
            return
        lines = isinstance(field, Gtk.TextView)

        def worker():
            pasted = ai_core.clipboard_text(lines=lines)
            GLib.idle_add(self.insert_pasted, field, pasted)

        threading.Thread(target=worker, daemon=True).start()

    def insert_pasted(self, field, pasted):
        if pasted is None:
            self.show_note(_("Clipboard text is too long to paste (max {0} KiB)").format(ai_core.MAX_PASTE_BYTES // 1024))
        elif pasted and isinstance(field, Gtk.TextView):
            buffer = field.get_buffer()
            buffer.delete_selection(True, True)
            buffer.insert_at_cursor(pasted)
        elif pasted:
            text = field.get_delegate() if isinstance(field, Gtk.Entry) else field
            text = text or field
            text.delete_selection()
            position = text.insert_text(pasted, text.get_position())
            text.set_position(position)
        return GLib.SOURCE_REMOVE
