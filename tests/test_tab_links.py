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
                              after_leaving=mock.Mock(), in_search_field=lambda: None)
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
                              ask_close=mock.Mock(), dialog_open=lambda: False, window=None)
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
        dialogs = []

        class Dialog:
            def __init__(self, **_k):
                self.handlers = []
                dialogs.append(self)

            def __getattr__(self, _name):
                return lambda *a, **k: None

            def connect(self, _signal, handler):
                self.handlers.append(handler)
        app.ask_close = lambda running: ai_popup.AgentsMap.ask_close(app, running)
        with mock.patch.object(ai_popup.Adw, "AlertDialog", Dialog):
            ai_popup.AgentsMap.close(app)
            dialogs[0].handlers[0](dialogs[0], "cancel")       # Cancel: nothing discarded, window stays
            self.assertEqual((ctrl.texts["title"], app.close_commit), ("Edited", None))
            app.hide_window.assert_not_called()
            ai_popup.AgentsMap.close(app)
            dialogs[1].handlers[0](dialogs[1], "keep")         # Keep running: closes, now discarded
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


class ReviewRound2Test(unittest.TestCase):
    """The second review round of WP7 (Codex and agy)."""

    def setUp(self):
        self.saved = {}
        patched_settings(self, self.saved)

    def test_show_on_map_scrolls_the_box_into_view(self):
        def adjustment(value, page, upper):
            a = SimpleNamespace(value=value, get_page_size=lambda: page, get_lower=lambda: 0,
                                get_upper=lambda: upper)
            a.get_value = lambda: a.value
            a.set_value = lambda v: setattr(a, "value", v)
            return a
        h, v = adjustment(0, 500, 2000), adjustment(0, 400, 3000)
        host = Host(page="projects")
        host.main_tab = "projects"
        host.side_map.boxes["agent:claude"] = (1200, 1500, 100, 40)
        host.map_scroller = SimpleNamespace(get_hadjustment=lambda: h, get_vadjustment=lambda: v)
        host.show_on_map_link("claude")
        self.assertEqual((h.value, v.value), (1200 + 50 - 250, 1500 + 20 - 200))   # centred on the box
        self.assertEqual(host.side_map.focused, "agent:claude")

    def test_stale_map_is_read_on_a_worker(self):
        """Codex r2 #3: back on the AI Map the deferred map is drawn from facts read off the GTK thread."""
        host = Host(page="projects")
        host.main_tab = "projects"
        host.map_stale = True
        host.refresh_project_summary = lambda then=None: None
        host.select_tab("map")
        self.assertIn(("refresh_map", "fresh"), host.calls)
        self.assertNotIn(("refresh_map",), host.calls)
        self.assertFalse(host.map_stale)

    def test_no_map_scan_before_the_start_tab_is_decided(self):
        """Codex r2 #5 / agy r2 #4: while the summary decides, the map waits; the decision draws it."""
        app = SimpleNamespace(main_tab="map", tab_decided=False, map_stale=False, projects_map_changed=mock.Mock())
        ai_popup.AgentsMap.refresh_map(app)
        self.assertTrue(app.map_stale)
        app.projects_map_changed.assert_called_once_with()
        host = Host()
        host.map_stale = True
        host.tab_decided = True
        host.apply_start_tab("map")
        self.assertIn(("refresh_map", "fresh"), host.calls)

    def light_app(self, seeded):
        app = SimpleNamespace(gpu_reading=False, inventory=SimpleNamespace(runtimes=[]), main_tab="projects",
                              tab_decided=True, installed_commands=lambda: ["claude", "pi"],
                              running_now=lambda pids: {}, working_now=lambda *a: set(),
                              observe_activity=mock.Mock(), working_agents=set(), running={},
                              update_terminal_tabs=mock.Mock(), limits_seeded=seeded, agent_infos={},
                              agent_batteries={}, side_map=SimpleNamespace(set_batteries=lambda b: None),
                              warn_red=mock.Mock(), costs_busy=False)
        for name in ("limits_maintenance", "set_agent_battery", "show_batteries", "update_costs",
                     "batteries_past_reset"):
            setattr(app, name, getattr(ai_popup.AgentsMap, name).__get__(app))
        return app

    def test_start_on_projects_watches_limits_and_costs(self):
        """Codex r3 #1 / agy r3 #1: started on Projects (no map drawn), the first poll reads every agent's
        limits on its worker; a red limit is noticed and paying agents are known for the daily cost."""
        import datetime as dt
        import agent_details
        red = agent_details.Details(limits=[agent_details.Limit("5h", 0.97, dt.datetime.now(dt.timezone.utc)
                                                                + dt.timedelta(hours=1))])
        paying = agent_details.Details()
        details = {"claude": red, "pi": paying}
        import settings_schema
        self.saved.update({key: setting.default for key, setting in settings_schema.SCHEMA.items()})
        app = self.light_app(seeded=False)
        threads = []
        with mock.patch.object(ai_popup.threading, "Thread",
                               side_effect=lambda target, **_k: SimpleNamespace(start=lambda: threads.append(target))), \
                mock.patch.object(ai_popup.GLib, "idle_add", lambda fn, *a: fn(*a)), \
                mock.patch.object(ai_popup.agent_details, "usage_stamps", return_value={"claude": 1}), \
                mock.patch.object(ai_popup.agent_details, "details_for", side_effect=lambda c: details[c]), \
                mock.patch.object(ai_popup.agent_status, "pays_per_token", side_effect=lambda c, i: c == "pi"), \
                mock.patch.object(ai_popup.gpu_usage, "snapshot") as snapshot:
            ai_popup.AgentsMap.poll_gpu(app)
            threads.pop(0)()                           # the poll's worker
        self.assertEqual(set(app.agent_infos), {"claude", "pi"})
        self.assertTrue(app.limits_seeded)
        self.assertIn("agent:claude", app.agent_batteries)
        self.assertEqual(app.warn_red.call_args[0][1], "claude")   # the red limit is notified at once
        self.assertTrue(threads)                       # the daily-cost look started (paying agent pi)
        snapshot.assert_not_called()

    def test_light_poll_keeps_the_limit_notifications(self):
        """Codex r2 #4: on Projects the poll stays light but renewed limits and costs are still checked."""
        app = SimpleNamespace(gpu_reading=False, inventory=SimpleNamespace(runtimes=[]), main_tab="projects",
                              tab_decided=True, installed_commands=lambda: ["claude"],
                              running_now=lambda pids: {"claude": 1}, working_now=lambda *a: set(),
                              observe_activity=mock.Mock(), working_agents=set(), running={"claude": 1},
                              limits_maintenance=mock.Mock(), update_terminal_tabs=mock.Mock())
        renewed = {"claude": SimpleNamespace(limits=[])}
        with mock.patch.object(ai_popup.threading, "Thread",
                               side_effect=lambda target, **_k: SimpleNamespace(start=target)), \
                mock.patch.object(ai_popup.GLib, "idle_add", lambda fn, *a: fn(*a)), \
                mock.patch.object(ai_popup.agent_details, "usage_stamps", return_value={"claude": 2}), \
                mock.patch.object(ai_popup.agent_details, "renewed_records", return_value=["claude"]), \
                mock.patch.object(ai_popup.agent_details, "details_for", side_effect=lambda c: renewed[c]), \
                mock.patch.object(ai_popup.gpu_usage, "snapshot") as snapshot:
            ai_popup.AgentsMap.poll_gpu(app)
        app.limits_maintenance.assert_called_once_with({"claude": 2}, renewed)
        snapshot.assert_not_called()                      # no GPU read without the activity log
        app.observe_activity.assert_called_once()

    def test_fingerprint_counts_connections_and_setup(self):
        """Codex r2 #6 / agy r2 #2: a line drawn to a running LLM (same models) is a discovery change."""
        host = Host()
        runtime = SimpleNamespace(key="ollama", installed=True, running=True, models=["qwen"], agents=[],
                                  address="127.0.0.1:11434")
        host.inventory = SimpleNamespace(runtimes=[runtime])
        before = host.discovery_fingerprint()
        runtime.agents = ["pi"]
        self.assertNotEqual(host.discovery_fingerprint(), before)
        before = host.discovery_fingerprint()
        host.models_stamp = ("models", "installs", "setup changed")
        self.assertNotEqual(host.discovery_fingerprint(), before)

    def test_close_waits_for_an_open_dialog(self):
        """agy r2 #1: the close button under an open question does nothing (no second question)."""
        app = SimpleNamespace(booting=False, close_asked=False, dialog_open=lambda: True, page_name=lambda: "main",
                              sessions={}, hide_window=mock.Mock(), ask_close=mock.Mock())
        ai_popup.AgentsMap.close(app)
        app.hide_window.assert_not_called()
        app.close_asked = True                     # the question's own Keep running / Stop and close
        ai_popup.AgentsMap.close(app)
        app.hide_window.assert_called_once_with()
        source = Path(ai_popup.__file__).read_text()
        hide = source[source.index("    def hide_window(self):"):source.index("\ndef main():")]
        self.assertIn("self.close_commit = None", hide)

    def test_search_fields_escape(self):
        """agy r2 #3 / r3 #2: Esc in any search field (Settings, Reports › Library, Add-ons, Add MCP
        server, Models) clears a search first, then gives up the keyboard, then goes back."""
        for page in ("settings", "reports", "addons", "addmcp", "models"):
            entry = SimpleNamespace(text="map")
            entry.get_text = lambda e=entry: e.text
            entry.set_text = lambda t, e=entry: setattr(e, "text", t)
            back = mock.Mock()
            app = KeysTest.app(None, page=page)
            app.back_actions = lambda b=back, p=page: {p: b}
            app.editing = lambda: True
            app.in_search_field = lambda e=entry: e
            self.assertTrue(ai_popup.AgentsMap.on_key(app, None, Gdk.KEY_Escape, 0, 0))
            self.assertEqual(entry.text, "")
            app.window.set_focus.assert_not_called()
            self.assertTrue(ai_popup.AgentsMap.on_key(app, None, Gdk.KEY_Escape, 0, 0))
            app.window.set_focus.assert_called_once_with(None)
            back.assert_not_called()

    def test_old_map_read_does_not_overwrite_newer_facts(self):
        """Codex r3 #2: a background read that ends after a newer collect and drawing is dropped."""
        host = Host(page="projects")
        host.main_tab = "projects"
        host.tab_decided = True
        host.map_stale = True
        workers = []
        with mock.patch.object(main_tabs.threading, "Thread",
                               side_effect=lambda target, args=(), **_k: SimpleNamespace(
                                   start=lambda: workers.append(target))):
            host.refresh_project_summary = lambda then=None: None
            host.select_tab("map")                     # the deferred map: a read starts
            host.map_generation = getattr(host, "map_generation", 0) + 1   # a connection made: collected anew
            workers.pop()()                            # the old read ends now
        self.assertNotIn(("refresh_map", "fresh"), host.calls)

    def test_stale_proposal_model_opens_the_page_with_a_note(self):
        """Codex r3 #3: Review configuration whose model went away: the page opens on a model that
        exists, with a message, instead of staying empty."""
        import provider_config_page
        page = provider_config_page.ProviderConfigPage
        calls = []
        app = SimpleNamespace(provider_config_busy=False, provider_config_cancel_timer=lambda: None,
                              provider_config_body=SimpleNamespace(set_sensitive=lambda v: None),
                              provider_config_progress=SimpleNamespace(set_visible=lambda v: None),
                              page_name=lambda: "main", provider_config_generation=0, show_page=lambda n: None,
                              clear_box=lambda b: None, card_text=lambda *a: None,
                              note_card=lambda *a: calls.append(("note",) + a[1:]),
                              show_error=lambda text: calls.append(("error", text)),
                              render_provider_config=lambda: calls.append(("render",)))

        def draft(facts, model=None, profile="balanced"):
            if model == "gone":
                raise ValueError("Choose an available model")
            return SimpleNamespace(model=model or "kept", fill_proposal=mock.Mock())
        with mock.patch.object(provider_config_page, "Draft", side_effect=draft), \
                mock.patch.object(provider_config_page.config, "collect", return_value={}), \
                mock.patch.object(provider_config_page.threading, "Thread",
                                  side_effect=lambda target, **_k: SimpleNamespace(start=target)), \
                mock.patch.object(provider_config_page.GLib, "idle_add", lambda fn, *a: fn(*a)):
            page.open_provider_config(app, SimpleNamespace(key="lmstudio"), "gone", "fast", prefill={"values": {}})
        self.assertEqual(app.provider_config_draft.model, "kept")
        app.provider_config_draft.fill_proposal.assert_not_called()
        self.assertIn(("render",), calls)
        self.assertTrue(any(c[0] == "error" for c in calls))

    def test_unusable_answer_keeps_open_configuration(self):
        """agy r3 #3: no usable values: the proposal stays, Review is Open Configuration (the manual)."""
        import askai_page
        import provider_config
        app = SimpleNamespace(inventory=SimpleNamespace(runtimes=[SimpleNamespace(key="llamacpp", installed=True)]))
        result = {"status": "proposal", "candidates": [
            {"kind": "provider_config", "key": "llamacpp", "profile": "fast", "model": None,
             "action": {"kind": "provider_config", "key": "llamacpp", "model": None}}]}
        with mock.patch.object(provider_config, "collect", return_value={}), \
                mock.patch.object(provider_config, "select_model", return_value="*"), \
                mock.patch.object(provider_config, "ask", side_effect=ValueError("Invalid configuration answer")), \
                mock.patch("action_rules.allows", return_value=True):
            askai_page.AskAiPage.ask_provider_values(app, None, result, "q", lambda _t: None,
                                                     SimpleNamespace(is_set=lambda: False))
        candidate = result["candidates"][0]
        self.assertEqual((result["status"], candidate["model"], askai_page.review_label(candidate)),
                         ("proposal", "*", "Open Configuration"))
        facts = {"providers": [{"key": "ollama", "name": "Ollama", "configurable": True, "models": ["qwen"],
                                "config_scope": "global"}]}
        with self.assertRaises(smart_card.SmartError):        # global settings: no single model
            smart_card.providers_answer({"action": "provider_config", "provider": "ollama", "model": "qwen"}, facts)

    def test_map_facts_carry_their_stamp(self):
        """agy r3 #4: a drawing from worker facts takes their models_stamp (no second scan on the next poll)."""
        app = SimpleNamespace(main_tab="map", tab_decided=True)
        with mock.patch.object(ai_popup.agents, "list_agents", return_value=[]), \
                mock.patch.object(ai_popup.inventory, "collect", return_value=SimpleNamespace(runtimes=[])), \
                mock.patch.object(ai_popup.inventory, "models_stamp", return_value="m"), \
                mock.patch.object(ai_popup.agents, "install_stamp", return_value="i"), \
                mock.patch.object(ai_popup.agent_details, "setup_stamp", return_value="s"):
            facts = ai_popup.AgentsMap.read_map_facts()
        self.assertEqual(facts[3], ("m", "i", "s"))

    def test_all_models_only_where_the_scope_allows(self):
        """Codex r2 #1: "*" for a per-model provider is refused; invalid candidates are not offered."""
        facts = {"providers": [{"key": "lmstudio", "name": "LM Studio", "configurable": True, "models": ["m"],
                                "config_scope": "per_model"},
                               {"key": "ollama", "name": "Ollama", "configurable": True, "models": [],
                                "config_scope": "global"}]}
        with self.assertRaises(smart_card.SmartError):
            smart_card.providers_answer({"action": "provider_config", "provider": "lmstudio", "model": "*"}, facts)
        found = smart_card.providers_answer({"action": "provider_config", "provider": "ollama", "model": "*"}, facts)
        self.assertEqual(found["candidates"][0]["model"], "*")
        import askai_page
        import provider_config
        app = SimpleNamespace(inventory=SimpleNamespace(runtimes=[SimpleNamespace(key="lmstudio", installed=True)]))
        result = {"status": "proposal", "candidates": [
            {"kind": "provider_config", "key": "lmstudio", "profile": "fast", "model": "*",
             "action": {"kind": "provider_config", "key": "lmstudio", "model": "*"}}]}
        with mock.patch.object(provider_config, "collect", return_value={}), \
                mock.patch.object(provider_config, "select_model", side_effect=ValueError("Choose an available model")), \
                mock.patch("action_rules.allows", return_value=True):
            askai_page.AskAiPage.ask_provider_values(app, None, result, "q", lambda _t: None,
                                                     SimpleNamespace(is_set=lambda: False))
        self.assertEqual((result["candidates"], result["status"]), ([], "error"))

    def test_rebuilding_configuration_stops_dictation(self):
        """Codex r2 #2: a new form (profile, model, Reset) stops the recording behind the old mic."""
        import provider_config_page
        app = SimpleNamespace(provider_config_cancel_timer=lambda: None, provider_config_draft=None,
                              stop_provider_config_dictation=mock.Mock())
        provider_config_page.ProviderConfigPage.render_provider_config(app)
        app.stop_provider_config_dictation.assert_called_once_with()


class ReviewRound4Test(unittest.TestCase):
    """The fourth review round of WP7 (Codex and agy)."""

    def setUp(self):
        self.saved = {}
        patched_settings(self, self.saved)

    def test_closing_never_quits_while_a_project_runs(self):
        """Codex r4 #1: a project started since the last summary still keeps the program running."""
        for active, quits in ((True, False), (False, True)):
            app = SimpleNamespace(window=SimpleNamespace(get_width=lambda: 0, get_height=lambda: 0, hide=mock.Mock()),
                                  stop_gpu_poll=lambda: None, map_view=SimpleNamespace(set_shift=lambda _h: None),
                                  page_name=lambda: "main", sessions={}, jobs=SimpleNamespace(active=lambda: []),
                                  quit=mock.Mock(), project_summary={"connected": True, "running": 0},
                                  projects_ctrl=SimpleNamespace(disposed=False, has_active=active))
            with mock.patch.object(ai_popup.desktop, "stays_in_background", return_value=False), \
                    mock.patch.object(ai_popup.guide_navigation, "abandon_read", lambda _a: None), \
                    mock.patch.object(ai_popup.ai_core, "setting", return_value=None):
                ai_popup.AgentsMap.hide_window(app)
            self.assertEqual(app.quit.called, quits)

    def test_newest_map_request_wins(self):
        """Codex r4 #2: a tab refresh still reading when Show on map asks again does not win."""
        host = Host(page="projects")
        host.main_tab = "projects"
        host.tab_decided = True
        host.map_stale = True
        workers = []
        with mock.patch.object(main_tabs.threading, "Thread",
                               side_effect=lambda target, args=(), **_k: SimpleNamespace(
                                   start=lambda: workers.append(target))):
            host.refresh_project_summary = lambda then=None: None
            host.select_tab("map")                     # the older read (the tab)
            host.select_tab("projects")
            host.fresh_facts = ([SimpleNamespace(command="claude", name="Claude Code", installed=True)], None, {})
            host.show_on_map_link("claude")            # the newer read (the link)
            old, new = workers
            old()                                      # the older one ends first: dropped
            self.assertEqual([c for c in host.calls if c[0] == "refresh_map"], [])
            new()
        self.assertEqual([c for c in host.calls if c[0] == "refresh_map"], [("refresh_map", "fresh")])
        self.assertEqual(host.side_map.focused, "agent:claude")

    def test_failed_map_read_stays_off_the_gtk_thread(self):
        """Codex r4 #3: a failed read keeps the map stale, says so and retries on a worker."""
        host = Host(page="projects")
        host.main_tab = "projects"
        retries = []

        def broken():
            raise OSError("no /proc")
        host.read_map_facts = broken
        with mock.patch.object(main_tabs.GLib, "timeout_add_seconds", lambda _s, fn, *a: retries.append(fn) or 1):
            host.show_on_map_link("claude")
        self.assertNotIn(("refresh_map",), host.calls)          # no synchronous scan
        self.assertTrue(host.map_stale)
        self.assertIn("could not be read", host.toasts[-1])
        host.read_map_facts = lambda: Host.fresh_facts
        retries[0]()                                            # the retry: on a worker, then drawn
        self.assertIn(("refresh_map", "fresh"), host.calls)

    def test_refocus_refreshes_the_board_once(self):
        """Codex r4 #4: back in front, the next project list looks at agents and runtimes again."""
        host = Host(page="projects")
        host._projects_closed = False
        host.projects_ctrl.refresh = mock.Mock()
        host.projects_refocused()
        host.projects_ctrl.refresh.assert_called_once_with(profiles=True)   # agy r5: Team and Setup choices too
        import projects_page
        import threading
        center = SimpleNamespace(bump_discovery=mock.Mock(), call=lambda op, params: {"projects": []})
        host._project_center, host._project_center_lock = center, threading.Lock()
        projects_page.ProjectsPage._project_call(host, "list", {})
        projects_page.ProjectsPage._project_call(host, "list", {})
        center.bump_discovery.assert_called_once_with()          # consumed once

    def test_summary_poll_follows_the_window(self):
        """Codex r4 nit: the counts are read every 10 s while shown, not while hidden, again on reopen."""
        host = Host()
        host._projects_closed = False
        timers, removed = [], []
        with mock.patch.object(main_tabs.GLib, "timeout_add_seconds",
                               lambda secs, fn, *a: timers.append((secs, fn)) or len(timers)), \
                mock.patch.object(main_tabs.GLib, "source_remove", removed.append):
            host.start_summary_poll()
            host.start_summary_poll()                  # once only
            host.stop_summary_poll()                   # hidden
            host.start_summary_poll()                  # shown again
        self.assertEqual(([t[0] for t in timers], removed), ([main_tabs.SUMMARY_SECONDS] * 2, [1]))
        host.refresh_project_summary = mock.Mock()
        self.assertTrue(timers[1][1]())                # a tick reads the counts
        host.refresh_project_summary.assert_called_once_with()

    def test_closing_asks_about_a_report_being_generated(self):
        """Codex r5 #1: closing while a topic report is generated asks "Cancel the report?" first; Back
        keeps the window, Cancel report closes."""
        answers = []
        app = SimpleNamespace(booting=False, close_asked=False, dialog_open=lambda: False, page_name=lambda: "reports",
                              sessions={}, hide_window=mock.Mock(), reports_running=object())
        app.confirm_leave_reports = lambda then, stay=None: answers.append(then)
        app.close = lambda: ai_popup.AgentsMap.close(app)
        ai_popup.AgentsMap.close(app)
        app.hide_window.assert_not_called()             # asked, not closed (Back: nothing more happens)
        answers[0]()                                     # Cancel report
        app.hide_window.assert_called_once_with()

    def test_failed_map_reads_back_off_and_stop(self):
        """agy r5 nit: retries are tracked, back off, note once, and end when the map is left or hidden."""
        host = Host()
        timers = []
        with mock.patch.object(main_tabs.GLib, "timeout_add_seconds",
                               lambda secs, fn, *a: timers.append(secs) or len(timers)), \
                mock.patch.object(main_tabs.GLib, "source_remove", lambda _id: timers.append("removed")):
            host.map_read_failed()
            host.map_read_failed()
            host.map_read_failed()
            host.forget_map_mark()
        self.assertEqual([t for t in timers if t != "removed"], [5, 10, 20])
        self.assertEqual(timers[-1], "removed")
        self.assertEqual(len(host.toasts), 1)

    def test_map_chosen_before_the_start_decision_is_drawn(self):
        """agy r4 #1: AI Map, Ask AI or Problems chosen while the summary decides draws the waiting map."""
        for choose in (lambda h: h.select_tab("map"), lambda h: h.to_map_first(lambda: None)):
            host = Host()
            host.tab_decided = False
            host.map_stale = True
            host.refresh_project_summary = lambda then=None: None
            choose(host)
            self.assertIn(("refresh_map", "fresh"), host.calls)
            self.assertFalse(host.map_stale)


@unittest.skipUnless(__import__("shutil").which("gtk4-broadwayd"), "needs gtk4-broadwayd for a headless GTK build")
class HeaderNarrowTest(unittest.TestCase):
    """Codex r6: at narrow widths Reports, Help, Settings and Refresh move into More; nothing is cut off
    (a real GTK allocation under a headless Broadway display, isolated HOME)."""

    def test_header_fits_narrow_and_wide(self):
        import os
        import subprocess
        import tempfile
        import time
        root = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as home:
            runtime = Path(home) / "run"
            runtime.mkdir(mode=0o700)
            env = {k: v for k, v in os.environ.items()
                   if k not in ("DISPLAY", "WAYLAND_DISPLAY", "XDG_CURRENT_DESKTOP", "HYPRLAND_INSTANCE_SIGNATURE",
                                "KDE_FULL_SESSION")}
            display = f":{240 + os.getpid() % 200}"
            env.update(HOME=home, XDG_RUNTIME_DIR=str(runtime), XDG_CONFIG_HOME=f"{home}/.config",
                       XDG_STATE_HOME=f"{home}/.local/state", XDG_CACHE_HOME=f"{home}/.cache",
                       GDK_BACKEND="broadway", BROADWAY_DISPLAY=display, GSK_RENDERER="cairo")
            daemon = subprocess.Popen(["gtk4-broadwayd", display], env=env, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL)
            try:
                time.sleep(0.5)
                done = subprocess.run([sys.executable, str(root / "tests" / "header_smoke.py")], env=env,
                                      capture_output=True, text=True, timeout=420, cwd=str(root))
            finally:
                daemon.terminate()
                daemon.wait(5)
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            self.assertIn("HEADER OK", done.stdout)


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
        facts = {"providers": [{"key": "llamacpp", "name": "llama.cpp", "configurable": True, "models": [],
                                "config_scope": "default"}]}
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
