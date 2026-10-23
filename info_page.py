"""AI Agents Map's Information page (a mixin of ai_popup.AgentsMap; the ⓘ in the header): cards
for the author's other Omarchy plugins in LLM Monitor's layout (more_plugins, made in
~/Projects/omarchy-plugin-i18n). The list comes from the marketplace catalog when the page opens
(plugin_catalog: at most hourly, only if it changed; the cached or built-in list otherwise); a card
opens the plugin's page in the marketplace.
"""

from __future__ import annotations

import threading
import time

from gi.repository import GLib, Gtk

import ai_core
import more_plugins
import plugin_catalog
from i18n import _

OWN_ID = "ubruckhaus.ai-agents-map"

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
        # The author's plugins, from the marketplace (plugin_catalog): filled when the page opens.
        self.info_cards = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        body.append(self.info_cards)
        self.info_source = Gtk.Label(xalign=0, wrap=True, max_width_chars=1)
        self.info_source.add_css_class("am-agent-detail")
        body.append(self.info_source)
        self.info_shown = None
        self.fill_info_cards(plugin_catalog.load().get("plugins", []), plugin_catalog.load().get("checked"))
        page.append(self.page_scroller(body))
        return page

    def fill_info_cards(self, plugins: list, checked=None):
        """The cards for this list (AI Agents Map itself left out: this app); the built-in list
        when there is none yet."""
        cards = plugin_catalog.cards(plugins)
        if cards != self.info_shown:
            self.info_shown = cards
            self.clear_box(self.info_cards)
            self.info_cards.append(more_plugins.build_cards(OWN_ID, "am", plugins=cards))
        if checked:
            when = time.strftime("%d.%m.%Y %H:%M", time.localtime(float(checked)))
            self.info_source.set_label(_("From the Omarchy Plugin Marketplace, checked {0}.").format(when))
        else:
            self.info_source.set_label(_("The marketplace could not be read yet: the plugins known to this "
                                         "version are shown."))

    def refresh_info_cards(self):
        """On opening the page: the marketplace's list (at most hourly, only if it changed), on a thread."""
        if getattr(self, "info_refreshing", False):
            return
        self.info_refreshing = True

        def work():
            plugins, checked = None, None
            try:
                plugins = plugin_catalog.refresh()
                checked = plugin_catalog.load().get("checked")
            finally:
                # Always back on the main loop, so a failure never leaves the page refreshing.
                GLib.idle_add(done, plugins, checked)

        def done(plugins, checked):
            self.info_refreshing = False
            if plugins is not None:
                self.fill_info_cards(plugins, checked)
            return False
        threading.Thread(target=work, daemon=True).start()

    def open_info(self):
        if self.page_name() not in ("settings", "help", "info"):
            self.remember_page()
        self.show_page("info")
        self.refresh_info_cards()

    def close_info(self):
        self.back_to_last_page()
