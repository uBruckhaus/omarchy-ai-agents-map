"""Guide persistence and asynchronous lifecycle; no GTK display or real settings required."""
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import gi
gi.require_version("Gtk", "4.0")
from first_agent_state import FirstAgentState, READ_ERROR, CHOICE_ERROR, SAVE_ERROR, valid_steps
import first_agent_page as page
import first_agent
import gpu_stack
import install_ctl


class Scheduler:
    def __init__(self):
        self.timers, self.idles, self.threads = {}, [], []
        self.next = 1

    def timeout_add(self, _ms, callback):
        token = self.next
        self.next += 1
        self.timers[token] = callback
        return token

    def source_remove(self, token):
        self.timers.pop(token, None)

    def idle_add(self, callback, *args):
        self.idles.append((callback, args))

    def thread(self, *, target, daemon):
        return types.SimpleNamespace(start=lambda: self.threads.append(target))

    def work(self, index=0):
        self.threads.pop(index)()

    def dispatch(self):
        while self.idles:
            callback, args = self.idles.pop(0)
            callback(*args)

    def expire(self, token):
        self.timers.pop(token)()


class Host(page.FirstAgentPage):
    def __init__(self):
        self.active = "firstagent"
        self.view = "main"
        self.addons = []
        self.empty_area = mock.Mock()
        self.fills = self.collections = self.overviews = self.maps = 0

    def page_name(self):
        return self.active

    def show_page(self, name):
        self.active = name

    def show_view(self, name, save=False):
        self.active = name

    def fill_first_agent(self):
        self.fills += 1

    def collect(self):
        self.collections += 1

    def refresh_overview(self):
        self.overviews += 1

    def refresh_map(self):
        self.maps += 1


class Box:
    def __init__(self):
        self.children = []

    def append(self, value):
        self.children.append(value)


class GuideWidget(Box):
    def __init__(self, **properties):
        super().__init__()
        self.__dict__.update({"visible": True, "sensitive": True, "callback": None, **properties})

    def get_child(self):
        return self.child

    def set_sensitive(self, value):
        self.sensitive = value

    def set_visible(self, value):
        self.visible = value

    def add_css_class(self, _value):
        pass

    def set_wrap(self, value):
        self.wrap = value

    def set_wrap_mode(self, _value):
        pass

    def set_max_width_chars(self, _value):
        pass


def action_button(label, on_click, **_kwargs):
    return GuideWidget(child=GuideWidget(label=label), callback=on_click)


class PageRenderingTest(unittest.TestCase):
    def setUp(self):
        self.host = Host()
        self.host.first_body = Box()
        self.host.clear_box = lambda box: box.children.clear()
        self.host.note = lambda box, text: box.append(text)
        self.host.action_button = action_button
        self.host.open_addons = mock.Mock()
        self.host.open_my_setup = mock.Mock()
        self.host.first_refresh = mock.Mock()
        self.host.first_retry = mock.Mock()
        self.host.first_hw = self.host.first_catalog = object()
        self.host.first_route = mock.Mock(return_value=object())
        self.host.first_steps = mock.Mock(side_effect=ValueError("invalid checklist"))
        self.patch = mock.patch.multiple(page.Gtk, Box=GuideWidget, Label=GuideWidget,
                                         Spinner=GuideWidget, FlowBox=GuideWidget)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def render(self):
        page.FirstAgentPage.fill_first_agent(self.host)
        return self.host.first_body.children

    def buttons(self):
        return [button for flow in self.host.first_body.children if isinstance(flow, GuideWidget)
                for button in flow.children if button.callback is not None]

    def test_timed_out_save_keeps_recovery_navigation_and_hides_retry_until_writer_exits(self):
        state = self.host.first_state()
        token = state.begin_save({"route": "new"})
        state.save_timeout(token)
        self.host.first_steps = mock.Mock(return_value=[("Test", False, "", "", None, "")])
        self.host.fill_first_steps = mock.Mock()
        self.render()
        buttons = {button.child.label: button for button in self.buttons()}
        for label, callback in (("Add-ons", self.host.open_addons), ("Setup", self.host.open_my_setup)):
            self.assertTrue(buttons[label].sensitive)
            self.assertTrue(buttons[label].child.wrap)
            buttons[label].callback()
            callback.assert_called_once_with()
        self.assertNotIn("Try again", buttons)
        state.save_done(token, failed=True)
        self.render()
        retry = next(button for button in self.buttons() if button.child.label == "Try again")
        self.assertTrue(retry.sensitive)
        retry.callback()
        self.host.first_retry.assert_called_once_with()

    def test_invalid_checklist_render_preserves_errors_and_refreshes_only_once(self):
        state = self.host.first_state()
        for error in ("", READ_ERROR, CHOICE_ERROR, SAVE_ERROR):
            for invalid in (ValueError("invalid checklist"), []):
                with self.subTest(error=error, invalid=invalid):
                    state.error = error
                    self.host.first_steps.side_effect = invalid if isinstance(invalid, Exception) else None
                    self.host.first_steps.return_value = invalid
                    for _ in range(2):
                        children = self.render()
                        self.assertEqual(state.error, error)
                        self.assertEqual(children.count(READ_ERROR), 1)
                        if error != READ_ERROR:
                            retry = children[children.index(READ_ERROR) + 1].children[0]
                            self.host.first_refresh.reset_mock()
                            retry.callback()
                            self.host.first_refresh.assert_called_once_with()


class Row:
    def __init__(self, title, subtitle):
        self.title, self.subtitle = title, subtitle

    def set_subtitle(self, subtitle):
        self.subtitle = subtitle

    def add_prefix(self, _widget):
        pass

    def add_suffix(self, _widget):
        pass


class Mark:
    def __init__(self, *, label):
        self.label = label

    def add_css_class(self, _name):
        pass


class RenderHost(Host):
    def info_card(self, title, level):
        return types.SimpleNamespace(title=title, level=level, body=Box())

    def card_text(self, box, text):
        box.append(text)

    def boxed_list(self):
        return Box()

    def plain_row(self, title, subtitle):
        return Row(title, subtitle)

    def first_button(self, label, _callback, **_kwargs):
        return label

    def first_actions(self, *buttons):
        return buttons


class ChecklistRenderingTest(unittest.TestCase):
    def setUp(self):
        self.host, self.body = RenderHost(), Box()
        self.route = types.SimpleNamespace(title="Fixture", access="Fixture access", kind="online")

    def render(self, steps):
        with mock.patch.object(page.Gtk, "Label", Mark):
            self.host.fill_first_steps(self.body, self.route, steps)
        return self.body.children[1].children

    def test_manual_hint_keeps_the_next_steps_details_and_later_steps_wait(self):
        rows = self.render([
            ("Account", False, "Use the account panel.", "", None, ""),
            ("Test", False, "Wait for sign-in.", "", None, ""),
        ])
        self.assertIn("Use the account panel.", rows[0].subtitle)
        self.assertIn("manual setup", rows[0].subtitle)
        self.assertEqual(rows[1].subtitle, "Wait for sign-in.")

    def test_pending_model_fit_keeps_its_waiting_state_without_manual_setup_hint(self):
        import llm_engine
        engine = types.SimpleNamespace(context_now=lambda *_args: (1024, "f16", "fixture.gguf"))
        runtime = types.SimpleNamespace(models_dir="/fixture")
        model = types.SimpleNamespace(context=32768)
        with mock.patch.object(llm_engine, "of", return_value=engine), \
                mock.patch.object(self.host, "first_fit", return_value=None):
            step = self.host.first_context_step(runtime, (model, "fixture"), 123)
        rows = self.render([step])
        self.assertEqual(rows[0].subtitle, "Looking at the model…")
        self.assertNotIn("manual setup", rows[0].subtitle)


class StateTest(unittest.TestCase):
    def test_malformed_or_empty_checklist_cannot_be_ready_or_activate_an_action(self):
        self.assertTrue(valid_steps([("Test", False, "", "Ask", lambda: None, "")]))
        for steps in ([], None, [None], [("short", False)], [("Test", False, None, "Ask", None, "")],
                      [("Test", False, "", "Ask", "untrusted action", "")]):
            with self.subTest(steps=steps):
                self.assertFalse(valid_steps(steps))

    def test_failed_check_keeps_prior_snapshot_and_unsaved_choice(self):
        state = FirstAgentState()
        read = state.begin_read()
        state.read_done(read, "catalog", "hardware", {"route": "old"})
        save = state.begin_save({"route": "new"})
        state.save_done(save, failed=True)
        self.assertEqual(state.saved["route"], "old")
        self.assertEqual(state.error, CHOICE_ERROR)
        refresh = state.begin_read()
        state.read_failed(refresh)
        self.assertEqual((state.catalog, state.hardware), ("catalog", "hardware"))
        self.assertEqual(state.retry_changes, {"route": "new"})
        refresh = state.begin_read()
        state.read_done(refresh, "fresh", "fresh", {"route": "old"})
        self.assertEqual(state.error, CHOICE_ERROR)

    def test_late_read_never_overwrites_reopened_guide(self):
        state = FirstAgentState()
        old = state.begin_read()
        state.abandon_read()
        new = state.begin_read()
        state.read_done(new, "new", "new", {"route": "new"})
        self.assertFalse(state.read_done(old, "old", "old", {}))
        self.assertFalse(state.read_failed(old))
        self.assertEqual(state.saved["route"], "new")

    def test_timed_out_save_keeps_duplicate_writes_blocked_until_writer_exits(self):
        state = FirstAgentState()
        state.saved = {"route": "old"}
        token = state.begin_save({"route": "new"})
        self.assertTrue(state.save_timeout(token))
        self.assertFalse(state.saving)
        self.assertTrue(state.busy)
        self.assertIsNone(state.begin_save({"route": "other"}))
        self.assertEqual(state.saved["route"], "old")
        state.save_done(token, {"route": "new"})
        self.assertFalse(state.busy)
        self.assertEqual(state.saved["route"], "old")
        self.assertEqual(state.error, CHOICE_ERROR)

    def test_verification_save_failure_does_not_claim_readiness(self):
        state = FirstAgentState()
        state.saved = {"verified": []}
        token = state.begin_save({"verified": ["fixture"], "answer": "A test answer"})
        state.save_done(token, failed=True)
        self.assertEqual(state.error, SAVE_ERROR)
        self.assertEqual(state.saved, {"verified": []})

    def test_check_can_confirm_a_save_that_finished_after_its_timeout(self):
        state = FirstAgentState()
        token = state.begin_save({"route": "new"})
        state.save_timeout(token)
        state.save_done(token, {"route": "new"})
        read = state.begin_read()
        state.read_done(read, "catalog", "hardware", {"route": "new"})
        self.assertEqual(state.error, "")
        self.assertIsNone(state.retry_changes)

    def test_check_confirms_verification_even_if_another_answer_has_also_completed(self):
        state = FirstAgentState()
        token = state.begin_save({"verified": ["first"]})
        state.save_timeout(token)
        state.save_done(token, {"verified": ["first"]})
        read = state.begin_read()
        state.read_done(read, "catalog", "hardware", {"verified": ["first", "second"]})
        self.assertEqual(state.error, "")
        self.assertIsNone(state.retry_changes)


class PageLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.scheduler = Scheduler()
        self.host = Host()
        self.saved = {"route": "old"}
        self.catalog = first_agent.Catalog()
        self.patches = [
            mock.patch.object(page.GLib, "timeout_add", self.scheduler.timeout_add),
            mock.patch.object(page.GLib, "source_remove", self.scheduler.source_remove),
            mock.patch.object(page.GLib, "idle_add", self.scheduler.idle_add),
            mock.patch.object(page.threading, "Thread", self.scheduler.thread),
            mock.patch.object(first_agent, "load", return_value=self.catalog),
            mock.patch.object(first_agent, "state", side_effect=lambda: dict(self.saved)),
            mock.patch.object(install_ctl, "detect_gpu", return_value=install_ctl.Gpu()),
            mock.patch.object(gpu_stack, "look", return_value=gpu_stack.Stack()),
        ]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)

    def read(self):
        self.host.first_look()
        self.scheduler.work()
        self.scheduler.dispatch()

    def test_open_shows_the_guide_before_catalog_or_hardware_finishes(self):
        self.host.active = "main"
        self.host.open_first_agent()
        self.assertEqual(self.host.active, "firstagent")
        self.assertTrue(self.host.first_state().loading)
        self.assertIsNone(self.host.first_hw)
        self.scheduler.work()
        self.scheduler.dispatch()
        self.assertFalse(self.host.first_state().loading)
        self.assertIs(self.host.first_catalog, self.catalog)

    def test_background_save_restarts_cancelled_check_with_existing_snapshot(self):
        self.read()
        self.host.first_look()
        self.host.first_save(verified=["fixture"])
        with mock.patch.object(page.ai_core, "update_settings"):
            self.scheduler.work(1)
            self.scheduler.dispatch()
        self.assertTrue(self.host.first_state().loading)
        self.assertEqual(len(self.scheduler.threads), 2)  # cancelled worker and replacement
        self.scheduler.work()  # cancelled read must not finish the replacement
        self.scheduler.dispatch()
        self.assertTrue(self.host.first_state().loading)
        self.scheduler.work()
        self.scheduler.dispatch()
        self.assertFalse(self.host.first_state().loading)

    def test_pending_write_restarts_skipped_check_after_queue_drains(self):
        self.read()
        self.host.first_save(verified=["first"])
        self.host.first_save(verified=["second"])
        self.host.first_look()
        with mock.patch.object(page.ai_core, "update_settings"):
            self.scheduler.work()
            self.scheduler.dispatch()
            self.assertFalse(self.host.first_state().loading)
            self.scheduler.work()
            self.scheduler.dispatch()
        self.assertTrue(self.host.first_state().loading)
        self.assertEqual(len(self.scheduler.threads), 1)
        self.scheduler.work()
        self.scheduler.dispatch()

    def test_leaving_clears_skipped_read_request(self):
        self.read()
        self.host.first_save(verified=["fixture"])
        self.host.first_look()
        self.assertTrue(getattr(self.host, "_first_read_wanted", False))
        self.host.first_abandon_read()
        self.assertFalse(self.host._first_read_wanted)
        with mock.patch.object(page.ai_core, "update_settings"), mock.patch.object(self.host, "first_look") as look:
            self.scheduler.work()
            self.scheduler.dispatch()
        look.assert_not_called()

    def test_four_failed_verification_retries_call_saved_once_on_success(self):
        self.read()
        saved = mock.Mock()
        with mock.patch.object(page.ai_core, "update_settings", side_effect=OSError("fixture")):
            self.host.first_save(verified=["fixture"], answer="Ready", on_saved=saved)
            self.scheduler.work()
            self.scheduler.dispatch()
            for _ in range(4):
                self.host.first_retry()
                self.scheduler.work()
                self.scheduler.dispatch()
        saved.assert_not_called()
        with mock.patch.object(page.ai_core, "update_settings"):
            self.host.first_retry()
            self.scheduler.work()
            self.scheduler.dispatch()
        saved.assert_called_once_with()

    def test_later_retry_after_failed_verification_calls_saved_once_on_success(self):
        self.read()
        saved = mock.Mock()
        with mock.patch.object(page.ai_core, "update_settings", side_effect=OSError("fixture")):
            self.host.first_save(verified=["fixture"], answer="Ready", on_saved=saved)
            self.scheduler.work()
            self.scheduler.dispatch()
            self.host.first_later()
            self.scheduler.work()
            self.scheduler.dispatch()
            for _ in range(3):
                self.host.first_retry()
                self.scheduler.work()
                self.scheduler.dispatch()
        saved.assert_not_called()
        with mock.patch.object(page.ai_core, "update_settings"):
            self.host.first_retry()
            self.scheduler.work()
            self.scheduler.dispatch()
        saved.assert_called_once_with()
        self.assertEqual(self.host.active, "main")

    def test_failed_route_save_keeps_choice_error_after_read_failure(self):
        for timeout in (False, True):
            with self.subTest(timeout=timeout):
                self.host = Host()
                self.scheduler = Scheduler()
                with mock.patch.object(page.GLib, "timeout_add", self.scheduler.timeout_add), \
                        mock.patch.object(page.GLib, "source_remove", self.scheduler.source_remove), \
                        mock.patch.object(page.GLib, "idle_add", self.scheduler.idle_add), \
                        mock.patch.object(page.threading, "Thread", self.scheduler.thread):
                    self.read()
                    with mock.patch.object(page.ai_core, "update_settings", side_effect=OSError("fixture")):
                        self.host.first_save(route="new")
                        self.scheduler.work()
                        self.scheduler.dispatch()
                    self.host.first_look()
                    if timeout:
                        self.scheduler.expire(self.host._first_read_timer)
                        self.scheduler.threads.clear()
                    else:
                        with mock.patch.object(first_agent, "load", side_effect=OSError("fixture")):
                            self.scheduler.work()
                        self.scheduler.dispatch()
                    state = self.host.first_state()
                    self.assertEqual(state.retry_changes, {"route": "new"})
                    self.assertEqual(state.error, CHOICE_ERROR)
                    with mock.patch.object(page.ai_core, "update_settings"):
                        self.host.first_retry()
                        self.scheduler.work()
                        self.scheduler.dispatch()
                    self.assertTrue(state.loading)
                    self.assertEqual(len(self.scheduler.threads), 1)
                    self.scheduler.work()
                    self.scheduler.dispatch()
                    self.assertFalse(state.loading)

    def test_failed_route_save_keeps_choice_error_after_fit_timeout(self):
        self.read()
        with mock.patch.object(page.ai_core, "update_settings", side_effect=OSError("fixture")):
            self.host.first_save(route="new")
            self.scheduler.work()
            self.scheduler.dispatch()
        state = self.host.first_state()
        self.host.first_fit("fixture.gguf", 123, "f16")
        self.scheduler.expire(next(iter(self.host._first_fit_timers.values())))
        self.scheduler.threads.clear()
        self.assertEqual(state.retry_changes, {"route": "new"})
        self.assertEqual(state.error, CHOICE_ERROR)
        with mock.patch.object(page.ai_core, "update_settings"):
            self.host.first_retry()
            self.scheduler.work()
            self.scheduler.dispatch()
        self.assertTrue(state.loading)
        self.assertEqual(self.host.first_fits, {})
        self.scheduler.work()
        self.scheduler.dispatch()
        self.assertFalse(state.loading)

    def test_overview_later_failure_or_timeout_reports_error_and_keeps_card(self):
        for timeout in (False, True):
            with self.subTest(timeout=timeout):
                host = Host()
                host.active = "main"
                host.first_route = lambda: None
                card = GuideWidget(body=Box())
                host.info_card = lambda *_: card
                host.card_text = mock.Mock()
                host.action_button = action_button
                host.show_error = mock.Mock()
                with mock.patch.multiple(page.Gtk, Box=GuideWidget, Label=GuideWidget):
                    host.first_agent_card(Box())
                    card.body.children[0].children[1].callback()
                if timeout:
                    self.scheduler.expire(host._first_save_timer)
                    host.show_error.assert_called_once_with(SAVE_ERROR)
                with mock.patch.object(page.ai_core, "update_settings", side_effect=OSError("fixture")):
                    self.scheduler.work()
                    self.scheduler.dispatch()
                host.show_error.assert_called_once_with(SAVE_ERROR)
                self.assertTrue(card.visible)
                self.assertEqual(host.active, "main")

    def test_close_cancels_timer_and_late_worker_cannot_replace_a_new_read(self):
        self.host.open_first_agent()
        self.host.close_first_agent()
        self.assertFalse(self.scheduler.timers)
        self.saved["route"] = "fresh"
        self.host.open_first_agent()
        self.scheduler.work(1)
        self.scheduler.dispatch()
        self.saved["route"] = "late old data"
        self.scheduler.work()
        self.scheduler.dispatch()
        self.assertEqual(self.host.first_progress()["route"], "fresh")

    def test_catalog_failure_retains_hardware_and_route_with_retry(self):
        self.read()
        hardware = self.host.first_hw
        self.host.first_refresh()
        with mock.patch.object(first_agent, "load", side_effect=OSError("fixture")):
            self.scheduler.work()
        self.scheduler.dispatch()
        self.assertIs(self.host.first_hw, hardware)
        self.assertEqual(self.host.first_progress()["route"], "old")
        self.assertEqual(self.host.first_state().error, READ_ERROR)
        self.host.first_retry()
        self.scheduler.work()
        self.scheduler.dispatch()
        self.assertEqual(self.host.first_state().error, "")

    def test_hung_read_times_out_and_late_success_is_ignored(self):
        self.host.first_look()
        self.scheduler.expire(self.host._first_read_timer)
        self.assertEqual(self.host.first_state().error, READ_ERROR)
        self.scheduler.work()
        self.scheduler.dispatch()
        self.assertIsNone(self.host.first_hw)

    def test_failed_route_write_retains_choice_and_explicit_retry_uses_intended_choice(self):
        self.read()
        with mock.patch.object(page.ai_core, "update_settings", side_effect=OSError("fixture")) as write:
            self.host.first_choose(types.SimpleNamespace(id="new"))
            self.scheduler.work()
            self.scheduler.dispatch()
            self.assertEqual(write.call_args.kwargs["first_agent"]["route"], "new")
        self.assertEqual(self.host.first_progress()["route"], "old")
        self.assertEqual(self.host.first_state().error, CHOICE_ERROR)
        with mock.patch.object(page.ai_core, "update_settings") as write:
            self.host.first_retry()
            self.scheduler.work()
            self.scheduler.dispatch()
        self.assertEqual(write.call_args.kwargs["first_agent"]["route"], "new")
        self.assertEqual(self.host.first_progress()["route"], "new")

    def test_later_closes_only_after_confirmed_save_and_retry_preserves_that_action(self):
        with mock.patch.object(page.ai_core, "update_settings", side_effect=OSError("fixture")):
            self.host.first_later()
            self.scheduler.work()
            self.scheduler.dispatch()
        self.assertEqual(self.host.active, "firstagent")
        with mock.patch.object(page.ai_core, "update_settings"):
            self.host.first_retry()
            self.scheduler.work()
            self.scheduler.dispatch()
        self.assertEqual(self.host.active, "main")

    def test_later_result_after_leaving_and_reopening_does_not_close_the_new_visit(self):
        self.read()
        self.host.first_later()
        self.host.close_first_agent()
        self.host.open_first_agent()
        self.assertEqual(self.host.active, "firstagent")
        with mock.patch.object(page.ai_core, "update_settings"):
            self.scheduler.work()
            self.scheduler.dispatch()
        self.assertEqual(self.host.active, "firstagent")
        self.assertTrue(self.host.first_progress()["later"])

    def test_hiding_invalidates_later_navigation_without_canceling_its_write(self):
        self.read()
        self.host.first_later()
        # The window can hide while its logical GTK page remains firstagent.
        self.host.first_abandon_read()
        with mock.patch.object(page.ai_core, "update_settings") as write:
            self.scheduler.work()
            self.scheduler.dispatch()
        write.assert_called_once()
        self.assertEqual(self.host.active, "firstagent")
        self.assertTrue(self.host.first_progress()["later"])

    def test_explicit_later_retry_after_reopening_belongs_to_the_current_visit(self):
        self.read()
        with mock.patch.object(page.ai_core, "update_settings", side_effect=OSError("fixture")):
            self.host.first_later()
            self.scheduler.work()
            self.scheduler.dispatch()
        self.host.close_first_agent()
        self.host.open_first_agent()
        self.scheduler.work()
        self.scheduler.dispatch()
        with mock.patch.object(page.ai_core, "update_settings"):
            self.host.first_retry()
            self.scheduler.work()
            self.scheduler.dispatch()
        self.assertEqual(self.host.active, "main")

    def test_refresh_after_failed_later_preserves_its_original_visit_callback(self):
        self.read()
        with mock.patch.object(page.ai_core, "update_settings", side_effect=OSError("fixture")):
            self.host.first_later()
            self.scheduler.work()
            self.scheduler.dispatch()
        self.read()
        with mock.patch.object(page.ai_core, "update_settings"):
            self.host.first_retry()
            self.scheduler.work()
            self.scheduler.dispatch()
        self.assertEqual(self.host.active, "main")

    def test_background_confirmation_refresh_is_not_tied_to_a_guide_visit(self):
        self.read()
        callbacks = []
        self.host.first_save(verified=["fixture"], answer="Ready", on_saved=lambda: callbacks.append(True))
        self.host.close_first_agent()
        self.host.open_first_agent()
        with mock.patch.object(page.ai_core, "update_settings"):
            self.scheduler.work()
            self.scheduler.dispatch()
        self.assertEqual(callbacks, [True])
        self.assertEqual(self.host.first_progress()["answer"], "Ready")
        self.assertEqual(self.host.active, "firstagent")

    def test_pending_choice_does_not_block_leaving_and_cannot_spawn_another_write(self):
        self.read()
        self.host.first_choose(types.SimpleNamespace(id="new"))
        timer = self.host._first_save_timer
        self.scheduler.expire(timer)
        self.assertEqual(self.host.first_state().error, CHOICE_ERROR)
        self.host.first_choose(types.SimpleNamespace(id="other"))
        self.assertEqual(len(self.scheduler.threads), 1)
        self.host.close_first_agent()
        with mock.patch.object(page.ai_core, "update_settings"):
            self.scheduler.work()
            self.scheduler.dispatch()
        self.assertEqual(self.host.active, "main")
        self.assertFalse(self.host.first_state().write_pending)

    def test_background_verification_save_is_serialized_after_a_route_choice(self):
        self.read()
        self.host.first_choose(types.SimpleNamespace(id="new"))
        completed = []
        self.host.first_save(verified=["fixture"], answer="A test answer", on_saved=lambda: completed.append(True))
        self.assertEqual(len(self.scheduler.threads), 1)

        def write(**values):
            self.saved.update(values["first_agent"])

        with mock.patch.object(page.ai_core, "update_settings", side_effect=write):
            self.scheduler.work()
            self.scheduler.dispatch()
            self.assertEqual(completed, [])
            self.scheduler.work()
            self.scheduler.dispatch()
        self.assertEqual(self.host.first_progress(), {"route": "new", "later": False,
                                                    "verified": ["fixture"], "answer": "A test answer"})
        self.assertEqual(completed, [True])

    def test_background_progress_save_does_not_drop_failed_route_retry(self):
        self.read()
        self.host.first_choose(types.SimpleNamespace(id="new"))
        self.host.first_save(verified=["fixture"])
        with mock.patch.object(page.ai_core, "update_settings", side_effect=OSError("fixture")):
            self.scheduler.work()
            self.scheduler.dispatch()
        with mock.patch.object(page.ai_core, "update_settings"):
            self.scheduler.work()
            self.scheduler.dispatch()
        self.assertEqual(self.host.first_progress()["route"], "old")
        self.assertEqual(self.host.first_state().retry_changes, {"route": "new", "later": False})
        self.assertEqual(self.host.first_state().error, CHOICE_ERROR)

    def test_two_confirmed_background_answers_keep_both_agents_verified(self):
        self.read()
        self.host.first_save(verified=["first"], answer="First answer")
        self.host.first_save(verified=["second"], answer="Second answer")

        def write(**values):
            self.saved.update(values["first_agent"])

        with mock.patch.object(page.ai_core, "update_settings", side_effect=write):
            self.scheduler.work()
            self.scheduler.dispatch()
            self.scheduler.work()
            self.scheduler.dispatch()
        self.assertEqual(self.saved["verified"], ["first", "second"])
        self.assertEqual(self.host.first_progress()["verified"], ["first", "second"])
        self.assertEqual(self.saved["answer"], "Second answer")

    def test_failed_first_answer_stays_retryable_after_a_second_answer_is_saved(self):
        self.read()
        self.host.first_save(verified=["first"], answer="First answer")
        self.host.first_save(verified=["second"], answer="Second answer")
        with mock.patch.object(page.ai_core, "update_settings", side_effect=OSError("fixture")):
            self.scheduler.work()
            self.scheduler.dispatch()

        def write(**values):
            self.saved.update(values["first_agent"])

        with mock.patch.object(page.ai_core, "update_settings", side_effect=write):
            self.scheduler.work()
            self.scheduler.dispatch()
            self.assertEqual(self.saved["verified"], ["second"])
            self.assertEqual(self.host.first_state().retry_changes, {"verified": ["first"]})
            self.assertEqual(self.host.first_state().error, SAVE_ERROR)
            self.host.first_retry()
            self.scheduler.work()
            self.scheduler.dispatch()
        self.assertEqual(set(self.saved["verified"]), {"first", "second"})
        self.assertEqual(self.saved["answer"], "Second answer")
        self.assertEqual(self.host.first_state().error, "")

    def test_failed_later_remains_retryable_after_background_progress_is_saved(self):
        self.read()
        self.host.first_later()
        self.host.first_save(verified=["fixture"], answer="An answer")
        with mock.patch.object(page.ai_core, "update_settings", side_effect=OSError("fixture")):
            self.scheduler.work()
            self.scheduler.dispatch()
        with mock.patch.object(page.ai_core, "update_settings"):
            self.scheduler.work()
            self.scheduler.dispatch()
            self.assertEqual(self.host.first_state().retry_changes, {"later": True})
            self.assertEqual(self.host.active, "firstagent")
            self.host.first_retry()
            self.scheduler.work()
            self.scheduler.dispatch()
        self.assertEqual(self.host.active, "main")

    def test_two_failed_background_answers_can_be_explicitly_retried_together(self):
        self.read()
        self.host.first_save(verified=["first"], answer="First answer")
        self.host.first_save(verified=["second"], answer="Second answer")
        with mock.patch.object(page.ai_core, "update_settings", side_effect=OSError("fixture")):
            self.scheduler.work()
            self.scheduler.dispatch()
            self.scheduler.work()
            self.scheduler.dispatch()
        self.assertEqual(self.host.first_state().retry_changes, {"verified": ["first", "second"], "answer": "Second answer"})
        self.assertEqual(len(self.scheduler.threads), 0)
        with mock.patch.object(page.ai_core, "update_settings") as write:
            self.host.first_retry()
            self.scheduler.work()
            self.scheduler.dispatch()
        self.assertEqual(write.call_args.kwargs["first_agent"]["verified"], ["first", "second"])
        self.assertEqual(self.host.first_state().error, "")

    def test_background_save_during_check_does_not_drop_a_failed_verification(self):
        self.read()
        with mock.patch.object(page.ai_core, "update_settings", side_effect=OSError("fixture")):
            self.host.first_save(verified=["first"])
            self.scheduler.work()
            self.scheduler.dispatch()
        self.host.first_look()  # loading temporarily replaces the visible save error
        self.host.first_save(verified=["second"])
        with mock.patch.object(page.ai_core, "update_settings"):
            self.scheduler.work(1)  # save; the older check will be ignored
            self.scheduler.dispatch()
        self.assertEqual(self.host.first_state().retry_changes, {"verified": ["first"]})
        self.assertEqual(self.host.first_state().error, SAVE_ERROR)

    def test_confirmed_context_write_does_not_refresh_a_reopened_guide(self):
        import llm_engine
        engine = types.SimpleNamespace(context_changes=mock.Mock(return_value=None), set_context=mock.Mock())
        self.host.toast = mock.Mock()
        self.host.show_error = mock.Mock()
        self.host.first_refresh = mock.Mock()
        with mock.patch.object(llm_engine, "of", return_value=engine):
            self.host.first_set_context(types.SimpleNamespace(), "fixture", 32768)
        self.host.close_first_agent()
        self.host.open_first_agent()
        self.scheduler.work()  # old context operation, not the new guide read
        self.scheduler.dispatch()
        engine.set_context.assert_called_once()
        self.host.toast.assert_called_once()
        self.host.first_refresh.assert_not_called()

    def test_model_fit_after_leave_or_timeout_does_not_refresh_a_new_presentation(self):
        self.host.first_fits = {}
        self.host.first_fit("fixture.gguf", 123, "cache")
        self.host.first_abandon_read()
        before = self.host.fills
        import context_ctl
        with mock.patch.object(context_ctl, "fit", return_value=context_ctl.Fit(0, 0, 0, 0, known=False)):
            self.scheduler.work()
            self.scheduler.dispatch()
        self.assertEqual(self.host.fills, before)
        self.assertFalse(self.scheduler.timers)

    def test_cancelled_model_fit_can_restart_for_the_same_model(self):
        self.host.first_fits = {}
        self.host.first_fit("fixture.gguf", 123, "cache")
        self.host.first_abandon_read(internal=True)
        self.assertEqual(self.host.first_fits, {})
        self.host.first_fit("fixture.gguf", 123, "cache")
        self.assertEqual(len(self.scheduler.threads), 2)
        import context_ctl
        result = context_ctl.Fit(0, 0, 0, 0, known=False)
        with mock.patch.object(context_ctl, "fit", return_value=result):
            self.scheduler.work()  # old worker cannot overwrite the new pending fit
            self.scheduler.dispatch()
            self.assertIsNone(self.host.first_fit("fixture.gguf", 123, "cache"))
            self.scheduler.work()
            self.scheduler.dispatch()
        self.assertIs(self.host.first_fit("fixture.gguf", 123, "cache"), result)


if __name__ == "__main__":
    unittest.main()
