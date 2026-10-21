"""AI Agents Map: an agent's network traffic (a mixin of ai_popup.AgentsMap, split off to keep
ai_popup.py small; its methods use the window's state through self).

The sampler (traffic.py) runs every few seconds while an agent runs; the map shows a ⇅ in a
running agent's box, lit while it moves data, and a click on it opens this page: now (rates,
totals, connections), the last minutes as a chart, where it goes, and which of its programs
moves what. Built from the layout kit (page_parts, docs/layout.md)."""

import threading
import time

from gi.repository import GLib, Gtk

import ai_core
import gpu_usage
import inventory
import provider_watch
import traffic
from i18n import _, ngettext

ACTIVE = 1024               # bytes per second from which the map's ⇅ lights up
KINDS = {"api": "Online API", "local-llm": "Local LLM", "local": "This PC", "lan": "Local network",
         "web": "Internet"}


class TrafficPage:
    def build_traffic_page(self) -> Gtk.Widget:
        self.traffic = traffic.Meter()
        self.traffic_lock = threading.Lock()
        self.traffic_busy = False
        self.traffic_agent = None
        self.traffic_running = set()
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP)
        self.traffic_title = Gtk.Label()
        page.append(self.page_header(self.traffic_title, self.close_traffic, _("Back (Backspace, Esc)")))
        column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP_TIGHT * 2)
        column.add_css_class("am-settings")
        self.traffic_now = self.info_card(_("Now"))
        column.append(self.traffic_now)
        self.traffic_chart_card = self.info_card(_("The last 5 minutes"))
        self.traffic_chart = Gtk.DrawingArea(content_height=150, hexpand=True)
        self.traffic_chart.set_draw_func(self.draw_traffic_chart)
        self.traffic_chart_card.body.append(self.traffic_chart)
        self.traffic_legend = Gtk.Label(xalign=0)
        self.traffic_legend.add_css_class("am-agent-detail")
        self.traffic_chart_card.body.append(self.traffic_legend)
        column.append(self.traffic_chart_card)
        self.traffic_places = self.info_card(_("Where it goes"))
        column.append(self.traffic_places)
        self.traffic_programs = self.info_card(_("Programs"))
        column.append(self.traffic_programs)
        self.traffic_about = self.info_card(_("How it is counted"), "info")
        column.append(self.traffic_about)
        page.append(self.scrolled(column))
        GLib.timeout_add_seconds(traffic.SAMPLE_EVERY, self.sample_traffic)
        return page

    # --- sampling ----------------------------------------------------------------------------------

    def sample_traffic(self) -> bool:
        """Every few seconds: the running agents' connections, on a thread (never blocks the window)."""
        if self.traffic_busy or not getattr(self, "agent_list", None):
            return True
        if not self.running and self.page_name() != "traffic":
            if self.traffic_running:
                self.traffic_running = set()
                self.side_map.set_traffic({})
            return True
        self.traffic_busy = True
        names = [a.command for a in self.agent_list if a.installed]
        runtimes = list(self.inventory.runtimes)

        def work():
            running = set()
            try:
                pids = {}
                provider_watch.agent_instances(names, pids=pids)
                running = {name for name, found in pids.items() if found}
                owners = traffic.agent_processes(pids, inventory.own_processes(), inventory.parent_pid)
                self.traffic.names.refresh_known()
                llms = {gpu_usage.port_of(rt): rt.name for rt in runtimes if gpu_usage.port_of(rt)}
                socks = traffic.read_sockets() if owners else []
                with self.traffic_lock:
                    self.traffic.update(socks, owners, llms)
            except Exception:         # the meter must never take the window down
                pass
            GLib.idle_add(done, running)

        def done(running):
            self.traffic_busy = False
            self.traffic_running = running
            with self.traffic_lock:
                rates = {f"agent:{name}": self.traffic.stats[name].rate
                         for name in running if name in self.traffic.stats}
            self.side_map.set_traffic({key: (rate[0], rate[1], rate[0] + rate[1] >= ACTIVE)
                                       for key, rate in rates.items()})
            self.mark_traffic_button(rates)
            if self.page_name() == "traffic":
                self.refresh_traffic_page()
            return False

        threading.Thread(target=work, daemon=True).start()
        return True

    def mark_traffic_button(self, rates: dict):
        """The header's ⇅: lit while a running agent moves data; the total rates on hover."""
        button = getattr(self, "traffic_button", None)
        if button is None:
            return
        rate_in = sum(r[0] for r in rates.values())
        rate_out = sum(r[1] for r in rates.values())
        sample = "traffic:moving" if rate_in + rate_out >= ACTIVE else "traffic:quiet"
        if self.traffic_mark.sample != sample:
            self.traffic_mark.sample = sample
            self.traffic_mark.queue_draw()
        button.set_tooltip_text(_("Network: ↓ {0} ↑ {1} ({2})").format(
            traffic.rate_text(rate_in), traffic.rate_text(rate_out),
            ngettext("1 agent running", "{0} agents running", len(rates)).format(len(rates)))
            if rates else _("Network traffic of the running agents"))

    def traffic_popover(self) -> Gtk.Popover:
        """The header's ⇅ clicked: each running agent's traffic now and in all; a click on one opens
        its network page."""
        popover = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(10)
        title = Gtk.Label(label=_("Network").upper(), xalign=0)
        title.add_css_class("am-section-title")
        box.append(title)
        with self.traffic_lock:
            rows = [(agent, self.traffic.stats.get(agent.command)) for agent in self.agent_list
                    if agent.command in self.traffic_running or agent.command in self.traffic.stats]
        if not rows:
            text = Gtk.Label(label=_("No agent is running. Start one on the map: its traffic is counted while "
                                     "this app is open."), xalign=0, wrap=True, max_width_chars=40)
            text.add_css_class("am-agent-detail")
            box.append(text)
        grid = Gtk.Grid(column_spacing=14, row_spacing=4)
        for row, (agent, stats) in enumerate(rows):
            running = agent.command in self.traffic_running
            rate = stats.rate if stats and running else (0.0, 0.0)
            name = Gtk.Button(label=agent.name, halign=Gtk.Align.START)
            name.add_css_class("flat")
            name.add_css_class("am-menu-item")
            name.set_tooltip_text(_("Its network statistics"))
            name.connect("clicked", lambda *_a, c=agent.command: (popover.popdown(), self.open_traffic(c)))
            grid.attach(name, 0, row, 1, 1)
            for column, text in ((1, "↓ " + traffic.rate_text(rate[0])), (2, "↑ " + traffic.rate_text(rate[1])),
                                 (3, _("{0} in all").format(traffic.size_text((stats.received + stats.sent)
                                                                              if stats else 0)))):
                cell = Gtk.Label(label=text, xalign=1)
                cell.add_css_class("am-fact-value" if column < 3 else "am-agent-detail")
                if not running:
                    cell.add_css_class("dim-label")
                grid.attach(cell, column, row, 1, 1)
        box.append(grid)
        popover.set_child(box)
        return popover

    def traffic_tip(self, node_id: str) -> str:
        rate = self.side_map.traffic.get(node_id)
        if rate is None:
            return ""
        return _("Network: ↓ {0} ↑ {1}. Click for its network statistics.").format(
            traffic.rate_text(rate[0]), traffic.rate_text(rate[1]))

    # --- the page ----------------------------------------------------------------------------------

    def open_traffic(self, command: str):
        agent = next((a for a in self.agent_list if a.command == command), None)
        if agent is None:
            return
        if self.page_name() != "traffic":
            self.traffic_return = self.view if self.page_name() in ai_core.VIEWS else "main"
        self.traffic_agent = agent
        self.traffic_title.set_label(_("Network · {0}").format(agent.name).upper())
        self.refresh_traffic_page()
        self.show_page("traffic")
        self.sample_traffic()

    def close_traffic(self):
        self.traffic_agent = None
        self.show_view(getattr(self, "traffic_return", self.view), save=False)

    def refresh_traffic_page(self):
        agent = self.traffic_agent
        if agent is None:
            return
        with self.traffic_lock:
            stats = self.traffic.stats.get(agent.command)
            history = list(stats.history) if stats else []
            places = sorted(stats.places.items(), key=lambda p: -(p[1].received + p[1].sent)) if stats else []
            programs = sorted(stats.programs.items(), key=lambda p: -(p[1].received + p[1].sent)) if stats else []
        running = agent.command in self.traffic_running
        rate_in, rate_out = stats.rate if stats else (0.0, 0.0)

        body = self.traffic_now.body
        self.clear_box(body)
        self.traffic_now.set_level("ok" if running else "", _("Now") if running else _("Not running"))
        body.append(self.stat_tiles([
            (_("In"), "↓ " + traffic.rate_text(rate_in), _("{0} in all").format(traffic.size_text(stats.received))
             if stats else ""),
            (_("Out"), "↑ " + traffic.rate_text(rate_out), _("{0} in all").format(traffic.size_text(stats.sent))
             if stats else ""),
            (_("Connections"), str(stats.connections if stats and running else 0), ""),
            (_("Counted since"), time.strftime("%H:%M", time.localtime(stats.since)) if stats else "–", ""),
        ]))
        if not running and not stats:
            self.card_text(body, _("{0} is not running. Start it on the map: its traffic is counted from then "
                                   "on, while this app is open.").format(agent.name))

        self.traffic_history = history
        self.traffic_chart.queue_draw()
        top = max((max(r[1], r[2]) for r in history), default=0)
        self.traffic_legend.set_label(_("↓ in (green) · ↑ out (blue) · highest {0}").format(traffic.rate_text(top))
                                      if history else _("The chart fills while {0} runs.").format(agent.name))

        self.fill_traffic_table(self.traffic_places, [
            (name, _(KINDS.get(kind, kind)), totals) for (name, kind), totals in places[:30]],
            _("No connections yet."))
        self.fill_traffic_table(self.traffic_programs, [
            (name, _("the agent") if name.split(".")[0] == agent.command else _("started by it"), totals)
            for name, totals in programs[:20]], _("No program has moved data yet."))
        self.clear_box(self.traffic_about.body)
        self.card_text(self.traffic_about.body, _(
            "From the TCP connections of {0} and the programs it started (its MCP servers, its tools), read "
            "every {1} seconds while this app is open: what each moved since the last look. Connections to "
            "a local LLM on this PC count too. Not counted: UDP (QUIC), and traffic while the app was "
            "closed.").format(agent.name, traffic.SAMPLE_EVERY))

    def fill_traffic_table(self, card, rows, empty: str):
        """Rows of name · kind · ↓ in · ↑ out, with a bar for the share of all traffic."""
        body = card.body
        self.clear_box(body)
        if not rows:
            self.card_text(body, empty, "am-agent-detail")
            return
        total = sum(t.received + t.sent for _n, _k, t in rows) or 1
        grid = Gtk.Grid(column_spacing=16, row_spacing=6)
        for column, text in enumerate(("", "", _("In"), _("Out"), _("Share"))):
            if text:
                head = Gtk.Label(label=text.upper(), xalign=0)
                head.add_css_class("am-fact-key")
                grid.attach(head, column, 0, 1, 1)
        for row, (name, kind, totals) in enumerate(rows, 1):
            label = Gtk.Label(label=name, xalign=0, hexpand=True, selectable=True)
            label.add_css_class("am-card-text")
            if totals.connections:
                label.set_tooltip_text(ngettext("1 connection open", "{0} connections open",
                                                totals.connections).format(totals.connections))
            grid.attach(label, 0, row, 1, 1)
            tag = Gtk.Label(label=kind, xalign=0)
            tag.add_css_class("am-agent-detail")
            grid.attach(tag, 1, row, 1, 1)
            for column, value in ((2, "↓ " + traffic.size_text(totals.received)),
                                  (3, "↑ " + traffic.size_text(totals.sent))):
                cell = Gtk.Label(label=value, xalign=1)
                cell.add_css_class("am-fact-value")
                grid.attach(cell, column, row, 1, 1)
            share = Gtk.LevelBar(min_value=0, max_value=1, value=(totals.received + totals.sent) / total,
                                 valign=Gtk.Align.CENTER)
            share.set_size_request(90, 6)
            grid.attach(share, 4, row, 1, 1)
        body.append(grid)

    def draw_traffic_chart(self, _area, cr, width, height):
        """In (green) and out (accent) per second, the newest at the right, over a faint baseline."""
        history = getattr(self, "traffic_history", [])
        colors = self.side_map.colors
        from map_view import rgba
        cr.set_source_rgba(*rgba(colors.get("fg", "#cdd6f4"), 0.12))
        cr.set_line_width(1)
        cr.move_to(0, height - 0.5)
        cr.line_to(width, height - 0.5)
        cr.stroke()
        if len(history) < 2:
            return
        top = max(max(r[1], r[2]) for r in history) or 1
        step = width / (traffic.HISTORY - 1)
        start = width - step * (len(history) - 1)
        for index, color in ((1, colors.get("green", "#a6e3a1")), (2, colors.get("accent", "#89b4fa"))):
            cr.set_source_rgba(*rgba(color, 0.95))
            cr.set_line_width(2)
            for i, sample in enumerate(history):
                x, y = start + i * step, height - 4 - (height - 10) * sample[index] / top
                cr.line_to(x, y) if i else cr.move_to(x, y)
            cr.stroke()
