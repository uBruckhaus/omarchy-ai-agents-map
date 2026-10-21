"""AI Agents Map: the Add skills page (a mixin of ai_popup.AgentsMap, split off to keep ai_popup.py
small; its methods use the window's state through self)."""

from gi.repository import GLib, Gtk

import ai_core
import connect
import skills_ctl
import skills_web
import threading
import web_sources
from i18n import _, ngettext


class SkillsPage:
    # --- add skills: found on this PC or from the web ------------------------------------------------

    def build_add_skills_page(self) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP)
        page.add_css_class("am-add-page")       # one text scale (see the CSS)
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
        back.set_tooltip_text(_("Back without changing anything (Backspace, Esc)"))
        back.connect("clicked", lambda *_: self.close_add_skills())
        header.append(back)
        title = Gtk.Label(label=_("Add skills").upper(), xalign=0, hexpand=True)
        title.add_css_class("am-section-title")
        header.append(title)
        page.append(header)
        rule = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        rule.add_css_class("am-settings-rule")
        page.append(rule)
        self.intro_card(page, _("Skills teach your agents how to do a task."), [
            _("A skill is a folder with a SKILL.md: instructions, sometimes with scripts or templates, for one kind "
              "of work, e.g. making a PDF or releasing your app. An agent reads it only when a task fits, so many "
              "skills cost little."),
            _("Skills live in a folder; every agent that reads the folder has them at once. Pick skills from this "
              "PC or the web, choose the folder, then Add. Then just ask for the task: the agent finds the skill."),
        ])
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP)
        body.add_css_class("am-settings")
        scroller_holder = [self.page_scroller(body)]
        page.append(scroller_holder[0])

        local_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP)
        web_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP)
        body.append(self.where_switch("skills", local_box, web_box))
        body.append(local_box)
        body.append(web_box)
        self.section_heading(local_box, _("Found on this PC"))
        self.skills_found_status = Gtk.Label(xalign=0, wrap=True, max_width_chars=1)
        self.skills_found_status.add_css_class("am-agent-detail")
        local_box.append(self.skills_found_status)
        self.skills_found_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        local_box.append(self.skills_found_list)

        self.section_heading(web_box, _("Search the web"))
        web_box.append(self.source_picker("skills"))
        line = Gtk.Box(spacing=8)
        self.skills_query = Gtk.Entry(hexpand=True, placeholder_text=_("e.g. pdf, docker, testing"))
        self.skills_query.connect("activate", lambda *_: self.search_skills())
        line.append(self.skills_query)
        search = Gtk.Button(label=_("Search"))
        search.add_css_class("am-small-btn")
        search.connect("clicked", lambda *_: self.search_skills())
        line.append(search)
        web_box.append(line)
        self.skills_web_status = Gtk.Label(xalign=0, wrap=True, max_width_chars=1,
                                           label=_("From the skills.sh directory; the files come from the skill's "
                                                   "GitHub repository. Skills can hold scripts an agent may run: "
                                                   "check where one comes from before adding it."))
        self.skills_web_status.add_css_class("am-agent-detail")
        web_box.append(self.skills_web_status)
        self.skills_web_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        web_box.append(self.skills_web_list)

        self.skills_selected_heading = Gtk.Label(label=_("Selected").upper(), xalign=0)
        self.skills_selected_heading.add_css_class("am-section-heading")
        body.append(self.skills_selected_heading)
        self.skills_selected = Gtk.Label(xalign=0, wrap=True, max_width_chars=1, label=_("Nothing selected yet."))
        self.skills_selected.add_css_class("am-detail-value")
        body.append(self.skills_selected)
        self.skills_into = Gtk.DropDown.new_from_strings([""])
        self.skills_into.connect("notify::selected", lambda *_: self.update_skills_preview())
        self.skills_into_row = self.setting_line(body, _("Into"), _("The folder the skill goes to. Agents that read "
                                                                   "it get the skill at once."), self.skills_into)
        self.skills_agents = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=4,
                                         column_spacing=8, row_spacing=4)
        self.setting_line(body, _("Also for"), _("Agents that do not read this folder yet get it too."),
                          self.skills_agents)
        self.skills_agents.set_halign(Gtk.Align.FILL)
        buttons = Gtk.Box(spacing=10)
        self.skills_status = Gtk.Label(xalign=0, wrap=True, max_width_chars=1, hexpand=True)
        self.skills_status.add_css_class("am-agent-detail")
        buttons.append(self.skills_status)
        self.skills_ok = Gtk.Button(label=_("Add"), valign=Gtk.Align.CENTER)
        self.skills_ok.add_css_class("am-primary")
        self.skills_ok.add_css_class("am-download-btn")
        self.skills_ok.connect("clicked", lambda *_: self.apply_skills())
        self.skills_scroller = scroller_holder[0]
        self.skills_body = body
        buttons.append(self.skills_ok)
        body.append(buttons)
        self.section_heading(body, _("What changes"))
        self.skills_preview = Gtk.TextView(editable=False, cursor_visible=False, monospace=True)
        self.skills_preview.add_css_class("am-diff")
        self.skills_preview.set_wrap_mode(Gtk.WrapMode.CHAR)
        body.append(self.skills_preview)
        self.skills_choice = None      # {"set": SkillSet} or {"web": Skill, "files": [...]}
        self.skills_targets = []
        self.skills_checks = {}
        return page

    def open_add_skills(self):
        if self.page_name() != "addskills":
            self.skills_return = self.view if self.page_name() in ai_core.VIEWS else "main"
        self.skills_choice = None
        self.skills_selected.set_label(_("Nothing selected yet."))
        self.skills_status.set_label("")
        # Target folders: the shared one first, then the user's own skill folders.
        targets = [skills_ctl.SHARED] + [s.path for s in self.inventory.skill_sets
                                         if s.path.startswith("~/") and s.path != skills_ctl.SHARED]
        self.skills_targets = list(dict.fromkeys(targets))
        readers = {}
        for agent in self.agent_list:
            if agent.installed:
                for target in self.skills_targets:
                    if skills_ctl.is_connected(agent.command, skills_ctl.expand(target)):
                        readers.setdefault(target, []).append(agent.name)
        labels = [f"{t} · " + _("read by {0}").format(", ".join(readers[t])) if readers.get(t) else t
                  for t in self.skills_targets]
        self.skills_into.set_model(Gtk.StringList.new(labels))
        self.skills_into.set_selected(0)
        flow = self.skills_agents
        while (child := flow.get_first_child()) is not None:
            flow.remove(child)
        self.skills_checks = {}
        for agent in self.agent_list:
            if agent.installed and skills_ctl.own_dir(agent.command):
                check = self.small_check(agent.name)
                check.connect("toggled", lambda *_: self.update_skills_preview())
                flow.append(check)
                self.skills_checks[agent.command] = check
        self.show_found_skills()
        self.mark_seen("skills")
        self.show_page("addskills")
        self.update_header()
        self.update_skills_preview()

    def close_add_skills(self):
        self.show_view(getattr(self, "skills_return", self.view), save=False)

    def show_found_skills(self):
        box = self.skills_found_list
        while (child := box.get_first_child()) is not None:
            box.remove(child)
        found = self.found_skills or []
        self.skills_found_status.set_label(
            _("Looking for skills that apps brought along…") if self.found_skills is None else
            _("No skills from apps that the agents do not have yet.") if not found else
            ngettext("1 skill folder from an app.", "{0} skill folders from apps.", len(found)).format(len(found)))
        for skill_set in found:
            detail = f"{skill_set.path} · " + ngettext("1 skill", "{0} skills", len(skill_set.skills)).format(
                len(skill_set.skills)) + ": " + ", ".join(skill_set.skills[:6])
            box.append(self.choice_row(skill_set.origin, detail, lambda *_, s=skill_set: self.use_skill_set(s),
                                       icon="folder"))
        if self.found_skills is None and not self.checking_new:
            self.check_new_apps()

    def use_skill_set(self, skill_set):
        self.skills_choice = {"set": skill_set}
        self.skills_selected.set_label(_("The skill folder {0} ({1}): agents ticked below get it.").format(
            skill_set.path, ", ".join(skill_set.skills[:8])))
        self.skills_into_row.set_visible(False)
        self.update_skills_preview()
        GLib.idle_add(self.scroll_to, self.skills_scroller, self.skills_body, self.skills_selected_heading,
                      self.skills_ok)

    def search_skills(self):
        query = self.skills_query.get_text().strip()
        base = web_sources.chosen("skills")
        if not query and not web_sources.github_repo(base):
            return                    # a repository lists all its skills without a search word
        self.skills_web_status.set_label(_("Searching…"))

        def work():
            try:
                result, error = skills_web.search(query, base=base), ""
            except (OSError, ValueError) as failure:
                result, error = [], str(failure)
            GLib.idle_add(done, result, error)

        def done(result, error):
            box = self.skills_web_list
            while (child := box.get_first_child()) is not None:
                box.remove(child)
            self.skills_web_status.set_label(_("Search failed: {0}").format(error) if error else
                                             ngettext("1 skill found.", "{0} skills found.", len(result))
                                             .format(len(result)))
            for skill in result[:25]:
                detail = f"github.com/{skill.repo}" + (" · " + ngettext("1 install", "{0} installs", skill.installs)
                                                       .format(f"{skill.installs:,}") if skill.installs else "")
                box.append(self.choice_row(skill.name, detail, lambda *_, k=skill: self.use_web_skill(k),
                                           icon="skill"))
            return False

        threading.Thread(target=work, daemon=True).start()

    def use_web_skill(self, skill):
        self.skills_selected.set_label(_("Fetching the file list of {0}…").format(skill.name))
        GLib.idle_add(self.scroll_to, self.skills_scroller, self.skills_body, self.skills_selected_heading)
        self.skills_ok.set_sensitive(False)

        def work():
            try:
                files, error = skills_web.fetch(skill), ""
            except (OSError, ValueError) as failure:
                files, error = [], str(failure)
            GLib.idle_add(done, files, error)

        def done(files, error):
            if error:
                self.skills_selected.set_label(_("Could not fetch {0}: {1}").format(skill.name, error))
                return False
            self.skills_choice = {"web": skill, "files": files}
            scripts = [f for f, _d, _x in files if f.endswith((".py", ".sh", ".js", ".ts", ".rb", ".pl"))]
            text = f"{skill.name} · github.com/{skill.repo}\n{skills_web.description(files)}"
            if scripts:
                text += "\n" + ngettext("Holds 1 script an agent may run: {1}", "Holds {0} scripts an agent may run: {1}",
                                        len(scripts)).format(len(scripts), ", ".join(scripts[:6]))
            self.skills_selected.set_label(text)
            self.skills_into_row.set_visible(True)
            self.update_skills_preview()
            GLib.idle_add(self.scroll_to, self.skills_scroller, self.skills_body, self.skills_selected_heading,
                          self.skills_ok)
            return False

        threading.Thread(target=work, daemon=True).start()

    def skills_plan(self):
        choice = self.skills_choice
        if choice is None:
            raise ValueError(_("Choose a skill folder or a skill first."))
        agents = [c for c, check in self.skills_checks.items() if check.get_active()]
        plans = []
        if "set" in choice:
            folder = choice["set"].path
            if not agents:
                raise ValueError(_("Tick the agents that should get it."))
        else:
            folder = self.skills_targets[self.skills_into.get_selected()]
            skill = choice["web"]
            target = skills_ctl.expand(folder) / skill.skill_id
            if target.exists():
                raise ValueError(_("{0} is there already.").format(skills_ctl.tilde(target)))
            install = connect.Plan("", "", skill.skill_id)
            install.files_add = [(target / inner, data, executable) for inner, data, executable in choice["files"]]
            plans.append(install)
        for agent in agents:
            plan_now = skills_ctl.plan(agent, folder)
            if not plan_now.empty():
                plans.append(plan_now)
        if not plans:
            raise ValueError(_("Nothing changes: it is set up like this already."))
        return connect.merge(plans), folder, agents

    def update_skills_preview(self):
        try:
            plan_now, _folder, _agents = self.skills_plan()
            text = plan_now.diff()
            self.skills_ok.set_sensitive(True)
            self.skills_ok.set_tooltip_text(None)
            if self.skills_status.get_label().startswith("⚠"):
                self.skills_status.set_label("")
        except (OSError, ValueError) as error:
            text = str(error)
            self.skills_ok.set_sensitive(False)
            self.skills_ok.set_tooltip_text(text)
            self.skills_status.set_label("⚠ " + text)
        self.skills_preview.get_buffer().set_text(text)

    def apply_skills(self):
        try:
            plan_now, folder, agents = self.skills_plan()
            backup = connect.apply(plan_now)
        except (OSError, ValueError, RuntimeError) as error:
            self.skills_status.set_label(_("Not changed: {0}").format(error))
            return
        choice = self.skills_choice
        what = choice["web"].name if "web" in choice else choice["set"].path
        self.found_skills = None
        self.close_add_skills()
        self.collect()
        self.refresh_overview()
        self.refresh_map()
        self.show_undo(_("{0} added").format(what) + (f" · {folder}" if "web" in choice else ""), backup)

    updating_layers = False

