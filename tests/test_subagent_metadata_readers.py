import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

import subagent_readers as readers

NOW = 1800000000.0


class SubagentMetadataTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        readers._CACHE.clear()

    def rollout(self, name, *, source=True, events=()):
        path = self.root / f'{name}.jsonl'
        meta = {'id': name, 'cwd': '/project', 'parent_thread_id': 'parent-id',
                'agent_path': '/root/research', 'agent_role': 'worker'}
        if source:
            meta['source'] = {'subagent': {'thread_spawn': {'parent_thread_id': 'parent-id'}}}
        else:
            meta['source'] = 'cli'
        rows = [{'type': 'session_meta', 'payload': meta}]
        rows += [{'type': 'event_msg', 'payload': {'type': event}} for event in events]
        path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
        os.utime(path, (NOW - 2, NOW - 2))
        return path

    def collect(self):
        return readers.collect('codex-rollouts', 'test-agent', NOW, self.root, 1800)

    def test_delegated_origin_is_required_not_a_conversation_fork_parent(self):
        self.rollout('delegated')
        self.rollout('ordinary-fork', source=False)
        rows = self.collect()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].key, 'test-agent:delegated')
        self.assertIn('/root/research', rows[0].kind)
        self.assertTrue(rows[0].running)

    def test_completed_child_can_be_reactivated_by_another_task(self):
        path = self.rollout('child', events=['task_started', 'task_complete'])
        self.assertFalse(self.collect()[0].running)
        with path.open('a') as handle:
            handle.write(json.dumps({'type': 'event_msg', 'payload': {'type': 'task_started'}}) + '\n')
        os.utime(path, (NOW - 1, NOW - 1))
        self.assertTrue(self.collect()[0].running)

    def test_metadata_is_cached_but_duplicate_rollouts_are_not_double_counted(self):
        path = self.rollout('child')
        (self.root / 'copy.jsonl').write_bytes(path.read_bytes())
        os.utime(self.root / 'copy.jsonl', (NOW - 2, NOW - 2))
        self.assertEqual(len(self.collect()), 1)
        with mock.patch.object(readers, 'read', side_effect=AssertionError('unchanged file reread')):
            self.assertEqual(len(self.collect()), 1)

    def test_symlinked_files_and_roots_are_not_followed(self):
        path = self.rollout('child')
        link = self.root / 'alias.jsonl'
        link.symlink_to(path)
        self.assertEqual(len(self.collect()), 1)
        alias = self.root.parent / (self.root.name + '-alias')
        alias.symlink_to(self.root)
        try:
            self.assertEqual(readers.collect('codex-rollouts', 'test-agent', NOW, alias, 1800), [])
        finally:
            alias.unlink()

    def test_completed_lifecycle_in_large_copilot_tail_is_not_lost(self):
        folder = self.root / 'session'
        folder.mkdir()
        path = folder / 'events.jsonl'
        events = [
            {'type': 'session.start', 'data': {'context': {'cwd': '/project'}}},
            {'type': 'subagent.started', 'timestamp': NOW - 5,
             'data': {'toolCallId': 'call-one', 'agentName': 'research', 'prompt': 'private request'}},
        ]
        content = ''.join(json.dumps(event) + '\n' for event in events)
        content += json.dumps({'type': 'assistant.message', 'data': {'content': 'x' * 5000}}) + '\n'
        content += json.dumps({'type': 'subagent.completed', 'timestamp': NOW - 1,
                               'data': {'toolCallId': 'call-one'}}) + '\n'
        path.write_text(content)
        os.utime(path, (NOW - 1, NOW - 1))
        with mock.patch.object(readers, 'MAX_EVENTS', 1024), mock.patch.object(readers, 'MAX_HEAD', 512):
            rows = readers.collect('copilot-events', 'test-agent', NOW, self.root, 1800)
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0].running)
        self.assertEqual(rows[0].kind, 'research')
        self.assertNotIn('private request', repr(rows))

    def test_goose_reader_requires_explicit_subagent_type_and_parent(self):
        path = self.root / 'sessions.db'
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE sessions (id TEXT, session_type TEXT, name TEXT, working_dir TEXT, updated_at REAL, parent_session_id TEXT)')
            db.executemany('INSERT INTO sessions VALUES (?,?,?,?,?,?)', [
                ('child', 'sub_agent', 'private task title', '/project', NOW - 1, 'parent'),
                ('fork', 'user', 'not delegated', '/project', NOW - 1, 'parent'),
                ('root', 'sub_agent', 'not attached', '/project', NOW - 1, None),
            ])
        rows = readers.collect('goose-db', 'test-agent', NOW, path, 1800)
        self.assertEqual([row.key for row in rows], ['test-agent:child'])
        self.assertEqual(rows[0].task, '')

    def test_copilot_appended_events_preserve_cwd_without_rereading_old_bytes(self):
        folder = self.root / 'session'
        folder.mkdir()
        path = folder / 'events.jsonl'
        path.write_text(json.dumps({'type':'session.start','data':{'context':{'cwd':'/project'}}}) + '\n')
        os.utime(path, (NOW-2, NOW-2))
        self.assertEqual(readers.collect('copilot-events','fixture',NOW,self.root,1800), [])
        with path.open('a') as handle:
            handle.write(json.dumps({'type':'subagent.started','timestamp':NOW-1,
                'data':{'toolCallId':'child','agentName':'worker'}}) + '\n')
        os.utime(path, (NOW-1,NOW-1))
        with mock.patch.object(readers, 'read', side_effect=AssertionError('old bytes reread')):
            rows = readers.collect('copilot-events','fixture',NOW,self.root,1800)
        self.assertEqual(rows[0].folder, '/project')
        self.assertTrue(rows[0].running)
