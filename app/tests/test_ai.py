#!/usr/bin/env python3
"""Any AI model the person chooses: reading a model file's header, finding models, running one, choosing between them,
and bringing one from Hugging Face.

  PYTHONDONTWRITEBYTECODE=1 <venv python> -B app/tests/test_ai.py [-v]

No model is run and nothing is downloaded: model files are headers written by aikit.py, llama-server is a stand-in
script, and Hugging Face's answers are made up here. Uses the made-up library in derived/app-build/testdata. Never packaged."""
import json, os, shutil, signal, subprocess, sys, tempfile, threading, time, unittest
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'app'), str(HERE)]
import fixture  # noqa: E402
DATA = fixture.ensure()
os.environ['LASTBOOK_DATA'] = str(DATA)
os.environ['SURVIVAL_MODEL_BASE'] = 'http://127.0.0.1:9'
os.environ['SURVIVAL_DISABLE_INDEX_WATCH'] = '1'
sys.modules['onnxruntime'] = None   # this Mac runs no AI models
from engine.ai import gguf, models, picker, runtime  # noqa: E402
from engine.setup import download  # noqa: E402
from engine.wizard import hf_import  # noqa: E402
import aikit  # noqa: E402
import assistant  # noqa: E402
import server  # noqa: E402
from test_portable import ask  # noqa: E402

from engine.setup import hardware as _hardware  # noqa: E402
FREE_NOW = _hardware.available_ram      # the real one: tests replace it so that what is free today decides nothing
GiB = 2 ** 30
POSIX = os.name != 'nt'


class Temp(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='lb-ai-test-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.data = self.tmp / 'LastbookData'
        (self.data / 'models').mkdir(parents=True)
        from engine.setup import hardware
        for p in (mock.patch.object(models, 'SMALL', 0),                            # test models are a header and a hole
                  mock.patch.object(hardware, 'available_ram', return_value=None)):   # and what this computer has free today is not part of a test
            p.start()
            self.addCleanup(p.stop)

    def nowhere_else(self):
        """No other program's models: whatever this computer really holds is not part of a test."""
        p = mock.patch.object(models, 'places', return_value=[])
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.dict(os.environ, {'OLLAMA_MODELS': str(self.tmp / 'no-ollama')})
        p.start()
        self.addCleanup(p.stop)


class Header(unittest.TestCase):
    def test_a_models_shape_is_in_its_first_kilobytes(self):
        meta = aikit.plain(name='Tiny Talker', template='{{ messages }} {% if tools %}tools{% endif %}')
        whole = aikit.gguf_bytes(meta)
        r = gguf.read(whole)
        self.assertTrue(r['complete'])
        self.assertEqual((r['version'], r['tensors'], r['keys']), (3, 3, len(meta)))
        self.assertEqual(r['meta']['general.name'], 'Tiny Talker')
        self.assertEqual(r['meta']['tokenizer.ggml.tokens']['array'], 3000)
        self.assertEqual(len(r['meta']['tokenizer.ggml.tokens']['first']), gguf.KEEP_ARRAY)
        d = gguf.describe(r['meta'], 5 * GiB, 'tiny-talker-Q4_K_M.gguf')
        self.assertEqual((d['name'], d['arch'], d['size_label'], d['quant'], d['kind'], d['tools']), ('Tiny Talker', 'testarch', '3B', 'Q4_K_M', 'model', True))
        short = gguf.read(whole[:600])          # the read stops in the middle of the word list, and says so
        self.assertFalse(short['complete'])
        self.assertEqual(gguf.numbers(short['meta'])['layers'], 24)
        self.assertIsNone(gguf.describe(short['meta'])['tools'])        # the template was not reached: not known, rather than no
        for cut in range(24, 400, 7):
            gguf.read(whole[:cut])              # wherever the bytes end, no error

    def test_what_is_not_a_model_file_is_refused(self):
        for bad in (b'', b'GGUF', b'PK\x03\x04' + bytes(100), b'GGUF' + bytes(40), b'GGUF' + (9).to_bytes(4, 'little') + bytes(40)):
            with self.assertRaises(gguf.NotGGUF):
                gguf.read(bad)
        huge = b'GGUF' + (3).to_bytes(4, 'little') + (1).to_bytes(8, 'little') + (10 ** 9).to_bytes(8, 'little')
        with self.assertRaises(gguf.NotGGUF):
            gguf.read(huge)

    def test_kinds_and_quantisations_from_names(self):
        self.assertEqual(gguf.kind_of({}, 'mmproj-Qwen3VL-4B-Instruct-F16.gguf'), 'projector')
        self.assertEqual(gguf.kind_of({'general.architecture': 'clip'}, 'vision.gguf'), 'projector')
        self.assertEqual(gguf.kind_of({}, 'model.mmproj-f16.gguf'), 'projector')
        self.assertEqual(gguf.kind_of({}, 'mtp-gemma-4-12B-it.gguf'), 'draft')
        self.assertEqual(gguf.kind_of({}, 'gemma.imatrix.gguf'), 'other')
        self.assertEqual(gguf.kind_of({}, 'gemma-4-12b-it-Q4_K_M.gguf'), 'model')
        for name, want in (('gemma-4-12b-it-Q4_K_M.gguf', 'Q4_K_M'), ('x.Q8_0.gguf', 'Q8_0'), ('gemma-4-26B-A4B-it-UD-Q4_K_XL.gguf', 'UD-Q4_K_XL'),
                           ('Qwen3VL-4B-Instruct-F16.gguf', 'F16'), ('model-IQ4_XS.gguf', 'IQ4_XS'), ('m-bf16.gguf', 'BF16'), ('plain.gguf', None)):
            self.assertEqual(gguf.quant_of({}, name), want, name)
        self.assertEqual(gguf.quant_of({'general.file_type': 15}, 'plain.gguf'), 'Q4_K_M')
        self.assertEqual(gguf.shard('big-00002-of-00005.gguf'), (2, 5))
        self.assertIsNone(gguf.shard('big.gguf'))

    def test_memory_for_the_context_counts_each_layer_as_it_is(self):
        plain = gguf.read(aikit.gguf_bytes(aikit.plain(layers=36, embed=2560, heads=32, kv_heads=8, **{'testarch.attention.key_length': 128, 'testarch.attention.value_length': 128})))['meta']
        self.assertEqual(gguf.context_bytes(plain, 8192), 8192 * 36 * 8 * 256 * 2)         # Qwen3-VL 4B's numbers: 1.2 GB, as measured
        # Gemma's way: five layers in six look back through a window with eight small heads; the sixth sees everything with one large head
        windowed = [i % 6 != 5 for i in range(48)]
        gemma = gguf.read(aikit.gguf_bytes(aikit.plain(arch='gemma4', layers=48, embed=3840, heads=16, kv_heads=[8 if w else 1 for w in windowed], **{
            'gemma4.attention.key_length': 512, 'gemma4.attention.value_length': 512, 'gemma4.attention.key_length_swa': 256, 'gemma4.attention.value_length_swa': 256,
            'gemma4.attention.sliding_window': 1024, 'gemma4.attention.sliding_window_pattern': windowed})))['meta']
        want = 40 * (1024 + gguf.BATCH) * 8 * 512 * 2 + 8 * 16384 * 1 * 1024 * 2
        self.assertEqual(gguf.context_bytes(gemma, 16384), want)
        self.assertLess(want, 1.4 * GiB)
        self.assertLess(gguf.context_bytes(gemma, 16384) - gguf.context_bytes(gemma, 8192), 0.2 * GiB)      # a larger context costs such a model little
        older = gguf.read(aikit.gguf_bytes(aikit.plain(arch='gemma3', layers=12, embed=1024, heads=4, kv_heads=4, **{'gemma3.attention.sliding_window': 512, 'gemma3.attention.sliding_window_pattern': 6})))['meta']
        self.assertEqual(gguf.context_bytes(older, 8192), (10 * (512 + gguf.BATCH) + 2 * 8192) * 4 * 512 * 2)
        self.assertIsNone(gguf.context_bytes({'general.architecture': 'x'}, 8192))
        self.assertIsNone(gguf.memory({'general.architecture': 'x'}, 5 * GiB, 8192))

    def test_the_largest_context_that_fits(self):
        meta = gguf.read(aikit.gguf_bytes(aikit.plain(layers=36, embed=2560, heads=32, kv_heads=8)))['meta']
        size = int(2.5 * GiB)
        ctx, need = gguf.best_context(meta, size, 13 * GiB)
        self.assertEqual(ctx, 16384)
        self.assertEqual(need, gguf.memory(meta, size, 16384))
        ctx, _ = gguf.best_context(meta, size, int(4.3 * GiB))
        self.assertEqual(ctx, 12288)
        ctx, need = gguf.best_context(meta, size, 2 * GiB)
        self.assertIsNone(ctx)
        self.assertGreater(need, 2 * GiB)
        small = gguf.read(aikit.gguf_bytes(aikit.plain(context=8192)))['meta']
        self.assertEqual(gguf.best_context(small, GiB, 64 * GiB)[0], 8192)       # never more than the model was trained for
        self.assertEqual(gguf.best_context({'general.architecture': 'x'}, GiB, 64 * GiB), (None, None))
        self.assertGreater(gguf.memory(meta, size, 8192, extra=GiB), gguf.memory(meta, size, 8192) + GiB - 1)


class Found(Temp):
    def test_models_in_the_library_with_their_projector_and_draft(self):
        self.nowhere_else()
        d = self.data / 'models' / 'acme--talker'
        aikit.write_model(d / 'talker-Q4_K_M.gguf', aikit.plain(name='Talker'), 3 * GiB)
        aikit.write_model(d / 'talker-Q8_0.gguf', aikit.plain(name='Talker'), 5 * GiB)
        aikit.write_model(d / 'mmproj-talker-F16.gguf', {'general.architecture': 'clip', 'general.type': 'mmproj'}, 400 * 2**20)
        aikit.write_model(d / 'mtp-talker.gguf', aikit.plain(name='Talker draft'), 200 * 2**20)
        (d / 'model.json').write_text(json.dumps({'repo': 'acme/talker', 'revision': 'abc', 'license': 'mit', 'added': '2026-11-18', 'files': []}))
        (d / 'notes.txt').write_text('not a model')
        (d / 'broken.gguf').write_bytes(b'not a model either')
        aikit.write_model(self.data / 'models' / 'loose.gguf', aikit.plain(name='Loose', template='x'), GiB)
        got = {m['file']: m for m in models.on_drive(self.data)}
        self.assertEqual(set(got), {'talker-Q4_K_M.gguf', 'talker-Q8_0.gguf', 'loose.gguf'})
        m = got['talker-Q4_K_M.gguf']
        self.assertEqual((m['id'], m['name'], m['where'], m['origin']), ('drive:acme--talker/talker-Q4_K_M.gguf', 'Talker Q4_K_M', 'drive', 'This library'))
        self.assertTrue(m['projector'].endswith('mmproj-talker-F16.gguf'))
        self.assertTrue(m['draft'].endswith('mtp-talker.gguf'))
        self.assertTrue(m['vision'])
        self.assertEqual(m['bytes'], 3 * GiB + 600 * 2**20)
        self.assertEqual(m['source'], {'repo': 'acme/talker', 'revision': 'abc', 'license': 'mit', 'added': '2026-11-18'})
        self.assertFalse(got['loose.gguf']['vision'])
        self.assertIsNone(got['loose.gguf']['projector'])
        self.assertNotIn('source', got['loose.gguf'])

    def test_a_model_in_numbered_parts_is_one_model(self):
        self.nowhere_else()
        d = self.data / 'models' / 'big'
        for i in (1, 2, 3):
            aikit.write_model(d / f'giant-Q4_K_M-0000{i}-of-00003.gguf', aikit.plain(name='Giant'), 4 * GiB)
        got = models.on_drive(self.data)
        self.assertEqual([m['file'] for m in got], ['giant-Q4_K_M-00001-of-00003.gguf'])
        self.assertEqual(got[0]['model_bytes'], 12 * GiB)

    def test_a_projector_goes_with_the_model_it_is_named_for(self):
        self.nowhere_else()
        d = self.data / 'models' / 'mixed'
        aikit.write_model(d / 'alpha-7b-Q4_K_M.gguf', aikit.plain(name='Alpha'), GiB)
        aikit.write_model(d / 'beta-3b-Q4_K_M.gguf', aikit.plain(name='Beta'), GiB)
        aikit.write_model(d / 'mmproj-beta-3b-f16.gguf', {'general.architecture': 'clip'}, 2**20)
        aikit.write_model(d / 'alpha-7b.mmproj-f16.gguf', {'general.architecture': 'clip'}, 2**20)
        got = {m['file']: Path(m['projector']).name for m in models.on_drive(self.data)}
        self.assertEqual(got, {'alpha-7b-Q4_K_M.gguf': 'alpha-7b.mmproj-f16.gguf', 'beta-3b-Q4_K_M.gguf': 'mmproj-beta-3b-f16.gguf'})

    def test_models_other_programs_hold_are_found_and_left_alone(self):
        any_llm = self.tmp / 'anything/storage/models'
        aikit.write_model(any_llm / 'lemond/models--Qwen--Q/snapshots/abc/Q-4B-Q4_K_M.gguf', aikit.plain(name='Q 4B'), 2 * GiB)
        aikit.write_model(any_llm / 'lemond/models--Qwen--Q/snapshots/abc/mmproj-Q-4B-F16.gguf', {'general.architecture': 'clip'}, GiB)
        hf = self.tmp / 'hf/hub'
        real = aikit.write_model(hf / 'models--a--b/blobs/0123', aikit.plain(name='Linked'), GiB)
        snap = hf / 'models--a--b/snapshots/rev'
        snap.mkdir(parents=True)
        os.symlink(real, snap / 'linked-Q5_K_M.gguf')                   # the Hugging Face cache keeps files behind links
        (any_llm / '.hidden').mkdir()
        aikit.write_model(any_llm / '.hidden/secret-Q4_K_M.gguf', aikit.plain(), GiB)
        ollama = self.tmp / 'ollama'
        blob = aikit.write_model(ollama / 'blobs/sha256-aaaa', aikit.plain(name='Llama'), 3 * GiB)
        proj = aikit.write_model(ollama / 'blobs/sha256-bbbb', {'general.architecture': 'clip'}, 2**20)
        man = ollama / 'manifests/registry.ollama.ai/library/llama/latest'
        man.parent.mkdir(parents=True)
        man.write_text(json.dumps({'layers': [{'mediaType': 'application/vnd.ollama.image.model', 'digest': 'sha256:aaaa'},
                                              {'mediaType': 'application/vnd.ollama.image.projector', 'digest': 'sha256:bbbb'},
                                              {'mediaType': 'application/vnd.ollama.image.license', 'digest': 'sha256:cccc'}]}))
        (ollama / 'manifests/registry.ollama.ai/library/llama/broken').write_text('{not json')
        before = sorted((str(p), p.stat().st_mtime_ns) for p in self.tmp.rglob('*') if p.is_file())
        with mock.patch.object(models, 'places', return_value=[(any_llm, 'AnythingLLM'), (hf, 'Hugging Face cache'), (self.tmp / 'not-there', 'LM Studio')]), \
                mock.patch.dict(os.environ, {'OLLAMA_MODELS': str(ollama)}):
            got = {m['origin']: m for m in models.on_computer()}
        self.assertEqual(set(got), {'AnythingLLM', 'Hugging Face cache', 'Ollama'})
        self.assertTrue(all(m['where'] == 'computer' and m['id'].startswith('here:') for m in got.values()))
        self.assertTrue(got['AnythingLLM']['vision'])
        self.assertEqual(got['Hugging Face cache']['quant'], 'Q5_K_M')
        self.assertEqual(got['Ollama']['name'], 'Llama Q4_K_M (llama:latest)')
        self.assertEqual(got['Ollama']['projector'], str(proj))
        self.assertEqual(got['Ollama']['path'], str(blob))
        self.assertEqual(before, sorted((str(p), p.stat().st_mtime_ns) for p in self.tmp.rglob('*') if p.is_file()))     # nothing written, moved or touched

    def test_does_it_fit_this_computer(self):
        small = {'meta': gguf.read(aikit.gguf_bytes(aikit.plain(layers=36, embed=2560, heads=32, kv_heads=8)))['meta'], 'model_bytes': int(2.5 * GiB), 'extra_bytes': 0, 'bytes': int(2.5 * GiB)}
        f = models.judge(small, aikit.HW16)
        self.assertEqual((f['fits'], f['context'], f['tight']), (True, 16384, False))
        self.assertIn('16 GB of memory', f['why'])
        self.assertEqual(f['needs_bytes'], f['uses_bytes'] + int(2.5 * GiB))
        big = dict(small, model_bytes=17 * GiB, bytes=17 * GiB)
        f = models.judge(big, aikit.HW16)
        self.assertEqual((f['fits'], f['context']), (False, None))
        self.assertIn('would use about', f['why'])
        near = dict(small, model_bytes=int(10.5 * GiB), bytes=int(10.5 * GiB))
        f = models.judge(near, aikit.HW16)
        self.assertTrue(f['fits'] and f['tight'])
        self.assertIn('close them', f['why'])
        self.assertEqual(models.judge(dict(small, model_bytes=6 * GiB), aikit.HW8)['fits'], False)
        self.assertTrue(models.judge(dict(small, model_bytes=20 * GiB), aikit.HW_PC)['fits'])      # what goes on the graphics card is not in the computer's own memory
        unknown = models.judge(small, dict(aikit.HW16, ram_bytes=None))
        self.assertIsNone(unknown['fits'])
        no_numbers = {'meta': {'general.architecture': 'odd'}, 'model_bytes': 4 * GiB, 'extra_bytes': 0, 'bytes': 4 * GiB}
        self.assertTrue(models.judge(no_numbers, aikit.HW16)['fits'])                               # judged by its size alone
        self.assertFalse(models.judge(dict(no_numbers, bytes=40 * GiB), aikit.HW16)['fits'])

    def test_the_page_is_not_told_where_files_are(self):
        self.nowhere_else()
        aikit.write_model(self.data / 'models' / 'a' / 'm-Q4_K_M.gguf', aikit.plain(), GiB)
        m = models.found(self.data, hw=aikit.HW16)[0]
        p = models.public(m)
        self.assertNotIn(str(self.tmp), json.dumps(p))
        for hidden in ('path', 'projector', 'draft', 'meta'):
            self.assertNotIn(hidden, p)
        self.assertEqual(p['folder'], 'a')
        self.assertTrue(p['fit']['fits'])

    def test_those_that_travel_come_first_then_what_fits(self):
        aikit.write_model(self.data / 'models' / 'small-Q4_K_M.gguf', aikit.plain(name='Small'), GiB)
        aikit.write_model(self.data / 'models' / 'huge-Q4_K_M.gguf', aikit.plain(name='Huge'), 40 * GiB)
        other = self.tmp / 'other'
        aikit.write_model(other / 'mid-Q4_K_M.gguf', aikit.plain(name='Mid'), 4 * GiB)
        with mock.patch.object(models, 'places', return_value=[(other, 'LM Studio')]), mock.patch.dict(os.environ, {'OLLAMA_MODELS': str(self.tmp / 'none')}):
            names = [m['name'] for m in models.found(self.data, hw=aikit.HW16)]
        self.assertEqual(names, ['Small Q4_K_M', 'Huge Q4_K_M', 'Mid Q4_K_M'])


@unittest.skipUnless(POSIX, 'the stand-in for llama-server is a shell script')
class Running(Temp):
    def setUp(self):
        super().setUp()
        self.nowhere_else()
        self.server = aikit.fake_server(self.tmp)
        self.record = self.tmp / 'record.jsonl'
        self.env = {'LASTBOOK_LLAMA_SERVER': str(self.server), 'FAKE_LLAMA_RECORD': str(self.record)}
        aikit.write_model(self.data / 'models' / 'm' / 'talker-Q4_K_M.gguf', aikit.plain(name='Talker'), GiB)
        aikit.write_model(self.data / 'models' / 'm' / 'mmproj-talker-f16.gguf', {'general.architecture': 'clip'}, 2**20)
        aikit.write_model(self.data / 'models' / 'm' / 'mtp-talker.gguf', aikit.plain(name='d'), 2**20)

    def start(self, **env):
        p = mock.patch.dict(os.environ, {**self.env, **{k: str(v) for k, v in env.items()}})
        p.start()
        self.addCleanup(p.stop)
        runtime._flags.clear()
        r = runtime.Runner(self.data)
        self.addCleanup(r.stop, 'test over')
        model = models.found(self.data, hw=aikit.HW16)[0]
        return r, model

    def test_started_on_a_socket_no_one_else_can_reach(self):
        r, model = self.start(FAKE_LLAMA_SCRIPT=json.dumps([[{'content': 'Hello'}, {'content': ' there'}]]))
        self.assertEqual(runtime.find(self.data), {'path': str(self.server), 'origin': 'the path you gave'})
        self.assertIsNone(r.ensure(model))
        self.assertTrue(r.running(model['id']))
        self.assertEqual(r.status()['context'], 16384)
        argv = aikit.recorded(self.record)[0]['argv']
        host = argv[argv.index('--host') + 1]
        self.assertTrue(host.endswith('.sock'))
        self.assertNotIn('--port', argv)
        self.assertNotIn('--api-key', argv)
        self.assertEqual(os.stat(Path(host).parent).st_mode & 0o077, 0)      # the folder holding the socket: this user only
        self.assertNotIn(str(self.data), host)                               # not on the library's drive: exFAT holds no sockets
        self.assertEqual(argv[argv.index('-m') + 1], model['path'])
        self.assertEqual(argv[argv.index('--mmproj') + 1], model['projector'])
        self.assertEqual(argv[argv.index('-md') + 1], model['draft'])
        self.assertEqual(argv[argv.index('--spec-type') + 1], 'draft-mtp')
        for flag in ('--jinja', '--offline', '--no-webui'):
            self.assertIn(flag, argv)
        resp = r.stream({'messages': [{'role': 'user', 'content': 'hi'}], 'stream': True})
        text = ''.join(json.loads(line[5:])['choices'][0]['delta'].get('content', '') for line in (raw.decode().strip() for raw in resp) if line.startswith('data: {'))
        resp.close()
        self.assertEqual(text, 'Hello there')
        self.assertIsNone(r.ensure(model))                                    # already running: nothing is started again
        self.assertEqual(len([x for x in aikit.recorded(self.record) if 'argv' in x]), 1)
        folder = Path(host).parent
        pid = aikit.recorded(self.record)[0]['pid']
        r.stop('asked')
        self.assertEqual(r.status()['state'], 'stopped')
        self.assertFalse(folder.exists())                                     # the socket and its folder are gone
        for _ in range(50):
            try:
                os.kill(pid, 0)
                time.sleep(0.1)
            except OSError:
                break
        else:
            self.fail('the model program is still running after stop')

    def test_a_smaller_context_is_tried_when_the_model_will_not_load(self):
        r, model = self.start(FAKE_LLAMA_MAX_CTX=8192)
        self.assertIsNone(r.ensure(model))
        self.assertEqual(r.status()['context'], 8192)
        tried = [int(x['argv'][x['argv'].index('-c') + 1]) for x in aikit.recorded(self.record) if 'argv' in x]
        self.assertEqual(tried, [16384, 12288, 8192])

    def test_a_model_that_never_loads_says_so(self):
        r, model = self.start(FAKE_LLAMA_MAX_CTX=100)
        why = r.ensure(model)
        self.assertIn('could not be loaded', why)
        self.assertEqual(r.status()['state'], 'error')
        self.assertIsNone(r.folder)

    def test_no_program_to_run_models(self):
        r = runtime.Runner(self.data)
        model = models.found(self.data, hw=aikit.HW16)[0]
        with mock.patch.object(runtime, 'find', return_value=None):
            self.assertIn('No program to run AI models', r.ensure(model))
        gone = dict(model, path=str(self.tmp / 'gone.gguf'))
        with mock.patch.dict(os.environ, self.env):
            self.assertIn('not where it was', r.ensure(gone))

    def test_a_model_is_not_started_into_memory_that_is_in_use(self):
        from engine.setup import hardware
        r, model = self.start()
        uses = model['fit']['uses_bytes']
        with mock.patch.object(hardware, 'available_ram', return_value=int(0.5 * uses)):
            why = r.ensure(model)
            self.assertIn('is free right now', why)
            self.assertIn('Close other programs', why)
            self.assertEqual(aikit.recorded(self.record), [])                     # nothing was started
        with mock.patch.object(hardware, 'available_ram', return_value=int(0.8 * uses)):
            self.assertIsNone(r.ensure(model))                                    # near enough: the system finds the rest
        self.assertIsNone(runtime.Runner.short_of_memory({'fit': {}}))
        free = FREE_NOW()                                                         # the real reading, on this computer
        if free is not None:
            self.assertGreater(free, 100 * 2**20)
            self.assertLessEqual(free, hardware.detect()['ram_bytes'])

    def test_waiting_can_be_given_up(self):
        r, model = self.start(FAKE_LLAMA_LOAD_S=30)
        t = time.time()
        self.assertEqual(r.ensure(model, cancelled=lambda: time.time() - t > 0.5), 'stopped')

    def test_the_model_goes_when_lastbook_goes_however_it_went(self):
        """Lastbook is stood in for by a small program that starts a model and is then killed outright."""
        script = self.tmp / 'parent.py'
        script.write_text(f'''
import os, sys, time
sys.path[:0] = [{str(ROOT)!r}, {str(HERE)!r}]
os.environ["LASTBOOK_DATA"] = {str(self.data)!r}
from unittest import mock
from engine.ai import models, runtime
import aikit
with mock.patch.object(models, "SMALL", 0), mock.patch.object(models, "places", return_value=[]):
    m = models.found({str(self.data)!r}, hw=aikit.HW16)[0]
r = runtime.Runner({str(self.data)!r})
print("ready" if r.ensure(m) is None else "failed", flush=True)
time.sleep(60)
''')
        env = {**os.environ, **self.env, 'OLLAMA_MODELS': str(self.tmp / 'none')}
        p = subprocess.Popen([sys.executable, '-B', str(script)], stdout=subprocess.PIPE, env=env, text=True)
        try:
            said = ''
            while said not in ('ready', 'failed'):          # its own log lines come first
                said = p.stdout.readline().strip()
                self.assertTrue(said, 'the stand-in for Lastbook ended early')
            self.assertEqual(said, 'ready')
            pid = aikit.recorded(self.record)[0]['pid']
            os.kill(pid, 0)                     # the model program is there
            p.send_signal(signal.SIGKILL)       # no goodbye
            p.wait(5)
            for _ in range(80):
                try:
                    os.kill(pid, 0)
                    time.sleep(0.1)
                except OSError:
                    return
            self.fail('the model program outlived Lastbook')
        finally:
            if p.poll() is None:
                p.kill()
            p.stdout.close()

    def test_the_options_come_from_the_programs_own_help(self):
        r, model = self.start()
        self.assertIn('--jinja', runtime.flags(str(self.server)))
        with mock.patch.object(runtime, 'flags', return_value={'-m', '-c'}):
            r._listen_at()
            argv = r.argv(str(self.server), model, 8192)
        for unknown in ('--jinja', '--offline', '--no-webui', '-md', '--spec-type', '-ngl'):
            self.assertNotIn(unknown, argv)             # an older llama-server is not handed options it would refuse
        self.assertEqual(runtime.flags(str(self.tmp / 'no-such-program')), set())

    def test_where_there_is_no_such_socket_a_key_guards_a_loopback_port(self):
        r, model = self.start()
        with mock.patch.object(runtime.os, 'name', 'nt'):
            r._listen_at()
        self.assertIsNone(r.sock)
        self.assertGreater(r.port, 1023)
        self.assertGreater(len(r.key), 30)
        argv = r.argv(str(self.server), model, 8192)
        self.assertEqual(argv[argv.index('--host') + 1], '127.0.0.1')
        self.assertEqual(argv[argv.index('--api-key') + 1], r.key)
        self.assertEqual(r._headers()['Authorization'], 'Bearer ' + r.key)


class Chosen(Temp):
    def setUp(self):
        super().setUp()
        self.nowhere_else()
        aikit.write_model(self.data / 'models' / 'small-Q4_K_M.gguf', aikit.plain(name='Small'), GiB)
        aikit.write_model(self.data / 'models' / 'mid-Q4_K_M.gguf', aikit.plain(name='Mid'), 5 * GiB)
        aikit.write_model(self.data / 'models' / 'huge-Q4_K_M.gguf', aikit.plain(name='Huge'), 40 * GiB)
        self.p = picker.Picker(self.data)
        self.p._hw = aikit.HW16
        f = mock.patch.object(runtime, 'find', return_value={'path': '/x/llama-server', 'origin': 'test'})
        f.start()
        self.addCleanup(f.stop)

    def test_the_choice_is_kept_with_the_library(self):
        self.assertEqual(self.p.choice(), {'id': 'auto'})
        self.p.select('drive:small-Q4_K_M.gguf')
        again = picker.Picker(self.data)
        self.assertEqual(again.choice()['id'], 'drive:small-Q4_K_M.gguf')
        self.assertEqual(json.loads((self.data / 'derived/llm/picked.json').read_text())['name'], 'Small Q4_K_M')
        with self.assertRaises(KeyError):
            self.p.select('drive:not-here.gguf')
        for fixed in ('none', 'stock', 'server', 'auto'):
            self.assertEqual(self.p.select(fixed), {'id': fixed})
        (self.data / 'derived/llm/picked.json').write_text('{broken')
        self.assertEqual(picker.Picker(self.data).choice(), {'id': 'auto'})

    def test_by_itself_it_takes_the_largest_that_fits(self):
        self.assertEqual(self.p.by_itself('drive')['name'], 'Mid Q4_K_M')
        self.assertIsNone(self.p.by_itself('computer'))
        with mock.patch.object(runtime, 'find', return_value=None):
            self.assertIsNone(self.p.by_itself('drive'))           # nothing to run it with

    def test_a_choice_that_cannot_run_here_is_not_used(self):
        self.p.select('drive:huge-Q4_K_M.gguf')
        self.assertIsNone(self.p.chosen())                         # too large for this computer
        self.p.select('drive:small-Q4_K_M.gguf')
        self.assertEqual(self.p.chosen()['name'], 'Small Q4_K_M')
        (self.data / 'models' / 'small-Q4_K_M.gguf').unlink()
        self.p.models(refresh=True)
        self.assertIsNone(self.p.chosen())                         # the drive is on another computer now, or the file was removed

    def test_what_the_page_is_shown(self):
        d = self.p.describe(active='drive:mid-Q4_K_M.gguf', stock=None, server={'model': 'x', 'online': False})
        self.assertEqual(d['selected'], 'auto')
        self.assertEqual(d['runtime'], {'origin': 'test'})
        self.assertEqual(d['computer']['ram_bytes'], 16 * GiB)
        self.assertEqual(len(d['models']), 3)
        self.assertNotIn(str(self.tmp), json.dumps(d))


class Answering(Temp):
    """assistant.backend(): which of the ways to answer is used."""

    def setUp(self):
        super().setUp()
        self.nowhere_else()
        self.p = picker.Picker(self.data)
        self.p._hw = aikit.HW16
        for patch in (mock.patch.object(assistant, 'PICKER', self.p), mock.patch.object(assistant, 'LOCAL', None),
                      mock.patch.object(assistant, '_server_online', return_value=False),
                      mock.patch.object(runtime, 'find', return_value={'path': '/x/llama-server', 'origin': 'test'})):
            patch.start()
            self.addCleanup(patch.stop)

    def model(self, name='talker', where=None, size=GiB, vision=False):
        folder = where or self.data / 'models'
        aikit.write_model(folder / f'{name}-Q4_K_M.gguf', aikit.plain(name=name.title()), size)
        if vision:
            aikit.write_model(folder / f'mmproj-{name}-f16.gguf', {'general.architecture': 'clip'}, 2**20)
        self.p.models(refresh=True)

    def test_nothing_here_nothing_answers(self):
        self.assertEqual(assistant.backend(), ('none', None))
        st = assistant.model_status()
        self.assertFalse(st['online'])
        self.assertNotIn('picked', st)

    def test_a_model_that_travels_with_the_library_answers_without_being_chosen(self):
        self.model(vision=True)
        kind, m = assistant.backend()
        self.assertEqual((kind, m['name']), ('picked', 'Talker Q4_K_M'))
        st = assistant.model_status()
        self.assertEqual((st['online'], st['model'], st['vision']), (True, 'Talker Q4_K_M', True))
        self.assertEqual((st['picked']['where'], st['picked']['state']), ('drive', 'stopped'))

    def test_a_model_another_program_holds_answers_only_when_chosen(self):
        other = self.tmp / 'elsewhere'
        with mock.patch.object(models, 'places', return_value=[(other, 'AnythingLLM')]):
            self.model(where=other)
            self.assertEqual(assistant.backend(), ('none', None))
            self.p.select(self.p.models()[0]['id'])
            self.assertEqual(assistant.backend()[0], 'picked')
            self.assertEqual(assistant.model_status()['picked']['origin'], 'AnythingLLM')

    def test_the_stock_model_and_a_model_server_keep_their_places(self):
        local = mock.Mock()
        local.available.return_value = True
        local.status.return_value = {'name': 'Gemma 4 26B', 'state': 'stopped'}
        with mock.patch.object(assistant, 'LOCAL', local):
            self.assertEqual(assistant.backend(), ('local', None))
            self.model()
            self.assertEqual(assistant.backend()[0], 'picked')          # one that travels with the library comes before the stock model
            self.p.select('stock')
            self.assertEqual(assistant.backend(), ('local', None))      # unless the person says otherwise
            self.assertEqual(assistant.model_status()['model'], 'Gemma 4 26B')
        with mock.patch.object(assistant, '_server_online', return_value=True):
            self.p.select('server')
            self.assertEqual(assistant.backend(), ('server', None))
        self.p.select('server')
        self.assertEqual(assistant.backend()[0], 'picked')              # the server is not there: Lastbook chooses by itself

    def test_no_ai_means_none(self):
        self.model()
        self.p.select('none')
        self.assertEqual(assistant.backend(), ('off', None))
        st = assistant.model_status()
        self.assertEqual((st['online'], st['off']), (False, True))

    def test_without_the_picker_everything_is_as_it_was(self):
        with mock.patch.object(assistant, 'PICKER', None):
            self.assertEqual(assistant.backend(), ('none', None))
            self.assertIsNone(assistant.picker_view())
            self.assertFalse(assistant.model_status()['online'])


class Words(unittest.TestCase):
    def shown(self, pieces):
        out, states = [], []
        s = assistant._Shown(out.append, states.append)
        for p in pieces:
            s.add(p)
        return ''.join(out), s.text(), states

    def test_thinking_is_not_shown(self):
        seen, final, states = self.shown(['<th', 'ink>let me', ' see…</th', 'ink>\n\nBoil', ' it [S1].'])
        self.assertEqual(seen, 'Boil it [S1].')
        self.assertEqual(final, 'Boil it [S1].')
        self.assertIn('thinking', states)

    def test_plain_words_pass_as_they_come(self):
        seen, final, states = self.shown(['Boil ', 'water ', 'for 1 <', ' 2 minutes'])
        self.assertEqual(seen, 'Boil water for 1 < 2 minutes')
        self.assertEqual(states, [])
        self.assertEqual(self.shown(['a <b> c'])[0], 'a <b> c')

    def test_thinking_that_never_ends_shows_nothing(self):
        seen, final, _ = self.shown(['<think>', 'on and on'])
        self.assertEqual((seen, final), ('', ''))

    def test_messages_are_cut_to_the_models_context(self):
        long = 'word ' * 4000
        msgs = [{'role': 'system', 'content': 'rules'}, {'role': 'system', 'content': long}, {'role': 'user', 'content': 'old question'},
                {'role': 'assistant', 'content': 'old answer'}, {'role': 'user', 'content': 'the question'}, {'role': 'tool', 'tool_call_id': 'c', 'content': long}]
        fitted = assistant._fit_messages(msgs, None, 6144)
        self.assertEqual(fitted[0]['content'], 'rules')
        self.assertEqual([m['content'] for m in fitted if m['role'] == 'user'], ['the question'])       # the older turn went first
        self.assertTrue(any('shortened to fit' in m['content'] for m in fitted))
        self.assertEqual(assistant._fit_messages(msgs[:1] + msgs[4:5], None, 16384), msgs[:1] + msgs[4:5])       # what fits is left alone
        self.assertIsNone(assistant._fit_messages([{'role': 'system', 'content': 'x'}, {'role': 'user', 'content': long * 10}], None, 6144))
        picture = [{'role': 'user', 'content': [{'type': 'text', 'text': 'see'}, {'type': 'image_url', 'image_url': {'url': 'data:...'}}]}]
        self.assertEqual(assistant._fit_messages(picture, None, 6144), picture)


@unittest.skipUnless(POSIX, 'the stand-in for llama-server is a shell script')
class Asked(Temp):
    """A whole question, through the chosen model: search, a tool call, an answer with its sources."""

    def setUp(self):
        super().setUp()
        self.nowhere_else()
        self.record = self.tmp / 'record.jsonl'
        self.server = aikit.fake_server(self.tmp)
        self.p = picker.Picker(self.data)
        self.p._hw = aikit.HW16
        runtime._flags.clear()
        for patch in (mock.patch.object(assistant, 'PICKER', self.p), mock.patch.object(assistant, 'LOCAL', None), mock.patch.object(assistant, 'S', server),
                      mock.patch.object(assistant, 'INDEX', None), mock.patch.object(assistant, '_server_online', return_value=False)):
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(self.p.runner.stop, 'test over')

    def ask(self, script, vision=False, question='How do I make water safe to drink?'):
        aikit.write_model(self.data / 'models' / 'talker-Q4_K_M.gguf', aikit.plain(name='Talker'), GiB)
        if vision:
            aikit.write_model(self.data / 'models' / 'mmproj-talker-f16.gguf', {'general.architecture': 'clip'}, 2**20)
        events = []
        env = {'LASTBOOK_LLAMA_SERVER': str(self.server), 'FAKE_LLAMA_RECORD': str(self.record), 'FAKE_LLAMA_SCRIPT': json.dumps(script)}
        with mock.patch.dict(os.environ, env):
            assistant.chat([{'role': 'user', 'content': question}], None, events.append, lambda: False)
        return events, [x['request'] for x in aikit.recorded(self.record) if 'request' in x]

    def test_search_then_an_answer_with_its_source(self):
        call = {'tool_calls': [{'index': 0, 'id': 'c1', 'function': {'name': 'search_library', 'arguments': json.dumps({'query': 'safe drinking water'})}}]}
        events, requests = self.ask([[call], [{'content': 'Boil it for one minute'}, {'content': ' [S1].'}]])
        kinds = [e['type'] for e in events]
        self.assertEqual(kinds[-1], 'done', events[-1])
        done = events[-1]
        self.assertEqual(done['answer'], 'Boil it for one minute [S1].')
        self.assertEqual([s['key'] for s in done['sources']], ['S1'])
        self.assertEqual(done['invalid'], [])
        self.assertIn({'type': 'state', 'state': 'starting'}, events)
        self.assertEqual([e['name'] for e in events if e['type'] == 'tool' and e['phase'] == 'done'], ['search_library'])
        self.assertEqual(len(requests), 2)
        first = requests[0]
        self.assertNotIn('model', first)                                            # the runner holds one model: none is named
        self.assertEqual(first['chat_template_kwargs'], {'enable_thinking': False})
        names = [t['function']['name'] for t in first['tools']]
        self.assertNotIn('view_page', names)                                        # no projector: it is not told it can look at pages
        self.assertIn('search_library', names)
        self.assertNotIn('view_page', first['messages'][0]['content'])
        self.assertEqual(requests[1]['messages'][-1]['role'], 'tool')
        self.assertEqual(assistant.model_status()['picked']['state'], 'ready')

    def test_a_model_with_a_projector_is_offered_page_images(self):
        events, requests = self.ask([[{'content': 'See the page [S1].'}]], vision=True)
        self.assertEqual(events[-1]['type'], 'done')
        self.assertIn('view_page', [t['function']['name'] for t in requests[0]['tools']])
        self.assertIn('view_page', requests[0]['messages'][0]['content'])

    def test_thinking_between_marks_never_reaches_the_person(self):
        events, _ = self.ask([[{'content': '<think>hmm'}, {'content': ' ok</think>'}, {'content': 'Boil it [S1].'}]])
        self.assertEqual(events[-1]['answer'], 'Boil it [S1].')
        self.assertEqual(''.join(e['text'] for e in events if e['type'] == 'delta'), 'Boil it [S1].')

    def test_a_source_it_made_up_is_named_as_such(self):
        events, _ = self.ask([[{'content': 'Boil it [S1]. Add salt [S99].'}]])
        self.assertEqual(events[-1]['invalid'], ['S99'])

    def test_a_model_that_will_not_start_is_said_plainly(self):
        aikit.write_model(self.data / 'models' / 'talker-Q4_K_M.gguf', aikit.plain(name='Talker'), GiB)
        events = []
        with mock.patch.dict(os.environ, {'LASTBOOK_LLAMA_SERVER': str(self.server), 'FAKE_LLAMA_MAX_CTX': '10'}):
            assistant.chat([{'role': 'user', 'content': 'hello'}], None, events.append, lambda: False)
        self.assertEqual(events[-1]['type'], 'error')
        self.assertEqual(events[-1]['error'], 'model_unavailable')
        self.assertIn('could not be loaded', events[-1]['text'])

    def test_stop_ends_the_models_writing(self):
        aikit.write_model(self.data / 'models' / 'talker-Q4_K_M.gguf', aikit.plain(name='Talker'), GiB)
        events, stop = [], [False]

        def emit(e):
            events.append(e)
            if e['type'] == 'delta':
                stop[0] = True
        script = [[{'content': 'Boil'}, {'sleep': 0.3}, {'content': ' it'}, {'sleep': 5}, {'content': ' never sent'}]]
        env = {'LASTBOOK_LLAMA_SERVER': str(self.server), 'FAKE_LLAMA_RECORD': str(self.record), 'FAKE_LLAMA_SCRIPT': json.dumps(script)}
        t = time.time()
        with mock.patch.dict(os.environ, env):
            assistant.chat([{'role': 'user', 'content': 'hello'}], None, emit, lambda: stop[0])
        self.assertEqual(events[-1]['type'], 'stopped')
        self.assertLess(time.time() - t, 4)
        self.assertFalse(self.p.runner.busy.locked())                               # the next question is not kept waiting


class Hub:
    """Hugging Face, made up: its two lists for one repository, and the first bytes of its files."""

    def __init__(self, files, meta=None, info=None):
        self.files, self.meta, self.info = files, meta or aikit.plain(name='Hub Model', layers=36, embed=2560, heads=32, kv_heads=8, template='tools'), info or {}
        self.asked = []

    def get_small(self, url, limit=0, **kw):
        self.asked.append(url)
        if '/tree/' in url:
            return 200, json.dumps([{'type': 'file', 'path': name, 'size': 130, 'lfs': {'oid': 'sha-' + name, 'size': size}} for name, size in self.files]
                                   + [{'type': 'file', 'path': 'README.md', 'size': 900}, {'type': 'directory', 'path': 'onnx'}]).encode()
        if url.endswith('/api/models/acme/talker-GGUF'):
            return 200, json.dumps({'id': 'acme/talker-GGUF', 'sha': 'rev123', 'cardData': {'license': 'apache-2.0'}, **self.info}).encode()
        if 'gated' in url:
            return 401, b'{}'
        return 404, b'{}'

    def read_range(self, url, start, end, **kw):
        self.asked.append(url)
        return aikit.gguf_bytes(self.meta)[start:end + 1]

    def patch(self, case):
        for p in (mock.patch.object(download, 'get_small', self.get_small), mock.patch.object(download, 'read_range', self.read_range)):
            p.start()
            case.addCleanup(p.stop)
        return self


FILES = [('talker-Q4_K_M.gguf', int(2.5 * GiB)), ('talker-Q8_0.gguf', int(4.3 * GiB)), ('talker-Q2_K.gguf', int(1.4 * GiB)), ('talker-F16.gguf', 30 * GiB),
         ('mmproj-talker-F16.gguf', 800 * 2**20), ('mmproj-talker-Q8_0.gguf', 500 * 2**20), ('mtp-talker.gguf', 250 * 2**20), ('talker.imatrix.gguf', 5 * 2**20)]


class Brought(Temp):
    def test_what_a_person_pastes(self):
        self.assertEqual(hf_import.parse('HauhauCS/Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced'), ('HauhauCS/Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced', None))
        self.assertEqual(hf_import.parse(' https://huggingface.co/Qwen/Qwen3-VL-4B-Instruct-GGUF/tree/main?x=1#files '), ('Qwen/Qwen3-VL-4B-Instruct-GGUF', None))
        self.assertEqual(hf_import.parse('hf.co/unsloth/gemma-4-12B-it-GGUF:q4_k_m'), ('unsloth/gemma-4-12B-it-GGUF', 'Q4_K_M'))
        self.assertEqual(hf_import.parse('huggingface.co/models/a/b'), ('a/b', None))
        for bad in ('', 'just-a-name', 'a/b/../../etc', '../x/y', 'a b/c', 'https://example.com/a/b', 'a/' + 'x' * 200, '-a/b', 'a/.b'):
            with self.assertRaises(hf_import.Refused, msg=bad):
                hf_import.parse(bad)
        self.assertEqual(hf_import.folder_for('acme/talker-GGUF'), 'acme--talker-GGUF')
        with self.assertRaises(hf_import.Refused):
            hf_import.folder_for('../x')

    def test_a_repository_is_read_and_every_version_judged_before_anything_large_is_fetched(self):
        hub = Hub(FILES).patch(self)
        f = hf_import.inspect('acme/talker-GGUF', hw=aikit.HW16, data=self.data)
        self.assertEqual((f['repo'], f['revision'], f['license'], f['name'], f['vision'], f['tools']), ('acme/talker-GGUF', 'rev123', 'apache-2.0', 'Hub Model', True, True))
        self.assertEqual([v['quant'] for v in f['versions']], ['Q2_K', 'Q4_K_M', 'Q8_0', 'F16'])
        self.assertEqual(f['projector']['name'], 'mmproj-talker-F16.gguf')              # of two precisions, the 16-bit one
        self.assertEqual(f['draft']['name'], 'mtp-talker.gguf')
        by = {v['quant']: v for v in f['versions']}
        self.assertTrue(by['Q4_K_M']['fit']['fits'])
        self.assertFalse(by['F16']['fit']['fits'])
        self.assertTrue(by['Q2_K']['coarse'])
        self.assertEqual(by['Q4_K_M']['total_bytes'], int(2.5 * GiB) + 800 * 2**20 + 250 * 2**20)
        self.assertEqual(by['Q4_K_M']['files'][0]['sha256'], 'sha-talker-Q4_K_M.gguf')
        self.assertEqual(f['recommended'], 'talker-Q4_K_M.gguf')
        self.assertTrue(f['judged'])
        self.assertEqual(len(hub.asked), 3)                                             # two lists and 64 KB of one file: nothing else
        self.assertTrue(all(u.startswith('https://huggingface.co/') for u in hub.asked))
        self.assertIn('/resolve/rev123/', hub.asked[2])                                 # the revision the lists were for, not whatever 'main' is by then

    def test_which_version_is_offered_first(self):
        Hub(FILES).patch(self)
        self.assertEqual(hf_import.inspect('acme/talker-GGUF:Q8_0', hw=aikit.HW16)['recommended'], 'talker-Q8_0.gguf')       # the one asked for
        self.assertEqual(hf_import.inspect('acme/talker-GGUF:F16', hw=aikit.HW16)['recommended'], 'talker-Q4_K_M.gguf')      # asked for, but it cannot run here
        small = dict(aikit.HW16, ram_bytes=4 * GiB)
        self.assertIsNone(hf_import.inspect('acme/talker-GGUF', hw=small)['recommended'])       # only the very coarse one fits: not recommended
        v = lambda q, fits=True: {'name': q, 'quant': q, 'bytes': 1, 'fit': {'fits': fits}}
        self.assertEqual(hf_import.recommend([v('Q8_0'), v('Q5_K_M'), v('Q6_K')]), 'Q5_K_M')
        self.assertEqual(hf_import.recommend([v('UD-Q4_K_XL'), v('Q3_K_M')]), 'UD-Q4_K_XL')
        self.assertEqual(hf_import.recommend([v('Q4_K_M', False), v('Q3_K_M')]), 'Q3_K_M')
        self.assertIsNone(hf_import.recommend([v('Q4_K_M', False)]))
        self.assertTrue(hf_import.coarse('UD-IQ2_M') and hf_import.coarse('Q2_K') and not hf_import.coarse('Q3_K_S') and not hf_import.coarse(None))

    def test_repositories_that_cannot_be_brought_say_why(self):
        Hub(FILES).patch(self)
        with self.assertRaisesRegex(hf_import.Refused, 'no repository by that name'):
            hf_import.inspect('nobody/nothing')
        with self.assertRaisesRegex(hf_import.Refused, 'no repository by that name'):
            hf_import.inspect('acme/gated')
        Hub(FILES, info={'gated': 'auto'}).patch(self)
        with self.assertRaisesRegex(hf_import.Refused, 'asks for a Hugging Face account'):
            hf_import.inspect('acme/talker-GGUF')
        Hub([('weights.safetensors', GiB)]).patch(self)
        with self.assertRaisesRegex(hf_import.Refused, 'no GGUF files'):
            hf_import.inspect('acme/talker-GGUF')
        Hub([('mmproj-only.gguf', GiB)]).patch(self)
        with self.assertRaisesRegex(hf_import.Refused, 'none is a model'):
            hf_import.inspect('acme/talker-GGUF')
        with mock.patch.object(download, 'get_small', side_effect=download.Failed('network', 'no line')):
            with self.assertRaises(download.Failed):
                hf_import.inspect('acme/talker-GGUF')

    def test_a_header_that_cannot_be_read_is_judged_by_size(self):
        hub = Hub(FILES).patch(self)
        with mock.patch.object(download, 'read_range', side_effect=download.Failed('network', 'x')):
            f = hf_import.inspect('acme/talker-GGUF', hw=aikit.HW16)
        self.assertFalse(f['judged'])
        self.assertTrue({v['quant']: v['fit']['fits'] for v in f['versions']}['Q4_K_M'])

    def bring(self, found, version='talker-Q4_K_M.gguf', **kw):
        fetched = []

        class Fetch:
            def __init__(s, url, dest, control=None, progress=None, sha256=None):
                s.url, s.dest, s.progress, s.sha = url, Path(dest), progress, sha256

            def run(s):
                fetched.append((s.url, s.dest.name, s.sha))
                aikit.write_model(s.dest, aikit.plain(name='Hub Model') if 'mmproj' not in s.dest.name else {'general.architecture': 'clip'}, dict(FILES).get(s.dest.name))
                Path(str(s.dest) + '.fetched.json').write_text('{"url": "https://cdn.example/signed?sig=secret"}')
                s.progress(100, 100, 'downloading')
        states = []
        with mock.patch.object(download, 'Fetch', Fetch):
            folder = hf_import.Import(found, version, data=self.data, progress=states.append, **kw).run()
        return folder, fetched, states

    def test_bringing_a_version_with_its_projector(self):
        Hub(FILES).patch(self)
        found = dict(hf_import.inspect('acme/talker-GGUF', hw=aikit.HW16, data=self.data), draft=None)
        folder, fetched, states = self.bring(found)
        self.assertEqual(folder, self.data / 'models' / 'acme--talker-GGUF')
        self.assertEqual([f[1] for f in fetched], ['talker-Q4_K_M.gguf', 'mmproj-talker-F16.gguf'])
        self.assertEqual(fetched[0][0], 'https://huggingface.co/acme/talker-GGUF/resolve/rev123/talker-Q4_K_M.gguf')
        self.assertEqual(fetched[0][2], 'sha-talker-Q4_K_M.gguf')                       # every file is checked against its published SHA-256
        self.assertEqual(sorted(p.name for p in folder.iterdir()), ['mmproj-talker-F16.gguf', 'model.json', 'talker-Q4_K_M.gguf'])     # and no note of a signed address
        note = json.loads((folder / 'model.json').read_text())
        self.assertEqual((note['repo'], note['revision'], note['license']), ('acme/talker-GGUF', 'rev123', 'apache-2.0'))
        self.assertEqual([f['name'] for f in note['files']], ['mmproj-talker-F16.gguf', 'talker-Q4_K_M.gguf'])
        self.assertEqual(states[-1]['state'], 'done')
        self.nowhere_else()
        m = models.on_drive(self.data)[0]
        self.assertEqual((m['id'], m['vision'], m['source']['repo']), ('drive:acme--talker-GGUF/talker-Q4_K_M.gguf', True, 'acme/talker-GGUF'))
        folder, fetched, _ = self.bring(dict(found, draft=None), 'talker-Q8_0.gguf')     # another version later: what is here is not brought again
        self.assertEqual([f[1] for f in fetched], ['talker-Q8_0.gguf'])
        self.assertEqual(len(json.loads((folder / 'model.json').read_text())['files']), 3)

    def test_no_room_no_download(self):
        Hub(FILES).patch(self)
        found = hf_import.inspect('acme/talker-GGUF', hw=aikit.HW16, data=self.data)
        with mock.patch.object(shutil, 'disk_usage', return_value=mock.Mock(free=GiB)):
            with self.assertRaisesRegex(hf_import.Refused, 'free'):
                self.bring(found)
        with self.assertRaisesRegex(hf_import.Refused, 'not in the repository'):
            hf_import.Import(found, 'other.gguf', data=self.data)
        evil = dict(found, versions=[dict(found['versions'][1], files=[{'path': 'sub/../../../escape.gguf', 'bytes': 10, 'sha256': 'x', 'part': 1}])], projector=None, draft=None)
        folder, fetched, _ = self.bring(evil, evil['versions'][0]['name'])
        self.assertEqual(fetched[0][1], 'escape.gguf')                                  # only ever the last part of a name, inside the model's own folder
        self.assertTrue((folder / 'escape.gguf').is_file())
        hidden = dict(found, versions=[dict(found['versions'][1], files=[{'path': '.hidden.gguf', 'bytes': 10, 'sha256': 'x', 'part': 1}])], projector=None, draft=None)
        with self.assertRaisesRegex(hf_import.Refused, 'will not write'):
            self.bring(hidden, hidden['versions'][0]['name'])

    def test_one_at_a_time_and_it_can_be_stopped(self):
        Hub(FILES).patch(self)
        found = dict(hf_import.inspect('acme/talker-GGUF', hw=aikit.HW16, data=self.data), draft=None, projector=None)
        gate, done = threading.Event(), []

        class Slow:
            def __init__(s, url, dest, control=None, progress=None, sha256=None):
                s.control, s.dest = control, Path(dest)

            def run(s):
                while not gate.wait(0.02):
                    s.control.check()
                aikit.write_model(s.dest, aikit.plain())
        jobs = hf_import.Jobs()
        with mock.patch.object(download, 'Fetch', Slow):
            self.assertEqual(jobs.status(), {'state': 'idle'})
            first = jobs.start(found, 'talker-Q4_K_M.gguf', data=self.data, on_done=done.append)
            self.assertEqual((first['state'], first['total_bytes']), ('downloading', int(2.5 * GiB)))
            with self.assertRaisesRegex(hf_import.Refused, 'already being brought'):
                jobs.start(found, 'talker-Q8_0.gguf', data=self.data)
            jobs.stop()
            jobs.thread.join(5)
            self.assertEqual(jobs.status()['state'], 'stopped')
            self.assertEqual(done, [])
            jobs.start(found, 'talker-Q4_K_M.gguf', data=self.data, on_done=done.append)
            gate.set()
            jobs.thread.join(5)
            self.assertEqual(jobs.status()['state'], 'done')
            self.assertEqual([d.name for d in done], ['acme--talker-GGUF'])
        with mock.patch.object(download, 'Fetch', side_effect=download.BadChecksum('The downloaded file did not match its published checksum.')):
            jobs.start(found, 'talker-Q8_0.gguf', data=self.data)
            jobs.thread.join(5)
            self.assertEqual(jobs.status()['state'], 'failed')
            self.assertIn('checksum', jobs.status()['message'])

    def test_only_a_model_in_the_library_can_be_removed(self):
        d = self.data / 'models'
        aikit.write_model(d / 'acme--talker' / 'talker-Q4_K_M.gguf', aikit.plain(), GiB)
        aikit.write_model(d / 'acme--talker' / 'mmproj-talker.gguf', {'general.architecture': 'clip'}, 2**20)
        aikit.write_model(d / 'loose-Q4_K_M.gguf', aikit.plain(), GiB)
        outside = aikit.write_model(self.tmp / 'outside-Q4_K_M.gguf', aikit.plain(), GiB)
        for bad in ('here:abcd', 'drive:../outside-Q4_K_M.gguf', 'drive:/etc/hosts', 'drive:not-there.gguf', 'drive:acme--talker', 'stock'):
            with self.assertRaises(hf_import.Refused, msg=bad):
                hf_import.remove(bad, data=self.data)
        self.assertTrue(outside.is_file())
        self.assertTrue(hf_import.remove('drive:acme--talker/talker-Q4_K_M.gguf', data=self.data))
        self.assertFalse((d / 'acme--talker').exists())                 # the model, its projector and its note together
        self.assertTrue(hf_import.remove('drive:loose-Q4_K_M.gguf', data=self.data))
        self.assertTrue(d.is_dir())                                     # never the models folder itself

    def test_the_three_offered_by_name_are_real_repository_names(self):
        self.assertEqual([p['id'] for p in hf_import.PRESETS], ['small', 'middle', 'large'])
        for p in hf_import.PRESETS:
            repo, quant = hf_import.parse(f"{p['repo']}:{p['quant']}")
            self.assertEqual(repo, p['repo'])
            self.assertTrue(p['title'] and p['about'])
        import urllib.parse
        self.assertIn(urllib.parse.urlsplit(hf_import.SITE).hostname, download.ALLOWED_HOSTS)


class Carried(Temp):
    """Onto the library's drive, so it answers on another computer."""

    def other(self):
        folder = self.tmp / 'anything'
        aikit.write_model(folder / 'snap' / 'talker-Q4_K_M.gguf', aikit.plain(name='Talker'), 3 * 2**20)
        aikit.write_model(folder / 'snap' / 'mmproj-talker-f16.gguf', {'general.architecture': 'clip'}, 2**20)
        p = mock.patch.object(models, 'places', return_value=[(folder, 'AnythingLLM')])
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.dict(os.environ, {'OLLAMA_MODELS': str(self.tmp / 'no-ollama')})
        p.start()
        self.addCleanup(p.stop)
        return folder

    def test_a_model_another_program_holds_is_copied_and_the_original_left_alone(self):
        from engine.ai import carry
        folder = self.other()
        m = models.found(self.data, hw=aikit.HW16)[0]
        before = sorted((p.name, p.stat().st_size, p.stat().st_mtime_ns) for p in folder.rglob('*') if p.is_file())
        told = []
        new_id = carry.model(m, self.data, progress=told.append)
        self.assertEqual(new_id, 'drive:talker-Q4_K_M/talker-Q4_K_M.gguf')
        dest = self.data / 'models' / 'talker-Q4_K_M'
        self.assertEqual(sorted(p.name for p in dest.iterdir()), ['mmproj-talker-f16.gguf', 'model.json', 'talker-Q4_K_M.gguf'])
        self.assertEqual((dest / 'talker-Q4_K_M.gguf').read_bytes(), Path(m['path']).read_bytes())
        self.assertEqual(json.loads((dest / 'model.json').read_text())['copied_from'], 'AnythingLLM')
        self.assertEqual(told[-1], {'state': 'done', 'done_bytes': 4 * 2**20, 'total_bytes': 4 * 2**20})
        self.assertEqual(before, sorted((p.name, p.stat().st_size, p.stat().st_mtime_ns) for p in folder.rglob('*') if p.is_file()))
        got = [x for x in models.found(self.data, hw=aikit.HW16) if x['where'] == 'drive']
        self.assertEqual((got[0]['id'], got[0]['vision']), (new_id, True))
        with self.assertRaisesRegex(carry.Refused, 'already in this library'):
            carry.model(got[0], self.data)
        with mock.patch.object(carry, '_copy', side_effect=AssertionError('copied twice')):
            self.assertEqual(carry.model(m, self.data), new_id)                     # what is already there whole is not copied again

    def test_a_copy_that_stops_leaves_no_half_file_and_no_room_is_said_first(self):
        from engine.ai import carry
        self.other()
        m = models.found(self.data, hw=aikit.HW16)[0]
        with self.assertRaisesRegex(carry.Refused, 'Stopped'):
            carry.model(m, self.data, cancelled=lambda: True)
        dest = self.data / 'models' / 'talker-Q4_K_M'
        self.assertEqual([p.name for p in dest.iterdir()], [])
        with mock.patch.object(carry, '_free', return_value=100):
            with self.assertRaisesRegex(carry.Refused, 'free'):
                carry.model(m, self.data)
        with self.assertRaisesRegex(carry.Refused, 'not where they were'):
            carry.model(dict(m, path=str(self.tmp / 'gone.gguf')), self.data)

    def test_a_model_ollama_names_by_its_digest_gets_a_name(self):
        from engine.ai import carry
        self.nowhere_else()
        blob = aikit.write_model(self.tmp / 'ollama/blobs/sha256-aaaa', aikit.plain(name='Llama'), 2**20)
        proj = aikit.write_model(self.tmp / 'ollama/blobs/sha256-bbbb', {'general.architecture': 'clip'}, 2**20)
        m = {'id': 'here:x', 'name': 'Llama Q4_K_M (llama:latest)', 'path': str(blob), 'projector': str(proj), 'draft': None, 'where': 'computer', 'origin': 'Ollama'}
        self.assertEqual(carry.model(m, self.data), 'drive:Llama-Q4_K_M-llama-latest/Llama-Q4_K_M-llama-latest.gguf')
        names = sorted(p.name for p in (self.data / 'models' / 'Llama-Q4_K_M-llama-latest').iterdir())
        self.assertEqual(names, ['Llama-Q4_K_M-llama-latest.gguf', 'Llama-Q4_K_M-llama-latest.mmproj.gguf', 'model.json'])
        self.assertTrue(models.on_drive(self.data)[0]['vision'])                    # found again as a model with its projector

    def test_the_program_that_runs_models_is_copied_with_its_libraries(self):
        from engine.ai import carry
        src = self.tmp / 'App/llamacpp/metal'
        src.mkdir(parents=True)
        for name, body in (('llama-server', b'#!/bin/sh\n'), ('libllama.0.dylib', b'lib'), ('libggml.so.1', b'lib'), ('LICENSE', b'MIT'), ('.version', b'1'),
                           ('notes.txt', b'not needed'), ('llama-cli', b'another tool')):
            (src / name).write_bytes(body)
        (src / 'llama-server').chmod(0o755)
        with mock.patch.dict(os.environ, {'LASTBOOK_LLAMA_SERVER': str(src / 'llama-server')}):
            got = carry.runtime(self.data)
        dest = self.data / 'models' / 'runtime' / runtime.platform_folder()
        self.assertEqual(got['folder'], str(dest))
        self.assertEqual(sorted(p.name for p in dest.iterdir()), ['.version', 'LICENSE', 'copied-from.json', 'libggml.so.1', 'libllama.0.dylib', 'llama-server'])
        self.assertTrue(os.access(dest / 'llama-server', os.X_OK))
        self.assertEqual(runtime.find(self.data), {'path': str(dest / 'llama-server'), 'origin': 'this library'})      # and it is the one used from now on
        with self.assertRaisesRegex(carry.Refused, 'already the one in use'):
            carry.runtime(self.data)
        with mock.patch.object(runtime, 'find', return_value=None):
            with self.assertRaisesRegex(carry.Refused, 'No program'):
                carry.runtime(self.tmp / 'elsewhere')

    def test_long_work_is_shown_and_can_be_stopped(self):
        jobs = hf_import.Jobs()
        gate = threading.Event()
        done = []

        def work(tell, cancelled):
            tell({'file': 'a.gguf', 'done_bytes': 5, 'total_bytes': 10})
            while not gate.wait(0.02):
                if cancelled():
                    raise hf_import.Refused('Stopped.')
            return 'drive:x'
        self.assertEqual(jobs.task('copying', 'Talker', work, done.append)['state'], 'copying')
        with self.assertRaisesRegex(hf_import.Refused, 'already'):
            jobs.task('copying', 'Other', work)
        time.sleep(0.1)
        self.assertEqual(jobs.status()['done_bytes'], 5)
        jobs.stop()
        jobs.thread.join(5)
        self.assertEqual((jobs.status()['state'], done), ('stopped', []))
        jobs.task('copying', 'Talker', work, done.append)
        gate.set()
        jobs.thread.join(5)
        self.assertEqual((jobs.status()['state'], done), ('done', ['drive:x']))
        jobs.task('copying', 'Talker', lambda tell, cancelled: 1 / 0)
        jobs.thread.join(5)
        self.assertEqual(jobs.status()['state'], 'failed')
        self.assertIn('ZeroDivisionError', jobs.status()['message'])


class Requests(Temp):
    """/api/ai/*: only from the computer the library is open on."""

    def setUp(self):
        super().setUp()
        self.nowhere_else()
        aikit.write_model(self.data / 'models' / 'talker-Q4_K_M.gguf', aikit.plain(name='Talker'), GiB)
        self.p = picker.Picker(self.data)
        self.p._hw = aikit.HW16
        for patch in (mock.patch.object(assistant, 'PICKER', self.p), mock.patch.object(assistant, 'LOCAL', None), mock.patch.object(assistant, '_server_online', return_value=False),
                      mock.patch.object(runtime, 'find', return_value={'path': '/x/llama-server', 'origin': 'test'}), mock.patch.object(server, 'AI_SEEN', {}),
                      mock.patch.object(server, 'AI_JOBS', hf_import.Jobs())):
            patch.start()
            self.addCleanup(patch.stop)

    def post(self, path, body, client='127.0.0.1'):
        status, _, raw = ask('POST', path, body, client=client)
        return status, json.loads(raw or b'{}')

    def test_the_picker_is_shown_here_and_not_to_a_phone(self):
        status, _, raw = ask('GET', '/api/ai/models')
        d = json.loads(raw)
        self.assertEqual((status, d['available'], d['selected'], d['active']), (200, True, 'auto', 'drive:talker-Q4_K_M.gguf'))
        self.assertEqual([m['name'] for m in d['models']], ['Talker Q4_K_M'])
        self.assertEqual([p['id'] for p in d['presets']], ['small', 'middle', 'large'])
        self.assertEqual(d['bringing'], {'state': 'idle'})
        self.assertNotIn(str(self.tmp), raw.decode())
        with mock.patch.object(server, 'LAN_KEY', 'k' * 24):
            phone, cookie = '192.168.1.50', [f'Cookie: sg_key={"k" * 24}']        # a phone the library is shared with, holding the key
            status, _, raw = ask('GET', '/api/ai/models', client=phone, headers=cookie)
            self.assertEqual((status, json.loads(raw)), (200, {'available': False}))
            for path in ('/api/ai/select', '/api/ai/inspect', '/api/ai/import', '/api/ai/remove', '/api/ai/stop', '/api/ai/import/stop'):
                self.assertEqual(ask('POST', path, {'id': 'none', 'repo': 'acme/talker-GGUF'}, client=phone, headers=cookie)[0], 403, path)
            self.assertEqual(ask('GET', '/api/ai/import', client=phone, headers=cookie)[0], 403)
            self.assertEqual(json.loads(ask('GET', '/api/assistant/status', client=phone, headers=cookie)[2])['model'], 'Talker Q4_K_M')   # it may know what answers
        self.assertEqual(self.p.choice(), {'id': 'auto'})

    def test_choosing(self):
        status, d = self.post('/api/ai/select', {'id': 'none'})
        self.assertEqual((status, d['selected'], d['active']), (200, 'none', 'none'))
        self.assertEqual(json.loads(ask('GET', '/api/assistant/status')[2])['off'], True)
        status, d = self.post('/api/ai/select', {'id': 'drive:talker-Q4_K_M.gguf'})
        self.assertEqual((status, d['active']), (200, 'drive:talker-Q4_K_M.gguf'))
        self.assertEqual(self.post('/api/ai/select', {'id': 'drive:gone.gguf'})[0], 404)
        self.assertEqual(self.post('/api/ai/select', {})[0], 404)
        self.assertEqual(self.post('/api/ai/nothing', {})[0], 404)
        self.assertEqual(ask('POST', '/api/ai/select', b'not json')[0], 400)

    def test_looking_bringing_and_removing(self):
        self.assertEqual(self.post('/api/ai/import', {'repo': 'acme/talker-GGUF'}), (400, {'error': 'Look at the repository first.'}))
        Hub(FILES).patch(self)
        status, f = self.post('/api/ai/inspect', {'repo': 'acme/talker-GGUF'})
        self.assertEqual((status, f['recommended']), (200, 'talker-Q4_K_M.gguf'))
        status, e = self.post('/api/ai/inspect', {'repo': 'nobody/nothing'})
        self.assertEqual(status, 400)
        self.assertIn('no repository by that name', e['error'])
        with mock.patch.object(download, 'get_small', side_effect=download.Failed('network', 'The download server could not be reached.')):
            self.assertEqual(self.post('/api/ai/inspect', {'repo': 'acme/talker-GGUF'})[0], 502)

        class Fetch:
            def __init__(s, url, dest, control=None, progress=None, sha256=None):
                s.dest = Path(dest)

            def run(s):
                aikit.write_model(s.dest, aikit.plain(name='Hub Model') if 'mmproj' not in s.dest.name else {'general.architecture': 'clip'}, 2 * GiB if 'mmproj' not in s.dest.name else 2**20)
        with mock.patch.object(download, 'Fetch', Fetch), mock.patch.object(hf_import.models, 'folder', return_value=self.data / 'models'):
            status, job = self.post('/api/ai/import', {'repo': 'acme/talker-GGUF'})
            self.assertEqual((status, job['state'], job['version']), (200, 'downloading', 'talker-Q4_K_M.gguf'))
            server.AI_JOBS.thread.join(5)
        self.assertEqual(json.loads(ask('GET', '/api/ai/import')[2])['state'], 'done')
        self.assertEqual(self.p.choice()['id'], 'drive:acme--talker-GGUF/talker-Q4_K_M.gguf')       # what was just brought is what answers
        folder = self.data / 'models' / 'acme--talker-GGUF'
        self.assertEqual(sorted(p.name for p in folder.iterdir()), ['mmproj-talker-F16.gguf', 'model.json', 'talker-Q4_K_M.gguf'])   # no draft unless asked for
        with mock.patch.object(hf_import.models, 'folder', return_value=self.data / 'models'):
            status, d = self.post('/api/ai/remove', {'id': 'drive:acme--talker-GGUF/talker-Q4_K_M.gguf'})
        self.assertEqual((status, d['selected']), (200, 'auto'))
        self.assertFalse(folder.exists())
        self.assertEqual(self.post('/api/ai/remove', {'id': 'here:abc'})[0], 400)


if __name__ == '__main__':
    unittest.main()
