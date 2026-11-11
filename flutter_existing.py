"""Private discovery handles and confirmed reconnect jobs for mounted AI installations."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import signal
import stat
import subprocess
import tempfile
import time
import uuid

import ai_core
import connect
import existing_ai

HOME = Path.home()
MAX_BYTES = 256 * 1024
TTL = 600


def wrapper(record: dict, default_args: list[str], home_arguments: tuple = ()) -> str:
    lines = ['#!/bin/sh', '# Connected by AI Agents Map', 'set -eu']
    for name, value in record['environment'].items():
        lines.append('export ' + name + '=' + shlex.quote(value))
    launch = record.get('launch_path', record['path'])
    binary = shlex.quote(launch)
    lines += ['if [ "$#" -gt 0 ]; then exec ' + binary + ' "$@"; fi',
              'exec ' + shlex.join([launch, *default_args]) + ''.join(' \"$HOME\"/' + shlex.quote(path) for path in home_arguments)]
    return '\n'.join(lines) + '\n'


def unit_arg(value: str, *, executable=False, home=False) -> str:
    if any(ord(c) < 32 for c in value):
        raise ValueError('Control characters in a path are not supported')
    value = value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%')
    if executable:
        value = value.replace('$', '$$')
    if home:
        value = '%h/' + value
    return '"' + value + '"'


def service_files(record: dict, command: str, text: str, mounts: list[str], extra: list) -> list:
    unit = ('[Unit]\nDescription=AI Agents Map connected local AI: ' + record['name'] + '\nRequiresMountsFor=' +
            ' '.join(unit_arg(m) for m in dict.fromkeys(mounts)) + '\n\n[Service]\nType=simple\nExecStart=' +
            unit_arg('.local/bin/' + command, executable=True, home=True) + '\nEnvironmentFile=' +
            '%h/' + str(extra[0][0]) + '\nRestart=on-failure\nRestartSec=5\n\n'
            '[Install]\nWantedBy=default.target\n')
    files = [(HOME / '.local/bin' / command, text.encode(), True),
             (HOME / '.config/systemd/user' / (command + '.service'), unit.encode(), False)]
    files += [(HOME / path, content.encode(), False) for path, content in extra]
    for path, _data, _executable in files:
        if not path.is_relative_to(HOME) or any(p.is_symlink() for p in [path, *path.parents] if p.is_relative_to(HOME)):
            raise ValueError('A connecting path is a symbolic link')
        if os.path.lexists(path):
            raise ValueError('A connecting file already exists: ' + str(path))
    return files


def build_plan(record: dict, models: dict | None, settings_path: Path | None) -> connect.Plan:
    existing_ai.validate(record)
    if models is not None:
        existing_ai.validate(models)
    plan = connect.Plan('existing-ai', '', '')
    if record['kind'] == 'models':
        path = settings_path or ai_core.settings_file()
        if any(p.is_symlink() for p in [path, *path.parents]):
            raise ValueError('Settings path is a symbolic link')
        if path.exists() and (not stat.S_ISREG(path.stat().st_mode) or path.stat().st_size > ai_core.SETTINGS_MAX_BYTES):
            raise ValueError('Invalid settings file')
        before = connect._read(path)
        saved = json.loads(before) if before.strip() else {}
        if not isinstance(saved, dict):
            raise ValueError('Invalid settings file')
        folders = saved.get('model_folders', [])
        if not isinstance(folders, list) or any(not isinstance(p, str) for p in folders):
            raise ValueError('Invalid saved model folders')
        if record['path'] not in folders:
            saved['model_folders'] = [*folders, record['path']]
            plan.changes = [connect.Change(path, before, json.dumps(saved, ensure_ascii=False, indent=2) + '\n')]
    elif record['kind'] == 'runtime':
        if any(c in p for p in [record['path'], record.get('launch_path', record['path']), models['path'] if models else ''] for c in ('\\', '"', '\n', '\r')):
            raise ValueError('A runtime path contains unsupported quoting characters')
        rule = existing_ai.provider_rules().get(record['provider'])
        if rule is None or not rule.reconnect_existing:
            raise ValueError('Provider cannot reconnect this installation')
        plan.files_add = rule.reconnect_existing(record, models)
        plan.notes = record.get('notes', [])
    else:
        raise ValueError('Connect the model store through its runtime')
    return plan


def plan_signature(plan: connect.Plan) -> dict:
    return {'changes': [[str(c.path), c.old, c.new] for c in plan.changes],
            'files': [[str(p), d.decode(), x] for p, d, x in plan.files_add]}


def execute(body: bytes) -> dict:
    if len(body) > MAX_BYTES:
        raise ValueError('Reconnect plan too large')
    data = json.loads(body)
    path = Path(data['settings_path']) if data['settings_path'] else None
    plan = build_plan(data['record'], data['models'], path)
    if plan_signature(plan) != data['signature']:
        raise ValueError('Reconnect preview changed')
    # The existing app-wide lock also protects detached installs from reconnect writes.
    try:
        from flutter_addon_catalog import install_lock
    except ImportError:                 # the AI Agents Map plugin: no add-ons to install meanwhile
        from contextlib import nullcontext as install_lock
    with install_lock():
        plan = build_plan(data['record'], data['models'], path)
        if plan_signature(plan) != data['signature']:
            raise ValueError('Reconnect preview changed')
        backup = connect.apply(plan, backup_root=Path(data['backup_root']))
    # Do not enable, start, or execute the discovered program here.
    return {'backup': str(backup)}


def check(record: dict, *, validator=None) -> dict:
    (validator or existing_ai.validate)(record)
    if record['kind'] != 'runtime':
        raise ValueError('Choose a program')
    with tempfile.TemporaryDirectory(prefix='ai-agents-map-check-') as folder:
        env = {'PATH': '/usr/bin:/bin', 'HOME': folder, 'TMPDIR': folder, 'LANG': 'C',
               **record['environment']}
        previous = signal.getsignal(signal.SIGTERM)
        def interrupted(_signal, _frame):
            raise InterruptedError('Program check stopped')
        signal.signal(signal.SIGTERM, interrupted)
        process = None
        try:
            process = subprocess.Popen([record.get('launch_path', record['path']), '--version'], env=env, cwd=folder,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, start_new_session=True)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                exited = os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
                if exited is not None:
                    return {'ok': exited.si_code == os.CLD_EXITED and exited.si_status == 0, 'checked': True}
                time.sleep(0.02)
            return {'ok': False, 'checked': True}
        finally:
            if process is not None:
                # The leader remains unreaped until after signalling: its PID/PGID
                # cannot have been recycled into an unrelated process group.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
            signal.signal(signal.SIGTERM, previous)


class Existing:
    def __init__(self, settings_path=None, backup_root=None):
        self.settings_path = settings_path
        self.backup_root = backup_root or connect.BACKUP_ROOT
        self.records: dict[str, tuple[float, dict]] = {}
        self.plans: dict[str, tuple[float, dict]] = {}

    def register(self, result: dict) -> dict:
        self.records = {}  # A new scan invalidates old selections, not already reviewed plans.
        public = []
        for record in result['records'][:existing_ai.MAX_RESULTS]:
            ident = uuid.uuid4().hex
            self.records[ident] = (time.monotonic(), record)
            public.append({key: record[key] for key in ('kind', 'provider', 'name', 'path', 'count')} |
                          {'found': ident, 'environment': sorted(record['environment']), 'checked': False})
        return {'items': public, 'limited': bool(result['limited']), 'visited': result['visited'],
                **{key: result.get(key, [] if key != 'depth' else existing_ai.MAX_DEPTH)
                   for key in ('limits', 'depth', 'notices', 'foreign')}}

    def get(self, ident):
        if not isinstance(ident, str) or ident not in self.records:
            raise ValueError('Unknown discovery handle')
        created, record = self.records[ident]
        if time.monotonic() - created >= TTL:
            raise ValueError('Discovery expired; scan again')
        return record

    def preview(self, params: dict) -> dict:
        if set(params) != {'found', 'models'} or (params['models'] is not None and not isinstance(params['models'], str)):
            raise ValueError('Invalid reconnect selection')
        record = self.get(params['found'])
        models = self.get(params['models']) if params['models'] else None
        plan = build_plan(record, models, self.settings_path)
        body = {'record': record, 'models': models, 'settings_path': str(self.settings_path) if self.settings_path else None,
                'backup_root': str(self.backup_root), 'signature': plan_signature(plan)}
        if len(json.dumps(body).encode()) > MAX_BYTES:
            raise ValueError('Reconnect plan too large')
        now = time.monotonic()
        self.plans = {k: v for k, v in self.plans.items() if now - v[0] < TTL}
        while len(self.plans) >= 8:
            self.plans.pop(next(iter(self.plans)))
        token = uuid.uuid4().hex
        self.plans[token] = (now, body)
        # Only generated connecting files are shown; existing settings values remain private.
        return {'plan': token, 'empty': plan.empty(), 'notes': plan.notes,
                'files': [{'path': str(p), 'text': data.decode()} for p, data, _x in plan.files_add] +
                         [{'path': str(c.path), 'text': ''} for c in plan.changes]}

    def apply(self, token, queue, finalize):
        if not isinstance(token, str) or token not in self.plans:
            raise ValueError('Unknown reconnect plan')
        created, body = self.plans[token]
        if time.monotonic() - created >= TTL:
            raise ValueError('Reconnect preview expired')
        result = queue(body, finalize)
        del self.plans[token]
        return result
