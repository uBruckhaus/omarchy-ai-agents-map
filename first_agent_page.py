"""The first-agent guide's page (first_agent.py): "Your first agent". First the way (online with a plan or a
key, or on this PC with a local LLM), then one checklist that is worked out again whenever the page is
shown, so it can be left and taken up again: what is missing first (an add-on, mise, Node), the agent,
then its sign-in or the local LLM with a starter model and the connection, and a test question. Each step
has one button, the app's own way to do it (installs in a terminal or on the install page, sign-in in the
agent's own terminal, the connection with its preview, backup and Undo); the next steps wait. When the
agent answers the test, it is marked as working and the page says so."""

from __future__ import annotations

import threading
import types

from gi.repository import GLib, Gtk

import ai_core
import first_agent
from i18n import _
from ui_helpers import size_text

GB = 1000 ** 3


class FirstAgentPage:
    first_catalog = None

    def build_first_agent_page(self) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP)
        page.append(self.page_header(Gtk.Label(label=_("Your first agent").upper()), self.close_first_agent,
                                     _("Back (Backspace, Esc)")))
        self.first_body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP)
        self.first_body.add_css_class("am-settings")
        page.append(self.page_scroller(self.first_body))
        return page

    first_hw = None                          # (gpu, gpu_stack.Stack), looked at on a thread (first_look)

    def first_look(self):
        """The graphics card and its software, once per open (lspci, pacman: seconds at worst), on a
        thread; the page is filled again when they are known."""
        import gpu_stack
        import install_ctl

        def work():
            try:
                gpu = install_ctl.detect_gpu()
                hw = (gpu, gpu_stack.look(gpu.vendor))
            except Exception:
                hw = (install_ctl.Gpu("", count=0, vram=0), gpu_stack.Stack())
            GLib.idle_add(done, hw)

        def done(hw):
            self.first_hw = hw
            if self.page_name() == "firstagent":
                self.fill_first_agent()
            return False
        threading.Thread(target=work, daemon=True).start()

    def open_first_agent(self):
        if self.page_name() != "firstagent":
            self.first_return = self.view if self.page_name() in ai_core.VIEWS else "main"
        self.first_catalog = first_agent.load(loaded_addons=getattr(self, "addons", ()))
        self.first_hw = None
        self.first_look()
        self.fill_first_agent()
        self.empty_area.set_visible(False)       # its "set up your first agent" is this page
        self.show_page("firstagent")

    def close_first_agent(self):
        self.show_view(getattr(self, "first_return", self.view), save=False)
        self.refresh_overview()                  # the card above the map again, where it stands

    def first_save(self, **changes):
        value = dict(first_agent.state())
        value.update(changes)
        self.save_setting(first_agent=value)

    # --- what is there -------------------------------------------------------------------------------

    def first_installed(self) -> set:
        return {a.command for a in self.agent_list if a.installed}

    def first_signed_in(self, command: str) -> bool:
        """Signed in, a key set, or connected to a local LLM (only whether a file or setting is there)."""
        import agent_details
        import agent_status
        access = agent_status.access_table().get(command)
        try:
            if access is not None and agent_status.credentials_present(access):
                return True
            return any(p.local for p in agent_details.details_for(command).providers)
        except Exception:
            return False

    def first_route(self):
        catalog = self.first_catalog or first_agent.load(loaded_addons=getattr(self, "addons", ()))
        chosen = first_agent.state().get("route")
        return next((r for r in catalog.routes if r.id == chosen), None)

    # --- the page ------------------------------------------------------------------------------------

    def fill_first_agent(self):
        body = self.first_body
        self.clear_box(body)
        if self.first_hw is None:
            waiting = Gtk.Box(spacing=ai_core.GAP_INLINE)
            waiting.append(Gtk.Spinner(spinning=True))
            label = Gtk.Label(label=_("Looking at this PC's graphics card…"), xalign=0)
            label.add_css_class("am-agent-detail")
            waiting.append(label)
            body.append(waiting)
            return
        route = self.first_route()
        if route is None:
            self.fill_first_choice(body)
        else:
            self.fill_first_steps(body, route)

    def fill_first_choice(self, body):
        catalog = self.first_catalog or first_agent.load()
        card = self.info_card(_("Get one agent working"), "info")
        self.card_text(card.body, _("An agent is the program you talk to: it reads, writes and runs things for you. "
                                    "It needs either an online account (a plan or an API key) or a model running "
                                    "on this PC. Pick one way; AI Agents Map shows every step and asks before each one."))
        body.append(card)
        vram = self.first_hw[0].vram
        for kind, title, hint in (("online", _("With an online account"),
                                   _("The strongest models; needs a plan or an API key, which costs money.")),
                                  ("local", _("On this PC"),
                                   _("Free and private; needs a graphics card with enough memory for a good model."))):
            routes = [r for r in catalog.routes if r.kind == kind]
            if not routes:
                continue
            self.section_heading(body, title, muted=False)
            self.note(body, hint)
            rows = self.boxed_list()
            for route in routes:
                lines = [_(route.access)]
                missing = self.first_unavailable(route)
                if missing:
                    row = self.plain_row(route.title, missing)
                    row.set_sensitive(False)
                    rows.append(row)
                    continue
                if kind == "local":
                    model = first_agent.suggested(catalog.models, vram)
                    if model is not None:
                        lines.append(_("Suggested for this graphics card ({0}): {1}, {2}").format(
                            size_text(vram) if vram else _("not known"), model.title, size_text(model.bytes)))
                row = self.plain_row(route.title, "\n".join(filter(None, lines)))
                if route.pricing:
                    row.add_suffix(self.action_button(_("Prices"), on_click=lambda r=route: self.open_uri(r.pricing)))
                row.add_suffix(self.action_button(_("Choose"), role="primary",
                                                  on_click=lambda r=route: self.first_choose(r)))
                rows.append(row)
            body.append(rows)
        body.append(self.action_row(self.action_button(_("Later"), size="page", on_click=self.first_later)))

    def first_unavailable(self, route) -> str:
        """Why this edition cannot offer the route ("" when it can): its agent or its local LLM is
        neither here nor in an add-on that could be switched on."""
        import harnesses
        import providers
        addons = [a for a in getattr(self, "addons", ()) if a.state in ("verified", "loaded")]
        for key, kind, here in ((route.agent, "agent", harnesses.get(route.agent) is not None),
                                (route.runtime, "provider", not route.runtime or providers.get(route.runtime) is not None)):
            if not here and not any(a.kind == kind and key in a.provides for a in addons):
                return _("Not available in this edition (its add-on is not here).")
        return ""

    def first_choose(self, route):
        self.first_save(route=route.id, later=False)
        self.fill_first_agent()

    def first_later(self):
        self.first_save(later=True)
        self.close_first_agent()

    # --- the steps -----------------------------------------------------------------------------------

    def first_steps(self, route) -> list:
        """[(title, done, detail, button label, action, blocked)] in order; the first one not done is
        the one to do now."""
        import harnesses
        import providers
        # mise (~/.local/bin) or its shims may have come since the app started: on PATH now, so the
        # install recipes and the agents' starts find them.
        ai_core.add_user_program_dirs()
        steps = []
        agent = next((a for a in self.agent_list if a.command == route.agent), None)
        name = agent.name if agent else route.name

        # An add-on that brings the agent or the local LLM, switched off: on first.
        for key, kind, present in ((route.agent, "agent", harnesses.get(route.agent) is not None),
                                   (route.runtime, "provider", not route.runtime or providers.get(route.runtime) is not None)):
            if present:
                continue
            addon = next((a for a in getattr(self, "addons", ()) if a.kind == kind and key in a.provides
                          and a.state in ("verified", "loaded")), None)
            steps.append((_("Switch on its add-on"), False, addon.name if addon else "",
                          _("Switch on"), (lambda a=addon: self.first_addon_on(a)) if addon else None,
                          "" if addon else _("This edition has no add-on for it.")))
        for tool in first_agent.blockers(route):
            steps.append((_("{0} on this PC").format(tool), False, "", "", None,
                          _("Install {0} with your system's package manager first.").format(tool)))
        missing = first_agent.missing(route)
        if "mise" in route.needs:
            steps.append((_("mise, the tool agents are installed with"), "mise" not in missing,
                          _("Installed for you only, in your home folder; no password needed."),
                          _("Install mise"), self.first_install_mise, ""))
        if "node" in route.needs:
            steps.append((_("Node.js, which {0} runs on").format(name), "node" not in missing,
                          _("Installed for you only by mise, in a terminal you watch."),
                          _("Install Node.js"), self.first_install_node, ""))
        installed = bool(agent and agent.installed)
        steps.append((_("{0} itself").format(name), installed, "", _("Install {0}").format(name),
                      lambda: self.offer_install("agent", route.agent), ""))
        if route.kind == "online":
            steps.append((_("Sign in or set an API key"), installed and self.first_signed_in(route.agent),
                          _(route.access), _("Sign in…"), lambda: self.offer_sign_in(route.agent), ""))
        else:
            steps += self.first_local_steps(route, name, installed)
        verified = route.agent in first_agent.working(self.first_installed())
        steps.append((_("A test question"), verified,
                      _("{0} answers one short question, so you see that it works.").format(name),
                      _("Send the test question"), lambda: self.first_test(route), ""))
        return steps

    def first_local_steps(self, route, name: str, installed: bool) -> list:
        import connect
        import install_ctl
        catalog = self.first_catalog or first_agent.load()
        import gpu_stack
        rt = next((r for r in self.inventory.runtimes if r.key == route.runtime), None)
        rt_name = rt.name if rt else route.runtime
        steps = []
        gpu, stack = self.first_hw
        if gpu.vendor == "amd":
            # Vulkan is enough to start (the graphics driver alone); ROCm is offered below, for speed.
            steps.append((_("Graphics card support"), stack.vulkan or bool(stack.rocm),
                          ", ".join(stack.summary()) or _("Vulkan or ROCm, so the model runs on the graphics card."),
                          _("Install Vulkan…"), lambda: self.offer_install("tool", "vulkan-radeon"), ""))
        steps += [(_("{0}, which runs the model").format(rt_name), bool(rt and rt.installed),
                  _("Installed for you only, as a background service; the install page shows what it does."),
                  _("Install {0}").format(rt_name), lambda: self.offer_install("runtime", route.runtime), "")]
        here = self.first_model_here(rt, catalog.models)
        have = here[0].title if here else None
        vram = gpu.vram
        model = first_agent.suggested(catalog.models, vram)
        detail = _("{0}: {1}, {2} (fits your graphics card: {3})").format(
            _("Suggested"), model.title, size_text(model.bytes), {"fits": _("yes"), "partly": _("partly"),
                                                                   "too large": _("no"), "unknown": _("not known")}[
                first_agent.fit(model, vram)]) if model else ""
        steps.append((_("A model"), have is not None, have or detail, _("Download it"),
                      (lambda m=model: self.first_download(rt, m)) if model and rt and rt.installed else None, ""))
        if here is not None:
            steps.append(self.first_context_step(rt, here, vram))
        connected = installed and bool(rt) and here is not None and connect.is_connected(route.agent, route.runtime) \
            and connect.is_active(route.agent, route.runtime) and self.first_uses(route.agent, here[1])
        steps.append((_("{0} connected to {1}").format(name, rt_name), connected,
                      _("A preview shows what changes in its settings; Undo puts it back."), _("Connect…"),
                      (lambda: self.first_connect(route, here[1])) if here else None, ""))
        return steps

    def first_context_step(self, rt, here, vram: int) -> tuple:
        """The model's context: at least what an agent needs (first_agent.MIN_CONTEXT), at most what the
        card holds for it (context_ctl.fit) and the catalog's size for it; set with a backup (Undo)."""
        import os
        import context_ctl
        import llm_engine
        from pathlib import Path
        model, name = here
        engine = llm_engine.of(rt)
        title = _("Enough context for an agent")
        try:
            folder = Path(os.path.expanduser(rt.models_dir)) if rt.models_dir else Path()
            found = engine.context_now(rt, folder, name)
        except Exception:
            found = None
        if not found:
            return (title, True, _("Set by the local LLM itself."), "", None, "")
        now, cache, model_file = found
        best = model.context
        if model_file is not None and vram:
            try:
                fit = context_ctl.fit(model_file, vram, cache or "f16")
                if fit.known:
                    best = min(best, fit.best)      # 0: the model leaves no room at all (blocked below)
            except Exception:
                pass
        if best < first_agent.MIN_CONTEXT:
            return (title, False, "", "", None,
                    _("This model leaves too little room for an agent on this card; choose a smaller one."))
        done = bool(now) and now >= first_agent.MIN_CONTEXT
        return (title, done, _("now {0} tokens; an agent needs at least {1}").format(now or "?", first_agent.MIN_CONTEXT),
                _("Set to {0}").format(best), lambda: self.first_set_context(rt, name, best), "")

    def first_set_context(self, rt, name: str, ctx: int):
        """As the Performance card does: the settings change with its backup (Undo), or the local
        LLM's own way (Ollama: its API), on a thread."""
        import llm_engine
        engine = llm_engine.of(rt)
        if engine.context_changes(rt, name, ctx) is not None:
            self.apply_tuning(rt, name, "ctx-size", str(ctx))
            GLib.timeout_add(1500, lambda: self.page_name() == "firstagent" and self.first_refresh() and False)
            return

        def work():
            try:
                engine.set_context(rt, name, ctx)
                return None
            except (OSError, ValueError, RuntimeError) as error:
                return str(error)

        def done(error):
            if error:
                self.show_error(_("Not changed: {0}").format(error))
            else:
                self.toast(_("{0}: context set to {1}").format(name, ctx), 5)
            self.first_refresh()
            return False
        threading.Thread(target=lambda: GLib.idle_add(done, work()), daemon=True).start()

    @staticmethod
    def first_uses(command: str, model: str) -> bool:
        """The agent's settings name this model (its model, without the provider)."""
        import agent_details
        import graph
        try:
            name, _provider = graph.split_model(agent_details.details_for(command).model)
        except Exception:
            return False
        return name.lower() == model.lower()

    def first_connect(self, route, model: str):
        """The connect page for the agent and the local LLM, with the starter model chosen."""
        self.connect_preselect = model
        self.on_map_link(f"agent:{route.agent}", f"runtime:{route.runtime}")

    @staticmethod
    def first_model_here(rt, models) -> tuple | None:
        """(starter model, the local LLM's own name for it) when it has one of them (by file or repo)."""
        if rt is None or not rt.installed:
            return None
        for model in models:
            stem, repo = model.file[:-5].lower(), model.repo.lower()
            for name in rt.models or ():
                if stem in str(name).lower() or repo in str(name).lower():
                    return model, str(name)
        return None

    def fill_first_steps(self, body, route):
        steps = self.first_steps(route)
        now = next((i for i, step in enumerate(steps) if not step[1]), None)
        card = self.info_card(route.title.upper(), "ok" if now is None else "info")
        self.card_text(card.body, _(route.access))
        body.append(card)
        rows = self.boxed_list()
        for index, (title, done, detail, label, action, blocked) in enumerate(steps):
            row = self.plain_row(title, blocked or detail)
            mark = Gtk.Label(label="✓" if done else ("→" if index == now else "·"))
            mark.add_css_class("am-check-mark")
            mark.add_css_class("am-state-ok" if done else "am-state-info")
            row.add_prefix(mark)
            if index == now and action is not None and not blocked:
                row.add_suffix(self.action_button(label, role="primary", on_click=action))
            rows.append(row)
        body.append(rows)
        if route.kind == "local":
            self.fill_first_optional(body)
        if now is None:
            self.fill_first_ready(body, route)
        buttons = [self.action_button(_("Check again"), size="page", on_click=self.first_refresh),
                   self.action_button(_("Choose another way"), size="page",
                                      on_click=lambda: (self.first_save(route=""), self.fill_first_agent()))]
        body.append(self.action_row(*buttons))

    def fill_first_optional(self, body):
        """On an AMD card: ROCm (and PyTorch for ROCm) for more speed, not needed to start."""
        import gpu_stack
        stack = self.first_hw[1]
        extra = [k for k in gpu_stack.wanted(stack) if k in ("rocm", "pytorch-rocm")]
        if not extra:
            return
        card = self.info_card(_("Optional: more speed on AMD cards"))
        self.card_text(card.body, _("Vulkan is enough to start. ROCm, AMD's compute software, is often faster; "
                                    "PyTorch for ROCm lets speech and image tools use the graphics card."))
        line = Gtk.Box(spacing=ai_core.GAP_INLINE)
        for key in extra:
            line.append(self.action_button(_("Install {0}…").format(gpu_stack.PACKAGES[key][0]),
                                           on_click=lambda k=key: self.offer_install("tool", k)))
        card.body.append(line)
        body.append(card)

    def fill_first_ready(self, body, route):
        agent = next((a for a in self.agent_list if a.command == route.agent), None)
        card = self.info_card(_("Your agent is ready"), "ok")
        self.card_text(card.body, _("{0} answered the test question. Start it here, or see it on the map.")
                       .format(agent.name if agent else route.title))
        answer = first_agent.state().get("answer")
        if isinstance(answer, str) and answer:
            self.card_text(card.body, "“" + answer[:300] + "”")
        line = Gtk.Box(spacing=ai_core.GAP_INLINE)
        if agent is not None:
            line.append(self.action_button(_("Start {0}").format(agent.name), role="primary",
                                           on_click=lambda: self.start_agent(agent)))
        line.append(self.action_button(_("Show on the map"), on_click=lambda: self.show_view("main", save=False)))
        card.body.append(line)
        body.append(card)

    def first_refresh(self):
        self.first_hw = None                 # an install may have brought ROCm or Vulkan
        self.first_look()
        self.collect()
        self.refresh_overview()
        self.refresh_map()
        self.fill_first_agent()

    # --- the actions ---------------------------------------------------------------------------------

    def first_addon_on(self, addon):
        import addon_store
        addon_store.set_enabled(addon.id, True)
        self.reload_addons()
        self.toast(_("{0} is on.").format(addon.name), 4)
        self.first_refresh()

    def first_install_mise(self):
        self.open_install(types.SimpleNamespace(key="mise", name="mise", installed=False), "install")

    def first_install_node(self):
        import terminal_pane
        ai_core.add_user_program_dirs()
        argv = first_agent.node_step()
        if terminal_pane.vte() is None:
            # No terminal inside the window: a terminal window of its own; Check again afterwards.
            import desktop
            from pathlib import Path
            if desktop.open_terminal(argv, str(Path.home())):
                self.toast(_("Node.js is installing in a terminal window. Check again when it is done."), 8)
            else:
                self.show_note(_("No terminal found (xdg-terminal-exec)"))
            return
        self.open_command_tab("install:node", _("Install Node.js"), "", self.back_to_here(),
                              on_end=self.first_refresh, argv=argv)

    def first_download(self, rt, model):
        """The starter model, as the Models page downloads one (a background job, continued after an
        interruption): a GGUF file into the local LLM's folder, or Ollama's pull of it."""
        import llm_engine
        import models_ctl
        entry = models_ctl.GgufFile(model.file, [model.file], model.bytes)
        engine = llm_engine.of(rt)
        name = ""
        if engine.download == "pull":
            try:
                name = engine.pull_name(rt, model.repo, entry, models_ctl.HF)
            except ValueError as problem:
                self.show_error(str(problem))
                return
        spec = {"kind": "pull" if engine.download == "pull" else "model", "runtime": rt.key, "name": name,
                "repo": model.repo, "base": models_ctl.HF, "into": "",
                "entry": {"name": entry.name, "paths": list(entry.paths), "size": entry.size}}
        if self.start_model_job(spec) is not None:
            self.toast(_("{0} is downloading: see the top of the main page. Check again when it is done.")
                       .format(model.title), 8)

    def first_test(self, route):
        """One fixed question through the agent's own ask command, in the progress box; its answer shown.
        Only an answer marks the agent as working."""
        import agent_profiles
        argv = first_agent.verify_argv(route.agent)
        if argv is None:
            self.show_note(_("{0} cannot be asked a test question from here.").format(route.title))
            return
        agent = next((a for a in self.agent_list if a.command == route.agent), None)
        name = agent.name if agent else route.title

        rt = next((r for r in self.inventory.runtimes if r.key == route.runtime), None) if route.runtime else None
        here = self.first_model_here(rt, (self.first_catalog or first_agent.load()).models) if rt else None

        def work(step):
            import ai_helper
            if rt is not None and here is not None:
                # Our own runs do not wake the local LLM (the watcher leaves them alone): started here
                # and its model loaded (waited for), so the question finds it ready.
                import runtime_ctl
                step("start")
                ok, why = runtime_ctl.load(rt, here[1], wait=90)
                if not ok:
                    raise RuntimeError(_("{0} could not load {1}: {2}").format(rt.name, here[1], why))
                step("ask")
            work_dir = agent_profiles.WORK_DIR
            work_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            out, ok = ai_helper._bounded(argv, max_bytes=64 * 1024, timeout=180, cwd=str(work_dir),
                                         merge_stderr=False)
            answer = first_agent.answer_ok(out, ok)
            if not answer:
                raise RuntimeError(_("no answer (is it signed in, and its model loaded?)"))
            return answer

        def done(result):
            if isinstance(result, Exception):
                return
            verified = [c for c in first_agent.state().get("verified", []) if c != route.agent] + [route.agent]
            self.first_save(verified=verified, answer=result[:300])
            self.collect()
            self.refresh_overview()
            self.refresh_map()
            if self.page_name() == "firstagent":
                self.fill_first_agent()

        steps = [("ask", _("{0} answers a test question").format(name))]
        if rt is not None and here is not None:
            steps.insert(0, ("start", _("Starting {0}").format(rt.name)))
        self.run_ai_help(_("Test of {0}").format(name), route.agent, steps, work, done)

    # --- where the guide is offered ------------------------------------------------------------------

    def first_agent_wanted(self) -> bool:
        """No agent works yet (and the guide was not put off): the welcome page and the empty map offer it."""
        if first_agent.state().get("later"):
            return False
        return first_agent.needs_guide(self.first_installed(), self.first_signed_in)

    def first_agent_card(self, box):
        """The card on the main view and on Setup while no agent works: the guide, or where it stands."""
        route = self.first_route()
        card = self.info_card(_("Set up your first agent") if route is None else
                              _("Finish setting up {0}").format(route.title), "info")
        self.card_text(card.body, _("No agent works on this PC yet. AI Agents Map guides you to one, online (a plan or "
                                    "an API key) or on this PC (a local LLM), and asks before each step."))
        line = Gtk.Box(spacing=ai_core.GAP_INLINE)
        line.append(self.action_button(_("Get started") if route is None else _("Continue"), role="primary",
                                       on_click=self.open_first_agent))
        line.append(self.action_button(_("Later"), on_click=lambda: (self.first_save(later=True), card.set_visible(False))))
        card.body.append(line)
        box.append(card)
        return card
