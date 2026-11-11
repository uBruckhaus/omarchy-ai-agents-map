"""Reviewed map edits built from known graph identities and fresh adapter facts."""
from __future__ import annotations

import json
import math
import re

import agents
import ai_core
import link_hints
import connect
import inventory
import llm_engine
import mcp_ctl
import online_models
import provider_watch
import skills_ctl


def _color(value) -> bool:
    """#rrggbb, or one of the theme's colours."""
    return isinstance(value, str) and bool(
        re.fullmatch('#[0-9a-f]{6}', value) or re.fullmatch('theme:(primary|secondary|tertiary|outline)', value))


def valid_layout(value) -> bool:
    if not isinstance(value, dict) or set(value) - {'cards', 'order', 'collapsed', 'pins', 'card_order', 'column_widths', 'column_defaults', 'used_only', 'line_colors', 'card_border', 'dim_lines'}:
        return False
    def ident(v):
        return isinstance(v, str) and 0 < len(v) <= 8192 and not any(ord(c) < 32 for c in v)
    def ids(v, limit):
        return isinstance(v, list) and len(v) <= limit and all(ident(i) for i in v) and len(set(v)) == len(v)
    for key, limit in (('collapsed', 100), ('pins', 100), ('card_order', 100), ('used_only', 100)):
        if key in value and not ids(value[key], limit):
            return False
    border = value.get('card_border', {})
    if not isinstance(border, dict) or set(border) - {'on', 'width', 'color'}:
        return False
    if type(border.get('on', False)) is not bool or type(border.get('width', 1)) is not int \
            or not 1 <= border.get('width', 1) <= 8:
        return False
    if 'color' in border and not _color(border['color']):
        return False
    if type(value.get('dim_lines', True)) is not bool:
        return False
    lines = value.get('line_colors', {})
    if not isinstance(lines, dict) or any(
            kind not in ('provider', 'model', 'serves', 'mcp', 'skills') or not _color(c)
            for kind, c in lines.items()):
        return False
    for key in ('column_widths', 'column_defaults'):
        widths = value.get(key, {})
        if not isinstance(widths, dict) or any(
                name not in ('mcp', 'agents', 'providers', 'models') or type(n) not in (int, float)
                or not math.isfinite(n) or not 200 <= n <= 900 for name, n in widths.items()):
            return False
    for key in ('cards', 'order'):
        mapping = value.get(key, {})
        if not isinstance(mapping, dict) or len(mapping) > 100:
            return False
        for name, data in mapping.items():
            if not ident(name):
                return False
            if key == 'order':
                if not ids(data, 600): return False
            elif (not isinstance(data, list) or len(data) != 2
                  or any(type(n) not in (int, float) or not 0 <= n <= 30000 or not math.isfinite(n) for n in data)):
                return False
    return len(json.dumps(value, ensure_ascii=True)) <= 128 * 1024


def _ids(params, fields):
    if set(params) != set(fields):
        raise ValueError('Unexpected map edit fields')
    for key, value in params.items():
        if key == 'agents':
            if (not isinstance(value, list) or len(value) > 32
                    or any(not isinstance(v, str) or len(v) > 128 for v in value) or len(set(value)) != len(value)):
                raise ValueError('Invalid agent selection')
        elif value is not None and (not isinstance(value, str) or len(value) > 8192):
            raise ValueError('Invalid map identity')


def preview(method: str, params: dict, graph: dict, connections, settings_path=None) -> dict:
    if method not in ('connections.move_preview', 'models.attach_preview', 'models.switch_preview') or not isinstance(params, dict):
        raise ValueError('Invalid map edit')
    fields = {'agent', 'old', 'new'} if method == 'connections.move_preview' else (
        {'provider', 'model', 'agents'} if method == 'models.attach_preview' else {'provider', 'old', 'new'})
    _ids(params, fields)
    nodes = {n['id']: n for n in graph.get('nodes', [])}
    edges = graph.get('edges', [])
    catalog = {a.command: a for a in agents.list_agents() if a.installed}
    inv = inventory.collect(list(catalog.values()))
    runtimes = {f'runtime:{r.key}': r for r in inv.runtimes if r.installed and r.key in connect.KEYS}
    def node(ident, kinds):
        if ident not in nodes or nodes[ident]['kind'] not in kinds:
            raise ValueError('Map item unavailable; refresh')
        return nodes[ident]
    def agent(ident):
        node(ident, ('agent',))
        command = ident.removeprefix('agent:')
        if command not in catalog: raise ValueError('Agent unavailable')
        return command
    def runtime(ident):
        node(ident, ('runtime',))
        if ident not in runtimes: raise ValueError('Runtime unavailable')
        return runtimes[ident]
    def linked(source, target, kinds):
        return any(e['source'] == source and e['target'] == target and e['kind'] in kinds for e in edges)
    builders, actions, focus, notes = [], [], set(), []
    selected = []
    failure_change = None
    if method == 'connections.move_preview':
        aid, old, new = params['agent'], params['old'], params['new']
        command = agent(aid)
        target = node(old, ('runtime', 'mcp', 'skills'))
        failure_path = (settings_path or ai_core.settings_file()).parent / 'failed-links.json'
        failures = {} if failure_path.is_symlink() else link_hints.load(failure_path)
        failure_key = link_hints.key(aid, old)
        failed = failures.get(failure_key)
        if failed:
            # Keep hint removal in the same stale-checked backup and Undo transaction.
            raw = agents.read_capped(failure_path, 1024 * 1024)
            if raw is None: raise ValueError('Failure metadata changed')
            content = json.loads(raw)
            if not isinstance(content, dict): raise ValueError('Invalid failure metadata')
            content.pop(failure_key, None)
            failure_change = connect.Change(failure_path, raw, json.dumps(content, ensure_ascii=False, indent=2) + '\n')
        unapplied = bool(failed and not failed['applied'])
        dangling = next((e.get('dangling') for e in edges if e['source'] == aid and e['target'] == old and e.get('dangling')), '')
        if dangling:
            import agent_details
            import graph as graph_module
            configured, provider = graph_module.split_model(agent_details.details_for(command).model)
            if configured != dangling or provider != connect.KEYS.get(old.partition(':')[2]):
                raise ValueError('Model changed; refresh')
        if not linked(aid, old, ('uses', 'available', 'mcp', 'skills', 'failed')):
            raise ValueError('Connection changed; refresh')
        focus.update((aid, old))
        selected = [command]
        if new is not None:
            # Like the reference editor, only provider lines can move to another provider.
            before, after = runtime(old), runtime(new)
            if before.key == after.key or (not unapplied and not dangling and not connect.is_connected(command, before.key)):
                raise ValueError('Connection unavailable')
            builders.append(lambda: connect.plan(command, after, '', False, replaces=None if unapplied else before))
            focus.add(new)
        elif unapplied:
            pass
        elif target['kind'] == 'runtime':
            before = runtime(old)
            if not dangling and not connect.is_connected(command, before.key): raise ValueError('Connection unavailable')
            builders.append(lambda: connect.plan(command, before, '', False, remove=True))
        elif target['kind'] == 'mcp':
            name = old.removeprefix('mcp:')
            if not mcp_ctl.is_connected(command, name): raise ValueError('Connection unavailable')
            builders.append(lambda: mcp_ctl.plan(command, name, remove=True))
        else:
            folder = old.removeprefix('skills:')
            if not skills_ctl.is_connected(command, skills_ctl.expand(folder)): raise ValueError('Connection unavailable')
            builders.append(lambda: skills_ctl.plan(command, folder, remove=True))
    else:
        pid = params['provider']
        provider = node(pid, ('runtime', 'cloud'))
        old = params.get('old', params.get('model'))
        model_node = node(old, ('model',))
        if not linked(pid, old, ('serves',)): raise ValueError('Model/provider mismatch')
        new = params.get('new') if method == 'models.switch_preview' else old
        if new is not None:
            node(new, ('model',))
            if not linked(pid, new, ('serves',)) or (method == 'models.switch_preview' and new == old):
                raise ValueError('Choose another model of this provider')
        if method == 'models.attach_preview':
            selected = [agent(f'agent:{c}') for c in params['agents']]
        else:
            import agent_details
            import graph as graph_module
            fresh = graph_module.build(list(catalog.values()),
                {c: agent_details.details_for(c) for c in catalog}, inv)
            expected = {e['source'] for e in edges if e['kind'] == 'default' and e['target'] == old}
            actual = {e.source for e in fresh.edges if e.kind == 'default' and e.target == old}
            if expected != actual: raise ValueError('Model users changed; refresh')
            selected = [agent(e['source']) for e in edges if e['target'] == old and e['kind'] == 'default']
            selected = list(dict.fromkeys(selected))
        focus.update((pid, old, *([new] if new else []), *(f'agent:{c}' for c in selected)))
        if provider['kind'] == 'cloud':
            if new is None or not selected: raise ValueError('Online models require agents and another model')
            for command in selected:
                if not linked(f'agent:{command}', pid, ('uses', 'available', 'offer')):
                    raise ValueError('Agent/provider mismatch')
                chosen = online_models.target(command, provider['label'], nodes[new]['label'])
                if chosen is None: raise ValueError('Unsupported online model')
                builders.append(lambda c=command, m=chosen: online_models.plan(c, m))
        else:
            rt = runtime(pid)
            name = nodes[new]['label'] if new else ''
            # A model switched away from may be gone (a missing model's line is repaired this way);
            # the model switched to must be served.
            present = model_node['label'] in rt.models
            if (method != 'models.switch_preview' and not present) or (name and name not in connect.chat_models(rt.models)):
                raise ValueError('Model unavailable')
            for command in selected:
                if command not in connect.SUPPORTED or connect.FIXED.get(command):
                    raise ValueError('Agent cannot switch provider')
                if method == 'models.switch_preview' and not connect.is_active(command, rt.key):
                    raise ValueError('Model users changed; refresh')
                builders.append(lambda c=command: connect.plan(c, rt, name, bool(name), remove=not bool(name)))
            if llm_engine.of(rt).can_load:
                # The worker rechecks these conditions before writing any configuration.
                if method == 'models.switch_preview' and present:
                    _guard_model(rt, selected)
                    actions.append({'operation': 'model.unload', 'key': rt.key, 'model': model_node['label'], 'agents': selected})
                if new is not None:
                    actions.append({'operation': 'model.load', 'key': rt.key, 'model': name, 'agents': []})
                notes.append('Undo restores configuration files; runtime load and unload actions are not undone.')
    if not builders and not actions and failure_change is None: raise ValueError('No editable model users')
    plan = connect.chain(builders) if builders else connect.Plan('', '', '')
    if failure_change is not None: plan.changes.append(failure_change)
    plan.model_actions = actions
    result = connections.keep(plan)
    if actions: result['empty'] = False
    result.update(focus=[n for n in nodes if n in focus], agents=[catalog[c].name for c in selected],
                  notes=notes, model_actions=[{'operation': a['operation'], 'model': a['model']} for a in actions])
    return result


def _guard_model(runtime, commands):
    # Refuse unloading a model while an agent or another known consumer uses the runtime.
    import gpu_usage
    users = list(set(commands) | set(runtime.active_agents))
    if any(provider_watch.agent_instances(users).values()) or gpu_usage.snapshot([runtime]).generating:
        raise ValueError('Model is in use; stop its users first')


def validate_actions(actions):
    import flutter_providers
    if not isinstance(actions, list) or len(actions) > 2: raise ValueError('Invalid model actions')
    resolved = []
    for action in actions:
        if not isinstance(action, dict) or set(action) != {'operation', 'key', 'model', 'agents'}:
            raise ValueError('Invalid model action')
        if action['operation'] not in ('model.load', 'model.unload'): raise ValueError('Invalid model operation')
        commands = action['agents']
        if (not isinstance(commands, list) or len(commands) > 32
                or any(not isinstance(c, str) or not c or len(c) > 128 for c in commands)
                or len(set(commands)) != len(commands)):
            raise ValueError('Invalid model users')
        params = {'key': action['key'], 'model': action['model']}
        flutter_providers.validate(action['operation'], params)
        rt = flutter_providers.runtime(action['key'])
        if action['model'] not in rt.models or not llm_engine.of(rt).can_load: raise ValueError('Model changed')
        if action['operation'] == 'model.unload': _guard_model(rt, action['agents'])
        resolved.append(action)
    return resolved


def repair_preview(params, graph, connections, settings_path=None):
    """Retry a known failed edge and clear its hints in the same backup/Undo."""
    if not isinstance(params, dict): raise ValueError('Invalid repair')
    _ids(params, ('agent', 'target'))
    aid, target = params['agent'], params['target']
    nodes = {n['id']: n for n in graph.get('nodes', [])}
    if nodes.get(aid, {}).get('kind') != 'agent' or nodes.get(target, {}).get('kind') not in ('runtime', 'mcp', 'skills'):
        raise ValueError('Unknown failed connection')
    edge = next((e for e in graph.get('edges', []) if e['source'] == aid and e['target'] == target), None)
    path = (settings_path or ai_core.settings_file()).parent / 'failed-links.json'
    failures = {} if path.is_symlink() else link_hints.load(path)
    entry = failures.get(link_hints.key(aid, target))
    # The map's own broken local line (its LLM moved, its provider gone): no recorded failure, and a
    # reason in fixed when it is not this app's to repair (the agent has no local-LLM planner).
    moved = not entry and bool(edge) and edge.get('kind') == 'failed' and nodes[target]['kind'] == 'runtime'
    if not edge or (not entry and not moved):
        raise ValueError('Failure changed; refresh')
    if moved and edge.get('fixed'):
        raise ValueError(edge['fixed'])
    kind = nodes[target]['kind']
    request = {'kind':'provider' if kind == 'runtime' else kind, 'command':aid.removeprefix('agent:'),
               'item':target.partition(':')[2], 'remove':False}
    if kind == 'runtime':
        # The adapter validates the saved model against fresh chat-model inventory.
        model = edge.get('dangling') or (entry or {}).get('target', '')
        request.update(model=model, use_now=bool(model))
        if edge.get('provider'):          # a moved LLM the agent knows by its own name: that entry is repaired
            request['name'] = edge['provider']
    result = connections.preview(request)
    _, plan = connections.plans.pop(result['plan'])
    if entry:
        raw = agents.read_capped(path, 1024 * 1024)
        if raw is None: raise ValueError('Failure metadata changed')
        data = json.loads(raw)
        if not isinstance(data, dict): raise ValueError('Invalid failure metadata')
        data.pop(link_hints.key(aid, target), None)
        plan.changes.append(connect.Change(path, raw, json.dumps(data, ensure_ascii=False, indent=2) + '\n'))
    public = connections.keep(plan)
    return {**result, **public, 'focus':[aid, target]}
