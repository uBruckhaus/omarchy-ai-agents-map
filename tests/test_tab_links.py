"""The header tabs [ Projects ] [ AI Map ] and the links between them (WP7; Maprisma tasks 070, 086,
091, 092, 093), headless: the start tab, Ctrl+1 / Ctrl+2, the departure guards, Esc, Settings coming
back to the tab, Show on map / Fix on map, Used in N projects, the terminal's progress line and the
Ask AI route to Configure for this PC. Run with DISPLAY / WAYLAND_DISPLAY unset."""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import gi  # noqa: E402
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk  # noqa: E402

import ai_popup  # noqa: E402
import main_tabs  # noqa: E402
import settings_pages  # noqa: E402
import smart_card  # noqa: E402
import terminal_pane  # noqa: E402
from main_tabs import MainTabs  # noqa: E402
from projects_page import ProjectsPage  # noqa: E402

CTRL = Gdk.ModifierType.CONTROL_MASK


class Ctrl:
    """The Projects page's controller as the shell sees it."""

    def __init__(self):
        self.open = False
        self.busy = self.confirming = self.leaving = False
        self.disposed = False
        self.resets = 0
        self.leave_answer = True
        self.asked = 0
        self.back = 0

    def reset_board_choices(self):
        self.resets += 1

    def before_leave(self, done):
        self.asked += 1
        done(self.leave_answer)

    def back_to_board(self, done=None):
        self.back += 1
        self.open = False
        if done is not None:
            done(True)


class Map:
    def __init__(self, boxes=None):
        self.focused = None
        self.boxes = boxes or {}
        self.used = None

    def set_focused(self, node_id):
        self.focused = node_id

    def box_rect(self, node_id):
        return self.boxes.get(node_id)

    def set_used_in(self, counts):
        self.used = counts


class Host(MainTabs):
    """MainTabs over a stand-in window: pages by name, no GTK."""

    def __init__(self, page="main"):
        self.page = page
        self.stack = object()
        self.projects_ctrl = Ctrl()
        self.side_map = Map({"agent:claude": (10, 20, 100, 40), "runtime:ollama": (300, 20, 100, 40)})
        self.map_scroller = None
        self.agent_list = [SimpleNamespace(command="claude", name="Claude Code", installed=True)]
        self.toasts = []
        self.saved = {}
        self.calls = []
        self.view = "main"
        self.return_page = None
        self.updating_buttons = False
        self.setup_welcome = False
        self.reports_running = None

    # what MainTabs asks of the window
    def page_name(self):
        return self.page

    def show_page(self, name):
        self.page = name
        self.calls.append(("show_page", name))

    def show_view(self, name, save=True):
        # As ai_popup.AgentsMap.show_view: "main" on the Projects tab is the Projects page.
        self.page = "projects" if name == "main" and self.main_tab == "projects" else name
        self.calls.append(("show_view", name))

    def open_projects(self, agent=None, name=""):
        self.page = "projects"
        self.calls.append(("open_projects", agent, name))

    def projects_prepare_leave(self, done):
        ProjectsPage.projects_prepare_leave(self, done)

    def toast(self, text, seconds, *_a, **_k):
        self.toasts.append(text)

    def update_header(self):
        pass

    def refresh_map(self, facts=None):
        self.calls.append(("refresh_map",) if facts is None else ("refresh_map", "fresh"))
        if facts is not None:
            self.agent_list = facts[0]

    def read_map_facts(self):
        return self.fresh_facts

    fresh_facts = ([SimpleNamespace(command="claude", name="Claude Code", installed=True)], None, {})

    def close_connect(self, navigate=True):
        self.calls.append(("close_connect",))

    def close_ask_ai(self):
        self.page = "main"
        self.calls.append(("close_ask_ai",))

    def stop_reports_timer(self):
        self.calls.append(("stop_reports_timer",))

    def confirm_leave_reports(self, then):
        self.calls.append(("confirm_leave_reports",))
        then()

    def close(self):
        self.calls.append(("close",))


def patched_settings(testcase, saved):
    testcase.enterContext(mock.patch.object(main_tabs.ai_core, "setting", side_effect=lambda key: saved.get(key)))
    testcase.enterContext(mock.patch.object(main_tabs.ai_core, "update_settings",
                                            side_effect=lambda **values: saved.update(values)))
    testcase.enterContext(mock.patch.object(main_tabs.guide_navigation, "abandon_handoffs", lambda _app: None))
    testcase.enterContext(mock.patch.object(main_tabs.GLib, "timeout_add", lambda _ms, fn, *a: fn(*a)))
    testcase.enterContext(mock.patch.object(main_tabs.GLib, "timeout_add_seconds", lambda _s, _fn, *a: 1))
    testcase.enterContext(mock.patch.object(main_tabs.GLib, "source_remove", lambda _id: None))
    testcase.enterContext(mock.patch.object(main_tabs.GLib, "idle_add", lambda fn, *a: fn(*a)))
    testcase.enterContext(mock.patch.object(main_tabs.threading, "Thread",
                                            side_effect=lambda target, args=(), **_k:
                                            SimpleNamespace(start=lambda: target(*args))))


class StartTabTest(unittest.TestCase):
    def test_start_tab_rules(self):
        summary = {"total": 2, "connected": True}
        self.assertEqual(main_tabs.start_tab(True, "projects", summary), "map")       # setup first
        self.assertEqual(main_tabs.start_tab(False, "projects", None), "projects")   # the last used
        self.assertEqual(main_tabs.start_tab(False, "map", summary), "map")
        self.assertEqual(main_tabs.start_tab(False, "", summary), "projects")         # a project exists
        self.assertEqual(main_tabs.start_tab(False, None, {"total": 0}), "map")
        self.assertEqual(main_tabs.start_tab(False, None, None), "map")               # not read in time
        self.assertEqual(main_tabs.start_tab(False, None, {"total": 3, "connected": False}), "map")

    def test_counts_and_description(self):
        counts = main_tabs.tab_counts({"running": 1, "pausing": 1, "needs_you": 2, "attention_projects": 1,
                                       "attention": 5, "error": "project_store_corrupt"})
        self.assertEqual((counts["running"], counts["needs_you"], counts["attention"]), (2, 2, 1))
        text = main_tabs.describe(counts)
        self.assertIn("2 running", text)
        self.assertIn("2 need you", text)
        self.assertIn("1 need attention", text)
        self.assertIn("problem", text)
        old = main_tabs.tab_counts({"waiting": 3, "attention": 4})      # an older summary
        self.assertEqual((old["needs_you"], old["attention"]), (3, 4))
        self.assertEqual(main_tabs.describe(main_tabs.tab_counts({"connected": False})),
                         "Projects · status unavailable")

    def test_used_in(self):
        self.assertEqual(main_tabs.used_in({"by_agent": {"claude": 2, "pi": 0, "x": "3", "y": True}}),
                         {"claude": 2})
        self.assertEqual(main_tabs.used_in_text(1), "Used in 1 project")
        self.assertEqual(main_tabs.used_in_text(4), "Used in 4 projects")

    def test_decide_start_tab_uses_saved_tab_then_summary(self):
        saved = {"main_tab": "projects"}
        patched_settings(self, saved)
        host = Host()
        host.decide_start_tab()
        self.assertEqual((host.main_tab, host.page), ("projects", "projects"))
        saved.clear()
        host = Host()
        host.refresh_project_summary = lambda then=None: then({"total": 1, "connected": True})
        host.decide_start_tab()
        self.assertEqual(host.main_tab, "projects")
        host = Host()
        host.setup_pending = True               # the welcome page is due: the map, the summary is not asked
        saved["main_tab"] = "projects"
        host.refresh_project_summary = mock.Mock()
        host.decide_start_tab()
        self.assertEqual(host.main_tab, "map")
        host.refresh_project_summary.assert_not_called()

    def test_setup_done_without_agents_keeps_the_saved_tab(self):
        """Codex r1 #5: setup finished, the last agent uninstalled: still the tab used last."""
        saved = {"main_tab": "projects", "setup_done": True}
        patched_settings(self, saved)
        host = Host()
        host.agent_list = []
        host.adapter_addons = lambda: ["an add-on"]
        host.decide_start_tab()
        self.assertEqual(host.main_tab, "projects")
        saved["setup_done"] = False                 # add-ons there, setup not finished: the map
        host = Host()
        host.adapter_addons = lambda: ["an add-on"]
        host.decide_start_tab()
        self.assertEqual(host.main_tab, "map")


class TabSwitchTest(unittest.TestCase):
    def setUp(self):
        self.saved = {}
        patched_settings(self, self.saved)

    def test_tab_choice_is_saved_and_map_resets_the_board(self):
        host = Host()
        host.refresh_project_summary = lambda then=None: None
        host.select_tab("projects")
        self.assertEqual((host.page, host.main_tab, self.saved["main_tab"]), ("projects", "projects", "projects"))
        host.select_tab("map")
        self.assertEqual((host.page, host.main_tab, self.saved["main_tab"]), ("main", "map", "map"))
        self.assertEqual(host.projects_ctrl.resets, 1)          # filter, chip and Cards | Phases reset

    def test_leaving_an_open_project_asks_and_cancel_keeps_it(self):
        host = Host(page="projects")
        host.main_tab = "projects"
        host.projects_ctrl.open = True
        host.projects_ctrl.leave_answer = False                 # Cancel or a failed save
        host.select_tab("map")
        self.assertEqual((host.page, host.main_tab), ("projects", "projects"))
        self.assertEqual(host.projects_ctrl.asked, 1)
        self.assertFalse(host.departing)
        host.projects_ctrl.leave_answer = True
        host.refresh_project_summary = lambda then=None: None
        host.select_tab("map")
        self.assertEqual((host.page, host.main_tab), ("main", "map"))

    def test_busy_project_waits(self):
        host = Host(page="projects")
        host.main_tab = "projects"
        host.projects_ctrl.busy = True
        host.select_tab("map")
        self.assertEqual(host.page, "projects")
        self.assertEqual(host.toasts, ["Wait for the current step"])

    def test_projects_again_goes_back_to_the_board(self):
        host = Host(page="projects")
        host.main_tab = "projects"
        host.projects_ctrl.open = True
        host.select_tab("projects")
        self.assertEqual(host.projects_ctrl.back, 1)
        self.assertEqual(host.page, "projects")

    def test_header_actions_go_through_the_guard(self):
        host = Host(page="projects")
        host.main_tab = "projects"
        opened = []
        host.projects_ctrl.leave_answer = False
        host.after_leaving(lambda: opened.append("settings"))
        self.assertEqual(opened, [])
        host.projects_ctrl.leave_answer = True
        host.after_leaving(lambda: opened.append("settings"))
        self.assertEqual(opened, ["settings"])
        host.to_map_first(lambda: opened.append("ask"))           # Ask AI: the AI Map first
        self.assertEqual((opened[-1], host.main_tab, self.saved["main_tab"]), ("ask", "map", "map"))

    def test_following_a_page_keeps_neutral_pages(self):
        host = Host(page="projects")
        host.main_tab = "projects"
        for page in ("settings", "help", "reports"):
            host.page = page
            host.follow_tab()
            self.assertEqual(host.main_tab, "projects")
        host.page = "askai"                          # a page of the map: the AI Map tab
        host.follow_tab()
        self.assertEqual(host.main_tab, "map")

    def test_leaving_ask_ai_by_tab_stops_it(self):
        host = Host(page="askai")
        host.select_tab("projects")
        self.assertIn(("close_ask_ai",), host.calls)
        self.assertEqual(host.page, "projects")


class MapLinksTest(unittest.TestCase):
    def setUp(self):
        self.saved = {}
        patched_settings(self, self.saved)

    def test_show_on_map_pans_and_marks(self):
        host = Host(page="projects")
        host.main_tab = "projects"
        host.show_on_map_link("claude")
        self.assertEqual((host.page, host.main_tab), ("main", "map"))
        self.assertIn(("refresh_map", "fresh"), host.calls)        # facts read on a worker
        self.assertEqual(host.side_map.focused, "agent:claude")
        host.forget_map_mark()
        self.assertIsNone(host.side_map.focused)

    def test_fix_on_map_for_a_box_not_on_the_map(self):
        host = Host(page="projects")
        host.main_tab = "projects"
        host.map_fix_link("runtime:vllm")
        self.assertEqual(host.toasts, ["This is not on the map."])
        host.map_fix_link("agent:pi")
        self.assertEqual(host.toasts[-1], "This agent is not on the map.")
        host.side_map.boxes.pop("agent:claude")      # removed meanwhile: the fresh facts decide
        host.show_on_map_link("claude")
        self.assertEqual(host.toasts[-1], "This agent is not on the map.")
        host.map_fix_link("runtime:ollama")
        self.assertEqual(host.side_map.focused, "runtime:ollama")

    def test_links_respect_the_guard(self):
        host = Host(page="projects")
        host.main_tab = "projects"
        host.projects_ctrl.leave_answer = False
        host.map_fix_link("runtime:ollama")
        self.assertEqual((host.page, host.side_map.focused), ("projects", None))

    def test_used_in_opens_the_board_with_the_chip(self):
        host = Host()
        host.projects_for_agent("claude")
        self.assertIn(("open_projects", "claude", "Claude Code"), host.calls)
        self.assertEqual((host.main_tab, self.saved["main_tab"]), ("projects", "projects"))

    def test_summary_feeds_the_map(self):
        host = Host()
        host.tab_buttons = {}
        host.project_summary = {"connected": True, "by_agent": {"claude": 2}}
        host.show_project_summary()
        self.assertEqual(host.side_map.used, {"claude": 2})

    def test_discovery_generation_rises_only_when_the_facts_change(self):
        """agy r1 #3: a redraw is no rescan; a change of agents or runtimes is, under the center's lock."""
        import threading
        import project_center
        host = Host()
        host.inventory = SimpleNamespace(runtimes=[SimpleNamespace(key="ollama", installed=True, running=False,
                                                                   models=["qwen"])])
        host._project_center_lock = threading.Lock()
        center = host._project_center = SimpleNamespace(discovery_generation=4, _lock=threading.RLock())
        center.bump_discovery = lambda: project_center.ProjectCenter.bump_discovery(center)
        host.projects_map_changed()                       # the first read: the center scans anyway
        host.projects_map_changed()                       # a redraw: nothing changed
        self.assertEqual(center.discovery_generation, 4)
        host.inventory.runtimes[0].running = True         # a local LLM started
        host.projects_map_changed()
        self.assertEqual(center.discovery_generation, 5)
        host.agent_list.append(SimpleNamespace(command="pi", name="Pi", installed=True, version="1"))
        host.projects_map_changed()
        host.projects_map_changed()
        self.assertEqual(center.discovery_generation, 6)

    def test_problems_from_projects_return_to_the_map(self):
        """agy r1 #4: to_map_first leaves the Projects page before the page it opens remembers where."""
        host = Host(page="projects")
        host.main_tab = "projects"
        seen = []
        host.to_map_first(lambda: seen.append(host.page))
        self.assertEqual(seen, ["main"])

    def test_departures_stop_dictation(self):
        """Codex r1 #3: any route away from Ask AI or a provider's page stops its microphone."""
        host = Host(page="askai")
        host.stop_ask_dictation = mock.Mock()
        host.stop_provider_config_dictation = mock.Mock()
        host.follow_tab()
        host.page = "settings"                         # Settings over Ask AI
        host.follow_tab()
        host.stop_ask_dictation.assert_called_once_with()
        host.page = "providerconfig"
        host.follow_tab()
        host.page = "help"
        host.follow_tab()
        host.stop_provider_config_dictation.assert_called_once_with()

    def test_header_actions_respect_a_report_being_generated(self):
        """Codex r1 #4: Help or Settings from Reports asks Cancel the report? first; Back keeps it."""
        host = Host(page="reports")
        host.reports_running = object()
        answers = []
        host.confirm_leave_reports = lambda then, stay=None: answers.append(stay) or stay()
        opened = []
        host.after_leaving(lambda: opened.append("help"))
        self.assertEqual(opened, [])
        host.confirm_leave_reports = lambda then, stay=None: then()
        host.after_leaving(lambda: opened.append("help"))
        self.assertEqual(opened, ["help"])

    def test_a_late_reveal_is_dropped(self):
        """agy r1 #8: a reveal still on its way when the map is left (or the window hides) does nothing."""
        host = Host()
        waits = []
        with mock.patch.object(main_tabs.GLib, "timeout_add", lambda _ms, fn, *a: waits.append(fn) or 7):
            host.map_fix_link("runtime:ollama")
            host.forget_map_mark()
            for wait in waits:
                wait()
        self.assertIsNone(host.side_map.focused)

    def test_wiring_in_the_window(self):
        source = Path(ai_popup.__file__).read_text()
        self.assertIn("self.projects_map_fix = self.map_fix_link", source)
        self.assertIn("self.projects_show_on_map = self.show_on_map_link", source)
        self.assertIn("self.side_map.on_used_in = self.projects_for_agent", source)
        self.assertIn("self.projects_map_changed()", source)
        self.assertIn('self.projects_header.set_visible(False)', source)


class KeysTest(unittest.TestCase):
    def app(self, page="main", selection=()):
        app = SimpleNamespace(page_name=lambda: page, selection=list(selection), select_tab=mock.Mock(),
                              close=mock.Mock(), set_selection=mock.Mock(), editing=lambda: False,
                              back_actions=lambda: {}, map_view=SimpleNamespace(set_shift=lambda _h: None),
                              window=SimpleNamespace(set_focus=mock.Mock(), get_visible_dialog=lambda: None),
                              after_leaving=mock.Mock(), in_search_field=lambda: False)
        app.dialog_open = lambda: ai_popup.AgentsMap.dialog_open(app)
        return app

    def test_keys_wait_under_a_dialog(self):
        """Both reviews r1: Esc and Ctrl+1/2 belong to an open dialog (or an agent's task box)."""
        app = self.app()
        app.window.get_visible_dialog = lambda: object()
        self.assertFalse(ai_popup.AgentsMap.on_key(app, None, Gdk.KEY_1, 0, CTRL))
        self.assertFalse(ai_popup.AgentsMap.on_key(app, None, Gdk.KEY_Escape, 0, 0))
        app.select_tab.assert_not_called()
        app.close.assert_not_called()
        app = self.app()
        app.ai_box = SimpleNamespace(gone=False)
        self.assertFalse(ai_popup.AgentsMap.on_key(app, None, Gdk.KEY_Escape, 0, 0))
        app.close.assert_not_called()
        ctrl = Ctrl()
        host = SimpleNamespace(page_name=lambda: "projects", projects_ctrl=ctrl, editing=lambda: False,
                               projects_escape=mock.Mock(), window=SimpleNamespace(get_visible_dialog=lambda: object()))
        self.assertFalse(ProjectsPage._projects_key(host, None, Gdk.KEY_Escape, 0, 0))
        host.projects_escape.assert_not_called()

    def test_esc_leaves_a_field_on_a_page_first(self):
        """agy r1 #6 / Codex r1 #9: Esc in a Settings field leaves the field; the next Esc goes back."""
        back = mock.Mock()
        app = self.app(page="settings")
        app.back_actions = lambda: {"settings": back}
        app.editing = lambda: True
        self.assertTrue(ai_popup.AgentsMap.on_key(app, None, Gdk.KEY_Escape, 0, 0))
        app.window.set_focus.assert_called_once_with(None)
        back.assert_not_called()
        self.assertFalse(ai_popup.AgentsMap.on_key(app, None, Gdk.KEY_BackSpace, 0, 0))   # the field's
        app.in_search_field = lambda: True           # Find in Settings clears itself
        self.assertFalse(ai_popup.AgentsMap.on_key(app, None, Gdk.KEY_Escape, 0, 0))
        app.editing = lambda: False
        self.assertTrue(ai_popup.AgentsMap.on_key(app, None, Gdk.KEY_Escape, 0, 0))
        back.assert_called_once_with()

    def test_ctrl_1_and_2_choose_the_tabs(self):
        app = self.app()
        self.assertTrue(ai_popup.AgentsMap.on_key(app, None, Gdk.KEY_1, 0, CTRL))
        app.select_tab.assert_called_with("projects")
        self.assertTrue(ai_popup.AgentsMap.on_key(app, None, Gdk.KEY_2, 0, CTRL))
        app.select_tab.assert_called_with("map")
        self.assertTrue(ai_popup.AgentsMap.on_key(app, None, Gdk.KEY_KP_1, 0, CTRL))
        app.select_tab.assert_called_with("projects")
        terminal = self.app(page="terminal")      # the agent's keys
        self.assertFalse(ai_popup.AgentsMap.on_key(terminal, None, Gdk.KEY_1, 0, CTRL))
        terminal.select_tab.assert_not_called()

    def test_esc_on_the_map_clears_then_closes(self):
        app = self.app(selection=["agent:claude"])
        self.assertTrue(ai_popup.AgentsMap.on_key(app, None, Gdk.KEY_Escape, 0, 0))
        app.set_selection.assert_called_once_with([])
        app.close.assert_not_called()
        app = self.app()
        self.assertTrue(ai_popup.AgentsMap.on_key(app, None, Gdk.KEY_Escape, 0, 0))
        app.close.assert_called_once_with()
        app = self.app()
        app.editing = lambda: True                # a text field first gives up the keyboard
        self.assertTrue(ai_popup.AgentsMap.on_key(app, None, Gdk.KEY_Escape, 0, 0))
        app.window.set_focus.assert_called_once_with(None)
        app.close.assert_not_called()

    def test_esc_on_the_board_closes_backspace_does_nothing(self):
        ctrl = Ctrl()
        escape = mock.Mock()
        host = SimpleNamespace(page_name=lambda: "projects", projects_ctrl=ctrl, editing=lambda: False,
                               projects_escape=escape, close_projects=mock.Mock(), toast=mock.Mock(),
                               window=SimpleNamespace(get_visible_dialog=lambda: None))
        self.assertTrue(ProjectsPage._projects_key(host, None, Gdk.KEY_Escape, 0, 0))
        escape.assert_called_once_with()
        self.assertTrue(ProjectsPage._projects_key(host, None, Gdk.KEY_BackSpace, 0, 0))
        escape.assert_called_once_with()
        host.close_projects.assert_not_called()
        ctrl.open = True                          # an open project: back to the board first
        self.assertTrue(ProjectsPage._projects_key(host, None, Gdk.KEY_Escape, 0, 0))
        self.assertEqual(ctrl.back, 1)
        escape.assert_called_once_with()

    def closing_app(self, choice, running=()):
        """The window over the real ProjectsController with an open, edited project."""
        import projects_controller
        asked = []

        def ask(kind, _question, answer):
            asked.append(kind)
            answer(choice)
        ctrl = projects_controller.ProjectsController(lambda *_a: {}, lambda work, done: done(work(), None), ask)
        ctrl.project = {"id": "p1", "title": "Old"}
        ctrl._adopt(ctrl.project)
        ctrl.set_text("title", "Edited")
        panes = [SimpleNamespace(running=True, agent=SimpleNamespace(name="Pi"), stop=mock.Mock()) for _ in running]
        app = SimpleNamespace(booting=False, page_name=lambda: "projects", sessions=dict(enumerate(panes)),
                              hide_window=mock.Mock(), projects_ctrl=ctrl, toast=mock.Mock(), departing=False,
                              ask_close=mock.Mock())
        app.close = lambda: ai_popup.AgentsMap.close(app)
        return app, ctrl, asked

    def test_closing_the_window_from_projects_asks_first(self):
        app, ctrl, asked = self.closing_app("cancel")
        ai_popup.AgentsMap.close(app)
        app.hide_window.assert_not_called()
        self.assertEqual((asked, ctrl.texts["title"]), (["leave"], "Edited"))
        app, ctrl, _asked = self.closing_app("discard")
        ai_popup.AgentsMap.close(app)
        app.hide_window.assert_called_once_with()
        self.assertEqual(ctrl.texts["title"], "Old")

    def test_discard_waits_for_the_running_agents_question(self):
        """Both reviews r1 (P1/HIGH): Discard, then Cancel on "still running here": nothing is lost."""
        app, ctrl, _asked = self.closing_app("discard", running=[1])
        ai_popup.AgentsMap.close(app)
        app.ask_close.assert_called_once()
        self.assertEqual(ctrl.texts["title"], "Edited")         # not discarded yet
        app.close_commit = None                                  # ask_close's Cancel
        app.hide_window.assert_not_called()
        self.assertEqual(ctrl.texts["title"], "Edited")
        app, ctrl, _asked = self.closing_app("discard", running=[1])
        ai_popup.AgentsMap.close(app)
        app.close_asked = True                                   # ask_close's Keep running
        ai_popup.AgentsMap.close(app)
        app.hide_window.assert_called_once_with()
        self.assertEqual(ctrl.texts["title"], "Old")


class ReturnPageTest(unittest.TestCase):
    def test_settings_and_help_return_to_projects_and_reports(self):
        for outer in ("projects", "reports"):
            shown = []
            app = SimpleNamespace(stack=SimpleNamespace(get_visible_child_name=lambda o=outer: o),
                                  show_page=shown.append, show_reports_tab=mock.Mock(), reports_tab="topics")
            settings_pages.SettingsPages.remember_page(app)
            self.assertEqual(app.return_page, outer)
            settings_pages.SettingsPages.back_to_last_page(app)
            self.assertEqual(shown, [outer])
            if outer == "reports":
                app.show_reports_tab.assert_called_once_with("topics")

    def test_show_view_main_on_the_projects_tab_shows_projects(self):
        shown = []
        app = SimpleNamespace(main_tab="projects", show_page=shown.append, update_header=lambda: None, view="main")
        ai_popup.AgentsMap.show_view(app, "main", save=False)
        self.assertEqual(shown, ["projects"])


class ActivityAndConfigTest(unittest.TestCase):
    def test_progress_line(self):
        self.assertEqual(terminal_pane.progress_text({"done": 2, "total": 5, "step": "Tests", "note": None}),
                         "2/5 · Tests")
        self.assertEqual(terminal_pane.progress_text({"done": 1, "total": 3, "step": "Build", "note": "slow"}),
                         "1/3 · Build · slow")

    def test_terminal_activity_button(self):
        pane = SimpleNamespace(on_activity=mock.Mock(), activity_instance="42:99", running=True,
                               agent=SimpleNamespace(command="claude"))
        terminal_pane.TerminalPane.show_activity(pane)
        pane.on_activity.assert_called_once_with("claude", "42:99")
        pane.running = False
        terminal_pane.TerminalPane.show_activity(pane)
        pane.on_activity.assert_called_once()

    def test_provider_config_answers(self):
        """agy r1 #7: "*" (all models) is accepted, and an automatic answer without an area is the
        providers' step."""
        facts = {"providers": [{"key": "llamacpp", "name": "llama.cpp", "configurable": True, "models": []}]}
        found = smart_card.providers_answer({"action": "provider_config", "provider": "llamacpp", "model": "*"},
                                            facts)
        self.assertEqual(found["candidates"][0]["model"], "*")
        found = smart_card.auto_answer({"action": "provider_config", "provider": "llamacpp"}, facts)
        self.assertEqual(found["candidates"][0]["kind"], "provider_config")

    def test_refused_configuration_reads_nothing(self):
        import askai_page
        import provider_config
        app = SimpleNamespace(inventory=SimpleNamespace(runtimes=[SimpleNamespace(key="llamacpp", installed=True)]))
        result = {"candidates": [{"kind": "provider_config", "key": "llamacpp", "profile": "fast", "model": None,
                                  "action": {"kind": "provider_config", "key": "llamacpp", "model": None}}]}
        with mock.patch.object(provider_config, "collect") as collect, \
                mock.patch("action_rules.allows", return_value=False):
            askai_page.AskAiPage.ask_provider_values(app, None, result, "q", lambda _t: None,
                                                     SimpleNamespace(is_set=lambda: False))
        collect.assert_not_called()

    def test_provider_dictation_stops_on_leaving(self):
        """Codex r1 #3/#8: the start runs on a worker; leaving drops a transcription on its way and
        turns the mic off."""
        import provider_config_page
        page = provider_config_page.ProviderConfigPage
        runs = []
        recorder = SimpleNamespace(start=lambda: None, stop=lambda: "late words", cancel=mock.Mock())
        entry = SimpleNamespace(text="", get_text=lambda: entry.text, grab_focus=lambda: None,
                                set_text=lambda t: setattr(entry, "text", t))
        button = SimpleNamespace(active=True, sensitive=True)
        button.get_active = lambda: button.active
        button.set_active = lambda v: setattr(button, "active", v)
        button.set_sensitive = lambda v: setattr(button, "sensitive", v)
        app = SimpleNamespace(page_name=lambda: "providerconfig", toast=mock.Mock())
        for name in ("provider_config_dictate", "provider_config_mic_reset", "stop_provider_config_dictation"):
            setattr(app, name, getattr(page, name).__get__(app))
        app._cancel_provider_dictation = page._cancel_provider_dictation
        with mock.patch("dictation.Dictation", return_value=recorder), \
                mock.patch.object(provider_config_page.threading, "Thread",
                                  side_effect=lambda target, args=(), **_k: SimpleNamespace(
                                      start=lambda: runs.append((target, args)))), \
                mock.patch.object(provider_config_page.GLib, "idle_add", lambda fn, *a: fn(*a)):
            app.provider_config_dictate(button, entry, "superwhisper")
            target, args = runs.pop()
            target(*args)                                  # the worker's start
            button.active = False
            app.provider_config_dictate(button, entry, "superwhisper")   # Stop: the transcription runs
            stop, args = runs.pop()
            app.stop_provider_config_dictation()           # the page is left meanwhile
            stop(*args)
        self.assertEqual(entry.text, "")                   # the late words are dropped
        button.active = True
        app.provider_config_mic = button
        app.stop_provider_config_dictation()
        self.assertFalse(button.active)                    # the mic is off again

    def test_folded_box_is_not_on_the_map(self):
        import map_view
        view = SimpleNamespace(boxes={"agent:a": (0, 0, 10, 10)}, folded={"agent:a"}, zoom=1.0)
        view.to_pixels = lambda rect: map_view.MapView.to_pixels(view, rect)
        self.assertIsNone(map_view.MapView.box_rect(view, "agent:a"))
        view.folded = set()
        self.assertEqual(map_view.MapView.box_rect(view, "agent:a"), (0, 0, 10, 10))

    def test_ask_ai_routes_to_configure_for_this_pc(self):
        facts = {"providers": [{"key": "llamacpp", "name": "llama.cpp", "configurable": True,
                                "models": ["qwen.gguf"]},
                               {"key": "jan", "name": "Jan", "configurable": False, "models": []}]}
        found = smart_card.providers_answer({"action": "provider_config", "provider": "llamacpp",
                                             "profile": "fast", "focus": "speed"}, facts)
        candidate = found["candidates"][0]
        self.assertEqual((candidate["kind"], candidate["action"]["key"], candidate["profile"]),
                         ("provider_config", "llamacpp", "fast"))
        for bad in ({"provider": "jan"}, {"provider": "llamacpp", "profile": "turbo"},
                    {"provider": "llamacpp", "model": "other.gguf"}):
            with self.assertRaises(smart_card.SmartError):
                smart_card.providers_answer({"action": "provider_config", **bad}, facts)
        import action_rules
        self.assertEqual(action_rules.candidate_checks(candidate), [("update", "providers", "llamacpp")])
        import askai_page
        self.assertEqual(askai_page.review_label(candidate), "Open Configuration")
        self.assertEqual(askai_page.review_label({**candidate, "prefill": {"values": {}}}), "Review configuration")

    def test_review_opens_the_setup_page_filled_in(self):
        import askai_page
        rt = SimpleNamespace(key="llamacpp", installed=True)
        app = SimpleNamespace(rule_allows=lambda *_a: True, close_ask_ai=mock.Mock(),
                              open_provider_config=mock.Mock(), inventory=SimpleNamespace(runtimes=[rt]))
        prefill = {"values": {"ctx": 8192}, "summary": "s"}
        app.ask_proposals = SimpleNamespace(get=lambda *_a: {
            "kind": "provider_config", "key": "llamacpp",
            "action": {"kind": "provider_config", "key": "llamacpp", "model": "*", "profile": "fast",
                       "prefill": prefill}})
        askai_page.AskAiPage.apply_candidate(app, "p", "c1")
        app.open_provider_config.assert_called_once_with(rt, "*", "fast", prefill=prefill)


if __name__ == "__main__":
    unittest.main()
