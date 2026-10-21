"""AI Agents Map: Configure… on an agent's or local LLM's box (a mixin of ai_popup.AgentsMap, split
off to keep ai_popup.py small; its methods use the window's state through self).

Its own setup (configure_ctl) in a terminal tab of the window, or its app window; when the tab
ends, the map looks again, and a local LLM that is running is offered a restart, so it reads what
was changed."""

from gi.repository import Adw, Gdk

import configure_ctl
import runtime_ctl
from i18n import _


class ConfigureMenu:
    def configure_entries(self, kind: str, ident: str) -> list:
        if kind == "agent":
            agent = next((a for a in self.agent_list if a.command == ident), None)
            return configure_ctl.for_agent(ident) if agent is not None and agent.installed else []
        if kind == "runtime":
            rt = next((r for r in self.inventory.runtimes if r.key == ident), None)
            return configure_ctl.for_runtime(ident) if rt is not None and rt.installed else []
        return []

    def configure_menu_item(self, kind: str, ident: str, popover):
        """The menu line, when there is something to configure; else None."""
        if not self.configure_entries(kind, ident):
            return None
        return self.menu_item(_("Configure…"), popover, lambda: self.offer_configure(kind, ident),
                              _("Its own setup, in a terminal here; the map looks again when it ends"))

    def offer_configure(self, kind: str, ident: str):
        entries = self.configure_entries(kind, ident)
        if not entries:
            return
        name = self.item_name(f"{kind}:{ident}")
        body = _("Its own setup runs in a terminal tab here. When it ends, the map shows what changed.")
        dialog = Adw.AlertDialog(heading=_("Configure {0}").format(name), body=body)
        dialog.set_prefer_wide_layout(False)
        dialog.add_response("cancel", _("Cancel"))
        for number, entry in enumerate(entries):
            dialog.add_response(f"e{number}", _(entry.label))
        dialog.set_close_response("cancel")

        def answered(_d, response):
            if response.startswith("e"):
                self.run_configure(kind, ident, name, entries[int(response[1:])])
        dialog.connect("response", answered)
        dialog.present(self.window)

    def run_configure(self, kind: str, ident: str, name: str, entry):
        import desktop
        import terminal_pane
        if entry.window:
            if not desktop.launch_app(list(entry.argv)):
                self.toast(_("{0} could not be opened.").format(_(entry.label)), 5)
            return

        def ended():
            self.collect()
            self.refresh_overview()
            self.refresh_map()
            self.refresh_setup()
            if kind == "runtime":
                self.offer_restart(ident)
        if terminal_pane.vte() is None:
            if desktop.run_in_terminal(list(entry.argv)):
                self.toast(_("{0} runs in a terminal window; the map shows its changes when you open it "
                             "again.").format(name), 6)
            else:
                Gdk.Display.get_default().get_clipboard().set(" ".join(entry.argv))
                self.toast(_("No terminal could be opened; the command is copied."), 6)
            return
        done = _("Done. You can close this tab (Back).")
        title = _(entry.label) + f" · {name}"
        self.open_command_tab(f"configure:{kind}:{ident}", title, configure_ctl.script(entry, done),
                              self.back_to_here(ident), on_end=ended)

    def offer_restart(self, key: str):
        """A running local LLM reads its settings when it starts: offer to restart it."""
        rt = next((r for r in self.inventory.runtimes if r.key == key), None)
        if rt is None or not rt.running or not runtime_ctl.can_control(rt):
            return

        def restart(r):
            ok, why = runtime_ctl.stop(r)
            return runtime_ctl.start(r) if ok else (ok, why)
        self.show_offer(_("Restart {0} so it reads its new settings?").format(rt.name), _("Restart"), _("Not now"),
                        lambda: self.control_runtime(rt, restart, _("Restart")), lambda: None)
