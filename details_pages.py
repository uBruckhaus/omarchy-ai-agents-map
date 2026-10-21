"""AI Agents Map: the agent and local LLM details pages, with the Healthy & Security check (a mixin
of ai_popup.AgentsMap, split off to keep ai_popup.py small; its methods use the window's state
through self)."""

from gi.repository import Adw, GLib, Gtk

import agent_details
import agent_security
import agent_status
import ai_core
import claude_sessions
import desktop
import edition
import graph
import harnesses
import herdr_state
import inventory
import link_hints
import providers
import runtime_ctl
import threading
import time
from i18n import _, N_, ngettext
from ui_helpers import short_number, TokenChart, when
try:
    import addon_api            # Asterism only: signed add-ons (not in the Omarchy plugin)
    import addons
except ImportError:
    addon_api = addons = None


class DetailsPages:
    # --- agent and local LLM details (the (i) on their cards) -----------------------------------

    def build_status_page(self) -> Gtk.Widget:
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
        back.connect("clicked", lambda *_: self.close_status())
        header.append(back)
        self.status_title = Gtk.Label(xalign=0, hexpand=True)
        self.status_title.add_css_class("am-section-title")
        header.append(self.status_title)
        page.append(header)
        rule = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        rule.add_css_class("am-settings-rule")
        page.append(rule)
        self.status_body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP)
        self.status_body.add_css_class("am-settings")
        page.append(self.page_scroller(self.status_body))
        self.status_showing = None
        return page

    def clear_status(self, title: str):
        self.status_title.set_label(title.upper())
        while (child := self.status_body.get_first_child()) is not None:
            self.status_body.remove(child)

    def close_status(self):
        self.status_showing = None
        self.show_view(self.view, save=False)

    def open_agent_status(self, agent, refreshed: bool = False):
        """Usage over time, whether it is set up (and what to do if not), its configuration. An old
        usage record (plan, limits) is refreshed in the background, then the page shows it again."""
        self.clear_status(agent.name)
        self.status_showing = ("agent", agent.command)
        self.note(self.status_body, _("Reading…"))
        self.show_page("status")
        inventory = self.inventory

        def work():
            info = agent_details.details_for(agent.command)
            record = agent_details.usage_record(agent.command)
            sessions = None
            harness = harnesses.get(agent.command)
            if harness is not None and harness.sessions is not None and agent.installed:
                try:
                    sessions = harness.sessions.collect()           # numbers only
                except Exception:
                    sessions = None
            try:
                usage = agent_status.usage_for(agent.command, record, sessions)
            except Exception:
                usage = None
            logged_in = agent_status.login_state(agent.command) if agent.installed else None
            checks = agent_status.agent_checks(agent, info, record, inventory, link_hints.load(), logged_in,
                                               issue=agent_status.load_issues().get(agent.command))
            try:
                panes = herdr_state.snapshot()
            except Exception:
                panes = None
            try:
                servers = sum(1 for m in inventory.mcp if agent.command in m.agents)
                security = agent_security.report_for(agent.command, servers) if agent.installed else None
            except Exception:
                security = None
            GLib.idle_add(done, info, usage, checks, sessions, panes, security)

        def done(info, usage, checks, sessions, panes, security):
            if self.status_showing == ("agent", agent.command):
                self.fill_agent_status(agent, info, usage, checks, sessions, panes, security)
                if not refreshed and agent.installed and agent_details.usage_is_stale(agent.command):
                    threading.Thread(target=refresh, daemon=True).start()
            return False

        def refresh():
            if agent_details.refresh_usage(agent.command):
                GLib.idle_add(again)

        def again():
            if self.status_showing == ("agent", agent.command):
                self.open_agent_status(agent, refreshed=True)
            return False

        threading.Thread(target=work, daemon=True).start()

    def fill_agent_status(self, agent, info, usage, checks, sessions=None, panes=None, security=None):
        self.clear_status(agent.name)
        body = self.status_body
        # At the top: one sentence, then what it is doing now.
        state, sentence = agent_status.summary_line(checks)
        models = self.running.get(agent.command)
        if models:
            now = self.instance_label(agent)
        elif info.last_used or (usage and usage.last):
            now = _("Not running · last used {0}").format(when(max(x for x in (info.last_used, usage and usage.last) if x)))
        else:
            now = _("Not running")
        hero = self.status_hero(state, sentence, now)
        body.append(hero)
        self.details_cards = {"hero": hero}           # cards add-on sections join (fill_addon_cards)
        self.pending_health = []                      # add-on sections for the Health card, built later
        if panes is not None:
            # herdr's live state, read again every few seconds while this page shows.
            self.herdr_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP_TIGHT)
            body.append(self.herdr_box)
            self.fill_herdr_panes(agent, panes)
            self.watch_herdr(agent)

        self.fill_usage_card(body, usage, info)

        harness = harnesses.get(agent.command)
        if sessions and harness is not None and harness.sessions is not None:
            self.fill_session_projects(body, sessions, harness)
        if harness is not None and agent.installed:
            # Add-ons' sections, for every agent (with or without sessions); Topics only where there are
            # sessions to be about.
            self.fill_addon_cards(body, harness, sessions or {}, time.time() - 30 * 86400, topics=bool(sessions))
        if harness is None and hasattr(self, "open_addons"):
            # No adapter, built in or from an add-on: say so, and where to ask for one.
            self.note(body, _("No add-on for {0} yet: Asterism only shows that it is installed (no sessions, "
                              "security settings, MCP servers or skills).").format(agent.name))
            import urllib.parse
            title = urllib.parse.quote(f"Add-on for {agent.name} ({agent.command})")
            body.append(Gtk.LinkButton(uri=f"https://github.com/uBruckhaus/asterism-addons/issues/new?title={title}",
                                       label=_("Ask for an add-on"), halign=Gtk.Align.START))

        self.health_security_groups(body, agent, checks, security)
        group = self.group_card(body, "configuration", _("Configuration"), "emblem-system-symbolic",
                                " · ".join(filter(None, [info.model, inventory.tilde(info.config_dir)
                                                         if info.config_dir else ""])))
        config = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP)
        config.add_css_class("am-boxed")
        self.fill_agent(config, agent, summary=True)
        group.append(config)

    def fill_usage_card(self, body, usage, info=None, title: str = "", summary_prefix: str = ""):
        """Usage and tokens, as far as they are known: tiles, tokens per day, limits and plan (info:
        agent_details.Details of an agent; None on a local LLM's page), tokens by model, and where
        the numbers come from. Remembers the card for add-on sections ("statistics")."""
        info = info or agent_details.Details()
        tiles = []
        if usage is not None and usage.has_tokens:
            tiles.append((_("Today"), _("{0} tokens").format(short_number(usage.today_tokens)),
                          ngettext("1 session", "{0} sessions", usage.sessions_today).format(usage.sessions_today)
                          if usage.sessions_today is not None else ""))
            tiles.append((_("Last 7 days"), _("{0} tokens").format(short_number(usage.week_tokens)),
                          ngettext("1 session", "{0} sessions", usage.sessions_week).format(usage.sessions_week)
                          if usage.sessions_week is not None else ""))
        elif usage is not None:
            tiles.append((_("Last 7 days"), ngettext("1 session", "{0} sessions", usage.sessions_week or 0)
                          .format(usage.sessions_week or 0), _("tokens not recorded")))
        if usage is not None and usage.sessions_total is not None:
            tiles.append((_("In total"), ngettext("1 session", "{0} sessions", usage.sessions_total)
                          .format(usage.sessions_total), _("{0} active days").format(info.active_days)
                          if info.active_days else ""))
        if info.plan:
            tiles.append((_("Plan"), info.plan, _("covered by the subscription · limits below") if info.limits
                          else _("covered by the subscription")))
        elif usage is not None and usage.cost and usage.api_value:
            # The agent's own cost of its sessions: the tokens at API prices (as on the Projects card).
            tiles.append((_("API value"), f"$ {usage.cost:.2f}", _("at API prices, not what a plan costs")))
        elif usage is not None and usage.cost:
            tiles.append((_("Cost"), f"$ {usage.cost:.2f}", _("as the agent estimates it")))
        chart = usage is not None and usage.has_tokens and any(t for _d, t in usage.days)
        if tiles or chart or info.limits or (usage is not None and usage.by_model):
            # Usage and tokens, as far as the agent records them.
            summary = " · ".join(filter(None, [
                _("{0} tokens in 7 days").format(short_number(usage.week_tokens))
                if usage is not None and usage.has_tokens else "",
                ngettext("1 session", "{0} sessions", usage.sessions_week or 0).format(usage.sessions_week or 0)
                if usage is not None and usage.sessions_week is not None else "",
                _("Plan: {0}").format(info.plan) if info.plan else ""]))
            group = self.group_card(body, "usage", title or _("Usage and tokens"), "am-chart-symbolic",
                                    " · ".join(filter(None, [summary_prefix, summary])))
            self.details_cards["usage"] = group
            if tiles:
                group.append(self.stat_tiles(tiles))
            if chart:
                self.settings_heading(group, _("Tokens per day"))
                group.append(TokenChart(self.side_map.color, usage.days))
            if info.limits:
                self.settings_heading(group, _("Limits: what is left"))
                for limit in info.limits:
                    group.append(self.limit_row(limit))
            if usage is not None and usage.by_model:
                self.settings_heading(group, _("Tokens by agent and model") if usage.source == "agents"
                                      else _("Tokens by model"))
                rows = sorted(usage.by_model.items(), key=lambda item: -sum(item[1]))[:8]
                self.detail_grid(group, [(model, _("{0} in · {1} out · {2} cache").format(
                    short_number(i), short_number(o), short_number(c)), "") for model, (i, o, c) in rows])
            if usage is not None and usage.source:
                source = _("Usage from Omarchy, updated {0}").format(when(info.usage_updated)) \
                    if usage.source == "Omarchy" and info.usage_updated else \
                    _("What your agents sent through it, from their own session records (numbers only; "
                      "matched by model name)") if usage.source == "agents" else \
                    _("Usage from {0}'s own session records (numbers and times only)").format(usage.source)
                self.note(group, source)

    HERDR_STATES = {"blocked": (N_("Waiting for you"), "dialog-question-symbolic"),
                    "working": (N_("Working"), "content-loading-symbolic"),
                    "idle": (N_("Idle"), "media-playback-pause-symbolic"),
                    "done": (N_("Done"), "object-select-symbolic"),
                    "unknown": (N_("Running"), "media-playback-start-symbolic")}
    HERDR_EVERY = 4                # seconds between reads while an agent's page shows

    def fill_herdr_panes(self, agent, panes):
        """The agent's panes in herdr now: state, folder and workspace (nothing of the screen)."""
        box = self.herdr_box
        while (child := box.get_first_child()) is not None:
            box.remove(child)
        mine = herdr_state.for_agent(panes, agent.command)
        if not mine:
            return
        self.settings_heading(box, _("In herdr now"))
        listbox = self.boxed_list()
        for pane in mine[:12]:
            label, icon = self.HERDR_STATES[pane.state]
            where = inventory.tilde(pane.cwd) if pane.cwd else _("Unknown folder")
            space = " › ".join(x for x in (pane.session, pane.workspace) if x)
            row = self.plain_row(_(label), _("{0} · workspace {1}").format(where, space) if space else where)
            row.add_prefix(Gtk.Image.new_from_icon_name(icon))
            if pane.state == "blocked":
                row.add_css_class("warning")
            if pane.pane_id:
                row.set_activatable(True)
                row.set_tooltip_text(_("Show this pane in herdr"))
                row.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
                row.connect("activated", lambda _r, p=pane: self.focus_herdr_pane(p))
            listbox.append(row)
        box.append(listbox)
        if len(mine) > 12:
            self.note(box, ngettext("1 more pane", "{0} more panes", len(mine) - 12).format(len(mine) - 12))

    def focus_herdr_pane(self, pane):
        """herdr switches to the pane; its terminal window comes to the front (where the desktop can)."""
        def work():
            try:
                switched = herdr_state.focus(pane)
                shown = switched and desktop.focus_process(
                    herdr_state.client_pids(pane.session, inventory.own_processes()))
            except Exception:
                switched = shown = False
            GLib.idle_add(done, switched, shown)

        def done(switched, shown):
            if not switched:
                self.toast(_("herdr could not switch to this pane"), 4)
            elif not shown:
                self.toast(_("Switched in herdr; open herdr in a terminal to see it"), 5)
            return False

        threading.Thread(target=work, daemon=True).start()

    def watch_herdr(self, agent):
        """While this agent's page shows: herdr's state again every HERDR_EVERY seconds (on a thread)."""
        showing, box, busy = ("agent", agent.command), self.herdr_box, {"on": False}

        def current() -> bool:
            return self.status_showing == showing and getattr(self, "herdr_box", None) is box

        def tick():
            if not current():
                return False
            if not busy["on"]:
                busy["on"] = True
                threading.Thread(target=work, daemon=True).start()
            return True

        def work():
            try:
                panes = herdr_state.snapshot()
            except Exception:
                panes = None
            GLib.idle_add(done, panes)

        def done(panes):
            busy["on"] = False
            if current():
                self.fill_herdr_panes(agent, panes or [])
            return False

        GLib.timeout_add_seconds(self.HERDR_EVERY, tick)

    WEEKDAYS = (N_("Monday"), N_("Tuesday"), N_("Wednesday"), N_("Thursday"), N_("Friday"), N_("Saturday"),
                N_("Sunday"))

    @staticmethod
    def duration_text(seconds: float) -> str:
        minutes = int(seconds // 60)
        return _("{0} h {1} min").format(minutes // 60, minutes % 60) if minutes >= 60 else \
            _("{0} min").format(minutes)

    def fill_session_projects(self, body, sessions, harness):
        """A harness's last 30 days per project folder: working time, sessions, tokens, lines
        changed, API value (numbers from its own session store; no text)."""
        since = time.time() - 30 * 86400
        rows = claude_sessions.projects(list(sessions.values()), since)
        if not rows:
            return
        summary = " · ".join([ngettext("1 project", "{0} projects", len(rows)).format(len(rows)),
                              _("{0} tokens").format(short_number(sum(r.tokens for r in rows))),
                              _("{0} working").format(self.duration_text(sum(r.active for r in rows)))])
        group = self.group_card(body, "projects", _("Projects, last 30 days"), "folder-symbolic", summary)
        listbox = self.boxed_list()
        for project in rows[:10]:
            parts = [_("{0} working").format(self.duration_text(project.active)),
                     ngettext("1 session", "{0} sessions", project.sessions).format(project.sessions),
                     _("{0} tokens").format(short_number(project.tokens))]
            if project.subagent_runs:
                parts.append(ngettext("1 subagent run", "{0} subagent runs", project.subagent_runs)
                             .format(project.subagent_runs))
            if project.lines_added or project.lines_removed:
                parts.append(_("+{0} −{1} lines").format(project.lines_added, project.lines_removed))
            if project.cost:
                parts.append(_("API value {0}").format(f"$ {project.cost:.2f}"))
            row = self.plain_row(inventory.tilde(project.cwd) if project.cwd != "?" else _("Unknown folder"),
                                 " · ".join(parts))
            row.set_title_selectable(True)
            row.add_prefix(Gtk.Image.new_from_icon_name("folder-symbolic"))
            listbox.append(row)
        group.append(listbox)
        runs = claude_sessions.subagents(list(sessions.values()), since)
        if runs:
            self.settings_heading(group, _("Subagents, last 30 days"))
            listbox = self.boxed_list()
            for kind, count, tokens, active in runs[:10]:
                row = self.plain_row(kind if kind != "?" else _("Unknown type"), " · ".join((
                    ngettext("1 run", "{0} runs", count).format(count),
                    _("{0} tokens").format(short_number(tokens)),
                    _("{0} working").format(self.duration_text(active)))))
                row.add_prefix(Gtk.Image.new_from_icon_name("system-run-symbolic"))
                listbox.append(row)
            group.append(listbox)
        peak = claude_sessions.busiest(list(sessions.values()), since)
        if peak is not None:
            self.note(group, _("Most active: {0}, around {1}:00").format(_(self.WEEKDAYS[peak[0]]), peak[1]))
        self.note(group, harness.sessions.note())

    def fill_addon_cards(self, body, harness, sessions, since, topics: bool = True):
        """Add-ons' details sections, each in the card it names: "statistics" joins Usage and tokens,
        "health" joins Health, "topics" fills Topics, any other key is a card of its own that the
        add-on names with its first heading."""
        hooks = [(i, b, p, c) for i, b, p, c, page in self.addon_hooks("details")
                 if page == "agent" and edition.allows(p)]
        by_card = {}
        for hook in hooks:
            by_card.setdefault(hook[3], []).append(hook)
        run = lambda box, found: [addon_api.call(i, b, addon_api.UI(self, i), box, harness, sessions, since)  # noqa: E731
                                  for i, b, _p, _c in found]
        statistics = by_card.pop("statistics", [])
        if statistics:
            box = self.details_cards.get("usage")
            if box is None:            # no numbers of its own: the card comes with the add-on's section
                box = self.group_card(body, "usage", _("Usage and tokens"), "am-chart-symbolic")
                card = box.get_ancestor(Gtk.Box)
                while card is not None and card.get_parent() is not body:
                    card = card.get_parent()
                if card is not None:
                    body.reorder_child_after(card, self.details_cards["hero"])
            run(box, statistics)
        self.pending_health = [(h, (harness, sessions, since)) for h in by_card.pop("health", [])]
        topic_hooks = by_card.pop("topics", [])
        if topics or topic_hooks:
            self.fill_topics(body, harness, sessions, since, topic_hooks)
        for key, found in by_card.items():
            level = max(p for _i, _b, p, _c in found)
            box = self.group_card(body, f"addon-{key}", key.replace("-", " "), "application-x-addon-symbolic",
                                  "", [(_("Privacy") + ": " + _(self.PRIVACY_NAMES.get(level, "numbers")),
                                        "medium" if level >= 2 else "low")], named_by_addon=True)
            run(box, found)

    PRIVACY_NAMES = {1: N_("numbers"), 2: N_("titles"), 3: N_("summary")}

    def fill_topics(self, body, harness, sessions, since, hooks=()):
        """Topics: what the sessions were about. Its own card, marked Privacy, because it goes beyond
        numbers; an add-on (Insights) fills it and names it with its first heading. Without one, the
        card says what would show here."""
        level = max((p for _i, _b, p, _c in hooks), default=0)
        badges = [(_("Privacy") + (": " + _(self.PRIVACY_NAMES[level]) if level in self.PRIVACY_NAMES else ""),
                   "medium" if level >= 2 else "low")]
        group = self.group_card(body, "topics", _("Topics"), "dialog-information-symbolic",
                                _("Session titles and a summary, from an add-on") if hooks else
                                _("Not filled yet: needs an add-on"), badges, named_by_addon=bool(hooks))
        if not hooks:
            self.note(group, _("What your sessions were about — their titles per project, and a summary on request — "
                               "comes from an add-on (Insights). It reads only the titles the agent gave its sessions, "
                               "never the conversations."))
            if hasattr(self, "open_addons"):
                button = Gtk.Button(label=_("Get add-ons"), halign=Gtk.Align.START)
                button.add_css_class("am-small-btn")
                button.connect("clicked", lambda *_a: self.open_addons())
                group.append(button)
            return
        for addon_id, builder, _privacy, _card in hooks:
            addon_api.call(addon_id, builder, addon_api.UI(self, addon_id), group, harness, sessions, since)

    def open_runtime_status(self, key: str):
        rt = next((r for r in self.inventory.runtimes if r.key == key), None)
        if rt is None:
            return
        self.clear_status(rt.name)
        self.status_showing = ("runtime", key)
        provider = providers.get(key)
        hooks = [(i, b, p, c) for i, b, p, c, page in self.addon_hooks("details")
                 if page == "llm" and edition.allows(p)]
        self.pending_health = [((i, b, p, c), (provider, rt)) for i, b, p, c in hooks if c == "health"]
        body = self.status_body
        names = self.agent_names()
        try:
            login = runtime_ctl.start_at_login(rt) if rt.installed else None
        except Exception:
            login = None
        checks = agent_status.runtime_checks(rt, link_hints.load(), names, login)
        state, sentence = agent_status.summary_line(checks)
        use = self.side_map.runtime_use(graph.Node(f"runtime:{key}", 2, rt.name, kind="runtime"))
        if rt.running:
            now = _("Running") + (" · " + _("{0} GB of GPU memory").format(f"{use.vram / 1024 ** 3:.1f}")
                                   if use is not None and use.vram >= 64 * 1024 ** 2 else "")
            loaded = [m for m, state_ in (use.models.items() if use is not None else []) if state_ == "loaded"]
            if loaded:
                now += " · " + _("loaded: {0}").format(", ".join(loaded))
            if self.runtime_users(key):
                now += "\n" + _("In use by {0}").format(self.runtime_users(key))
        else:
            now = _("Not running") if rt.installed else _("Not installed")
        body.append(self.status_hero(state, sentence, now))
        tiles = [(_("Models"), str(len(rt.models)), rt.models_dir),
                 (_("Agents"), str(len(rt.agents)), ", ".join(names.get(a, a) for a in rt.agents))]
        if rt.address:
            tiles.append((_("Address"), rt.address, rt.service))
        body.append(self.stat_tiles(tiles))
        self.details_cards = {"hero": body.get_last_child()}
        # Usage and tokens: its own reader (a provider fact), else what the agents sent through it
        # (their sessions), made on a thread; the card goes here when it is ready.
        slot = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        body.append(slot)
        statistics = [h for h in hooks if h[3] == "statistics"]
        self.fill_runtime_usage(slot, rt, provider, statistics)
        self.health_security_groups(body, None, checks, agent_security.runtime_report(rt))
        group = self.group_card(body, "configuration", _("Configuration"), "emblem-system-symbolic",
                                " · ".join(filter(None, [rt.address, inventory.tilde(rt.models_dir)])))
        config = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP)
        config.add_css_class("am-boxed")
        self.fill_runtime(config, rt, names, summary=True)
        group.append(config)
        for card in dict.fromkeys(h[3] for h in hooks if h[3] not in ("statistics", "health")):
            found = [h for h in hooks if h[3] == card]
            level = max(h[2] for h in found)
            box = self.group_card(body, f"addon-{card}", card.replace("-", " "), "application-x-addon-symbolic", "",
                                  [(_("Privacy") + ": " + _(self.PRIVACY_NAMES.get(level, "numbers")),
                                    "medium" if level >= 2 else "low")], named_by_addon=True)
            for addon_id, builder, _p, _c in found:
                addon_api.call(addon_id, builder, addon_api.UI(self, addon_id), box, provider, rt)
        self.show_page("status")

    def fill_runtime_usage(self, slot, rt, provider, statistics):
        """A local LLM's Usage and tokens, on a thread: the provider's own usage(runtime) fact, else
        derived from the agents' sessions (agent_status.runtime_usage); add-ons' "statistics"
        sections join the card."""
        showing = self.status_showing
        names = self.agent_names()

        def work():
            usage = None
            if provider is not None and provider.usage is not None:
                try:
                    usage = provider.usage(rt)
                except Exception:
                    usage = None
            if usage is None:
                found = {}
                for h in harnesses.with_sessions():
                    try:
                        found[names.get(h.command, h.name)] = list(h.sessions.collect().values())
                    except Exception:
                        continue
                models = list(rt.models) + list(getattr(rt, "identifiers", []) or [])
                usage = agent_status.runtime_usage(models, found, provider.connect_key if provider else "")
            GLib.idle_add(done, usage)

        def done(usage):
            if self.status_showing != showing or slot.get_parent() is None:
                return False
            self.fill_usage_card(slot, usage, None, summary_prefix=_("through {0}").format(rt.name))
            if statistics:
                box = self.details_cards.get("usage") or self.group_card(
                    slot, "usage", _("Usage and tokens"), "am-chart-symbolic")
                for addon_id, builder, _p, _c in statistics:
                    addon_api.call(addon_id, builder, addon_api.UI(self, addon_id), box, provider, rt)
            return False

        threading.Thread(target=work, daemon=True).start()

    def check_list(self, checks) -> Gtk.Widget:
        """One row per check: a mark, what it is, and under it what to do when it is not done;
        a command to run gets a row of its own with a copy button."""
        listbox = self.boxed_list()
        for check in checks:
            lines = [check.detail] if check.detail else []
            if check.step and check.state != "ok":
                lines.append("→ " + check.step)
            row = self.plain_row(check.title, "\n".join(lines))
            row.set_subtitle_selectable(True)
            mark = Gtk.Label(label={"ok": "✓", "warn": "!", "missing": "✕", "info": "·"}[check.state])
            mark.add_css_class(f"am-state-{check.state}")
            mark.add_css_class("am-check-mark")
            row.add_prefix(mark)
            listbox.append(row)
            if check.command and check.state != "ok":
                listbox.append(self.command_row(check.command))
        return listbox

    # --- Healthy & Security check (agent and local LLM pages) ------------------------------------

    RISK_MARKS = {"high": ("!", "am-state-missing"), "medium": ("!", "am-state-warn"), "low": ("·", "am-state-info"),
                  "ok": ("✓", "am-state-ok")}
    RISK_NAMES = {"high": N_("High risk"), "medium": N_("Medium risk"), "low": N_("Low risk")}
    RISK_SENTENCES = {
        "high": N_("It can do serious things on this PC without asking you. What lowers the risk is listed below."),
        "medium": N_("Some things happen without asking you. What lowers the risk is listed below."),
        "low": N_("It asks before it does anything that matters, or cannot do it at all."),
    }
    HEALTH_NAMES = {"ok": N_("Healthy"), "warn": N_("Needs a look"), "missing": N_("Not set up")}

    def risk_mark(self, level: str) -> Gtk.Label:
        text, css = self.RISK_MARKS.get(level, ("·", "am-state-info"))
        mark = Gtk.Label(label=text)
        mark.add_css_class(css)
        mark.add_css_class("am-check-mark")
        return mark

    @staticmethod
    def risk_badge(text: str, level: str) -> Gtk.Label:
        badge = Gtk.Label(label=text, valign=Gtk.Align.CENTER)
        badge.add_css_class("am-risk-badge")
        badge.add_css_class(f"am-risk-{level}")
        return badge

    def health_security_groups(self, body, agent, checks, security):
        """The Healthy & Security check as group cards: Health (is it set up and working), Security
        settings (what it may do without asking, each choice explained, changeable with a preview),
        Risk management (what the settings mean together and what lowers each risk) and the words
        explained."""
        state, sentence = agent_status.summary_line(checks)
        group = self.group_card(body, "health", _("Health"), "object-select-symbolic", sentence,
                                [(_(self.HEALTH_NAMES.get(state, "Needs a look")),
                                  {"ok": "ok", "warn": "medium", "missing": "high"}.get(state, "medium"))])
        group.append(self.check_list(checks))
        for (addon_id, builder, _p, _c), args in getattr(self, "pending_health", []):
            addon_api.call(addon_id, builder, addon_api.UI(self, addon_id), group, *args)
        self.pending_health = []
        if security is None:
            return
        level = security.level() if security.settings or security.risks else ""
        risk_badge = [(_(self.RISK_NAMES[level]), level)] if level else []
        if security.settings or security.rules or security.note or security.files:
            group = self.group_card(body, "security", _("Security settings"), "security-high-symbolic",
                                    _("What it may do on this PC without asking you") + " · " +
                                    ngettext("1 setting", "{0} settings", len(security.settings))
                                    .format(len(security.settings)), risk_badge)
            if security.settings:
                listbox = self.boxed_list()
                for setting in security.settings:
                    listbox.append(self.security_setting_row(agent, setting))
                group.append(listbox)
                if agent is not None:
                    self.note(group, _("Open a setting to see every choice. A change is shown first, backed up and "
                                       "can be undone; it applies the next time {0} starts.").format(agent.name))
            for title, _key, entries in security.rules:
                listbox = self.boxed_list()
                rules = Adw.ExpanderRow(title=title, subtitle=ngettext("1 rule", "{0} rules", len(entries))
                                        .format(len(entries)), use_markup=False)
                rules.add_prefix(Gtk.Image.new_from_icon_name("view-list-symbolic"))
                for entry in entries[:60]:
                    row = self.plain_row(entry)
                    row.add_css_class("monospace")
                    row.set_title_selectable(True)
                    rules.add_row(row)
                listbox.append(rules)
                group.append(listbox)
            if security.note:
                self.note(group, security.note)
            if security.files:
                self.note(group, ngettext("Settings file: {0}", "Settings files: {0}", len(security.files)).format(
                    ", ".join(inventory.tilde(f) for f in security.files)))

        if security.risks:
            open_risks = sum(1 for r in security.risks if r.level != "ok")
            group = self.group_card(body, "risks", _("Risk management"), "dialog-warning-symbolic",
                                    _(self.RISK_SENTENCES[level]) if level else
                                    ngettext("1 risk", "{0} risks", open_risks).format(open_risks), risk_badge)
            listbox = self.boxed_list()
            for risk in security.risks:
                lines = [risk.why] + (["→ " + risk.lower] if risk.lower and risk.level != "ok" else [])
                row = self.plain_row(risk.title, "\n".join(lines))
                row.add_prefix(self.risk_mark(risk.level))
                setting = next((s for s in security.settings if s.key == risk.fix_key), None)
                fix = next((c for c in setting.choices if agent_security._same(c.value, risk.fix_value)), None) \
                    if setting is not None else None
                if agent is not None and fix is not None and setting.editable \
                        and not agent_security._same(setting.value(), fix.value):
                    button = Gtk.Button(label=_("Lower it…"), valign=Gtk.Align.CENTER)
                    button.add_css_class("am-small-btn")
                    button.set_tooltip_text(_("{0}: {1}").format(setting.title, fix.label))
                    button.connect("clicked", lambda _b, s=setting, c=fix: self.open_security_change(agent, s, c))
                    row.add_suffix(button)
                listbox.append(row)
            group.append(listbox)

        words = agent_security.terms()
        terms = [t for t in security.terms() if t in words]
        if terms:
            group = self.group_card(body, "words", _("What the words mean"), "help-about-symbolic",
                                    ", ".join(words[t][0] for t in terms[:5]) + ("…" if len(terms) > 5 else ""))
            listbox = self.boxed_list()
            for term in terms:
                title, text = words[term]
                row = Adw.ExpanderRow(title=title, use_markup=False)
                row.add_prefix(Gtk.Image.new_from_icon_name("help-about-symbolic"))
                row.add_row(self.plain_row("", text))
                listbox.append(row)
            group.append(listbox)

    def security_setting_row(self, agent, setting) -> Gtk.Widget:
        """One setting: its value now and what it means; opened, every choice with its risk and Use."""
        chosen = setting.choice()
        row = Adw.ExpanderRow(title=setting.title, subtitle=f"{chosen.label} — {chosen.explain}", use_markup=False)
        row.set_subtitle_lines(0)
        row.add_prefix(self.risk_mark(agent_security.LEVELS[chosen.risk] if chosen.risk else "ok"))
        about = setting.explain
        if setting.current is None:
            about += " " + _("Not set: the agent's own default applies.")
        explain = self.plain_row("", about)
        row.add_row(explain)
        for choice in setting.choices:
            if choice.value is None and setting.current is None and choice is not chosen:
                continue
            title = choice.label
            if agent_security._same(choice.value, setting.recommended):
                title += " · " + _("recommended")
            line = self.plain_row(title, choice.explain)
            line.add_prefix(self.risk_mark(agent_security.LEVELS[choice.risk] if choice.risk else "ok"))
            if choice.risk:
                line.add_suffix(self.risk_badge(_(self.RISK_NAMES[agent_security.LEVELS[choice.risk]]),
                                                agent_security.LEVELS[choice.risk]))
            if choice is chosen:
                mark = Gtk.Label(label=_("In use"), valign=Gtk.Align.CENTER)
                mark.add_css_class("am-agent-badge")
                line.add_suffix(mark)
            elif setting.editable and agent is not None:
                use = Gtk.Button(label=_("Use…"), valign=Gtk.Align.CENTER)
                use.add_css_class("am-small-btn")
                use.connect("clicked", lambda _b, c=choice: self.open_security_change(agent, setting, c))
                line.add_suffix(use)
            row.add_row(line)
        return row

    def open_security_change(self, agent, setting, choice):
        """The change of one security setting on the connect page: what it means, the diff, Change."""
        self.connecting = {"agent": agent, "security": setting.key, "value": choice.value, "title": setting.title,
                           "label": choice.label, "from": self.view}
        self.connect_title.set_label(_("{0}: {1}").format(agent.name, setting.title).upper())
        self.connect_reset()
        form = self.connect_form
        old = setting.choice()
        levels = agent_security.LEVELS
        self.connect_intro.set_label(_("{0} → {1}: {2}").format(old.label, choice.label, choice.explain) + " "
                                     + _("It applies the next time {0} starts; Undo puts it back.").format(agent.name))
        if old.risk != choice.risk:
            line = Gtk.Box(spacing=8)
            line.append(Gtk.Label(label=_("Risk"), xalign=0))
            line.append(self.risk_badge(_(self.RISK_NAMES[levels[old.risk]]), levels[old.risk]))
            line.append(Gtk.Label(label="→"))
            line.append(self.risk_badge(_(self.RISK_NAMES[levels[choice.risk]]), levels[choice.risk]))
            form.append(line)
        self.connect_preview_heading.set_visible(True)
        self.connect_preview_scroller.set_visible(True)
        self.connect_status.set_label("")
        self.connect_remove.set_visible(False)
        self.connect_ok.set_visible(True)
        self.connect_ok.set_label(_("Change"))
        self.update_connect_preview()
        self.show_page("connect")
