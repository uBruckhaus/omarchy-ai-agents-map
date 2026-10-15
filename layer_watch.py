"""Close the popup when one of Omarchy's own menus opens.

The popup is a keyboard layer on top of everything. While it is open, a menu
started by a shortcut (Super + Space: Omarchy's app menu) opens underneath it
without the keyboard, so it seems to need several presses. Hyprland reports
every new layer on its event socket ("openlayer>>NAME"); while the popup is
open we listen there and step aside for menus and panels that want the
keyboard. Notifications, the OSD, the bar and the password dialog are not
in the list, so they never close it.
"""
import os
import re
from pathlib import Path

from gi.repository import Gio, GLib

# Layers that take the keyboard: Omarchy's menus and panels, Walker, and the
# other popup of this family (each closes the other).
MENU_LAYERS = frozenset({
    "omarchy-menu", "omarchy-keyboard-panel", "omarchy-emojis", "omarchy-clipboard",
    "omarchy-image-selector", "omarchy-network-qr", "walker",
    "theme-manager", "simple-launcher-for-fools", "ai-agents-map",
})
MAX_PENDING = 64 * 1024   # a line longer than this is not a Hyprland event


def event_socket():
    """Hyprland's event socket in our runtime folder, or None."""
    runtime = os.environ.get("XDG_RUNTIME_DIR", "")
    signature = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE", "")
    if not os.path.isabs(runtime) or not re.fullmatch(r"[A-Za-z0-9_]{1,200}", signature):
        return None
    path = Path(runtime) / "hypr" / signature / ".socket2.sock"
    return path if path.is_socket() else None


def opened_menus(chunk, pending, own):
    """Feed socket bytes; returns (menu layers opened, bytes kept for the
    next chunk). Pure, for tests."""
    data = pending + chunk
    if len(data) > MAX_PENDING:
        data = data[-MAX_PENDING:].split(b"\n", 1)[-1] if b"\n" in data[-MAX_PENDING:] else b""
    *lines, rest = data.split(b"\n")
    found = []
    for line in lines:
        if line.startswith(b"openlayer>>"):
            name = line[len(b"openlayer>>"):].decode("utf-8", "replace").strip()
            if name in MENU_LAYERS and name != own:
                found.append(name)
    return found, rest


class LayerWatch:
    """Calls on_menu() when a menu layer opens, between start() and stop()."""

    def __init__(self, own_namespace, on_menu):
        self.own = own_namespace
        self.on_menu = on_menu
        self.connection = None
        self.cancellable = None
        self.pending = b""

    def start(self):
        if self.connection is not None:
            return
        path = event_socket()
        if path is None:
            return
        try:
            self.connection = Gio.SocketClient().connect(Gio.UnixSocketAddress.new(str(path)), None)
        except GLib.Error:
            self.connection = None
            return
        self.cancellable = Gio.Cancellable()
        self.pending = b""
        self._read()

    def stop(self):
        if self.cancellable is not None:
            self.cancellable.cancel()
        if self.connection is not None:
            try:
                self.connection.close(None)
            except GLib.Error:
                pass
        self.connection = self.cancellable = None
        self.pending = b""

    def _read(self):
        connection, cancellable = self.connection, self.cancellable
        if connection is None:
            return
        connection.get_input_stream().read_bytes_async(
            4096, GLib.PRIORITY_DEFAULT, cancellable, self._got, (connection, cancellable))

    def _got(self, stream, result, owner):
        try:
            data = stream.read_bytes_finish(result).get_data()
        except GLib.Error:
            return                      # cancelled by stop(), or the socket went away
        if owner[0] is not self.connection:
            return                      # a read from before the last stop()
        if not data:
            self.stop()                 # Hyprland closed the socket
            return
        found, self.pending = opened_menus(data, self.pending, self.own)
        if found:
            self.on_menu(found[0])
        if self.connection is owner[0]:
            self._read()
