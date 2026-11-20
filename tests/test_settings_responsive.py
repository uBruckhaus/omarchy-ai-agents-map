"""Settings state/GTK wiring checks without a display or live settings.

The small widget doubles exercise the real handlers; pixel allocation still
needs a GTK display and is not claimed by these tests.
"""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
import settings_pages as module


class Widget:
    def __init__(self, **properties):
        self.properties = {"visible": True, "sensitive": True, "mapped": True, **properties}
        self.children, self.signals = [], {}
        self.parent = None
        self.removed_ticks = []

    def append(self, child):
        self.children.append(child)
        child.parent = self

    add = append
    add_suffix = append

    def remove(self, child):
        self.children.remove(child)
        child.parent = None

    def set_child(self, child):
        self.children = []
        self.append(child)

    def get_child(self):
        return self.get_first_child()

    def get_first_child(self):
        return self.children[0] if self.children else None

    def get_next_sibling(self):
        if self.parent is None:
            return None
        siblings = self.parent.children
        index = siblings.index(self) + 1
        return siblings[index] if index < len(siblings) else None

    def get_parent(self):
        return self.parent

    def is_ancestor(self, ancestor):
        parent = self.parent
        while parent is not None:
            if parent is ancestor:
                return True
            parent = parent.parent
        return False

    def get_root(self):
        return self.parent.get_root() if self.parent is not None else self.properties.get("root")

    def grab_focus(self):
        root = self.get_root()
        if root is None:
            return False
        root.focus = self
        return True

    def connect(self, name, callback, *extra):
        self.signals[name] = (callback, extra)

    def emit(self, name, *args):
        if name in self.signals:
            callback, extra = self.signals[name]
            return callback(self, *args, *extra)

    def add_css_class(self, name):
        self.properties.setdefault("classes", set()).add(name)

    def has_css_class(self, name):
        return name in self.properties.get("classes", ())

    def update_property(self, keys, values):
        self.properties["accessible"] = dict(zip(keys, values))

    def add_tick_callback(self, callback):
        self.tick = callback
        return 1

    def remove_tick_callback(self, value):
        self.removed_ticks.append(value)

    def get_pango_context(self):
        return SimpleNamespace(get_font_description=lambda: SimpleNamespace(get_size=lambda: 11 * module.Pango.SCALE))

    def compute_bounds(self, parent):
        return True, SimpleNamespace(get_y=lambda: self.properties.get("y", 0), get_height=lambda: 40)

    def __getattr__(self, name):
        if name.startswith("set_"):
            return lambda *values: self.properties.__setitem__(name[4:], values[0] if len(values) == 1 else values)
        if name.startswith("get_"):
            return lambda: self.properties.get(name[4:], 0)
        raise AttributeError(name)


class Entry(Widget):
    def get_text(self):
        return self.properties.get("text", "")

    def set_text(self, text):
        self.properties["text"] = text
        self.emit("notify::text", None)


class Selector(Widget):
    @classmethod
    def new_from_strings(cls, strings):
        return cls(strings=strings, selected=0)

    def set_selected(self, selected):
        if selected != self.get_selected():
            self.properties["selected"] = selected
            self.emit("notify::selected", None)


class Navigation(Widget):
    selected = None

    def select_row(self, row):
        if self.selected is not row:
            self.selected = row
            self.emit("row-selected", row)

    def unselect_all(self):
        self.select_row(None)


class Adjustment:
    value, size = 0, 400

    def get_value(self):
        return self.value

    def set_value(self, value):
        self.value = value

    def get_page_size(self):
        return self.size


class Scroller(Widget):
    def __init__(self, **properties):
        super().__init__(**properties)
        self.adjustment = Adjustment()

    def get_vadjustment(self):
        return self.adjustment


class PreferenceRow(Widget):
    pass


class ActionRow(PreferenceRow):
    pass


class View(module.SettingsPages):
    def __init__(self):
        self.root = SimpleNamespace(focus=None)
        self.root.get_focus = lambda: self.root.focus
        self.scrollers = []
        self.settings_box = Widget(root=self.root)
        self.settings_clamp = Widget()
        self.build_settings_sections(self.settings_box)
        self.fill_general_settings(None, None)
        self.find_in_settings()

    def page_name(self):
        return getattr(self, "page", "settings")

    def page_scroller(self, child):
        result = Scroller()
        result.set_child(child)
        self.scrollers.append(result)
        return result

    def addon_hooks(self, key):
        return []

    def fill_general_settings(self, left, right):
        for section, title in (("general", "Language"), ("map", "Notify map"), ("agents", "Notify when red")):
            group = Widget()
            group.append(ActionRow(title=title, subtitle="fixture detail"))
            self.settings_sections[section].append(group)


class ResponsiveSettingsTest(unittest.TestCase):
    def setUp(self):
        self.patches = [
            mock.patch.multiple(module.Gtk, Box=Widget, Label=Widget, SearchEntry=Entry,
                                DropDown=Selector, ListBox=Navigation, ListBoxRow=Widget,
                                ScrolledWindow=Scroller, SignalListItemFactory=Widget, Button=Widget),
            mock.patch.multiple(module.Adw, PreferencesRow=PreferenceRow, ActionRow=ActionRow,
                                PreferencesGroup=Widget),
            mock.patch.object(module.GLib, "idle_add", lambda callback, *args: callback(*args)),
            mock.patch.object(module.desktop, "launcher_entry", return_value=False),
            mock.patch.object(module, "_", lambda text: text),
        ]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.view = View()

    def test_compact_layout_retains_search_and_controls_without_reparenting(self):
        view = self.view
        view.choose_settings_section("agents")
        view.settings_find.set_text("notify")
        search, content = view.settings_find, view.settings_content
        for width, points in ((540, 11), (960, 22), (1400, 11)):
            view.update_settings_layout(width, points)
            self.assertIs(view.settings_find, search)
            self.assertIs(view.settings_content, content)
            self.assertEqual(search.get_text(), "notify")
            self.assertEqual(view.settings_current_section, "agents")
        self.assertTrue(view.settings_navigation_scroll.get_visible())
        self.assertFalse(view.settings_compact_selector.get_visible())

    def test_unallocated_tick_keeps_wide_layout_and_geometry(self):
        view = self.view
        view.update_settings_layout(1400)
        geometry = view.settings_geometry
        view.settings_layout.set_width(0)
        self.assertTrue(view.settings_layout_tick(view.settings_layout, None))
        self.assertFalse(view.settings_compact)
        self.assertIs(view.settings_geometry, geometry)
        self.assertTrue(view.settings_navigation_scroll.get_visible())

    def test_tick_normalizes_absolute_and_unset_font_sizes(self):
        view = self.view
        view.settings_layout.set_width(960)
        for size, absolute, expected in ((14, True, 10.5), (0, False, 11), (-1, True, 11)):
            with self.subTest(size=size, absolute=absolute):
                description = SimpleNamespace(get_size=lambda: size * module.Pango.SCALE,
                                              get_size_is_absolute=lambda: absolute)
                context = SimpleNamespace(get_font_description=lambda: description)
                with mock.patch.object(view.settings_layout, "get_pango_context", return_value=context):
                    view.settings_layout_tick(view.settings_layout, None)
                self.assertEqual(view.settings_geometry[2], expected)
                self.assertFalse(view.settings_compact)

    def test_global_search_shows_all_matching_sections_and_no_selected_section(self):
        view = self.view
        view.settings_find.set_text("notify")
        self.assertTrue(view.settings_section_containers["map"].get_visible())
        self.assertTrue(view.settings_section_containers["agents"].get_visible())
        self.assertFalse(view.settings_section_containers["general"].get_visible())
        self.assertTrue(view.settings_section_headings["agents"].get_visible())
        self.assertIsNone(view.settings_list.selected)
        self.assertEqual(view.settings_compact_selector.get_selected(), 0)

    def test_clearing_query_restores_the_previous_section(self):
        view = self.view
        view.choose_settings_section("general")
        view.settings_find.set_text("notify")
        view.settings_find.set_text("")
        self.assertTrue(view.settings_section_containers["general"].get_visible())
        self.assertFalse(view.settings_section_containers["map"].get_visible())
        self.assertIs(view.settings_list.selected, view.settings_section_rows["general"])

    def test_explicit_compact_selection_clears_search_and_shows_that_section(self):
        view = self.view
        view.settings_find.set_text("notify")
        view.settings_compact_selector.set_selected(3)  # Map, after the search entry.
        self.assertEqual(view.settings_current_section, "map")
        self.assertEqual(view.settings_find.get_text(), "")
        self.assertTrue(view.settings_section_containers["map"].get_visible())

    def test_no_results_are_explicit_and_keep_the_query(self):
        view = self.view
        view.settings_find.set_text("nothing matches")
        self.assertTrue(view.settings_no_matches.get_visible())
        self.assertEqual(view.settings_no_matches.get_label(), "No setting matches “nothing matches”")
        self.assertFalse(any(box.get_visible() for box in view.settings_section_containers.values()))

    def test_search_finds_full_hints_even_when_they_are_tooltip_only(self):
        view = self.view
        row = view.settings_sections["agents"].get_first_child().get_first_child()
        row.set_tooltip_text("An alert after the quota resets, with its full explanation kept on hover.")
        view.settings_find.set_text("quota resets")
        self.assertTrue(view.settings_section_containers["agents"].get_visible())
        self.assertTrue(row.get_visible())
        self.assertFalse(view.settings_section_containers["general"].get_visible())

    def test_searching_a_group_heading_shows_its_settings(self):
        view = self.view
        group = view.settings_sections["agents"].get_first_child()
        group.set_title("Costs")
        view.settings_find.set_text("costs")
        self.assertTrue(view.settings_section_containers["agents"].get_visible())
        self.assertTrue(group.get_first_child().get_visible())

    def test_search_unescapes_group_titles_and_descriptions(self):
        view = self.view
        group = view.settings_sections["agents"].get_first_child()
        for field in ("title", "description"):
            group.set_title("")
            group.set_description("")
            getattr(group, "set_" + field)(module.GLib.markup_escape_text("An agent's findings & limits"))
            for query, expected in (("agent's", True), ("&", True), ("apos", False), ("amp", False)):
                with self.subTest(field=field, query=query):
                    view.settings_find.set_text(query)
                    self.assertEqual(group.get_first_child().get_visible(), expected)

    def test_resize_keeps_search_focus_and_transfers_navigation_focus(self):
        view = self.view
        view.update_settings_layout(1400)
        view.settings_find.grab_focus()
        view.update_settings_layout(540)
        self.assertIs(view.root.focus, view.settings_find)
        view.choose_settings_section("agents")
        view.settings_compact_selector.grab_focus()
        view.update_settings_layout(1400)
        self.assertIs(view.root.focus, view.settings_section_rows["agents"])
        view.update_settings_layout(540)
        self.assertIs(view.root.focus, view.settings_compact_selector)

    def test_focused_sidebar_row_is_revealed_after_a_height_only_resize(self):
        view = self.view
        view.settings_layout.set_width(1400)
        view.settings_layout.set_height(900)
        row = view.settings_section_rows["about"]
        row.properties["y"] = 650
        row.grab_focus()
        adjustment = view.settings_navigation_scroll.get_vadjustment()
        adjustment.size = 900
        view.settings_layout_tick(view.settings_layout, None)
        self.assertEqual(adjustment.value, 0)
        adjustment.size = 300
        view.settings_layout.set_height(300)
        view.settings_layout_tick(view.settings_layout, None)
        self.assertEqual(adjustment.value, 390)

    def test_rebuild_preserves_selected_section_query_and_search_focus(self):
        view = self.view
        view.choose_settings_section("map")
        view.settings_find.set_text("notify")
        view.settings_find.grab_focus()
        view.fill_settings()
        self.assertEqual(view.settings_current_section, "map")
        self.assertEqual(view.settings_find.get_text(), "notify")
        self.assertIs(view.root.focus, view.settings_find)
        self.assertTrue(view.settings_section_containers["agents"].get_visible())
        view.update_settings_layout(540)
        self.assertFalse(view.settings_navigation_scroll.get_visible())

    def test_compact_rebuild_restores_selector_focus_before_any_tick(self):
        view, queued = self.view, []
        view.update_settings_layout(540)
        view.settings_compact_selector.grab_focus()
        with mock.patch.object(module.GLib, "idle_add", lambda callback, *args: queued.append((callback, args)) or 1):
            view.fill_settings()
        self.assertIs(view.settings_compact, True)
        self.assertTrue(view.settings_compact_selector.get_visible())
        self.assertFalse(view.settings_navigation_scroll.get_visible())
        for callback, args in queued:
            callback(*args)
        self.assertIs(view.root.focus, view.settings_compact_selector)

    def test_queued_scroll_rejects_left_unmapped_rebuilt_or_remapped_settings(self):
        for change in ("leave", "unmap", "rebuild", "remap"):
            with self.subTest(change=change):
                view, queued = View(), []
                view.update_settings_layout(1400)
                view.settings_layout.set_width(1400)
                row = view.settings_section_rows["about"]
                row.properties["y"] = 650
                row.grab_focus()
                old_adjustment = view.settings_navigation_scroll.get_vadjustment()
                with mock.patch.object(module.GLib, "idle_add", lambda callback, *args: queued.append((callback, args)) or 1):
                    view.settings_layout_tick(view.settings_layout, None)
                self.assertTrue(queued)
                if change == "leave":
                    view.page = "main"
                elif change == "unmap":
                    view.settings_layout.set_mapped(False)
                elif change == "rebuild":
                    view.fill_settings()
                    row = view.settings_section_rows["about"]
                    row.properties["y"] = 650
                    row.grab_focus()
                else:
                    view.settings_layout_unmapped(view.settings_layout)
                    view.settings_layout_mapped(view.settings_layout)
                adjustment = view.settings_navigation_scroll.get_vadjustment()
                for callback, args in queued:
                    callback(*args)
                self.assertEqual(old_adjustment.value, 0)
                self.assertEqual(adjustment.value, 0)

    def test_mapping_installs_one_tick_and_unmapping_removes_it(self):
        view, layout = self.view, self.view.settings_layout
        view.settings_layout_mapped(layout)
        view.settings_layout_mapped(layout)
        self.assertEqual(layout.settings_tick, 1)
        view.settings_layout_unmapped(layout)
        view.settings_layout_unmapped(layout)
        self.assertEqual(layout.removed_ticks, [1])
        view.settings_layout_mapped(layout)
        self.assertEqual(layout.settings_tick, 1)

    def test_viewport_scrollers_do_not_accumulate_or_receive_full_page_minimum_heights(self):
        self.assertEqual(self.view.scrollers, [])
        self.view.fill_settings()
        self.assertEqual(self.view.scrollers, [])

    def test_rebuild_keeps_focus_on_a_result_from_another_section(self):
        view = self.view
        view.settings_find.set_text("notify")
        row = view.settings_sections["agents"].get_first_child().get_first_child()
        row.grab_focus()
        view.fill_settings()
        self.assertEqual(view.settings_current_section, "general")
        self.assertEqual(view.root.focus.get_title(), "Notify when red")
        self.assertIsNot(view.root.focus, row)

    def test_opening_settings_does_not_restore_a_different_pages_focus(self):
        view = self.view
        other_page = Widget(root=view.root)
        outside = ActionRow(title="Language")
        other_page.append(outside)
        outside.grab_focus()
        view.fill_settings()
        self.assertIs(view.root.focus, outside)

    def test_returning_from_settings_or_addons_can_resume_the_guide(self):
        resumed = []
        view = self.view
        view.return_page = "firstagent"
        view.first_resume = lambda: resumed.append(True)
        view.back_to_last_page()
        self.assertEqual(resumed, [True])
        self.assertIsNone(view.return_page)

    def test_queued_focus_restore_does_not_return_to_settings_after_a_page_change(self):
        view, queued = self.view, []
        view.update_settings_layout(1400)
        view.settings_section_rows["agents"].grab_focus()
        with mock.patch.object(module.GLib, "idle_add", lambda callback, *args: queued.append((callback, args)) or 1):
            view.update_settings_layout(540)
        self.assertEqual(len(queued), 1)
        other = Widget(root=view.root)
        other.grab_focus()
        view.page = "firstagent"
        for callback, args in queued:
            callback(*args)
        self.assertIs(view.root.focus, other)

    def test_queued_rebuild_restore_does_not_apply_to_a_later_widget_tree(self):
        view, queued = self.view, []
        view.settings_find.grab_focus()
        with mock.patch.object(module.GLib, "idle_add", lambda callback, *args: queued.append((callback, args)) or 1):
            view.fill_settings()
            view.fill_settings()
        self.assertTrue(queued)
        target = view.settings_section_rows["about"]
        target.grab_focus()
        for callback, args in queued:
            callback(*args)
        self.assertIs(view.root.focus, target)

    def test_unmapping_invalidates_a_restore_even_if_the_same_tree_returns(self):
        view, queued = self.view, []
        view.update_settings_layout(1400)
        view.settings_section_rows["agents"].grab_focus()
        with mock.patch.object(module.GLib, "idle_add", lambda callback, *args: queued.append((callback, args)) or 1):
            view.update_settings_layout(540)
        view.settings_layout_unmapped(view.settings_layout)
        view.settings_layout_mapped(view.settings_layout)
        view.settings_find.grab_focus()
        for callback, args in queued:
            callback(*args)
        self.assertIs(view.root.focus, view.settings_find)

    def test_unmapped_layout_never_accepts_a_queued_restore(self):
        view, queued = self.view, []
        view.update_settings_layout(1400)
        view.settings_section_rows["agents"].grab_focus()
        with mock.patch.object(module.GLib, "idle_add", lambda callback, *args: queued.append((callback, args)) or 1):
            view.update_settings_layout(540)
        view.settings_layout.set_mapped(False)
        target = Widget(root=view.root)
        target.grab_focus()
        for callback, args in queued:
            callback(*args)
        self.assertIs(view.root.focus, target)


if __name__ == "__main__":
    unittest.main()
