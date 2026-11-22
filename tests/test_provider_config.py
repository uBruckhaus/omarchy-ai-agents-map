"""Provider setup: deterministic memory math, hostile files, reviewed writes and GTK handlers.

Every configuration and backup in these tests lives in an isolated temporary HOME.
"""
import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import connect
import context_ctl
import inventory
import llm_engine
import provider_config as config
from tests.isolate import desktop_in

G = config.GIB
RULES = [{'effect': 'allow', 'action': '*', 'category': '*', 'item': '*', 'agent': '*'}]
UNIT = '[Unit]\nDescription=llama.cpp (installed by AI Agents Map)\n[Service]\nExecStart=/bin/llama-server --models-preset /models.ini --gpu-layers all --flash-attn on\n'
META = {'general.architecture': 'llama', 'llama.block_count': 32, 'llama.context_length': 262144,
        'llama.embedding_length': 4096, 'llama.attention.head_count': 32, 'llama.attention.head_count_kv': 8}


def hardware(vram=24, ram=64, vendor='amd'):
    return {'gpu': 'Fixture', 'vendor': vendor, 'vram_total': int(vram * G), 'vram_available': int(vram * G),
            'ram_total': int(ram * G), 'ram_available': int(ram * G), 'physical_cores': 8, 'logical_cores': 16,
            'gpu_count': int(vram > 0), 'fp8_supported': False, 'stacks': ['Vulkan'] if vram else []}


def facts(key='llamacpp', vram=24, ram=64, size=4, meta=None):
    fields = config.schema(key)
    defaults = {s['id']: s['default'] for s in fields}
    return {'key': key, 'runtime': key, 'schema': fields, 'hardware': hardware(vram, ram), 'defaults': defaults,
            'models': [{'name': 'm', 'size': int(size * G), 'meta': copy.deepcopy(meta if meta is not None else META), 'writable': True}],
            'settings': {'*': defaults.copy(), 'm': defaults.copy()}, 'scope_models': [] if fields[0]['scope'] == 'global' else ['*', 'm'] if key == 'llamacpp' else ['m'],
            'writable': key not in ('vllm', 'strata'), 'read_only_reason': '', 'restart': fields[0]['restart'],
            'can_control': True, 'can_reload': key in ('lmstudio', 'koboldcpp'), 'running': False,
            'build': 'vulkan' if vram else 'cpu', 'build_mark': '', 'version': '', 'measurements': {}, 'notes': [], 'overrides': {}}


class MathTest(unittest.TestCase):
    def test_all_seven_schemas_and_fallback(self):
        self.assertEqual([len(config.schema(k)) for k in config.PROVIDERS], [9, 5, 6, 7, 6, 5, 1])
        self.assertFalse(config.configurable('openai'))
        fields = config.schema('llamacpp')
        fields[0]['default'] = 42
        self.assertNotEqual(config.schema('llamacpp')[0]['default'], 42)
        old = llm_engine.Engine()
        with mock.patch('providers.get', return_value=SimpleNamespace(engine=old)):
            self.assertTrue(llm_engine.of('jan').config)

    def test_fixed_recommendations(self):
        cases = [
            ('llamacpp', 24, 64, 'balanced', 262144, 'q8_0', 'all', -1),
            ('llamacpp', 8, 64, 'balanced', 36864, 'q8_0', 'all', -1),
            ('llamacpp', 0, 16, 'balanced', 86016, 'f16', 0, 8),
            ('llamacpp', 24, 64, 'long_context', 262144, 'q4_0', 'all', -1),
            ('llamacpp', 24, 64, 'fast', 262144, 'q8_0', 'all', -1),
            ('llamacpp', 24, 64, 'several_agents', 262144, 'q8_0', 'all', -1),
        ]
        for key, gpu, ram, profile, context, cache, layers, threads in cases:
            with self.subTest(gpu=gpu, profile=profile):
                calc = config.ConfigMath(facts(key, gpu, ram))
                result = calc.recommend(profile)
                v = result['values']
                self.assertEqual((v['ctx-size'], v['cache-type-k'], v['gpu-layers'], v['threads']),
                                 (context, cache, layers, threads))
                self.assertFalse(calc.validate(v)['errors'])
                self.assertLessEqual(result['fit']['total'], result['fit']['available'])

    def test_ollama_per_slot_and_loaded_models(self):
        f = facts('ollama')
        calc = config.ConfigMath(f)
        result = calc.recommend('several_agents')
        self.assertEqual(result['values']['OLLAMA_CONTEXT_LENGTH'], 69632)
        self.assertEqual(result['values']['OLLAMA_NUM_PARALLEL'], 4)
        v = {'OLLAMA_CONTEXT_LENGTH': 8192, 'OLLAMA_NUM_PARALLEL': 4}
        self.assertEqual(calc.fit(v)['kv'], 4 * calc.fit({'OLLAMA_CONTEXT_LENGTH': 8192})['kv'])
        f['models'].append({'name': 'bigger', 'size': 8 * G, 'meta': META.copy()})
        one = calc.fit(v)
        two = calc.fit({**v, 'OLLAMA_MAX_LOADED_MODELS': 2})
        self.assertGreater(two['weights'], one['weights'])
        self.assertGreater(two['kv'], one['kv'])
        f['models'] = []
        self.assertEqual(calc.recommend()['values']['OLLAMA_CONTEXT_LENGTH'], 8192)

    def test_total_context_does_not_multiply_cache_by_slots(self):
        for key, context, parallel in [('llamacpp', 'ctx-size', 'parallel'), ('jan', 'ctx_size', 'parallel')]:
            calc = config.ConfigMath(facts(key), 'm')
            self.assertEqual(calc.fit({context: 32768, parallel: 1})['kv'], calc.fit({context: 32768, parallel: 4})['kv'])

    def test_weights_too_large_use_cpu_but_vllm_never_does(self):
        for size in (18, 40):  # 32B and 70B Q4-sized fixtures
            for profile in config.PROFILES:
                calc = config.ConfigMath(facts(size=size, vram=12, ram=64))
                r = calc.recommend(profile)
                self.assertEqual(r['values']['gpu-layers'], 0)
                self.assertEqual(r['fit']['basis'], 'ram')
                self.assertFalse(calc.validate(r['values'])['errors'])
        cpu = config.ConfigMath(facts(vram=0, ram=16, size=40))
        self.assertTrue(cpu.validate(cpu.recommend()['values'])['errors'])
        calc = config.ConfigMath(facts('vllm', vram=8, size=18))
        r = calc.recommend()
        self.assertEqual(r['fit']['basis'], 'vram')
        self.assertTrue(calc.validate(r['values'])['errors'])
        self.assertEqual(r['recommended'][0]['reason'], 'Model too large for this GPU')

    def test_vllm_no_gpu_tensor_float_and_fp8(self):
        calc = config.ConfigMath(facts('vllm', vram=0))
        self.assertTrue(calc.validate(calc.recommend()['values'])['errors'])
        f = facts('vllm', vram=8)
        f['hardware']['fp8_supported'] = True
        calc = config.ConfigMath(f)
        result = calc.recommend()
        self.assertEqual(result['values']['gpu-memory-utilization'], .95)
        self.assertEqual(result['values']['kv-cache-dtype'], 'fp8')
        self.assertEqual(result['values']['max-model-len'], 32768)
        self.assertTrue(calc.validate({'tensor-parallel-size': 2})['errors'])
        for value in (.49, .96, float('nan'), float('inf'), True):
            self.assertTrue(calc.validate({'gpu-memory-utilization': value})['errors'])

    def test_separate_caches_and_sliding_fixed_terms(self):
        meta = copy.deepcopy(META)
        meta.update({'llama.attention.sliding_window': 4096, 'llama.attention.sliding_window_pattern': 4})
        per, fixed = context_ctl.kv_layout(meta, 'q8_0', cache_v='f16')
        self.assertEqual(per, 25088)
        self.assertEqual(fixed, 385351680)
        calc = config.ConfigMath(facts(meta=meta))
        self.assertGreater(calc.fit({})['kv'], fixed)
        result = calc.recommend()
        self.assertLessEqual(result['fit']['total'], result['fit']['available'])

    def test_verified_output_first_partial_offload_and_cpu_budget(self):
        f = facts(vram=8)
        f.update(version='b12078', build_mark='binary')
        calc = config.ConfigMath(f)
        self.assertEqual(calc.fit({'gpu-layers': 1})['kv'], 0)
        self.assertEqual(calc.fit({'gpu-layers': 2})['kv'], 8 * 256 * 2 * 8192)
        f['hardware']['vram_available'] = int(5.8 * G)
        r = calc.recommend()
        self.assertTrue(0 < r['values']['gpu-layers'] < 33)
        self.assertLessEqual(r['fit']['total'], r['fit']['available'])
        f['hardware']['ram_total'] = G
        self.assertTrue(calc.validate({'gpu-layers': 1})['errors'])
        f['hardware']['ram_total'] = 64 * G
        f['hardware']['ram_available'] = 0
        self.assertTrue(calc.validate({'gpu-layers': 1})['warnings'])

    def test_unverified_cache_upper_bound_is_warning_not_false_error(self):
        f = facts(vram=8)
        calc = config.ConfigMath(f)
        check = calc.validate({'gpu-layers': 1, 'ctx-size': 262144})
        self.assertFalse(check['errors'])
        self.assertTrue(any('upper bound' in w['text'] for w in check['warnings']))
        self.assertTrue(calc.validate({'gpu-layers': 'all', 'ctx-size': 262144})['errors'])

    def test_current_free_memory_is_warning_capacity_is_error(self):
        f = facts(vram=24)
        calc = config.ConfigMath(f)
        f['hardware']['vram_available'] = 0
        check = calc.validate({'ctx-size': 8192})
        self.assertFalse(check['errors'])
        self.assertTrue(check['warnings'])
        f['hardware']['vram_total'] = G
        self.assertTrue(calc.validate({'ctx-size': 8192})['errors'])

    def test_measurements_identity_cache_profile_and_reserve(self):
        f = facts()
        f.update(build_mark='same', measurements={'m': {'mark': 'same', 'ctx': 32768, 'ubatch': 1024, 'reserve': G}})
        calc = config.ConfigMath(f)
        result = calc.recommend()
        self.assertEqual(result['values']['ctx-size'], 32768)
        self.assertEqual(result['fit']['reserve'], G)
        self.assertEqual(result['recommended'][0]['source'], 'measured')
        self.assertNotEqual(calc.recommend('several_agents')['values']['ctx-size'], 32768)
        self.assertNotEqual(calc.recommend('long_context')['values']['ctx-size'], 32768)
        del f['measurements']['m']['reserve']
        self.assertEqual(calc.reserve, context_ctl.RESERVE)
        f['measurements']['m']['mark'] = 'stale'
        self.assertFalse(calc.measurement)
        f['measurements']['m']['mark'] = 'same'
        f['measurements']['m']['reason'] = 'skipped'
        self.assertFalse(calc.measurement)

    def test_malformed_metadata_never_enters_memory_math(self):
        for key, value in [('llama.block_count', 10000000), ('llama.block_count', []),
                           ('llama.attention.head_count_kv', -8), ('llama.context_length', float('nan'))]:
            f = facts()
            f['models'][0]['meta'][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                config.ConfigMath(f)

    def test_context_clamp_trained_below_minimum(self):
        f = facts()
        f['models'][0]['meta']['llama.context_length'] = 2048
        self.assertEqual(config.ConfigMath(f).recommend()['values']['ctx-size'], 2048)

    def test_validator_types_ranges_cross_keys_and_specials(self):
        calc = config.ConfigMath(facts())
        for bad in ({'oops': 1}, {'threads': True}, {'parallel': 0}, {'ctx-size': '8192'}, {'gpu-layers': 1000},
                    {'batch-size': 512, 'ubatch-size': 1024}, {'cache-type-v': 'q8_0', 'flash-attn': 'off'},
                    {'threads': 17}, {'parallel': None}, {str(i): 1 for i in range(33)}):
            with self.subTest(bad=bad):
                self.assertTrue(calc.validate(bad)['errors'])
        for good in ({'gpu-layers': 'all'}, {'threads': -1}, {'cache-type-k': 'q8_0', 'flash-attn': 'off'}):
            self.assertFalse(calc.validate(good)['errors'])
        self.assertTrue(calc.validate({'ctx-size': 262145})['warnings'])
        f = facts()
        f['configured'] = {'*': [], 'm': []}
        calc = config.ConfigMath(f)
        self.assertTrue(calc.validate({'ctx-size': 8192})['errors'])
        self.assertFalse(calc.validate({'ctx-size': 8192, 'parallel': 1})['errors'])
        self.assertFalse(config.ConfigMath(f, 'm').validate({'ctx-size': None})['errors'] == [])  # parallel still unknown
        self.assertTrue(config.ConfigMath(facts('strata')).validate({'context': 32768})['errors'])

    def test_rename_and_scope_contract(self):
        self.assertEqual(config.select_model(facts()), '*')
        self.assertEqual(config.select_model(facts('jan')), 'm')
        with self.assertRaises(ValueError):
            config.select_model(facts('jan'), '*')
        with self.assertRaises(ValueError):
            config.select_model(facts('ollama'), 'm')
        root = Path(__file__).resolve().parent.parent
        source = (root / 'ai_popup.py').read_text()
        self.assertIn('self.menu_item(_("Setup"), popover, lambda: self.open_provider_config(rt)', source)


class FileFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.enterContext(mock.patch.dict(os.environ, {'HOME': str(self.home)}))
        desktop_in(self, self.home)
        self.enterContext(mock.patch.object(connect, 'HOME', self.home))
        self.enterContext(mock.patch.object(inventory, 'JAN_ROOT', self.home / '.local/share/Jan'))
        self.enterContext(mock.patch.object(inventory, 'KOBOLD_CONFIGS', self.home / '.config/koboldcpp/models'))
        # Managed workspace maps the immutable container / and /tmp to nobody.
        # Model those two sandbox ancestors as root, preserving real leaf ownership checks.
        original_lstat = os.lstat
        def sandbox_lstat(path, *args, **kwargs):
            info = original_lstat(path, *args, **kwargs)
            if str(path) in ('/', '/tmp') and info.st_uid == 65534:
                parts = list(info)
                parts[4] = 0
                return os.stat_result(parts)
            return info
        self.enterContext(mock.patch('os.lstat', side_effect=sandbox_lstat))
        self.files = config.ConfigFiles(self.home)
        self.rt = inventory.Runtime('llamacpp', 'llama.cpp', True, False, service='llama-server', models=['m', 'other'])
        self.backups = self.home / 'backups'
        self.enterContext(mock.patch.object(connect, 'BACKUP_ROOT', self.backups))

    def write(self, path, text):
        path = self.home / path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def collect(self, key=None):
        if key:
            self.rt.key, self.rt.name = key, key
        return config.collect(self.rt, files=self.files, hardware=hardware(), build={})

    def preview(self, f, values, model='*'):
        return config.preview(self.rt, f, values, model, files=self.files, rules=RULES)

class FilesTest(FileFixture):
    def test_llama_defaults_migration_preview_backup_undo_reload_after_write(self):
        ini = self.write('.config/llama.cpp/models.ini', '# retained\n[*]\nparallel=1\ngpu-layers=0\n[m]\ngpu-layers=3\n[other]\n# other model\ngpu-layers=4\n')
        original = ini.read_text()
        unit = self.write('.config/systemd/user/llama-server.service', UNIT)
        f = self.collect()
        self.assertEqual(f['settings']['*']['gpu-layers'], 'all')
        review = self.preview(f, {'gpu-layers': 8}, 'm')
        self.assertEqual(ini.read_text(), original)
        self.assertEqual(unit.read_text(), UNIT)
        self.assertIn('--gpu-layers all', review.plan.diff())
        after = context_ctl._ini_sections(review.plan.changes[0].new)
        self.assertEqual(after['other']['gpu-layers'], 'all')
        self.assertEqual(after['*']['flash-attn'], 'on')
        self.assertEqual(after['m']['gpu-layers'], '8')
        order = []
        def reload(argv):
            self.assertEqual(argv, ['systemctl', '--user', 'daemon-reload'])
            self.assertNotIn('--gpu-layers', unit.read_text())
            order.append('reload')
        result = config.apply(review, acknowledged=True, backup_root=self.backups, rules=RULES, run=reload)
        self.assertEqual(order, ['reload'])
        self.assertEqual(ini.stat().st_mode & 0o777, 0o600)
        self.assertEqual((result.backup / 'manifest.json').stat().st_mode & 0o777, 0o600)
        def undo_reload(argv):
            self.assertEqual(unit.read_text(), UNIT)
            order.append('undo-reload')
        config.undo(review, result.backup, rules=RULES, run=undo_reload)
        self.assertEqual(ini.read_text(), original)
        self.assertEqual(order, ['reload', 'undo-reload'])

    def test_foreign_unit_unchanged_override_remove_duplicates_and_comments(self):
        original = '# hello\n[*]\nctx-size = 8192\nparallel=1\n[m]\nctx-size = 16384 # own\nctx-size=32768\nother = yes\n[m]\nctx-size=20480 # duplicate\n'
        ini = self.write('.config/llama.cpp/models.ini', original)
        unit = self.write('.config/systemd/user/llama-server.service', '[Service]\nExecStart=/custom --gpu-layers 7\n')
        f = self.collect()
        self.assertTrue(any('flags win' in n for n in f['notes']))
        review = self.preview(f, {'ctx-size': None, 'parallel': 1}, 'm')
        self.assertEqual(len(review.plan.changes), 1)
        new = review.plan.changes[0].new
        self.assertNotIn('ctx-size', new.split('[m]')[1])
        self.assertIn('# own', new)
        self.assertIn('# duplicate', new)
        self.assertIn('other = yes', new)
        self.assertNotIn('unit', str(review.plan.changes[0].path))
        self.assertEqual(unit.read_text(), '[Service]\nExecStart=/custom --gpu-layers 7\n')
        self.assertEqual(ini.read_text(), original)

    def test_new_ini_can_be_created_and_undo_removes_it(self):
        f = self.collect()
        review = self.preview(f, {'ctx-size': 8192, 'parallel': 1})
        result = config.apply(review, acknowledged=True, backup_root=self.backups, rules=RULES)
        path = self.files.paths(self.rt, '*')['main']
        self.assertTrue(path.exists())
        config.undo(review, result.backup, rules=RULES)
        self.assertFalse(path.exists())

    def test_ollama_app_only_and_duplicate_env_lines(self):
        self.rt.service = 'ollama'
        env = self.write('.config/ollama/server.env', '# kept\nOLLAMA_MODELS=/my/models\nexport OLLAMA_CONTEXT_LENGTH=8192 # old\nOLLAMA_CONTEXT_LENGTH=4096\n')
        self.write('.config/systemd/user/ollama.service', UNIT.replace('llama.cpp', 'Ollama'))
        f = self.collect('ollama')
        self.assertTrue(f['writable'])
        review = self.preview(f, {'OLLAMA_CONTEXT_LENGTH': 16384})
        new = review.plan.changes[0].new
        self.assertEqual(new.count('OLLAMA_CONTEXT_LENGTH='), 1)
        self.assertIn('# old', new)
        self.assertIn('OLLAMA_MODELS=/my/models', new)
        result = config.apply(review, acknowledged=True, backup_root=self.backups, rules=RULES)
        config.undo(review, result.backup, rules=RULES)
        self.assertIn('=8192', env.read_text())
        self.write('.config/systemd/user/ollama.service', '[Unit]\nDescription=System\n')
        system = self.collect()
        self.assertFalse(system['writable'])
        with self.assertRaises(ValueError):
            self.preview(system, {'OLLAMA_CONTEXT_LENGTH': 16384})
        self.assertIn('Environment="OLLAMA_CONTEXT_LENGTH=8192"', config.snippet(system, config.ConfigMath(system).effective({})))
        env.unlink()
        with self.assertRaises(ValueError):
            self.files.changes(self.rt, '*', {'OLLAMA_CONTEXT_LENGTH': 16384})

    def test_lms_nested_checked_values_deduplicated_and_created(self):
        self.rt.models = ['m']
        self.write('.lmstudio/.internal/model-index-cache.json', json.dumps({'models': [
            {'defaultIdentifier': 'm', 'indexedModelIdentifier': 'publisher/repo/m'}]}))
        path = self.write('.lmstudio/.internal/user-concrete-model-default-config/publisher/repo/m.json', json.dumps({
            'unknown': {'retain': True}, 'load': {'fields': [{'key': 'other', 'value': 6},
                {'key': 'llm.load.contextLength', 'value': 4096}, {'key': 'llm.load.contextLength', 'value': 8192}]}}))
        original = path.read_text()
        f = self.collect('lmstudio')
        review = self.preview(f, {'contextLength': 16384, 'kCacheQuantizationType': 'q8_0'}, 'm')
        new = json.loads(review.plan.changes[0].new)
        rows = new['load']['fields']
        self.assertEqual(new['unknown'], {'retain': True})
        self.assertEqual(sum(r['key'] == 'llm.load.contextLength' for r in rows), 1)
        self.assertIn({'key': 'llm.load.llama.kCacheQuantizationType', 'value': {'checked': True, 'value': 'q8_0'}}, rows)
        result = config.apply(review, acknowledged=True, backup_root=self.backups, rules=RULES)
        config.undo(review, result.backup, rules=RULES)
        self.assertEqual(path.read_text(), original)
        path.unlink()
        self.assertTrue(self.files.changes(self.rt, 'm', {'gpuOffload': .5}))

    def test_jan_requires_and_changes_both_files_and_keeps_comments(self):
        self.rt.models = ['m']
        yml = self.write('.local/share/Jan/data/llamacpp/models/m/model.yml', 'ctx_size: 8192 # keep\nctx_size: 8192\nmodel_path: m.gguf\nunknown: yes\n')
        preset = self.write('.local/share/Jan/data/llamacpp/router.preset.ini', '[*]\nparallel=1\n[m]\nctx-size = 8192\n')
        f = self.collect('jan')
        review = self.preview(f, {'ctx_size': 16384, 'ngl': 0, 'parallel': 1}, 'm')
        self.assertEqual(len(review.plan.changes), 2)
        self.assertIn('n_gpu_layers: 0', review.plan.changes[0].new)
        self.assertIn('ctx_size: 16384 # keep', review.plan.changes[0].new)
        self.assertEqual(review.plan.changes[0].new.count('ctx_size:'), 1)
        original = yml.read_text(), preset.read_text()
        result = config.apply(review, acknowledged=True, backup_root=self.backups, rules=RULES)
        config.undo(review, result.backup, rules=RULES)
        self.assertEqual((yml.read_text(), preset.read_text()), original)
        preset.unlink()
        with self.assertRaises(ValueError):
            self.files.changes(self.rt, 'm', {'ctx_size': 16384})

    def test_kobold_aliases_and_undo(self):
        self.rt.models = ['m.kcpps']
        path = self.write('.config/koboldcpp/models/m.kcpps', json.dumps({'contextsize': 8192, 'unknown': 42, 'noflashattention': True, 'batchsize': 512}))
        f = self.collect('koboldcpp')
        review = self.preview(f, {'contextsize': 16384, 'flashattention': True, 'blasbatchsize': 1024}, 'm.kcpps')
        after = json.loads(review.plan.changes[0].new)
        self.assertEqual((after['unknown'], after['noflashattention'], after['batchsize']), (42, False, 1024))
        result = config.apply(review, acknowledged=True, backup_root=self.backups, rules=RULES)
        self.assertEqual(json.loads(path.read_text())['contextsize'], 16384)
        config.undo(review, result.backup, rules=RULES)
        self.assertEqual(json.loads(path.read_text())['contextsize'], 8192)
        path.unlink()
        with self.assertRaises(ValueError):
            self.files.changes(self.rt, 'm.kcpps', {'contextsize': 16384})

    def test_hostile_leaf_parent_and_credentials(self):
        target = self.write('target', 'secret')
        path = self.home / '.config/llama.cpp/models.ini'
        path.parent.mkdir(parents=True)
        path.symlink_to(target)
        with self.assertRaises(ValueError):
            config.read_config(path)
        path.unlink()
        path.write_text('x')
        with mock.patch('os.getuid', return_value=os.getuid() + 1):
            with self.assertRaises((ValueError, RuntimeError)):
                config.read_config(path)
        path.chmod(0o666)
        with self.assertRaises(ValueError):
            config.read_config(path)
        path.chmod(0o600)
        path.parent.chmod(0o777)
        with self.assertRaises(RuntimeError):
            config.read_config(path)
        path.parent.chmod(0o700)
        with mock.patch.object(connect, '_read', side_effect=AssertionError('must not read')):
            for name in ('auth.json', '.credentials.json', 'credentials.json'):
                with self.assertRaises(ValueError):
                    config.read_config(self.home / name)

    def test_read_bound_and_invalid_model_names(self):
        path = self.write('big', 'x' * (connect.MAX_BYTES + 1))
        with self.assertRaises(ValueError):
            config.read_config(path)
        for key, name in [('jan', '../bad'), ('koboldcpp', '../m.kcpps'), ('llamacpp', 'bad\nsection')]:
            self.rt.key = key
            with self.assertRaises((ValueError, RuntimeError)):
                self.files.changes(self.rt, name, {config.schema(key)[0]['id']: 8192})
        with self.assertRaises(ValueError):
            config.ini_value('', 'bad]\n[owned', 'ctx-size', 8192)

    def test_stale_plan_ack_and_deny_at_preview_apply_undo(self):
        path = self.write('.config/llama.cpp/models.ini', '[*]\nctx-size=8192\nparallel=1\n')
        f = self.collect()
        review = self.preview(f, {'ctx-size': 16384})
        with self.assertRaises(ValueError):
            config.apply(review, rules=RULES, backup_root=self.backups)
        deny = [dict(RULES[0], effect='deny')]
        with self.assertRaises(PermissionError):
            config.preview(self.rt, f, {'ctx-size': 16384}, files=self.files, rules=deny)
        with self.assertRaises(PermissionError):
            config.apply(review, acknowledged=True, rules=deny, backup_root=self.backups)
        path.write_text('externally changed')
        with self.assertRaises(RuntimeError):
            config.apply(review, acknowledged=True, rules=RULES, backup_root=self.backups)
        self.assertFalse(self.backups.exists())
        path.write_text(review.plan.changes[0].old)
        result = config.apply(review, acknowledged=True, rules=RULES, backup_root=self.backups)
        with self.assertRaises(PermissionError):
            config.undo(review, result.backup, rules=deny)
        path.write_text('changed after applying')
        self.assertEqual(config.undo(review, result.backup, rules=RULES).restored, [])
        self.assertEqual(path.read_text(), 'changed after applying')

    def test_reload_failure_keeps_undo(self):
        self.write('.config/systemd/user/llama-server.service', UNIT)
        self.write('.config/llama.cpp/models.ini', '[*]\nparallel=1\n')
        review = self.preview(self.collect(), {'threads': 4})
        def failed(argv):
            raise RuntimeError('daemon-reload failed')
        result = config.apply(review, acknowledged=True, backup_root=self.backups, rules=RULES, run=failed)
        self.assertTrue(result.backup.is_dir())
        self.assertIn('failed', result.reload_error)
        undone = config.undo(review, result.backup, rules=RULES, run=failed)
        self.assertTrue(undone.restored)
        self.assertIn('failed', undone.reload_error)

    def test_collect_never_runs_a_provider_binary_or_wrapper(self):
        self.write('.config/llama.cpp/models.ini', '[*]\nparallel=1\n')
        with mock.patch('provider_llamacpp.Engine.build_now', side_effect=AssertionError('must not run')), \
                mock.patch('llama_tune.server_binary', return_value=None), \
                mock.patch.object(config.ai_core, 'read_bounded_output', side_effect=AssertionError('must not run')):
            f = config.collect(self.rt, hardware=hardware(), files=self.files)
        self.assertTrue(f['writable'])
        self.assertEqual(f['build'], '')

    def test_context_sync_all_models_preserves_hermes_default(self):
        self.write('.config/llama.cpp/models.ini', '[*]\nctx-size=32768\nparallel=1\n[other]\nctx-size=16384\n')
        hermes = self.write('.hermes/config.yaml', 'model:\n  default: m\n  provider: llama-local\nproviders:\n  llama-local:\n    api: http://127.0.0.1:8080/v1\n    default_model: other\n    models:\n      m:\n        context_length: 32768\n      other:\n        context_length: 16384\n')
        f = self.collect()
        review = self.preview(f, {'ctx-size': 65536, 'parallel': 2})
        change = next(c for c in review.plan.changes if c.path == hermes)
        self.assertIn('default_model: other', change.new)
        self.assertIn('      m:\n        context_length: 32768', change.new)
        self.assertIn('      other:\n        context_length: 8192', change.new)
        self.assertEqual(hermes.read_text(), change.old)
        result = config.apply(review, acknowledged=True, backup_root=self.backups, rules=RULES)
        config.undo(review, result.backup, rules=RULES)
        self.assertEqual(hermes.read_text(), change.old)

    def test_strata_no_preset_read_vllm_metadata_only(self):
        self.rt.models = ['m']
        def no_read(*args, **kwargs):
            raise AssertionError('Strata presets must not be opened')
        with mock.patch.object(config, 'read_config', side_effect=no_read):
            f = self.collect('strata')
            self.assertFalse(f['writable'])
            self.assertEqual(config.snippet(f, {'context': 8192}), 'strata launch')
        self.rt.models_dir = str(self.home / 'hf')
        self.write('hf/config.json', json.dumps({'num_hidden_layers': 32, 'num_attention_heads': 32,
            'num_key_value_heads': 8, 'hidden_size': 4096, 'max_position_embeddings': 131072}))
        self.write('hf/model.safetensors.index.json', json.dumps({'metadata': {'total_size': 4 * G}}))
        f = self.collect('vllm')
        self.assertEqual(f['models'][0]['size'], 4 * G)
        self.assertFalse(f['writable'])
        self.assertIn('vllm serve <model>', config.snippet(f, config.ConfigMath(f).recommend()['values']))


class HardwareTest(FileFixture):
    def test_physical_cores_memavailable_and_amd_own_memory(self):
        self.write('proc/meminfo', 'MemTotal: 16777216 kB\nMemFree: 100 kB\nMemAvailable: 8388608 kB\n')
        for cpu, core in enumerate((0, 0, 1, 1)):
            self.write(f'cpu/cpu{cpu}/topology/physical_package_id', '0\n')
            self.write(f'cpu/cpu{cpu}/topology/core_id', str(core))
        gpu = SimpleNamespace(vendor='amd', name='AMD', count=1, vram=24 * G)
        usage = SimpleNamespace(vram_used=10 * G, vram_total=24 * G,
                                runtimes={'llamacpp': SimpleNamespace(vram=8 * G)})
        with mock.patch('llm_tuning.stacks_here', return_value=['ROCm', 'Vulkan']):
            hw = config.collect_hardware(self.rt, proc=self.home / 'proc', cpu=self.home / 'cpu', gpu=gpu, usage=usage, cards=[])
        self.assertEqual(hw['physical_cores'], 2)
        self.assertEqual(hw['ram_available'], 8 * G)
        self.assertEqual(hw['other_used'], 2 * G)
        self.assertEqual(hw['vram_available'], 22 * G)
        self.assertEqual(hw['stacks'], ['ROCm', 'Vulkan'])

    def test_nvidia_reserve_deducted_once(self):
        self.write('proc/meminfo', 'MemTotal: 67108864 kB\nMemAvailable: 33554432 kB\n')
        gpu = SimpleNamespace(vendor='nvidia', name='NVIDIA', count=1, vram=8 * G)
        with mock.patch('llm_tuning.stacks_here', return_value=['CUDA']):
            hw = config.collect_hardware(self.rt, proc=self.home / 'proc', cpu=self.home / 'missing', gpu=gpu,
                    cards=[{'name': 'NVIDIA', 'total': 8 * G, 'compute_capability': 8.9}])
        self.assertEqual(hw['vram_available'], 8 * G)
        self.assertTrue(hw['fp8_supported'])
        f = facts(vram=8)
        f['hardware'] = hw
        calc = config.ConfigMath(f)
        result = calc.recommend()
        self.assertEqual(result['fit']['reserve'], int(1.5 * G))
        self.assertEqual(result['values']['ctx-size'], 36864)


class ControlTest(unittest.TestCase):
    def setUp(self):
        self.rt = inventory.Runtime('llamacpp', 'llama.cpp', True, True, service='llama-server', models=['m'])

    def test_restart_checks_stop_and_start_before_guard_or_calls(self):
        deny_start = [*RULES, dict(RULES[0], effect='deny', action='start', category='providers', item='llamacpp')]
        with mock.patch('runtime_ctl.stop') as stop, mock.patch('runtime_ctl.start') as start:
            guard = mock.Mock()
            with self.assertRaises(PermissionError):
                config.control(self.rt, 'restart', guard=guard, rules=deny_start)
            guard.assert_not_called()
            stop.assert_not_called()
            start.assert_not_called()
            order = []
            stop.side_effect = lambda rt: (order.append('stop') or True, '')
            start.side_effect = lambda rt: (order.append('start') or True, '')
            config.control(self.rt, 'restart', guard=lambda rt: order.append('guard'), rules=RULES)
            self.assertEqual(order, ['guard', 'stop', 'start'])

    def test_guard_refusal_and_stop_failure_never_start(self):
        with mock.patch('runtime_ctl.stop') as stop, mock.patch('runtime_ctl.start') as start:
            with self.assertRaises(RuntimeError):
                config.control(self.rt, 'restart', guard=mock.Mock(side_effect=RuntimeError('busy')), rules=RULES)
            stop.assert_not_called()
            stop.return_value = False, 'stop failed'
            with self.assertRaises(RuntimeError):
                config.control(self.rt, 'restart', guard=lambda rt: None, rules=RULES)
            start.assert_not_called()

    def test_kobold_reload_uses_admin_api_without_stop(self):
        self.rt.key = 'koboldcpp'
        self.rt.models = ['m.kcpps']
        with mock.patch('provider_koboldcpp.Engine.loaded_models', return_value={'m.kcpps': 'loaded'}), \
                mock.patch('llm_engine._http', return_value={'success': True}) as http, \
                mock.patch('runtime_ctl.stop') as stop, mock.patch('runtime_ctl.load') as load:
            config.control(self.rt, 'reload_model', 'm.kcpps', guard=lambda rt: None, rules=RULES)
            http.assert_called_once_with(self.rt, '/api/admin/reload_config', 'POST', {'filename': 'unload_model'}, timeout=30)
            stop.assert_not_called()
            load.assert_not_called()


class EditorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import gi
        gi.require_version('Gtk', '4.0')
        gi.require_version('Adw', '1')
        import provider_config_page
        cls.module = provider_config_page

    def test_draft_recommend_edit_reset_expiry_and_cancel(self):
        draft = self.module.Draft(facts())
        self.assertFalse(draft.changed)
        draft.use_recommended()
        self.assertTrue(draft.changed)
        self.assertFalse(draft.check()['errors'])
        values = draft.values.copy()
        draft.review = object()
        draft.acknowledged = True
        draft.edit('ctx-size', 16384)
        self.assertIsNone(draft.review)
        self.assertFalse(draft.acknowledged)
        self.assertEqual(draft.values['cache-type-k'], values['cache-type-k'])
        draft.reset()
        self.assertFalse(draft.changed)
        draft.created -= 601
        self.assertTrue(draft.check()['errors'])

    def test_explicit_engine_default_can_be_saved_when_unset(self):
        f = facts()
        f['configured'] = {'*': [], 'm': []}
        draft = self.module.Draft(f)
        draft.edit('parallel', 1)
        self.assertTrue(draft.changed)
        self.assertFalse(draft.check()['errors'])

    def test_debounce_apply_disabled_and_special_parser(self):
        module = self.module
        view = module.ProviderConfigPage()
        view.provider_config_draft = module.Draft(facts())
        view.provider_config_review_box = object()
        view.provider_config_apply_button = mock.Mock()
        view.clear_box = mock.Mock()
        view.provider_config_timer = 17
        with mock.patch.object(module.GLib, 'source_remove') as remove, mock.patch.object(module.GLib, 'timeout_add', return_value=18) as timer:
            field = next(s for s in config.schema('llamacpp') if s['id'] == 'gpu-layers')
            view.provider_config_text(field, 'all')
            self.assertEqual(view.provider_config_draft.values['gpu-layers'], 'all')
            view.provider_config_apply_button.set_sensitive.assert_called_with(False)
            remove.assert_called_once_with(17)
            self.assertEqual(timer.call_args.args[0], 250)

    def test_render_live_validation_and_advice_with_widget_doubles(self):
        # Reuse the established headless GTK widget pattern, without allocating GTK widgets.
        from tests.test_settings_responsive import Widget
        module = self.module
        class Bar(Widget):
            pass
        class Buffer:
            def set_text(self, text):
                self.text = text
        class Text(Widget):
            def __init__(self):
                super().__init__()
                self.buffer = Buffer()
            def get_buffer(self):
                return self.buffer
        view = module.ProviderConfigPage()
        view.provider_config_draft = module.Draft(facts('vllm', vram=8))
        view.provider_config_fit = Widget()
        view.provider_config_fit.set_level = mock.Mock()
        view.provider_config_fit_text = Widget()
        view.provider_config_fit_bar = Bar()
        view.provider_config_messages = Widget()
        view.provider_config_apply_button = Widget()
        view.provider_config_snippet = Text()
        view.provider_config_snippet_card = Widget()
        view.provider_config_draft.use_recommended()
        self.assertFalse(view.provider_config_validate())
        self.assertFalse(view.provider_config_apply_button.get_sensitive())
        self.assertTrue(view.provider_config_snippet_card.get_visible())
        self.assertIn('vllm serve <model>', view.provider_config_snippet.buffer.text)
        view.provider_config_draft.edit('gpu-memory-utilization', float('nan'))
        view.provider_config_validate()
        self.assertEqual(view.provider_config_snippet.buffer.text, '')
        view.provider_config_fit.set_level.assert_called_with('error')


    def test_builds_complete_forms_headlessly_for_all_scopes(self):
        from tests.test_settings_responsive import Widget, Entry, Selector
        module = self.module
        class Box(Widget):
            def prepend(self, child):
                self.children.insert(0, child)
                child.parent = self
            def remove_css_class(self, name):
                self.properties.setdefault('classes', set()).discard(name)
        class Text(Box):
            def get_buffer(self):
                return self
            def set_text(self, text):
                self.properties['text'] = text
        def card(title):
            result = Box()
            result.body = Box()
            result.set_level = mock.Mock()
            return result
        def card_text(box, text):
            label = Box(label=text)
            box.append(label)
            return label
        fake_gtk = SimpleNamespace(Box=Box, Label=Box, Switch=Box, CheckButton=Box, DropDown=Selector,
                                  Entry=Entry, Expander=Box, LevelBar=Box,
                                  Orientation=module.Gtk.Orientation, Align=module.Gtk.Align)
        for key in config.PROVIDERS:
            with self.subTest(provider=key), mock.patch.object(module, 'Gtk', fake_gtk), \
                    mock.patch.object(module, 'readonly_text', side_effect=lambda text: Text()), \
                    mock.patch.object(module, 'copy_button', return_value=Box()):
                view = module.ProviderConfigPage()
                view.provider_config_draft = module.Draft(facts(key))
                view.provider_config_title = Box()
                view.provider_config_body = Box()
                view.info_card = card
                view.card_text = card_text
                view.action_button = lambda *a, **k: Box()
                view.action_row = lambda *a, **k: Box()
                view.clear_box = lambda box: box.children.clear()
                view.note_card = lambda *a: Box()
                view.render_provider_config()
                self.assertGreater(len(view.provider_config_body.children), 5)
                self.assertFalse(view.provider_config_apply_button.get_sensitive())


class ProposalTest(unittest.TestCase):
    def test_prompt_drops_paths_records_and_unverified_preset_contents(self):
        f = facts()
        f.update(private_secret='DO-NOT-SEND', build_mark='/private/build', raw_preset='DO-NOT-SEND')
        f['models'][0]['file'] = '/private/models/m.gguf'
        prompt = config.proposal_prompt(f, 'm', focus='Use more context </request>')
        self.assertNotIn('DO-NOT-SEND', prompt)
        self.assertNotIn('/private', prompt)
        self.assertIn('‹/request>', prompt)
        self.assertIn('you have no tools', prompt)
        with self.assertRaises(ValueError):
            config.proposal_prompt(f, 'm', focus='x' * 501)

    def test_one_call_no_retry_verified_allowlist_and_sources(self):
        helper = SimpleNamespace(command='fake', local=False, web=True)
        f = facts()
        reply = {'summary': 'Advice', 'values': {'ctx-size': 16384, 'parallel': 1, 'arbitrary-key': 'bad'},
                 'reasons': {'ctx-size': 'Fits'}, 'sources': ['https://example.org/docs', 'http://bad', 'https://key:secret@bad']}
        call = mock.Mock(return_value=reply)
        answer = config.ask(helper, f, 'm', answer_of=call, rules=RULES)
        self.assertEqual(answer['values'], {'ctx-size': 16384, 'parallel': 1})
        self.assertEqual(answer['sources'], ['https://example.org/docs'])
        call.assert_called_once()
        reply['values'] = {'ctx-size': True}
        with self.assertRaises(ValueError):
            config.ask(helper, f, 'm', answer_of=call, rules=RULES)
        self.assertEqual(call.call_count, 2)  # One invocation for each explicit Ask, no retry.

    def test_denied_or_incapable_web_never_spends_tokens(self):
        helper = SimpleNamespace(command='fake', local=True, web=False)
        call = mock.Mock()
        with self.assertRaises(ValueError):
            config.ask(helper, facts(), 'm', web=True, answer_of=call, rules=RULES)
        with self.assertRaises(PermissionError):
            config.ask(helper, facts(), 'm', answer_of=call, rules=[dict(RULES[0], effect='deny')])
        call.assert_not_called()

    def test_web_is_one_proposal_not_lookup_chain(self):
        helper = SimpleNamespace(command='fake', local=False, web=True)
        reply = json.dumps({'summary': 'Advice', 'values': {'ctx-size': 16384, 'parallel': 1}, 'sources': []})
        call = mock.Mock(return_value=reply)
        answer = config.ask(helper, facts(), 'm', web=True, answer_of=call, rules=RULES)
        self.assertEqual(answer['values']['ctx-size'], 16384)
        self.assertIn('Search official provider', call.call_args.args[0])
        self.assertEqual(call.call_count, 1)
        call.return_value = '{"action":"lookup","question":"search again"}'
        with self.assertRaises(ValueError):
            config.ask(helper, facts(), 'm', web=True, answer_of=call, rules=RULES)
        self.assertEqual(call.call_count, 2)

    def test_prefill_uses_same_validator_before_it_becomes_a_draft(self):
        from provider_config_page import Draft
        draft = Draft(facts())
        draft.fill_proposal({'summary': 'Configure it', 'values': {'ctx-size': 16384, 'parallel': 1}})
        self.assertEqual(draft.values['ctx-size'], 16384)
        self.assertTrue(draft.from_ai)
        with self.assertRaises(ValueError):
            draft.fill_proposal({'summary': 'Bad', 'values': {'ctx-size': True}})
        self.assertEqual(draft.values['ctx-size'], 16384)

    def test_edit_drops_ai_provenance(self):
        from provider_config_page import Draft
        draft = Draft(facts())
        draft.from_ai = True
        draft.edit('ctx-size', 8192)
        self.assertFalse(draft.from_ai)


class MeasurementTest(FileFixture):
    def test_benchmark_only_writes_numeric_cache_then_recommendation_is_reviewed(self):
        self.write('.config/llama.cpp/models.ini', '[*]\nparallel=1\n')
        f = facts()
        f['build_mark'] = 'same'
        f['models'][0]['file'] = '/fixture/m.gguf'
        result = {'name': 'm', 'ctx': 32768, 'ubatch': 512, 'used': 6 * G, 'cache': 'q8_0'}
        save = mock.Mock()
        with mock.patch.object(config.llm_engine, 'of', wraps=config.llm_engine.of), mock.patch.object(connect, 'apply') as apply:
            records = config.measure(self.rt, f, 'm', run_measure=mock.Mock(return_value=result), save=save, rules=RULES)
        apply.assert_not_called()
        save.assert_called_once()
        record = records['m']
        self.assertEqual(record['mark'], 'same')
        self.assertEqual(record['cache_k'], 'q8_0')
        self.assertGreater(record['reserve'], 0)
        f['measurements'] = records
        self.assertEqual(config.ConfigMath(f).recommend()['values']['ctx-size'], 32768)
        self.assertEqual(self.files.paths(self.rt, '*')['main'].read_text(), '[*]\nparallel=1\n')

    def test_cancel_and_failed_bench_never_save(self):
        import threading
        self.write('.config/llama.cpp/models.ini', '[*]\nparallel=1\n')
        f = facts()
        f['build_mark'] = 'same'
        f['models'][0]['file'] = '/fixture/m.gguf'
        save = mock.Mock()
        with self.assertRaises(RuntimeError):
            config.measure(self.rt, f, 'm', run_measure=lambda *a, **k: {'reason': 'bad model'}, save=save, rules=RULES)
        cancel = threading.Event()
        cancel.set()
        with self.assertRaisesRegex(RuntimeError, 'stopped'):
            config.measure(self.rt, f, 'm', cancel=cancel, run_measure=lambda *a, **k: {'ctx': 32768, 'ubatch': 512}, save=save, rules=RULES)
        save.assert_not_called()



if __name__ == '__main__':
    unittest.main()
