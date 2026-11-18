#!/usr/bin/env python3
"""Build your own library: the questions the page asks (/api/wizard/*), what each answer is checked against, and a build
run in a process of its own, paused, carried on and finished.

  PYTHONDONTWRITEBYTECODE=1 <venv python> -B app/tests/test_builder.py [-v]

The pack is made up (buildkit.py), the published one is on test_wizard.py's server on this computer, and the AI model is a
file that only opens like one (aikit.py). Nothing here reaches the network. Never packaged."""
import http.server, json, os, shutil, subprocess, sys, tempfile, threading, time, unittest, urllib.error, urllib.request, zipfile
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'app'), str(HERE)]
from engine import portable  # noqa: E402
from engine.ai import models, picker  # noqa: E402
from engine.mapbuild import catalog  # noqa: E402
from engine.setup import api as setup_api, hardware  # noqa: E402
from engine.setup import manifest as manifests  # noqa: E402
from engine.wizard import api, build, contents, drives, hf_import, packzip, worker  # noqa: E402
import aikit  # noqa: E402
import buildkit  # noqa: E402
import test_wizard  # noqa: E402

LIB = 'library-data/'
SMALL = {'documents': ['water'], 'encyclopedias': ['collections/small_wiki.zim'], 'videos': {'default': 'words'}}
REGIONS = {'sizes_as_of': '2026-11-18', 'regions': [
    {'id': 'europe', 'name': 'Europe', 'parent': None, 'box': [-10, 35, 30, 70], 'iso': [], 'composite': False, 'parts': False, 'mb': 600.0, 'summed': True},
    {'id': 'isle-of-man', 'name': 'Isle of Man', 'parent': 'europe', 'box': [-5.0, 54.0, -4.0, 54.5], 'iso': ['IM'], 'composite': False, 'parts': False, 'mb': 6.0},
    {'id': 'faroe-islands', 'name': 'Faroe Islands', 'parent': 'europe', 'box': [-8.0, 61.0, -6.0, 63.0], 'iso': ['FO'], 'composite': False, 'parts': False, 'mb': 8.0},
    {'id': 'nowhere', 'name': 'Nowhere', 'parent': 'europe', 'box': [0, 0, 1, 1], 'iso': [], 'composite': False, 'parts': False}]}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='lb-builder-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        from engine.setup import download as dl
        for p in (mock.patch.object(contents, 'BIG_CHANNEL', 500_000), mock.patch.object(models, 'places', return_value=[]),
                  mock.patch.object(dl, 'ALLOWED_HOSTS', frozenset()), mock.patch.object(dl, 'ALLOWED_SUFFIXES', ()),     # no test here reaches a real server
                  mock.patch.object(api, '_regions_catalog', return_value=json.loads(json.dumps(REGIONS))),
                  mock.patch.object(drives, 'listed', return_value=[]), mock.patch.dict(os.environ, {'OLLAMA_MODELS': str(self.tmp / 'no-ollama')})):
            p.start()
            self.addCleanup(p.stop)
        self.recs, self.files = buildkit.write_pack(self.tmp / 'pack')
        self.source = {'kind': 'folder', 'path': str(self.tmp / 'pack')}
        self.w = self.wizard()
        self.addCleanup(self.finish)

    def wizard(self, current=None, **more):
        m = manifests.Manifest(bundled=more.pop('bundled', None) or {'version': 1, 'published': '2026-11-01', 'packs': [], 'models': [], 'maps': [], 'address_indexes': []}, remote=None)
        s = setup_api.Setup('manage', self.tmp / 'settings', self.tmp / 'home', current=current, manifest=m, **more)
        return api.Wizard(s)

    def finish(self):
        """No build outlives its test."""
        try:
            self.w.stop('pause')
        except Exception:
            pass
        if self.w.proc is not None:
            try:
                self.w.proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.w.proc.kill()

    def until(self, states, seconds=60):
        end = time.time() + seconds
        while time.time() < end:
            st = self.w.status()
            if st and st['state'] in states:
                return st
            time.sleep(0.1)
        self.fail(f'the build did not reach {states}: {self.w.status()}\n{self.log()}')

    def log(self):
        out = ''
        for name in ('log.txt', 'worker-errors.txt'):
            try:
                out += (self.w.dir / name).read_text(encoding='utf-8')[-3000:]
            except OSError:
                pass
        return out


class Where(Base):
    def test_a_library_gets_a_folder_of_its_own(self):
        (self.tmp / 'somewhere').mkdir()
        t = self.w.target(str(self.tmp / 'somewhere'))
        self.assertEqual(t['path'], str(self.tmp / 'somewhere' / 'LastbookData'))
        self.assertEqual((t['exists'], t['library'], t['ok'], t['writable'], t['built']), (False, False, True, True, None))
        self.assertGreater(t['free_bytes'], 0)
        self.assertEqual(self.w.target(str(self.tmp / 'x' / 'LastbookData'))['path'], str(self.tmp / 'x' / 'LastbookData'))     # named so already
        t = self.w.target(str(self.tmp / 'pack'))                   # a library whatever it is called: built in, not beside
        self.assertEqual((t['path'], t['library']), (str(self.tmp / 'pack'), True))

    def test_what_is_not_a_place(self):
        for bad in ('', '   ', None, 'relative/folder'):
            with self.assertRaises(api.Refused, msg=repr(bad)):
                self.w.target(bad)

    def test_a_folder_that_cannot_be_saved_to(self):
        locked = self.tmp / 'locked'
        locked.mkdir()
        os.chmod(locked, 0o500)
        self.addCleanup(os.chmod, locked, 0o700)
        if os.access(locked, os.W_OK):
            self.skipTest('this user can write anywhere')
        t = self.w.target(str(locked))
        self.assertEqual((t['ok'], t['writable']), (False, False))
        self.assertIn('cannot save', t['note'])

    def test_a_build_begun_there_is_offered_again(self):
        lib = self.tmp / 'd' / 'LastbookData'
        (lib / 'derived' / 'wizard').mkdir(parents=True)
        (lib / 'derived' / 'wizard' / 'build.json').write_text(json.dumps({'choice': SMALL, 'state': {'phase': 'videos'}, 'at': '2026-11-18T10:00:00'}))
        t = self.w.target(str(lib))
        self.assertEqual(t['built'], {'choice': SMALL, 'phase': 'videos', 'at': '2026-11-18T10:00:00'})

    def test_this_computer_is_not_the_open_library(self):
        home = self.tmp / 'home' / 'LastbookData'
        self.assertEqual(self.w.state()['default_target'], str(home))
        (home / 'library-data').mkdir(parents=True)
        (home / 'library-data' / 'catalog.json').write_text('[]')
        w = self.wizard(current=home)
        st = w.state()
        self.assertEqual(st['default_target'], str(self.tmp / 'home' / 'Lastbook 2' / 'LastbookData'))      # beside it, never on top of it
        self.assertEqual(st['current'], {'path': str(home), 'portable': False})
        self.assertTrue(w.target(str(home))['is_current'])

    def test_what_a_format_means(self):
        self.assertEqual({k: drives.judge('exfat')[k] for k in ('ok', 'travels')}, {'ok': True, 'travels': 'all'})
        fat = drives.judge('fat32')
        self.assertFalse(fat['ok'])
        self.assertIn('4 GB', fat['note'])
        with mock.patch.object(sys, 'platform', 'darwin'):
            self.assertFalse(drives.judge('ntfs')['ok'])            # a Mac reads it and cannot write it
        with mock.patch.object(sys, 'platform', 'win32'):
            self.assertEqual({k: drives.judge('ntfs')[k] for k in ('ok', 'travels')}, {'ok': True, 'travels': 'windows'})
        self.assertEqual(drives.judge('apfs')['travels'], 'mac')
        self.assertEqual(drives.judge('ext')['travels'], 'linux')
        self.assertFalse(drives.judge('network')['ok'])
        self.assertTrue(drives.judge(None)['ok'])                   # not known is not refused: the person is told it is not known
        self.assertIn('cannot tell', drives.judge(None)['note'])

    @unittest.skipIf(os.name == 'nt', 'a Mac\'s and Linux\'s way of naming disks: Windows is asked another way')
    def test_which_disk_a_folder_is_on(self):
        mounts = [('/', 'apfs', ['local']), ('/Volumes/Stick', 'msdos', ['local']), ('/Volumes/Stick 2', 'exfat', ['local'])]
        with mock.patch.object(sys, 'platform', 'darwin'), mock.patch.object(drives, '_mac_mounts', return_value=mounts), \
                mock.patch.object(drives, '_existing', side_effect=lambda p: Path(p)), mock.patch.object(Path, 'resolve', lambda self: self):
            self.assertEqual(drives.filesystem('/Volumes/Stick/LastbookData'), 'fat32')
            self.assertEqual(drives.filesystem('/Volumes/Stick 2/a/b'), 'exfat')        # not the drive whose name it begins with
            self.assertEqual(drives.filesystem('/Users/someone/Documents'), 'apfs')

    def test_a_fat32_stick_is_refused(self):
        with mock.patch.object(drives, 'filesystem', return_value='fat32'):
            t = self.w.target(str(self.tmp / 'stick'))
        self.assertFalse(t['ok'])
        with mock.patch.object(drives, 'filesystem', return_value='fat32'), self.assertRaises(api.Refused) as e:
            self.w.start(self.source, SMALL, str(self.tmp / 'stick'))
        self.assertIn('FAT32', str(e.exception))
        self.assertIsNone(self.w.status())


class Chosen(Base):
    def test_the_menu_of_a_library_on_a_disk(self):
        m = self.w.menu(self.source)
        self.assertEqual([d['id'] for d in m['documents']], ['medical', 'water', 'food', 'aviation', 'science'])
        self.assertEqual([z['id'] for z in m['encyclopedias']], ['collections/small_wiki.zim', 'collections/big_wiki.zim'])
        self.assertEqual({g['id'] for g in m['videos']}, {'channel:Health Films', 'channel:Corporals Corner', 'topic:science'})
        self.assertEqual((m['remote'], m['title'], m['modes']), (False, 'pack', ['small', 'original', 'words', 'none']))
        self.assertIs(self.w._pack(self.source)[0], self.w._pack(self.source)[0])       # read once

    def test_what_is_not_a_library_to_build_from(self):
        (self.tmp / 'empty').mkdir()
        for bad in (None, {}, {'kind': 'folder', 'path': str(self.tmp / 'empty')}, {'kind': 'pack', 'id': 'all'}, {'kind': 'url', 'url': 'https://example.org/x.zip'}):
            with self.assertRaises(api.Refused, msg=repr(bad)):
                self.w.menu(bad)
        (self.tmp / 'pack' / 'pack-manifest.tsv').unlink()
        with self.assertRaises(api.Refused) as e:
            self.wizard().menu(self.source)
        self.assertIn('pack-manifest.tsv', str(e.exception))

    def test_the_folder_above_a_library_will_do(self):
        os.rename(self.tmp / 'pack', self.tmp / 'LastbookData')
        m = self.w.menu({'kind': 'folder', 'path': str(self.tmp)})
        self.assertEqual(len(m['documents']), 5)

    def test_a_choice_comes_to_a_size(self):
        target = str(self.tmp / 'out')
        p = self.w.plan(self.source, SMALL, target)
        pack = self.w._pack(self.source)[0]
        self.assertEqual(p['pack_bytes'], pack.plan(SMALL)['bytes'])
        self.assertEqual((p['bytes'], p['map_bytes'], p['ai_bytes'], p['download_bytes'], p['working_bytes']), (p['pack_bytes'], 0, 0, 0, 0))     # from a disk: nothing downloaded
        self.assertEqual(p['counts']['documents'], 1)
        self.assertEqual((p['fits'], p['short_bytes'], p['videos_to_shrink']), (True, 0, 0))
        self.assertEqual(p['target']['path'], str(self.tmp / 'out' / 'LastbookData'))
        more = self.w.plan(self.source, dict(SMALL, documents='all'), target)
        self.assertGreater(more['bytes'], p['bytes'])

    def test_maps_add_what_they_are_estimated_at(self):
        p = self.w.plan(self.source, dict(SMALL, maps=[{'id': 'isle-of-man', 'level': 'detail'}, {'id': 'faroe-islands', 'level': 'overview', 'terrain': False}]), str(self.tmp / 'out'))
        iom = catalog.estimate(REGIONS['regions'][1], 'detail', True)
        faroe = catalog.estimate(REGIONS['regions'][2], 'overview', False)
        self.assertEqual(iom['bytes'], int((6.0 * 2.75 + 0.5 * 3.3) * 1e6))
        self.assertEqual(faroe['bytes'], int(8.0 * 0.17 * 1e6))
        self.assertEqual(p['map_bytes'], iom['bytes'] + faroe['bytes'])
        self.assertEqual(p['bytes'], p['pack_bytes'] + p['map_bytes'])
        coast = int(catalog.LAND_MB * 1e6)
        self.assertEqual((p['download_bytes'], p['coast_bytes']), (iom['download_bytes'] + faroe['download_bytes'] + coast, coast))     # the coastline once, not once a region
        self.assertEqual(p['working_bytes'], p['download_bytes'] + int(catalog.LAND_UNPACKED_MB * 1e6))
        self.assertEqual((p['maps'], p['map_minutes']), (2, 2))
        self.assertIsNone(catalog.estimate(REGIONS['regions'][3]))          # a region whose extract's size is not known: no number is made up
        nowhere = self.w.plan(self.source, dict(SMALL, maps=[{'id': 'nowhere'}]), str(self.tmp / 'out'))
        self.assertEqual(nowhere['map_bytes'], 0)

    def test_a_choice_is_read_not_trusted(self):
        target = str(self.tmp / 'out')
        for bad in (None, 'all', dict(SMALL, documents=7), dict(SMALL, documents=['x' * 400]), dict(SMALL, videos={'default': 'tiny'}), dict(SMALL, videos='small'),
                    dict(SMALL, maps=[{'id': 'atlantis'}]), dict(SMALL, maps=['isle-of-man']), dict(SMALL, ai={'mode': 'computer', 'id': 'here:nothing'}),
                    dict(SMALL, ai={'mode': 'download', 'repo': 'not a repo'}), dict(SMALL, ai={'mode': 'download', 'repo': 'someone/unseen'}), dict(SMALL, ai={'mode': 'magic'})):
            with self.assertRaises(api.Refused, msg=repr(bad)):
                self.w.plan(self.source, bad, target)
        _, clean, _ = self.w.plan(self.source, dict(SMALL, encoder='hardware', threads=64, map_workers=99, portable=1, surprise='x',
                                                    maps=[{'id': 'isle-of-man', 'level': 'sideways', 'terrain': 0, 'extra': 1}]), target, _full=True)
        self.assertEqual(set(clean), {'documents', 'encyclopedias', 'videos', 'encoder', 'portable', 'keep_sources', 'maps', 'ai'})      # nothing the page added goes to the build
        self.assertEqual(clean['maps'], [{'id': 'isle-of-man', 'name': 'Isle of Man', 'level': 'detail', 'terrain': False}])
        self.assertEqual((clean['portable'], clean['ai']), (True, {'mode': 'none'}))
        with mock.patch.object(self.w, 'tools', return_value={'video': True, 'video_hardware': False, 'maps': True, 'ai_runtime': False}):
            self.assertEqual(self.w.plan(self.source, dict(SMALL, encoder='hardware'), target)['encoder'], 'software')

    def test_no_room(self):
        with mock.patch.object(hardware, 'disk_of', return_value=(1_000_000, 10**12)):
            p = self.w.plan(self.source, dict(SMALL, documents='all'), str(self.tmp / 'out'))
            self.assertEqual((p['fits'], p['short_bytes']), (False, p['bytes'] + api.SPARE - 1_000_000))
            with self.assertRaises(api.Refused) as e:
                self.w.start(self.source, dict(SMALL, documents='all'), str(self.tmp / 'out'))
        self.assertEqual(e.exception.status, 507)
        self.assertIsNone(self.w.status())

    def test_videos_made_smaller_need_an_encoder_and_time(self):
        choice = {'documents': [], 'encyclopedias': [], 'videos': {'default': 'small'}}
        with mock.patch.object(self.w, 'tools', return_value={'video': False, 'video_hardware': False, 'maps': True, 'ai_runtime': False}), self.assertRaises(api.Refused) as e:
            self.w.plan(self.source, choice, str(self.tmp / 'out'))
        self.assertIn('ffmpeg', str(e.exception))
        with mock.patch.object(self.w, 'tools', return_value={'video': True, 'video_hardware': True, 'maps': True, 'ai_runtime': False}), mock.patch.object(os, 'cpu_count', return_value=10):
            slow = self.w.plan(self.source, choice, str(self.tmp / 'out'))
            fast = self.w.plan(self.source, dict(choice, encoder='hardware'), str(self.tmp / 'out'))
        self.assertEqual((slow['videos_to_shrink'], slow['video_hours']), (2, round(8 / 3600, 1)))
        self.assertEqual(slow['encoder'], 'software')
        self.assertEqual(fast['encoder'], 'hardware')

    def test_a_published_library_is_read_in_place(self):
        srv = test_wizard.Base
        srv.setUpClass()
        self.addCleanup(srv.tearDownClass)
        for p in (mock.patch.object(packzip, 'BIG', 64 * 1024), mock.patch.object(packzip, 'RUN_MAX', 256 * 1024), mock.patch.object(packzip, 'GAP', 4 * 1024)):
            p.start()
            self.addCleanup(p.stop)
        body = test_wizard.make_zip([(p, b, zipfile.ZIP_STORED) for p, b in self.files.items()])
        srv.files.reset()
        srv.files.data['Lastbook-Made.zip'] = body
        bundled = manifests.validate({'version': 1, 'published': '2026-11-01', 'models': [], 'packs': [
            {'id': 'made', 'title': 'Made up', 'summary': 'For a test.', 'records': len(self.recs), 'available': True, 'installed_bytes': len(body),
             'files': [{'name': 'Lastbook-Made.zip', 'url': srv.base + '/Lastbook-Made.zip', 'size': len(body), 'sha256': buildkit.sha(body)}]},
            {'id': 'later', 'title': 'Later', 'available': False, 'files': []}]}, test_wizard.ALLOWED)
        w = self.wizard(bundled=bundled, allowed=test_wizard.ALLOWED, context=srv.ctx)
        self.assertEqual([(s['kind'], s['id'], s['title']) for s in w.state()['sources']], [('pack', 'made', 'Made up')])     # one not published yet is not offered
        m = w.menu({'kind': 'pack', 'id': 'made'})
        self.assertEqual((m['remote'], m['title'], len(m['documents'])), (True, 'Made up', 5))
        asked = sum(int(r['Range'][6:].split('-')[1]) - int(r['Range'][6:].split('-')[0]) + 1 for n, r in srv.files.log if isinstance(r, dict) and r.get('Range', '').startswith('bytes=') and r['Range'][6:].split('-')[1])
        self.assertLess(asked, 0.6 * len(body))             # the list of what is in it, not the pack
        p = w.plan({'kind': 'pack', 'id': 'made'}, SMALL, str(self.tmp / 'out'))
        whole_index = len(self.files[LIB + 'search.sqlite'])
        copied = sum(b for _, b, _ in w._pack({'kind': 'pack', 'id': 'made'})[0].plan(SMALL)['copy'])
        self.assertEqual(p['download_bytes'], copied + whole_index)        # what is kept, and the whole index its own is made from
        self.assertEqual(p['working_bytes'], whole_index)
        with self.assertRaises(api.Refused):
            w.menu({'kind': 'pack', 'id': 'later'})


class Kept(Base):
    """A library that is there already, kept as it is: maps and an AI model are added to it."""

    def test_only_maps_and_ai_count(self):
        p = self.w.plan({'kind': 'keep'}, {'maps': [{'id': 'isle-of-man'}]}, str(self.tmp / 'pack'))
        self.assertEqual((p['pack_bytes'], p['bytes']), (0, p['map_bytes']))
        self.assertEqual(p['already_bytes'], 0)             # what the library holds is not "already built"
        m = self.w.menu({'kind': 'keep'})
        self.assertEqual((m['keep'], m['documents'], m['videos']), (True, [], []))

    def test_what_cannot_be_started(self):
        with self.assertRaises(api.Refused) as e:           # nothing to add
            self.w.start({'kind': 'keep'}, {}, str(self.tmp / 'pack'))
        self.assertIn('Nothing is chosen', str(e.exception))
        with self.assertRaises(api.Refused) as e:           # nothing to add to
            self.w.start({'kind': 'keep'}, {'maps': [{'id': 'isle-of-man'}]}, str(self.tmp / 'nothing-here'))
        self.assertIn('no library', str(e.exception))
        with self.assertRaises(api.Refused) as e:           # from itself
            self.w.start(self.source, SMALL, str(self.tmp / 'pack'))
        self.assertIn('from itself', str(e.exception))
        w = self.wizard(current=self.tmp / 'pack')          # the one that is open: its catalog is not rewritten under the app
        (self.tmp / 'other').mkdir()
        buildkit.write_pack(self.tmp / 'other')
        with self.assertRaises(api.Refused) as e:
            w.start({'kind': 'folder', 'path': str(self.tmp / 'other')}, SMALL, str(self.tmp / 'pack'))
        self.assertIn('open', str(e.exception))
        with mock.patch.object(self.w, 'tools', return_value={'video': True, 'video_hardware': False, 'maps': False, 'ai_runtime': False}), self.assertRaises(api.Refused) as e:
            self.w.start({'kind': 'keep'}, {'maps': [{'id': 'isle-of-man'}]}, str(self.tmp / 'pack'))
        self.assertIn('cannot build maps', str(e.exception))
        self.assertIsNone(self.w.status())

    def test_a_model_from_this_computer_goes_into_the_library_and_answers(self):
        theirs = self.tmp / 'AnythingLLM'
        aikit.write_model(theirs / 'lab' / 'Tiny-Q4_K_M.gguf', size=60 * 2**20)
        with mock.patch.object(models, 'places', return_value=[(theirs, 'AnythingLLM')]):
            got = self.w.ai(str(self.tmp / 'pack'))
            self.assertEqual([(m['file'], m['origin']) for m in got['here']], [('Tiny-Q4_K_M.gguf', 'AnythingLLM')])
            self.assertNotIn('path', got['here'][0])                # the page is told what it is, not where it is
            self.assertEqual([p['id'] for p in got['presets']], ['compact', 'balanced', 'workstation'])
            mid = got['here'][0]['id']
            p = self.w.plan({'kind': 'keep'}, {'ai': {'mode': 'computer', 'id': mid}}, str(self.tmp / 'pack'))
            self.assertEqual((p['ai_bytes'], p['bytes']), (60 * 2**20, 60 * 2**20))
            linked = self.w.plan({'kind': 'keep'}, {'ai': {'mode': 'computer', 'id': mid, 'copy': False}}, str(self.tmp / 'pack'))
            self.assertEqual(linked['ai_bytes'], 0)                 # used where it is: nothing added
            status = build.Build(self.tmp / 'pack', None, {'ai': {'mode': 'computer', 'id': mid, 'copy': True}}).run()
        self.assertEqual((status['phase'], status['problems']), ('done', []))
        there = models.on_drive(self.tmp / 'pack')
        self.assertEqual([m['file'] for m in there], ['Tiny-Q4_K_M.gguf'])
        self.assertEqual(picker.Picker(self.tmp / 'pack').choice()['id'], there[0]['id'])       # and it is the one chosen
        self.assertEqual(json.loads((self.tmp / 'pack' / 'library-data' / 'catalog.json').read_text()), self.recs)    # the library itself untouched

    def test_a_model_that_has_gone_is_a_problem_not_a_failure(self):
        status = build.Build(self.tmp / 'pack', None, {'ai': {'mode': 'computer', 'id': 'here:gone'}}).run()
        self.assertEqual(status['phase'], 'done')
        self.assertEqual(len(status['problems']), 1)
        self.assertIn('not on this computer any more', status['problems'][0])

    def test_a_model_from_hugging_face_is_sized_from_what_was_looked_at(self):
        found = {'repo': 'someone/tiny-GGUF', 'revision': 'abc', 'name': 'Tiny', 'recommended': 'tiny-Q4_K_M.gguf', 'license': 'mit',
                 'versions': [{'name': 'tiny-Q4_K_M.gguf', 'quant': 'Q4_K_M', 'bytes': 500, 'files': [], 'total_bytes': 650},
                              {'name': 'tiny-Q8_0.gguf', 'quant': 'Q8_0', 'bytes': 900, 'files': [], 'total_bytes': 1050}],
                 'projector': {'bytes': 100, 'path': 'mmproj.gguf'}, 'draft': {'bytes': 50, 'path': 'mtp.gguf'}}
        with mock.patch.object(hf_import, 'inspect', return_value=found) as seen:
            self.assertEqual(self.w.inspect('https://huggingface.co/someone/tiny-GGUF', str(self.tmp / 'pack')), found)
        self.assertEqual(seen.call_args.kwargs['data'], self.tmp / 'pack')
        plan = lambda **ai: self.w.plan({'kind': 'keep'}, {'ai': dict({'mode': 'download', 'repo': 'someone/tiny-GGUF'}, **ai)}, str(self.tmp / 'pack'), _full=True)
        p, clean, _ = plan()
        self.assertEqual((p['ai_bytes'], p['download_bytes']), (600, 600))                      # the suggested version and what lets it read pictures
        self.assertEqual(clean['ai'], {'mode': 'download', 'repo': 'someone/tiny-GGUF', 'version': 'tiny-Q4_K_M.gguf', 'draft': False, 'name': 'Tiny Q4_K_M'})
        self.assertEqual(plan(version='tiny-Q8_0.gguf', draft=True)[0]['ai_bytes'], 1050)
        with self.assertRaises(api.Refused):
            plan(version='tiny-Q2.gguf')
        with mock.patch.object(hf_import, 'inspect', side_effect=hf_import.Refused('That repository has no GGUF files.')), self.assertRaises(api.Refused) as e:
            self.w.inspect('someone/none')
        self.assertIn('no GGUF', str(e.exception))

    def test_regions_built_there_are_named(self):
        r = self.tmp / 'pack' / 'derived' / 'geo' / 'regions' / 'isle-of-man'
        r.mkdir(parents=True)
        (r / 'region.json').write_text(json.dumps({'id': 'isle-of-man', 'geofabrik': 'isle-of-man', 'name': 'Isle of Man', 'level': 'detail', 'bytes': 123, 'data_as_of': '2026-11-10T21:00:00Z'}))
        (r.parent / '.building-faroe-islands').mkdir()
        got = self.w.regions(str(self.tmp / 'pack'))
        self.assertEqual(got['built'], [{'id': 'isle-of-man', 'folder': 'isle-of-man', 'name': 'Isle of Man', 'level': 'detail', 'bytes': 123, 'data_as_of': '2026-11-10'}])
        self.assertEqual((len(got['regions']), got['sizes_as_of'], got['land_mb']), (4, '2026-11-18', catalog.LAND_MB))
        self.assertEqual(self.w.regions()['built'], [])


class Built(Base):
    """The build, in a process of its own."""

    def start(self, choice=SMALL, **more):
        return self.w.start(self.source, choice, str(self.tmp / 'out'), **more)

    def test_it_builds_and_says_so(self):
        st = self.start()
        self.assertIn(st['state'], ('running', 'done'))
        self.assertEqual(st['job']['target'], str(self.tmp / 'out' / 'LastbookData'))
        st = self.until(('done', 'failed', 'paused'))
        self.assertEqual((st['state'], st['phase'], st['problems'], st['error']), ('done', 'done', [], None), self.log())
        lib = self.tmp / 'out' / 'LastbookData' / 'library-data'
        ids = {r['id'] for r in json.loads((lib / 'catalog.json').read_text())}
        self.assertEqual(ids, {'water-guide', 'small-wiki', 'video-bandage', 'metadata-video-bandage', 'transcript-bandage', 'video-campfire', 'metadata-video-campfire',
                               'transcript-campfire', 'video-lecture', 'metadata-video-lecture'})
        self.assertEqual((lib / 'documents/water-sanitation/water.pdf').read_bytes(), self.files[LIB + 'documents/water-sanitation/water.pdf'])
        self.assertEqual(self.w.proc.wait(timeout=20), 0)
        self.assertEqual(self.w.state()['build']['state'], 'done')
        self.assertEqual(self.w.target(str(self.tmp / 'out'))['built']['phase'], 'done')
        self.assertEqual(self.w.status()['job']['source'], self.source)             # as the page asked, for "carry on"
        log = (self.w.dir / 'log.txt').read_text()
        self.assertIn('ended: done', log)
        self.assertEqual((self.w.dir / 'worker-errors.txt').read_text(), '')

    def test_a_second_build_waits_for_the_first(self):
        slow = mock.patch.object(self.w, '_running', return_value=True)
        with slow, self.assertRaises(api.Refused) as e:
            self.start()
        self.assertEqual(e.exception.status, 409)

    def test_paused_before_it_began_then_carried_on(self):
        real = self.w._spawn

        def late():             # the person presses Pause before the build's process has started
            (self.w.dir / 'stop').write_text('paused')
            return real()
        with mock.patch.object(self.w, '_spawn', side_effect=late):
            self.start(dict(SMALL, documents='all'))
        st = self.until(('paused', 'done', 'failed'))
        self.assertEqual(st['state'], 'paused', self.log())
        self.assertFalse((self.tmp / 'out' / 'LastbookData' / 'library-data' / 'catalog.json').exists())
        self.w.proc.wait(timeout=20)
        st = self.w.resume()
        self.assertEqual(st['job']['choice']['documents'], 'all')                   # the same choices
        st = self.until(('done', 'failed', 'paused'))
        self.assertEqual((st['state'], st['problems']), ('done', []), self.log())
        self.assertTrue((self.tmp / 'out' / 'LastbookData' / 'library-data' / 'documents/aviation/glider.pdf').is_file())

    def test_cancelled_and_forgotten(self):
        real = self.w._spawn

        def late():
            (self.w.dir / 'stop').write_text('cancelled')
            return real()
        with mock.patch.object(self.w, '_spawn', side_effect=late):
            self.start()
        st = self.until(('cancelled', 'done', 'failed', 'paused'))
        self.assertEqual(st['state'], 'cancelled', self.log())
        self.w.proc.wait(timeout=20)
        self.assertEqual(self.w.stop('cancel'), {'state': None})                    # given up: nothing is offered to carry on
        self.assertIsNone(self.w.status())
        with self.assertRaises(api.Refused):
            self.w.resume()
        self.assertTrue((self.tmp / 'out' / 'LastbookData').is_dir())               # what was built is the person's to delete

    def test_the_build_ends_when_the_app_is_gone(self):
        gone = subprocess.Popen([sys.executable, '-c', 'pass'])
        gone.wait()
        self.assertFalse(worker.alive(gone.pid))
        self.assertTrue(worker.alive(os.getpid()))
        self.assertTrue(worker.alive(None))
        folder = self.tmp / 'job'
        folder.mkdir()
        (folder / 'job.json').write_text(json.dumps({'target': str(self.tmp / 'out2' / 'LastbookData'), 'source': self.source, 'choice': dict(SMALL, documents='all'),
                                                      'effort': 'normal', 'parent': gone.pid}))
        done = subprocess.run([sys.executable, '-B', '-m', 'engine.wizard.worker', str(folder)], cwd=str(ROOT), capture_output=True, text=True, timeout=60)
        st = json.loads((folder / 'status.json').read_text())
        self.assertEqual((done.returncode, st['state']), (1, 'paused'), done.stderr + (folder / 'log.txt').read_text())

    def test_a_build_that_cannot_go_on_says_why(self):
        self.start()
        self.until(('done',))
        self.w.proc.wait(timeout=20)
        shutil.rmtree(self.tmp / 'pack')            # the library it was built from is not there any more
        with self.assertRaises(api.Refused):
            self.w.resume()
        folder = self.w.dir
        job = json.loads((folder / 'job.json').read_text())
        done = subprocess.run([sys.executable, '-B', '-m', 'engine.wizard.worker', str(folder)], cwd=str(ROOT), capture_output=True, text=True, timeout=60)
        st = json.loads((folder / 'status.json').read_text())
        self.assertEqual((done.returncode, st['state']), (1, 'failed'))
        self.assertIn('does not hold a pack', st['error'])
        self.assertEqual(job['parent'], os.getpid())

    def test_how_hard_the_computer_may_work(self):
        self.assertEqual([worker.threads_for(e, 10) for e in ('gentle', 'normal', 'full', 'nonsense')], [2, 6, 10, 6])
        self.assertEqual([worker.threads_for('normal', c) for c in (2, 4, 8, 16)], [2, 2, 4, 9])
        st = self.start(effort='sideways', rate='fast')
        self.assertEqual((st['job']['effort'], st['job']['rate']), ('normal', None))
        self.until(('done',))
        self.w.proc.wait(timeout=20)
        st = self.w.start(self.source, SMALL, str(self.tmp / 'out'), effort='gentle', rate='2.5')
        self.assertEqual((st['job']['effort'], st['job']['rate']), ('gentle', 2.5))
        self.until(('done',))

    def test_carried_on_after_its_library_was_opened(self):
        self.assertFalse(self.w.after_restart())            # nothing was left running
        self.start()
        self.until(('done',))
        self.w.proc.wait(timeout=20)
        self.assertEqual(self.w.reopen(), {'ok': True})     # not running: nothing to note
        self.assertNotIn('reopen', json.loads((self.w.dir / 'job.json').read_text()))
        with mock.patch.object(self.w, '_running', return_value=True):
            self.w.reopen()                                  # the person opens the library while it is still being built
        self.assertTrue(json.loads((self.w.dir / 'job.json').read_text())['reopen'])
        again = self.wizard()                                # the app, started again
        self.addCleanup(lambda: again.proc and again.proc.wait(timeout=20))
        with mock.patch.object(api.time, 'sleep'):
            self.assertTrue(again.after_restart())
        self.assertNotIn('reopen', json.loads((again.dir / 'job.json').read_text()))
        self.w = again
        self.assertEqual(self.until(('done', 'failed'))['state'], 'done')
        with mock.patch.object(api.time, 'sleep'):
            self.assertFalse(again.after_restart())          # once


class Asked(Base):
    """Over HTTP, as the page asks: in setup mode's own handler."""

    def setUp(self):
        super().setUp()
        p = mock.patch.object(api, 'WIZARD', self.w)
        p.start()
        self.addCleanup(p.stop)
        self.srv = http.server.ThreadingHTTPServer(('127.0.0.1', 0), setup_api.SetupHandler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.base = f'http://127.0.0.1:{self.srv.server_address[1]}'

    def ask(self, path, body=None, raw=None):
        data = raw if raw is not None else None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, headers={'Content-Type': 'application/json'} if data is not None else {})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, json.loads(r.read() or b'null') if 'json' in r.headers.get('Content-Type', '') else r.read()
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b'null')

    def test_the_page_and_its_questions(self):
        code, page = self.ask('/ui/wizard.html')
        self.assertEqual(code, 200)
        self.assertIn(b'/ui/wizard.js', page)
        code, st = self.ask('/api/wizard/state')
        self.assertEqual((code, st['build'], st['current']), (200, None, None))
        self.assertEqual(set(st['tools']), {'video', 'video_hardware', 'maps', 'ai_runtime'})
        self.assertEqual(self.ask('/api/wizard/status'), (200, {'state': None}))
        code, m = self.ask('/api/wizard/menu', {'source': self.source})
        self.assertEqual((code, len(m['documents'])), (200, 5))
        code, p = self.ask('/api/wizard/plan', {'source': self.source, 'choice': SMALL, 'target': str(self.tmp / 'out')})
        self.assertEqual((code, p['fits']), (200, True))
        code, r = self.ask('/api/wizard/regions', {})
        self.assertEqual((code, len(r['regions'])), (200, 4))
        self.assertEqual(self.ask('/api/wizard/pick', {}), (200, {'cancelled': True}))       # no window, no folder chooser

    def test_what_is_refused(self):
        self.assertEqual(self.ask('/api/wizard/nothing')[0], 404)
        self.assertEqual(self.ask('/api/wizard/state', {})[0], 404)                 # a question asked the wrong way
        self.assertEqual(self.ask('/api/wizard/plan', raw=b'[1,2]')[0], 400)
        self.assertEqual(self.ask('/api/wizard/plan', raw=b'{not json')[0], 400)
        self.assertEqual(self.ask('/api/wizard/plan', raw=b'{"x":"' + b'y' * api.MAX_BODY + b'"}')[0], 413)
        code, e = self.ask('/api/wizard/target', {'path': ''})
        self.assertEqual((code, e), (400, {'error': 'Choose a folder or a drive first.'}))
        code, e = self.ask('/api/wizard/resume', {})
        self.assertEqual((code, e['error']), (400, 'There is no build to carry on.'))
        with mock.patch.object(self.w, 'state', side_effect=RuntimeError('secret path /Users/someone')), mock.patch.object(api.traceback, 'print_exc'):
            code, e = self.ask('/api/wizard/state')
        self.assertEqual((code, e), (500, {'error': 'The builder stopped on an internal error (RuntimeError).'}))       # what went wrong, not what was in it

    def test_only_this_computer(self):
        class Phone:
            client_address = ('192.168.1.20', 5555)
            command = 'GET'
            headers = {}
            sent = []

            def send_response(self, code):
                self.sent.append(code)

            def send_header(self, *a):
                pass

            def end_headers(self):
                pass

            class wfile:
                body = b''

                @classmethod
                def write(cls, b):
                    cls.body += b
        for method, path in (('GET', '/api/wizard/state'), ('POST', '/api/wizard/start'), ('POST', '/api/wizard/stop')):
            Phone.sent.clear()
            Phone.wfile.body = b''
            api.handle(Phone(), method, path)
            self.assertEqual(Phone.sent, [403], path)
            self.assertIn(b'only on the computer', Phone.wfile.body)

    def test_a_build_started_and_watched_from_the_page(self):
        code, st = self.ask('/api/wizard/start', {'source': self.source, 'choice': SMALL, 'target': str(self.tmp / 'out'), 'effort': 'gentle'})
        self.assertEqual(code, 200, st)
        st = self.until(('done', 'failed'))
        self.assertEqual(st['state'], 'done', self.log())
        code, st = self.ask('/api/wizard/status')
        self.assertEqual((code, st['state'], st['phase']), (200, 'done', 'done'))
        code, st = self.ask('/api/wizard/stop', {'how': 'cancel'})                  # "build another": the finished one is put away
        self.assertEqual((code, st), (200, {'state': None}))


if __name__ == '__main__':
    unittest.main(verbosity=1)
