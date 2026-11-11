"""Settings › What may be done (Maprisma's action_rules_window.dart): the allow and deny rules, added and
removed here, a request tested against them, and whether a refusal names the rule (a mixin)."""

from __future__ import annotations

from gi.repository import Gdk, Gtk

import action_rules
import ai_core
from i18n import _


class RulesWindow:
    def open_rules(self):
        window = Gtk.Window(title=_("What may be done"), transient_for=self.window, modal=True,
                            default_width=720, default_height=640)
        window.add_css_class("am-card")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin_start=18, margin_end=18,
                      margin_top=14, margin_bottom=14)
        note = Gtk.Label(label=_("Rules for Ask AI and for what you draw on the map."), xalign=0, wrap=True)
        note.add_css_class("am-hint")
        box.append(note)
        listing = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        listing.add_css_class("boxed-list")
        scroller = Gtk.ScrolledWindow(vexpand=True, child=listing)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        box.append(scroller)

        actions = ["*"] + list(action_rules.RULE_ACTIONS)
        categories = ["*"] + list(action_rules.RULE_CATEGORIES)

        def word(kind: str, value: str) -> str:
            if value == "*":
                return _("All")
            names = action_rules.ACTION_NAMES if kind == "action" else action_rules.CATEGORY_NAMES
            return _(names.get(value, value))

        def rules() -> list:
            value = ai_core.setting("action_rules")
            return list(value) if isinstance(value, list) else action_rules.default_rules()

        def fill():
            while (row := listing.get_first_child()) is not None:
                listing.remove(row)
            for index, rule in enumerate(rules()):
                row = Gtk.Box(spacing=8, margin_start=8, margin_end=8, margin_top=4, margin_bottom=4)
                text = " · ".join([_("Allow") if rule["effect"] == "allow" else _("Deny"),
                                   word("action", rule["action"]), word("category", rule["category"]),
                                   _("All") if rule["item"] == "*" else rule["item"],
                                   _("All") if rule["agent"] == "*" else rule["agent"]])
                label = Gtk.Label(label=text, xalign=0, hexpand=True, wrap=True)
                row.append(label)
                remove = Gtk.Button(label=_("Remove"))
                remove.add_css_class("am-small-btn")
                remove.connect("clicked", lambda _b, i=index: (save([r for n, r in enumerate(rules()) if n != i])))
                row.append(remove)
                listing.append(row)

        def save(value: list):
            self.save_setting(action_rules=value)
            fill()

        # A new rule, and a request to test (the same fields).
        form = Gtk.Box(spacing=6)
        effect = Gtk.DropDown.new_from_strings([_("Allow"), _("Deny")])
        action = Gtk.DropDown.new_from_strings([word("action", a) for a in actions])
        category = Gtk.DropDown.new_from_strings([word("category", c) for c in categories])
        item = Gtk.Entry(placeholder_text=_("All"), hexpand=True)
        agent = Gtk.Entry(placeholder_text=_("All"), width_chars=10)
        for widget in (effect, action, category, item, agent):
            form.append(widget)
        box.append(form)
        buttons = Gtk.Box(spacing=8)
        result = Gtk.Label(xalign=0, hexpand=True, wrap=True)

        def fields() -> dict:
            return {"effect": "allow" if effect.get_selected() == 0 else "deny",
                    "action": actions[action.get_selected()], "category": categories[category.get_selected()],
                    "item": item.get_text().strip() or "*", "agent": agent.get_text().strip() or "*"}

        def add(*_args):
            rule = fields()
            if rule["item"] != "*" and rule["category"] == "*":
                result.set_label(_("Choose a category for an item."))
                return
            save(rules() + [rule])
            result.set_label("")

        def test(*_args):
            rule = fields()
            if "*" in (rule["action"], rule["category"]):
                result.set_label(_("Choose an action and a category to test."))
                return
            text = action_rules.refusal(rules(), True, rule["action"], rule["category"],
                                        "" if rule["item"] == "*" else rule["item"],
                                        "" if rule["agent"] == "*" else rule["agent"])
            result.set_label(text or _("Allowed"))
        for label, handler in ((_("Add"), add), (_("Test"), test)):
            button = Gtk.Button(label=label)
            button.add_css_class("am-small-btn")
            button.connect("clicked", handler)
            buttons.append(button)
        buttons.append(result)
        reset = Gtk.Button(label=_("Restore defaults"))
        reset.add_css_class("am-small-btn")
        reset.connect("clicked", lambda *_: save(action_rules.default_rules()))
        buttons.append(reset)
        box.append(buttons)
        hints = Gtk.CheckButton(label=_("Say which rule refused something"),
                                active=ai_core.setting("action_rule_hints") is not False)
        hints.connect("toggled", lambda b: self.save_setting(action_rule_hints=b.get_active()))
        box.append(hints)
        window.set_child(box)
        self.guard_text_fields_in(box)          # paste only through the bounded read
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", lambda _c, key, _code, _state: (window.close(), True)[1]
                     if key == Gdk.KEY_Escape else False)
        window.add_controller(keys)
        fill()
        window.present()
