"""GTK Configuration editor, shared by map Setup and Configure for this PC.

The draft survives Cancel in the review. Collection and writes are workers;
validation is pure and debounced. No provider is started by opening this page.
"""
from __future__ import annotations

import threading
import time

from gi.repository import GLib, Gtk

import ai_core
import provider_config as config
from i18n import _
from ui_helpers import copy_button, readonly_text


class Draft:
    """Display-independent editor state, including the reviewed snapshot."""
    def __init__(self, facts, model=None, profile='balanced'):
        self.facts = facts
        self.model = config.select_model(facts, model)
        self.profile = profile
        self.values = {}
        self.created = time.monotonic()
        self.review = None
        self.acknowledged = False
        self.backup = None
        self.applied_review = None
        self.reload_error = ''
        self.from_ai = False
        self.ai_answer = None
        self.helper_command = ''
        self.focus = ''
        self.web = False

    @property
    def missing_model(self):
        return self.facts['schema'][0]['scope'] == 'per_model' and not self.model

    @property
    def math(self):
        return config.ConfigMath(self.facts, self.model)

    @property
    def changed(self):
        current = self.math.display_current
        return any(v is None or v != current.get(k) for k, v in self.values.items())

    def edit(self, key, value):
        self.from_ai = False
        self.values[key] = value
        self.review = None
        self.acknowledged = False

    def reset(self):
        self.from_ai = False
        self.ai_answer = None
        self.values = {}
        self.review = None
        self.acknowledged = False

    def use_recommended(self):
        self.from_ai = False
        self.ai_answer = None
        recommended = self.math.recommend(self.profile)['values']
        self.values = {s['id']: recommended[s['id']] for s in self.facts['schema'] if s['verified']}
        self.review = None
        self.acknowledged = False

    def fill_proposal(self, answer):
        checked = config.proposal_answer(answer, self.facts, self.model)
        self.values, self.ai_answer = checked['values'].copy(), checked
        self.from_ai, self.review, self.acknowledged = True, None, False

    def check(self):
        if time.monotonic() - self.created >= 600:
            return {'errors': [{'key': '', 'text': _('Hardware facts have expired; reopen Configuration')}],
                    'warnings': [], 'fit': {}}
        return self.math.validate(self.values)

    def prepare(self, rt, **kwargs):
        check = self.check()
        if check['errors']:
            raise ValueError('\n'.join(e['text'] for e in check['errors']))
        self.review = config.preview(rt, self.facts, self.values, self.model, **kwargs)
        self.acknowledged = False
        return self.review


class ProviderConfigPage:
    def build_provider_config_page(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP)
        self.provider_config_title = Gtk.Label(label=_('Configuration'))
        page.append(self.page_header(self.provider_config_title, self.close_provider_config, _('Back (Backspace, Esc)')))
        self.provider_config_body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP)
        self.provider_config_body.add_css_class('am-settings')
        page.append(self.scrolled(self.provider_config_body))
        self.provider_config_generation = 0
        self.provider_config_timer = None
        self.provider_config_busy = False
        self.provider_config_cancel = None
        self.provider_config_progress = Gtk.Box(spacing=8, visible=False)
        self.provider_config_progress.append(Gtk.Spinner(spinning=True))
        self.provider_config_progress.append(Gtk.Label(label=_('Working…'), hexpand=True, xalign=0))
        self.provider_config_stop = self.action_button(_('Stop'), on_click=lambda: self.provider_config_cancel.set()
                                                      if self.provider_config_cancel is not None else None)
        self.provider_config_progress.append(self.provider_config_stop)
        page.append(self.provider_config_progress)
        return page

    def open_provider_config(self, rt, model=None, profile='balanced', prefill=None):
        if self.provider_config_busy:
            self.show_page('providerconfig')
            return  # Keep the job's draft, reviewed snapshot and eventual Undo handle.
        self.provider_config_cancel_timer()
        self.provider_config_body.set_sensitive(True)
        self.provider_config_progress.set_visible(False)
        if self.page_name() != 'providerconfig':
            self.provider_config_from = self.page_name()
        self.provider_config_rt = rt
        self.provider_config_generation += 1
        generation = self.provider_config_generation
        self.provider_config_draft = None
        self.show_page('providerconfig')
        self.clear_box(self.provider_config_body)
        self.card_text(self.provider_config_body, _('Reading hardware and provider settings…'))
        def work():
            try:
                found = config.collect(rt)
            except Exception as error:
                found = error
            GLib.idle_add(done, found)
        def done(found):
            if generation != self.provider_config_generation:
                return False
            self.clear_box(self.provider_config_body)
            if isinstance(found, Exception):
                self.note_card(self.provider_config_body, 'error', str(found))
            else:
                proposal = prefill
                try:
                    self.provider_config_draft = Draft(found, model, profile)
                except ValueError:
                    # The proposed model is gone (Ask AI's proposal is older than this PC's facts): the
                    # page opens on the provider's own choice, without the stale values.
                    try:
                        self.provider_config_draft = Draft(found, None, profile)
                    except ValueError as error:
                        self.note_card(self.provider_config_body, 'error', str(error))
                        return False
                    if proposal is not None:
                        self.show_error(_('The proposed model is no longer available. Choose a model and '
                                          'ask again.'))
                    proposal = None
                if proposal is not None:
                    try:
                        self.provider_config_draft.fill_proposal(proposal)
                    except ValueError as error:
                        self.show_error(str(error))
                self.render_provider_config()
            return False
        threading.Thread(target=work, daemon=True).start()

    def close_provider_config(self):
        self.stop_provider_config_dictation()      # leaving stops the microphone
        if not self.provider_config_busy:
            self.provider_config_generation += 1
        self.provider_config_cancel_timer()
        self.show_page(self.provider_config_from if self.provider_config_from != 'providerconfig' else 'main')

    def render_provider_config(self):
        self.provider_config_cancel_timer()
        # The form is built anew (a profile, a model, Reset…): a recording behind the old microphone
        # stops, and its transcription is dropped.
        self.stop_provider_config_dictation()
        draft = self.provider_config_draft
        if draft is None:
            return
        body, facts = self.provider_config_body, draft.facts
        self.provider_config_labels = {}
        self.provider_config_defaults = {}
        self.provider_config_ai_badge = None
        self.clear_box(body)
        self.provider_config_title.set_label(_('{0} · Configuration').format(facts['runtime']))
        hw = facts['hardware']
        card = self.info_card(_('This PC'))
        self.card_text(card.body, _('{0} · {1:.1f} GiB video memory · {2:.1f} GiB RAM · {3} physical / {4} logical cores').format(
            hw.get('gpu') or _('CPU'), hw['vram_total'] / config.GIB, hw['ram_total'] / config.GIB,
            hw['physical_cores'], hw['logical_cores']))
        self.card_text(card.body, _('Build: {0} · GPU stacks: {1}').format(facts['build'] or _('not known'), ', '.join(hw['stacks']) or _('none found')))
        body.append(card)
        profiles = Gtk.Box(spacing=6)
        for key, label in zip(config.PROFILES, config.PROFILE_LABELS):
            button = self.action_button(label, on_click=lambda p=key: self.provider_config_profile(p))
            if key == draft.profile:
                button.add_css_class('am-primary')
            profiles.append(button)
        body.append(profiles)
        if facts['scope_models']:
            names = facts['scope_models']
            picker = Gtk.DropDown.new_from_strings([_('All models (default)') if n == '*' else n for n in names])
            picker.set_selected(names.index(draft.model))
            picker.connect('notify::selected', lambda w, _p: self.provider_config_model(names[w.get_selected()]))
            body.append(picker)
        elif facts['schema'][0]['scope'] == 'per_model':
            self.note_card(body, 'info', _('Add a model before configuring this provider.'))
        for note in facts.get('notes', []):
            self.note_card(body, 'warning', note)
        recommended = {r['id']: r for r in draft.math.recommend(draft.profile)['recommended']}
        table = self.info_card(_('Configuration'))
        self.card_text(table.body, _('Current → recommended · Your value'))
        body.append(table)
        advanced = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        expander = Gtk.Expander(label=_('Advanced'), child=advanced)
        for field in facts['schema']:
            key = field['id']
            row = Gtk.Box(spacing=8)
            row_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            now = draft.math.display_current.get(key)
            source_icon = '◴' if recommended[key]['source'] == 'measured' else '≈'
            hint = _('{0} → {1} {2}').format(_('Engine default') if now is None else now, source_icon, recommended[key]['recommended'])
            label = Gtk.Label(label=_(field['label']) + '\n' + hint, xalign=0, wrap=True, hexpand=True, max_width_chars=1)
            label.set_tooltip_text(_(recommended[key]['reason']))
            self.provider_config_labels[key] = label
            row.append(label)
            current = draft.values.get(key, draft.math.effective({}).get(key))
            changed = key in draft.values and (current is None or current != draft.math.display_current.get(key))
            if changed:
                label.add_css_class('am-state-info')
            if field['type'] == 'bool':
                editor = Gtk.Switch(active=bool(current), valign=Gtk.Align.CENTER)
                editor.connect('notify::active', lambda w, _p, k=key: self.provider_config_edit(k, w.get_active()))
            elif field['type'] == 'enum':
                choices = field['values']
                editor = Gtk.DropDown.new_from_strings([str(v) for v in choices])
                editor.set_selected(choices.index(current) if current in choices else 0)
                editor.connect('notify::selected', lambda w, _p, k=key, c=choices: self.provider_config_edit(k, c[w.get_selected()]))
            else:
                # Text fields retain incomplete/invalid input so live validation can explain it.
                editor = Gtk.Entry(text='' if current is None else str(current), width_chars=10, max_length=32)
                editor.set_tooltip_text(_(field['help']))
                editor.connect('changed', lambda w, s=field: self.provider_config_text(s, w.get_text()))
            if draft.from_ai and draft.ai_answer and draft.ai_answer['reasons'].get(key):
                editor.set_tooltip_text(draft.ai_answer['reasons'][key])
            control = editor
            if field['type'] in ('int', 'float', 'int_or_special'):
                control = Gtk.Box(spacing=2)
                control.append(editor)
                control.append(self.action_button('−', tip=_('Decrease'),
                    on_click=lambda s=field, e=editor: self.provider_config_step(s, e, -1)))
                control.append(self.action_button('+', tip=_('Increase'),
                    on_click=lambda s=field, e=editor: self.provider_config_step(s, e, 1)))
                for special in field.get('special', []):
                    control.append(self.action_button(str(special),
                        on_click=lambda value=special, e=editor, k=key: self.provider_config_special(k, value, e)))
            control.set_sensitive(field['verified'] and not draft.missing_model and not (current is None and key in draft.values))
            if not field['verified']:
                control.set_tooltip_text(_('Not verified on this PC'))
            row.append(control)
            if field['scope'] == 'default' and draft.model != '*':
                default = Gtk.CheckButton(label=_('Use the default'), active=key in draft.values and current is None)
                default.set_sensitive(field['verified'] and (key in facts.get('overrides', {}).get(draft.model, []) or key in draft.values))
                default.connect('toggled', lambda w, k=key, e=control: self.provider_config_default(k, w.get_active(), e))
                self.provider_config_defaults[key] = default
                row_box.append(default)
            row_box.prepend(row)
            (advanced if field['importance'] == 'advanced' else table.body).append(row_box)
        table.body.append(expander)
        self.provider_config_fit = self.info_card(_('Memory fit'))
        self.provider_config_fit_text = self.card_text(self.provider_config_fit.body, '')
        self.provider_config_fit_bar = Gtk.LevelBar(min_value=0, max_value=1)
        self.provider_config_fit.body.append(self.provider_config_fit_bar)
        self.provider_config_messages = self.card_text(self.provider_config_fit.body, '')
        body.append(self.provider_config_fit)
        self.provider_config_snippet = readonly_text('')
        self.provider_config_snippet_card = self.info_card(_('Configuration advice'))
        self.provider_config_snippet_card.body.append(self.provider_config_snippet)
        self.provider_config_snippet_card.body.append(copy_button(lambda: self.provider_config_snippet.get_buffer().get_text(
            self.provider_config_snippet.get_buffer().get_start_iter(), self.provider_config_snippet.get_buffer().get_end_iter(), False), label=_('Copy')))
        body.append(self.provider_config_snippet_card)
        if not config.can_write(facts, draft.model) and not draft.missing_model:
            reason = facts['read_only_reason'] or _('Configuration file is unavailable')
            if facts['schema'][0]['writer'] in ('environment', 'command_advice', 'launch_advice'):
                self.card_text(self.provider_config_snippet_card.body, reason)
            else:
                self.note_card(body, 'info', reason)
        self.provider_config_apply_button = self.action_button(_('Apply…'), role='primary', on_click=self.review_provider_config)
        reset = self.action_button(_('Reset'), on_click=lambda: self.provider_config_reset())
        use = self.action_button(_('Use recommended'), on_click=lambda: self.provider_config_recommended())
        use.set_sensitive(not draft.missing_model)
        undo = self.action_button(_('Undo'), on_click=self.undo_provider_config)
        undo.set_sensitive(draft.backup is not None)
        body.append(self.action_row(use, reset, undo, self.provider_config_apply_button))
        self.provider_config_review_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP)
        body.append(self.provider_config_review_box)
        target = config.config_model(facts, draft.model)
        if facts['key'] == 'llamacpp' and draft.model != '*' and target.get('file') and facts.get('build_mark'):
            body.append(self.action_button(_('Measure and tune'), on_click=self.measure_provider_config))
        self.provider_config_ask_form(body)
        self.provider_config_validate()
        self.guard_text_fields_in(body)

    def provider_config_profile(self, profile):
        self.provider_config_draft.profile = profile
        self.render_provider_config()

    def provider_config_model(self, model):
        draft = self.provider_config_draft
        draft.model = model
        draft.reset()
        self.render_provider_config()

    def provider_config_reset(self):
        self.provider_config_draft.reset()
        self.render_provider_config()

    def provider_config_recommended(self):
        self.provider_config_draft.use_recommended()
        self.render_provider_config()

    def provider_config_default(self, key, active, editor):
        draft = self.provider_config_draft
        field = next(s for s in draft.facts['schema'] if s['id'] == key)
        if not field['verified']:
            editor.set_sensitive(False)
            return
        overridden = key in draft.facts.get('overrides', {}).get(draft.model, [])
        value = None if active else draft.math.effective({})[key]
        self.provider_config_edit(key, value)
        if active and not overridden:
            draft.values.pop(key, None)
        self.render_provider_config()

    def provider_config_step(self, field, editor, direction):
        import math
        try:
            value = float(editor.get_text())
            if not math.isfinite(value):
                raise ValueError('nonfinite')
        except ValueError:
            value = field['range'][0]
        step = .01 if field['type'] == 'float' else 4096 if field['role'] == 'context' else 64 if field['role'] in ('batch', 'ubatch') else 1
        value = max(field['range'][0], min(field['range'][1], value + step * direction))
        editor.set_text(str(round(value, 2) if field['type'] == 'float' else int(value)))

    def provider_config_text(self, field, text):
        try:
            value = float(text) if field['type'] == 'float' else int(text)
        except ValueError:
            value = text  # Includes special 'all'; invalid text remains visible with an error.
        self.provider_config_edit(field['id'], value)

    def provider_config_special(self, key, value, editor):
        editor.set_text(str(value))
        self.provider_config_edit(key, value)

    def provider_config_edit(self, key, value):
        self.provider_config_draft.edit(key, value)
        default = getattr(self, 'provider_config_defaults', {}).get(key)
        if default is not None:
            field = next(s for s in self.provider_config_draft.facts['schema'] if s['id'] == key)
            default.set_sensitive(field['verified'])
        if getattr(self, 'provider_config_ai_badge', None) is not None:
            self.provider_config_ai_badge.set_label(_('AI advice (draft edited manually)'))
        label = getattr(self, 'provider_config_labels', {}).get(key)
        if label is not None:
            changed = value is None or value != self.provider_config_draft.math.display_current.get(key)
            label.add_css_class('am-state-info') if changed else label.remove_css_class('am-state-info')
        self.clear_box(self.provider_config_review_box)
        self.provider_config_apply_button.set_sensitive(False)
        if self.provider_config_timer:
            GLib.source_remove(self.provider_config_timer)
        self.provider_config_timer = GLib.timeout_add(250, self.provider_config_validate)

    def provider_config_validate(self):
        self.provider_config_cancel_timer()
        draft = self.provider_config_draft
        if draft is None:
            return False
        check = draft.check()
        f = check['fit']
        level = 'error' if check['errors'] else 'warning' if check['warnings'] else 'ok'
        self.provider_config_fit.set_level(level)
        self.provider_config_fit_text.set_label(
            _('Weights {0:.1f} + KV {1:.1f} + reserve {2:.1f} = {3:.1f} / {4:.1f} GiB ({5})').format(
                f['weights'] / config.GIB, f['kv'] / config.GIB, f['reserve'] / config.GIB,
                f['total'] / config.GIB, f['available'] / config.GIB, f['basis'].upper()) if f else '')
        self.provider_config_fit_bar.set_value(min(1, f['total'] / f['available']) if f and f['available'] else 0)
        self.provider_config_messages.set_label('\n'.join(e['text'] for e in check['errors'] + check['warnings']))
        self.provider_config_apply_button.set_sensitive(bool(draft.changed and not check['errors'] and config.can_write(draft.facts, draft.model)))
        advice = config.snippet(draft.facts, draft.math.effective(draft.values)) if (
            not check['errors'] and not draft.missing_model
            and not config.can_write(draft.facts, draft.model)
            and draft.facts['schema'][0]['writer'] in ('environment', 'command_advice', 'launch_advice')) else ''
        self.provider_config_snippet.get_buffer().set_text(advice)
        self.provider_config_snippet_card.set_visible(bool(advice))
        return False

    def provider_config_cancel_timer(self):
        timer = getattr(self, 'provider_config_timer', None)
        if timer is not None:
            GLib.source_remove(timer)
            self.provider_config_timer = None

    def review_provider_config(self):
        draft = self.provider_config_draft
        def prepare():
            return draft.prepare(self.provider_config_rt)
        def show(review):
            box = self.provider_config_review_box
            self.clear_box(box)
            card = self.info_card(_('What changes'))
            card.body.append(readonly_text(review.plan.diff()))
            box.append(card)
            self.card_text(box, _('Takes effect on the next start'))
            for note in draft.facts.get('notes', []):
                self.card_text(box, note)
            confirm = self.action_button(_('Apply configuration'), role='primary', on_click=self.apply_provider_config)
            empty = review.plan.empty()
            confirm.set_sensitive(not empty)
            if empty:
                self.card_text(box, _('No changes to write'))
            if review.check['warnings']:
                ack = Gtk.CheckButton(label=_('I understand these warnings'))
                for warning in review.check['warnings']:
                    self.card_text(box, warning['text'])
                confirm.set_sensitive(False)
                def acknowledge(w):
                    draft.acknowledged = w.get_active()
                    confirm.set_sensitive(w.get_active() and not empty)
                ack.connect('toggled', acknowledge)
                box.append(ack)
            cancel = self.action_button(_('Cancel'), on_click=lambda: self.clear_box(box))
            box.append(self.action_row(cancel, confirm))
            self.guard_text_fields_in(box)
        self.provider_config_job(prepare, show)

    def provider_config_job(self, work, done, cancel_event=None):
        if self.provider_config_busy:
            return
        self.provider_config_busy = True
        self.provider_config_cancel = cancel_event
        self.provider_config_stop.set_visible(cancel_event is not None)
        self.provider_config_progress.set_visible(True)
        self.provider_config_body.set_sensitive(False)
        generation = self.provider_config_generation
        def run():
            try:
                result = work()
            except Exception as error:
                result = error
            GLib.idle_add(finish, result)
        def finish(result):
            self.provider_config_busy = False
            self.provider_config_cancel = None
            self.provider_config_progress.set_visible(False)
            self.provider_config_body.set_sensitive(True)
            if generation != self.provider_config_generation:
                return False
            if isinstance(result, Exception):
                self.show_error(_('Not changed: {0}').format(result))
            else:
                done(result)
            return False
        threading.Thread(target=run, daemon=True).start()

    def apply_provider_config(self):
        draft = self.provider_config_draft
        if draft.review is None:
            return
        def done(result):
            draft.backup, draft.applied_review, draft.reload_error = result.backup, draft.review, result.reload_error
            # Recollect after saving: defaults may have affected several model scopes.
            # A failed recollection still leaves the successful write's backup available.
            if result.fresh_facts is not None:
                draft.facts = result.fresh_facts
                draft.created = time.monotonic()
            else:
                draft.created -= 600
            draft.reset()
            self.render_provider_config()
            message = _('Configuration saved. Takes effect on the next start.')
            if result.reload_error:
                self.show_error(_('Configuration saved with Undo; service reload failed: {0}').format(result.reload_error))
            else:
                self.toast(message, 6)
                self.provider_config_offer_control()
        def work():
            result = config.apply(draft.review, acknowledged=draft.acknowledged)
            try:
                result.fresh_facts = config.collect(self.provider_config_rt)
            except Exception:
                result.fresh_facts = None
            return result
        self.provider_config_job(work, done)

    def provider_config_offer_control(self):
        draft, rt = self.provider_config_draft, self.provider_config_rt
        if not rt.running:
            return
        operation = 'restart' if draft.facts['restart'] == 'service' and draft.facts['can_control'] else 'reload_model' if draft.facts['can_reload'] else ''
        if operation:
            label = _('Restart now') if operation == 'restart' else _('Reload the model')
            button = self.action_button(label, on_click=lambda: self.provider_config_job(
                lambda: config.control(rt, operation, draft.model, guard=self.provider_config_guard),
                lambda _r: self.toast(_('Provider configuration is active'), 6)))
            self.provider_config_review_box.append(button)

    def provider_config_guard(self, rt):
        import gpu_usage
        import llm_clients
        connected = set(rt.agents) | set(rt.active_agents) | {a for a in config.connect.SUPPORTED if config.connect.is_connected(a, rt.key)}
        clients = getattr(self, 'llm_clients', None)
        active_client = clients is not None and any(age <= llm_clients.ACTIVE for _name, age in clients.recent(rt.key))
        if connected & getattr(self, 'working_agents', set()) or active_client or gpu_usage.snapshot([rt]).generating:
            raise RuntimeError(_('This provider is in use; wait until its agents finish'))

    def undo_provider_config(self):
        draft = self.provider_config_draft
        if draft.backup is None or draft.applied_review is None:
            return
        def done(result):
            # Conditional Undo may leave files changed by another program; collect fresh facts.
            restored = len(result.restored or [])
            expected = len(draft.applied_review.plan.changes)
            self.toast(_('Restored {0} of {1} configuration files').format(restored, expected), 6)
            if result.reload_error:
                self.show_error(_('Undo saved; service reload failed: {0}').format(result.reload_error))
            self.open_provider_config(self.provider_config_rt, draft.model, draft.profile)
        self.provider_config_job(lambda: config.undo(draft.applied_review, draft.backup), done)

    def provider_config_ask_form(self, body):
        helpers = self.ask_helpers() if hasattr(self, 'ask_helpers') else []
        if not helpers:
            return
        draft = self.provider_config_draft
        card = self.info_card(_('Ask AI'))
        names = [helper.name for helper in helpers]
        picker = Gtk.DropDown.new_from_strings(names)
        index = next((i for i, h in enumerate(helpers) if h.command == draft.helper_command), 0)
        picker.set_selected(index)
        picker.set_sensitive(not draft.missing_model)
        card.body.append(picker)
        focus = Gtk.Entry(text=draft.focus, placeholder_text=_('What matters most?'), max_length=500)
        focus.connect('changed', lambda w: setattr(draft, 'focus', w.get_text()))
        import dictation
        engine = dictation.chosen(ai_core.setting('dictation_engine')) \
            if ai_core.setting('dictation') is not False else None
        if engine is None:
            card.body.append(focus)
        else:
            # Dictation into "What matters most?" (Maprisma's configuration page), the same engine as Ask AI.
            row = Gtk.Box(spacing=ai_core.GAP_INLINE)
            focus.set_hexpand(True)
            row.append(focus)
            mic = Gtk.ToggleButton(icon_name='audio-input-microphone-symbolic', tooltip_text=_('Dictate'),
                                   valign=Gtk.Align.CENTER)
            mic.connect('toggled', lambda b: self.provider_config_dictate(b, focus, engine))
            row.append(mic)
            card.body.append(row)
            names = {'omarchy': _('Omarchy dictation'), 'superwhisper': 'Superwhisper'}
            self.card_text(card.body, _('Dictation engine') + ' · ' + names.get(engine, engine))
        web = Gtk.CheckButton(label=_('Search the web for guidance'), active=draft.web)
        card.body.append(web)
        cost = self.card_text(card.body, '')
        def helper_changed(*_args):
            helper = helpers[picker.get_selected()]
            draft.helper_command = helper.command
            web.set_sensitive(helper.web and not helper.local)
            if not helper.web or helper.local:
                web.set_active(False)
            cost.set_label(_('Local, no tokens') if helper.local else _('Uses plan/tokens'))
        picker.connect('notify::selected', helper_changed)
        web.connect('toggled', lambda w: setattr(draft, 'web', w.get_active()))
        helper_changed()
        ask = self.action_button(_('Ask AI'), on_click=lambda: self.ask_provider_config(
            helpers[picker.get_selected()], focus.get_text(), web.get_active()))
        ask.set_sensitive(not draft.missing_model)
        card.body.append(ask)
        if draft.ai_answer is not None:
            self.provider_config_ai_badge = self.card_text(card.body, _('From Ask AI') if draft.from_ai else _('AI advice (draft edited manually)'))
            self.card_text(card.body, draft.ai_answer['summary'])
            for source in draft.ai_answer['sources']:
                self.card_text(card.body, source)
        body.append(card)

    def provider_config_dictate(self, button, entry, engine):
        """Start dictating into the focus field, or stop and put the words in (at most 500 characters).
        Both run on a worker (the engines' commands may take seconds); leaving the page, or hiding the
        window, stops a recording and drops a transcription still on its way (the generation)."""
        import dictation
        if getattr(self, 'provider_config_mic_busy', False):
            return
        generation = self.provider_config_mic_generation = getattr(self, 'provider_config_mic_generation', 0) + 1
        self.provider_config_mic = button
        if button.get_active():
            running = self.provider_config_dictation = dictation.Dictation(engine)
            entry.grab_focus()                       # where the desktop types the words
            button.set_sensitive(False)

            def start():
                try:
                    error = running.start()
                except Exception as failure:
                    error = str(failure) or 'could not start'
                GLib.idle_add(started, error)

            def started(error):
                button.set_sensitive(True)
                if generation != getattr(self, 'provider_config_mic_generation', 0):
                    if not error:
                        threading.Thread(target=self._cancel_provider_dictation, args=(running,), daemon=True).start()
                    return False                     # left meanwhile: the recording does not go on
                if error:
                    self.provider_config_dictation = None
                    self.provider_config_mic_reset()
                    self.toast(_('Could not start dictation: {0}').format(error), 8)
                return False
            threading.Thread(target=start, daemon=True).start()
            return
        running, self.provider_config_dictation = getattr(self, 'provider_config_dictation', None), None
        if running is None:
            return
        before = entry.get_text()
        button.set_sensitive(False)

        def work():
            try:
                text = running.stop()
            except Exception:
                text = ''
            GLib.idle_add(done, text if isinstance(text, str) else '')

        def done(text):
            button.set_sensitive(True)
            if generation != getattr(self, 'provider_config_mic_generation', 0) or self.page_name() != 'providerconfig':
                return False                         # left meanwhile: the words are dropped
            if text and entry.get_text() == before:
                entry.set_text(((before + ' ') if before and not before.endswith(' ') else before) + text)
                entry.set_text(entry.get_text()[:500])
            return False
        threading.Thread(target=work, daemon=True).start()

    def provider_config_mic_reset(self):
        button = getattr(self, 'provider_config_mic', None)
        if button is not None and button.get_active():
            self.provider_config_mic_busy = True
            try:
                button.set_active(False)
            finally:
                self.provider_config_mic_busy = False

    @staticmethod
    def _cancel_provider_dictation(running):
        try:
            running.cancel()
        except Exception:
            pass

    def stop_provider_config_dictation(self):
        """Leaving the page (any route) or hiding the window: the microphone stops, a transcription
        still on its way is dropped, the mic button is off again."""
        self.provider_config_mic_generation = getattr(self, 'provider_config_mic_generation', 0) + 1
        running, self.provider_config_dictation = getattr(self, 'provider_config_dictation', None), None
        self.provider_config_mic_reset()
        if running is not None:
            threading.Thread(target=self._cancel_provider_dictation, args=(running,), daemon=True).start()

    def ask_provider_config(self, helper, focus, web):
        draft = self.provider_config_draft
        if self.provider_config_busy or draft.missing_model:
            return
        try:
            config.require_allowed(draft.facts['key'])
        except PermissionError as error:
            self.show_error(str(error))
            return
        self.provider_config_busy = True
        self.provider_config_body.set_sensitive(False)
        generation = self.provider_config_generation
        rt = self.provider_config_rt
        def work(step):
            if helper.local:
                config.require_allowed(helper.candidate.runtime, action='start')
                config.require_allowed(helper.candidate.model, action='start', category='models')
                step('load')
                self.ready_helper_model(helper.candidate)
            step('check')
            fresh = config.collect(rt)
            config.select_model(fresh, draft.model)
            step('ask')
            return fresh, config.ask(helper, fresh, draft.model, draft.profile, focus, web=web)
        def done(result):
            self.provider_config_busy = False
            self.provider_config_body.set_sensitive(True)
            if generation != self.provider_config_generation:
                return
            if isinstance(result, Exception):
                return  # run_ai_help shows the error; it never retries.
            fresh, answer = result
            draft.facts, draft.created = fresh, time.monotonic()
            draft.fill_proposal(answer)
            self.render_provider_config()
        steps = [('check', _('Checking hardware and settings')), ('ask', _('Ask AI'))]
        if helper.local:
            steps.insert(0, ('load', _('Loading the local helper')))
        agent = helper.candidate.agent if helper.local else helper.command
        self.run_ai_help(_('Configure for this PC'), agent, steps, work, done,
                         model=helper.candidate.model if helper.local else None)

    def measure_provider_config(self):
        draft, rt = self.provider_config_draft, self.provider_config_rt
        cancel = threading.Event()
        def work():
            import gpu_usage
            config.require_allowed(rt.key)
            self.provider_config_guard(rt)
            use = gpu_usage.snapshot([rt]).runtimes.get(rt.key)
            if use is not None and (use.vram > gpu_usage.MODEL_VRAM or use.models):
                raise RuntimeError(_('Unload this provider’s models before measuring'))
            fresh = config.collect(rt)
            return fresh, config.measure(rt, fresh, draft.model, cancel=cancel)
        def done(found):
            fresh, records = found
            draft.facts, draft.created = fresh, time.monotonic()
            draft.facts['measurements'] = records
            draft.use_recommended()
            self.render_provider_config()
            self.toast(_('Measured settings are ready to review; no provider settings were written'), 6)
        self.provider_config_job(work, done, cancel_event=cancel)
