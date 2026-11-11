"""Mounted discovery and reconnect use temporary disks and homes; never live installs."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock
from types import SimpleNamespace

import connect
import existing_ai as disk
import flutter_existing as bridge
import provider_llamacpp
import provider_ollama


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.mount = self.base / 'data'
        self.mount.mkdir()
        self.home = self.base / 'home'
        self.home.mkdir()
        self.mounted = {'path': str(self.mount), 'filesystem': 'ext4', 'identity': '7:8:1'}
        self.patch('existing_ai.mounts', return_value=[self.mounted])
        self.patch('flutter_existing.HOME', new=self.home)
        self.patch('existing_ai.provider_rules', return_value={
            'llamacpp': provider_llamacpp.PROVIDER, 'ollama': provider_ollama.PROVIDER})
        self.settings = self.home / 'settings.json'
        self.backups = self.home / 'backups'
        self.registry = bridge.Existing(self.settings, self.backups)

    def patch(self, name, **kwargs):
        item = patch(name, **kwargs)
        self.addCleanup(item.stop)
        return item.start()

    def binary(self, relative='Ai/llama.cpp-rocm/b123/llama-server'):
        path = self.mount / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'\x7fELFfixture')
        path.chmod(0o755)
        return path

    def models(self, relative='Ai/Models/chat'):
        path = self.mount / relative
        path.mkdir(parents=True, exist_ok=True)
        (path / 'chat.gguf').write_bytes(b'fixture')
        return path

    def scan(self):
        return disk.scan(str(self.mount))

    def records(self):
        return self.registry.register(self.scan())['items']

    def test_mount_parser_unescapes_and_filters_system_network_fuse(self):
        text = '\n'.join([
            '1 0 8:1 / / rw - ext4 /dev/a rw',
            '2 0 8:2 / /mnt/Data\\040Disk rw - ext4 /dev/b rw',
            '3 0 8:3 / /boot/efi rw - vfat /dev/c rw',
            '4 0 0:4 / /mnt/network rw - nfs server:/ rw',
            '5 0 0:5 / /mnt/fuse rw - fuse.sshfs server rw',
            'bad row'])
        # Call the original parser, not setUp's fake table.
        with patch('existing_ai.mounts', new=self._mount_parser):
            parsed = disk.mounts(text)
        self.assertEqual([r['path'] for r in parsed], ['/mnt/Data Disk'])

    # Saved before setUp patches the module function.
    _mount_parser = staticmethod(disk.mounts)

    def test_find_binary_models_store_without_execution(self):
        binary = self.binary()
        self.models()
        self.binary('Ai/ollama/bin/ollama')
        store = self.mount / 'Ai/ollama/models'
        (store / 'manifests').mkdir(parents=True)
        (store / 'blobs').mkdir()
        with patch('subprocess.Popen', side_effect=AssertionError('Discovery executed a program')):
            result = self.scan()
        self.assertFalse(result['limited'])
        self.assertEqual({r['kind'] for r in result['records']}, {'runtime', 'models', 'store'})
        self.assertIn(str(binary), [r['path'] for r in result['records']])

    def test_duplicate_current_link_is_one_program(self):
        binary = self.binary()
        (binary.parent.parent / 'current').symlink_to(binary.parent.name)
        result = self.scan()
        self.assertEqual(len([r for r in result['records'] if r['kind'] == 'runtime']), 1)

    def test_link_outside_mount_and_script_are_not_programs(self):
        outside = self.base / 'outside'
        outside.mkdir()
        binary = outside / 'llama-server'
        binary.write_bytes(b'\x7fELFfixture')
        binary.chmod(0o755)
        folder = self.mount / 'escape'
        folder.mkdir()
        (folder / 'llama-server').symlink_to(binary)
        (self.mount / 'outside-dir').symlink_to(outside, target_is_directory=True)
        script = self.binary('script/llama-server')
        script.write_text('#!/bin/sh\nexit 0\n')
        self.assertEqual(self.scan()['records'], [])

    def test_symlink_model_is_not_counted(self):
        folder = self.mount / 'models'
        folder.mkdir()
        (folder / 'external.gguf').symlink_to(self.base / 'external.gguf')
        (self.base / 'external.gguf').write_bytes(b'fixture')
        self.assertEqual(self.scan()['records'], [])

    def test_depth_entries_and_time_are_bounded(self):
        self.models('a/b/c/d/e/f/g')
        with patch('existing_ai.MAX_DEPTH', 2):
            self.assertEqual(self.scan()['records'], [])
        with patch('existing_ai.MAX_ENTRIES', 2):
            self.assertTrue(self.scan()['limited'])
        with patch('existing_ai.MAX_SECONDS', -1):
            self.assertTrue(self.scan()['limited'])

    def test_non_owned_subtree_is_skipped(self):
        self.models()
        actual = disk.safe
        with patch('existing_ai.safe', side_effect=lambda p, *a, **kw: None if p.name == 'Ai' else actual(p, *a, **kw)):
            self.assertEqual(self.scan()['records'], [])

    def test_outside_root_and_symlink_root_rejected(self):
        with self.assertRaises(ValueError):
            disk.root_for(str(self.home), [self.mounted])
        link = self.mount / 'link'
        link.symlink_to(self.mount)
        with self.assertRaises(ValueError):
            disk.root_for(str(link), [self.mounted])

    def test_rocm_environment_is_detected_without_reading_old_config(self):
        self.binary()
        root = self.mount / 'Ai/rocm-runtime/root'
        (root / 'opt/rocm/lib/llvm/lib').mkdir(parents=True)
        (root / 'usr/lib').mkdir(parents=True)
        row = self.scan()['records'][0]
        self.assertEqual(row['environment']['ROCM_PATH'], str(root / 'opt/rocm'))
        self.assertIn(str(root / 'usr/lib'), row['environment']['LD_LIBRARY_PATH'])

    def test_mount_replacement_and_binary_changes_invalidate(self):
        binary = self.binary()
        row = self.scan()['records'][0]
        binary.write_bytes(b'\x7fELFchanged')
        with self.assertRaises(ValueError):
            disk.validate(row)
        row = self.scan()['records'][0]
        with patch('existing_ai.mounts', return_value=[{**self.mounted, 'identity': 'new'}]):
            with self.assertRaises(ValueError):
                disk.validate(row)

    def runtime_preview(self):
        self.binary()
        self.models()
        records = self.records()
        program = next(r for r in records if r['kind'] == 'runtime')
        models = next(r for r in records if r['kind'] == 'models')
        return self.registry.preview({'found': program['found'], 'models': models['found']})

    def test_preview_has_no_writes_and_generated_wrapper_is_quoted(self):
        plan = self.runtime_preview()
        self.assertEqual(list(self.home.iterdir()), [])
        self.assertEqual(len(plan['files']), 4)
        unit = next(f['text'] for f in plan['files'] if f['path'].endswith('.service'))
        self.assertIn('RequiresMountsFor=', unit)
        self.assertNotIn('enable', unit)
        self.assertNotIn('start ', unit)
        self.assertIn('EnvironmentFile=%h/.config/llama.cpp/server.env', unit)
        text = bridge.wrapper({'path': '/disk/with space/llama-server', 'environment': {}}, ['--models-dir', '/disk/my models'])
        self.assertIn("'/disk/with space/llama-server'", text)
        self.assertIn("'/disk/my models'", text)

    def test_existing_file_or_parent_symlink_is_never_overwritten(self):
        target = self.home / '.local/bin'
        target.mkdir(parents=True)
        (target / 'llama-server').write_text('keep')
        with self.assertRaises(ValueError):
            self.runtime_preview()
        (target / 'llama-server').unlink()
        target.rmdir()
        target.symlink_to(self.base)
        with self.assertRaises(ValueError):
            self.runtime_preview()

    def execute_plan(self, token):
        body = self.registry.plans[token][1]
        from contextlib import nullcontext
        with nullcontext():                 # the plugin has no add-on install lock
            return bridge.execute(json.dumps(body).encode())

    def test_apply_and_undo_runtime_only_changes_new_home(self):
        plan = self.runtime_preview()
        result = self.execute_plan(plan['plan'])
        wrapper = self.home / '.local/bin/llama-server'
        self.assertTrue(os.access(wrapper, os.X_OK))
        self.assertTrue((self.home / '.config/systemd/user/llama-server.service').is_file())
        restored = connect.undo(Path(result['backup']))
        self.assertIn(wrapper, restored)
        self.assertFalse(wrapper.exists())
        self.assertTrue((self.mount / 'Ai/Models/chat/chat.gguf').is_file())

    def test_add_models_preserves_other_settings_and_undo(self):
        models = self.models()
        self.settings.write_text('{"language":"de","unrelated":"keep"}\n')
        before = self.settings.read_text()
        row = self.records()[0]
        plan = self.registry.preview({'found': row['found'], 'models': None})
        self.assertEqual(plan['files'][0]['text'], '')
        result = self.execute_plan(plan['plan'])
        saved = json.loads(self.settings.read_text())
        self.assertEqual(saved['model_folders'], [str(models)])
        self.assertEqual(saved['unrelated'], 'keep')
        connect.undo(Path(result['backup']))
        self.assertEqual(self.settings.read_text(), before)

    def test_changed_settings_refuse_stale_apply(self):
        self.models()
        row = self.records()[0]
        plan = self.registry.preview({'found': row['found'], 'models': None})
        self.settings.write_text('{"language":"fr"}')
        with self.assertRaises(ValueError):
            self.execute_plan(plan['plan'])
        self.assertEqual(json.loads(self.settings.read_text()), {'language': 'fr'})

    def test_apply_rechecks_existing_file_after_preview(self):
        plan = self.runtime_preview()
        target = self.home / '.local/bin/llama-server'
        target.parent.mkdir(parents=True)
        target.write_text('created meanwhile')
        with self.assertRaises(ValueError):
            self.execute_plan(plan['plan'])
        self.assertEqual(target.read_text(), 'created meanwhile')

    @unittest.skip("Maprisma Flutter route; the GTK map gets it in parity phase 2")
    def test_handles_are_opaque_expire_and_failed_queue_preserves_plan(self):
        plan = self.runtime_preview()
        with self.assertRaises(ValueError):
            self.registry.get('/mnt/arbitrary/program')
        with self.assertRaises(RuntimeError):
            self.registry.apply(plan['plan'], Mock(side_effect=RuntimeError()), Mock())
        self.assertIn(plan['plan'], self.registry.plans)
        with patch('flutter_existing.time.monotonic', return_value=10**12):
            with self.assertRaises(ValueError):
                self.registry.apply(plan['plan'], Mock(), Mock())

    @unittest.skip("Maprisma Flutter route; the GTK map gets it in parity phase 2")
    def test_explicit_version_check_has_timeout_clean_env_and_no_output(self):
        self.binary()
        row = self.scan()['records'][0]
        proc = Mock(pid=12345)
        proc.wait.return_value = 0
        with patch('flutter_existing.subprocess.Popen', return_value=proc) as run, patch('flutter_existing.os.killpg'), patch('flutter_existing.os.waitid', return_value=SimpleNamespace(si_code=os.CLD_EXITED, si_status=0)):
            self.assertTrue(bridge.check(row)['ok'])
        args, kwargs = run.call_args
        self.assertEqual(args[0], [row['path'], '--version'])
        self.assertNotIn('ANTHROPIC_API_KEY', kwargs['env'])
        self.assertEqual(kwargs['stdout'], -3)
        proc.wait.assert_called_once_with()

    def test_ollama_store_reconnect_does_not_download_models(self):
        self.binary('Ai/ollama/bin/ollama')
        store = self.mount / 'Ai/ollama/models'
        (store / 'manifests-v2').mkdir(parents=True)
        (store / 'blobs').mkdir()
        rows = self.records()
        program = next(r for r in rows if r['kind'] == 'runtime')
        models = next(r for r in rows if r['kind'] == 'store')
        plan = self.registry.preview({'found': program['found'], 'models': models['found']})
        text = next(f['text'] for f in plan['files'] if f['path'].endswith('/bin/ollama'))
        self.assertIn('OLLAMA_MODELS=', text)
        self.assertIn('serve', text)
        self.assertNotIn('pull', text)

    def test_malformed_settings_are_not_replaced(self):
        self.models()
        row = self.records()[0]
        self.settings.write_text('malformed')
        with self.assertRaises(ValueError):
            self.registry.preview({'found': row['found'], 'models': None})
        self.assertEqual(self.settings.read_text(), 'malformed')

    def test_public_records_hide_private_stamps_and_environment_values(self):
        self.binary()
        row = self.records()[0]
        self.assertNotIn('stamps', row)
        self.assertNotIn('dependencies', row)
        self.assertIsInstance(row['environment'], list)

    def test_existing_model_folder_is_idempotent(self):
        path = self.models()
        self.settings.write_text(json.dumps({'model_folders': [str(path)]}))
        row = self.records()[0]
        plan = self.registry.preview({'found': row['found'], 'models': None})
        self.assertTrue(plan['empty'])

    @unittest.skip("Maprisma Flutter route; the GTK map gets it in parity phase 2")
    def test_check_timeout_kills_child_group_and_restores_signal_handler(self):
        import signal
        import subprocess
        self.binary()
        row = self.scan()['records'][0]
        proc = Mock(pid=12345)
        old = signal.getsignal(signal.SIGTERM)
        with patch('flutter_existing.subprocess.Popen', return_value=proc), patch('flutter_existing.os.killpg') as kill, patch('flutter_existing.time.monotonic', side_effect=[0, 10]):
            self.assertFalse(bridge.check(row)['ok'])
            kill.assert_called_once_with(proc.pid, signal.SIGKILL)
        self.assertEqual(signal.getsignal(signal.SIGTERM), old)

    @unittest.skip("Maprisma Flutter route; the GTK map gets it in parity phase 2")
    def test_backend_accepts_handles_not_frontend_commands(self):
        from flutter_backend import Service
        service = Service(self.settings)
        service.tasks = Mock(items={})
        for method, params in (
            ('existing.mounts', {'path': '/tmp'}),
            ('existing.scan', {'path': str(self.mount), 'argv': ['sh']}),
            ('existing.check', {'found': 'bad', 'argv': ['sh']}),
            ('existing.preview', {'found': 'bad', 'models': None, 'path': '/tmp'}),
            ('existing.apply', {'plan': 'bad', 'command': 'sh'}),
        ):
            with self.subTest(method=method), self.assertRaises(ValueError):
                service.dispatch(method, params)
        service.tasks.start_existing_apply.assert_not_called()

    @unittest.skip("Maprisma Flutter route; the GTK map gets it in parity phase 2")
    def test_backend_scan_queues_worker_and_install_gate_blocks_apply(self):
        from types import SimpleNamespace
        from flutter_backend import Service
        service = Service(self.settings)
        service.tasks = Mock(items={})
        service.tasks.start_existing_scan.return_value = {'task': 'scan'}
        self.assertEqual(service.dispatch('existing.scan', {'path': str(self.mount)}), {'task': 'scan'})
        service.tasks.start_existing_scan.assert_called_once()
        service.tasks.items = {'install': SimpleNamespace(operation='installers.install', state='running')}
        with self.assertRaises(ValueError):
            service.dispatch('existing.apply', {'plan': 'untrusted'})

    def test_inventory_finds_reconnected_runtime_and_models_without_running_it(self):
        import inventory
        plan = self.runtime_preview()
        self.execute_plan(plan['plan'])
        with patch('inventory.HOME', self.home), patch('inventory.shutil.which', return_value=None):
            rt = inventory.llamacpp([])
        self.assertTrue(rt.installed)
        self.assertEqual(rt.models_dir, str(self.mount / 'Ai/Models/chat'))
        self.assertIn('chat', rt.models)

    def test_internal_model_links_and_directory_cycles_are_bounded(self):
        models = self.models('models/chat')
        router = self.mount / 'Ai/router'
        router.mkdir(parents=True)
        (router / 'chat.gguf').symlink_to(models / 'chat.gguf')
        (router / 'cycle').symlink_to(self.mount)
        (self.mount / 'Models').symlink_to(self.mount / 'models')
        result = self.scan()
        model_rows = [r for r in result['records'] if r['kind'] == 'models']
        self.assertEqual(len(model_rows), 2)
        self.assertLess(result['visited'], 30)
        for row in model_rows:
            disk.validate(row)
        (models / 'chat.gguf').write_bytes(b'changed')
        for row in model_rows:
            with self.assertRaises(ValueError):
                disk.validate(row)

    def test_bundled_provider_engine_is_not_a_standalone_install(self):
        self.binary('Ai/ollama/v1/lib/ollama/llama-server')
        self.binary('Ai/ollama/v1/bin/ollama')
        runtime_rows = [r for r in self.scan()['records'] if r['kind'] == 'runtime']
        self.assertEqual([r['provider'] for r in runtime_rows], ['ollama'])

    @unittest.skip("Maprisma Flutter route; the GTK map gets it in parity phase 2")
    def test_provider_snapshot_lists_saved_shared_folders(self):
        import flutter_providers
        path = self.models()
        self.settings.write_text(json.dumps({'model_folders': [str(path)]}))
        with patch('flutter_providers.providers.local', return_value=[]), patch('flutter_providers.providers.online', return_value=[]), patch('inventory.own_processes', return_value=[]):
            result = flutter_providers.snapshot(self.settings)
        self.assertEqual(result['folders'], [{'path': str(path), 'available': True}])

    def linked_runtime(self, provider='llamacpp'):
        if provider == 'llamacpp':
            binary = self.binary('Ai/runtime/v1/llama-server')
        else:
            binary = self.binary('Ai/ollama/v1/bin/ollama')
        base = self.mount / ('Ai/runtime' if provider == 'llamacpp' else 'Ai/ollama')
        (base / 'current').symlink_to('v1')
        rows = self.scan()['records']
        return next(r for r in rows if r['kind'] == 'runtime' and r['provider'] == provider), base, binary

    def test_launch_alias_is_preserved_for_both_providers(self):
        for provider in ('llamacpp', 'ollama'):
            row, base, binary = self.linked_runtime(provider)
            self.assertEqual(row['path'], str(binary))
            expected = base / 'current' / ('llama-server' if provider == 'llamacpp' else 'bin/ollama')
            self.assertEqual(row['launch_path'], str(expected))
            self.assertIn(str(base / 'current'), row['links'])
            disk.validate(row)
            self.assertIn(str(expected), bridge.wrapper(row, []))

    def test_retargeted_current_invalidates_the_reviewed_plan(self):
        row, base, binary = self.linked_runtime()
        folder = self.models()
        models = next(r for r in self.scan()['records'] if r['kind'] == 'models')
        self.registry.register({'records': [row, models], 'limited': False, 'visited': 1})
        tokens = list(self.registry.records)
        plan = self.registry.preview({'found': tokens[0], 'models': tokens[1]})
        self.binary('Ai/runtime/v2/llama-server')
        (base / 'current').unlink()
        (base / 'current').symlink_to('v2')
        with self.assertRaises(ValueError):
            self.execute_plan(plan['plan'])
        self.assertEqual(list(self.home.iterdir()), [])

    def test_created_wrapper_follows_later_current_updates(self):
        row, base, binary = self.linked_runtime()
        self.models()
        models = next(r for r in self.scan()['records'] if r['kind'] == 'models')
        plan = bridge.build_plan(row, models, self.settings)
        text = next(data.decode() for path, data, _ in plan.files_add if path.name == 'llama-server')
        self.assertIn(str(base / 'current/llama-server'), text)
        self.assertNotIn(str(binary), text)
        new = self.binary('Ai/runtime/v2/llama-server')
        (base / 'current').unlink()
        (base / 'current').symlink_to('v2')
        binary.unlink()
        self.assertEqual((base / 'current/llama-server').resolve(), new)

    def test_same_target_recreated_link_and_outside_alias_are_rejected(self):
        row, base, _ = self.linked_runtime()
        (base / 'current').unlink()
        (base / 'current').symlink_to('v1')
        with self.assertRaises(ValueError):
            disk.validate(row)
        outside = self.base / 'outside'
        outside.symlink_to(base / 'v1', target_is_directory=True)
        (base / 'current').unlink()
        (base / 'current').symlink_to(outside)
        self.assertIsNone(disk.program(base / 'current/llama-server', self.mount, self.mount.stat().st_dev))

    def test_indirect_alias_links_are_tracked_and_cannot_leave_the_mount(self):
        row, base, _ = self.linked_runtime()
        (base / 'current').unlink()
        (base / 'active').symlink_to('v1')
        (base / 'current').symlink_to('active')
        row = next(r for r in self.scan()['records'] if r['kind'] == 'runtime')
        self.assertIn(str(base / 'active'), row['links'])
        disk.validate(row)
        outside = self.base / 'back'
        outside.symlink_to(base / 'v1')
        (base / 'active').unlink()
        (base / 'active').symlink_to(outside)
        self.assertIsNone(disk.program(base / 'current/llama-server', self.mount, self.mount.stat().st_dev))
        with self.assertRaises(ValueError):
            disk.validate(row)

    def test_named_home_relative_units_and_new_preset(self):
        plan = self.runtime_preview()
        unit = next(f['text'] for f in plan['files'] if f['path'].endswith('.service'))
        self.assertIn('Description=AI Agents Map connected local AI: llama.cpp', unit)
        self.assertIn('ExecStart="%h/.local/bin/llama-server"', unit)
        self.assertNotIn(str(self.home), unit)
        preset = next(f['text'] for f in plan['files'] if f['path'].endswith('models.ini'))
        self.assertEqual(preset, '[*]\ngpu-layers = all\n')
        wrapper = next(f['text'] for f in plan['files'] if f['path'].endswith('/llama-server'))
        self.assertIn('--models-preset "$HOME"/.config/llama.cpp/models.ini', wrapper)
        self.assertTrue(plan['notes'])

    def test_specific_scan_limit_and_foreign_owner_notices(self):
        self.models('a/b/c/d')
        with patch('existing_ai.MAX_DEPTH', 1):
            result = self.scan()
        self.assertEqual(result['limits'], ['depth'])
        self.assertEqual(result['depth'], 1)
        self.binary('llama-server')
        original_uid = os.getuid()
        with patch('existing_ai.os.getuid', return_value=original_uid + 1):
            result = self.scan()
        self.assertIn('ownership', result['notices'])
        self.assertIn(str(self.mount / 'llama-server'), result['foreign'])
        self.assertEqual(result['records'], [])


if __name__ == '__main__':
    unittest.main()
