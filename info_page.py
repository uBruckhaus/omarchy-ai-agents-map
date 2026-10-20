"""AI Agents Map's Information page (the Omarchy plugin only; a mixin of ai_popup.AgentsMap): AI
Playground in a section of its own, then cards for the author's other Omarchy plugins (more_plugins,
made in ~/Projects/omarchy-plugin-i18n). A card opens the plugin's page in the Omarchy Plugin
Marketplace; nothing is read from the network.

When AI Playground is installed (the Omarchy plugin, or the app on PATH for other desktops), the
header shows its button and its section here starts it, with the agent selected on the map chosen
there. Its launcher must be a regular executable of this user or of root that no one else can
change.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

from gi.repository import Gtk, Pango

import ai_core
import more_plugins
from i18n import _

OWN_ID = "ubruckhaus.ai-agents-map"
PLAYGROUND_ID = "ubruckhaus.ai-playground"
PLAYGROUND_LAUNCHER = Path.home() / ".config/omarchy/plugins" / PLAYGROUND_ID / "ai-playground"
PLAYGROUND_GLYPH = "\U000F0297"


def trusted_launcher(path) -> Path | None:
    """path when it is a regular executable file of this user or of root that no one else may
    change (a link is followed only on PATH, where packages put links; else None)."""
    try:
        info = Path(path).stat()
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_uid not in (os.getuid(), 0) or info.st_mode & 0o022 \
            or not info.st_mode & 0o100:
        return None
    return Path(path)


def playground_launcher(path: Path = PLAYGROUND_LAUNCHER, on_path: bool = True) -> Path | None:
    """AI Playground's launcher when it is installed: the Omarchy plugin's (never a link), else
    `ai-playground` on PATH (the app on other desktops); None when there is none."""
    try:
        own = not Path(path).is_symlink() and trusted_launcher(path)
    except OSError:
        own = None
    if own:
        return own
    found = shutil.which("ai-playground") if on_path else None
    return trusted_launcher(found) if found else None


def playground_argv(launcher: Path, agent_command: str = "") -> list:
    """The launcher, and the agent selected on the map (AI Playground opens its chess tab with it)."""
    argv = [str(launcher)]
    if agent_command and all(c.isalnum() or c in "-_." for c in agent_command) and len(agent_command) <= 64:
        argv += ["--agent", agent_command]
    return argv


class InfoPage:
    def build_info_page(self) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP)
        header = Gtk.Box(spacing=8)
        back = Gtk.Button()
        back.add_css_class("flat")
        back.add_css_class("am-header-btn")
        back.add_css_class("am-back-bordered")
        back_box = Gtk.Box(spacing=6)
        back_box.append(Gtk.Image.new_from_icon_name("go-previous-symbolic"))
        back_box.append(Gtk.Label(label=_("Back")))
        back_box.append(Gtk.Label(label="· ⌫"))
        back.set_child(back_box)
        back.set_tooltip_text(_("Back (Backspace, Esc)"))
        back.connect("clicked", lambda *_a: self.close_info())
        header.append(back)
        title = Gtk.Label(label=_(more_plugins.INFORMATION).upper(), xalign=0, hexpand=True)
        title.add_css_class("am-section-title")
        header.append(title)
        page.append(header)
        rule = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        rule.add_css_class("am-settings-rule")
        page.append(rule)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP)
        body.add_css_class("am-settings")
        body.append(self.playground_section())
        body.append(more_plugins.build_cards(OWN_ID, "am", leave_out=(PLAYGROUND_ID,)))
        page.append(self.page_scroller(body))
        return page

    def playground_section(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        heading = Gtk.Label(label=_("Made to go with AI Playground").upper(), xalign=0)
        heading.add_css_class("am-more-heading")
        box.append(heading)
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        card.add_css_class("am-help-card")
        top = Gtk.Box(spacing=10)
        glyph = Gtk.Label(label=PLAYGROUND_GLYPH, valign=Gtk.Align.START)
        glyph.add_css_class("am-more-icon")
        top.append(glyph)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, hexpand=True)
        name = Gtk.Label(label="AI Playground", xalign=0)
        name.add_css_class("am-more-name")
        texts.append(name)
        for text in (_("Try out the agents you set up here, a test per tab: chess against you or against each "
                       "other, speed and cost side by side, two answers judged blind, pictures, screenshots and "
                       "video frames, and your local LLMs with tokens per second."),
                     _("It asks the agents that are ready here (signed in, or connected to a local LLM): "
                       "nothing to set up twice.")):
            line = Gtk.Label(label=text, xalign=0, wrap=True)
            line.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
            line.add_css_class("am-chess-said")
            texts.append(line)
        top.append(texts)
        card.append(top)
        launcher = playground_launcher()
        if launcher is not None:
            button = Gtk.Button(label=_("Start AI Playground"), halign=Gtk.Align.START)
            button.set_tooltip_text(_("With the agent selected on the map chosen there"))
            button.connect("clicked", lambda *_a: self.start_playground())
        else:
            button = Gtk.Button(label=_("Open AI Playground in the Omarchy Plugin Marketplace"),
                                halign=Gtk.Align.START)
            button.connect("clicked", lambda *_a: more_plugins.open_page(PLAYGROUND_ID))
        button.add_css_class("am-small-btn")
        card.append(button)
        box.append(card)
        return box

    def start_playground(self):
        launcher = playground_launcher()
        if launcher is None:
            return
        selected = next((i.partition(":")[2] for i in getattr(self, "selection", []) if i.startswith("agent:")), "")
        try:
            subprocess.Popen(playground_argv(launcher, selected), stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError:
            pass

    def mark_playground(self):
        """The header's AI Playground button: only when it is installed; its tip names the selected
        agent."""
        button = getattr(self, "playground_button", None)
        if button is None:
            return
        installed = playground_launcher() is not None
        button.set_visible(installed)
        if installed:
            agent = self.selected_agent_name()
            button.set_tooltip_text(_("AI Playground: test {0} (chess, speed, vision…)").format(agent) if agent
                                    else _("AI Playground: test your agents (chess, speed, vision…)"))

    def selected_agent_name(self) -> str:
        command = next((i.partition(":")[2] for i in getattr(self, "selection", []) if i.startswith("agent:")), "")
        return next((a.name for a in getattr(self, "agent_list", []) if a.command == command), "")

    def open_info(self):
        if self.page_name() not in ("settings", "help", "info"):
            self.remember_page()
        self.show_page("info")

    def close_info(self):
        self.back_to_last_page()
