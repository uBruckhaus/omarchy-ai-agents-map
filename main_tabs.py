"""AI Agents Map: the header tabs [ Projects ] [ AI Map ] and the links between them (a mixin of
ai_popup.AgentsMap; Maprisma's main_tabs.dart and the tab wiring of main.dart, tasks 070, 086, 091,
092 and 093).

- The two tabs sit after the title; Ctrl+1 shows Projects, Ctrl+2 the AI Map. The Projects tab
  carries the running / needs-you / attention counts of the project summary (a dot in a narrow
  window, a crossed cloud while the list cannot be read, a warning mark for a store fault).
- The start tab is decided once: the AI Map while nothing is set up, else the tab used last (the
  `main_tab` setting), else Projects when a project exists. Every explicit choice is saved.
- Leaving the open project (another tab, a header action, closing the window) goes through
  projects_prepare_leave: unsaved changes ask Save / Discard / Cancel, a running step waits.
- Settings, Help and Reports cover the tab they were opened from and return to it.
- Show on map / Fix on map pan the AI Map to a box and mark it for a few seconds (pulse); the agent
  boxes' "Used in N projects" opens the board with that agent's chip.
- Switching to the AI Map resets the board's filter, agent chip and Cards | Phases (not saved)."""

from __future__ import annotations

import threading

from gi.repository import GLib, Gtk

import ai_core
import guide_navigation
from i18n import _, ngettext

TABS = ("projects", "map")
# Pages over the tabs that keep the tab they were opened from (they return to it).
NEUTRAL_PAGES = frozenset({"settings", "help", "info", "reports", "addons", "reorganize"})
COMPACT_WIDTH = 760          # below it the Projects tab shows one dot instead of its counts
SUMMARY_SECONDS = 10         # the project counts are read again this often while the window shows
START_WAIT = 3.0             # the first summary decides the start tab only when it comes this fast
REVEAL_SECONDS = 6           # how long a box stays marked after Show on map / Fix on map


def tab_of(code) -> str | None:
    return code if code in TABS else None


def start_tab(setup_needed: bool, saved, summary) -> str:
    """Maprisma's startTab (task 086): the AI Map while setup is needed, else the last used tab, else
    Projects when a project exists. summary None: not read in time (the AI Map)."""
    if setup_needed:
        return "map"
    chosen = tab_of(saved)
    if chosen is not None:
        return chosen
    if not isinstance(summary, dict) or summary.get("connected") is False:
        return "map"
    total = summary.get("total")
    return "projects" if isinstance(total, int) and total > 0 else "map"


def tab_counts(summary) -> dict:
    """What the Projects tab shows of the summary (Maprisma's ProjectTabCounts)."""
    summary = summary if isinstance(summary, dict) else {"connected": False}

    def count(key):
        value = summary.get(key)
        return value if type(value) is int and value > 0 else 0
    error = summary.get("error")
    return {"connected": summary.get("connected") is not False,
            "running": count("running") + count("pausing"),
            "needs_you": count("needs_you") if "needs_you" in summary else count("waiting"),
            "attention": count("attention_projects") if "attention_projects" in summary else count("attention"),
            "error": error if isinstance(error, str) and error else None}


def describe(counts: dict) -> str:
    """The Projects tab's tooltip and accessible description."""
    if not counts["connected"]:
        return _("Projects · status unavailable")
    parts = [_("Projects")]
    if counts["running"]:
        parts.append(_("{0} running").format(counts["running"]))
    if counts["needs_you"]:
        parts.append(_("{0} need you").format(counts["needs_you"]))
    if counts["attention"]:
        parts.append(_("{0} need attention").format(counts["attention"]))
    if counts["error"]:
        parts.append(_("The project list has a problem"))
    return " · ".join(parts)


def used_in_text(count: int) -> str:
    """"Used in 1 project" / "Used in N projects" (the agent box's badge, task 091)."""
    return ngettext("Used in 1 project", "Used in {0} projects", count).format(count)


def used_in(summary) -> dict:
    """{agent command: projects} from the summary's by_agent (only positive whole numbers)."""
    found = summary.get("by_agent") if isinstance(summary, dict) else None
    if not isinstance(found, dict):
        return {}
    return {str(k): v for k, v in found.items() if type(v) is int and v > 0}


class MainTabs:
    """The tabs, the departure guards and the links between the Projects tab and the AI Map."""

    main_tab = "map"
    tab_decided = False
    departing = False                  # one departure at a time (its question may be open)
    project_summary = {"connected": False}
    _summary_reading = False
    _summary_timer = 0
    _reveal_timer = 0
    _reveal_wait = 0                   # the short wait before a reveal (the map laid out once more)
    _reveal_seq = 0                    # the reveal asked for last; an older one's answer is dropped
    _last_page = None                  # the page shown before (follow_tab): leaving it cleans it up
    map_stale = False                  # the map was not drawn again while Projects was shown
    discovery_facts = None             # what the map's last read found (projects_map_changed)
    tab_buttons: dict = {}

    # --- the tabs in the header ----------------------------------------------------------------------

    def build_main_tabs(self) -> Gtk.Widget:
        box = Gtk.Box(spacing=2, valign=Gtk.Align.CENTER)
        box.add_css_class("am-main-tabs")
        self.tab_buttons = {}
        self.tab_counts_box = None
        for code, label in (("projects", _("Projects")), ("map", _("AI Map"))):
            button = Gtk.ToggleButton(valign=Gtk.Align.CENTER)
            for css in ("flat", "am-main-tab"):
                button.add_css_class(css)
            inner = Gtk.Box(spacing=6)
            inner.append(Gtk.Label(label=label))
            if code == "projects":
                self.tab_counts_box = Gtk.Box(spacing=4)
                inner.append(self.tab_counts_box)
            button.set_child(inner)
            button.set_tooltip_text(_("Projects") + " (Ctrl+1)" if code == "projects" else _("AI Map") + " (Ctrl+2)")
            button.connect("toggled", self.on_tab_button, code)
            self.tab_buttons[code] = button
            box.append(button)
        self.main_tabs_box = box
        return box

    def on_tab_button(self, button, code: str):
        if getattr(self, "updating_buttons", False):
            return
        self.select_tab(code)
        self.update_main_tabs()             # a refused departure keeps the tab that is shown

    def update_main_tabs(self):
        buttons = getattr(self, "tab_buttons", None) or {}
        if not buttons:
            return
        self.updating_buttons = True
        for code, button in buttons.items():
            button.set_active(code == self.main_tab)
        self.updating_buttons = False
        counts = tab_counts(self.project_summary)
        projects = buttons.get("projects")
        if projects is not None:
            projects.set_tooltip_text(describe(counts) + " (Ctrl+1)")
        self.fill_tab_counts(counts)

    def fill_tab_counts(self, counts: dict):
        holder = getattr(self, "tab_counts_box", None)
        if holder is None:
            return
        while (child := holder.get_first_child()) is not None:
            holder.remove(child)
        compact = 0 < self.window.get_width() < COMPACT_WIDTH if getattr(self, "window", None) else False

        def mark(icon: str, css: str, text: str = ""):
            item = Gtk.Box(spacing=1)
            import project_parts
            image = project_parts.icon(icon, 14)          # with its fallbacks for other icon themes
            item.append(image)
            if text:
                item.append(Gtk.Label(label=text))
            item.add_css_class(css)
            holder.append(item)
        if not counts["connected"]:
            mark("network-offline-symbolic", "am-tab-dim")
        elif compact:
            if counts["running"] or counts["needs_you"] or counts["attention"]:
                dot = Gtk.Box(valign=Gtk.Align.CENTER)
                dot.add_css_class("am-tab-dot")
                dot.add_css_class("am-tab-red" if counts["attention"] else "am-tab-yellow" if counts["needs_you"]
                                  else "am-tab-accent")
                holder.append(dot)
        else:
            if counts["running"]:
                mark("media-playback-start-symbolic", "am-tab-accent", str(counts["running"]))
            if counts["needs_you"]:
                mark("avatar-default-symbolic", "am-tab-yellow", str(counts["needs_you"]))
            if counts["attention"]:
                mark("dialog-error-symbolic", "am-tab-red", str(counts["attention"]))
        if counts["connected"] and counts["error"]:
            mark("dialog-warning-symbolic", "am-tab-red")
        holder.set_visible(holder.get_first_child() is not None)

    # --- which tab -----------------------------------------------------------------------------------

    def decide_start_tab(self):
        """Once, when the window is built: saved → that tab; nothing set up → the map; else the first
        summary within 3 s decides (a project → Projects). A later choice is never overridden."""
        if self.tab_decided:
            return
        setup_needed = self.start_setup_needed()
        saved = ai_core.setting("main_tab")
        if setup_needed or tab_of(saved) is not None:
            self.tab_decided = True
            self.apply_start_tab(start_tab(setup_needed, saved, None))
            return
        began = GLib.get_monotonic_time()

        def answered(summary):
            if self.tab_decided:
                return
            self.tab_decided = True
            fast = (GLib.get_monotonic_time() - began) / 1e6 <= START_WAIT
            self.apply_start_tab(start_tab(False, None, summary if fast else None))
        self.refresh_project_summary(then=answered)
        GLib.timeout_add(int(START_WAIT * 1000) + 50, lambda: (answered(None) if not self.tab_decided else None,
                                                               False)[1])

    def start_setup_needed(self) -> bool:
        """Maprisma's setup_needed: the welcome page is due (add-ons there, setup not finished), not
        whether an agent happens to be installed right now."""
        if getattr(self, "setup_pending", False):
            return True
        adapters = self.adapter_addons() if callable(getattr(self, "adapter_addons", None)) else []
        return bool(adapters) and ai_core.setting("setup_done") is not True

    def apply_start_tab(self, tab: str):
        if tab == "projects" and self.page_name() in ("main", "setup"):
            self.main_tab = "projects"
            self.open_projects()
        elif self.map_stale:
            self.refresh_map_async()           # the map's first drawing waited for the decision
        self.update_main_tabs()
        self.update_header()

    def map_work_waits(self) -> bool:
        """The map is not drawn and its polls stay light: on the Projects tab, or while the start tab
        is still being decided (a start on Projects makes no map scan, task 086)."""
        return self.main_tab == "projects" or not self.tab_decided

    _map_read_seq = 0

    def refresh_map_async(self):
        """The deferred map drawn again from facts read on a worker (agents, local LLMs, details: file
        and session scans stay off the GTK thread); a newer read or the Projects tab drops the answer."""
        self.map_stale = False
        self._map_read_seq += 1                # one sequence for this and the map links' reads: the
        seq = self._map_read_seq               # newest request wins, an older answer is dropped
        generation = getattr(self, "map_generation", 0)

        def work():
            try:
                facts = self.read_map_facts()
            except Exception:
                facts = None
            GLib.idle_add(done, facts)

        def done(facts):
            if seq != self._map_read_seq or self.map_work_waits():
                self.map_stale = self.map_stale or self.map_work_waits()
                return False
            if generation != getattr(self, "map_generation", 0):
                return False                   # collected and drawn since (a connection made…): newer
            if facts is None:
                self.map_read_failed()
                return False
            try:
                self.refresh_map(facts=facts)
            except Exception:
                self.map_stale = True
            return False
        threading.Thread(target=work, name="map-read", daemon=True).start()

    def map_read_failed(self):
        """A background read of the map failed: the map stays as it was (stale), a note says so, and the
        next read is tried on a worker again in a few seconds — never a scan on the GTK thread."""
        self.map_stale = True
        self.toast(_("The AI Map could not be read again. It is tried again in a moment."), 5)

        def again():
            if self.map_stale and not self.map_work_waits() and self.window_shown():
                self.refresh_map_async()
            return False
        GLib.timeout_add_seconds(5, again)

    def save_tab(self, tab: str):
        if ai_core.setting("main_tab") != tab:
            try:
                ai_core.update_settings(main_tab=tab)
            except OSError:
                pass

    def set_main_tab(self, tab: str):
        """The tab in view changed (chosen, or a page of the other tab opened)."""
        if tab == self.main_tab:
            if tab == "map" and self.map_stale and self.tab_decided:
                self.refresh_map_async()       # chosen before the start decision drew it (agy r4)
            return
        if tab == "map" and getattr(self, "projects_ctrl", None) is not None and not self.projects_ctrl.disposed:
            self.projects_ctrl.reset_board_choices()
        if tab != "map":
            self.forget_map_mark()
        self.main_tab = tab
        self.update_main_tabs()
        if tab == "map" and self.map_stale:
            self.refresh_map_async()           # deferred while Projects was shown

    def follow_tab(self):
        """A page shown by any route: a page of the map (details, a terminal, Ask AI…) is on the AI Map
        tab, the Projects page on the Projects tab; Settings, Help and Reports keep the tab."""
        if getattr(self, "stack", None) is None:
            return
        page = self.page_name()
        last, self._last_page = self._last_page, page
        if last != page:
            self.left_page(last)
        if page in NEUTRAL_PAGES:
            return
        self.set_main_tab("projects" if page == "projects" else "map")
        if getattr(self, "legend_button", None) is not None:
            self.legend_button.set_visible(self.main_tab == "map")

    def left_page(self, page):
        """Whatever route left a page (a tab, Help, Settings, Reports, Back, hiding): what it was doing
        in the background stops — a dictation in Ask AI or on a provider's Configuration page, and
        its transcription on the way is dropped."""
        if page == "askai" and callable(getattr(self, "stop_ask_dictation", None)):
            self.stop_ask_dictation()
        elif page == "providerconfig" and callable(getattr(self, "stop_provider_config_dictation", None)):
            self.stop_provider_config_dictation()

    def home_page(self) -> str:
        return "projects" if self.main_tab == "projects" else "main"

    def select_tab(self, tab: str):
        """A tab chosen (click, Ctrl+1 / Ctrl+2): the pages over the tabs close, the open project asks
        first, the choice is remembered. Projects again while a project is open: back to its board."""
        if tab_of(tab) is None or getattr(self, "booting", False) or getattr(self, "stack", None) is None:
            return
        if self.departing or (callable(getattr(self, "dialog_open", None)) and self.dialog_open()):
            return
        page = self.page_name()
        self.tab_decided = True
        if tab == "projects" and page == "projects":
            ctrl = self.projects_ctrl
            if ctrl.open:
                if ctrl.busy:
                    self.toast(_("Wait for the current step"), 4)
                    return
                self.departing = True

                def back(ok=True):
                    self.departing = False
                    if ok:
                        self.save_tab(tab)
                    self.update_main_tabs()
                ctrl.back_to_board(back)
                return
            self.save_tab(tab)
            return

        def left(ok):
            if not ok:
                self.update_main_tabs()
                return
            self.return_page = None
            guide_navigation.abandon_handoffs(self)
            if self.page_name() == "connect":
                self.close_connect(navigate=False)
            elif self.page_name() == "askai":
                self.close_ask_ai()                 # an Ask under way stops with it (left_page: the mic)
            if tab == "map":
                self.set_main_tab("map")
                self.setup_welcome = False
                self.show_view("main", save=False)
                self.refresh_project_summary()
            else:
                self.set_main_tab("projects")
                self.open_projects()
            self.save_tab(tab)
            self.update_header()
            if tab == "map":
                self.focus_tab(tab)     # Projects gives the keyboard back to its own page
        self.leave_pages(left)

    def focus_tab(self, tab: str):
        """The keyboard on the tab chosen: the page it left had it (else GTK gives it to the first
        selectable text of the new page, all of it selected)."""
        button = (getattr(self, "tab_buttons", None) or {}).get(tab)
        if button is not None and button.get_mapped():
            button.grab_focus()

    def leave_pages(self, done):
        """The open project, then Reports: what a tab switch or a header action leaves first."""
        def after_projects(ok):
            self.departing = False
            if not ok:
                done(False)
                return
            if self.page_name() == "reports" and getattr(self, "reports_running", None) is not None:
                def cancelled():
                    self.stop_reports_timer()
                    done(True)
                self.confirm_leave_reports(cancelled, stay=lambda: done(False))
                return
            if self.page_name() == "reports":
                self.stop_reports_timer()
            done(True)
        if self.page_name() == "projects" and getattr(self, "projects_ctrl", None) is not None:
            if self.departing:
                done(False)
                return
            self.departing = True
            self.projects_prepare_leave(after_projects)
            return
        after_projects(True)

    def after_leaving(self, action):
        """A header action (Settings, Help, Reports, Ask AI, the problems list): the open project's
        unsaved changes ask first; Settings and Help come back to the page they cover."""
        def left(ok):
            if ok:
                action()
        self.leave_pages(left)             # the open project, and a report being generated, ask first

    def to_map_first(self, then):
        """Ask AI and the problems list work on the map: from Projects through the guard to the AI Map."""
        def left(ok):
            if not ok:
                return
            self.tab_decided = True
            if self.main_tab == "map" and self.map_stale:
                self.refresh_map_async()       # the map waited for the start decision: draw it now
            if self.main_tab != "map":
                self.set_main_tab("map")
                self.save_tab("map")
                self.return_page = None
                self.show_view("main", save=False)   # the page it opens returns to the map, not Projects
                self.focus_tab("map")
            then()
        self.leave_pages(left)

    # --- the project summary: the tab's counts, the map's Used in badges ---------------------------------

    def refresh_project_summary(self, then=None):
        """The project center's summary on a worker (it may scan for a while); the tab and the map's
        badges follow. then(summary) once it is known (None: it could not be read)."""
        if getattr(self, "_projects_closed", True) or not hasattr(self, "_project_call"):
            if then is not None:
                then(None)
            return
        if self._summary_reading and then is None:
            return
        self._summary_reading = True

        def work():
            try:
                summary = self._project_call("summary", {})
            except Exception:
                summary = None
            GLib.idle_add(done, summary)

        def done(summary):
            self._summary_reading = False
            if not getattr(self, "_projects_closed", True):
                if isinstance(summary, dict):
                    self.project_summary = {**summary, "connected": True}
                elif self.project_summary.get("connected") is not False:
                    self.project_summary = {**self.project_summary, "connected": False}
                self.show_project_summary()
            if then is not None:
                then(summary if isinstance(summary, dict) else None)
            return False
        threading.Thread(target=work, name="project-summary", daemon=True).start()

    def show_project_summary(self):
        self.update_main_tabs()
        view = getattr(self, "side_map", None)
        if view is not None and hasattr(view, "set_used_in"):
            view.set_used_in(used_in(self.project_summary))

    def start_summary_poll(self):
        if not self._summary_timer:
            self._summary_timer = GLib.timeout_add_seconds(SUMMARY_SECONDS, self._summary_tick)

    def stop_summary_poll(self):
        if self._summary_timer:
            GLib.source_remove(self._summary_timer)
            self._summary_timer = 0

    def _summary_tick(self):
        if getattr(self, "_projects_closed", True):
            self._summary_timer = 0
            return False
        self.refresh_project_summary()
        return True

    def discovery_fingerprint(self):
        """What the project center discovers that the map shows: the agents (installed, version), the
        local LLMs (installed, running, address, their models, the agents connected to them — what its
        _member_options reads as bindings) and the agents' own settings (models_stamp: installs and
        setup files, so a line drawn or removed counts). Redraws for other reasons leave it alone."""
        found_agents = tuple(sorted((a.command, bool(a.installed), str(getattr(a, "version", "") or ""))
                                    for a in getattr(self, "agent_list", [])))
        inventory = getattr(self, "inventory", None)
        runtimes = tuple(sorted((r.key, bool(r.installed), bool(getattr(r, "running", False)),
                                 str(getattr(r, "address", "") or ""),
                                 tuple(sorted(str(m) for m in getattr(r, "models", []) or [])),
                                 tuple(sorted(str(a) for a in getattr(r, "agents", []) or [])))
                                for r in getattr(inventory, "runtimes", []) or []))
        return found_agents, runtimes, repr(getattr(self, "models_stamp", None))

    def projects_map_changed(self):
        """The map's data was read again: only when what it found changed does the next project list
        look at agents and runtimes again (no rescan for a redraw, a theme or a running change)."""
        facts = self.discovery_fingerprint()
        if facts == self.discovery_facts:
            return
        first = self.discovery_facts is None
        self.discovery_facts = facts
        lock = getattr(self, "_project_center_lock", None)
        if lock is None or first:
            return                             # the center's first scan sees these facts anyway

        def work():
            with lock:
                center = getattr(self, "_project_center", None)
            if center is not None and hasattr(center, "bump_discovery"):
                center.bump_discovery()        # under the center's own lock
        threading.Thread(target=work, name="project-discovery", daemon=True).start()

    # --- links from the Projects tab to the map, and back -------------------------------------------------

    def show_on_map_link(self, command: str):
        """Show on map from a project's team member (task 091)."""
        self.reveal_on_map(f"agent:{command}")

    def map_fix_link(self, node: str):
        """Fix on map from an issue (task 092): the box (an agent, a local LLM) that needs it."""
        self.reveal_on_map(node)

    def reveal_on_map(self, node_id: str):
        """Through the guards to the AI Map, which is read again, pans the box into view and marks it
        for a few seconds. Temporary: nothing about the layout, pins or zoom is saved."""
        if not isinstance(node_id, str) or ":" not in node_id or getattr(self, "booting", False):
            return

        def left(ok):
            if not ok:
                return
            self.forget_map_mark()
            self._reveal_seq += 1
            seq = self._reveal_seq
            self._map_read_seq += 1            # newer than a tab refresh still reading
            request = self._map_read_seq
            self.tab_decided = True
            self.map_stale = False             # drawn below from the fresh facts
            self.set_main_tab("map")
            self.save_tab("map")
            self.return_page = None
            self.show_view("main", save=False)
            self.focus_tab("map")
            generation = getattr(self, "map_generation", 0)

            def work():
                # Fresh facts, read off the GTK thread: an agent removed meanwhile is not revealed.
                try:
                    facts = self.read_map_facts()
                except Exception:
                    facts = None
                GLib.idle_add(read, facts)

            def read(facts):
                if seq != self._reveal_seq or self.main_tab != "map" or not self.window_shown():
                    return False
                if request != self._map_read_seq:
                    return False                   # a newer read of the map was asked for since
                if facts is None:
                    self.map_read_failed()         # stale, a note, a retry on a worker: no GTK scan
                    return False
                if generation == getattr(self, "map_generation", 0):   # else drawn from newer facts already
                    try:
                        self.refresh_map(facts=facts)
                    except Exception:
                        self.map_stale = True
                # After the map has been laid out once more (its boxes are placed while it is drawn).
                self._reveal_wait = GLib.timeout_add(120, lambda: (self.finish_reveal(node_id, seq), False)[1])
                return False
            threading.Thread(target=work, name="map-reveal", daemon=True).start()
        self.leave_pages(left)

    def projects_refocused(self):
        """The window came back to the front (Maprisma: the first list after a refocus forces a fresh
        discovery): one pending refresh, consumed by the next project list; the board reads now when
        it is shown."""
        if getattr(self, "_projects_closed", True):
            return
        self.projects_refocus = True
        ctrl = getattr(self, "projects_ctrl", None)
        if ctrl is not None and not ctrl.disposed and self.page_name() == "projects":
            ctrl.refresh()

    projects_refocus = False

    def window_shown(self) -> bool:
        window = getattr(self, "window", None)
        return window is None or not hasattr(window, "is_visible") or window.is_visible()

    def finish_reveal(self, node_id: str, seq=None):
        self._reveal_wait = 0
        if seq is not None and seq != self._reveal_seq:
            return
        if self.main_tab != "map" or self.page_name() != "main" or not self.window_shown():
            return
        view = self.side_map
        rect = view.box_rect(node_id)
        if rect is None:
            self.toast(_("This agent is not on the map.") if node_id.startswith("agent:")
                       else _("This is not on the map."), 5)
            return
        scroller = getattr(self, "map_scroller", None)
        if scroller is not None:
            x, y, w, h = rect
            for adjustment, start, size in ((scroller.get_hadjustment(), x, w), (scroller.get_vadjustment(), y, h)):
                page = adjustment.get_page_size()
                top = adjustment.get_value()
                if page > 0 and (start < top or start + size > top + page):
                    adjustment.set_value(max(adjustment.get_lower(),
                                             min(start + size / 2 - page / 2, adjustment.get_upper() - page)))
        view.set_focused(node_id)
        if self._reveal_timer:
            GLib.source_remove(self._reveal_timer)

        def unmark():
            self._reveal_timer = 0
            if view.focused == node_id:
                view.set_focused(None)
            return False
        self._reveal_timer = GLib.timeout_add_seconds(REVEAL_SECONDS, unmark)

    def forget_map_mark(self):
        self._reveal_seq += 1                  # a reveal still on its way is dropped
        if self._reveal_wait:
            GLib.source_remove(self._reveal_wait)
            self._reveal_wait = 0
        if self._reveal_timer:
            GLib.source_remove(self._reveal_timer)
            self._reveal_timer = 0
        view = getattr(self, "side_map", None)
        if view is not None and getattr(view, "focused", None) is not None:
            view.set_focused(None)

    def projects_for_agent(self, command: str):
        """"Used in N projects" on an agent box: the Projects tab with that agent's chip on the board."""
        if getattr(self, "booting", False) or self.departing:
            return
        agent = next((a for a in getattr(self, "agent_list", []) if a.command == command), None)
        name = agent.name if agent is not None else command

        def left(ok):
            if not ok:
                return
            self.return_page = None
            self.tab_decided = True
            self.set_main_tab("projects")
            self.open_projects(command, name)
            self.save_tab("projects")
            self.update_header()
        self.leave_pages(left)

    # --- Esc on a tab with nothing open: the window closes (task 070) ---------------------------------

    def projects_escape(self):
        """Esc on the board (no project open, no field): closes the window as its close button does."""
        self.close()
