"""AI Project Center pages: the state of the Projects tab and every action on it (Maprisma's
AiProjectCenterPageState in ai_project_center.dart, tasks 082, 086, 088-096). GTK-free: the pages
(projects_page.py, project_detail_page.py, project_phases_page.py, project_messages_view.py) draw
what it holds and call its methods; tests drive it without a display.

Every ProjectCenter call can block (a pause waits for the confirmed stop, about 10 s; a scan reads
agents and runtimes), so none runs on the GTK thread: `run(work, done)` runs work() on a worker and
done(result, error) back on the GTK thread (GLib.idle_add). The lessons of Maprisma's reviews are
kept here:

- Reads against mutations: a read records the generations when it starts (`selection` for the
  open project, `view_generation` for mutation results taken over, `order_seq` for moves,
  `mark_gen` per project for importance marks, `quick_failures` for failed quick actions); an older
  read never undoes what was shown since.
- One read at a time; a refresh asked for meanwhile runs once afterwards (never lost), and whoever
  waits on it is told when both have ended.
- A failed quick action (Pause, Delete, Move, a mark) keeps its reason on the board through the
  read it asks for (`keep_error`), and a read's own failure never hides it.
- After a move, a mark, Back or a delete the card the keyboard belongs to is revealed (`reveal`,
  `reveal_seq`), else the first card shown, else New project ("").
- Busy states: one page action at a time (`busy`), one quick action per card (`quick`), one move
  at a time (`moving`), one mark per card (`marking`), one question at a time (`confirming`,
  `leaving`).
- Drafts per project: message drafts and notes by project id, setup requests by "<id>:<phase>"; a
  deleted project's drafts go with it.
"""

from __future__ import annotations

import copy

import project_texts as texts
from i18n import _

FIELDS = ("title", "brief", "path", "plan")
POLL_SECONDS = 2
QUIET_TICKS = 8          # about every 15 s when nothing runs


def _rows(value) -> list:
    return [dict(v) for v in value if isinstance(v, dict)] if isinstance(value, list) else []


class ProjectsController:
    def __init__(self, call, run, ask, notify=lambda: None):
        """call(operation, params) -> dict, blocking (ProjectCenter.call; raises ProjectError).
        run(work, done): work() on a worker, done(result, error) on the GTK thread.
        ask(kind, question, answer): kind "confirm" with question (title, body, action) answers
        True/False; kind "leave" answers "save", "discard" or "cancel".
        notify(): the state changed; the pages draw it again."""
        self.call, self.run, self.ask, self.notify = call, run, ask, notify
        self.active = False            # the Projects page is in view (the poll reads only then)
        self.loaded = False
        self.unavailable = False       # the list could not be read: status unavailable, never zero
        self.store_error = None        # the store's fault from the summary (a banner)
        self.error = None              # the board's or the open page's message
        self.projects: list = []       # board items in the user's order
        self.profiles: list = []
        self.runtimes: list = []
        self.filter = None             # a bucket code
        self.importance = None         # high / medium / low
        self.view = "cards"            # cards / phases: kept while the page lives, not saved
        self.agent = None              # (command, name): the map's "Used in N projects" chip
        self.open = False              # a project (or a new one) is open over the board
        self.opened_id = None
        self.project = None            # the open project's view (None: a new one)
        self.tab = texts.TAB_GOAL
        self.phase = "run"             # Team and Setup: plan / run
        self.texts = dict.fromkeys(FIELDS, "")
        self.adopted = dict.fromkeys(FIELDS, "")
        self.run_team: list = []
        self.plan_team = None
        self.base_revision = None
        self.dirty = False
        self.busy = False
        self.confirming = False
        self.leaving = False
        self.quick: set = set()        # cards with a quick action on its way
        self.marking: dict = {}        # id -> the mark on its way
        self.mark_gen: dict = {}       # id -> marks sent and answered
        self.moving = False
        self.order_seq = 0
        self.view_generation = 0
        self.selection = 0
        self.keep_error = 0
        self.quick_failures = 0
        self.quick_reason = None
        self.reveal = None             # a card id ("" : the first card or New project)
        self.reveal_seq = 0
        self.adopt_version = 0         # the editor's fields take the texts again
        self.team_version = 0          # the team's form is built again
        self.setup_drafts: dict = {}
        self.setup_request = ""
        self.setup_agent = None
        self.message_drafts: dict = {}
        self.message_notes: dict = {}  # id -> (text, error)
        self.message_sending = None
        self.ticks = 0
        self._reading = False
        self._read_again = self._read_again_profiles = self._read_again_adopt = self._read_again_force = False
        self._next_waiters: list = []
        self.disposed = False          # the page is gone: late answers change nothing

    # --- what the pages show -------------------------------------------------------------------------

    @property
    def running(self) -> bool:
        return isinstance(self.project, dict) and self.project.get("status") in texts.ACTIVE_STATUSES

    @property
    def has_active(self) -> bool:
        return (self.running and self.open) or any(p.get("status") in texts.ACTIVE_STATUSES for p in self.projects)

    @property
    def team(self) -> list:
        return self.plan_team if self.phase == "plan" and self.plan_team is not None else self.run_team

    @property
    def team_locked(self) -> bool:
        return self.busy or self.running or (self.phase == "plan" and self.plan_team is None)

    @property
    def approved(self) -> bool:
        project = self.project or {}
        return bool(self.texts["plan"].strip()) and project.get("approval_valid") is True and not self.dirty

    @property
    def card_of_open(self):
        """The open project as its board card describes it (stage, activity, effective teams)."""
        pid = (self.project or {}).get("id")
        return next((p for p in self.projects if p.get("id") == pid), None) if pid else None

    def agent_projects(self) -> list:
        command = self.agent[0] if self.agent else None
        return [p for p in self.projects if texts.names_agent(p, command)]

    def shows(self, project: dict, agent: str | None = None) -> bool:
        return ((self.filter is None or project.get("bucket") == self.filter)
                and (self.importance is None or project.get("importance") == self.importance)
                and texts.names_agent(project, agent))

    def shown(self) -> list:
        command = self.agent[0] if self.agent else None
        return [p for p in self.projects if self.shows(p, command)]

    def counts(self) -> dict | None:
        """Bucket counts of the (agent-narrowed) board; None while it cannot be read."""
        if self.unavailable:
            return None
        projects = self.agent_projects()
        found = {code: 0 for code, _label, _icon in texts.FILTERS}
        for project in projects:
            if project.get("bucket") in found:
                found[project["bucket"]] += 1
        found["all"] = len(projects)
        return found

    def importance_counts(self) -> dict | None:
        projects = self.agent_projects()
        if self.importance is None and not any(texts.importance_of(p.get("importance")) for p in projects):
            return None                    # no marks at all (an older store): no importance filter
        return {code: (None if self.unavailable else sum(1 for p in projects if p.get("importance") == code))
                for code, _label, _icon in texts.IMPORTANCES}

    def card_busy(self, pid: str) -> bool:
        return pid in self.quick or pid in self.marking

    def setup_helpers(self) -> list:
        return [p for p in self.profiles if isinstance(p.get("setup_argv"), list) and p["setup_argv"]]

    @property
    def setup_helper(self):
        helpers = self.setup_helpers()
        if any(p.get("id") == self.setup_agent for p in helpers):
            return self.setup_agent
        lead = next((m.get("agent") for m in self.team if m.get("role") == "lead"), None)
        return next((p["id"] for p in helpers if p.get("id") == lead), None) or (helpers[0]["id"] if helpers else None)

    def profile(self, agent):
        return next((p for p in self.profiles if p.get("id") == agent), None)

    def agent_name(self, agent: str) -> str:
        return str((self.profile(agent) or {}).get("name") or agent)

    # --- filters, view, agent ------------------------------------------------------------------------

    def set_filter(self, code):
        """A filter chosen again clears it; All (None) clears the importance filter and the agent too."""
        if code is None:
            self.filter = self.importance = self.agent = None
        else:
            self.filter = None if self.filter == code else code
        self.notify()

    def set_importance_filter(self, code):
        self.importance = None if code is None or self.importance == code else code
        self.notify()

    def set_view(self, view: str):
        if view in ("cards", "phases") and view != self.view:
            self.view = view
            self.notify()

    def set_agent(self, command: str | None, name: str = ""):
        self.agent = (command, name or command) if command else None
        self.notify()

    def reset_board_choices(self):
        """Switching to the AI Map resets the filter, the agent chip and Cards | Phases (not saved)."""
        self.filter = self.importance = self.agent = None
        self.view = "cards"
        self.notify()

    def reveal_card(self, pid: str):
        self.reveal = pid
        self.reveal_seq += 1

    def revealed(self, seq: int) -> bool:
        """The board brought the card of reveal `seq` into view: True when it is still the one asked."""
        if seq != self.reveal_seq or self.reveal is None:
            return False
        self.reveal = None
        return True

    # --- reads ---------------------------------------------------------------------------------------

    def dispose(self):
        """The page is gone: answers still on their way and later calls change nothing."""
        self.disposed = True
        self.active = False
        self._next_waiters = []

    def set_active(self, active: bool):
        was, self.active = self.active, active
        if active and not was:
            self.refresh(profiles=True, force=True)     # back on the page: readiness looked at again

    def tick(self):
        """Every POLL_SECONDS while the app runs: every tick while something runs or stops, else about
        every 15 s (also to retry after the backend could not be reached)."""
        if not self.active or self.busy:
            return
        self.ticks += 1
        stopping = any(p.get("processes_stopping") is True for p in self.projects)
        if self.has_active or stopping or self.ticks >= QUIET_TICKS:
            self.refresh()

    def refresh(self, profiles: bool = False, adopt: bool = False, then=None, force: bool = False):
        """One read at a time; a refresh asked for meanwhile runs once afterwards (never lost). `adopt`:
        the read after a message action takes the open project even while the page is still busy with
        it. `force` (F5, Refresh, Retry): the center looks at agents and runtimes again (readiness).
        then(): called once this read ended, or, asked while one reads, once the read after it ended
        (one read later at most: asks during that one wait for the one after it)."""
        if self.disposed:
            return
        if self._reading:
            if then is not None:
                self._next_waiters.append(then)
            self._read_again = True
            self._read_again_profiles |= profiles
            self._read_again_adopt |= adopt
            self._read_again_force |= force
            return
        self._start_read(profiles, adopt, force, [then] if then is not None else [])

    def _start_read(self, profiles: bool, adopt: bool, force: bool, waiters: list):
        self._reading = True
        since = {"selection": self.selection, "generation": self.view_generation, "moves": self.order_seq,
                 "marks": dict(self.mark_gen), "failures": self.quick_failures}
        pid = None if not self.open else (self.project or {}).get("id")
        want_profiles = not self.loaded or profiles

        def work():
            listed = self.call("list", {"refresh": True} if force else {})
            answer, profile_error = None, None
            if want_profiles:
                try:
                    answer = self.call("profiles", {})
                except Exception as error:      # a failed profile read is only a message
                    profile_error = error
            selected, detail_error = None, None
            if pid is not None:
                try:
                    selected = self.call("get", {"id": pid}).get("project")
                except Exception as error:
                    detail_error = error
            return listed, answer, profile_error, selected, detail_error

        self.run(work, lambda result, error: self._read_done(result, error, since, pid, adopt, waiters))

    def _reason(self, error, since) -> str:
        """A read's failure as the message, unless a quick action failed since it began or its reason is
        still kept: then that reason stays (the read's failure shows as "status unavailable")."""
        if self.quick_failures != since["failures"] or self.keep_error > 0:
            return self.quick_reason
        return texts.failure(error)

    def _read_done(self, result, error, since, pid, adopt, waiters):
        if self.disposed:
            return
        if error is not None:
            self.error = self._reason(error, since)
            self.unavailable = True
            self.ticks = 0
        else:
            listed, answer, profile_error, selected, detail_error = result
            fresh = _rows(listed.get("projects"))
            for project in fresh:
                self._keep_mark(project, since["marks"])
            self.projects = self._hold_order(fresh, moved=self.moving or since["moves"] != self.order_seq)
            self.ticks = 0
            self.unavailable = False
            summary = listed.get("summary")
            fault = summary.get("error") if isinstance(summary, dict) else None
            self.store_error = fault if isinstance(fault, str) and fault else None
            # An earlier board message ends with a read (a failed quick action's survives the read it asked for).
            if not self.open and self.keep_error == 0:
                self.error = None
            if answer is not None:
                self.profiles = _rows(answer.get("profiles"))
                self.runtimes = _rows(answer.get("runtimes"))
            if (isinstance(selected, dict) and (not self.busy or adopt) and since["selection"] == self.selection
                    and since["generation"] == self.view_generation and pid == (self.project or {}).get("id")):
                selected = dict(selected)
                self._keep_mark(selected, since["marks"])
                if self.dirty:
                    self.project = selected
                else:
                    self._adopt(selected)
            self.loaded = True
            # A late answer about a project closed meanwhile is not shown.
            if detail_error is not None and since["selection"] == self.selection and self.open:
                self.error = self._reason(detail_error, since)
            elif profile_error is not None and self.open:
                self.error = texts.failure(profile_error)
        self._reading = False
        self.notify()
        if self._read_again:
            again = (self._read_again_profiles, self._read_again_adopt, self._read_again_force)
            self._read_again = self._read_again_profiles = self._read_again_adopt = self._read_again_force = False
            queued, self._next_waiters = self._next_waiters, []
            self._start_read(*again, queued)
        for waiter in waiters:
            waiter()

    def _hold_order(self, fresh: list, moved: bool) -> list:
        """The read's order (the user's), unless a move was sent after the read began or is still on its
        way: then the cards stay as moved, new ones on top."""
        if not moved:
            return fresh
        by_id = {str(p.get("id")): p for p in fresh}
        kept = [by_id.pop(str(old.get("id"))) for old in self.projects if str(old.get("id")) in by_id]
        return list(by_id.values()) + kept

    @staticmethod
    def _ordered(projects: list, ids: list) -> list:
        by_id = {str(p.get("id")): p for p in projects}
        return [by_id.pop(i) for i in ids if i in by_id] + list(by_id.values())

    def _local_mark(self, pid: str):
        if pid in self.marking:
            return self.marking[pid]
        card = next((p for p in self.projects if p.get("id") == pid), None)
        if card is not None and card.get("importance"):
            return card["importance"]
        return (self.project or {}).get("importance") if (self.project or {}).get("id") == pid else None

    def _keep_mark(self, project: dict, since: dict):
        """A read keeps the mark shown now when one was sent or answered since the read began."""
        pid = project.get("id")
        if not isinstance(pid, str):
            return
        if pid not in self.marking and self.mark_gen.get(pid, 0) == since.get(pid, 0):
            return
        mark = self._local_mark(pid)
        if mark:
            project["importance"] = mark

    # --- the open project's draft --------------------------------------------------------------------

    def _adopt(self, project):
        before = (self.run_team, self.plan_team, (self.project or {}).get("id"))
        self.project = project
        for field in FIELDS:
            self.texts[field] = str((project or {}).get(field) or "")
        self.adopted = dict(self.texts)
        self.run_team = _rows((project or {}).get("team"))
        plan_team = (project or {}).get("plan_team")
        self.plan_team = _rows(plan_team) if isinstance(plan_team, list) else None
        if project is None and self.profiles:
            first = self.profiles[0]
            modes = first.get("access_modes") or ["read-only", "project-edit"]
            self.run_team = [{"agent": first.get("id"), "role": "lead", "instructions": "", "access": modes[-1]}]
        for team in [self.run_team] + ([self.plan_team] if self.plan_team is not None else []):
            for member in team:
                modes = (self.profile(member.get("agent")) or {}).get("access_modes") or []
                if "access" not in member and len(modes) == 1:
                    member["access"] = modes[0]
        self.base_revision = (project or {}).get("revision")
        self._restore_setup_request()
        self.dirty = False
        self.adopt_version += 1
        if before != (self.run_team, self.plan_team, (project or {}).get("id")):
            self.team_version += 1        # the team's form is built again only when the team changed

    def _draft_key(self, phase: str | None = None) -> str:
        return f"{(self.project or {}).get('id') or 'new'}:{phase or self.phase}"

    def _restore_setup_request(self):
        requests = (self.project or {}).get("setup_requests")
        stored = requests.get(self.phase) if isinstance(requests, dict) else (self.project or {}).get("setup_request")
        self.setup_request = self.setup_drafts.get(self._draft_key(), str(stored or ""))

    def _forget_setup_drafts(self, prefix: str):
        for key in [k for k in self.setup_drafts if k.startswith(prefix)]:
            del self.setup_drafts[key]

    def set_text(self, field: str, value: str):
        """One of the four fields: only a text that differs from the adopted one counts as a change."""
        was = self.texts.get(field, "")
        self.texts[field] = value
        if not self.dirty and value != self.adopted.get(field):
            self.dirty = True
            self.notify()
        elif field == "plan" and bool(was.strip()) != bool(value.strip()):
            self.notify()                 # Approve plan follows whether there is a plan

    def changed(self):
        """An edit outside the four fields (a member's instructions): always a change."""
        if not self.dirty:
            self.dirty = True
            self.notify()

    def set_tab(self, tab: int):
        if tab != self.tab:
            self.tab = tab
            self.notify()

    def change_phase(self, phase: str):
        if phase == self.phase or self.busy:
            return
        self.setup_drafts[self._draft_key()] = self.setup_request
        self.phase = phase
        self.selection += 1
        self.team_version += 1
        self._restore_setup_request()
        self.notify()

    def set_setup_request(self, text: str):
        self.setup_request = text
        self.setup_drafts[self._draft_key()] = text
        self.changed()

    def set_setup_agent(self, agent):
        self.setup_agent = agent
        self.notify()

    def toggle_plan_team(self):
        if self.busy or self.running:
            return
        if self.plan_team is None:
            self.plan_team = [dict(m, access="read-only") for m in copy.deepcopy(self.run_team)]
        else:
            self.plan_team = None
        self.selection += 1
        self.team_version += 1
        self.dirty = True
        self.notify()

    def add_member(self):
        if self.team_locked or not self.profiles:
            return
        first = self.profiles[0]
        team = self.team
        access = "read-only" if self.phase == "plan" or team else \
            (first.get("access_modes") or ["read-only", "project-edit"])[-1]
        team.append({"agent": first.get("id"), "role": "lead" if not team else "reviewer", "instructions": "",
                     "access": access})
        self.team_version += 1
        self.dirty = True
        self.notify()

    def remove_member(self, index: int):
        if self.team_locked or not 0 <= index < len(self.team):
            return
        del self.team[index]
        self.team_version += 1
        self.dirty = True
        self.notify()

    def set_member(self, index: int, key: str, value):
        """A member's agent, role, access, runtime, model, subagents or instructions."""
        if self.team_locked or not 0 <= index < len(self.team):
            return
        member = self.team[index]
        rebuild = True
        if key == "agent":
            member["agent"] = value
            for gone in ("model", "subagents", "runtime", "provider"):
                member.pop(gone, None)
            modes = (self.profile(value) or {}).get("access_modes") or ["read-only", "project-edit"]
            member["access"] = "read-only" if self.phase == "plan" or member.get("role") == "reviewer" else modes[-1]
        elif key == "role":
            member["role"] = value
            if value == "reviewer":
                member["access"] = "read-only"
        elif key == "runtime":
            for gone in ("runtime", "provider", "model"):
                member.pop(gone, None)
            runtime = next((r for r in self.runtimes if r.get("key") == value), None) if value else None
            bound = ((runtime or {}).get("bindings") or {}).get(member.get("agent")) if runtime else None
            if isinstance(bound, dict):
                member["runtime"] = value
                member["provider"] = bound.get("provider")
                models = bound.get("models") or []
                if models:
                    member["model"] = models[0]
        elif key in ("model", "instructions"):
            rebuild = False
            value = value.strip() if key == "model" else value
            if key == "model" and not value:
                member.pop("model", None)
            else:
                member[key] = value
        elif key == "bound_model":
            member["model"] = value
        else:
            member[key] = value
        if rebuild:
            self.team_version += 1
        self.dirty = True
        self.notify()

    # --- choosing a project, leaving it --------------------------------------------------------------

    def before_leave(self, done):
        """Unsaved changes ask Save / Discard / Cancel first; done(True) when the page may be left."""
        def committed(commit):
            if commit is not None:
                commit()
            done(commit is not None)
        self.prepare_leave(committed)

    def prepare_leave(self, done):
        """done(commit) with a function the caller calls once the whole departure succeeded, or
        done(None): the page stays as it is (Cancel, a failed save, a step on its way)."""
        if self.busy or self.leaving:
            done(None)
            return
        if not self.dirty:
            done(lambda: None)
            return
        self.leaving = True
        self.notify()

        def answer(choice):
            if choice == "discard":
                self.leaving = False

                def discard():
                    self._forget_setup_drafts(f"{(self.project or {}).get('id') or 'new'}:")
                    self._adopt(self.project)
                    self.notify()
                done(discard)
                return
            if choice != "save":
                self.leaving = False
                self.notify()
                done(None)
                return
            self.busy = True                  # saving locks the page: nothing typed meanwhile is lost
            self.notify()

            def saved(ok):
                self.busy = False
                self.leaving = False
                self.notify()
                done((lambda: None) if ok else None)
            self._save(saved)
        self.ask("leave", None, answer)

    def _choose(self, project, done):
        """Shows `project` (None: a new one); done(False) when it could not be read."""
        def left(ok):
            if not ok:
                done(False)
                return
            self.selection += 1
            self.busy = project is not None
            self.error = None
            if project is None:
                self._forget_setup_drafts("new:")
            since_marks, failures = dict(self.mark_gen), self.quick_failures
            self.notify()
            if project is None:
                self._adopt(None)
                self.tab = texts.TAB_GOAL
                self.notify()
                done(True)
                return

            def read(result, error):
                self.busy = False
                if error is not None:
                    self.error = texts.failure(error) if self.quick_failures == failures else self.quick_reason
                    self.notify()
                    done(False)
                    return
                found = dict(result.get("project") or {})
                self._keep_mark(found, since_marks)
                self._adopt(found)
                self.tab = texts.TAB_GOAL
                self.notify()
                done(True)
            self.run(lambda: self.call("get", {"id": project.get("id")}), read)
        self.before_leave(left)

    def open_card(self, project, tab: int = texts.TAB_GOAL, done=None):
        """Open a card's project (None: New project); opened only once it was read (a deleted project
        stays on the board, with the reason)."""
        def chosen(ok):
            if ok:
                self.opened_id = project.get("id") if project else None
                self.open = True
                self.tab = tab
                self.notify()
            if done is not None:
                done(ok)
        self._choose(project, chosen)

    def back_to_board(self, done=None):
        """Back from the project page: unsaved changes ask first; the keyboard goes to the card that
        was opened (shown again even when it left the filter or the agent chip)."""
        if not self.open:
            if done is not None:
                done(True)
            return

        def left(ok):
            if not ok:
                if done is not None:
                    done(False)
                return
            pid = self.opened_id or (self.project or {}).get("id")
            self.open = False
            if self.keep_error == 0:
                self.error = None
            card = next((p for p in self.projects if p.get("id") == pid), None)
            if card is not None:
                if self.filter is not None and card.get("bucket") != self.filter:
                    self.filter = None
                if self.importance is not None and card.get("importance") != self.importance:
                    self.importance = None
                if self.agent is not None and not texts.names_agent(card, self.agent[0]):
                    self.agent = None
            shown = [str(p.get("id")) for p in self.shown()]
            self.reveal_card(pid if pid in shown else (shown[0] if shown else ""))
            self.notify()
            if done is not None:
                done(True)
        self.before_leave(left)

    def fix_issue(self, project, issue: dict, map_fix=None):
        """An issue's link (task 092): the map for a box it can act on (map_fix(node)), else the
        project's Goal or Team tab (from the board the project opens there)."""
        fix = texts.issue_fix(issue, map_fix is not None)
        if fix == "map":
            map_fix(issue["node"])
            return
        tab = texts.TAB_GOAL if fix == "goal" else texts.TAB_TEAM
        if self.open:
            self.set_tab(tab)
        else:
            self.open_card(project, tab=tab)

    # --- quick actions on the board ------------------------------------------------------------------

    def _quick_failed(self, error, message=None):
        self.error = message or texts.failure(error)
        self.keep_error += 1
        self.quick_failures += 1
        self.quick_reason = self.error

    def pause_from_board(self, project: dict):
        """Pause or Retry pause from a card, for the run the card shows."""
        pid = str(project.get("id"))
        if pid in self.quick:
            return
        self.quick.add(pid)
        self.notify()
        params = {"id": pid}
        if project.get("run_id") is not None:
            params["run_id"] = project["run_id"]

        def answered(_result, error):
            kept = error is not None
            if kept:
                self._quick_failed(error)

            def after():
                if kept:
                    self.keep_error -= 1
                self.quick.discard(pid)       # off until the board shows the new state
                self.notify()
            self.refresh(then=after)
        self.run(lambda: self.call("pause", params), answered)

    def delete_from_board(self, project: dict):
        """The card's trash symbol (task 094): the footer's question, then the saved project is removed
        (its folder is kept); a refusal keeps its reason on the board."""
        pid = str(project.get("id"))
        if pid in self.quick or self.busy or self.confirming:
            return
        self.busy = True
        self.notify()

        def finish(kept=False):
            if kept:
                self.keep_error -= 1
            self.quick.discard(pid)
            self.busy = False
            self.notify()

        def answer(yes):
            if not yes:
                finish()
                return
            self.quick.add(pid)
            self.notify()

            def answered(_result, error):
                kept = error is not None
                if kept:
                    self._quick_failed(error)
                else:
                    self._forget_setup_drafts(f"{pid}:")
                    self._forget_messages(pid)
                self.refresh(then=lambda: finish(kept))
            self.run(lambda: self.call("delete", {"id": pid, "revision": project.get("revision")}), answered)
        self._confirm(texts.DELETE_QUESTION, answer)

    def move_from_board(self, pid: str, target, after: bool):
        """A card moved on the board (drag, Move menu or keys): target None is the top (with `after`:
        the end); after a card means before the next card the board shows, else before the next one of
        the whole order (a filter hides it), else the end."""
        ids = [str(p.get("id")) for p in self.projects]
        if target is None:
            before = None if after else next((i for i in ids if i != pid), None)
            if not after and before is None:
                return
        elif not after:
            before = target
        else:
            if target not in ids:
                return                        # a card gone meanwhile: no place to go after
            shown = [str(p.get("id")) for p in self.shown()]
            there = shown.index(target) if target in shown else -1
            before = (next((i for i in shown[there + 1:] if i != pid), None) if there >= 0 else None) \
                or next((i for i in ids[ids.index(target) + 1:] if i != pid), None)
        self.move(pid, before)

    def move_options(self, pid: str) -> list:
        """The Move menu: [(key, label, (target, after) or None when not possible here)]."""
        shown = [str(p.get("id")) for p in self.shown()]
        ids = [str(p.get("id")) for p in self.projects]
        at = shown.index(pid) if pid in shown else -1
        on = at >= 0 and not self.unavailable and not self.card_busy(pid)
        return [("up", _("Move up"), (shown[at - 1], False) if on and at > 0 else None),
                ("down", _("Move down"), (shown[at + 1], True) if on and at < len(shown) - 1 else None),
                ("top", _("Move to top"), (None, False) if on and ids and ids[0] != pid else None),
                ("end", _("Move to end"), (None, True) if on and ids and ids[-1] != pid else None)]

    def move_by(self, pid: str, key: str) -> bool:
        """Ctrl+Up / Down / Home / End on a card."""
        for name, _label, place in self.move_options(pid):
            if name == key and place is not None:
                self.move_from_board(pid, *place)
                return True
        return False

    def move(self, pid: str, before):
        """Sends move: the card moves at once, then takes the answer's order; a refusal puts it back and
        keeps its reason on the board."""
        if self.moving or pid in self.quick or self.unavailable:
            return
        old = [str(p.get("id")) for p in self.projects]
        if pid not in old:
            return
        rest = [i for i in old if i != pid]
        if before is not None and before not in rest:
            return
        moved = list(rest)
        moved.insert(len(rest) if before is None else rest.index(before), pid)
        if moved == old:
            return
        self.moving = True
        self.order_seq += 1
        self.projects = self._ordered(self.projects, moved)
        self.reveal_card(pid)                 # the keyboard stays with the card
        self.notify()

        def answered(result, error):
            kept = False
            if error is None:
                order = result.get("order") if isinstance(result, dict) else None
                if isinstance(order, list):
                    self.projects = self._ordered(self.projects, [str(i) for i in order])
                    self.reveal_card(pid)
            else:
                self.projects = self._ordered(self.projects, old)
                self.reveal_card(pid)
                self._quick_failed(error)
                kept = True
            self.moving = False
            self.order_seq += 1               # a read begun while it was on its way is older too
            self.notify()
            if kept:
                self.refresh(then=self._release_error)
        self.run(lambda: self.call("move", {"id": pid, "before": before}), answered)

    def _release_error(self):
        self.keep_error -= 1

    def set_importance(self, pid: str, code: str):
        """Sends set_importance from a card's mark or the Goal tab: the mark changes at once (the open
        project's draft stays as it is); a refusal puts it back with its reason."""
        if pid in self.marking or pid in self.quick or self.unavailable:
            return
        was = self._local_mark(pid)

        def mark(value):
            for item in [p for p in self.projects if p.get("id") == pid] + \
                    ([self.project] if (self.project or {}).get("id") == pid else []):
                if value is None:
                    item.pop("importance", None)
                else:
                    item["importance"] = value
        self.marking[pid] = code
        self.mark_gen[pid] = self.mark_gen.get(pid, 0) + 1
        mark(code)
        self.notify()

        def answered(result, error):
            kept = False
            if error is None:
                project = result.get("project") if isinstance(result, dict) else None
                if isinstance(project, dict) and isinstance(project.get("importance"), str):
                    mark(project["importance"])
            else:
                mark(was)
                self._quick_failed(error)
                kept = True
            self.marking.pop(pid, None)
            self.mark_gen[pid] = self.mark_gen.get(pid, 0) + 1
            self.notify()
            if kept:
                self.refresh(then=self._release_error)
        self.run(lambda: self.call("set_importance", {"id": pid, "importance": code}), answered)

    # --- the footer's actions ------------------------------------------------------------------------

    def _confirm(self, question, answer):
        self.confirming = True
        self.notify()

        def answered(yes):
            self.confirming = False
            self.notify()
            answer(bool(yes))
        self.ask("confirm", question, answered)

    def _save(self, done):
        if not (self.texts["title"].strip() and self.texts["brief"].strip() and self.texts["path"].strip()):
            self.error = _("Enter a title, goal and existing folder.")
            self.notify()
            done(False)
            return
        project = self.project
        key_id = (project or {}).get("id") or "new"
        params = {"title": self.texts["title"].strip(), "brief": self.texts["brief"].strip(),
                  "path": self.texts["path"].strip(), "plan": self.texts["plan"],
                  "team": copy.deepcopy(self.run_team), "plan_team": copy.deepcopy(self.plan_team),
                  "setup_requests": {phase: self.setup_drafts.get(
                      f"{key_id}:{phase}", ((project or {}).get("setup_requests") or {}).get(phase, ""))
                      for phase in ("plan", "run")}}
        if project is not None:
            params["id"] = project.get("id")
        if self.base_revision is not None:
            params["revision"] = self.base_revision

        def answered(result, error):
            if error is not None:
                self.error = texts.failure(error)
                self.notify()
                done(False)
                return
            self._forget_setup_drafts(f"{key_id}:")
            self._adopt(dict(result.get("project") or {}))
            self.error = None
            self.notify()
            self.refresh(then=lambda: done(True))
        self.run(lambda: self.call("save", params), answered)

    def perform(self, operation: str):
        """A footer action (save, plan, review, run, continue, pause, delete, setup, setup_apply)."""
        if self.busy:
            return
        failures = self.quick_failures
        setup_text, setup_target, setup_agent = self.setup_request, self.phase, self.setup_helper
        self.busy = True
        self.error = None
        self.notify()
        state = {"back_after_delete": False}

        def finish():
            self.busy = False
            if state["back_after_delete"] and self.open:
                # Deleted from its own page: back to the board, the keyboard on the first card shown.
                self.open = False
                self.opened_id = None
                if not self.unavailable:
                    self.error = None if self.quick_failures == failures else self.quick_reason
                shown = [str(p.get("id")) for p in self.shown()]
                self.reveal_card(shown[0] if shown else "")
            self.notify()

        def send():
            project = self.project
            params = {"id": project["id"], "revision": project.get("revision")}
            if operation == "pause" and project.get("run_id") is not None:
                params["run_id"] = project["run_id"]
            if operation == "review":
                params["approved"] = True
            if operation == "continue" or (operation == "run" and project.get("needs_reconciliation") is True):
                params["acknowledge_interrupted"] = True
            if operation == "setup":
                params.update({"request": setup_text, "target": setup_target})
                if setup_agent:
                    params["agent"] = setup_agent

            def answered(result, error):
                if error is not None:
                    self.error = texts.failure(error)
                    finish()
                    return
                if operation == "delete":
                    gone = (self.project or {}).get("id")
                    self._forget_setup_drafts(f"{gone}:")
                    self._forget_messages(gone)
                    self.selection += 1        # a read of the deleted project on its way is outdated
                    self.error = None if self.quick_failures == failures else self.quick_reason
                    self._adopt(None)
                    self.projects = [p for p in self.projects if p.get("id") != gone]
                    state["back_after_delete"] = self.open
                elif isinstance(result.get("project"), dict):
                    if operation == "setup_apply":
                        proposal = (self.project or {}).get("setup_proposal")
                        self.phase = "plan" if isinstance(proposal, dict) and proposal.get("target") == "plan" \
                            else "run"
                        self.selection += 1
                    self._adopt(dict(result["project"]))
                    self.tab = {"plan": texts.TAB_PLAN, "run": texts.TAB_ACTIVITY, "continue": texts.TAB_ACTIVITY,
                                "setup_apply": texts.TAB_TEAM}.get(operation, self.tab)
                self.notify()
                self.refresh(then=finish)
            self.run(lambda: self.call(operation, params), answered)

        def confirmed(go):
            if go:
                send()
            else:
                finish()

        def ready():
            project = self.project
            if operation == "delete":
                self._confirm(texts.DELETE_QUESTION, confirmed)
            elif operation == "review":
                self._confirm(texts.APPROVE_QUESTION, confirmed)
            elif operation == "continue" or (operation == "run" and (project or {}).get("needs_reconciliation") is True):
                self._confirm(texts.CONTINUE_QUESTION, confirmed)
            else:
                send()

        if operation == "save":
            self._save(lambda _ok: finish())
        elif operation == "delete":
            if self.project is None:
                finish()
            else:
                ready()                       # deleting asks first and never saves the draft it removes
        elif self.dirty or self.project is None:
            self._save(lambda ok: ready() if ok else finish())
        else:
            ready()

    # --- messages to a running project (task 096) ----------------------------------------------------

    def set_message_draft(self, pid: str, text: str):
        self.message_drafts[pid] = text

    def _forget_messages(self, pid):
        self.message_notes.pop(pid, None)
        self.message_drafts.pop(pid, None)

    def _take_project(self, result):
        """A mutation's project view; edits in the form stay. Reads that started before are older."""
        view = result.get("project") if isinstance(result, dict) else None
        if not isinstance(view, dict) or view.get("id") != (self.project or {}).get("id"):
            return
        self.view_generation += 1
        if self.dirty:
            self.project = dict(view)
        else:
            self._adopt(dict(view))

    def _after_message(self, done):
        self.view_generation += 1
        self.refresh(adopt=True, then=done)

    def send_message(self, text: str, now: bool):
        """Send (queue) or Send now; Send now first asks as Continue. The draft is cleared once stored
        (only this text: one typed meanwhile stays); the note stays with the project."""
        project = self.project
        pid = (project or {}).get("id")
        if self.busy or not isinstance(pid, str):
            return
        self.busy = True
        self.message_sending = pid
        self.notify()

        def finish():
            self.busy = False
            self.message_sending = None
            self.notify()

        def send():
            self.message_notes.pop(pid, None)
            params = {"id": pid, "run_id": project.get("run_id"), "text": text, "mode": "now" if now else "queue"}
            if now:
                params["acknowledge_interrupted"] = True
            self.notify()

            def answered(result, error):
                if error is not None:
                    self.message_notes[pid] = (texts.message_failure(error), True)
                else:
                    if self.message_drafts.get(pid) == text:
                        self.message_drafts[pid] = ""
                    if now and result.get("continued") is not True:
                        self.message_notes[pid] = (texts.not_continued(result.get("reason"), result.get("message")),
                                                   False)
                    self._take_project(result)
                self.notify()
                self._after_message(finish)
            self.run(lambda: self.call("message", params), answered)

        if now:
            self._confirm(texts.CONTINUE_QUESTION, lambda yes: send() if yes else finish())
        else:
            send()

    def drop_message(self, message_id: str):
        project = self.project
        pid = (project or {}).get("id")
        if self.busy or not isinstance(pid, str):
            return
        self.busy = True
        self.message_sending = pid
        self.message_notes.pop(pid, None)
        self.notify()

        def finish():
            self.busy = False
            self.message_sending = None
            self.notify()

        def answered(result, error):
            if error is not None:
                self.message_notes[pid] = (texts.message_failure(error), True)
            else:
                self._take_project(result)
            self.notify()
            self._after_message(finish)
        self.run(lambda: self.call("message_drop", {"id": pid, "message_id": message_id}), answered)
