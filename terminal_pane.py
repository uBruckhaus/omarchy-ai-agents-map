"""An agent's own terminal inside the window (right of the cards, like a details page).

The terminal is VTE for GTK4 (the `vte4` package). Without it, Start opens the agent
in the user's terminal instead (desktop.open_terminal: `xdg-terminal-exec` on Omarchy).

The agent runs as a child of this window's process, in the folder the user picked,
with this process's environment plus TERM/COLORTERM. Its argv is the agent's own
program found on PATH; nothing typed or read from a file becomes part of it.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, GLib, Gtk, Pango  # noqa: E402

from ai_core import GAP  # noqa: E402
from i18n import _  # noqa: E402

_VTE = None

# Agents that are programs with a window of their own, not a terminal program.
FONT = "JetBrainsMono Nerd Font, Symbols Nerd Font, monospace 11"
# The 16 terminal colours: color0…color15 when the theme has them, else Omarchy's names.
ANSI = (("dark_background", "#1e1e2e"), ("red", "#f38ba8"), ("green", "#a6e3a1"), ("yellow", "#f9e2af"),
        ("blue", "#89b4fa"), ("magenta", "#f5c2e7"), ("cyan", "#94e2d5"), ("light_foreground", "#bac2de"),
        ("muted", "#585b70"), ("bright_red", "#f38ba8"), ("bright_green", "#a6e3a1"),
        ("bright_yellow", "#f9e2af"), ("bright_blue", "#89b4fa"), ("bright_magenta", "#f5c2e7"),
        ("bright_cyan", "#94e2d5"), ("bright_foreground", "#a6adc8"))


def theme_colors(path: Path | None = None) -> dict:
    """The desktop theme's colours that are colours ("#rrggbb"); path: another theme file (tests)."""
    import ai_core
    import desktop
    data = desktop.theme_colors(path) if path is not None else ai_core.look_colors()
    return {k: v for k, v in data.items() if isinstance(v, str) and v.startswith("#") and len(v) <= 9}


def vte():
    """The Vte module (GTK4 build), or None when vte4 is not installed. A miss is
    asked again next time, so installing vte4 needs no restart."""
    global _VTE
    if _VTE is None:
        try:
            gi.require_version("Vte", "3.91")
            from gi.repository import Vte
            _VTE = Vte
        except (ValueError, ImportError):
            _VTE = None
    return _VTE


def program(command: str) -> str | None:
    """The agent's program on PATH, or None (a GUI app, or not installed)."""
    import harnesses
    harness = harnesses.get(command)
    if harness is not None and harness.chat_app:       # a chat app, no program for a terminal
        return None
    return shutil.which(command)


def start_argv(command: str, path: str) -> list[str]:
    """The agent's program with the arguments its adapter starts it with (start = […])."""
    import harnesses
    harness = harnesses.get(command)
    return [path, *(harness.start if harness is not None else ())]


def child_env(environ=None) -> list[str]:
    import ai_core
    env = dict(os.environ if environ is None else environ)
    ai_core.drop_agent_session_vars(env)      # an agent here is a session of its own, not a child of another
    env.update({"TERM": "xterm-256color", "COLORTERM": "truecolor"})
    env.pop("AI_AGENTS_MAP_PAGE", None)
    env.pop("AI_AGENTS_MAP_VIEW", None)
    return [f"{k}={v}" for k, v in env.items() if "\0" not in k + v]


def open_outside(path: str, cwd: str, command: str = "") -> bool:
    """No vte4: the agent in the user's own terminal (a new window)."""
    import desktop
    return desktop.open_terminal(start_argv(command, path) if command else [path], cwd)


class TerminalPane(Gtk.Box):
    """Header (Back, title, state, Stop/Start again) over the agent's terminal."""

    def __init__(self, agent, cwd: str, colors: dict, on_back, on_state, argv: list | None = None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=GAP)
        Vte = vte()
        self.agent = agent
        self.argv = list(argv) if argv else None   # a fixed command (an install) instead of the agent
        self.cwd = cwd
        self.on_state = on_state          # called when it starts or ends
        self.pid = None
        self.leader_fd = None             # pidfd of the agent, taken when it started
        self.stopping = {}                # launch (pid, start time) -> process_group.Group while its Stop escalates
        self.running = False
        self.started = 0.0
        self.exit_code = None

        header = Gtk.Box(spacing=8)
        from page_parts import PageParts       # here: gi's versions are set by then
        back = PageParts.back_button(lambda: on_back(), _("Back to the map; the agent keeps running here"), shortcut="")
        header.append(back)
        title = Gtk.Label(label=agent.name.upper(), xalign=0)
        title.add_css_class("am-section-title")
        header.append(title)
        self.state_label = Gtk.Label(xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.MIDDLE)
        self.state_label.add_css_class("am-agent-detail")
        header.append(self.state_label)
        self.again = Gtk.Button(label=_("Start again"))
        self.again.add_css_class("am-small-btn")
        self.again.connect("clicked", lambda *_: self.start())
        header.append(self.again)
        self.stop_button = Gtk.Button(label=_("Stop"))
        self.stop_button.add_css_class("am-small-btn")
        self.stop_button.set_tooltip_text(_("End the agent (as closing its terminal would)"))
        self.stop_button.connect("clicked", lambda *_: self.stop())
        header.append(self.stop_button)
        self.append(header)

        self.terminal = Vte.Terminal()
        self.terminal.set_vexpand(True)
        self.terminal.set_hexpand(True)
        self.terminal.set_font(Pango.FontDescription.from_string(FONT))
        self.terminal.set_scrollback_lines(10000)
        self.terminal.set_mouse_autohide(True)
        self.terminal.add_css_class("am-terminal")
        self.apply_colors(colors)
        self.terminal.connect("child-exited", self.on_exited)
        # VTE's own paste reads the whole clipboard offer (no size limit, no deadline): every
        # paste, also the native action, goes through the bounded read instead.
        self.terminal.connect("paste-clipboard", self.on_native_paste)
        self.pasting = False
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self.on_key)
        self.terminal.add_controller(keys)
        frame = Gtk.Frame()
        frame.add_css_class("am-terminal-frame")
        frame.set_child(self.terminal)
        self.append(frame)
        self.start()

    # --- the process ------------------------------------------------------------------------

    def start(self):
        Vte = vte()
        path = self.argv[0] if self.argv else program(self.agent.command)
        if path is None:
            self.set_state(False, _("{0} was not found").format(self.agent.command))
            return
        self.terminal.reset(True, True)
        self.exit_code = None
        self.started = time.monotonic()
        self.set_state(True, _("starting…"))
        self.terminal.spawn_async(Vte.PtyFlags.DEFAULT, self.cwd, self.argv or start_argv(self.agent.command, path), child_env(),
                                  GLib.SpawnFlags.DEFAULT, None, None, -1, None, self.on_spawned)

    def on_spawned(self, _terminal, pid, error, *_rest):
        if error is not None or pid is None or pid <= 0:
            self.set_state(False, _("could not start: {0}").format(getattr(error, "message", "") or "?"))
            return
        self.pid = pid
        self.close_leader_fd()
        try:
            self.leader_fd = os.pidfd_open(pid)      # VTE reaps it; the pidfd keeps it ours
        except OSError:
            self.leader_fd = None
        self.set_state(True, _("running in {0}").format(self.short_cwd()))
        self.terminal.grab_focus()

    def on_exited(self, _terminal, status):
        self.pid = None
        self.close_leader_fd()          # a Stop in progress holds its own copy (self.stopping)
        code = os.waitstatus_to_exitcode(status) if status >= 0 else status
        self.exit_code = code
        self.set_state(False, _("ended") + (f" ({code})" if code else ""))

    def transcript(self, most: int = 256 * 1024) -> str:
        """What the terminal shows and keeps (its scrollback), as plain text, the end of it at most."""
        Vte = vte()
        try:
            row, _col = self.terminal.get_cursor_position()
            start = max(0, row - self.terminal.get_scrollback_lines())
            text = self.terminal.get_text_range_format(Vte.Format.TEXT, start, 0, row, self.terminal.get_column_count())
            text = text[0] if isinstance(text, tuple) else text
        except (AttributeError, TypeError, ValueError):
            return ""
        text = (text or "").rstrip()
        return text[-most:]

    def stop(self):
        """End the agent and what it started (its MCP servers, tools): SIGHUP to its process
        group, as a terminal closing would; SIGTERM after 3 s and SIGKILL after 6 s to whatever
        of the group is still there, also when the agent itself ended on SIGHUP and VTE reaped
        it. The members are held by pidfds (process_group.Group), so a reused pid is never hit."""
        if self.pid is None or not self.running or self.leader_fd is None:
            return
        import process_group
        launch = (self.pid, self.started)            # per launch: Start again → Stop is its own
        group = self.stopping.get(launch)
        if group is None:
            group = self.stopping[launch] = process_group.Group(self.pid, os.dup(self.leader_fd))
        group.signal(signal.SIGHUP)
        # While the stop escalates, the group is looked through often (Group.refresh: only while a
        # held member still anchors it), so what the agent starts on SIGHUP is held as well.
        GLib.timeout_add(150, self.watch_stop, group)
        GLib.timeout_add(3000, self.force_stop, launch, group, signal.SIGTERM)
        GLib.timeout_add(6000, self.force_stop, launch, group, signal.SIGKILL)
        self.set_state(True, _("stopping…"))

    def watch_stop(self, group) -> bool:
        if not group.fds:                       # closed: the stop is over
            return False
        group.refresh()
        return group.alive()

    def force_stop(self, launch, group, sig) -> bool:
        group.signal(sig)
        if sig == signal.SIGKILL or not group.alive():
            group.close()
            if self.stopping.get(launch) is group:
                del self.stopping[launch]
        return False

    def stop_now(self):
        """When AI Agents Map ends: the agent and its group get SIGHUP and SIGTERM, a second to
        end, then SIGKILL to what is left (also a Stop still escalating)."""
        import process_group
        groups = list(self.stopping.values())     # every stop still escalating, and the agent running now
        if self.pid is not None and self.running and self.leader_fd is not None \
                and (self.pid, self.started) not in self.stopping:
            groups.append(process_group.Group(self.pid, os.dup(self.leader_fd)))
        if not groups:
            return
        for group in groups:
            group.signal(signal.SIGHUP)
            group.signal(signal.SIGTERM)
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and any(group.alive() for group in groups):
            time.sleep(0.05)
        for group in groups:
            group.signal(signal.SIGKILL)
            group.close()
        self.stopping.clear()

    def close_leader_fd(self):
        if self.leader_fd is not None:
            try:
                os.close(self.leader_fd)
            except OSError:
                pass
            self.leader_fd = None

    def set_state(self, running: bool, text: str):
        self.running = running
        self.state_label.set_label(text)
        self.again.set_visible(not running)
        self.stop_button.set_visible(running)
        if self.on_state:
            self.on_state(self)

    def short_cwd(self) -> str:
        home = str(Path.home())
        return "~" + self.cwd[len(home):] if self.cwd.startswith(home) else self.cwd

    # --- keys and look ---------------------------------------------------------------------

    def on_key(self, _controller, keyval, _keycode, state):
        ctrl_shift = Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK
        if state & ctrl_shift == ctrl_shift:
            if keyval in (Gdk.KEY_C, Gdk.KEY_c):
                self.terminal.copy_clipboard_format(vte().Format.TEXT)
                return True
            if keyval in (Gdk.KEY_V, Gdk.KEY_v):
                self.bounded_paste()
                return True
        return False

    def on_native_paste(self, terminal):
        terminal.stop_emission_by_name("paste-clipboard")
        self.bounded_paste()

    def bounded_paste(self):
        """The clipboard's text read with a byte cap and a deadline (ai_core.clipboard_text, on a
        worker), then handed to VTE with paste_text (its bracketed paste stays). One at a time:
        a paste while one is being read is dropped."""
        import threading
        import ai_core
        if self.pasting:
            return
        self.pasting = True

        def worker():
            text = ai_core.clipboard_text(max_bytes=ai_core.MAX_TERMINAL_PASTE, terminal=True)
            GLib.idle_add(self.finish_paste, text)

        threading.Thread(target=worker, daemon=True).start()

    def finish_paste(self, text):
        import ai_core
        self.pasting = False
        if text is None:
            self.state_label.set_label(_("Clipboard text is too long to paste (max {0} KiB)")
                                       .format(ai_core.MAX_TERMINAL_PASTE // 1024))
        elif text and self.running:
            self.terminal.paste_text(text)
        return GLib.SOURCE_REMOVE

    def apply_colors(self, colors: dict):
        theme = {**theme_colors(), **{k: v for k, v in colors.items() if k in ("background", "foreground", "accent")}}

        def rgba(value, fallback):
            color = Gdk.RGBA()
            if not color.parse(value or fallback):
                color.parse(fallback)
            return color
        palette = [rgba(theme.get(f"color{i}") or theme.get(name), fallback)
                   for i, (name, fallback) in enumerate(ANSI)]
        self.terminal.set_colors(rgba(theme.get("foreground"), "#cdd6f4"), rgba(theme.get("background"), "#1e1e2e"),
                                 palette)
        self.terminal.set_color_cursor(rgba(theme.get("cursor") or theme.get("accent"), "#89b4fa"))
