"""Native page handoffs and lifecycle without constructing display widgets."""
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
import ai_popup
import connect_page
import guide_navigation as navigation
import models_page
import addons_page
import setup_page


class OriginTest(unittest.TestCase):
    def test_direct_and_nested_guided_origins(self):
        for page, previous, expected in (("firstagent", None, "firstagent"),
                                         ("addons", "firstagent", "firstagent"),
                                         ("addons", "models", None),
                                         ("main", "firstagent", None)):
            app = types.SimpleNamespace(page_name=lambda page=page: page, return_page=previous)
            self.assertEqual(navigation.guide_origin(app), expected)

    def test_missing_optional_host_hooks_keep_ordinary_navigation(self):
        app = types.SimpleNamespace()
        self.assertIsNone(navigation.guide_origin(app))
        self.assertFalse(navigation.resume_guide(app, {"origin_page": "firstagent"}))
        navigation.abandon_read(app)
        navigation.abandon_handoffs(app)

    def test_explicit_header_navigation_discards_only_guided_provenance(self):
        events = []
        app = types.SimpleNamespace(
            view="setup", return_page="firstagent",
            installing={"origin_page": "firstagent", "job": "running-install", "from": "setup"},
            connecting={"origin_page": "firstagent", "from": "main"},
            models_state={"origin_page": "firstagent", "job": "running-download"},
            updating_buttons=False, page_name=lambda: "firstagent",
            open_my_setup=lambda: events.append("setup"), update_header=lambda: None,
            first_resume=lambda: self.fail("Map must not reopen the guide"),
        )
        ai_popup.AgentsMap.on_view_button(app, None, "setup")
        self.assertEqual(events, ["setup"])
        self.assertEqual(app.installing, {"job": "running-install", "from": "setup"})
        self.assertEqual(app.models_state, {"job": "running-download"})
        self.assertIsNone(app.return_page)

    def test_map_header_from_guided_connection_does_not_resume_guide(self):
        events = []
        app = types.SimpleNamespace(
            view="setup", updating_buttons=False, page_name=lambda: "connect",
            connecting={"from": "setup", "origin_page": "firstagent"},
            first_resume=lambda: self.fail("Map is not a Back action"),
            show_view=lambda name, save=False: events.append(name), update_header=lambda: None,
        )
        app.close_connect = lambda **kwargs: connect_page.ConnectPage.close_connect(app, **kwargs)
        ai_popup.AgentsMap.on_view_button(app, None, "main")
        self.assertIsNone(app.connecting)
        self.assertEqual(events[-1], "main")

    def test_map_header_cleans_special_connection_without_special_returns(self):
        for marker in ({"security": True, "agent": "fixture"},
                       {"skill_update": True, "server": "fixture"}, {"page": "smartcheck"}):
            with self.subTest(marker=marker):
                events = []
                app = types.SimpleNamespace(
                    view="setup", updating_buttons=False, page_name=lambda: "connect",
                    connecting={"from": "setup", **marker}, update_header=lambda: None,
                    open_agent_status=mock.Mock(), open_mcp_details=mock.Mock(),
                    show_smart_check_again=mock.Mock(),
                    show_view=lambda name, save=False: events.append(name),
                )
                app.close_connect = lambda **kwargs: connect_page.ConnectPage.close_connect(app, **kwargs)
                ai_popup.AgentsMap.on_view_button(app, None, "main")
                self.assertIsNone(app.connecting)
                self.assertEqual(events, ["main"])
                app.open_agent_status.assert_not_called()
                app.open_mcp_details.assert_not_called()
                app.show_smart_check_again.assert_not_called()

    def test_internal_return_through_main_keeps_active_guided_page_origin(self):
        app = types.SimpleNamespace(
            view="main", connecting={"origin_page": "firstagent"}, return_page="connect",
            show_page=lambda name: None, update_header=lambda: None,
        )
        ai_popup.AgentsMap.show_view(app, "main", save=False)
        self.assertEqual(app.connecting["origin_page"], "firstagent")
        self.assertEqual(app.return_page, "connect")

    def test_native_setup_recovery_is_an_explicit_destination(self):
        app = types.SimpleNamespace(
            connecting={"origin_page": "firstagent"}, return_page="firstagent",
            with_addons=lambda: False, fill_setup=lambda: None, show_view=lambda *args, **kwargs: None,
        )
        setup_page.SetupPage.open_my_setup(app, focus={"agents"})
        self.assertNotIn("origin_page", app.connecting)
        self.assertIsNone(app.return_page)
        self.assertEqual(app.setup_focus, {"agents"})


class CloseTest(unittest.TestCase):
    def app(self, field, state):
        events = []
        app = types.SimpleNamespace(view="main", **{field: state})
        app.first_resume = lambda: events.append(("guide", getattr(app, field)))
        app.show_view = lambda name, save=False: events.append((name, getattr(app, field)))
        return app, events

    def test_install_back_cleans_page_then_resumes_guide(self):
        app, events = self.app("installing", {"from": "setup", "origin_page": "firstagent", "busy": True, "job": "keep-running"})
        ai_popup.AgentsMap.close_install(app)
        self.assertEqual(events, [("guide", None)])

    def test_ordinary_install_back_keeps_its_original_view(self):
        app, events = self.app("installing", {"from": "setup"})
        ai_popup.AgentsMap.close_install(app)
        self.assertEqual(events, [("setup", None)])

    def test_connection_cancel_or_apply_returns_after_cleanup(self):
        app, events = self.app("connecting", {"from": "main", "origin_page": "firstagent"})
        connect_page.ConnectPage.close_connect(app)
        self.assertEqual(events, [("guide", None)])

    def test_special_connection_returns_keep_precedence(self):
        for marker, callback, argument in (({"security": True, "agent": "fixture"}, "open_agent_status", "fixture"),
                                            ({"skill_update": True, "server": "fixture-server"}, "open_mcp_details", "fixture-server"),
                                            ({"page": "smartcheck"}, "show_smart_check_again", None)):
            app, events = self.app("connecting", {"origin_page": "firstagent", **marker})
            setattr(app, callback, lambda *args: events.append(("special", args)))
            connect_page.ConnectPage.close_connect(app)
            self.assertEqual(events, [("special", () if argument is None else (argument,))])
            self.assertIsNone(app.connecting)

    def test_models_back_keeps_background_download_and_resumes_guide(self):
        state = {"from": "setup", "origin_page": "firstagent", "busy": True, "job": "download"}
        app, events = self.app("models_state", state)
        models_page.ModelsPage.close_models(app)
        self.assertEqual(events, [("guide", None)])
        self.assertEqual(state["job"], "download")

    def test_addon_detail_back_stays_in_list_before_final_back(self):
        events = []
        class Stack:
            name = "detail"
            def get_visible_child_name(self): return self.name
            def set_visible_child_name(self, name): self.name = name
        app = types.SimpleNamespace(addons_stack=Stack(), back_to_last_page=lambda: events.append("return"))
        addons_page.AddonsPage.close_addons(app)
        self.assertEqual(app.addons_stack.name, "list")
        self.assertEqual(events, [])
        addons_page.AddonsPage.close_addons(app)
        self.assertEqual(events, ["return"])


class BackgroundCompletionTest(unittest.TestCase):
    def test_completed_install_keeps_foreground_and_defers_guide_until_back(self):
        for page in ("settings", "help", "install"):
            with self.subTest(page=page):
                events = []
                app = types.SimpleNamespace(
                    view="main", return_page="install", page_name=lambda: page,
                    installing={"job": "fixture", "origin_page": "firstagent"},
                    install_cancel=mock.Mock(), first_resume=lambda: events.append("guide"),
                    collect=mock.Mock(), refresh_overview=mock.Mock(), refresh_map=mock.Mock(),
                    show_note=mock.Mock(), run_health=mock.Mock(), render_jobs=mock.Mock(),
                    dismiss_done_job=mock.Mock(),
                )
                app.close_install = lambda **kwargs: ai_popup.AgentsMap.close_install(app, **kwargs)
                job = types.SimpleNamespace(id="fixture", state="done")
                with mock.patch.object(ai_popup.GLib, "timeout_add_seconds"):
                    ai_popup.AgentsMap.install_job_ended(app, job, "install", "Fixture")
                self.assertIsNone(app.installing)
                app.collect.assert_called_once_with()
                app.show_note.assert_called_once()
                self.assertEqual(events, ["guide"] if page == "install" else [])
                if page != "install":
                    self.assertEqual(app.return_page, "firstagent")

    def connection_host(self):
        work = []
        events = []
        app = types.SimpleNamespace(
            view="main", return_page="connect", current_page="connect",
            connecting={"updates": [types.SimpleNamespace(appimage="/tmp/fixture.AppImage")],
                        "origin_page": "firstagent"},
            connect_ok=mock.Mock(), connect_status=mock.Mock(),
            first_resume=lambda: events.append("guide"), link_of=lambda _: None,
            current_connect_plan=lambda **_: types.SimpleNamespace(empty=lambda: True),
        )
        app.page_name = lambda: app.current_page
        app.close_connect = lambda **kwargs: connect_page.ConnectPage.close_connect(app, **kwargs)
        app.apply_connect = lambda remove: connect_page.ConnectPage.apply_connect(app, remove)
        thread = lambda *, target, daemon: types.SimpleNamespace(start=lambda: work.append(target))
        return app, work, events, thread

    def test_unpack_completion_in_settings_keeps_foreground(self):
        app, work, events, thread = self.connection_host()
        with mock.patch.object(connect_page.threading, "Thread", side_effect=thread), \
                mock.patch.object(connect_page.mcp_scan, "unpack"), \
                mock.patch.object(connect_page.GLib, "idle_add", side_effect=lambda f, *args: f(*args)):
            app.apply_connect(False)
            app.current_page = "settings"
            work.pop()()
        self.assertIsNone(app.connecting)
        self.assertEqual(app.current_page, "settings")
        self.assertEqual(app.return_page, "firstagent")
        self.assertEqual(events, [])

    def test_old_unpack_completion_does_not_modify_replacement_form(self):
        app, work, events, thread = self.connection_host()
        with mock.patch.object(connect_page.threading, "Thread", side_effect=thread), \
                mock.patch.object(connect_page.mcp_scan, "unpack"), \
                mock.patch.object(connect_page.GLib, "idle_add", side_effect=lambda f, *args: f(*args)):
            app.apply_connect(False)
            replacement = {"from": "main"}
            app.connecting = replacement
            app.connect_ok.reset_mock()
            work.pop()()
        self.assertIs(app.connecting, replacement)
        app.connect_ok.set_sensitive.assert_not_called()
        app.connect_status.set_label.assert_not_called()
        self.assertEqual(events, [])


class LifecycleTest(unittest.TestCase):
    class Stack:
        def set_visible_child_name(self, name): self.name = name
        def get_visible_child(self): return None

    def test_header_and_handoff_page_changes_abandon_only_presentation_reads(self):
        for destination in ("settings", "help", "main", "connect", "install"):
            events = []
            app = types.SimpleNamespace(
                page_name=lambda: "firstagent", first_abandon_read=lambda: events.append("abandon"),
                stack=self.Stack(), right_stack=self.Stack(),
                connect_tidy=lambda: None, place_panes=lambda: None, update_header=lambda: None,
                auto_layers=lambda: None,
            )
            ai_popup.AgentsMap.show_page(app, destination)
            self.assertEqual(events, ["abandon"], destination)

    def test_entering_guide_does_not_cancel_its_new_read(self):
        app = types.SimpleNamespace(
            page_name=lambda: "main", first_abandon_read=lambda: self.fail("new guide check cancelled"),
            stack=self.Stack(), right_stack=self.Stack(), place_panes=lambda: None,
            update_header=lambda: None, auto_layers=lambda: None,
        )
        ai_popup.AgentsMap.show_page(app, "firstagent")

    def test_hiding_guide_abandons_read_without_stopping_work(self):
        events = []
        app = types.SimpleNamespace(
            window=types.SimpleNamespace(get_width=lambda: 540, get_height=lambda: 400,
                                         hide=lambda: events.append("hide")),
            page_name=lambda: "firstagent", first_abandon_read=lambda: events.append("abandon"),
            stop_gpu_poll=lambda: None, map_view=types.SimpleNamespace(set_shift=lambda _: None),
            sessions={}, jobs=types.SimpleNamespace(active=lambda: ["running-job"]),
            quit=lambda: self.fail("running job must remain active"),
        )
        with mock.patch.object(ai_popup.ai_core, "setting", return_value=[540, 400]), \
                mock.patch.object(ai_popup.desktop, "stays_in_background", return_value=False):
            ai_popup.AgentsMap.hide_window(app)
        self.assertEqual(events, ["abandon", "hide"])

    def test_hiding_guided_forms_cleans_up_without_resuming_guide(self):
        for name, field in (("connect", "connecting"), ("install", "installing")):
            with self.subTest(page=name):
                events = []
                app = types.SimpleNamespace(
                    view="main", **{field: {"from": "setup", "origin_page": "firstagent"}},
                    window=types.SimpleNamespace(get_width=lambda: 540, get_height=lambda: 400,
                                                 hide=lambda: events.append("hide")),
                    page_name=lambda: name, first_resume=mock.Mock(),
                    first_abandon_read=lambda: events.append("abandon"),
                    show_view=lambda name, save=False: events.append(name),
                    stop_gpu_poll=lambda: None, map_view=types.SimpleNamespace(set_shift=lambda _: None),
                    sessions={}, jobs=types.SimpleNamespace(active=lambda: ["running-job"]),
                )
                app.close_connect = lambda: connect_page.ConnectPage.close_connect(app)
                app.close_install = lambda: ai_popup.AgentsMap.close_install(app)
                with mock.patch.object(ai_popup.ai_core, "setting", return_value=[540, 400]), \
                        mock.patch.object(ai_popup.desktop, "stays_in_background", return_value=True):
                    ai_popup.AgentsMap.hide_window(app)
                self.assertIsNone(getattr(app, field))
                app.first_resume.assert_not_called()
                self.assertEqual(events, ["setup", "abandon", "hide"])

    def test_hiding_models_or_addons_keeps_guided_return_while_settings_skips_resume(self):
        events = []
        app = types.SimpleNamespace(
            view="main", return_page="firstagent",
            models_state={"from": "main", "origin_page": "firstagent"},
            window=types.SimpleNamespace(get_width=lambda: 540, get_height=lambda: 400,
                                         hide=lambda: events.append("hide")),
            page_name=lambda: "models", first_resume=mock.Mock(),
            first_abandon_read=lambda: events.append("abandon"),
            stop_gpu_poll=lambda: None, map_view=types.SimpleNamespace(set_shift=lambda _: None),
            sessions={}, jobs=types.SimpleNamespace(active=lambda: ["running-job"]),
        )
        with mock.patch.object(ai_popup.ai_core, "setting", return_value=[540, 400]), \
                mock.patch.object(ai_popup.desktop, "stays_in_background", return_value=True):
            ai_popup.AgentsMap.hide_window(app)
        self.assertEqual(app.models_state["origin_page"], "firstagent")
        self.assertEqual(app.return_page, "firstagent")
        app.first_resume.assert_not_called()

        events.clear()
        app.page_name = lambda: "settings"
        app.show_view = lambda name, save=False: events.append(name)
        app.close_settings = lambda: settings_pages.SettingsPages.back_to_last_page(app)
        import settings_pages
        with mock.patch.object(ai_popup.ai_core, "setting", return_value=[540, 400]), \
                mock.patch.object(ai_popup.desktop, "stays_in_background", return_value=True):
            ai_popup.AgentsMap.hide_window(app)
        self.assertIsNone(app.return_page)
        self.assertEqual(app.models_state["origin_page"], "firstagent")
        app.first_resume.assert_not_called()
        self.assertEqual(events, ["main", "abandon", "hide"])


if __name__ == "__main__":
    unittest.main()
