"""Smart Check (a mixin of ai_popup.AgentsMap): what the installed online agent did and found for
this PC, in group cards: the setup summary (on a click: the agent's self-diagnosis of the AI setup,
with "Fix it with …" per finding), the updates it found, token statistics, and every kind of AI task
(this app's and the other plugins', from plugin_ai's shared log) with starts, results and tokens.
The header button shows while an agent that can do look-ups is installed (ai_helper.helpers)."""

from __future__ import annotations

import datetime as dt
import threading

from gi.repository import GLib, Gtk

import agent_details
import ai_core
import ai_helper
import health
import plugin_ai
import smart_check
from i18n import _, ngettext
from ui_helpers import short_number, TokenChart, when

LEVEL_MARK = {"problem": ("!", "warn"), "tip": ("·", "hint"), "good": ("✓", "ok")}


class SmartCheckPage:
    smart_analyzing = False

    def build_smart_check_page(self) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP)
        title = Gtk.Label(label=_("Smart Check").upper())
        page.append(self.page_header(title, self.close_smart_check, _("Back (Backspace, Esc)")))
        self.smart_body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP)
        self.smart_body.add_css_class("am-settings")
        page.append(self.page_scroller(self.smart_body))
        return page

    @staticmethod
    def own_risk_text() -> str:
        """The disclaimer on Smart Check and over Settings' AI features."""
        return _("You use the AI features at your own risk. An agent's findings, answers and changes can be wrong, "
                 "incomplete or out of date: read what it proposes before you agree to a step, and keep backups. "
                 "Its look-ups and tasks use your tokens or plan limits. AI Agents Map gives no warranty for what "
                 "an agent does.")

    def smart_check_available(self) -> bool:
        return bool(ai_helper.helpers(set(self.installed_commands())))

    def update_smart_check_button(self):
        button = getattr(self, "smartcheck_button", None)
        if button is not None:
            button.set_visible(self.smart_check_available())

    def open_smart_check(self):
        if self.page_name() != "smartcheck":
            self.smart_return = self.view if self.page_name() in ai_core.VIEWS else "main"
        self.fill_smart_check()
        self.show_page("smartcheck")

    def show_smart_check_again(self):
        """Back on Smart Check after one of its actions (an update, a task's terminal), up to date;
        its Back still leads where it led before."""
        self.fill_smart_check()
        self.show_page("smartcheck")

    def close_smart_check(self):
        self.show_view(getattr(self, "smart_return", self.view), save=False)

    # --- the page ------------------------------------------------------------------------------

    def fill_smart_check(self):
        body = self.smart_body
        while (child := body.get_first_child()) is not None:
            body.remove(child)
        agent = ai_helper.chosen(set(self.installed_commands()))
        if agent is None:
            body.append(self.status_hero("missing", _("No agent for Smart Check"),
                                         _("Install Claude Code or Codex: Smart Check lets it check this PC's AI "
                                           "setup and look for updates.")))
            return
        analysis = smart_check.last()
        runs = ai_helper.runs()
        self.fill_smart_hero(body, agent, analysis)
        self.note_card(body, "warning", self.own_risk_text(), _("At your own risk"))
        self.fill_smart_analysis(body, agent, analysis, runs)
        self.fill_smart_updates(body, agent)
        self.fill_smart_tokens(body, runs)
        for category in smart_check.categories(runs):
            self.fill_smart_category(body, category)

    def fill_smart_hero(self, body, agent, analysis):
        name = ai_helper.name_of(agent)
        if self.smart_analyzing:
            state, sentence = "", _("{0} is checking this PC's AI setup…").format(name)
        elif analysis is None:
            state, sentence = "", _("Not checked yet: Check now lets {0} look at this PC's AI setup.").format(name)
        else:
            problems = analysis.count("problem")
            state = "warn" if problems else "ok"
            sentence = analysis.summary
        line = _("Smart Check uses {0} (Settings › Agent for AI checks); its look-ups use its tokens.").format(name)
        if analysis is not None and not self.smart_analyzing:
            line = _("Checked {0} by {1}").format(self.smart_when(analysis.when), ai_helper.name_of(analysis.agent)) \
                + " · " + line
        body.append(self.status_hero(state, sentence, line))

    @staticmethod
    def smart_when(text: str) -> str:
        try:
            return when(dt.datetime.fromisoformat(text))
        except ValueError:
            return text

    def fill_smart_analysis(self, body, agent, analysis, runs):
        summary = (_("{0} problems · {1} tips").format(analysis.count("problem"), analysis.count("tip"))
                   if analysis is not None else _("not checked yet"))
        group = self.group_card(body, "smart-analysis", _("PC setup summary"), "am-smartcheck-symbolic", summary,
                                default_open=True)
        helpers = ai_helper.helpers(set(self.installed_commands()))
        if len(helpers) > 1 and not self.smart_analyzing:
            group.append(self.ai_agent_picker(helpers, agent, self.fill_smart_check))
        row = Gtk.Box(spacing=8)
        button = Gtk.Button(label=_("Checking…") if self.smart_analyzing else _("Check now"))
        button.add_css_class("am-small-btn")
        button.set_sensitive(not self.smart_analyzing)
        button.connect("clicked", lambda *_: self.run_smart_analysis())
        row.append(button)
        previous = next((r for r in reversed(runs) if r.get("task") == "setup-analysis"
                         and r.get("plugin") == ai_helper.plugin_id()), None)
        cost = plugin_ai.tokens_of(previous) if previous else None
        hint = _("{0} reads a summary of the setup (names, versions, models; no keys or settings) and may look "
                 "things up on the web. A few minutes.").format(ai_helper.name_of(agent))
        if cost:
            hint += " " + _("Last time: {0} tokens.").format(short_number(cost))
        label = Gtk.Label(label=hint, xalign=0, wrap=True, max_width_chars=1, hexpand=True)
        label.add_css_class("am-agent-detail")
        row.append(label)
        group.append(row)
        if analysis is None:
            return
        task_agent = self.task_agent()
        listbox = self.boxed_list()
        for finding in analysis.findings:
            mark, state = LEVEL_MARK[finding.level]
            item = self.plain_row(finding.title, finding.detail)
            sign = Gtk.Label(label=mark)
            sign.add_css_class("am-status-mark")
            sign.add_css_class(f"am-state-{state}")
            item.add_prefix(sign)
            if task_agent and finding.level != "good":
                fix = Gtk.Button(label=_("Fix it with {0}…").format(ai_helper.name_of(task_agent)),
                                 valign=Gtk.Align.CENTER)
                fix.add_css_class("am-small-btn")
                fix.connect("clicked", lambda *_a, f=finding: self.do_with_agent(
                    "smart-fix:" + f.title[:40], f.title, smart_check.fix_prompt(f)))
                item.add_suffix(fix)
            listbox.append(item)
        group.append(listbox)

    def fill_smart_updates(self, body, agent):
        found = self.open_ai_mcp_updates()
        # The map's own finds too (as on Setup): a newer version on this PC that an agent still does
        # not start, e.g. one an agent's settings missed when it was moved by hand.
        local = [c for c in self.mcp_updates() if c.name not in {u.name for u in found}]
        count = len(found) + len(local)
        on = bool(ai_core.setting("ai_checks"))
        summary = (ngettext("1 update", "{0} updates", count).format(count) if count
                   else _("nothing new") if on else _("daily checks off"))
        group = self.group_card(body, "smart-updates", _("Updates found"), "software-update-available-symbolic",
                                summary, default_open=bool(count))
        listbox = self.boxed_list()
        names = self.agent_names()
        for candidate in local:
            users = [names.get(a, a) for a in candidate.users if a != "list"]
            item = self.plain_row(_("MCP server {0} {1} is on this PC, the agents still start {2}").format(
                candidate.name, candidate.version, candidate.update_from),
                _("Still starts {0}: {1}").format(candidate.update_from, ", ".join(users)) if users else "")
            button = Gtk.Button(label=_("Use it…"), valign=Gtk.Align.CENTER)
            button.add_css_class("am-small-btn")
            button.connect("clicked", lambda *_a, c=candidate: self.open_mcp_updates([c]))
            item.add_suffix(button)
            listbox.append(item)
        for update in found:
            item = self.plain_row(self.mcp_update_text(update), update.notes)
            for label, action in self.mcp_update_buttons(update):
                button = Gtk.Button(label=label, valign=Gtk.Align.CENTER)
                button.add_css_class("am-small-btn")
                button.connect("clicked", lambda *_a, act=action: act())
                item.add_suffix(button)
            listbox.append(item)
        if count:
            group.append(listbox)
        row = Gtk.Box(spacing=8)
        check = Gtk.Button(label=_("Check now"))
        check.add_css_class("am-small-btn")
        check.set_sensitive(not self.ai_checking_mcp)
        check.connect("clicked", lambda *_: self.ai_check_mcp_updates(force=True))
        row.append(check)
        note = Gtk.Label(label=_("Once a day while Settings › Daily AI checks is on; Check now asks {0} at once.")
                         .format(ai_helper.name_of(agent)), xalign=0, wrap=True, max_width_chars=1, hexpand=True)
        note.add_css_class("am-agent-detail")
        row.append(note)
        group.append(row)

    def fill_smart_tokens(self, body, runs):
        totals = plugin_ai.totals(runs)
        if not totals.runs:
            return
        group = self.group_card(body, "smart-tokens", _("Token statistics"), "am-chart-symbolic",
                                _("{0} tokens in 30 days").format(short_number(totals.tokens)))
        tiles = [(_("Today"), _("{0} tokens").format(short_number(totals.tokens_today)), ""),
                 (_("Last 30 days"), _("{0} tokens").format(short_number(totals.tokens)),
                  ngettext("1 run", "{0} runs", totals.runs).format(totals.runs))]
        covers = {agent: ai_helper.billing(agent)[0] for agent in totals.by_agent}
        if totals.cost:
            kinds = set(covers.values())
            tiles.append((_("API value"), f"$ {totals.cost:.2f}",
                          _("covered by your subscription") if kinds == {"plan"} else
                          _("charged to your API key") if kinds == {"key"} else
                          _("at API prices, not what a plan costs")))
        group.append(self.stat_tiles(tiles))
        days = plugin_ai.per_day(runs, days=14)
        if any(t for _d, t in days):
            self.settings_heading(group, _("Tokens per day"))
            group.append(TokenChart(self.side_map.color, days))
        if totals.by_agent:
            self.settings_heading(group, _("By agent"))
            rows = []
            for agent, tokens in sorted(totals.by_agent.items(), key=lambda kv: -kv[1]):
                cost = plugin_ai.totals(runs, agent=agent).cost
                rows.append((ai_helper.name_of(agent), " · ".join(filter(None, [
                    _("{0} tokens").format(short_number(tokens)), f"$ {cost:.2f}" if cost else "",
                    self.cover_text(agent)])), self.cost_line(agent, cost) if cost else ""))
            self.detail_grid(group, rows)
        if totals.by_plugin:
            self.settings_heading(group, _("By plugin"))
            self.detail_grid(group, [(self.plugin_title(key), _("{0} tokens").format(short_number(value)), "")
                                     for key, value in sorted(totals.by_plugin.items(), key=lambda kv: -kv[1])])
        for agent in sorted(a for a in totals.by_agent if plugin_ai.totals(runs, agent=a).cost):
            self.note(group, f"{ai_helper.name_of(agent)}: " + self.cost_line(agent, plugin_ai.totals(runs, agent=agent).cost))
        if totals.unknown:
            self.note(group, ngettext("1 run without token numbers (a task in a terminal tab counts in its own "
                                      "session; the agent's detail page shows those).",
                                      "{0} runs without token numbers (tasks in a terminal tab count in their own "
                                      "sessions; the agent's detail page shows those).",
                                      totals.unknown).format(totals.unknown))

    def cover_text(self, command: str) -> str:
        """Short: who pays for this agent's runs."""
        kind, plan = ai_helper.billing(command)
        return (_("covered by your {0} plan").format(plan) if kind == "plan" else
                _("charged to your API key") if kind == "key" else "")

    def cost_line(self, command: str, cost: float) -> str:
        """The API-price value of an agent's runs, and whether a subscription covers it."""
        kind, plan = ai_helper.billing(command)
        if kind == "plan":
            return _("$ {0:.2f} at API prices: covered by your {1} plan (it counts against the plan's limits, "
                     "no extra charge).").format(cost, plan)
        if kind == "key":
            return _("$ {0:.2f} at API prices: charged to your API key.").format(cost)
        return _("$ {0:.2f} at API prices: charged only when it runs on an API key, else covered by your "
                 "plan.").format(cost)

    def plugin_title(self, plugin: str) -> str:
        if plugin == ai_helper.plugin_id():
            return _("this app")
        known = {"ai-agents-map": "AI Agents Map", "asterism": "Asterism"}      # the same app's two editions
        return known.get(plugin) or plugin.replace("-", " ").title()

    def fill_smart_category(self, body, category):
        task = smart_check.TASKS.get(category.task)
        if task and category.plugin == ai_helper.plugin_id():
            title = _(task)
        elif task and category.plugin in ("ai-agents-map", "asterism"):     # the same app's other edition
            title = f"{self.plugin_title(category.plugin)} · {_(task)}"
        else:
            title = f"{self.plugin_title(category.plugin)} · {category.task}"
        totals = category.totals
        failed = totals.runs - totals.ok
        summary = " · ".join(filter(None, [
            ngettext("1 run", "{0} runs", totals.runs).format(totals.runs),
            _("{0} tokens").format(short_number(totals.tokens)) if totals.tokens else "",
            ngettext("1 failed", "{0} failed", failed).format(failed) if failed else "",
            _("last {0}").format(self.smart_when(str(category.runs[0].get("when"))))]))
        group = self.group_card(body, f"smart-task:{category.plugin}:{category.task}", title,
                                "emblem-system-symbolic", summary)
        last = category.runs[0]
        tiles = [(_("Runs"), str(totals.runs), ngettext("1 failed", "{0} failed", failed).format(failed)
                  if failed else _("all worked")),
                 (_("Tokens"), _("{0} tokens").format(short_number(totals.tokens)) if totals.tokens
                  else _("tokens not recorded"),
                  " · ".join(filter(None, [f"$ {totals.cost:.2f}" if totals.cost else "",
                                           self.cover_text(str(last.get("agent", ""))) if totals.cost else ""]))),
                 (_("Last run"), self.smart_when(str(last.get("when"))),
                  " · ".join(filter(None, [ai_helper.name_of(str(last.get("agent", ""))), str(last.get("result", ""))])))]
        group.append(self.stat_tiles(tiles))
        self.settings_heading(group, _("Recent runs"))
        rows = []
        for run in category.runs[:8]:
            tokens = plugin_ai.tokens_of(run)
            rows.append((self.smart_when(str(run.get("when"))),
                         " · ".join(filter(None, [ai_helper.name_of(str(run.get("agent", ""))),
                                                  str(run.get("result", "")) or (_("worked") if run.get("ok")
                                                                                 else _("failed")),
                                                  _("{0} tokens").format(short_number(tokens)) if tokens else ""])),
                         ""))
        self.detail_grid(group, rows)

    # --- the analysis --------------------------------------------------------------------------

    def run_smart_analysis(self):
        """Check now: the chosen agent looks at a summary of the setup (in the background, minutes)."""
        agent = ai_helper.chosen(set(self.installed_commands()))
        if agent is None or self.smart_analyzing:
            return
        self.smart_analyzing = True
        self.fill_smart_check()
        agent_list, inv, report = list(self.agent_list), self.inventory, self.health_report
        updates = self.open_ai_mcp_updates() or None          # else the last answer kept
        waiting = list(self.mcp_updates())

        def work():
            try:
                details = {a.command: agent_details.details_for(a.command) for a in agent_list if a.installed}
                pc = health.pc_info([rt.models_dir for rt in inv.runtimes if rt.installed and rt.models_dir])
                setup = smart_check.snapshot(agent_list, details, inv, report, pc, updates, waiting)
                result = smart_check.analyze(agent, setup)
            except Exception as error:          # the agent, its sign-in, the web, an answer that did not hold up
                result = error
            GLib.idle_add(done, result)

        def done(result):
            self.smart_analyzing = False
            if isinstance(result, Exception):
                self.toast(_("Smart Check did not work: {0}").format(str(result)[:120]), 8)
            if self.page_name() == "smartcheck":
                self.fill_smart_check()
            return False

        threading.Thread(target=work, daemon=True).start()
