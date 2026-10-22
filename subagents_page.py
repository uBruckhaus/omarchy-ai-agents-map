"""AI Agents Map: the subagents an agent works with (a mixin of ai_popup.AgentsMap, split off to keep
ai_popup.py small; its methods use the window's state through self).

An agent that started another one and uses it (subagents.agents_using_agents, from the process tree)
gets an arrow to it on the map, along the left of the agents' card.

A running agent that works with subagents (subagents.py: Claude Code, OpenCode) has a mark in its box
on the map while one works; a click on it opens this page: a map of its own, laid out like the big one,
with the agent's card at the left (and its marks) and a card of its subagents at the right, each with
its task. The ones working are lit; the ones that finished in the last half hour are dimmed. The poll
(ai_popup.poll_gpu) reads them every 2 s while the window is open."""

from gi.repository import Gtk

import ai_core
import graph
import map_view
import subagents
from i18n import _, ngettext


class SubagentsPage:
    def build_subagents_page(self) -> Gtk.Widget:
        self.subagents_command = None
        self.subagent_lists = {}          # agent command -> [subagents.SubAgent], from the poll
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP)
        self.subagents_title = Gtk.Label()
        page.append(self.page_header(self.subagents_title, self.close_subagents, _("Back (Backspace, Esc)")))
        self.subagents_note = Gtk.Label(xalign=0, wrap=True)
        self.subagents_note.add_css_class("dim-label")
        page.append(self.subagents_note)
        self.sub_map = map_view.MapView()
        self.sub_map.group_titles = {"agents": _("Agents"), "subagents": _("Subagents")}
        self.sub_map.traffic_tip = self.traffic_tip
        self.sub_map.on_traffic = lambda node_id: self.open_traffic(node_id.partition(":")[2])
        self.sub_map.subagents_tip = self.subagents_tip
        page.append(self.page_scroller(self.sub_map.with_flow_layer(), bar_room=False))   # lights on working lines
        return page

    def subagents_tip(self, node_id: str) -> str:
        count = self.side_map.subagents.get(node_id, (0, 0))[0]
        if not count:
            return _("Its subagents of the last {0} minutes, all finished. Click to see them.").format(
                ai_core.setting("subagents_minutes"))
        return ngettext("Works with 1 subagent now. Click to see what it does.",
                        "Works with {0} subagents now. Click to see what they do.", count).format(count)

    def agent_link_tip(self, user: str, used: str) -> str:
        names = {f"agent:{a.command}": a.name for a in self.agent_list}
        return _("{0} uses {1}: it started it and works with it.").format(
            names.get(user, user.partition(":")[2]), names.get(used, used.partition(":")[2]))

    def set_subagent_lists(self, lists: dict):
        """From the poll: {agent command: [subagents.SubAgent]}; the map's marks and the open page follow."""
        self.subagent_lists = lists
        self.side_map.set_subagents({f"agent:{c}": (subagents.working(s), len(s)) for c, s in lists.items()})
        if self.page_name() == "subagents":
            self.refresh_subagents_page()

    def open_subagents(self, command: str):
        agent = next((a for a in self.agent_list if a.command == command), None)
        if agent is None:
            return
        if self.page_name() != "subagents":
            self.subagents_return = self.view if self.page_name() in ai_core.VIEWS else "main"
        self.subagents_command = command
        self.subagents_title.set_label(_("Subagents · {0}").format(agent.name).upper())
        self.refresh_subagents_page()
        self.show_page("subagents")

    def close_subagents(self):
        self.subagents_command = None
        self.show_view(getattr(self, "subagents_return", self.view), save=False)

    def subagents_graph(self, command: str, subs) -> graph.Graph:
        """The page's map: the agent's box as on the big map, one box per subagent, a line to each."""
        g = graph.Graph()
        agent_id = f"agent:{command}"
        main = self.side_map.graph
        agent = next((n for n in main.nodes if n.id == agent_id), None) if main is not None else None
        g.add(agent if agent is not None else graph.Node(agent_id, 1, command, kind="agent", status="running"))
        for sub in subs:
            if sub.running:
                state = _("working")
            else:
                minutes = max(1, round(sub.age / 60))
                state = ngettext("done 1 minute ago", "done {0} minutes ago", minutes).format(minutes)
            detail = " · ".join(p for p in (sub.kind, sub.folder, state) if p)
            node_id = f"sub:{sub.key}"
            g.add(graph.Node(node_id, 2, sub.task or sub.kind, detail, kind="subagent",
                             status="running" if sub.running else "", dim=not sub.running))
            g.link(agent_id, node_id, "subagent")
        return g

    def refresh_subagents_page(self):
        command = self.subagents_command
        if command is None:
            return
        subs = self.subagent_lists.get(command, [])
        working = subagents.working(subs)
        minutes, kept = ai_core.setting("subagents_minutes"), ai_core.setting("subagents_kept")
        if not subs:
            self.subagents_note.set_label(_("No subagent worked for it in the last {0} minutes.").format(minutes))
        else:
            self.subagents_note.set_label(
                ngettext("1 working now", "{0} working now", working).format(working) + " · "
                + _("finished ones stay here dimmed for {0} minutes, at most {1} (Settings › Subagents)")
                .format(minutes, kept))
        view, agent_id = self.sub_map, f"agent:{command}"
        view.set_palette(self.side_map.colors, self.side_map.radius)
        view.set_animate(self.side_map.animate)
        view.set_traffic({k: v for k, v in self.side_map.traffic.items() if k == agent_id})
        view.set_batteries({k: v for k, v in self.side_map.batteries.items() if k == agent_id})
        view.set_subagents({agent_id: (working, len(subs))})
        view.set_graph(self.subagents_graph(command, subs), ["", _("Agents"), _("Subagents"), ""])
