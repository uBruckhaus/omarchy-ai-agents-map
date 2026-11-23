"""AI Agents Map: the folder dialog with the mounted disks in its sidebar (Maprisma's folder_picker.dart).

Every "Choose folder" opens GTK's own folder dialog, whose sidebar also lists every mounted data disk
(/mnt/Data, /mnt/Games, a USB stick, a network share) and the folders pinned in the desktop's file
manager (COSMIC Files favorites, KDE Places). Gtk.FileDialog cannot add sidebar entries, and a portal
dialog drops them, so this is Gtk.FileChooserDialog (deprecated in GTK 4.10, still in GTK 4)."""

from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

import warnings

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gio, Gtk  # noqa: E402

from i18n import _  # noqa: E402

# Where data disks are mounted (fstab under /mnt, udisks under /run/media).
_MOUNT_ROOTS = ("/mnt/", "/media/", "/run/media/", "/srv/")
# Mounts that hold data besides disks (/dev/...): network shares and pools.
_NETWORK = frozenset({"nfs", "nfs4", "cifs", "smb3", "fuse.sshfs", "zfs", "9p"})
_ENTITIES = {"amp": "&", "lt": "<", "gt": ">", "quot": '"', "apos": "'"}


def _xml_text(text: str) -> str:
    def one(m):
        name = m.group(1)
        if name.startswith("#"):
            code = int(name[2:], 16) if name.startswith("#x") else int(name[1:])
            return chr(code) if 0 < code < 0x110000 else m.group(0)    # out of range: left as written
        return _ENTITIES[name]
    return re.sub(r"&(#x[0-9a-fA-F]+|#[0-9]+|amp|lt|gt|quot|apos);", one, text)


def folder_shortcuts(mounts: str, home: str, cosmic_favorites: str | None = None, kde_places: str | None = None,
                     is_dir=None, is_system_root=None) -> list[str]:
    """The extra sidebar entries: mounted data disks first, then pinned folders, in this order, once
    each, without the home folder or what is gone. GTK's own bookmarks the dialog shows by itself.
    `is_system_root`: whether a mount is another system's root (the Omarchy disk opened at
    /mnt/omarchy-root), which, with every mount inside it, is no place to start a project."""
    exists = is_dir or os.path.isdir
    system = is_system_root or (lambda _path: False)
    found: list[str] = []

    def trim(path: str) -> str:
        while len(path) > 1 and path.endswith("/"):
            path = path[:-1]
        return path
    home = trim(home)

    def add(path: str):
        clean = trim(path)
        if not clean.startswith("/") or clean in (home, "/") or clean in found or not exists(clean):
            return
        found.append(clean)

    roots: list[str] = []
    for line in mounts.split("\n"):
        fields = line.split(" ")
        if len(fields) < 3 or not (fields[0].startswith("/dev/") or fields[2] in _NETWORK):
            continue
        # /proc/self/mounts writes a space as \040 (and tab, newline, \ alike).
        target = trim(re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), fields[1]))
        if not target.startswith(_MOUNT_ROOTS) or any(target.startswith(root + "/") for root in roots):
            continue
        if system(target):
            roots.append(target)
            continue
        add(target)
    for m in re.finditer(r'Path\("((?:[^"\\]|\\.)*)"\)', cosmic_favorites or ""):
        add(m.group(1).replace('\\"', '"').replace("\\\\", "\\"))
    # Each <bookmark>; one the user hid in KDE stays hidden.
    for m in re.finditer(r"<bookmark\b([^>]*)>(.*?)</bookmark>", kde_places or "", re.S):
        if re.search(r"<isHidden>\s*true\s*</isHidden>", m.group(2)):
            continue
        href = re.search(r'href="(file://[^"]+)"', m.group(1))
        if href is None:
            continue
        try:
            url = urlsplit(_xml_text(href.group(1)))
        except ValueError:
            continue                       # not a usable local path (a broken bookmark)
        if url.netloc not in ("", "localhost"):
            continue
        add(unquote(url.path))
    return found


def _read(path: str) -> str | None:
    try:
        return Path(path).read_text(errors="replace")
    except OSError:
        return None


def current_folder_shortcuts() -> list[str]:
    """folder_shortcuts of this PC."""
    home = os.environ.get("HOME", "")

    def xdg(name: str, fallback: str) -> str:
        value = os.environ.get(name, "")
        return value if value.startswith("/") else fallback     # empty counts as unset (XDG spec)
    config, data = xdg("XDG_CONFIG_HOME", f"{home}/.config"), xdg("XDG_DATA_HOME", f"{home}/.local/share")
    return folder_shortcuts(_read("/proc/self/mounts") or "", home,
                            _read(f"{config}/cosmic/com.system76.CosmicFiles/v1/favorites"),
                            _read(f"{data}/user-places.xbel"),
                            is_system_root=lambda p: os.path.isdir(f"{p}/etc") and os.path.isdir(f"{p}/usr"))


# The open folder dialog: one at a time (a second "Choose folder" brings it to the front).
_open = None


def choose_folder(parent, title: str, initial, done):
    """Open the folder dialog over `parent`; done(path) when a folder is chosen (not when cancelled).
    It closes without an answer when its window hides or closes (the bar toggle, Esc on a page that
    goes), so a choice never lands in a page that is gone."""
    global _open
    if _open is not None:
        _open.present()
        return _open
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        dialog = Gtk.FileChooserDialog(title=title, transient_for=parent, modal=True, destroy_with_parent=True,
                                       action=Gtk.FileChooserAction.SELECT_FOLDER)
        dialog.add_button(_("Cancel"), Gtk.ResponseType.CANCEL)
        dialog.add_button(_("Select"), Gtk.ResponseType.ACCEPT)
        dialog.set_default_response(Gtk.ResponseType.ACCEPT)
        for path in current_folder_shortcuts():
            try:
                dialog.add_shortcut_folder(Gio.File.new_for_path(path))
            except Exception:              # GLib.Error: a folder GTK already lists; that is fine
                pass
        start = Path(os.path.expanduser(str(initial))) if initial else None
        if start is not None and start.is_dir():
            try:
                dialog.set_current_folder(Gio.File.new_for_path(str(start)))
            except Exception:
                pass
    watched = []

    def close(answer_path):
        global _open
        if _open is not dialog:
            return
        _open = None
        for widget, handler in watched:
            widget.disconnect(handler)
        dialog.destroy()
        if answer_path:
            done(answer_path)

    def response(widget, answer):
        path = None
        if answer == Gtk.ResponseType.ACCEPT:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                chosen = widget.get_file()
            path = chosen.get_path() if chosen is not None else None
        close(path)
    dialog.connect("response", response)
    if parent is not None:
        for signal in ("hide", "destroy"):
            watched.append((parent, parent.connect(signal, lambda *_a: close(None))))
    _open = dialog
    dialog.present()
    return dialog
