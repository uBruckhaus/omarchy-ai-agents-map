"""A local LLM's Performance card on its details page (llm_tuning): what runs it (the build, the GPU
stacks, the card), the context per model (what is set against what the card holds, with Set to …),
and Ask …: an agent (the local helper first) looks at these facts and suggests settings; each one the
app can stand behind gets Apply (written with a backup, Undo in the toast, noted in the protocol).

Also the GPU memory offer: when the last agent that used a local LLM's models closes and that LLM
still holds them, a toast asks whether to free the memory (unload its models)."""

from __future__ import annotations

import threading

from gi.repository import GLib, Gtk

import ai_core
import ai_helper
import connect
import llm_engine
from i18n import _
from ui_helpers import short_number


class LlmHelpPage:
    llm_tuning_answers: dict = {}          # runtime key -> (summary, [Suggestion]) of the last Ask

    # --- the card --------------------------------------------------------------------------------

    def fill_runtime_performance(self, body, rt):
        engine = llm_engine.of(rt)
        if not engine.tuning or not rt.installed:
            return
        group = self.group_card(body, "performance", _("Performance"), "am-chart-symbolic",
                                _("Build, context per model, and tips from an agent"))
        slot = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP)
        group.append(slot)
        waiting = Gtk.Box(spacing=8)
        waiting.append(Gtk.Spinner(spinning=True))
        label = Gtk.Label(label=_("Reading its settings and the models' sizes…"), xalign=0)
        label.add_css_class("am-agent-detail")
        waiting.append(label)
        slot.append(waiting)
        showing = self.status_showing

        def work():
            import install_ctl
            import llm_tuning
            try:
                facts = llm_tuning.collect(rt, engine, install_ctl.detect_gpu())
            except Exception as error:
                facts = error
            GLib.idle_add(done, facts)

        def done(facts):
            if self.status_showing != showing or slot.get_parent() is None:
                return False
            self.clear_box(slot)
            if isinstance(facts, Exception):
                self.note(slot, _("Its settings could not be read: {0}").format(str(facts)[:200]))
                return False
            self.fill_performance_facts(slot, rt, engine, facts)
            return False

        threading.Thread(target=work, daemon=True).start()

    def fill_performance_facts(self, slot, rt, engine, facts):
        import llm_tuning
        builds = dict(engine.builds)
        slot.append(self.stat_tiles([
            (_("Build"), builds.get(facts.build, facts.build or _("not known")), facts.devices),
            (_("GPU stacks"), ", ".join(facts.stacks) or _("none found"), ""),
            (_("Video memory"), llm_tuning.gb(facts.vram), facts.gpu)]))
        self.fill_gpu_software(slot, facts)
        self.settings_heading(slot, _("Context per model"))
        rows = self.boxed_list()
        for model in facts.models:
            over = bool(model.best) and model.context > model.best
            room = bool(model.best) and model.context and model.best > model.context
            line = " · ".join(filter(None, [
                llm_tuning.gb(model.size) if model.size else "",
                _("now {0}").format(short_number(model.context)) if model.context else _("its default"),
                _("fits {0}").format(short_number(model.best)) if model.best else "",
                _("trained for {0}").format(short_number(model.trained)) if model.trained else ""]))
            row = self.plain_row(model.name, line)
            if model.per_token:
                row.set_tooltip_text(_("{0} KB of video memory per token of context").format(model.per_token // 1024))
            if over or room:
                mark = Gtk.Label(label="!" if over else "↑")
                mark.add_css_class("am-status-mark")
                mark.add_css_class("am-state-warn" if over else "am-state-info")
                mark.set_tooltip_text(_("More than the card holds: it would not load, or spill into RAM") if over
                                      else _("The card holds more"))
                row.add_prefix(mark)
                button = self.action_button(_("Set to {0}").format(short_number(model.best)))
                button.connect("clicked", lambda _b, m=model.name, n=model.best:
                               self.apply_tuning(rt, m, "ctx-size", str(n)))
                row.add_suffix(button)
            rows.append(row)
        slot.append(rows)
        self.fill_tuning_ask(slot, rt, engine, facts)

    def fill_gpu_software(self, slot, facts):
        """ROCm, Vulkan, CUDA and the PyTorch builds here (gpu_stack), with Install… for what an AMD
        card would want (the system's package, sudo in a terminal; else the vendor's page)."""
        import gpu_stack
        import install_ctl
        stack = gpu_stack.look(install_ctl.detect_gpu().vendor, rocm_in_use=facts.build == "rocm")
        rows = [(_("Graphics card software"), ", ".join(stack.summary()) or _("none found"), "")]
        for torch in stack.torches[:6]:
            rows.append((_("PyTorch {0}").format(torch.version), f"{torch.backend} · {torch.where}", ""))
        self.detail_grid(slot, rows)
        for key in gpu_stack.wanted(stack):
            name = gpu_stack.PACKAGES[key][0]
            hint = {"rocm": _("ROCm lets a local LLM run its ROCm build, often faster than Vulkan on AMD cards."),
                    "pytorch-rocm": _("PyTorch is here for the CPU only: its ROCm build uses the graphics card."),
                    "vulkan-radeon": _("Without Vulkan or ROCm, a local LLM runs on the CPU only.")}[key]
            line = Gtk.Box(spacing=ai_core.GAP_INLINE)
            label = Gtk.Label(label=hint, xalign=0, wrap=True, hexpand=True, max_width_chars=1)
            label.add_css_class("am-agent-detail")
            line.append(label)
            line.append(self.action_button(_("Install {0}…").format(name),
                                           on_click=lambda k=key: self.offer_install("tool", k)))
            slot.append(line)

    def fill_tuning_ask(self, slot, rt, engine, facts):
        import local_helper
        local = self.local_ai_helper()
        online = ai_helper.chosen({a.command for a in self.agent_list if a.installed})
        if local is None and online is None:
            return
        name = local_helper.label(local) if local is not None else ai_helper.name_of(online)
        self.settings_heading(slot, _("Tips from an agent"))
        line = Gtk.Box(spacing=8)
        ask = self.action_button(_("Ask {0}").format(name), role="primary")
        ask.connect("clicked", lambda _b: self.ask_tuning(rt, engine, facts, slot))
        line.append(ask)
        hint = Gtk.Label(label=_("It sees the facts above (names and numbers only); you choose what to apply."),
                         xalign=0, wrap=True, hexpand=True)
        hint.add_css_class("am-agent-detail")
        line.append(hint)
        slot.append(line)
        answer = self.llm_tuning_answers.get(rt.key)
        if answer is not None:
            self.fill_tuning_answer(slot, rt, *answer)

    def fill_tuning_answer(self, slot, rt, summary, suggestions):
        if summary:
            self.note(slot, summary)
        if not suggestions:
            self.note(slot, _("No change suggested."))
            return
        builds = dict(llm_engine.of(rt).builds)
        rows = self.boxed_list()
        for item in suggestions:
            what = {"setting": f"{item.key} = {item.value}" + ("" if item.model == "*" else f" · {item.model}"),
                    "context": _("context {0} · {1}").format(item.value, item.model),
                    "build": _("the {0} build").format(builds.get(item.value, item.value)), "note": ""}[item.kind]
            row = self.plain_row(item.title, "\n".join(filter(None, [what, item.why])))
            if item.kind in ("setting", "context"):
                button = self.action_button(_("Apply"))
                button.connect("clicked", lambda _b, s=item: self.apply_tuning(rt, s.model, s.key, s.value))
                row.add_suffix(button)
            elif item.kind == "build":
                row.set_tooltip_text(_("Install it from the local LLM's menu on the map (Update…), then compare."))
            rows.append(row)
        slot.append(rows)

    # --- asking ---------------------------------------------------------------------------------

    def ask_tuning(self, rt, engine, facts, slot):
        import llm_tuning
        local = self.local_ai_helper()
        agent = local.agent if local is not None else ai_helper.chosen({a.command for a in self.agent_list
                                                                        if a.installed})
        if agent is None:
            return
        name = ai_helper.name_of(agent)

        def work(step):
            if local is not None:
                step("load")
                self.ready_helper_model(local)
            step("ask")
            look_up = ai_helper.advise_as_look_up(local) if local is not None else ai_helper.look_up
            answer = llm_tuning.ask(agent, facts, look_up)
            step.done("ask", _("{0} suggestions").format(len(answer[1])))
            return answer

        def done(result):
            if isinstance(result, Exception):
                return
            self.llm_tuning_answers[rt.key] = result
            if self.status_showing == ("runtime", rt.key) and slot.get_parent() is not None:
                self.clear_box(slot)
                self.fill_performance_facts(slot, rt, engine, facts)

        steps = [("ask", _("{0} looks at {1}'s settings").format(name, rt.name))]
        if local is not None:
            steps.insert(0, ("load", _("Loading {0}").format(local.model)))
        self.run_ai_help(_("Performance of {0}").format(rt.name), agent, steps, work, done,
                         model=local.model if local is not None else None)

    # --- applying -------------------------------------------------------------------------------

    def apply_tuning(self, rt, section: str, key: str, value: str):
        """One setting (or a model's context) written with a backup; Undo in the toast; a restart
        offered when it counts only after one."""
        engine = llm_engine.of(rt)

        def work():
            try:
                if key == "ctx-size":
                    changes = engine.context_changes(rt, section, int(value))
                else:
                    changes = engine.tuning_changes(rt, section, key, value)
                backup = connect.apply(connect.Plan(rt.name, rt.key, "", changes)) if changes else None
                return None, backup
            except (OSError, ValueError, RuntimeError) as error:
                return str(error), None

        def done(result):
            error, backup = result
            if error:
                self.show_error(_("Not changed: {0}").format(error))
                return False
            what = f"{key} = {value}" + ("" if section == "*" else f" · {section}")
            entry = getattr(self, "ai_entry", None)
            if entry is not None and backup is not None:
                entry.note_change(f"{rt.name}: {what}", str(backup))
            effect = engine.context_effect if key == "ctx-size" else engine.tuning_effect
            if effect == "restart" and rt.running:
                self.toast(_("{0}: {1}. It counts after a restart.").format(rt.name, what), 20, _("Restart now"),
                           lambda: self.restart_runtime(rt))
            elif backup is not None:
                self.show_undo(_("{0}: {1}").format(rt.name, what), backup)
            return False
        threading.Thread(target=lambda: GLib.idle_add(done, work()), daemon=True).start()

    def restart_runtime(self, rt):
        import runtime_ctl

        def work():
            runtime_ctl.stop(rt)
            ok, why = runtime_ctl.start(rt)
            GLib.idle_add(lambda: (self.toast(_("{0} restarted").format(rt.name) if ok else
                                              _("{0} did not start: {1}").format(rt.name, why), 6)) and False)
        threading.Thread(target=work, daemon=True).start()

    # --- freeing GPU memory after the last agent ---------------------------------------------------

    def offer_gpu_cleanup(self, before: dict, now: dict, usage):
        """`before`/`now`: the running agents (command -> models). A local LLM whose agents all closed
        and that still holds models: a toast asks whether to unload them (once per closing)."""
        import runtime_ctl
        if usage is None or not set(before) - set(now):
            return
        for rt in self.inventory.runtimes:
            use = getattr(usage, "runtimes", {}).get(rt.key)
            if use is None or not use.running or use.vram < 512 * 1024 ** 2:
                continue
            users = set(rt.agents) | set(rt.active_agents)
            if not users & (set(before) - set(now)) or users & set(now):
                continue                     # none of its agents closed, or one still runs
            size = f"{use.vram / 1024 ** 3:.1f}"

            def free(rt=rt):
                def work():
                    ok, why = runtime_ctl.unload(rt)
                    GLib.idle_add(lambda: (self.toast(_("{0}: GPU memory freed").format(rt.name) if ok else
                                                      _("Not freed: {0}").format(why), 5)) and False)
                threading.Thread(target=work, daemon=True).start()
            self.show_offer(_("No agent uses {0} now, but it still holds {1} GB of GPU memory.").format(rt.name, size),
                            _("Free it"), _("Keep"), free, lambda: None)
            return                           # one question at a time
